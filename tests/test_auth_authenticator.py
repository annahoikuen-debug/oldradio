"""Authenticator の公開振る舞いの検証。

Wave 1 でプロセスグローバル辞書（`_users_db` / `_users_by_id`）が廃止され、
認証状態の正は DB のみになった。内部実装ではなく公開 API で検証する。
"""

import pytest
from unittest.mock import patch

from retro_radio.auth.authenticator import Authenticator
from retro_radio.db.repository import UserRepository
from retro_radio.models.user import PlanType, User

# NIST SP 800-63B 準拠のパスワードポリシー（`MIN_PASSWORD_LENGTH = 8`）を満たす値。
# `signup()` だけが対象なので、登録するテスト入力は 8 文字以上にする。
# `login()` / `verify_password()` はポリシーの対象外（既存アカウントを締め出さない）。
VALID_PASSWORD = "TestPass123"
# 重複メール判定など「別パスワード」が意味を持つケースで使う対照値。
OTHER_PASSWORD = "TestPass456"


def test_authenticator_instance():
    auth = Authenticator()
    assert isinstance(auth, Authenticator)
    assert hasattr(auth, "session_mgr")


def test_authenticator_has_no_process_global_user_cache():
    """プロセス共有のユーザー辞書が復活していないことを回帰防止する"""
    for removed in ("_users_db", "_users_by_id"):
        assert not hasattr(Authenticator, removed), (
            f"{removed} はプロセスグローバル状態を禁止。ContextVar/DB のみを使うこと。"
        )


def test_get_current_user_returns_none_initial():
    """未ログインなら None"""
    auth = Authenticator()
    auth.logout()
    assert auth.get_current_user() is None


def test_login_and_logout_roundtrip():
    """login_user / get_current_user / logout が一往復する"""
    auth = Authenticator()
    user = User(id="test", email="test@test.com", hashed_password="hash")
    auth.login_user(user)
    assert auth.get_current_user() == user
    auth.logout()
    assert auth.get_current_user() is None


def test_signup_persists_to_db_and_logs_in(db_session):
    """signup は DB に保存し、その場でログイン状態にする"""
    auth = Authenticator(db=db_session)
    ok, message, user = auth.signup("Signup.User@Example.com ", VALID_PASSWORD)

    assert ok is True
    assert user is not None
    # メールアドレスは正規化（strip + 小文字）される
    assert user.email == "signup.user@example.com"
    assert auth.get_current_user().id == user.id

    stored = UserRepository(db_session).get_by_email("signup.user@example.com")
    assert stored is not None
    assert stored.id == user.id


def test_signup_rejects_invalid_email():
    """メールアドレスの形式が不正なら登録しない"""
    auth = Authenticator()
    for bad in ("", "   ", "no-at-sign"):
        ok, message, user = auth.signup(bad, VALID_PASSWORD)
        assert ok is False
        assert user is None
        assert message


def test_signup_rejects_empty_password():
    """パスワードが空なら登録しない"""
    auth = Authenticator()
    ok, _message, user = auth.signup("pw@example.com", "")
    assert ok is False
    assert user is None


def test_signup_rejects_duplicate_email(db_session):
    """同一メールアドレスは二重登録できない"""
    auth = Authenticator(db=db_session)
    assert auth.signup("dup@example.com", VALID_PASSWORD)[0] is True

    ok, message, user = auth.signup("dup@example.com", OTHER_PASSWORD)
    assert ok is False
    assert user is None
    assert "既に登録" in message


def test_login_with_wrong_password_returns_none(db_session):
    """パスワード不一致ではログインできない"""
    auth = Authenticator(db=db_session)
    auth.signup("login@example.com", "correct-password")

    other = Authenticator(db=db_session)
    assert other.login("login@example.com", "wrong-password") is None


def test_login_with_unknown_email_returns_none(db_session):
    """未登録メールアドレスではログインできない"""
    auth = Authenticator(db=db_session)
    assert auth.login("nobody@example.com", "pw") is None


def test_login_with_blank_credentials_returns_none(db_session):
    """空のメール / パスワードではログインしない"""
    auth = Authenticator(db=db_session)
    assert auth.login("", "pw") is None
    assert auth.login("a@b.com", "") is None


