import asyncio
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Optional, Tuple

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models.user import User, PlanType
from ..db.session import get_db, init_db
from ..db.repository import UserRepository
from ..utils.session import SessionManager

logger = logging.getLogger(__name__)
settings = get_settings()

PBKDF2_ITERATIONS = 600_000
LEGACY_PBKDF2_ITERATIONS = 100_000
SALT_BYTES = 16
UNLIMITED_GENERATIONS = -1

# --- パスワードポリシー ---------------------------------------------------------
# NIST SP 800-63B「Building Blocks」: 最小長は 8 文字を推奨し、
# 大文字/小文字/数字/記号の混在（composition rules）はmodernには非推奨。
# 最大長は 64 文字以上を推奨する。PBKDF2-HMAC-SHA256 に 72 バイトの切り詰めは
# 無いが、巨大入力を通すと 1 リクエストあたりの計算量を攻撃者が増やせるため上限を設ける。
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256

# --- 総当たり攻撃対策のデフォルト ------------------------------------------------
LOGIN_MAX_FAILURES = 5
LOGIN_BACKOFF_BASE_SECONDS = 1.0
LOGIN_BACKOFF_MAX_SECONDS = 60.0
LOGIN_BACKOFF_WINDOW_SECONDS = 900.0

# ユーザー不在時にも「パスワード不一致」と同量の PBKDF2 を走らせるためのダミーハッシュ。
# 固定 salt + ゼロダイジェストなので、実ユーザーのデータは一切含まれない。
# 16進 salt を持つため _verify_password は「現行失敗」と同じ 600,000 + 100,000 回を消費し、
# 「既存ユーザーの誤パスワード」と「未登録 email」の計算量が一致する。
_DUMMY_SALT_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
_DUMMY_HASHED_PASSWORD = f"{'0' * 64}:{_DUMMY_SALT_HEX}"


def _salt_bytes(salt: str) -> Optional[bytes]:
    try:
        return bytes.fromhex(salt)
    except ValueError:
        return None

def _pbkdf2(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations).hex()


def hash_password(password: str) -> str:
    """パスワードをハッシュ化（PBKDF2-SHA256 600,000回・16バイトのランダムソルト）"""
    salt = secrets.token_bytes(SALT_BYTES)
    return f"{_pbkdf2(password, salt, PBKDF2_ITERATIONS)}:{salt.hex()}"

def _verify_password(password: str, hashed: str) -> tuple[bool, bool]:
    """(一致したか, 新強度への再ハッシュが必要か) を返す。旧フォーマットは後方互換で検証する"""
    if not hashed or ":" not in hashed:
        return False, True
    hash_val, _, salt = hashed.partition(":")
    if not hash_val or not salt:
        return False, True
    salt_bin = _salt_bytes(salt)
    expected = hash_val.encode('utf-8')
    if salt_bin is not None:
        if hmac.compare_digest(_pbkdf2(password, salt_bin, PBKDF2_ITERATIONS).encode('ascii'), expected):
            return True, False
    if hmac.compare_digest(_pbkdf2(password, salt.encode('utf-8'), LEGACY_PBKDF2_ITERATIONS).encode('ascii'), expected):
        return True, True
    return False, True

def verify_password(password: str, hashed: str) -> bool:
    """ハッシュ化されたパスワードを検証（定数時間比較）"""
    return _verify_password(password, hashed)[0]


def validate_password_strength(password: str) -> Optional[str]:
    """パスワードポリシーに違反していれば違反理由を返す。通れば None。

    方針は NIST SP 800-63B に従い「長さ」だけを扱う。
    - 最小 8 文字
    - 最大 256 文字（巨大入力による計算量 DoS の防止）
    - 文字種（大文字/小文字/数字/記号）の混在は要求しない
    - 前後空白はトリムしない（パスワードの一部として扱う）
    """
    if not password:
        return "パスワードを入力してください"
    length = len(password)
    if length < MIN_PASSWORD_LENGTH:
        return f"パスワードは{MIN_PASSWORD_LENGTH}文字以上で入力してください"
    if length > MAX_PASSWORD_LENGTH:
        return f"パスワードは{MAX_PASSWORD_LENGTH}文字以下で入力してください"
    return None


def throttle_key(email: str, ip_address: str = "") -> Tuple[str, str]:
    """スロットリングの記録キー（email と IP の組）"""
    return (str(email).strip().lower(), str(ip_address or "").strip())


