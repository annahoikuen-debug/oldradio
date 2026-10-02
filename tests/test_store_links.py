"""Apple への送客導線（store link）の回帰テスト。

iTunes Search API はプレビュー URL に加えて `trackViewUrl`（Apple Music の
楽曲ページ）を返す。この導線を特约規約に沿って

1. 素通しせず Invisible Affiliate パラメータ（`at` / `ct`）を付けて返す
2. Apple 以外のホストを導線として通さない
3. 未登録トークンを推測で付けない
4. UI に「Apple Music で聴く」導線と帰属表記を出す

ことを固定する。
"""

import pytest

from retro_radio.core.preview_resolver import store_link
from retro_radio.services.song_store import PreviewCache


APPLE_URL = "https://music.apple.com/jp/album/%E7%A5%9E%E7%94%B0%E5%B7%9D/12345?i=6789"


# ==============================================================================
# 1. store_link: アフィリエイトパラメータの付与
# ==============================================================================
@pytest.fixture
def affiliate(monkeypatch):
    """`store_link` が読む設定を隔離で差し替える。

    `get_settings()` は `lru_cache` 済みの **共有インスタンス** を返すため、
    値を直接書き換えると後続テストまで漏れる。`monkeypatch.setattr` は
    テスト終了時に必ず元へ戻すため、漏れが起きない。
    """

    def _set(token: str = "", campaign: str = "") -> None:
        monkeypatch.setattr(
            "retro_radio.core.preview_resolver.settings",
            _StubSettings(token, campaign),
        )

    return _set


class TestStoreLink:
    def test_keeps_url_when_no_affiliate_token(self, affiliate):
        """トークン未設定なら**素通し**（推測でパラメータを足さない）。"""
        affiliate("", "")
        assert store_link(APPLE_URL) == APPLE_URL

    def test_appends_affiliate_token(self, affiliate):
        affiliate("1000l3eMk", "")
        result = store_link(APPLE_URL)
        assert result is not None
        assert "at=1000l3eMk" in result
        # 元のクエリ（`i=`）は保持されなければならない
        assert "i=6789" in result

    def test_appends_campaign_token(self, affiliate):
        affiliate("1000l3eMk", "retro_radio_ja")
        result = store_link(APPLE_URL)
        assert "ct=retro_radio_ja" in result

    def test_does_not_duplicate_existing_affiliate_params(self, affiliate):
        """iTunes が既に `at` を付けて返しても、二重に積まない。"""
        affiliate("1000l3eMk", "")
        result = store_link(APPLE_URL + "&at=old")
        assert result.count("at=") == 1
        assert "at=old" not in result

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "",
            "   ",
            "http://music.apple.com/jp/album/x/1",  # http は不可
            "https://evil.example.com/steal",  # 他ホスト
            "javascript:alert(1)",
        ],
    )
    def test_rejects_non_apple_urls(self, bad, affiliate):
        affiliate("1000l3eMk", "")
        assert store_link(bad) is None

    def test_accepts_itunes_host(self, affiliate):
        """旧形式（itunes.apple.com）の URL も導線として通さない。"""
        affiliate("", "")
        url = "https://itunes.apple.com/jp/album/x/1?i=2"
        assert store_link(url) == url


class _StubSettings:
    """`store_link` が読む項目だけを持つ Settings 相当。"""

    def __init__(self, token: str, campaign: str) -> None:
        self.itunes_affiliate_token = token
        self.itunes_affiliate_campaign = campaign


# ==============================================================================
# 2. キャッシュ: 導線を覚えており、再解決しない
# ==============================================================================
def test_cache_remembers_store_url(tmp_path):
    cache = PreviewCache(str(tmp_path / "p.db"))
    cache.put("key", "http://x/1.m4a", "http://x/1.jpg", APPLE_URL)
    assert cache.get("key")["track_view_url"] == APPLE_URL


