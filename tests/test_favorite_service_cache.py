"""`retro_radio.services.favorite_service` のキャッシュ・リトライ・ロールバックのテスト。

既存の `test_favorite_service.py` は正常系の1経路しか通しておらず、
- 5分 TTL のキャッシュが効くか / 失効後に再取得するか
- DB が Boud を吐いたとき 3回リトライして最終的に False/[] を返すか
- リトライ間のロールバックがdeo ているか
- context manager（`with`）で確実にクローズされるか

という本番運用で効いてくる経路が未実行だった。
ここではそれらを固定する。
"""

from unittest.mock import MagicMock

import pytest

from retro_radio.services import favorite_service
from retro_radio.services.favorite_service import FavoriteService


def _generation(gid="gen-1", year=1980):
    return {
        "id": gid,
        "year": year,
        "month": 5,
        "day": 15,
        "script": "テスト原稿",
        "song_title": "テスト曲",
        "artist_name": "テストアーティスト",
        "preview_url": "http://example.com/preview.mp3",
    }


@pytest.fixture
def service(monkeypatch):
    """FavoriteService をモックリポジトリ付きで組み立てる。"""
    db = MagicMock(name="db")
    fav_repo = MagicMock(name="FavoriteRepository")
    gen_repo = MagicMock(name="GenerationRepository")

    monkeypatch.setattr(favorite_service, "get_db_sync", lambda: db)
    monkeypatch.setattr(favorite_service, "FavoriteRepository", lambda session: fav_repo)
    monkeypatch.setattr(favorite_service, "GenerationRepository", lambda session: gen_repo)

    svc = FavoriteService()
    svc.db = db
    svc.fav_repo = fav_repo
    svc.gen_repo = gen_repo
    return svc


# --- 追加時のコミットとキャッシュ無効化 -----------------------------------------
def test_add_favorite_commits(service):
    service.fav_repo.add.return_value = True

    assert service.add_favorite("u1", "g1") is True
    service.db.commit.assert_called_once()


def test_add_favorite_does_not_commit_when_repo_returns_false(service):
    """追加できなかった場合、無意味な commit をしないこと。"""
    service.fav_repo.add.return_value = False

    assert service.add_favorite("u1", "g1") is False
    service.db.commit.assert_not_called()


def test_add_favorite_invalidates_user_cache(service):
    """追加後はそのユーザーのキャッシュを捨てる（古い一覧が漏れる）。"""
    service.fav_repo.add.return_value = True
    service._favorites_cache["u1"] = ([_generation()], 9999999999.0)

    service.add_favorite("u1", "g1")

    assert "u1" not in service._favorites_cache


def test_add_favorite_survives_empty_cache(service):
    service.fav_repo.add.return_value = True

    assert service.add_favorite("u1", "g1") is True


# --- 削除時 ---------------------------------------------------------------------
def test_remove_favorite_commits(service):
    service.fav_repo.remove.return_value = True

    assert service.remove_favorite("u1", "g1") is True
    service.db.commit.assert_called_once()


def test_remove_favorite_invalidates_user_cache(service):
    service.fav_repo.remove.return_value = True
    service._favorites_cache["u1"] = ([_generation()], 9999999999.0)

    service.remove_favorite("u1", "g1")

    assert "u1" not in service._favorites_cache


# --- 判定はキャッシュしない ------------------------------------------------------
def test_is_favorite_always_hits_db(service):
    """判定はリアルタイム性が必要なためキャッシュを使わない。"""
    service.fav_repo.is_favorite.return_value = True

    service.is_favorite("u1", "g1")
    service.is_favorite("u1", "g1")

    assert service.fav_repo.is_favorite.call_count == 2


# --- 取得時のキャッシュ ----------------------------------------------------------
def test_get_favorites_caches_result(service):
    service.fav_repo.get_user_favorites.return_value = ["g1"]
    service.gen_repo.get_by_id.return_value = _generation()

    first = service.get_favorites("u1")
    second = service.get_favorites("u1")

    assert service.fav_repo.get_user_favorites.call_count == 1, "キャッシュが効いていない"
    assert first == second


def test_cache_expires_after_timeout(service, monkeypatch):
    """TTL を超えると再取得する（他タブで追加された-MSLA 反映elayになる）。"""
    service.fav_repo.get_user_favorites.return_value = ["g1"]
    service.gen_repo.get_by_id.return_value = _generation()

    now = [1000.0]
    monkeypatch.setattr(favorite_service.time, "time", lambda: now[0])

    service.get_favorites("u1")
    now[0] = 1000.0 + service._cache_timeout - 1
    service.get_favorites("u1")
    assert service.fav_repo.get_user_favorites.call_count == 1, "TTL 内で再取得している"

    now[0] = 1000.0 + service._cache_timeout + 1
    service.get_favorites("u1")
    assert service.fav_repo.get_user_favorites.call_count == 2


def test_cache_is_per_user(service):
    service.fav_repo.get_user_favorites.return_value = ["g1"]
    service.gen_repo.get_by_id.return_value = _generation()

    service.get_favorites("u1")
    service.get_favorites("u2")

    assert service.fav_repo.get_user_favorites.call_count == 2


def test_missing_generation_records_are_skipped(service):
    """DB に残っていない favorites ID は黙って落とす。"""
    service.fav_repo.get_user_favorites.return_value = ["g1", "ghost"]
    service.gen_repo.get_by_id.side_effect = [_generation("g1"), None]

    favorites = service.get_favorites("u1")

    assert [f["id"] for f in favorites] == ["g1"]


def test_empty_favorites_is_cached_as_empty_list(service):
    """空結果もキャッシュする（毎回DBに“行ってくるのは無駄）。"""
    service.fav_repo.get_user_favorites.return_value = []

    assert service.get_favorites("u1") == []
    service.get_favorites("u1")

    assert service.fav_repo.get_user_favorites.call_count == 1


