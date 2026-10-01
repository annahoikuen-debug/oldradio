"""認証セキュリティのリグレッション防止テスト。

本ファイルは「一度直した不具合が復活していないこと」を固定するためのものである。
内部実装への結合を避け、公開 API でのみ観測する。

Fixed in this wave:
1. login() のタイミングオラクル（ユーザー列挙）
2. 総当たり攻撃のスロットリング / バックオフ
3. パスワードポリシー（最小長・最大長）
4. CORS の `origins=["*"]` + `allow_credentials=True` 組み合わせの拒否
5. secret_key 未設定の検出（認証機能が使えない状態の明示化）

既存の後方互換（PBKDF2 旧フォーマット検証 / transparent rehash / 無償アップグレードは
プランを変えない）も併せて回帰として固定する。
"""

import asyncio
import hashlib
import json
import secrets
import statistics
import time

import pytest
from pydantic import ValidationError

from retro_radio.auth import authenticator as auth_mod
from retro_radio.auth.authenticator import (
    LEGACY_PBKDF2_ITERATIONS,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    SALT_BYTES,
    Authenticator,
    LoginThrottle,
    hash_password,
    verify_password,
)
from retro_radio.config import ConfigurationError, Settings, parse_cors_origins
from retro_radio.db.repository import UserRepository
from retro_radio.models.user import PlanType

VALID_PASSWORD = "correct-password"


# --- テスト用の下準備 -----------------------------------------------------------
class RecordingSleeper:
    """`time.sleep` の差し替え用。呼び出した秒数を記録するだけで実際には待たない。"""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)


@pytest.fixture
def throttle():
    """テストごとに独立したスロットリング状態（プロセスグローバルを汚さない）"""
    return LoginThrottle(max_failures=5, base_delay=1.0, max_delay=60.0)


@pytest.fixture
def sleeper():
    return RecordingSleeper()


@pytest.fixture
def auth(db_session, throttle, sleeper):
    """独立した Authenticator（バックオフ待機を記録するだけでブロックしない）"""
    return Authenticator(db=db_session, throttle=throttle, sleeper=sleeper)


def _legacy_hash(password: str) -> str:
    """旧フォーマット（100,000回 + salt.encode()）のハッシュを再現する"""
    salt = secrets.token_bytes(SALT_BYTES).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), LEGACY_PBKDF2_ITERATIONS
    ).hex()
    return f"{digest}:{salt}"


def _spy_on_verify(monkeypatch) -> list:
    """`_verify_password` の呼び出し回数を数えるスパイを差し込む"""
    calls: list = []
    real = auth_mod._verify_password

    def spy(password, hashed):
        calls.append(hashed)
        return real(password, hashed)

    monkeypatch.setattr(auth_mod, "_verify_password", spy)
    return calls


# --- 1. タイムインオラクルの回帰防止 ---------------------------------------------
def test_login_verifies_password_even_when_email_is_unregistered(db_session, auth, monkeypatch):
    """構造テスト: ユーザー不在でも PBKDF2 検証が必ず1回走る（列挙オラクル防止）

    これが落ちている = 「未登録メールは 0.002ms で返る」時代のまま。
    """
    calls = _spy_on_verify(monkeypatch)

    assert auth.login("nobody@example.com", "some-password") is None

    assert len(calls) == 1, "ユーザー不在時にパスワード検証が1回も走っていない（列挙可能性）"


def test_login_verifies_password_exactly_once_in_every_outcome(db_session, auth, monkeypatch):
    """不成功・不登録・成功のどの経路でも検証はちょうど1回（計算量が揃っている）"""
    _ok, _msg, _user = auth.signup("spy@example.com", VALID_PASSWORD)

    for email, password, should_succeed in (
        ("spy@example.com", "wrong-password", False),
        ("unknown@example.com", "wrong-password", False),
        ("spy@example.com", VALID_PASSWORD, True),
    ):
        calls = _spy_on_verify(monkeypatch)
        result = auth.login(email, password)
        monkeypatch.undo()
        assert len(calls) == 1, f"{email}: 検証回数が {len(calls)} 回"
        assert (result is not None) is should_succeed