def test_cache_migrates_legacy_rows_without_store_url(tmp_path):
    """既存 DB（`track_view_url` を持たない行）でも読み書きできる。"""
    path = tmp_path / "legacy.db"
    fresh = PreviewCache(str(path))
    fresh.ensure_schema()

    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE song_preview")
    conn.execute(
        "CREATE TABLE song_preview ("
        "song_key TEXT PRIMARY KEY, preview_url TEXT, "
        "artwork_url TEXT, checked_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO song_preview VALUES ('legacy', 'http://x/1.m4a', "
        "'http://x/1.jpg', 9999999999.0)",
    )
    conn.commit()
    conn.close()

    # 新しいインスタンスが起動した時点で、その場で移行する。
    reopened = PreviewCache(str(path))
    reopened.ensure_schema()
    assert reopened.get("legacy") == {
        "preview_url": "http://x/1.m4a",
        "artwork_url": "http://x/1.jpg",
        "track_view_url": None,
    }


# ==============================================================================
# 3. API: songs / playlist に導線が出る
# ==============================================================================
def test_song_dicts_include_store_url():
    from retro_radio.server import _to_song_dicts

    songs = _to_song_dicts(
        [
            {
                "trackName": "神田川",
                "artistName": "南こうせつとかぐや姫",
                "previewUrl": "https://audio-ssl.itunes.apple.com/x.m4a",
                "trackViewUrl": APPLE_URL,
            },
            {
                "trackName": "間奏用の曲",
                "artistName": "誰か",
                "previewUrl": None,
                "trackViewUrl": APPLE_URL,
            },
        ]
    )
    assert songs[0]["store_url"] == APPLE_URL
    # 音源が無い間奏に導線を出すのは嘘になる
    assert songs[1]["store_url"] is None


def test_song_dicts_respect_store_link_toggle(monkeypatch):
    from retro_radio import server as server_module

    monkeypatch.setattr(
        server_module, "settings", _toggle_settings(show=False), raising=False
    )
    songs = server_module._to_song_dicts(
        [
            {
                "trackName": "神田川",
                "artistName": "南こうせつとかぐや姫",
                "previewUrl": "https://audio-ssl.itunes.apple.com/x.m4a",
                "trackViewUrl": APPLE_URL,
            }
        ]
    )
    assert songs[0]["store_url"] is None


class _ToggleSettings:
    """`_show_store_links()` が読む設定だけを持つ Settings 相当。"""

    def __init__(self, show: bool) -> None:
        self.itunes_show_store_links = show


def _toggle_settings(show: bool):
    return _ToggleSettings(show)


def test_song_playlist_item_carries_store_url_in_metadata():
    from retro_radio.server import _song_item

    item = _song_item(
        {
            "title": "神田川",
            "artist": "南こうせつとかぐや姫",
            "preview_url": "https://audio-ssl.itunes.apple.com/x.m4a",
            "store_url": APPLE_URL,
        },
        1,
    )
    assert item["metadata"]["store_url"] == APPLE_URL


def test_song_playlist_item_omits_store_url_for_intermission():
    from retro_radio.server import _song_item

    item = _song_item(
        {
            "title": "間奏",
            "artist": "",
            "preview_url": None,
            "store_url": APPLE_URL,
        },
        1,
    )
    assert "store_url" not in item["metadata"]


# ==============================================================================
# 4. フロント: 導線と帰属表記が HTML にあること
# ==============================================================================
def test_frontend_has_store_link_and_attribution():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="songStoreLink"' in html
    assert 'id="songStoreAnchor"' in html
    # 帰属表記（Apple ガイドライン）
    assert "Apple Music / iTunes" in html
    # 別タブで開く，且はリファラーを渡さない
    assert 'rel="noopener noreferrer"' in html


def test_frontend_validates_store_host_client_side():
    """フロントも Apple 以外のホストを導線として通さないこと。"""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    # ホスト検証が 1 箇所だけ存在し、`https:` 以外のプロトコルも弾く
    assert js.count("'music.apple.com'") == 1
    assert js.count("'itunes.apple.com'") == 1
    assert "absolute.protocol !== 'https:'" in js