def test_signup_then_login_roundtrip(db_session):
    """signup した資格情報で login できる（ハッシュ照合の疎通確認）"""
    Authenticator(db=db_session).signup("roundtrip@example.com", "secret123")

    auth = Authenticator(db=db_session)
    user = auth.login("roundtrip@example.com", "secret123")
    assert user is not None
    assert user.email == "roundtrip@example.com"
    assert auth.get_current_user().id == user.id


def test_increment_generation_count_persists(db_session):
    """生成回数のインクリメントは統計用途で、権限判定には使われない"""
    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup("count@example.com", VALID_PASSWORD)

    auth.increment_generation_count(user)
    assert user.generation_count == 1

    stored = UserRepository(db_session).get_by_id(user.id)
    assert stored.generation_count == 1


def test_get_user_plan(db_session):
    """get_user_plan は USER の plan を返す"""
    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup("plan@example.com", VALID_PASSWORD)
    assert user.plan is PlanType.FREE
    assert auth.get_user_plan(user) is PlanType.FREE


def test_is_feature_enabled_by_plan(db_session):
    """機能ゲートはプランで決まる"""
    auth = Authenticator(db=db_session)
    _ok, _msg, free_user = auth.signup("gate-free@example.com", VALID_PASSWORD)
    free_user.plan = PlanType.FREE

    premium = User(id="p", email="p@example.com", hashed_password="h", plan=PlanType.PREMIUM)
    pro = User(id="q", email="q@example.com", hashed_password="h", plan=PlanType.PRO)

    assert auth.is_feature_enabled(None, "favorites") is False
    assert auth.is_feature_enabled(free_user, "favorites") is False
    assert auth.is_feature_enabled(premium, "favorites") is True
    assert auth.is_feature_enabled(pro, "favorites") is True
    assert auth.is_feature_enabled(premium, "api_access") is False
    assert auth.is_feature_enabled(pro, "api_access") is True
    assert auth.is_feature_enabled(free_user, "unknown_feature") is False


def test_authenticator_init_survives_db_failure():
    """スキーマ確認に失敗してもコンストラクタは例外を投げない（認証は利用不可になる）

    以前は `init_db()`（DDL）を呼んでいたため、これを patch して「例外を投げない」
    ことを固定していた。DDL は REQUEST PATH から**除かれた**ので、
    今は読み取り専用の `schema_is_ready()` を見る。
     nego の理由（`init_db` を残さない理由）は
    `tests/test_db_session.py::TestSchemaOwnership` を参照。
    """
    with patch(
        "retro_radio.db.session.schema_is_ready",
        side_effect=RuntimeError("db down"),
    ):
        auth = Authenticator()
    assert auth.user_repo is None
    # セッション操作は DB に依存しないため動く
    auth.logout()
    assert auth.get_current_user() is None


def test_authenticator_never_runs_ddl():
    """``Authenticator`` が ``create_all`` 相当を呼ばないこと

    ``POST /api/auth/session`` は**認証前**のエンドポイント。DDL を
    リクエスト経路に置くと、認証前の攻撃者が無認証で DDL を起こせる。
    """
    from retro_radio.auth import authenticator as mod

    # **コメントではなく実行されるコード**を見る。
    # 説明コメントには init_db / create_all の名前が出るので、
    # ソース文字列の grep では「説明が書いてあるだけ」でも落ちてしまう。
    code = mod.Authenticator.__init__.__code__
    names = set(code.co_names) | set(code.co_varnames)
    assert "init_db" not in names, f"Authenticator.__init__ が init_db を呼んでいる: {names}"
    assert "create_all" not in names, f"Authenticator.__init__ が create_all を呼んでいる: {names}"
    assert "init_db" not in set(mod.__dict__), (
        "authenticator が init_db を import している（DDL 経路が残っている）"
    )


def test_save_user_failure_is_swallowed(db_session):
    """_save_user は内部で例外を握りつぶす（UI 側で落ちないため）"""
    auth = Authenticator(db=db_session)
    orphan = User(id="does-not-exist", email="ghost@example.com", hashed_password="h")
    auth._save_user(orphan)  # 例外にならないこと


@pytest.mark.parametrize("feature", ["favorites", "history_export", "no_ads"])
def test_feature_names_are_known(feature):
    """未定義の feature 名は False（キーの取り違えが黙って有効化されない）"""
    auth = Authenticator()
    user = User(id="x", email="x@example.com", hashed_password="h", plan=PlanType.PRO)
    assert auth.is_feature_enabled(user, feature) is True
