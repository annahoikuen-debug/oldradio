"""管理者ユーザー（PROプラン）を作成/更新する保守スクリプト。

transaction 方針: repository は flush() までが責務で、commit は
retro_radio.db.session.get_db() contextmanager のみが行う。
したがって必ず `with get_db() as db:` の中で操作する（contextmanager 終了時に
自動 commit / 例外時は rollback / 終了時に close）。

Usage:
    python scripts/create_admin.py user@example.com 'password'
    python scripts/create_admin.py user@example.com          # ランダムなパスワードを生成・表示

**引数は省略できない。** 既定のメールアドレスとパスワードを**リポジトリに
埋め込まない**。埋め込むと、引数なし実行が「その既定アカウントを PRO に
する」操作になり、リポジトリを読めた全員がその資格情報を知っていることになる。
"""
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retro_radio.db.session import get_db
from retro_radio.db.repository import UserRepository
from retro_radio.models.user import PlanType
from retro_radio.auth.authenticator import hash_password

#: 生成するパスワードの最小長（`Authenticator` の min_password_length に合わせる）。
GENERATED_PASSWORD_LENGTH = 24

USAGE = (
    "Usage:\n"
    "    python scripts/create_admin.py <email> '<password>'\n"
    "    python scripts/create_admin.py <email>          # ランダムなパスワードを生成"
)


def _resolve_password(provided: str | None) -> str:
    """引数のパスワードを返す。無ければ**ランダムな**ものを生成する。

    既定値は**持たない**。リポジトリに固定パスワードを書かないため。
    """
    if provided:
        return provided
    return secrets.token_urlsafe(GENERATED_PASSWORD_LENGTH)


def main() -> int:
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr)
        print(
            "エラー: メールアドレスは必須です。"
            "既定値では実行できません（コミット済みファイルに資格情報を"
            "残さないため）。",
            file=sys.stderr,
        )
        return 2

    email = sys.argv[1].strip()
    generated = len(sys.argv) <= 2
    password = _resolve_password(sys.argv[2] if len(sys.argv) > 2 else None)

    if not email:
        print("エラー: メールアドレスが空です。", file=sys.stderr)
        return 2

    # commit は get_db() が担当する。ここでは commit()/close() を呼ばない
    with get_db() as db:
        repo = UserRepository(db)
        user = repo.get_by_email(email)

        if user:
            user.plan = PlanType.PRO
            repo.update(user)
            action = "Updated existing user"
        else:
            user = repo.create(email, hash_password(password))
            user.plan = PlanType.PRO
            repo.update(user)
            action = "Created new user"

    print(f"[OK] {action} {email} to PRO plan")
    print(f"Email: {email}")
    print(f"Plan: {PlanType.PRO.value}")
    if generated:
        # 生成したパスワードは**この 1 回だけ**しか表示されない。
        print(f"Password (generated, shown once): {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
