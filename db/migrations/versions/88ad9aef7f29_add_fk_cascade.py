"""Add ON DELETE CASCADE to foreign keys

既存 DB には ON DELETE CASCADE を持つ外部キー制約が無いため、
retro_radio.db.models と retro_radio.db.session (PRAGMA foreign_keys=ON) の
期待と実際のテーブル定義が食い違っている。
この revision は既存テーブルに CASCADE 付き外部キー制約を retroactive に適用する。

Revision ID: 88ad9aef7f29
Revises: 54157f820607
Create Date: 2026-09-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '88ad9aef7f29'
down_revision: Union[str, None] = '54157f820607'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# batch recreate 時に、SQLite から無名のまま反射される外部キーへ名前を付けるための規則
NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

# (テーブル, カラム, 参照先テーブル)
FOREIGN_KEYS = (
    ("generations", "user_id", "users"),
    ("favorites", "user_id", "users"),
    ("favorites", "generation_id", "generations"),
)

# initial migration が index=True カラムのインデックスを生成し損なっていた分。
# retro_radio.db.models の定義に合わせる。
INDEXES = (
    ("ix_users_email", "users", ["email"], True),
    ("ix_generations_user_id", "generations", ["user_id"], False),
    ("ix_generations_user_created", "generations", ["user_id", "created_at"], False),
    ("ix_favorites_user_id", "favorites", ["user_id"], False),
    ("ix_favorites_user_gen", "favorites", ["user_id", "generation_id"], True),
)


def _constraint_name(table: str, column: str, referred_table: str) -> str:
    return f"fk_{table}_{column}_{referred_table}"


def _current_fk_ondeletes(bind) -> dict:
    """{テーブル: {カラム: ondelete}} を返す。SQLite/PostgreSQL 共通。"""
    inspector = sa.inspect(bind)
    result = {}
    tables = set(inspector.get_table_names())
    for table, column, _ in FOREIGN_KEYS:
        if table not in tables:
            continue
        result.setdefault(table, {})
        for fk in inspector.get_foreign_keys(table):
            if column in (fk.get("constrained_columns") or []):
                options = fk.get("options") or {}
                result[table][column] = (options.get("ondelete") or "").upper()
    return result


def _drop_fk(batch_op, table: str, column: str, referred_table: str, bind) -> None:
    """既存制約は名前なし（SQLite）またはDB既定名（PostgreSQL）なので、
    inspector で実名を引き、無い場合は命名規約で導いた名前を使う。"""
    name = _constraint_name(table, column, referred_table)
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys(table):
        if column in (fk.get("constrained_columns") or []) and fk.get("name"):
            name = fk["name"]
            break
    batch_op.drop_constraint(name, type_="foreignkey")


def _create_missing_indexes(bind) -> None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for name, table, columns, unique in INDEXES:
        if table not in tables:
            continue
        existing = {i["name"] for i in inspector.get_indexes(table)}
        if name in existing:
            continue
        op.create_index(name, table, columns, unique=unique)


def _drop_added_indexes(bind) -> None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for name, table, _columns, _unique in reversed(INDEXES):
        if table not in tables:
            continue
        if name in {i["name"] for i in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)


def upgrade() -> None:
    bind = op.get_bind()
    current = _current_fk_ondeletes(bind)
    is_sqlite = bind.dialect.name == "sqlite"

    for table, column, referred_table in FOREIGN_KEYS:
        if current.get(table, {}).get(column) == "CASCADE":
            # create_all() で新スキーマのテーブルが既に作られている場合など
            continue

        if is_sqlite:
            with op.batch_alter_table(
                table, naming_convention=NAMING_CONVENTION, recreate="always"
            ) as batch_op:
                _drop_fk(batch_op, table, column, referred_table, bind)
                batch_op.create_foreign_key(
                    _constraint_name(table, column, referred_table),
                    referred_table,
                    [column],
                    ["id"],
                    ondelete="CASCADE",
                )
        else:
            with op.batch_alter_table(table) as batch_op:
                _drop_fk(batch_op, table, column, referred_table, bind)
                batch_op.create_foreign_key(
                    _constraint_name(table, column, referred_table),
                    referred_table,
                    [column],
                    ["id"],
                    ondelete="CASCADE",
                )

    # テーブル再作成で消えるため、インデックスは最後に作り直す
    _create_missing_indexes(bind)


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    for table, column, referred_table in FOREIGN_KEYS:
        if is_sqlite:
            with op.batch_alter_table(
                table, naming_convention=NAMING_CONVENTION, recreate="always"
            ) as batch_op:
                _drop_fk(batch_op, table, column, referred_table, bind)
                batch_op.create_foreign_key(
                    _constraint_name(table, column, referred_table),
                    referred_table,
                    [column],
                    ["id"],
                )
        else:
            with op.batch_alter_table(table) as batch_op:
                _drop_fk(batch_op, table, column, referred_table, bind)
                batch_op.create_foreign_key(
                    _constraint_name(table, column, referred_table),
                    referred_table,
                    [column],
                    ["id"],
                )

    _drop_added_indexes(bind)