def test_login_dummy_hash_is_not_a_real_user_hash(db_session, auth, monkeypatch):
    """ダミー検証が使うハッシュは固定・空ダイジェストで、実ユーザーのデータではない"""
    calls = _spy_on_verify(monkeypatch)

    auth.login("nobody-at-all@example.com", "whatever-password")

    assert calls, "ダミー検証が走っていない"
    dummy = calls[0]
    assert dummy != "whatever-password"
    hash_part, sep, salt_part = dummy.partition(":")
    assert sep == ":" and len(hash_part) == 64 and len(salt_part) == 32
    assert int(hash_part, 16) == 0, "ダミーのダイジェストが0でない（実データの漏えい）"


def test_login_response_time_ratio_is_bounded(db_session, auth):
    """既存 email と未登録 email の応答時間比が緩い閾値内に収まる（参考値・構造テストの補強）

    PBKDF2 600,000回で1試行あたり数百msかかるため median を採り、
    閾値は実測比（~1.0）に対して大きく取った。回帰時は 100 倍以上の差になる。
    """
    _ok, _msg, _user = auth.signup("timing@example.com", VALID_PASSWORD)
    samples = 5

    def _measure(email: str) -> float:
        durations = []
        for _ in range(samples):
            started = time.perf_counter()
            assert auth.login(email, "wrong-password") is None
            durations.append(time.perf_counter() - started)
        return statistics.median(durations)

    known = _measure("timing@example.com")
    unknown = _measure("not-registered@example.com")

    ratio = known / unknown if unknown > 0 else float("inf")
    assert ratio <= 5.0, (
        f"タイミングオラクルの回帰: 既存 {known * 1000:.1f}ms / 未登録 "
        f"{unknown * 1000:.1f}ms = {ratio:.1f} 倍"
    )


# --- 2. パスワードポリシー --------------------------------------------------------
@pytest.mark.parametrize("weak", ["", "a", "123", "1234567"])
def test_signup_rejects_weak_passwords(db_session, auth, weak):
    """最小長を下回るパスワードは登録できない（NIST SP 800-63B: 8文字以上）"""
    ok, message, user = auth.signup(f"weak-{abs(hash(weak))}@example.com", weak)

    assert ok is False
    assert user is None
    assert message
    assert weak not in message or not weak.strip(), "拒否メッセージに平文パスワードを混ぜている"


def test_signup_accepts_password_at_minimum_length(db_session, auth):
    """ちょうど最小長なら登録できる"""
    password = "a" * MIN_PASSWORD_LENGTH
    ok, _message, user = auth.signup("min@example.com", password)

    assert ok is True
    assert user is not None
    assert verify_password(password, user.hashed_password) is True


def test_signup_rejects_password_over_maximum_length(db_session, auth):
    """最大長を超えるパスワードは拒否する（巨大入力による計算量DoSの防止）"""
    ok, _message, user = auth.signup("huge@example.com", "a" * (MAX_PASSWORD_LENGTH + 1))

    assert ok is False
    assert user is None


def test_signup_rejects_oversized_password_without_running_pbkdf2(db_session, auth):
    """上限超過パスワードは PBKDF2 を1回も回さず即座に拒否する（DoS 回帰防止）"""
    monster = "a" * 5_000_000

    started = time.perf_counter()
    ok, _message, _user = auth.signup("monster@example.com", monster)
    elapsed = time.perf_counter() - started

    assert ok is False
    assert elapsed < 1.0, f"巨大パスワードの処理に {elapsed:.2f} 秒かかった（計算量DoS）"


def test_signup_accepts_japanese_password(db_session, auth):
    """日本語パスワードは文字種ルールを持たないので受理される"""
    password = "ラジオの時間の旅人"

    ok, _message, user = auth.signup("jp@example.com", password)

    assert len(password) >= MIN_PASSWORD_LENGTH
    assert ok is True
    assert user is not None
    assert verify_password(password, user.hashed_password) is True


@pytest.mark.parametrize("composition", ["abcdefgh", "!!!!!!!!", "        ", "12345678"])
def test_password_policy_does_not_enforce_composition_rules(db_session, auth, composition):
    """大文字小文字数字記号の混在を要求しない（NIST SP 800-63B: composition rules は非推奨）"""
    ok, _message, user = auth.signup(f"comp-{abs(hash(composition))}@example.com", composition)

    assert ok is True, f"{composition!r} を拒否した（旧来の composition rule が残っている）"
    assert user is not None


