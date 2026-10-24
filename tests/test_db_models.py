import unittest
from sqlalchemy import create_engine, inspect
from retro_radio.db.models import Base

class TestDbModels(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.inspector = inspect(self.engine)

    def test_tables_exist(self):
        tables = self.inspector.get_table_names()
        # `stripe_webhook_events` は M-7（webhook の冪等化）で追加した台帳。
        # Stripe は 2xx を返すまで再送し、並行配送では古いイベントが後から
        # 届くため、「処理済み event id」を PK 付きで持つ。
        expected_tables = {'users', 'generations', 'favorites', 'stripe_webhook_events'}
        self.assertEqual(set(tables), expected_tables)

    def test_users_table_columns(self):
        columns = {c['name']: c for c in self.inspector.get_columns('users')}
        expected_columns = {
            'id', 'email', 'hashed_password', 'plan', 'stripe_customer_id',
            'stripe_subscription_id', 'generation_count', 'generation_reset_at',
            'created_at', 'updated_at'
        }
        self.assertEqual(set(columns.keys()), expected_columns)

    def test_generations_table_columns(self):
        columns = {c['name']: c for c in self.inspector.get_columns('generations')}
        expected_columns = {
            'id', 'user_id', 'year', 'month', 'day', 'script', 'song_title',
            'artist_name', 'preview_url', 'audio_path', 'all_songs', 'errors', 'created_at'
        }
        self.assertEqual(set(columns.keys()), expected_columns)

    def test_favorites_table_columns(self):
        columns = {c['name']: c for c in self.inspector.get_columns('favorites')}
        expected_columns = {
            'id', 'user_id', 'generation_id', 'created_at'
        }
        self.assertEqual(set(columns.keys()), expected_columns)

if __name__ == '__main__':
    unittest.main()
