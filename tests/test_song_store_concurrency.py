"""同時実行とキャッシュの意味論に関する回帰テスト。

実測していた不具合:

* ``services.song_store`` が接続のたびに ``PRAGMA journal_mode=WAL`` を
  走らせていたため、並列生成で ``database is locked`` が起き、
  ``record()`` の失敗が握り潰されてローテーションが進まなかった。
* ``core.preview_resolver`` が iTunes の一時的な失敗を
  「この曲には音源が無い」として共有キャッシュに書き込み、
  iTunes 復旧後も 6 時間そのままだった。
"""

from __future__ import annotations

import inspect
import sqlite3
import threading
import time

import pytest
import requests

from retro_radio.core import preview_resolver as pr
from retro_radio.services.song_store import PreviewCache, SongHistoryStore


# --------------------------------------------------------------------------- #
# 1. ストアの同時実行
# --------------------------------------------------------------------------- #


def test_song_store_is_singleton_per_path(tmp_path):
    """同じパスなら同じ ``_SqliteStore`` が返る（DDL / pragma が 1 度だけ）。"""
    path = str(tmp_path / "song.db")
    first = SongHistoryStore(path)
    second = SongHistoryStore(path)
    assert first._store is second._store


def test_busy_timeout_is_set_before_journal_mode(tmp_path):
    """``journal_mode`` を触る前に ``busy_timeout`` が効いていること。"""
    store = SongHistoryStore(str(tmp_path / "order.db"))
    store.ensure_schema()
    seen = {}

    with store._store.connect() as conn:
        seen["busy"] = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        seen["mode"] = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert int(seen["busy"]) > 0
    assert str(seen["mode"]).lower() == "wal"


def test_concurrent_records_do_not_lock_or_lose_writes(tmp_path):
    """N スレッドが同時に記録しても `database is locked` が出ず、書きこぼさない。"""
    path = str(tmp_path / "hammer.db")
    threads_count = 8
    per_thread = 25
    errors: list = []
    barrier = threading.Barrier(threads_count)

    def worker(worker_id: int) -> None:
        # サーバと同じ挙動を再現するため、スレッドごとに新しいストアを作る。
        store = SongHistoryStore(path)
        try:
            barrier.wait(timeout=10)
            for index in range(per_thread):
                year = 1970 + (worker_id % 5)
                store.record(year, [f"song_{worker_id}_{index}"])
                store.last_played(year)
        except Exception as exc:  # noqa: BLE001 - 記録して後で検証する
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"同時実行で例外が出ました: {errors}"
    assert not any(isinstance(exc, sqlite3.OperationalError) for exc in errors)

    store = SongHistoryStore(path)
    seen: set = set()
    for year in range(1970, 1975):
        seen |= set(store.last_played(year))
    assert len(seen) == threads_count * per_thread


def test_recorded_sequence_numbers_are_unique_across_stores(tmp_path):
    """リクエストごとに新しいストアでも再生番号が重複しない（順序の不変条件）。"""
    path = str(tmp_path / "seq.db")
    first = SongHistoryStore(path)
    first.record(1980, ["a", "b"])

    # サーバは要求ごとに新しいストアを生成する。カウンタはプロセス内ではない。
    second = SongHistoryStore(path)
    second.record(1980, ["c"])

    history = SongHistoryStore(path).last_played(1980)
    assert history == {"a": 1, "b": 2, "c": 3}
    assert sorted(history.values()) == [1, 2, 3]


def test_sequence_survives_restart_with_existing_history(tmp_path):
    """既存の再生番号より小さい番号を再利用しない（再起動後）。"""
    path = str(tmp_path / "restart.db")
    store = SongHistoryStore(path)
    store.record(1990, ["x", "y", "z"])
    del store

    reopened = SongHistoryStore(path)
    reopened.record(1990, ["w"])
    history = SongHistoryStore(path).last_played(1990)
    assert history["w"] == 4
    assert sorted(history.values()) == [1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# 2. 音源キャッシュの意味論
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, payload=None, status_code: int = 200) -> None:
        self._payload = payload if payload is not None else {"results": []}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def clean_breaker():
    pr._reset_breaker()
    yield
    pr._reset_breaker()


