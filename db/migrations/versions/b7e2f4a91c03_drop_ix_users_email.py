"""Drop redundant unique index ix_users_email (A-1 / code review H-1)

`users.email` の一意性は initial migration の ``sa.UniqueConstraint("email")``
と ``retro_radio.db.models.UserModel.email`` の ``unique=True`` が既に担っている。
`88ad9aef7f29` が当時のモデル定義（``index=True, unique=True``）に合わせて
追加した ``ix_users_email`` は、コミット 3faba84 がモデル側の ``index=True`` を
削除したことで**マイグレーション側だけが残る**二重定義になった。
結果 ``alembic check`` が ``remove_index ix_users_email`` を検出し、CI が赤に
なっていた。この revision で索引を落とし、モデルとマイグレーションを一致させる。

一意性は ``UNIQUE (email)`` 制約が維持するため、重複挿入の防止は失われない。

Revision ID: b7e2f4a91c03
Revises: 9f4b2c7d1e60
Create Date: 2026-11-13

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'b7e2f4a91c03'
down_revision: Union[str, None] = '9f4b2c7d1e60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")


def downgrade() -> None:
    op.create_index("ix_users_email", "users", ["email"], unique=True)
