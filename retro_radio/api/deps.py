"""FastAPI の認証依存（提案⑧・S4、タスク1）。

## 役割

`server.py` は **S5 の管轄**（S4 は触らない）。S4 がここに用意するのは
**差し込むだけ**の依存関数で、S5 は次の 3 行だけを足す:

```python
# retro_radio/server.py（擬似 diff。実際に編集するのは S5）
-from fastapi import FastAPI, HTTPException, Request
+from fastapi import FastAPI, HTTPException, Request, Depends
+from .api.deps import require_tenant, require_admin

 @app.post("/api/generate", response_model=GenerateResponse)
-def generate_radio(req: GenerateRequest):
+def generate_radio(req: GenerateRequest, principal: Principal = Depends(require_tenant)):
+    tenant_id = principal.tenant_id          # ← テナント別キャッシュに使う
```

`/api/audio/{filename}` には **配信テナントの照合** Melchior مكنる依存を用意している。

## 依存の契約

| 関数 | 返り値 | 拒否時 | 用途 |
|---|---|---|---|
| [`require_tenant`] | `Principal`（必ず認証済み相当） | 401 / 403 / 503 | `/api/generate`・`/api/audio/*` |
| [`require_admin`] | `Principal`（`role == admin`） | 401 / 403 / 503 | `/api/admin/audit` |
| [`optional_principal`] | `Principal`（失敗しても例外を出さない） | なし | `/health`・同意画面 |
| [`require_consent`] | `Principal`（現行版に同意済み） | 403 | 個人データを取り込む API |
| [`audio_tenant`] | `str`（配信テナント ID） | 404 | `/api/audio/{tenant}/{filename}` |

**503 的含义**: 認証を有効にしながら資格情報が 1 つも無い状態（`unavailable`）。
fail-closed。「認証があるつもりだが誰も通れない」は「認証が無い」と同じ危険を
持つため、**起動を止めずに全保護を拒否**する。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from ..auth.tokens import (
    SESSION_COOKIE_NAME,
    Principal,
    authenticate_request,
    resolve_mode,
)
from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

#: `unavailable`（認証は有効だが資格情報なし）の 503 で返す文言。
#: **運用者に，原因と対処を同時に伝える**。利用者にだけ見せる文言ではない。
AUTH_UNAVAILABLE_DETAIL = (
    "認証が有効（RETRO_RADIO_REQUIRE_AUTH=1）ですが資格情報が未設定です。"
    "RETRO_RADIO_SECRET_KEY または RETRO_RADIO_SINGLE_USER_KEY を設定してください。"
    "個人利用で認証なしで動かす場合は RETRO_RADIO_REQUIRE_AUTH=0 を設定します。"
)

#: 認証失敗の応答。**理由を区別しない**（列挙オラクルを避ける）。
AUTH_REQUIRED_DETAIL = "認証が必要です"

#: admin 権限不足の応答。
FORBIDDEN_DETAIL = "この操作を行う権限がありません"


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=AUTH_UNAVAILABLE_DETAIL,
    )


def _unauthorized(reason: str) -> HTTPException:
    """認証失敗の 401。`reason` は **ログだけ**に出す。"""
    logger.info("認証に失敗しました: reason=%s", reason)
    error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=AUTH_REQUIRED_DETAIL,
    )
    # `WWW-Authenticate` を付けて、ブラウザが 401 として扱えるようにする。
    error.headers = {"WWW-Authenticate": "Bearer"}
    return error


def settings_dependency() -> Settings:
    """依存注入用の設定取得。テストで `app.dependency_overrides` で差し替えられる。"""
    return get_settings()


def _principal_from_request(
    request: Request,
    settings: Settings,
    *,
    allow_disabled: bool = False,
) -> Optional[Principal]:
    """リクエストから `Principal` を作る。失敗時は `None`。"""
    principal, _reason = authenticate_request(
        authorization=request.headers.get("Authorization"),
        session_cookie=request.cookies.get(SESSION_COOKIE_NAME),
        settings=settings,
    )
    if principal is None:
        return None
    if not allow_disabled and principal.auth_mode == "disabled":
        # require_auth=1 なのに disabled は起こり得ないが、
        # 依存の合成やテストで設定が入れ替わった場合の安全網。
        return None
    return principal


def optional_principal(
    request: Request,
    settings: Settings = Depends(settings_dependency),
) -> Principal:
    """認証状態を返すだけ。**失敗しても例外を投げない**。

    `/health`（`auth_required` を返す）や同意画面（未ログインでも条文を見せる）が使う。
    """
    mode = resolve_mode(settings)
    principal = _principal_from_request(request, settings, allow_disabled=True)
    if principal is not None:
        return principal
    return Principal(tenant_id="default", role="member", auth_mode=mode, authenticated=False)


def require_tenant(
    request: Request,
    settings: Settings = Depends(settings_dependency),
) -> Principal:
    """**テナントを確定する**。`/api/generate` と `/api/audio/*` の基点。

    Returns
    -------
    Principal
        ``tenant_id`` が必ず決定している `Principal`。

    Raises
    ------
    HTTPException
        - 503: 認証は有効なのに資格情報が無い（fail-closed）。
        - 401: 資格情報が無い・不正・期限切れ。
        - 403: 論理削除済み（削除された利用者を再利用するのを防ぐ）。
    """
    mode = resolve_mode(settings)
    if mode == "unavailable":
        raise _unavailable()

    if mode == "disabled":
        # 意図的に保護を切った個人利用モード。`default` テナントで通す。
        return Principal(
            tenant_id="default", role="member", auth_mode="disabled", authenticated=False
        )

    principal, reason = authenticate_request(
        authorization=request.headers.get("Authorization"),
        session_cookie=request.cookies.get(SESSION_COOKIE_NAME),
        settings=settings,
    )
    if principal is None:
        raise _unauthorized(reason or "unknown")

    tenant_id = _resolve_tenant(principal, request, settings)
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="利用テナントを特定できませんでした。管理者に連絡してください。",
        )
    return Principal(
        user_id=principal.user_id,
        tenant_id=tenant_id,
        role=principal.role,
        auth_mode=principal.auth_mode,
        authenticated=principal.authenticated,
    )


def require_admin(
    request: Request,
    settings: Settings = Depends(settings_dependency),
) -> Principal:
    """**admin ロール限定**。`/api/admin/audit` の基点。

    権限判定の 2 段構え:
    1. `user_security.role`（DB）— 恒久的な管理権限の正。
    2. `RETRO_RADIO_ADMIN_EMAILS` による bootstrap（初回デプロイ用）。

    .. warning::
       **ロールの正は DB であり、署名済みトークンの `role` ではない。**
       トークンのロールは TTL（既定 8 時間）だけ有効なスナップショットなので、
       DB 側で降格された管理者が降格後も admin のまま残ってしまう。
       判定は毎回 DB を見る。

    個人モード（`disabled`）では admin 権限を**誰も持たない**。
    監査ログの閲覧は「運用者だけが操作できる」必要があるため、
    保護を切った個人モードでも admin は要求する（施設導入前の確認用）。
    """
    principal = require_tenant(request, settings)
    if _db_role_is_admin(principal) or _bootstrap_admin(principal, settings):
        return Principal(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            role="admin",
            auth_mode=principal.auth_mode,
            authenticated=principal.authenticated,
        )
    logger.info(
        "admin 権限のないアクセスを拒否しました: tenant=%s", principal.tenant_id
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL
    )


def require_consent(
    request: Request,
    principal: Principal = Depends(require_tenant),
    settings: Settings = Depends(settings_dependency),
) -> Principal:
    """**現行版の利用規約に同意済み**であること。

    `RETRO_RADIO_REQUIRE_CONSENT=0`（個人モード）では常に通す。
    同意していない場合は 403 + 同意画面への誘導を返す。
    """
    if not settings.require_consent:
        return principal
    if not principal.user_id:
        # 個人モード（bearer）で同意を強制するのは個人利用には重すぎる。
        # 同意が強制されるのは `require_consent=1` のときだけであり、
        # その設定搭配では screen ログイン（user_id あり）が前提になる。
        return principal

    from ..db.privacy_repository import ConsentRepository
    from ..db.session import get_db

    with get_db() as db:
        consented = ConsentRepository(db).has_consented(
            principal.user_id, settings.terms_version
        )
    if consented:
        return principal
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "consent_required",
            "terms_version": settings.terms_version,
            "consent_url": "/api/terms",
        },
    )


def audio_tenant(
    tenant_id: str,
    request: Request,
    settings: Settings = Depends(settings_dependency),
) -> str:
    """`/api/audio/{tenant_id}/{filename}` の配信テナントを照合する。

    方針:
    - **`tenant_id` は認証済みセッションのテナントと一致しなければ配信しない**。
      これが提案⑧ の「`tenant_id` のセッション照合後にのみ配信する」に対応する。
    - `RETRO_RADIO_REQUIRE_AUTH=0` の個人モードでは、URL の `tenant_id` を
      **信用しない**ため `default` へ寄せる（同一人のキャッシュを拾えるだけ）。

    Returns
    -------
    str
        配信を許可したテナント ID。

    Raises
    ------
    HTTPException
        404（他テナントのファイル名は「存在しない」に見せて情報泄露を防ぐ）。
    """
    mode = resolve_mode(settings)
    if mode == "unavailable":
        raise _unavailable()
    if mode == "disabled":
        return "default"

    principal = require_tenant(request, settings)
    if tenant_id != principal.tenant_id:
        # 404 にすることで、「他テナントにファイルが存在する」ことを漏らさない。
        logger.info(
            "テナント不一致の音声アクセスを拒否しました: requested=%s", tenant_id
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Audio file not found"
        )
    return tenant_id


# --- 内部ヘルパ -------------------------------------------------------------------
def _db_role_is_admin(principal: Principal) -> bool:
    """`user_security.role` が admin か（**恒久的な権限の正**）。

    署名済みトークンの `role` は使わない。トークンは TTL のスナップショットなので、
    DB で降格しても失効までの間だけ admin のまま通ってしまう。
    DB が読めないときは「admin ではない側」に倒す（fail-closed）。
    """
    if not principal.user_id:
        return False
    try:
        from ..db.privacy_repository import UserSecurityRepository
        from ..db.session import get_db

        with get_db() as db:
            return UserSecurityRepository(db).is_admin(principal.user_id)
    except Exception:  # noqa: BLE001 - 権限判定は fail-closed で落とす
        logger.warning("user_security の読み込みに失敗しました（admin ではない側に倒します）",
                       exc_info=True)
        return False


def _bootstrap_admin(principal: Principal, settings: Settings) -> bool:
    """`RETRO_RADIO_ADMIN_EMAILS` に含まれるメールなら admin とみなす。

    **恒久的な管理権限は DB 側 `user_security.role` が正。** ここは
    「初回デプロイ時に手動で当てる」ための bootstrap。
    運用で管理者が交代したら、このリストから外して DB 側を移すこと。

    .. warning::
       判定材料は **DB に紐づく認証済みユーザー自身のメールアドレス**のみ。
       リクエストヘッダー（従来存在した `X-Operator-Email`）は
       **攻撃者が自由に偽装できる**ため、認可の材料にしてはならない。
       ヘッダーを信用すると、認証済みの一般利用者が 1 行のヘッダーで
       管理者権限に昇格できてしまう。
    """
    if not settings.admin_emails:
        return False
    if not (principal.authenticated and principal.user_id):
        # 未認証（個人モード / bearer で user_id 無し）は bootstrap の対象にならない。
        return False
    try:
        from ..db.privacy_repository import UserSecurityRepository
        from ..db.repository import UserRepository
        from ..db.session import get_db

        with get_db() as db:
            user = UserRepository(db).get_by_id(principal.user_id)
            email = getattr(user, "email", None) if user is not None else None
            if not email:
                return False
            return UserSecurityRepository(db).is_bootstrap_admin_email(
                email, settings.admin_emails
            )
    except Exception:  # noqa: BLE001 - 認可は fail-closed で落とす
        logger.warning("bootstrap admin の判定に失敗しました（admin ではない側に倒します）",
                       exc_info=True)
        return False


def _resolve_tenant(
    principal: Principal,
    request: Request,
    settings: Settings,
) -> str:
    """`Principal` から実効テナント ID を決める。

    優先順位:
    1. `user_security.tenant_id`（DB）— 認証済み利用者は**必ず**これを使う。
    2. `principal.tenant_id`（署名済みトークンに焼かれた値）。
    3. `default`（個人利用）。

    .. warning::
       **テナントをクライアントに申告させない。**
       ヘッダー（`X-Tenant-Id` 等）でテナントを指定できる経路は
       実装していない。`RETRO_RADIO_ALLOW_TENANT_HEADER` も読み取的らない
       （`Settings` に存在しない設定値を指す古いコメントだった）。
       認証済みの利用者が他テナントへ自己申告で移動できる期間を作らないため。
    """
    from ..db.privacy_repository import UserSecurityRepository
    from ..db.session import get_db

    if principal.authenticated and principal.user_id:
        with get_db() as db:
            security = UserSecurityRepository(db)
            if security.is_deleted(principal.user_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="このアカウントは削除済みです",
                )
            record = security.resolve(principal.user_id)
        return record.get("tenant_id") or principal.tenant_id or "default"

    # 未認証（disabled / bearer）: テナント指定は信用しない
    return principal.tenant_id or "default"


__all__ = [
    "Principal",
    "require_tenant",
    "require_admin",
    "require_consent",
    "optional_principal",
    "audio_tenant",
    "settings_dependency",
    "AUTH_UNAVAILABLE_DETAIL",
    "AUTH_REQUIRED_DETAIL",
    "FORBIDDEN_DETAIL",
]