def test_transport_failure_does_not_write_negative_cache(tmp_path, monkeypatch, clean_breaker):
    """5xx / タイムアウトは「音源なし」をキャッシュに書かない。"""
    cache = PreviewCache(str(tmp_path / "preview.db"))

    def _boom(*args, **kwargs):
        raise requests.ConnectionError("connection reset by peer")

    monkeypatch.setattr(pr.requests, "get", _boom)

    assert pr.resolve_preview("卒業写真", "荒井由実", cache=cache) is None
    assert cache.stats() == {"total": 0, "with_preview": 0}

    # iTunes 復旧後はすぐ再解決できる（6 時間待たされない）。
    monkeypatch.setattr(
        pr.requests,
        "get",
        lambda *a, **k: _FakeResponse(
            {
                "results": [
                    {
                        "trackName": "卒業写真",
                        "artistName": "荒井由実",
                        "previewUrl": "https://example.test/p.m4a",
                        "artworkUrl100": "https://example.test/a.jpg",
                    }
                ]
            }
        ),
    )
    resolved = pr.resolve_preview("卒業写真", "荒井由実", cache=cache)
    assert resolved and resolved["preview_url"] == "https://example.test/p.m4a"


def test_genuine_miss_writes_negative_cache(tmp_path, monkeypatch, clean_breaker):
    """応答が正常で一致する音源が無い場合は、否定キャッシュに記録する。"""
    cache = PreviewCache(str(tmp_path / "preview.db"))
    monkeypatch.setattr(pr.requests, "get", lambda *a, **k: _FakeResponse({"results": []}))

    assert pr.resolve_preview("無い曲", "無いアーティスト", cache=cache) is None
    stats = cache.stats()
    assert stats["total"] == 1
    assert stats["with_preview"] == 0

    # 否定キャッシュは次の呼び出しで効いている（再問い合わせしない）。
    def _fail(*args, **kwargs):  # pragma: no cover - 呼ばれてはいけない
        raise AssertionError("否定キャッシュが効いていません")

    monkeypatch.setattr(pr.requests, "get", _fail)
    assert pr.resolve_preview("無い曲", "無いアーティスト", cache=cache) is None


def test_negative_cache_expires_under_short_ttl(tmp_path, monkeypatch, clean_breaker):
    """否定キャッシュは短縮した TTL で失効する。"""
    cache = PreviewCache(str(tmp_path / "preview.db"), negative_ttl_seconds=60.0)
    monkeypatch.setattr(pr.requests, "get", lambda *a, **k: _FakeResponse({"results": []}))
    pr.resolve_preview("無い曲", "なし", cache=cache)
    key = pr.song_key("無い曲", "なし")
    assert cache.get(key) == {
        "preview_url": None,
        "artwork_url": None,
        "track_view_url": None,
    }

    later = time.time() + 61.0
    monkeypatch.setattr(pr.time, "time", lambda: later)
    assert cache.get(key) is None


def test_default_negative_ttl_is_short():
    """既定の否定 TTL は 6 時間ではなく分単位。"""
    default = inspect.signature(PreviewCache.__init__).parameters["negative_ttl_seconds"].default
    assert default <= 300.0


def test_breaker_stops_requests_while_itunes_is_down(tmp_path, monkeypatch, clean_breaker):
    """連続した到達不能で問い合わせを止め、時間予算の消費を避ける。"""
    calls: list = []

    def _boom(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError("network is unreachable")

    monkeypatch.setattr(pr.requests, "get", _boom)
    for _ in range(6):
        pr.resolve_preview("曲", "アーティスト", cache=None)
    assert len(calls) < 6
