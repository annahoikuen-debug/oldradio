"""SessionManager の基本契約。

旧実装は `st.session_state`（プロセスグローバル dict）を前提としていたため、
`patch('retro_radio.utils.session.st')` を必要としていた。
Streamlit 廃止により `retro_radio/utils/session.py` に `st` は存在しない。
文脈依存の隔離検証は `tests/test_session_isolation.py` を参照。
"""

from retro_radio.models.user import User
from retro_radio.utils.session import SessionManager


def test_session_module_has_no_streamlit_dependency():
    """Streamlit が再導入されていないこと"""
    from retro_radio.utils import session as session_module

    assert not hasattr(session_module, "st")


def test_session_manager_user_roundtrip():
    mgr = SessionManager()
    mgr.clear_user()
    assert mgr.get_user() is None

    user = User(id="test", email="test@test.com", hashed_password="hash")
    mgr.set_user(user)
    assert mgr.get_user() == user

    mgr.clear_user()
    assert mgr.get_user() is None


def test_session_manager_has_no_st_session_state_attribute():
    """旧実装の `st.session_state` 互換属性を装っていないこと"""
    mgr = SessionManager()
    assert not hasattr(mgr, "session_state")


def test_default_language_is_japanese():
    from retro_radio.utils.session import DEFAULT_STATE

    assert DEFAULT_STATE["language"] == "ja"