def test_login_rejects_overlong_password_without_pbkdf2(db_session, auth):
    """ログイン時も上限超過は即座に拒否する（既存アカウントを締め出さずにDoSだけ防ぐ）"""
    _ok, _msg, _user = auth.signup("dos@example.com", VALID_PASSWORD)

    started = time.perf_counter()
    result = auth.login("dos@example.com", "b" * 100_000)
    elapsed = time.perf_counter() - started

    assert result is None
    assert elapsed < 1.0, f"巨大パスワードの照合に {elapsed:.2f} 秒かかった"


# --- 3. 総当たり攻撃のスロットリング ----------------------------------------------
def test_login_does_not_throttle_before_threshold(db_session, auth, sleeper):
    """閾値（5回）までは遅延しない"""
    _ok, _msg, _user = auth.signup("throttle@example.com", VALID_PASSWORD)

    for _ in range(5):
        assert auth.login("throttle@example.com", "wrong-password") is None

    assert sleeper.calls == [], f"閾値内で遅延が発生した: {sleeper.calls}"


def test_login_throttles_after_repeated_failures(db_session, auth, sleeper):
    """5回連続失敗の次の試行から遅延が入る"""
    _ok, _msg, _user = auth.signup("brute@example.com", VALID_PASSWORD)

    for _ in range(5):
        auth.login("brute@example.com", "wrong-password")

    auth.login("brute@example.com", "wrong-password")

    assert sleeper.calls, "連続失敗しても遅延が入らない（総当たり攻撃が無防備）"
    assert sleeper.calls[0] > 0


def test_login_backoff_grows_exponentially_and_is_capped(db_session, auth, sleeper):
    """バックオフは 1s, 2s, 4s ... と増え、上限（60s）を超えない"""
    _ok, _msg, _user = auth.signup("grow@example.com", VALID_PASSWORD)

    for _ in range(12):
        auth.login("grow@example.com", "wrong-password")

    delays = sleeper.calls
    assert len(delays) >= 3
    assert delays[0] < delays[1] < delays[2], f"指数的に増えていない: {delays[:3]}"
    assert max(delays) <= 60.0, f"バックオフ上限を超えている: {max(delays)}"
    assert delays[-1] == pytest.approx(60.0), "上限で頭打ちになっていない"


def test_login_throttle_resets_after_success(db_session, auth, sleeper):
    """正しいパスワードでのログインが失敗カウントをリセットする"""
    _ok, _msg, _user = auth.signup("reset@example.com", VALID_PASSWORD)

    for _ in range(4):
        auth.login("reset@example.com", "wrong-password")
    assert auth.login("reset@example.com", VALID_PASSWORD) is not None

    for _ in range(5):
        assert auth.login("reset@example.com", "wrong-password") is None
    assert sleeper.calls == [], "成功後もバックオフが消えていない"

    auth.login("reset@example.com", "wrong-password")
    assert sleeper.calls, "リセット後は再び閾値から数え直す"


def test_login_throttle_is_independent_per_email(db_session, auth, sleeper):
    """ある email の失敗が別の email を締め出さない"""
    _ok, _msg, _user = auth.signup("victim-a@example.com", VALID_PASSWORD)
    _ok, _msg, _user = auth.signup("victim-b@example.com", VALID_PASSWORD)

    for _ in range(8):
        auth.login("victim-a@example.com", "wrong-password")
    assert sleeper.calls, "前提:  victim-a は遅延に入っている"

    sleeper.calls.clear()
    assert auth.login("victim-b@example.com", VALID_PASSWORD) is not None
    assert sleeper.calls == [], "別 email まで巻き込んだスロットリング"


