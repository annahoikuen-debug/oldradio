"""楽曲検索の互換層。

実処理は 2 つのモジュールへ分けた。

* ``core/preview_resolver.py`` … 正本カタログ + iTunes 的一致検証付き
  プレビュー解決。
* ``core/song_selector.py`` … 再生履歴に基づくローテーション選曲。

このモジュールは**既存の公開 API を保つ薄い委譲層**である
（``retro_radio.core.__init__`` / ``server.py`` / 既存テストが
``search_itunes_songs`` / ``select_songs`` / ``get_fallback_song`` を
import しているため）。

変更の要点（実測に基づく）
--------------------------
旧実装は次の 2 つで曲数を確保しようとしていたが、どちらも機能していなかった。

1. **年キーワード検索**（``search_itunes_by_year``）。
   iTunes に「1965年 ヒット曲」を問い合わせると **0 件**、「1975年 ヒット曲」
   でも **0 件**が返る（2005 年でも 2004/2006 年の曲 3 件のみ）。
   この経路は曲数制約に何も寄与していないため、本番経路から外した
   （関数は互換性のため残す）。
2. **先頭 1 件の無検証採用**。実測で
   「卒業写真（荒井由実）」→ ルージュの伝言、
   「六本木心中（ゆり）」→ 雪の華（Ms.OOJA）、
   「神田川（南こうせつとかぐや姫）」→ 神田川(2014年新録音) と、
    6 件中 3 件が別の曲を返していた。修正は
   ``core/preview_resolver._pick_matching`` にある。

曲プールは**年あたり約 50 曲**の正本カタログ
（``core/songs/songs.json``）が担う。旧実装の ``FALLBACK_SONGS`` は
9 バケット × 4 曲 = **全 36 曲**しかなく、76 年に対して曲不足が構造的だった。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

# ``tests/conftest.py`` の ``mock_itunes`` フィクスチャは
# ``music_search.requests.get`` を置き換える。実処理は preview_resolver 側だが、
# **差し替え点が公開されていることは互換契約**なので、ここでは import したままにする。
import requests  # noqa: F401

from ..config import get_settings
from ..core.fallback import (  # noqa: F401 - 互換のため再輸出
    FALLBACK_SONGS,
    FALLBACK_SONGS_PER_BUCKET,
    FALLBACK_SONG_TOTAL,
    FALLBACK_SONG_YEARS,
    get_fallback_song,
    get_fallback_songs,
    get_song_bucket,
)
from ..core.preview_resolver import (
    enrich_songs,
    release_year,
    resolve_preview,
    search_itunes_songs as _search_itunes_songs,
    select_song,  # noqa: F401 - 既存公開 API の再輸出
)

logger = logging.getLogger(__name__)
settings = get_settings()


def search_itunes_by_year(year: int, tried: set, deadline: float) -> list:
    """年キーワード検索（**本番経路では使わない**。互換性のため残す）。

    実測で対象年のヒット曲が 1 曲も返らないため、曲数確保の
    根拠にならない。iTunes の ``releaseDate`` は配信日であり発表年では
    ないため、年フィルタも機能しない。
    """
    from ..core.preview_resolver import _search_itunes  # noqa: PLC0415

    queries = [
        f"{year}年 ヒット曲",
        f"{year}年 歌謡曲",
        f"{year}年 人気曲",
    ]
    found: list = []
    for query in queries:
        if len(found) >= 3 or time.monotonic() >= deadline:
            break
        for item in _search_itunes(query):
            if not item.get("previewUrl"):
                continue
            year_of = release_year(item)
            if year_of is None or abs(year_of - year) > 1:
                continue
            key = (item.get("trackName"), item.get("artistName"))
            if key in tried:
                continue
            tried.add(key)
            found.append(item)
            if len(found) >= 3:
                break
    return found


def search_itunes_songs(year: int, count: Optional[int] = None) -> list[dict]:
    """正本カタログから ``count`` 曲をローテーション順に選び、音源 URL を付ける。

    Parameters
    ----------
    year:
        対象年。
    count:
        1 パス（= 1 周）で必要な曲数。トーク N 個なら N+1 曲。
        周回数を掛けて 1 番組ぶんの曲数を要求することもできる
        （``server._build_generate_response`` がそうする）。

    Returns
    -------
    list[dict]
        iTunes 形式の dict。**音源が無い曲も脱落させず**
        ``previewUrl: None`` で含まれる（フロントが間奏として扱う）。
    """
    return _search_itunes_songs(year, count=count)


def select_songs(year: int, songs: list[dict], count: int = None) -> list[dict]:
    """1 番組で流す曲を選ぶ（プレビュー付き優先・1 番組内で重複なし）。

    Parameters
    ----------
    year:
        対象年（曲数が足りないときの補完に使う）。
    songs:
        候補（iTunes 形式）。
    count:
        必要な曲数。既定は ``settings.medley_song_count``。

    Notes
    -----
    音源が無いスロットは**静かに落とすのではなく、正本の曲で埋める**。
    落とすと「曲 → トーク → トーク」とトークが連続し、番組の骨組みが崩れる。
    """
    if count is None:
        count = settings.medley_song_count

    selected: list[dict] = []
    known: set = set()

    def _key(song: dict):
        return (song.get("trackName"), song.get("artistName"))

    def _take(pool: list[dict]) -> None:
        for song in pool:
            if len(selected) >= count:
                return
            key = _key(song)
            if key in known:
                continue
            known.add(key)
            selected.append(song)

    _take([s for s in songs if s.get("previewUrl")])
    _take([s for s in songs if not s.get("previewUrl")])

    missing = count - len(selected)
    if missing > 0:
        ask = min(
            FALLBACK_SONG_TOTAL,
            missing + len(known) + FALLBACK_SONGS_PER_BUCKET,
        )
        for title, artist in get_fallback_songs(year, count=ask):
            if len(selected) >= count:
                break
            key = (title, artist)
            if key in known:
                continue
            known.add(key)
            selected.append({
                "trackName": title,
                "artistName": artist,
                "previewUrl": None,
                "artworkUrl100": None,
            })

    return selected


__all__ = [
    "FALLBACK_SONGS",
    "FALLBACK_SONGS_PER_BUCKET",
    "FALLBACK_SONG_TOTAL",
    "FALLBACK_SONG_YEARS",
    "enrich_songs",
    "get_fallback_song",
    "get_fallback_songs",
    "get_song_bucket",
    "release_year",
    "resolve_preview",
    "search_itunes_by_year",
    "search_itunes_songs",
    "select_song",
    "select_songs",
]
