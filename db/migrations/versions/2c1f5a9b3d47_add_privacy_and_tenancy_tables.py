"""Add privacy and tenancy tables (proposal 8, S4)

Facilities' operational readiness requires six tables that the existing schema
does not have. The `users` / `generations` / `favorites` tables are **not**
modified: `tests/test_db_models.py` asserts an exact set of tables and an exact
set of `users` columns, so extending them would break existing tests.

Tables added here (all in a separate MetaData: `retro_radio.db.privacy_models`):

- `tenants`: one row per facility / deployment.
- `user_security`: role, tenant membership and the soft-delete flags,
  keyed by the existing `users.id` (no FK: see the module docstring).
- `music_profiles`: personal music profile (owner unique, optional group).
- `favorite_tracks`: favourite songs with familiarity and reaction.
- `consents`: terms-of-service acceptance, versioned.
- `audit_logs`: generation / playback / consent / deletion events.

Downgrade drops them. Dropping the tables is safe because they hold only data
created after this revision.

Revision ID: 2c1f5a9b3d47
Revises: d7a3f0b1c9e4
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2c1f5a9b3d47'
down_revision: Union[str, None] = 'd7a3f0b1c9e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: この revision が作るテーブル。`tenants` を先に作って、そこから FK を張る。
TABLES_IN_ORDER = (
    "tenants",
    "user_security",
    "music_profiles",
    "favorite_tracks",
    "consents",
    "audit_logs",
)


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "user_security",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=True),
        sa.Column("deletion_requested_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
        # テナントは論理削除（is_active=False）するので、行が消えることはない。
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_user_security_tenant_id_tenants",
        ),
        sa.CheckConstraint(
            "role IN ('member', 'admin')",
            name="ck_user_security_role",
        ),
    )
    op.create_index("ix_user_security_tenant_id", "user_security", ["tenant_id"])

    op.create_table(
        "music_profiles",
        sa.Column("id", sa.String(length=32), nullable=False),
        # owner_id は UNIQUE: 1 利用者 1 プロファイル。
        # `MusicProfileRepository.get()` は「無ければ空」を返すため、
        # 行が無い状態を許す（= 新規利用者でも呼び出せる）。
        sa.Column("owner_id", sa.String(length=32), nullable=False),
        sa.Column("group_id", sa.String(length=32), nullable=True),
        sa.Column("teenage_decades", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", name="uq_music_profiles_owner_id"),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["tenants.id"],
            name="fk_music_profiles_group_id_tenants",
        ),
    )
    op.create_index("ix_music_profiles_owner_id", "music_profiles", ["owner_id"])
    op.create_index("ix_music_profiles_group_id", "music_profiles", ["group_id"])

    op.create_table(
        "favorite_tracks",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("artist", sa.String(length=500), nullable=False),
        sa.Column("familiarity_score", sa.Integer(), nullable=False),
        sa.Column("last_played_at", sa.DateTime(), nullable=True),
        sa.Column("reaction", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # (owner_id, title, artist) UNIQUE のおかげで、`record_rejection` を
        # 「読み取り -> 書き込み」の 2 手順にせず単一 UPDATE にできる。
        sa.UniqueConstraint(
            "owner_id", "title", "artist",
            name="uq_favorite_tracks_owner_title_artist",
        ),
        sa.CheckConstraint(
            "familiarity_score >= 1 AND familiarity_score <= 5",
            name="ck_favorite_tracks_familiarity",
        ),
        sa.CheckConstraint(
            "reaction IN ('positive', 'neutral', 'negative')",
            name="ck_favorite_tracks_reaction",
        ),
    )
    op.create_index("ix_favorite_tracks_owner_id", "favorite_tracks", ["owner_id"])

    op.create_table(
        "consents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("terms_version", sa.String(length=32), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_consents_user_id", "consents", ["user_id"])
    op.create_index("ix_consents_tenant_id", "consents", ["tenant_id"])
    op.create_index("ix_consents_user_version", "consents", ["user_id", "terms_version"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=32), nullable=False),
        # tenant_id は NOT NULL: 「テナント不明のイベント」を作れないようにする。
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("meta_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index(
        "ix_audit_logs_tenant_created", "audit_logs", ["tenant_id", "created_at"]
    )
    op.create_index(
        "ix_audit_logs_tenant_action", "audit_logs", ["tenant_id", "action"]
    )


def downgrade() -> None:
    # 参照元の順に落とす。
    for table in reversed(TABLES_IN_ORDER):
        op.drop_table(table)
