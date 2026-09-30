"""認証状態の永続化テスト。

Wave 1 で `Authenticator._users_db` / `_users_by_id`（プロセスグローバル辞書）が
完全削除され、認証状態の正は DB のみになった。旧テストは内部実装を直接触っていたため、
公開 API（signup / login / UserRepository）経由の検証に書き換えた。
"""
from __future__ import annotations

import pytest

from retro_radio.auth.authenticator import Authenticator
from retro_radio.db.repository import UserRepository

# NIST SP 800-63B 準拠のパスワードポリシー（`MIN_PASSWORD_LENGTH = 8`）を満たす値。
# ポリシーは `signup()` のみを対象とする（`login()` は既存アカウントを締め出さない）。
VALID_PASSWORD = "TestPass123"
# 重複メール判定で「別パスワード」の意味を持つケースの対照値。
OTHER_PASSWORD = "TestPass456"


def test_signup_is_persisted_and_retrievable_by_email(db_session):
    """登録したユーザーは email / id の両方で取得できる"""
    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup("db@example.com", VALID_PASSWORD)

    by_email = auth._get_user_by_email("db@example.com")
    by_id = auth._get_user_by_id(user.id)

    assert by_email is not None
    assert by_email.id == user.id
    assert by_id is not None
    assert by_id.email == "db@example.com"
    # パスワードは平文で保存されない
    assert by_email.hashed_password != VALID_PASSWORD


def test_lookup_of_missing_user_returns_none(db_session):
    """存在しない email / id は None"""
    auth = Authenticator(db=db_session)
    assert auth._get_user_by_email("nonexist@example.com") is None
    assert auth._get_user_by_id("nonexist") is None


def test_duplicate_email_keeps_single_row(db_session):
    """同一メールアドレスは1行のみ（signup 側で拒否され、上書きされない）"""
    auth = Authenticator(db=db_session)
    assert auth.signup("dup@example.com", VALID_PASSWORD)[0] is True
    first_hash = auth._get_user_by_email("dup@example.com").hashed_password

    assert auth.signup("dup@example.com", OTHER_PASSWORD)[0] is False
    assert auth._get_user_by_email("dup@example.com").hashed_password == first_hash


def test_update_persists_plan_change(db_session):
    """プラン変更は UserRepository.update で永続化される"""
    from retro_radio.models.user import PlanType

    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup("plan@example.com", VALID_PASSWORD)

    user.plan = PlanType.PREMIUM
    auth._save_user(user)

    stored = UserRepository(db_session).get_by_id(user.id)
    assert stored.plan is PlanType.PREMIUM


def test_repository_update_rejects_unknown_user(db_session):
    """UserRepository.update は存在しない id に対して ValueError"""
    from retro_radio.models.user import User

    ghost = User(id="nope", email="nope@example.com", hashed_password="h")
    with pytest.raises(ValueError, match="not found"):
        UserRepository(db_session).update(ghost)


def test_repository_lowercases_email_on_lookup(db_session):
    """メール検索は大文字小文字を区別しない"""
    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup("Case.Test@Example.com", VALID_PASSWORD)

    assert UserRepository(db_session).get_by_email("case.test@example.com").id == user.id
    assert UserRepository(db_session).get_by_email("CASE.TEST@EXAMPLE.COM").id == user.id
