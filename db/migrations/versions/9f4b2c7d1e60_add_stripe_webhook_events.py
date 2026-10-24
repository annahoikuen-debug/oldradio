"""Add stripe_webhook_events (webhook idempotency ledger, M-7)

Stripe は **2xx を返すまで同じイベントを再送**してくる（タイムアウト後の再送、
ネットワーク断、プロセス再起動など）。さらに並行配送のため **古いイベントが
後から届く**ことがある。処理済みの記録が無いと:

- 同じ `checkout.session.completed` が 2 回処理される
- 解約で FREE になった利用者に、**遅れて届いた古い active イベント**が
  プレミアムを勝手に戻す（out-of-order）

`stripe_webhook_events` はその台帳。`id` は Stripe のイベント ID（`evt_...`）を
PK にし、「処理済みか？」の判定と「処理済みとした」記録が**同一トランザクション**
になる（別トランザクションだと、その間のクラッシュで再送が二重適用される）。

`users` / `generations` / `favorites` は**変更しない**
（`tests/test_db_models.py` が users の列集合を完全一致で固定しているため）。

Revision ID: 9f4b2c7d1e60
Revises: 2c1f5a9b3d47
Create Date: 2026-10-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9f4b2c7d1e60'
down_revision: Union[str, None] = '2c1f5a9b3d47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stripe_webhook_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("event_created", sa.Integer(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_stripe_webhook_events_user_id", "stripe_webhook_events", ["user_id"]
    )
    op.create_index(
        "ix_stripe_webhook_events_user_created",
        "stripe_webhook_events",
        ["user_id", "event_created"],
    )


def downgrade() -> None:
    op.drop_index("ix_stripe_webhook_events_user_created", table_name="stripe_webhook_events")
    op.drop_index("ix_stripe_webhook_events_user_id", table_name="stripe_webhook_events")
    op.drop_table("stripe_webhook_events")
