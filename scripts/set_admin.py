"""既存ユーザーのプランを PRO に変更する保守スクリプト。

transaction 方針: repository は flush() までが責務で、commit は
retro_radio.db.session.get_db() contextmanager のみが行う。
したがって必ず `with get_db() as db:` の中で操作する（contextmanager 終了時に
自動 commit / 例外時は rollback / 終了時に close）。

Usage:
    python scripts/set_admin.py <email>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retro_radio.db.session import get_db
from retro_radio.db.repository import UserRepository
from retro_radio.models.user import PlanType


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/set_admin.py <email>")
        return 1

    email = sys.argv[1]

    # commit は get_db() が担当する。ここでは commit()/close() を呼ばない
    with get_db() as db:
        repo = UserRepository(db)
        user = repo.get_by_email(email)
        if not user:
            print(f"[ERROR] User not found: {email}")
            return 1

        user.plan = PlanType.PRO
        repo.update(user)

    print(f"[OK] Updated {email} to {PlanType.PRO.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
