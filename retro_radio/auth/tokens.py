"""テナント対応のリクエスト認証プリミティブ（提案⑧・S4）。

## 何を Vince するか

`retro_radio/auth/authenticator.py` は 20KB の**認証基盤**（パスワードハッシュ、
スロットリング、ロールバック付きログイン）として既に実装されているが、
`server.py` から **1 度も呼ばれていない**。
本モジュールはその「起動の瞬間」に必要な薄い層で、既存の `Authenticator` を
壊さず・拡張せずに **接続する**。

## 3 つの認証経路

| モード | 資格情報 | 想定する導入先 |
|---|---|---|
| `session` | `RETRO_RADIO_SECRET_KEY` で署名したセッション Cookie | 施設（オペレータ）モード。画面ログイン |
| `bearer` | `RETRO_RADIO_SINGLE_USER_KEY`（`Authorization: Bearer`） | 個人利用モード |
| `disabled` | なし（`RETRO_RADIO_REQUIRE_AUTH=0`） | 個人利用・ intimate な用途 |
| `unavailable` | **どれも無いのに require_auth=1** | **fail-closed**（503） |

`disabled` を明示的に「保護を切った状態」として存在させるのが要点。
`require_auth=0` は「個人利用で認証なしで動かす」ための**意図的な選択**であり、
既定値を 0 にしないのは「施設にデプロイしたが誰も保護egerなかった」状態を
許さないため（既定は安全側）。

## トークンの設計

`session` と `bearer` は **同じ形式**（`itsdangerous` 相当）に揃える:

```
<base64url(payload)>.<base64url(HMAC-SHA256(payload))>
```

外部依存を増やさないため **標準ライブラリのみ**（`hmac` / `hashlib` / `base64`）で
実装する。改ざんは HMAC 不一致で検出する。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from ..config import ConfigurationError, Settings, get_settings

logger = logging.getLogger(__name__)

#: セッション Cookie 名。
SESSION_COOKIE_NAME = "retro_radio_session"

#: セッションの既定 TTL（秒）。施設では 1 シフト（8h）を超えて持ち越さない。
DEFAULT_SESSION_TTL_SECONDS = 8 * 3600

#: 署名に使うアルゴリズム。`hmac.compare_digest` で定数時間比較する。
_HASH_ALGORITHM = "sha256"

#: 署名に使う鍵のドメイン（鍵分離）。
#: **很重要**: セッションとベアラーで別の鍵を使う。
#: 同じ鍵だと「ベアラートークンをセッション Cookie として注入する」攻撃が通る。
_SESSION_KEY_DOMAIN = b"retro_radio.session.v1:"
_BEARER_KEY_DOMAIN = b"retro_radio.bearer.v1:"


class TokenError(ValueError):
    """トークンが不正・期限切れ・改ざんされた。"""


#: 署名鍵として**強制**する最小長。空文字・空白のみ・短すぎる鍵を弾く。
#:
#: `secret_key` はこのモジュールの HMAC/PBKDF2 入力として**そのまま**使われる。
#: 短いと 1 推測あたり 1 回の計算で Cookie の署名を検証できてしまい、
#: オフライン総当たりで `uid=<被害者>` `role=admin` の Cookie を偽造できる。
#: 偽造 Cookie で `GET /api/me/export`（他人の原稿・同意履歴の開示）が通り、
#: `DELETE /api/me`（他人のデータ削除）まで通る。
#:
#: 以前は 1 で「空でない」ことしか見ていなかった。しかし
#: `server.create_session` が `secret=` を**明示的に**渡すため
#: `Settings.require_secret_key()` の 32 文字検査が短絡され、
#: 検査があるのに**実経路では無効**だった。
#: 発行側 (`issue_*`) と検証側 (`read_*`) の両方で同じ検査を行う。
MIN_SECRET_LENGTH = 32

#: 実用上の推奨下限。`MIN_SECRET_LENGTH` と同じ値になったため、
#: この定数は「警告の閾値」としてのみ残る（外部参照の互換性のため）。
RECOMMENDED_SECRET_LENGTH = 32

_warned_weak_secrets: set = set()


def _require_secret(secret: Optional[str], what: str) -> str:
    """署名鍵を検証して返す。未設定・短すぎる鍵は `TokenError`。"""
    if not secret or not str(secret).strip():
        raise TokenError(f"{what} が未設定のためトークンを扱えません")
    if len(secret) < MIN_SECRET_LENGTH:
        raise TokenError(
            f"{what} が短すぎます（{len(secret)} 文字 < 最小 {MIN_SECRET_LENGTH} 文字）。"
            "セッション署名の HMAC 鍵として使うため、オフライン総当たりで"
            "Cookie を偽造できてしまいます。"
        )
    return secret


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _derive(secret: str, domain: bytes) -> bytes:
    """鍵導出。ドメイン文字列を salt として混ぜ、用途間で鍵を分離する。"""
    return hashlib.pbkdf2_hmac(
        _HASH_ALGORITHM, secret.encode("utf-8"), domain, 100_000
    )


def _sign(payload: bytes, secret: str, domain: bytes) -> str:
    """payload を署名付きトークン文字列にする。"""
    key = _derive(secret, domain)
    body = _b64e(payload)
    signature = hmac.new(key, body.encode("ascii"), _HASH_ALGORITHM).digest()
    return f"{body}.{_b64e(signature)}"


def _verify(token: str, secret: str, domain: bytes) -> bytes:
    """署名付きトークンを検証し、payload を返す。不正なら `TokenError`。"""
    if not token or "." not in token:
        raise TokenError("トークンの形式が不正です")
    body, _, signature = token.rpartition(".")
    if not body or not signature:
        raise TokenError("トークンの形式が不正です")
    key = _derive(secret, domain)
    expected = hmac.new(key, body.encode("ascii"), _HASH_ALGORITHM).digest()
    try:
        provided = _b64d(signature)
    except Exception as exc:  # noqa: BLE001 - どんな不正も TokenError に寄せる
        raise TokenError("署名のデコードに失敗しました") from exc
    # 定数時間比較（早見で一致させる timing oracle を避ける）
    if not hmac.compare_digest(expected, provided):
        raise TokenError("署名が一致しません")
    try:
        return _b64d(body)
    except Exception as exc:  # noqa: BLE001
        raise TokenError("payload のデコードに失敗しました") from exc


# --- セッション（施設モード）------------------------------------------------------
def issue_session_token(
    user_id: str,
    secret: Optional[str] = None,
    tenant_id: Optional[str] = None,
    role: str = "member",
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> str:
    """画面ログイン成功時に発行するセッション Cookie 値。

    payload には `user_id` 放入。**氏名・生年などの個人データは入れない**
    （Cookie はクライアントに持ち帰られるため、開示対象になる）。
    """
    key = _require_secret(
        secret if secret is not None else get_settings().require_secret_key(),
        "RETRO_RADIO_SECRET_KEY",
    )
    now = int(time.time())
    payload = {
        "uid": str(user_id),
        "tid": tenant_id or "",
        "role": role,
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    return _sign(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        key,
        _SESSION_KEY_DOMAIN,
    )


def read_session_token(
    token: str,
    secret: Optional[str] = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """セッション Cookie を検証して payload を返す。

    Raises
    ------
    TokenError
        形式不正・署名不一致・期限切れ。**すべて同じ例外**にして、
        呼び出し側が「署名が違う」と「期限切れ」を区別してログに出さないようにする。
    """
    # 検証側は「未設定」なら例外にする。`tokens.py` は DB を持たないので
    # 設定値の解決は呼び出し側（`authenticate_request`）の責務。
    _require_secret(secret, "RETRO_RADIO_SECRET_KEY")
    payload = _verify(token, secret, _SESSION_KEY_DOMAIN)
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TokenError("セッションの payload が壊れています") from exc
    if not isinstance(data, dict) or not data.get("uid"):
        raise TokenError("セッションにユーザー ID がありません")
    current = int(now if now is not None else time.time())
    if int(data.get("exp", 0)) <= current:
        # 境界は**排他**。`exp == now` はすでに失効している
        # （`<` にすると 1 秒分の猶予が生まれる）。
        raise TokenError("セッションの有効期限が切れています")
    # 失効（論理削除・ロール降格）のフック。`tokens.py` は DB を持たないため、
    # 呼び出し側が差し込む（既定は「何もしない」= 検証のみ）。
    verifier = _session_verifier_hook()
    if verifier is not None:
        try:
            alive = verifier(str(data["uid"]), data)
        except Exception as exc:  # noqa: BLE001 - 判定不能は拒否側に倒す
            raise TokenError("セッションの有効性を確認できませんでした") from exc
        if not alive:
            raise TokenError("セッションは失効しています")
    return data


# --- 単一ベアラートークン（個人モード）--------------------------------------------
def issue_bearer_token(
    key: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
) -> str:
    """個人モード用の単一ベアラートークンを生成する（運用側の補助関数）。"""
    settings = get_settings()
    secret = key if key is not None else settings.single_user_key
    if not secret:
        raise ConfigurationError(
            "RETRO_RADIO_SINGLE_USER_KEY が未設定のためベアラートークンを発行できません"
        )
    _require_secret(secret, "RETRO_RADIO_SINGLE_USER_KEY")
    now = int(time.time())
    exp = now + int(ttl_seconds) if ttl_seconds else now + 365 * 24 * 3600
    payload = {"iat": now, "exp": exp, "single_user": True}
    return _sign(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        secret,
        _BEARER_KEY_DOMAIN,
    )


def read_bearer_token(
    token: str,
    key: Optional[str] = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """ベアラートークンを検証する。失敗はすべて `TokenError`。"""
    secret = key if key is not None else get_settings().single_user_key
    if not secret:
        raise TokenError("RETRO_RADIO_SINGLE_USER_KEY が未設定です")
    _require_secret(secret, "RETRO_RADIO_SINGLE_USER_KEY")
    payload = _verify(token, secret, _BEARER_KEY_DOMAIN)
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TokenError("ベアラートークンの payload が壊れています") from exc
    current = int(now if now is not None else time.time())
    if int(data.get("exp", 0)) <= current:
        # 境界は**排他**（`exp == now` で失効）。
        raise TokenError("ベアラートークンの有効期限が切れています")
    return data


# --- セッションの失効フック（pluggable / DB 非依存）--------------------------------
#: `user_id` と payload を受け取り、そのセッション(now)を有効とみなしてよいかを返す。
#: `False` を返すと `read_session_token` は `TokenError` を投げる。
#: **DB を import しない**ため `tokens.py` は単独で import できる
#: （`tests/test_auth_wiring.py` は他モジュールなしで読み込む）。
#: 呼び出し側（アプリ起動時）が `UserSecurityRepository.is_deleted` などを
#: 束ねて `set_session_verifier` に渡す。
SessionVerifier = Callable[[str, Dict[str, Any]], bool]

_session_verifier: Optional[SessionVerifier] = None


def set_session_verifier(verifier: Optional[SessionVerifier]) -> None:
    """セッション検証フックを差し込む（`None` で解除）。

    差し込みの例::

        from retro_radio.db.privacy_repository import UserSecurityRepository
        tokens.set_session_verifier(
            lambda uid, payload: not UserSecurityRepository(db).is_deleted(uid)
        )
    """
    global _session_verifier
    _session_verifier = verifier


def _session_verifier_hook() -> Optional[SessionVerifier]:
    return _session_verifier


# --- 資格情報の抽出 ---------------------------------------------------------------
def extract_bearer(authorization: Optional[str]) -> Optional[str]:
    """`Authorization: Bearer <token>` からトークンを取り出す。

    - ヘッダー無し / 空白のみ / スキーム違い → `None`
    - auth-scheme は RFC 7235 により**大文字小文字を区別しない**。
    - `Bearer` の後にトークンが無い → `None`
    """
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, value = parts
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


# --- 解決結果 ---------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    """1 リクエストの主体（誰が、どのテナントで、どのロールで）。

    ``anonymous`` は「認証を要求しないエンドポイント（`/health` など）」用。
    ``require_tenant()`` が返すのは必ず ``anonymous=False``。
    """

    user_id: Optional[str] = None
    tenant_id: str = "default"
    role: str = "member"
    auth_mode: str = "disabled"
    authenticated: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_anonymous(self) -> bool:
        return not self.authenticated

    @property
    def is_authenticated(self) -> bool:
        return self.authenticated


def resolve_mode(settings: Optional[Settings] = None) -> str:
    """`session` / `bearer` / `disabled` / `unavailable` のいずれか。

    判定の優先順位は `Settings.require_auth_config` と同一。
    """
    return (settings or get_settings()).require_auth_config()


def authenticate_request(
    authorization: Optional[str] = None,
    session_cookie: Optional[str] = None,
    settings: Optional[Settings] = None,
    now: Optional[int] = None,
) -> Tuple[Optional[Principal], Optional[str]]:
    """資格情報から `Principal` を作る。

    Returns
    -------
    (principal, reason)
        認証に成功すれば ``(Principal, None)``。
        失敗時は ``(None, 理由)``。理由は監査ログ用で、**詳細を外部には出さない**。
    """
    conf = settings or get_settings()
    mode = conf.require_auth_config()

    if mode == "disabled":
        # 意図的に保護を切った個人利用モード。テナントは暗黙の `default`。
        return (
            Principal(tenant_id="default", role="member", auth_mode="disabled",
                      authenticated=False),
            None,
        )

    if mode == "unavailable":
        return None, "auth_not_configured"

    # --- セッション Cookie（施設モード）を先に試す ---------------------------------
    if session_cookie:
        try:
            payload = read_session_token(session_cookie, secret=conf.secret_key, now=now)
        except TokenError:
            payload = None
        if payload is not None:
            return (
                Principal(
                    user_id=str(payload.get("uid")),
                    tenant_id=str(payload.get("tid") or "default"),
                    role=str(payload.get("role") or "member"),
                    auth_mode="session",
                    authenticated=True,
                ),
                None,
            )
        return None, "invalid_session"

    # --- 単一ベアラートークン（個人モード） ---------------------------------------
    token = extract_bearer(authorization)
    if token is not None:
        try:
            read_bearer_token(token, key=conf.single_user_key, now=now)
        except TokenError:
            return None, "invalid_bearer"
        return (
            Principal(tenant_id="default", role="member", auth_mode="bearer",
                      authenticated=True),
            None,
        )

    return None, "missing_credentials"


__all__ = [
    "SESSION_COOKIE_NAME",
    "DEFAULT_SESSION_TTL_SECONDS",
    "TokenError",
    "Principal",
    "issue_session_token",
    "read_session_token",
    "issue_bearer_token",
    "read_bearer_token",
    "set_session_verifier",
    "extract_bearer",
    "authenticate_request",
    "resolve_mode",
]