def test_login_throttle_is_independent_per_ip(db_session, throttle, sleeper):
    """同じ email でも IP が違えば独立に数える"""
    session = db_session
    Authenticator(db=session, throttle=throttle).signup("multi-ip@example.com", VALID_PASSWORD)

    for _ in range(8):
        Authenticator(db=session, throttle=throttle, sleeper=sleeper).login(
            "multi-ip@example.com", "wrong-password", ip_address="10.0.0.1"
        )
    assert sleeper.calls, "同一IPの連続失敗で遅延が入っていない"

    sleeper.calls.clear()
    fresh = Authenticator(db=session, throttle=throttle, sleeper=sleeper)
    assert fresh.login("multi-ip@example.com", VALID_PASSWORD, ip_address="10.0.0.9") is not None
    assert sleeper.calls == [], "別IPが同じ email のロックアウトを受けた"


def test_login_can_skip_wait_for_async_callers(db_session, auth, sleeper):
    """`wait=False` はスレッドをブロックしない（HTTP レイヤーが自前で待機する用途）"""
    _ok, _msg, _user = auth.signup("nowait@example.com", VALID_PASSWORD)
    for _ in range(6):
        auth.login("nowait@example.com", "wrong-password")

    assert sleeper.calls, "前提: 遅延が設定されている"
    sleeper.calls.clear()

    auth.login("nowait@example.com", "wrong-password", wait=False)

    assert sleeper.calls == [], "wait=False でもブロックしている"


def test_login_async_awaits_instead_of_blocking(db_session, auth, sleeper):
    """`login_async` は `time.sleep` を使わず asyncio 側で待機する"""
    _ok, _msg, _user = auth.signup("async@example.com", VALID_PASSWORD)
    for _ in range(6):
        auth.login("async@example.com", "wrong-password")
    expected_delay = auth.throttle_delay("async@example.com")
    assert expected_delay > 0
    sleeper.calls.clear()

    async def run():
        started = time.perf_counter()
        result = await auth.login_async("async@example.com", VALID_PASSWORD)
        return result, time.perf_counter() - started

    result, elapsed = asyncio.run(run())

    assert result is not None
    assert sleeper.calls == [], "非同期経路でも time.sleep でスレッドをブロックしている"
    assert elapsed >= expected_delay * 0.8, (
        f"非同期待機が効いていない: elapsed={elapsed:.3f}s expected={expected_delay:.3f}s"
    )


def test_login_throttle_delay_is_observable_without_login(db_session, auth):
    """HTTP レイヤーは `throttle_delay` を見て自分で 429 を返せる"""
    _ok, _msg, _user = auth.signup("observe@example.com", VALID_PASSWORD)

    assert auth.throttle_delay("observe@example.com") == 0.0
    for _ in range(6):
        auth.login("observe@example.com", "wrong-password")
    assert auth.throttle_delay("observe@example.com") > 0.0
    assert auth.throttle_delay("someone-else@example.com") == 0.0


def test_login_throttle_can_be_reset_for_operations(db_session, auth):
    """運用用に失敗記録を消せる（テスト隔離・障害時の手動解除）"""
    _ok, _msg, _user = auth.signup("reset-all@example.com", VALID_PASSWORD)
    for _ in range(6):
        auth.login("reset-all@example.com", "wrong-password")
    assert auth.throttle_delay("reset-all@example.com") > 0.0

    auth.reset_login_throttle()

    assert auth.throttle_delay("reset-all@example.com") == 0.0


# --- 4. CORS ガード ---------------------------------------------------------------
def test_cors_wildcard_with_credentials_is_rejected():
    """`origins=["*"]` + `allow_credentials=True` は起動拒否（資格情報の窃取状態）"""
    with pytest.raises(ValidationError) as excinfo:
        Settings(cors_origins=["*"], cors_allow_credentials=True)

    message = str(excinfo.value)
    assert "CORS" in message
    assert "*" in message


def test_cors_wildcard_without_credentials_is_allowed():
    """ワイルドカード自体は credentials 無効なら安全なので許容する"""
    settings = Settings(cors_origins=["*"], cors_allow_credentials=False)

    assert settings.cors_origins == ["*"]
    assert settings.cors_allow_credentials is False


def test_cors_specific_origin_with_credentials_is_allowed():
    """明示的なオリジン + credentials は正常な運用（許可する）"""
    settings = Settings(cors_origins=["https://a.example.com"], cors_allow_credentials=True)

    assert settings.cors_origins == ["https://a.example.com"]
    assert settings.cors_allow_credentials is True


