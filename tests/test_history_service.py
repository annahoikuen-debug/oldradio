"""`retro_radio.services.history_service` のテスト。

生成履歴の保存は「Repository が flush だけなので commit 必須」という前提を持つ。
ここでは成功・失敗・close 失敗の各経路で commit / rollback / close の
 호출 순서가契約通りであること���固定する。
"""

from unittest.mock import MagicMock

import pytest

from retro_radio.services import history_service
from retro_radio.services.history_service import save_generation_result


@pytest.fixture
def db_and_repo(monkeypatch):
    """get_db_sync と GenerationRepository を差し替え、呼出を観測できるようにする。"""
    db = MagicMock(name="db")
    repo = MagicMock(name="GenerationRepository")
    sessions_seen = []

    def _make_repo(session):
        sessions_seen.append(session)
        return repo

    monkeypatch.setattr(history_service, "get_db_sync", lambda: db)
    monkeypatch.setattr(history_service, "GenerationRepository", _make_repo)
    return db, repo, sessions_seen


def _entry():
    return {
        "year": 1980,
        "month": 5,
        "day": 15,
        "script": "テスト原稿",
        "song_title": "テスト曲",
        "artist_name": "テストアーティスト",
    }


def test_saves_entry_and_commits(db_and_repo):
    db, repo, _ = db_and_repo
    entry = _entry()

    save_generation_result(entry, "user-1")

    repo.create.assert_called_once_with("user-1", entry)
    db.commit.assert_called_once()
    db.rollback.assert_not_called()


def test_repository_is_built_with_the_same_session(db_and_repo):
    """Repository に渡す Session と、実際の commit 対象が同一であること。"""
    db, _repo, sessions_seen = db_and_repo

    save_generation_result(_entry(), "user-1")

    assert sessions_seen == [db]


def test_always_closes_session_on_success(db_and_repo):
    db, _repo, _ = db_and_repo

    save_generation_result(_entry(), "user-1")

    db.close.assert_called_once()


def test_rolls_back_and_closes_then_reraises(db_and_repo):
    db, repo, _ = db_and_repo
    repo.create.side_effect = RuntimeError("DB exploded")

    with pytest.raises(RuntimeError, match="DB exploded"):
        save_generation_result(_entry(), "user-1")

    db.rollback.assert_called_once()
    db.commit.assert_not_called()
    db.close.assert_called_once()


def test_rollback_happens_even_when_commit_fails(db_and_repo):
    """commit 自体の失敗でもロールバックして例外を伝播すること。"""
    db, _repo, _ = db_and_repo
    db.commit.side_effect = RuntimeError("commit failed")

    with pytest.raises(RuntimeError, match="commit failed"):
        save_generation_result(_entry(), "user-1")

    db.rollback.assert_called_once()
    db.close.assert_called_once()


def test_close_failure_does_not_mask_original_error(db_and_repo):
    """close が壊れても元の例外がFatal ではない。"""
    db, repo, _ = db_and_repo
    repo.create.side_effect = ValueError("bad entry")
    db.close.side_effect = OSError("close failed")

    with pytest.raises(ValueError, match="bad entry"):
        save_generation_result(_entry(), "user-1")

    db.rollback.assert_called_once()


def test_uses_fresh_session_per_call(monkeypatch):
    """呼び出しごとに新しいセッションを取得すること（使い回しはしない）。"""
    sessions = [MagicMock(name=f"db{i}") for i in range(2)]
    get_db_sync = MagicMock(side_effect=sessions)
    repo = MagicMock(name="GenerationRepository")

    monkeypatch.setattr(history_service, "get_db_sync", get_db_sync)
    monkeypatch.setattr(history_service, "GenerationRepository", lambda session: repo)

    save_generation_result(_entry(), "u")
    save_generation_result(_entry(), "u")

    assert get_db_sync.call_count == 2
    assert sessions[0].commit.call_count == 1
    assert sessions[1].commit.call_count == 1
    assert sessions[0] is not sessions[1]


def test_empty_entry_is_still_forwarded(db_and_repo):
    """空 dict でもリポジトリまで到達すること（バリデーション責務は別モジュール）。"""
    _db, repo, _ = db_and_repo

    save_generation_result({}, "u")

    repo.create.assert_called_once_with("u", {})
