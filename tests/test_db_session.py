import unittest
import os
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
from retro_radio.db.session import init_db, get_db, get_db_sync, get_db_context, engine
from retro_radio.db.models import Base

class TestDbSession(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_init_db(self):
        # This should not raise an exception
        init_db()
        # Check that required tables exist
        from sqlalchemy import inspect
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        expected = {'users', 'generations', 'favorites'}
        # Check that all expected tables exist (ignore alembic_version if present)
        self.assertTrue(expected.issubset(tables))

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

if __name__ == '__main__':
    unittest.main()