def test_cors_wildcard_mixed_with_specific_origin_is_rejected():
    """混在した `["https://a", "*"]` も危険（ワイルドカードが含まれる）"""
    with pytest.raises(ValidationError):
        Settings(cors_origins=["https://a.example.com", "*"], cors_allow_credentials=True)


def test_cors_wildcard_via_env_with_credentials_is_rejected(monkeypatch):
    """環境変数経由でも同じ拒否が効く（`RETRO_RADIO_CORS_ORIGINS=*` 経路）"""
    monkeypatch.setenv("RETRO_RADIO_CORS_ORIGINS", "*")
    monkeypatch.setenv("RETRO_RADIO_CORS_ALLOW_CREDENTIALS", "true")

    with pytest.raises(ValidationError):
        Settings()


def test_default_cors_settings_are_unaffected():
    """既定値は従来どおり安全（localhost 限定 + credentials 無効）"""
    settings = Settings()

    assert settings.cors_origins == [
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ]
    assert settings.cors_allow_credentials is False


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('["https://a.example.com", "https://b.example.com"]',
         ["https://a.example.com", "https://b.example.com"]),
        ("https://a.example.com,https://b.example.com",
         ["https://a.example.com", "https://b.example.com"]),
        ("*", ["*"]),
        ("http://localhost:8501", ["http://localhost:8501"]),
    ],
    ids=["json", "comma", "wildcard", "single"],
)
def test_parse_cors_origins_keeps_its_four_forms(raw, expected):
    """ガード追加で `parse_cors_origins` の受理形式が変わっていない（回帰）"""
    assert parse_cors_origins(raw) == expected


def test_parse_cors_origins_edge_cases_unchanged():
    """None / 空文字 / list 直渡し / 壊れた JSON の挙動も変えていない"""
    assert parse_cors_origins(None) == []
    assert parse_cors_origins("") == []
    assert parse_cors_origins("   ") == []
    assert parse_cors_origins([" http://a ", "", "http://b"]) == ["http://a", "http://b"]
    assert parse_cors_origins("[http://a") == ["[http://a"]


def test_parse_cors_origins_json_output_is_stable():
    """正規化後の値は JSON 配列として壊れない（pydantic-settings が再解釈するため）"""
    assert json.loads(json.dumps(parse_cors_origins("https://a,https://b"))) == [
        "https://a",
        "https://b",
    ]


# --- 5. 後方互換ハッシュ ----------------------------------------------------------
def test_legacy_password_hash_is_still_verifiable(db_session, auth):
    """旧フォーマット（100,000回 + ASCII salt）は照合できる（回帰）"""
    password = "old-style-password"
    legacy = _legacy_hash(password)

    assert verify_password(password, legacy) is True
    assert verify_password("not-the-password", legacy) is False


def test_login_upgrades_legacy_hash_to_current_iterations(db_session, auth):
    """旧ハッシュでのログインは 600,000回 + binary salt へ移行される（回帰）"""
    password = "legacy-regression-password"
    _ok, _msg, user = auth.signup("legacy-upgrade@example.com", password)

    repo = UserRepository(db_session)
    stored = repo.get_by_email("legacy-upgrade@example.com")
    legacy = _legacy_hash(password)
    stored.hashed_password = legacy
    repo.update(stored)
    db_session.commit()

    logged_in = Authenticator(db=db_session).login("legacy-upgrade@example.com", password)
    assert logged_in is not None
    assert logged_in.id == user.id

    upgraded = repo.get_by_email("legacy-upgrade@example.com").hashed_password
    assert upgraded != legacy
    assert verify_password(password, upgraded) is True
    assert auth_mod.PBKDF2_ITERATIONS >= 600_000


def test_current_hash_needs_no_rehash():
    """新フォーマットは再ハッシュ不要（余分な PBKDF2 を増やさない）"""
    matched, needs_rehash = auth_mod._verify_password("pw", hash_password("pw"))

    assert matched is True
    assert needs_rehash is False