# --- キャッシュの掃除 -------------------------------------------------------------
def test_cleanup_cache_removes_expired_entries_only(service, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(favorite_service.time, "time", lambda: now[0])

    service._favorites_cache = {
        "fresh": ([], 1000.0),
        "stale": ([], 0.0),
    }
    service._last_cleanup = 0.0
    now[0] = 2000.0

    service._cleanup_cache()

    assert "fresh" not in service._favorites_cache, "TTL 内のエントリまで消している"
    assert "stale" not in service._favorites_cache


def test_cleanup_cache_is_throttled_to_once_a_minute(service, monkeypatch):
    """1分に1回しか走らない（全面走査のコストを避ける）。"""
    now = [1000.0]
    monkeypatch.setattr(favorite_service.time, "time", lambda: now[0])

    service._favorites_cache = {"stale": ([], 0.0)}
    service._last_cleanup = 990.0  # 10秒前

    service._cleanup_cache()

    assert "stale" in service._favorites_cache, "スロットリング間隔内に実行されている"
    assert service._last_cleanup == 990.0


def test_cleanup_cache_resets_the_timer(service, monkeypatch):
    now = [2000.0]
    monkeypatch.setattr(favorite_service.time, "time", lambda: now[0])

    service._favorites_cache = {}
    service._last_cleanup = 0.0

    service._cleanup_cache()

    assert service._last_cleanup == 2000.0


# --- リトライ --------------------------------------------------------------------
def test_retries_until_success(service, monkeypatch):
    """一時的な失敗ならリトライで回復する。"""
    monkeypatch.setattr(favorite_service.time, "sleep", lambda seconds: None)
    service.fav_repo.get_user_favorites.side_effect = [
        RuntimeError("locked"),
        RuntimeError("locked"),
        ["g1"],
    ]
    service.gen_repo.get_by_id.return_value = _generation()

    assert len(service.get_favorites("u1")) == 1
    assert service.fav_repo.get_user_favorites.call_count == 3


def test_rolls_back_between_retries(service, monkeypatch):
    """失敗した試行はロールバックしてから次へ進む（セッションが汚れるため）。"""
    monkeypatch.setattr(favorite_service.time, "sleep", lambda seconds: None)
    service.fav_repo.is_favorite.side_effect = [RuntimeError("locked"), True]

    assert service.is_favorite("u1", "g1") is True
    service.db.rollback.assert_called_once()


def test_gives_up_after_max_retries_and_returns_false(service, monkeypatch):
    monkeypatch.setattr(favorite_service.time, "sleep", lambda seconds: None)
    service.fav_repo.add.side_effect = RuntimeError("down")

    assert service.add_favorite("u1", "g1") is False
    assert service.fav_repo.add.call_count == 3


def test_gives_up_after_max_retries_and_returns_empty_list(service, monkeypatch):
    monkeypatch.setattr(favorite_service.time, "sleep", lambda seconds: None)
    service.fav_repo.get_user_favorites.side_effect = RuntimeError("down")

    assert service.get_favorites("u1") == []
    assert service.fav_repo.get_user_favorites.call_count == 3


def test_gives_up_after_max_retries_and_returns_false_for_is_favorite(
    service, monkeypatch
):
    monkeypatch.setattr(favorite_service.time, "sleep", lambda seconds: None)
    service.fav_repo.is_favorite.side_effect = RuntimeError("down")

    assert service.is_favorite("u1", "g1") is False


def test_sleeps_between_retries_not_after_the_last(service, monkeypatch):
    """最後の試行後にまで sleep すると無駄な1秒遅延になる。"""
    slept = []
    monkeypatch.setattr(favorite_service.time, "sleep", slept.append)
    service.fav_repo.add.side_effect = RuntimeError("down")

    service.add_favorite("u1", "g1")

    assert len(slept) == 2, "3回試行のうち sleep は2回のはず"


def test_rollback_failure_does_not_break_retry(service, monkeypatch):
    """ロールバック自体が壊れてもリトライは継続する。"""
    monkeypatch.setattr(favorite_service.time, "sleep", lambda seconds: None)
    service.db.rollback.side_effect = RuntimeError("rollback failed")
    service.fav_repo.is_favorite.side_effect = [RuntimeError("locked"), True]

    assert service.is_favorite("u1", "g1") is True


def test_original_error_is_reraised_by_retry_helper(service, monkeypatch):
    """ヘルパは False に潰さず再送出する（呼び出し側が責務を持つ）。"""
    monkeypatch.setattr(favorite_service.time, "sleep", lambda seconds: None)
    service.fav_repo.add.side_effect = RuntimeError("down")

    with pytest.raises(RuntimeError, match="down"):
        service._retry_db_operation(lambda: service.fav_repo.add("u1", "g1"))


# --- リソース管理 -----------------------------------------------------------------
def test_close_closes_db_and_clears_cache(service):
    service._favorites_cache["u1"] = ([], 0.0)

    service.close()

    service.db.close.assert_called_once()
    assert service._favorites_cache == {}


def test_context_manager_closes_on_success(service):
    with service as ctx:
        assert ctx is service

    service.db.close.assert_called_once()


def test_context_manager_closes_on_exception(service):
    with pytest.raises(RuntimeError):
        with service:
            raise RuntimeError("boom")

    service.db.close.assert_called_once()


def test_context_manager_does_not_swallow_exceptions(service):
    """`__exit__` が False を返さないと例外が握り潰される。"""
    with pytest.raises(RuntimeError, match="boom"):
        with service:
            raise RuntimeError("boom")
