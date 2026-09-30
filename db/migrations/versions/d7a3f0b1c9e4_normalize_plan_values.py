"""Normalize users.plan to lowercase enum values

SQLAlchemy の Enum は Python の enum クラスを渡すと既定で「メンバー名」を DB に書く。
その挙動のため users.plan には 'FREE' / 'PREMIUM' / 'PRO' が格納され、initial migration の
sa.Enum("free", "premium", "pro", name="plantypeenum") と食い違っていた。
SQLite は enum を CHECK 制約なしの VARCHAR に落とすため表面動作するが、PostgreSQL では
invalid input value for enum plantypeenum: "FREE" で insert が失敗する。

モデル側 (retro_radio.db.models) は values_callable で .value を書くよう修正済みだが、
修正前に書き込まれた行は小文字へ正規化する必要がある。

Revision ID: d7a3f0b1c9e4
Revises: 88ad9aef7f29
Create Date: 2026-09-29

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'd7a3f0b1c9e4'
down_revision: Union[str, None] = '88ad9aef7f29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# initial migration が作る enum type の名前。retro_radio.db.models の
# SQLEnum(PlanType, name="plantypeenum", ...) と一致している必要がある。
ENUM_TYPE_NAME = "plantypeenum"


def _normalize(bind) -> None:
    """plan カラムを小文字へ寄せる。既に小文字の行は WHERE で除外されるため冪等。

    この revision に到達する時点で users テーブルは必ず存在するため、
    テーブルの存在確認（sa.inspect）は行わない。inspect は接続を要求し、
    alembic の offline モード (--sql) で migration を出力できなくなるため。
    """
    if bind.dialect.name == "sqlite":
        # SQLite: plan は CHECK 制約のない VARCHAR。lower() をそのまま使える
        op.execute(
            "UPDATE users SET plan = lower(plan) "
            "WHERE plan IS NOT NULL AND plan <> lower(plan)"
        )
    else:
        # PostgreSQL: plan は enum 型。lower() は text しか受け取らないため
        # 明示キャストが必要。代入側も text -> plantypeenum の代入キャストが
        # 存在しないためキャストなしでは失敗する。
        # enum 型は大文字を許さないので通常 0 行になるが、
        # CHECK 制約が無い SQLite 由来の DB も考慮して残す。
        op.execute(
            "UPDATE users "
            f"SET plan = lower(CAST(plan AS VARCHAR))::{ENUM_TYPE_NAME} "
            "WHERE plan IS NOT NULL "
            "AND CAST(plan AS VARCHAR) <> lower(CAST(plan AS VARCHAR))"
        )


def upgrade() -> None:
    _normalize(op.get_bind())


def downgrade() -> None:
    # 小文字が正規形。大文字に戻すことは今回修正した不具合の再現になるため行わない。
    pass
