"""データベースを初期化する保守スクリプト。

推奨: `alembic upgrade head` と同じことを本スクリプトが行う。
Base.metadata.create_all() は **新規テーブルの作成のみ** を行い、
既存テーブルの定義（列の追加・外部キー制約・インデックス等）は更新しない。
そのため、Alembic が利用できる環境では必ず migration を適用する。

Alembic が未インストールの場合のみ create_all() にフォールバックするが、
その場合は新規DB専用の扱いになる（警告を表示する）。

Usage:
    python scripts/init_db.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retro_radio.db.session import init_db, _database_url


def _run_alembic_upgrade() -> bool:
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:
        return False

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ini_path = os.path.join(project_root, "alembic.ini")
    if not os.path.exists(ini_path):
        return False

    # env.py が retro_radio.config 経由で database_url を読むので
    # ここでの -x による上書きは不要
    config = Config(ini_path)
    config.set_main_option("script_location", os.path.join(project_root, "db", "migrations"))
    command.upgrade(config, "head")
    return True


def main() -> int:
    print(f"Database URL: {_database_url()}")

    if _run_alembic_upgrade():
        print("[OK] Database migrated with Alembic (upgrade head)")
        return 0

    print("[WARNING] alembic is not installed; falling back to create_all().")
    print("[WARNING] create_all() does NOT update existing table definitions.")
    print("[WARNING] Install alembic and run `alembic upgrade head` instead.")
    init_db()
    print("[OK] Database initialized (create_all)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
