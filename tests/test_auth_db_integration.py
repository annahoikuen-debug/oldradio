"""Authenticator と DB の統合テスト。

Wave 1 の変更追随:
  - `check_can_generate` は月間制限を撤廃し常に `(True, -1)` を返す（旧: FREE 5回/月）
  - `Authenticator` は外部セッションを渡すと自前で commit する
  - `upgrade_to_premium` はプランを変更せず URL のみ返す
"""

import unittest
from datetime import datetime, timedelta, timezone

from retro_radio.auth.authenticator import UNLIMITED_GENERATIONS, Authenticator
from retro_radio.db.models import UserModel
from retro_radio.db.repository import UserRepository
from retro_radio.models.user import PlanType, User


def _user(**overrides):
    now = datetime.now()
    fields = {
        "id": "testid",
        "email": "test@example.com",
        "hashed_password": "hashed",
        "plan": PlanType.FREE,
        "generation_count": 0,
        "generation_reset_at": now + timedelta(days=30),
        "created_at": now,
    }
    fields.update(overrides)
    return User(**fields)


class TestAuthDBIntegration(unittest.TestCase):
    def setUp(self):
        # conftest の db_session と同じ方針: テーブルは1度だけ作り、行を消す
        from retro_radio.db.models import Base
        from retro_radio.db.session import get_db_sync, get_engine

        self.engine = get_engine()
        Base.metadata.create_all(bind=self.engine)
        self.db = get_db_sync()
        with self.db.begin():
            for table in reversed(Base.metadata.sorted_tables):
                self.db.execute(table.delete())
        self.auth = Authenticator()
        self.auth.db = self.db
        self.auth.user_repo = UserRepository(self.db)

    def tearDown(self):
        self.db.close()

    def test_get_current_user_no_user(self):
        self.auth.logout()
        self.assertIsNone(self.auth.get_current_user())

    def test_login_user(self):
        user = _user()
        self.auth.login_user(user)
        self.assertEqual(self.auth.get_current_user(), user)

    def test_logout(self):
        self.auth.login_user(_user())
        self.auth.logout()
        self.assertIsNone(self.auth.get_current_user())

    def test_check_can_generate_is_unlimited(self):
        """月間生成制限は撤廃されたため、回数が残っている plan でも常に (True, -1)"""
        user = _user(generation_count=3)
        can_generate, remaining = self.auth.check_can_generate(user)
        self.assertTrue(can_generate)
        self.assertEqual(remaining, UNLIMITED_GENERATIONS)
        self.assertEqual(remaining, -1)

    def test_check_can_generate_after_heavy_usage(self):
        """生成回数が上限を超えていても制限は働かない"""
        user = _user(generation_count=10_000)
        self.assertEqual(self.auth.check_can_generate(user), (True, -1))

    def test_check_can_generate_resets_stale_counter(self):
        """月次リセット期を過ぎていればカウンタだけ初期化される（権限は変わらない）"""
        past = datetime.now(timezone.utc) - timedelta(days=1)
        user = _user(generation_count=42, generation_reset_at=past)
        self.auth._save_user(user)

        self.assertEqual(self.auth.check_can_generate(user), (True, -1))
        self.assertEqual(user.generation_count, 0)
        self.assertGreater(user.generation_reset_at, past)

    def test_increment_generation_count(self):
        created_user = self.auth.user_repo.create("test@example.com", "hashed")
        created_user.generation_count = 3
        created_user.generation_reset_at = datetime.now() + timedelta(days=30)
        self.auth.user_repo.update(created_user)
        self.db.commit()

        self.auth.increment_generation_count(created_user)

        self.assertEqual(created_user.generation_count, 4)
        self.db.expire_all()
        db_user = self.db.query(UserModel).filter_by(id=created_user.id).first()
        self.assertEqual(db_user.generation_count, 4)

    def test_upgrade_to_premium_does_not_change_plan(self):
        """決済前の upgrade_to_premium はプランを変えない（webhook のみが変更する）"""
        user = _user()
        url = self.auth.upgrade_to_premium(user, PlanType.PREMIUM)

        self.assertEqual(user.plan, PlanType.FREE)
        self.assertIn("premium", url)

    def test_signup_and_login_roundtrip(self):
        # NIST SP 800-63B 準拠のパスワードポリシー（`MIN_PASSWORD_LENGTH = 8`）を満たす値。
        # ポリシーは `signup()` のみを対象とするので、login 側も同じ値を使う。
        password = "TestPass123"
        ok, message, user = self.auth.signup("roundtrip@example.com", password)
        self.assertTrue(ok, message)
        self.assertIsNotNone(user)
        self.db.commit()

        self.auth.logout()
        logged_in = self.auth.login("roundtrip@example.com", password)
        self.assertIsNotNone(logged_in)
        self.assertEqual(logged_in.id, user.id)


if __name__ == "__main__":
    unittest.main()
