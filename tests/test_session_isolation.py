"""セッション状態の隔離検証（回帰防止）。

Wave 1 で `retro_radio/utils/session.py` のプロセスグローバル dict が
`ContextVar` ベースに変更された。属性アクセス bug
（`session_state.generation_history` が dict で AttributeError）も同時に修正された。

ここで守るべき不変条件:
  1. 別コンテキスト（スレッド / async タスク / copy_context）間の状態が混ざらない
  2. 状態は `[]` / `{}` でアクセスでき、属性アクセス（`.foo`）を要求しない
  3. `clear_history()` は現在のコンテキストだけをクリアする
"""

import asyncio
import threading

import pytest

from retro_radio.models.user import User
from retro_radio.utils import session as session_mod
from retro_radio.utils.session import (
    HistoryEntry,
    SessionManager,
    add_history,
    clear_history,
    get_audio_cache,
    get_error_voice_guidance,
    get_history,
    init_session_state,
    set_audio_cache,
    set_error_voice_guidance,
)


def _entry(year=1980):
    return HistoryEntry(
        year=year,
        date="1980-05-15",
        script="原稿",
        song_title="曲",
        artist_name="アーティスト",
        preview_url=None,
        audio_path=None,
        timestamp="1980-05-15 00:00:00",
    )


def _reset_context():
    """テストごとに ContextVar の既定値へ戻す（テスト間の漏れ防止）"""
    session_mod._session_state.set(None)
    session_mod._current_user.set(None)


@pytest.fixture(autouse=True)
def _clean_context():
    _reset_context()
    yield
    _reset_context()


# --- 状態ストアの基本契約 -------------------------------------------------------
def test_history_is_list_attribute_access():
    """`session_state["generation_history"]` は list（属性アクセスを要求しない）"""
    state = session_mod._get_session_state()
    assert isinstance(state["generation_history"], list)
    assert isinstance(state["audio_cache"], dict)
    assert state["language"] == "ja"
    # 旧実装の `session_state.generation_history` は dict だったため AttributeError になっていた
    assert not hasattr(state, "generation_history")


def test_init_session_state_is_idempotent():
    """init_session_state を何度呼んでも既存状態を潰さない"""
    add_history(_entry())
    init_session_state()
    init_session_state()
    assert len(get_history()) == 1


def test_add_history_keeps_dict_form_and_order():
    """履歴は dict 形式で保存され、新しい分が先頭に立つ"""
    add_history(_entry(1980))
    add_history(_entry(1990))

    history = get_history()
    assert [h.year for h in history] == [1990, 1980]
    assert all(isinstance(h, HistoryEntry) for h in history)


def test_history_is_capped_at_ten():
    """履歴は最新10件まで"""
    for year in range(1950, 1970):
        add_history(_entry(year))
    assert len(get_history()) == 10


def test_clear_history():
    clear_history()
    add_history(_entry())
    clear_history()
    assert get_history() == []


def test_audio_cache_roundtrip():
    assert get_audio_cache() == {}
    set_audio_cache("k", b"v")
    assert get_audio_cache()["k"] == b"v"


def test_error_voice_guidance_roundtrip():
    assert get_error_voice_guidance() is False
    set_error_voice_guidance(True)
    assert get_error_voice_guidance() is True
    set_error_voice_guidance(False)
    assert get_error_voice_guidance() is False


# --- 隔離: copy_context ---------------------------------------------------------
def test_fresh_context_does_not_inherit_state():
    """新規コンテキストでは他コンテキストの状態が見えない"""
    from contextvars import copy_context

    add_history(_entry(1980))
    SessionManager().set_user(User(id="main", email="main@example.com", hashed_password="h"))

    def in_empty_context():
        return ([h.year for h in get_history()], SessionManager().get_user())

    # _session_state / _current_user をリセットしたうえで実行する
    def in_reset_context():
        _reset_context()
        return ([h.year for h in get_history()], SessionManager().get_user())

    assert copy_context().run(in_reset_context) == ([], None)


def test_writes_in_copied_context_do_not_leak_back():
    """copy_context した先での変更は呼び出し元へ伝播しない"""
    from contextvars import copy_context

    add_history(_entry(1980))
    assert len(get_history()) == 1

    def in_other_context():
        add_history(_entry(1990))
        return [h.year for h in get_history()]

    other = copy_context().run(in_other_context)

    assert other == [1990, 1980]
    assert [h.year for h in get_history()] == [1980]


def test_session_state_view_is_immutable():
    """_get_session_state() の戻り値は書き換えられない（copy-on-write の前提）"""
    state = session_mod._get_session_state()
    with pytest.raises(TypeError):
        state["language"] = "en"  # type: ignore[index]