class LoginThrottle:
    """連続失敗の記録と指数バックオフ。

    **プロセス内メモリ実装であり、スレッド安全性のため単一の `threading.RLock`
    で全ての状態（`_records`）を保護している。**
    ロックは 1 本しかなく、ロック保持中に別のロックを取得する経路は存在しない
    ため、ロック順序によるデッドロックは起こり得ない。

    複数ワーカー/複数プロセスで動かす本番では各ワーカーが別のカウンタを持つため、
    攻撃者はプロセス数だけ試行を割り振れる。**本番では Redis / DB の
    排他的カウンタ（`SET NX EX` 相当）へ置き換えること。**
    置き換え時は `record_failure` / `record_success` / `delay_for` / `reset` の
    4 メソッドだけを実装すれば `Authenticator` 側は変更不要。

    ### メモリ上限と「キーを撒く」攻撃への耐性
    上限超過時に**全レコードを消す**と、攻撃者は 1 万個の別キー（＝別 email 文字列）を
    撒くだけで、攻撃対象アカウントの記録を消してバックオフを 0 に戻せてしまう。
    そのため:

    - 期限切れの掃除は全走査だが、消えるのは**窓を過ぎた記録だけ**。
    - 上限超過時は**古いものから最大 `EVICT_BATCH` 件ずつ**追い出す（一斉には消さない）。
    - 追い出しから**除外するキー**が 2 種類ある:
      (1) いま記録しているキー（`record_failure` / `delay_for` が `protect` で渡す）。
      (2) 既に `max_failures` 回失敗し、**バックオフが掛かり始めているキー**。
      (2) を外さないのは、攻撃対象アカウントの記録が撒鍵攻撃の시에消えるのを防ぐため。
    - 上限まで埋まっている状態で**新規キー**が来ても、既存記録を捨ててまで
      追跡はしない。**新規キーは記録せず fail-closed**（`delay_for` は
      `max_delay` を返す）。記録済みのキーは通常どおりに段階的に増える。

    """

    #: 記録を保持するキー数の上限。
    MAX_KEYS = 10_000
    #: 1 キーあたりに保持する失敗タイムスタンプ数の上限（メモリの横方向の上限）。
    MAX_STAMPS_PER_KEY = 64
    #: 上限超過時に 1 回に追い出すキー数。
    EVICT_BATCH = 256

    def __init__(
        self,
        max_failures: int = LOGIN_MAX_FAILURES,
        base_delay: float = LOGIN_BACKOFF_BASE_SECONDS,
        max_delay: float = LOGIN_BACKOFF_MAX_SECONDS,
        window: float = LOGIN_BACKOFF_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_KEYS,
    ):
        self.max_failures = max(int(max_failures), 0)
        self.base_delay = max(float(base_delay), 0.0)
        self.max_delay = max(float(max_delay), 0.0)
        self.window = float(window)
        self.max_keys = max(int(max_keys), 1)
        self._clock = clock
        self._records: "OrderedDict[Tuple[str, str], Deque[float]]" = OrderedDict()
        # 1 本のロックで全状態を守る（ロック順序＝1 段、デッドロックの余地なし）。
        self._lock = threading.RLock()

    # --- context manager（呼び出し側の便宜。状態は保持しない）--------------------
    def __enter__(self) -> "LoginThrottle":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def _prune(self, now: float, protect: Optional[Tuple[str, str]] = None) -> None:
        """期限切れを落とし、上限超過なら**保護対象を除いて**批量で追い出す。

        全レコードを一気に消すことはしない。消されるのは (a) 窓を過ぎた記録と
        (b) 上限超過分を古い順に最大 `EVICT_BATCH` 件だけ、という 2 つに限られる。

        追い出しは `protect`（いま記録しているキー）と、既に `max_failures` 回失敗して
        **バックオフ中のキー**をスキップする。それら以外が尽きた場合は上限を
        少し超えたままにして終了する（新規の攻撃者より既存の被害者を優先 =
        拒否側に倒す判断）。
        """
        for key, stamps in list(self._records.items()):
            while stamps and now - stamps[0] > self.window:
                stamps.popleft()
            if not stamps:
                del self._records[key]

        overflow = len(self._records) - self.max_keys
        if overflow <= 0:
            return
        evicted = 0
        for key in list(self._records.keys()):
            if evicted >= min(overflow, self.EVICT_BATCH):
                break
            if key == protect or len(self._records[key]) >= self.max_failures:
                continue
            del self._records[key]
            evicted += 1

    def is_saturated(self) -> bool:
        """記録上限に達し、**新規キー**を記録できない状態か。

        上限超過の「一時的な満杯」ではない。`MAX_KEYS` 件の記録が
        すべて窓の**内側**に残っている状態だけを指す。
        """
        with self._lock:
            self._prune(self._clock())
            return len(self._records) >= self.max_keys

    def record_failure(self, key: Tuple[str, str]) -> int:
        """失敗を1件記録し、連続失敗回数を返す。

        上限到達中で**新規キー**のときは記録しない（既存記録を消してまで
        追跡を保つことはせず、拒否側に倒す）。返り値は 0。
        """
        now = self._clock()
        with self._lock:
            self._prune(now, protect=key)
            stamps = self._records.get(key)
            if stamps is None:
                if len(self._records) >= self.max_keys:
                    logger.warning(
                        "ログイン失敗記録が上限(%dキー)に達したため新規キーの記録を拒否します",
                        self.max_keys,
                    )
                    return 0
                stamps = deque()
                self._records[key] = stamps
            if len(stamps) < self.MAX_STAMPS_PER_KEY:
                stamps.append(now)
            else:
                # 上限に達していれば、最も古い 1 件を捨てて**最新**の 1 件を
                # 差し替える。件数は増やさず、記録中のキーは消さない。
                stamps[0] = now
            # 記録されたキーを最新側へ（追い出し候補の最後尾から逃がす）。
            self._records.move_to_end(key)
            return len(stamps)

    def record_success(self, key: Tuple[str, str]) -> None:
        """成功したら記録を消す（1回の認証成功で連続失敗はリセットされる）"""
        with self._lock:
            self._records.pop(key, None)

    def failure_count(self, key: Tuple[str, str]) -> int:
        """現在の連続失敗回数（期限切れは数えない）"""
        now = self._clock()
        with self._lock:
            self._prune(now, protect=key)
            return len(self._records.get(key, ()))

    def check(self, key: Tuple[str, str]) -> float:
        """`delay_for` の別名（呼び出し側の意図が伝わるように持つ）。"""
        return self.delay_for(key)

    def delay_for(self, key: Tuple[str, str]) -> float:
        """次の試行までに挿入すべき遅延秒数。閾値未満なら 0.0。

        `max_failures` 回までは 0。`max_failures` 回目の失敗が決まった「次の試行」は
        `base_delay * 2 ** 0`、以降 n 回目は `base_delay * 2 ** (n - max_failures)`。
        上限は `max_delay`。既存ユーザーの応答時間を意図的に遅くはしない。

        記録上限に達している状態で**未知のキー**の遅延を要求された場合は、
        新規キーを記録できない以上閾値を判定できないため、**上限遅延**を返す
        （= 拒否）。記録済みのキーは通常のまま。
        """
        now = self._clock()
        with self._lock:
            self._prune(now, protect=key)
            stamps = self._records.get(key)
            if stamps is None:
                if len(self._records) >= self.max_keys:
                    return self.max_delay
                return 0.0
            count = len(stamps)
        if count < self.max_failures:
            return 0.0
        exponent = min(count - self.max_failures, 30)
        return min(self.base_delay * (2 ** exponent), self.max_delay)

    def reset(self) -> None:
        """全記録を破棄する（テスト隔離・障害時の手動解除用）"""
        with self._lock:
            self._records.clear()


