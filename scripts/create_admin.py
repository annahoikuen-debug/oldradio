"""管理者ユーザー（PROプラン）を作成/更新する保守スクリプト。

transaction 方針: repository は flush() までが責務で、commit は
retro_radio.db.session.get_db() contextmanager のみが行う。
したがって必ず `with get_db() as db:` の中で操作する（contextmanager 終了時に
自動 commit / 例外時は rollback / 終了時に close）。

Usage:
    python scripts/create_admin.py                 # 既定の管理者を作成
    python scripts/create_admin.py user@example.com 'password'
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retro_radio.db.session import get_db
from retro_radio.db.repository import UserRepository
from retro_radio.models.user import PlanType
from retro_radio.auth.authenticator import hash_password

DEFAULT_EMAIL = "herbmatsui@gmail.com"
DEFAULT_PASSWORD = "0000aiueo"


def main() -> int:
    email = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EMAIL
    password = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PASSWORD

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
