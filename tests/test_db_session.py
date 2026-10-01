import unittest
import os

# 設定を読むのは `Settings`（`env_prefix="RETRO_RADIO_"`）なので、
# **`RETRO_RADIO_DATABASE_URL` が正**。以前は `DATABASE_URL`（接頭辞なし）を
# 設定しておりまったく無効だった（実測: 単体実行でルートの
# `retro_radio.db` を `drop_all`/`create_all` していた）。
os.environ['RETRO_RADIO_DATABASE_URL'] = 'sqlite:///:memory:'
from retro_radio.db.session import (
    get_db,
    get_db_sync,
    get_db_context,
    engine,
    schema_is_ready,
)
from retro_radio.db.models import Base


class TestSchemaOwnership(unittest.TestCase):
    """スキーマの所有者は Alembic 一本であること。

    背景: 以前は `Authenticator.__init__` が毎リクエストで
    `init_db()`（= `create_all()`）を呼んでいた。`create_all()` は
    `alembic_version` を書き換えないので、`Authenticator` が一度でも
    動くと、その後の `alembic upgrade head` が
    ``table tenants already exists`` で**恒久的に失敗**する（実測）。
    つまり **アプリを起動した**だけで migration ゲートが壊れる。

    `init_db()` は開発用に残すが、**リクエスト経路からは呼ばない**。
    """

    def test_schema_is_ready_is_read_only(self):
        """`schema_is_ready()` はテーブルを増やさない（読み取り専用の診断）。"""
        from sqlalchemy import inspect

        before = set(inspect(engine).get_table_names())
        schema_is_ready()
        after = set(inspect(engine).get_table_names())
        self.assertEqual(before, after, "schema_is_ready() が DDL を発行した")

    def test_authenticator_does_not_issue_ddl(self):
        """`Authenticator()` は DDL を発行しないこと。

        リクエスト経路（`POST /api/auth/session` は**認証前**）から
        `create_all()` を呼ぶと、認証前の攻撃者が無認証で DDL を起こせる。
        """
        from sqlalchemy import inspect

        from retro_radio.auth.authenticator import Authenticator

        before = set(inspect(engine).get_table_names())
        Authenticator()
        Authenticator()
        after = set(inspect(engine).get_table_names())

        created = after - before
        self.assertEqual(
            set(),
            created,
            f"Authenticator() がテーブルを作った: {sorted(created)}",
        )


class TestDbSession(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_get_db(self):
        db = get_db_sync()
        self.assertIsNotNone(db)
        db.close()

    def test_get_db_context_manager(self):
        with get_db() as db:
            self.assertIsNotNone(db)
            # We can do a simple query
            from retro_radio.db.models import UserModel
            query = db.query(UserModel)
            self.assertIsNotNone(query)

    def test_get_db_sync_context_manager(self):
        with get_db_context() as db:
            self.assertIsNotNone(db)
            from retro_radio.db.models import UserModel
            query = db.query(UserModel)
            self.assertIsNotNone(query)

    def test_rollback_on_exception(self):
        from retro_radio.db.models import UserModel
        try:
            with get_db() as db:
                user = UserModel(id='test', email='test@example.com', hashed_password='hash')
                db.add(user)
                raise ValueError('Test exception')
        except ValueError:
            pass
        # Check that user was not committed
        with get_db() as db:
            user = db.query(UserModel).filter_by(id='test').first()
            self.assertIsNone(user)

    def test_engine_hides_bound_parameters_in_errors(self):
        """SQLAlchemy の例外文字列にバインドパラメータが含まれないこと。

        同一 email で 2 並列 `signup` が race すると UNIQUE 制約違反になり、
        既定（`hide_parameters=False`）では `str(exc)` に
        `[parameters: (..., 'pbkdf2_sha256$260000$...')]` が入る。
        `authenticator` は `logger.error(f"...: {e}")` でログに落とすため、
        パスワードハッシュと email（PII）が平文でアプリログに残る。
        """
        from sqlalchemy.exc import IntegrityError

        from retro_radio.db.models import UserModel

        secret_hash = 'pbkdf2_sha256$260000$SENSITIVEHASHVALUE'
        with get_db() as db:
            db.add(UserModel(id='dup', email='dup@example.com', hashed_password=secret_hash))
        try:
            with get_db() as db:
                db.add(UserModel(id='dup2', email='dup@example.com', hashed_password=secret_hash))
                db.flush()
        except IntegrityError as exc:
            rendered = str(exc)
            assert 'SENSITIVEHASHVALUE' not in rendered, (
                '例外文字列にパスワードハッシュが漏れています'
            )
            assert 'dup@example.com' not in rendered, (
                '例外文字列に email（PII）が漏れています'
            )
        else:
            self.fail('UNIQUE 制約違反が発生しなかった（テストの前提が崩れた）')


if __name__ == '__main__':
    unittest.main()
