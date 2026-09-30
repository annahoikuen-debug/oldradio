import logging
import random
import time
from typing import Optional
import requests
from ..config import get_settings
from ..core.fallback import (
    FALLBACK_SONGS,
    FALLBACK_SONGS_PER_BUCKET,
    get_fallback_song,
    get_fallback_songs,
    get_song_bucket,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _search_limit() -> int:
    """1曲につき何件まで候補を受け取るか

    従来は `settings.itunes_limit`（既定50）を要求し、使うのは先頭1件だけだった。
    数百KB のレスポンスを捨てるので、必要な曲数に合わせて絞る。
    """
    return max(3, min(settings.itunes_limit, max(1, settings.medley_song_count) * 3))


def _total_budget_seconds() -> float:
    """逐次 HTTP 全体の時間予算（秒）"""
    per_call = settings.itunes_timeout_connect + settings.itunes_timeout_read
    return max(10.0, per_call * max(1, settings.max_retries))


def search_itunes_by_track(title: str, artist: str) -> Optional[dict]:
    """曲名とアーティスト名でiTunes検索"""
    try:
        query = f"{title} {artist}"
        response = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": query,
                "country": "JP",
                "media": "music",
                "entity": "song",
                "limit": _search_limit()
            },
            timeout=(settings.itunes_timeout_connect, settings.itunes_timeout_read)
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        for item in results:
            if item.get("previewUrl"):
                return item
        return None
    except Exception as e:
        logger.error(f"iTunes個別検索失敗 ({title}): {e}")
        return None

def search_itunes_songs(year: int) -> list[dict]:
    """年代の代表曲をもとにiTunesプレビュー音源を精密検索

    1曲も見つからなくても `settings.max_retries` まで再試行するが、
    逐次 HTTP 全体の時間は `_total_budget_seconds()` で頭打ちにする。
    """
    logger.info(f"iTunes楽曲検索開始: year={year}")
    bucket = get_song_bucket(year)
    songs_pool = FALLBACK_SONGS.get(bucket) or FALLBACK_SONGS.get(1960, [])

    found_songs: list[dict] = []
    seen: set = set()
    wanted = max(1, settings.medley_song_count)
    attempts = max(1, settings.max_retries)
    deadline = time.monotonic() + _total_budget_seconds()

    # 候補曲からiTunes検索を試行
    shuffled = list(songs_pool)
    random.shuffle(shuffled)

    for title, artist in shuffled:
        for _ in range(attempts):
            if time.monotonic() >= deadline:
                logger.warning("iTunes検索の時間予算を使い切りました")
                break
            res = search_itunes_by_track(title, artist)
            if res:
                key = (res.get("trackName"), res.get("artistName"))
                if key not in seen:
                    seen.add(key)
                    found_songs.append(res)
                break
        if len(found_songs) >= wanted or time.monotonic() >= deadline:
            break

    if found_songs:
        logger.info(f"iTunes精密検索成功: {len(found_songs)}曲発見")
        return found_songs

    # プレビュー音源が見つからない場合は空を返し、呼び出し側のフォールバックに委ねる
    logger.warning("iTunesプレビュー音源が見つからず、フォールバックに委ねます")
    return []

def select_song(year: int, songs: list[dict]) -> tuple[str, str, Optional[str], bool, Optional[str]]:
    """楽曲選択（曲名, アーティスト, プレビューURL, フォールバックフラグ, ジャケット画像URL）"""
    if not songs:
        title, artist = get_fallback_song(year)
        return title, artist, None, True, None
    song = random.choice(songs)
    return (
        song.get("trackName", "不明"),
        song.get("artistName", "不明"),
        song.get("previewUrl"),
        song.get("previewUrl") is None,
        song.get("artworkUrl100")
    )

def select_songs(year: int, songs: list[dict], count: int = None) -> list[dict]:
    """複数曲選択（プレビュー付き優先、不足分は静的フォールバック曲で補完）

    1番組中に同一曲が2回流れないよう、プレビューあり／なしの両分岐と
    静的フォールバックの補完で重複を除去する。
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

    # プレビュー付きを優先
    _take([s for s in songs if s.get("previewUrl")])
    # 不足分は入力中のプレビューなしで補完
    _take([s for s in songs if not s.get("previewUrl")])

    # それでも不足する場合は静的フォールバック曲で補完（外部APIは呼ばない）
    missing = count - len(selected)
    if missing > 0:
        # 既に採用した曲と重複する分は捨てられるので、余裕を持って要求する
        ask = min(
            len(FALLBACK_SONGS) * FALLBACK_SONGS_PER_BUCKET,
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
                "artworkUrl100": None
            })

    return selected
