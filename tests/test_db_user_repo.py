import unittest
from datetime import datetime, timedelta

# `RETRO_RADIO_DATABASE_URL` の正本は `tests/conftest.py`（実行ごとに一意な
# DB ファイル）。import 時の env 書き換えは collection 順に依存する状態共有
# を作るため禁止（`test_hygiene_regression.TestNoImportTimeEnvMutation` が
# 検出する）。接頭辞なし `DATABASE_URL` は設定に読まれない無効な名前だった。
from retro_radio.db.session import get_db_sync, engine
from retro_radio.db.repository import UserRepository
from retro_radio.db.models import Base, UserModel
from retro_radio.models.user import User as UserDomain, PlanType

class TestUserRepository(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = get_db_sync()
        self.repo = UserRepository(self.db)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_get_by_email_not_found(self):
        user = self.repo.get_by_email('nonexistent@example.com')
        self.assertIsNone(user)

    def test_get_by_id_not_found(self):
        user = self.repo.get_by_id('nonexistentid')
        self.assertIsNone(user)

    def test_create_user(self):
        email = 'test@example.com'
        hashed_password = 'hashed'
        user = self.repo.create(email, hashed_password)
        self.assertIsInstance(user, UserDomain)
        self.assertEqual(user.email, email)
        self.assertEqual(user.hashed_password, hashed_password)
        self.assertEqual(user.plan, PlanType.FREE)
        self.assertEqual(user.generation_count, 0)
        self.assertIsNotNone(user.id)
        self.assertIsNotNone(user.created_at)
        self.assertIsNotNone(user.updated_at)

        # Check that it's in the database
        db_user = self.db.query(UserModel).filter_by(id=user.id).first()
        self.assertIsNotNone(db_user)
        self.assertEqual(db_user.email, email)

    def test_update_user(self):
        # Create a user
        user = self.repo.create('test@example.com', 'hashed')
        # Modify the user
        user.email = 'new@example.com'
        user.hashed_password = 'newhashed'
        user.plan = PlanType.PREMIUM
        user.generation_count = 5
        user.generation_reset_at = datetime.utcnow() + timedelta(days=30)
        # Update
        updated_user = self.repo.update(user)
        self.assertEqual(updated_user.email, 'new@example.com')
        self.assertEqual(updated_user.hashed_password, 'newhashed')
        self.assertEqual(updated_user.plan, PlanType.PREMIUM)
        self.assertEqual(updated_user.generation_count, 5)
        self.assertEqual(updated_user.generation_reset_at, user.generation_reset_at)
        # Check in database
        db_user = self.db.query(UserModel).filter_by(id=user.id).first()
        self.assertEqual(db_user.email, 'new@example.com')
        self.assertEqual(db_user.hashed_password, 'newhashed')
        self.assertEqual(db_user.plan.value, 'premium')
        self.assertEqual(db_user.generation_count, 5)

    def test_update_stripe_ids(self):
        user = self.repo.create('test@example.com', 'hashed')
        self.repo.update_stripe_ids(user.id, customer_id='cus_123', subscription_id='sub_123')
        db_user = self.db.query(UserModel).filter_by(id=user.id).first()
        self.assertEqual(db_user.stripe_customer_id, 'cus_123')
        self.assertEqual(db_user.stripe_subscription_id, 'sub_123')
        self.assertIsNotNone(db_user.updated_at)

if __name__ == '__main__':
    unittest.main()