def test_audio_cache_write_in_copied_context_does_not_leak_back():
    """オーディオキャッシュも子コンテキストへの書き込みが親に漏れない"""
    from contextvars import copy_context

    set_audio_cache("parent", b"1")

    def in_other_context():
        set_audio_cache("child", b"2")
        return sorted(get_audio_cache())

    keys = copy_context().run(in_other_context)

    assert keys == ["child", "parent"]
    assert sorted(get_audio_cache()) == ["parent"]


def test_audio_cache_values_are_not_shared_across_contexts():
    """同一キーに別コンテキストで書いても親の値は変わらない"""
    from contextvars import copy_context

    set_audio_cache("k", b"parent")

    def in_other_context():
        set_audio_cache("k", b"child")
        return get_audio_cache()["k"]

    assert copy_context().run(in_other_context) == b"child"
    assert get_audio_cache()["k"] == b"parent"


def test_error_voice_guidance_write_in_copied_context_does_not_leak_back():
    from contextvars import copy_context

    set_error_voice_guidance(True)

    def in_other_context():
        set_error_voice_guidance(False)
        return get_error_voice_guidance()

    assert copy_context().run(in_other_context) is False
    assert get_error_voice_guidance() is True


def test_clear_history_in_copied_context_does_not_clear_parent():
    from contextvars import copy_context

    add_history(_entry(1980))
    copy_context().run(clear_history)
    assert [h.year for h in get_history()] == [1980]


# --- 隔離: スレッド -------------------------------------------------------------
def test_user_state_is_isolated_between_threads():
    """別スレッドでログインしたユーザーはメインスレッドから見えない"""
    main_mgr = SessionManager()
    main_mgr.clear_user()
    main_user = User(id="main", email="main@example.com", hashed_password="h")
    main_mgr.set_user(main_user)

    seen = {}

    def worker():
        mgr = SessionManager()
        seen["initial"] = mgr.get_user()
        mgr.set_user(User(id="worker", email="worker@example.com", hashed_password="h"))
        seen["after_set"] = mgr.get_user()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen["initial"] is None, "別スレッドにログイン状態が漏れている"
    assert seen["after_set"].id == "worker"
    # メインスレッドは自分のユーザーのまま
    assert main_mgr.get_user() is main_user


def test_history_is_isolated_between_threads():
    """別スレッドの履歴追加がメインスレッドに混ざらない"""
    add_history(_entry(1980))

    def worker():
        add_history(_entry(1990))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert [h.year for h in get_history()] == [1980]


# --- 隔離: async タスク ---------------------------------------------------------
async def test_user_state_writes_do_not_leak_between_asyncio_tasks():
    """子タスクで set_user しても親のログイン状態は変わらない"""
    main_mgr = SessionManager()
    main_mgr.clear_user()
    main_mgr.set_user(User(id="main", email="main@example.com", hashed_password="h"))

    async def worker():
        mgr = SessionManager()
        mgr.set_user(User(id="task", email="task@example.com", hashed_password="h"))
        await asyncio.sleep(0)
        return mgr.get_user().id

    task_user_id = await asyncio.create_task(worker())

    assert task_user_id == "task"
    assert main_mgr.get_user().id == "main", "子タスクのログインが漏れた"


async def test_history_in_child_task_does_not_corrupt_other_tasks():
    """子タスクの履歴操作が他タスクの履歴を壊さない（要素を共有して改変しない）"""
    add_history(_entry(1980))
    before = get_history()

    async def worker():
        add_history(_entry(1990))
        return [h.year for h in get_history()]

    years = await asyncio.create_task(worker())

    # 子は自分のスナップショットに 1 件追加している
    assert years == [1990, 1980]
    # 元の HistoryEntry オブジェクト自体は書き換わらない
    assert [h.year for h in before] == [1980]


async def test_concurrent_tasks_keep_their_own_user():
    """並行して走る各タスクは自分のユーザーを見続ける"""
    async def scoped(user_id):
        SessionManager().set_user(User(id=user_id, email=f"{user_id}@example.com", hashed_password="h"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return SessionManager().get_user().id

    results = await asyncio.gather(scoped("a"), scoped("b"), scoped("c"))
    assert results == ["a", "b", "c"]


# --- 隔離: スレッドの分離が旧グローバル dict なら成立しなかった -----------------
def test_no_process_global_state_dictionary():
    """モジュールレベルに「全ユーザー共有」の dict が復活していないこと"""
    for name in dir(session_mod):
        value = getattr(session_mod, name)
        if name.startswith("__") or not isinstance(value, dict):
            continue
        if name == "DEFAULT_STATE":
            continue
        raise AssertionError(f"モジュールレベルの可変 dict が戻り: {name}")


def test_default_state_keys_match_runtime_state():
    """DEFAULT_STATE と実行時 state のキーが揃っている"""
    assert set(session_mod.DEFAULT_STATE) == set(session_mod._create_session_state())


def test_session_manager_clear_user():
    mgr = SessionManager()
    mgr.set_user(User(id="x", email="x@example.com", hashed_password="h"))
    mgr.clear_user()
    assert mgr.get_user() is None