# プロセス共有の既定インスタンス。Authenticator を毎回作り直しても攻撃者の回避-workersに
# pletsれないよう、記録は Authenticator インスタンスではなくプロセス単位で持つ。
_LOGIN_THROTTLE = LoginThrottle()


def reset_login_throttle() -> None:
    """プロセス共有のログイン失敗記録を破棄する（運用・テスト用）"""
    _LOGIN_THROTTLE.reset()

class Authenticator:
    def __init__(
        self,
        db: Optional[Session] = None,
        throttle: Optional[LoginThrottle] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ):
        try:
            init_db()
        except Exception as e:
            logger.warning(f"DBの初期化に失敗しました（認証はDB必須のため利用できません）: {e}")
        self.db = db
        self.user_repo = UserRepository(db) if db is not None else None
        self.session_mgr = SessionManager()
        # throttle=None ならプロセス共有の既定を使う（失敗記録が Authenticator ごとに
        # 分けると、毎リクエストで Authenticator を作り直す運用では無意味になる）。
        self._throttle = throttle if throttle is not None else _LOGIN_THROTTLE
        # sleeper はバックオフ待機の差し替え点。テストでは time.sleep ではなく記録器を渡し、
        # スレッドをブロックせずに検証する。
        self._sleeper = sleeper if sleeper is not None else time.sleep

    @contextmanager
    def _repo(self):
        """1操作分のリポジトリを開く。DBセッションは操作単位に作り、必ず解放する"""
        if self.db is not None:
            yield self.user_repo if self.user_repo is not None else UserRepository(self.db)
            return
        with get_db() as db:
            yield UserRepository(db)

    def _commit(self) -> None:
        """外部セッションの利用時は自前でコミットする（get_db() 利用時は自動コミット）"""
        if self.db is not None:
            self.db.commit()

    def _hash_password(self, password: str) -> str:
        return hash_password(password)

    def _save_user(self, user: User) -> None:
        """DB上のユーザー情報を更新する（認証状態はDBのみが正）"""
        if user is None or not getattr(user, "id", None):
            return
        try:
            with self._repo() as repo:
                repo.update(user)
            self._commit()
        except Exception as e:
            logger.error(f"ユーザー情報の保存に失敗しました: {e}")

    def _get_user_by_email(self, email: str) -> Optional[User]:
        try:
            with self._repo() as repo:
                return repo.get_by_email(email)
        except Exception as e:
            logger.error(f"ユーザー検索に失敗しました: {e}")
            return None

    def _get_user_by_id(self, user_id: str) -> Optional[User]:
        try:
            with self._repo() as repo:
                return repo.get_by_id(user_id)
        except Exception as e:
            logger.error(f"ユーザー検索に失敗しました: {e}")
            return None

    def get_current_user(self) -> Optional[User]:
        """現在のユーザーを取得（実行コンテキスト単位）"""
        return self.session_mgr.get_user()

    def login_user(self, user: User):
        """ユーザーをログイン状態に設定"""
        self.session_mgr.set_user(user)

    def logout(self):
        """ユーザーをログアウト状態に設定"""
        self.session_mgr.clear_user()

    def _reset_generation_period(self, user: User) -> None:
        """月次リセット期を過ぎていればカウンタをリセットして保存する"""
        if user is None or not user.generation_reset_at:
            return
        now = datetime.now(timezone.utc)
        reset_at = user.generation_reset_at
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        if now < reset_at:
            return
        user.generation_count = 0
        user.generation_reset_at = now + timedelta(days=30)
        user.updated_at = datetime.now()
        self._save_user(user)

    def check_can_generate(self, user: User) -> tuple[bool, int]:
        """生成可能かチェックし、可能なら残りの回数を返す（月間生成制限は設けない）"""
        if user is not None:
            self._reset_generation_period(user)
        return True, UNLIMITED_GENERATIONS

    def increment_generation_count(self, user: User):
        """生成回数をインクリメント（統計用途。権限付与には使わない）"""
        if user is None:
            return
        user.generation_count = (user.generation_count or 0) + 1
        user.updated_at = datetime.now()
        self._save_user(user)
        self.login_user(user)

    def get_user_plan(self, user: User) -> PlanType:
        """ユーザーのプランを取得"""
        return user.plan

    def upgrade_to_premium(self, user: User, plan: PlanType, cycle=None) -> str:
        """アップグレード（決済）URLを返す。プラン変更は決済完了 webhook 経由でのみ行う"""
        cycle_value = getattr(cycle, "value", "") or ""
        return f"{settings.app_url}/upgrade?plan={plan.value}&cycle={cycle_value}"

    def is_feature_enabled(self, user: User, feature: str) -> bool:
        """機能が有効かどうかをチェック"""
        if not user:
            return False

        plan = user.plan
        feature_map = {
            "unlimited_generations": True,
            "high_quality_audio": plan in [PlanType.PREMIUM, PlanType.PRO],
            "history_export": plan in [PlanType.PREMIUM, PlanType.PRO],
            "favorites": plan in [PlanType.PREMIUM, PlanType.PRO],
            "api_access": plan == PlanType.PRO,
            "batch_generation": plan == PlanType.PRO,
            "no_ads": plan in [PlanType.PREMIUM, PlanType.PRO],
        }
        return feature_map.get(feature, False)

    def _replace_password(self, user: User, hashed_password: str) -> None:
        """旧強度のパスワードハッシュを新強度へ移行して保存する"""
        try:
            user.hashed_password = hashed_password
            with self._repo() as repo:
                repo.update(user)
            self._commit()
        except Exception as e:
            logger.error(f"パスワードの再ハッシュに失敗しました: {e}")

    # --- 総当たり攻撃対策 ---------------------------------------------------------
    def throttle_delay(self, email: str, ip_address: str = "") -> float:
        """この(email, IP)の次の試行までの遅延秒数を返す。

        HTTP レイヤーはここを先に読んで 429 を返したり、`login_async` に渡して
        `asyncio.sleep` させたりできる。
        """
        return self._throttle.delay_for(throttle_key(email, ip_address))

    def login_failure_count(self, email: str, ip_address: str = "") -> int:
        """この(email, IP)の現在の連続失敗回数"""
        return self._throttle.failure_count(throttle_key(email, ip_address))

    def reset_login_throttle(self) -> None:
        """この Authenticator が使う失敗記録を破棄する"""
        self._throttle.reset()

    def signup(self, email: str, password: str) -> tuple[bool, str, Optional[User]]:
        """プログラム経由で新規登録（DBを利用できない場合は必ず失敗を返す）"""
        email_clean = str(email).strip().lower()
        if not email_clean or "@" not in email_clean:
            return False, "メールアドレスの形式が正しくありません", None
        weakness = validate_password_strength(password)
        if weakness is not None:
            return False, weakness, None
        hashed = hash_password(password)
        try:
            with self._repo() as repo:
                if repo.get_by_email(email_clean):
                    return False, "このメールアドレスは既に登録されています", None
                user = repo.create(email_clean, hashed)
            self._commit()
        except Exception as e:
            logger.error(f"新規登録に失敗しました: {e}")
            return False, "現在ユーザーを登録できません。時間をおいて再度お試しください。", None
        self.login_user(user)
        return True, "登録が完了しました", user

    def login(
        self,
        email: str,
        password: str,
        ip_address: str = "",
        wait: bool = True,
    ) -> Optional[User]:
        """プログラム経由でログイン（DBを利用できない場合は必ず None を返す）

        ユーザー不在・パスワード不一致のどちらでも PBKDF2 を必ず1回走らせる。
        これにより応答時間からメールアドレスの登録有無を推測できないようにする。

        Args:
            ip_address: スロットリングの記録に使う送信元 IP（取れない場合は空文字）
            wait: False ならバックオフを待たない。非同期呼び出しは
                `login_async` を使うか、HTTP レイヤーで 429 を返すこと。
        """
        email_clean = str(email).strip().lower()
        if not email_clean or not password:
            # 入力不備は登録の有無に依存しないので、PW検証を省略しても列挙オラクルは出ない
            return None
        if len(password) > MAX_PASSWORD_LENGTH:
            # 巨大入力の計算量を放置しない。既存アカウントを締め出さずに DoS だけ防ぐため
            # 最小長は login では課さない（旧形式の短いパスワードを締め出さないため）。
            return None

        key = throttle_key(email_clean, ip_address)
        delay = self._throttle.delay_for(key)
        if delay > 0 and wait:
            self._sleeper(delay)

        try:
            with self._repo() as repo:
                user = repo.get_by_email(email_clean)
        except Exception as e:
            logger.error(f"ログイン処理に失敗しました: {e}")
            return None

        if user is None:
            # タイミングオラクル対策: ここで return すると PBKDF2 に到達しないため、
            # 「ユーザー不在」側でも「パスワード不一致」と同量の計算量を発生させる。
            _verify_password(password, _DUMMY_HASHED_PASSWORD)
            self._throttle.record_failure(key)
            return None

        matched, needs_rehash = _verify_password(password, user.hashed_password)
        if not matched:
            self._throttle.record_failure(key)
            return None
        if needs_rehash:
            self._replace_password(user, hash_password(password))
        self._throttle.record_success(key)
        self.login_user(user)
        return user

    async def login_async(
        self,
        email: str,
        password: str,
        ip_address: str = "",
    ) -> Optional[User]:
        """非同期版。バックオフを `asyncio.sleep` で待つためスレッドをブロックしない。

        パスワード検証自体は `hashlib` を使うため同期のまま（この1回だけは避けられない）。
        ブロックする価値があるのは「攻撃者の待ち時間」側だけなので、そこだけを非同期化する。
        """
        delay = self._throttle.delay_for(throttle_key(str(email).strip().lower(), ip_address))
        if delay > 0:
            await asyncio.sleep(delay)
        return self.login(email, password, ip_address=ip_address, wait=False)