# --- 6. 平文漏えい ---------------------------------------------------------------
def test_stored_hash_never_contains_plaintext(db_session, auth):
    """DB に保存されるハッシュは平文パスワードを含まない"""
    password = "plaintext-canary-9f3a"
    _ok, _msg, user = auth.signup("canary@example.com", password)

    stored = UserRepository(db_session).get_by_email("canary@example.com").hashed_password
    assert stored != password
    assert password not in stored
    hash_part, sep, salt_part = stored.partition(":")
    assert sep == ":" and len(hash_part) == 64 and len(salt_part) == 32
    assert verify_password(password, stored) is True


def test_same_password_hashes_differently_per_user(db_session, auth):
    """同じパスワードでもユーザーごとにソルトが異なる（ Users 間のパスワード比較を禁ずる）"""
    _ok, _msg, first = auth.signup("salt-a@example.com", "identical-password")
    _ok, _msg, second = auth.signup("salt-b@example.com", "identical-password")

    assert first.hashed_password != second.hashed_password


# --- 7. 無償アップグレードがプランを変えない（A2 の回帰） -------------------------
def test_upgrade_to_premium_does_not_mutate_plan(db_session, auth):
    """`upgrade_to_premium` は URL を返すだけでプランを書き換えない"""
    _ok, _msg, user = auth.signup("paywall@example.com", VALID_PASSWORD)
    assert user.plan is PlanType.FREE

    url = auth.upgrade_to_premium(user, PlanType.PREMIUM)

    assert user.plan is PlanType.FREE
    assert UserRepository(db_session).get_by_id(user.id).plan is PlanType.FREE
    assert auth.is_feature_enabled(user, "favorites") is False
    assert "premium" in url and "/upgrade" in url


def test_feature_gates_open_only_after_payment_sets_plan(db_session, auth):
    """webhook が plan を書くまでは有料機能が開かない（対比ケース）"""
    _ok, _msg, user = auth.signup("paid@example.com", VALID_PASSWORD)
    auth.upgrade_to_premium(user, PlanType.PRO)

    assert auth.is_feature_enabled(user, "api_access") is False

    user.plan = PlanType.PRO
    assert auth.is_feature_enabled(user, "api_access") is True


# --- 8. secret_key 未設定の検出 --------------------------------------------------
def test_auth_availability_is_detectable_from_settings():
    """認証機能が使えるかどうかを設定から判定できる"""
    assert Settings(secret_key="real-key").auth_available is True
    assert Settings(secret_key="").auth_available is False


def test_require_secret_key_raises_when_missing():
    """認証経路がsecret_key を要求したときに明示的な設定エラーになる"""
    good = "a-sufficiently-long-test-secret-key-1234"
    assert Settings(secret_key=good).require_secret_key() == good

    with pytest.raises(ConfigurationError) as excinfo:
        Settings(secret_key="").require_secret_key()

    assert "RETRO_RADIO_SECRET_KEY" in str(excinfo.value)


def test_require_secret_key_rejects_a_short_key():
    """**短すぎる** `secret_key` も拒否されること

    `secret_key` はセッション署名の HMAC 鍵として**そのまま**使われる
    （`auth/tokens.py` で raw）。`RETRO_RADIO_SECRET_KEY=abc` だと
    Cookie の署名を**1 猜測あたり 1 回の計算**で検証できてしまい、
    オフラインで総当たりすると Cookie を偽造できる。

    かつてはエラー文言だけが「32文字以上」と指示していて、
    **どこも長さを検査していなかった**（指示と検査が食い違っていた）。
    """
    too_short = "abc"
    with pytest.raises(ConfigurationError) as excinfo:
        Settings(secret_key=too_short).require_secret_key()

    message = str(excinfo.value)
    assert "RETRO_RADIO_SECRET_KEY" in message
    assert "短すぎ" in message, message
    assert str(len(too_short)) in message, "実際の長さが示されていない"

    # 境界: ちょうど 32 文字なら通る。
    exactly = "x" * 32
    assert Settings(secret_key=exactly).require_secret_key() == exactly


def test_empty_secret_key_still_constructs_settings():
    """起動を止めない（開発者が .env なしで動かせる）+ 警告は継続する"""
    with pytest.warns(RuntimeWarning, match="RETRO_RADIO_SECRET_KEY"):
        settings = Settings(secret_key="")

    assert settings.auth_available is False
