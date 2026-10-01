import warnings
from logging.config import fileConfig

from sqlalchemy import MetaData
from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
from retro_radio.db.models import Base
from retro_radio.db.privacy_models import PrivacyBase

# S4 は privacy/tenancy 系テーブルを**独立した** `PrivacyBase` 上に定義している
# （`retro_radio/db/privacy_models.py`）。そのため `Base.metadata` だけでは
# 6 テーブル（tenants / user_security / music_profiles / favorite_tracks /
# consents / audit_logs）が autogenerate の「モデルに無いテーブル」扱いになる。
#
# そのままにしておくと:
#   * `alembic check` が「remove_table x 6」を検出して必ず失敗する（CI が赤になる）
#   * `alembic revision --autogenerate` が **DROP TABLE を並べた**マイグレーションを
#     生成し、 次回 `upgrade` でプライバシー/テナント/監査のスキーマを全消去する
#
# よって autogenerate には 2 つの MetaData を**まとめて**見せる。
# モデル側を 1 つの Base に統合しないのは、PrivacyBase が公開 API として
# import されているため（`privacy_models.init_privacy_db` と各テスト）。
#
# `MetaData.tables` は SQLAlchemy 2.x で不変（FacadeDict）のため直接代入できず、
# 新しい MetaData へ `Table.tometadata()` で複製して 1 つの台帳にまとめる。
_combined_metadata = MetaData()
for _source in (Base.metadata, PrivacyBase.metadata):
    for _table in _source.tables.values():
        # 同一名が両方に存在する場合は先に登録された方（models 側）を正とする
        if _table.name not in _combined_metadata.tables:
            _table.tometadata(_combined_metadata)
target_metadata = _combined_metadata

# アプリと同じ設定（RETRO_RADIO_DATABASE_URL / .env）を Alembic にも適用する。
# alembic.ini の sqlalchemy.url よりも優先される。
from retro_radio.config import get_settings


def _database_url() -> str:
    # SECRET_KEY 未設定の警告はマイグレーションの出力妨害になるので抑制する
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return get_settings().database_url


# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    '''Run migrations in offline mode.'''
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    '''Run migrations in online mode.'''
    configuration = config.get_section(config.config_ini_section, {})
    configuration['sqlalchemy.url'] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
