"""曲カタログの正本。iTunes からのプレビュー音源解決。

重要な変更点（実測に基づく）
--------------------------
旧実装の ``search_itunes_by_track`` は候補の**先頭 1 件を無検証で採用**
していた。iTunes の検索はあいまいなので、実際には別の曲が返っていた:

    卒業写真（荒井由実）  ->  ルージュの伝言（荒井由実）      を再生
    六本木心中（ゆり）    ->  雪の華（Ms.OOJA）               を再生
    神田川（南こうせつとかぐや姫）-> 神田川(2014年新録音)（南こうせつ）を再生

介護施設で「卒業写真（荒井由実）」と読み上げながら別の曲が流れるのは、
記憶が訂正される効果そのものなので許されない。ここでは
**曲名とアーティストの両方が正規化後に一致する候補だけ**を採用する。
一致する候補が無いなら ``None`` を返し、**何も鳴らさない**
（フロントは間奏として扱う）。誤って別の曲を流すより無音の方が安全。

また、``releaseDate`` による年フィルタ（``itunes_year_tolerance``）は
削除する。iTunes の ``releaseDate`` は**配信日**であり原典の発表年では
ないため、常に誤った年になる。年の一致は正本の ``release_year`` が
担保しており、iTunes の年情報は照合に使わない。
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence

import requests

from ..config import get_settings
from ..core.songs import normalize_song_text, song_key

if TYPE_CHECKING:  # pragma: no cover - 型のみ
    # 実行時に import すると循環参照になる:
    #   core.preview_resolver → services.song_store（実際には services/__init__）
    #   → services.history_service → db.privacy_repository → core.music_profile
    #   → core/__init__ → core.pipeline → core.music_search → core.preview_resolver
    # `PreviewCache` は引数の型注釈にしか使わないので、実行時には不要。
    from ..services.song_store import PreviewCache

logger = logging.getLogger(__name__)
settings = get_settings()

# 正規化しても一致として受理するアーティスト名の上流・下流一致を許す語。
# 「南こうせつとかぐや姫」/「南こうせつ」のように、iTunes 側が
# ユニット名を省略することがあるため。
_ARTIST_SUFFIXES = ("とかぐや姫", " feat. ", " featuring ")


def release_year(item: dict) -> Optional[int]:
    """iTunes の応答から発売年を取り出す（無ければ None）

    後方互換のためのヘルパ。**年フィルタには使わない**
    （この値は配信日であって発表年ではない。上の docstring 参照）。
    """
    raw = item.get("releaseDate")
    if not raw:
        return None
    match = re.match(r"(\d{4})", str(raw))
    return int(match.group(1)) if match else None


# 正本と iTunes 間で字面が異なるが同じものを指す語。
# 「Official HIGE DANdism」は正本がローマ字、iTunes が「Official髭男dism」。
# どちらが正しい表記か決められないため、ローマ字表記を kanji 表記へ寄せる。
# **推測で増やさないこと**（``--verify`` で不一致が出たものだけ足す）。
_ARTIST_ALIASES = (
    ("higedandism", "髭男dism"),
)

# 共同演歌の区切り。全部取り除いてから比較する（順序は保持する）。
_ARTIST_SEPARATORS = re.compile(r"[&＆・×/／、,+＋]|\band\b|\bwith\b|\bvs\.?\b")

# "artist feat. other" の手前だけ見るための区切り。
_FEATURE_CREDIT = re.compile(r"feat\.|featuring|ft\.")


def _normalize_artist(value: str) -> str:
    """アーティスト名の比較用正規化（``normalize_song_text`` より緩い）。"""
    text = normalize_song_text(value)
    text = _FEATURE_CREDIT.split(text)[0]
    text = _ARTIST_SEPARATORS.sub("", text)
    for alias, canonical in _ARTIST_ALIASES:
        text = text.replace(alias, canonical)
    return text


def _search_limit() -> int:
    """1 曲につき何件まで候補を受け取るか"""
    return max(5, min(settings.itunes_limit, 25))


def _total_budget_seconds() -> float:
    """逐次 HTTP 全体の時間予算（秒）"""
    per_call = settings.itunes_timeout_connect + settings.itunes_timeout_read
    return max(10.0, per_call * max(1, settings.max_retries))


def _artist_matches(expected: str, actual: str) -> bool:
    """アーティスト名の緩い一致（完全一致が第一）。

    実測で確認した表記ゆれを吸収する:

    * 共同演歌の区切り（正本は「A・B」、iTunes は「A & B」）
    * 「Official HIGE DANdism」（正本）/「Official髭男dism」（iTunes）の
      ような漢字とカタカナの差し替え
    * 「南こうせつとかぐや姫」/「南こうせつ」のようにユニット名を
      iTunes 側が省略する
    """
    left = _normalize_artist(expected)
    right = _normalize_artist(actual)
    if not left or not right:
        return False
    if left == right:
        return True
    # 「南こうせつとかぐや姫」に対して「南こうせつ」を許す（片方が上位語）。
    if left.startswith(right) or right.startswith(left):
        return True
    # "artist feat. other" / "artist" の形。
    base_left = re.split(r"feat\.|featuring", left)[0]
    base_right = re.split(r"feat\.|featuring", right)[0]
    return bool(base_left) and base_left == base_right


def _pick_matching(results: Iterable[dict], title: str, artist: str) -> Optional[dict]:
    """候補から「曲名もアーティストも一致するもの」だけを 1 件返す。

    優先順位:

    1. 曲名・アーティストとも完全一致
    2. 曲名が完全一致し、アーティストが緩く一致
       （iTunes 側の表記ゆれを吸収する）
    3. それ以外は **採用しない**

    あいまいな一致（部分一致だけの曲）は**返さない**。
    「毕业写真」で「卒業 (YUKI)」を鳴らすより間奏にする。
    """
    expected_title = normalize_song_text(title)
    expected_artist = normalize_song_text(artist)
    loose_artist_only: Optional[dict] = None

    for item in results:
        if not isinstance(item, dict):
            continue
        if not item.get("previewUrl"):
            continue
        # 正規化は両側に同じ規則を使う（iTunes 側の括弧書きも除去する）。
        # 「神田川(2014年新録)」は正本の「神田川」と一致し、**採用する**。
        # 同じ曲を同じ奏者が歌っている限り 虚偽の主張は含まれないため。
        actual_title = normalize_song_text(str(item.get("trackName", "")))
        actual_artist = normalize_song_text(str(item.get("artistName", "")))
        if actual_title != expected_title:
            continue
        if actual_artist == expected_artist:
            return item
        if loose_artist_only is None and _artist_matches(artist, str(item.get("artistName", ""))):
            loose_artist_only = item

    return loose_artist_only


def _search_itunes(term: str) -> List[dict]:
    """iTunes Search API を 1 回叩く。失敗は空リスト（呼び出し側で扱う）。"""
    try:
        response = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": term,
                "country": "JP",
                "media": "music",
                "entity": "song",
                "limit": _search_limit(),
            },
            timeout=(settings.itunes_timeout_connect, settings.itunes_timeout_read),
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        return [item for item in results if isinstance(item, dict)]
    except Exception as exc:  # noqa: BLE001 - ネットワークの失敗は曲不足に畳む
        logger.warning("iTunes 検索に失敗しました (%s): %s", term, exc)
        return []


def _lookup(title: str, artist: str) -> Optional[dict]:
    """``曲名 + アーティスト`` で iTunes を引き、一致するものだけを返す。

    検索語は **2 通り試す**。iTunes の結果は語句の出現回数で並ぶため、
    曲名が一般名词のときはアーティスト名の入った検索語の方が上位に来る。
    実測で「LOVE LOVE LOVE」（DEEN）は ``曲名 + アーティスト`` では
    DREAMS COME TRUE の同名曲が上位を占めたが、``アーティスト + 曲名``
    では DEEN の版が返った。
    """
    for term in (f"{title} {artist}", f"{artist} {title}"):
        matched = _pick_matching(_search_itunes(term), title, artist)
        if matched:
            return matched
    return None


def resolve_preview(
    title: str,
    artist: str,
    cache: "Optional[PreviewCache]" = None,
) -> Optional[Dict[str, Optional[str]]]:
    """正本の 1 曲についてプレビュー URL を解決する。

    Parameters
    ----------
    title, artist:
        正本の表記（``retro_radio/core/songs/songs.json`` より）。
    cache:
        解決結果のキャッシュ。``None`` を渡すと毎回ネットワークを叩く。

    Returns
    -------
    dict | None
        ``{"preview_url": str, "artwork_url": str | None}``。
        **一致する音源が無いときは ``None``**（この場合はこの曲を
        鳴らさず、間奏として扱う）。キャッシュには
        「この曲には音源が無い」ことが記録されている場合も ``None``。
    """
    key = song_key(title, artist)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            if not cached.get("preview_url"):
                return None
            return cached

    item = _lookup(title, artist)
    resolved = (
        {"preview_url": item.get("previewUrl"), "artwork_url": item.get("artworkUrl100")}
        if item
        else {"preview_url": None, "artwork_url": None}
    )
    if cache is not None:
        cache.put(key, resolved["preview_url"], resolved["artwork_url"])
    if not resolved["preview_url"]:
        logger.info(
            "音源が見つからないため間奏として扱います: 「%s」（%s）", title, artist
        )
    return resolved if resolved["preview_url"] else None


def enrich_songs(
    records: Sequence[Dict[str, Any]],
    cache: "Optional[PreviewCache]" = None,
    deadline: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """正本レコードにプレビュー URL を付けて iTunes 形式の dict にする。

    Parameters
    ----------
    records:
        ``core.songs`` のレコード。**変更しない**（新しい dict を返す）。
    cache:
        解決結果のキャッシュ。
    deadline:
        ``time.monotonic()`` の絶対時刻。超えたら残りは音源なしで返す
        （時間予算の消費を止めるため）。

    Notes
    -----
    音源が無い曲も **脱落させない**。``previewUrl: None`` のまま返し、
    フロントが間奏として扱う。落とすと「1 番組 6 曲」のスロットが
    埋まらず、トークが連続してしまう。
    """
    out: List[Dict[str, Any]] = []
    for record in records:
        title = str(record.get("title", "不明"))
        artist = str(record.get("artist", "不明"))

        if deadline is not None and time.monotonic() >= deadline:
            resolved = None
        else:
            resolved = resolve_preview(title, artist, cache=cache)

        out.append({
            "trackId": record.get("id"),
            "trackName": title,
            "artistName": artist,
            "releaseYear": record.get("release_year"),
            "previewUrl": (resolved or {}).get("preview_url"),
            "artworkUrl100": (resolved or {}).get("artwork_url"),
        })
    return out


def search_itunes_songs(
    year: int,
    count: Optional[int] = None,
    cache: "Optional[PreviewCache]" = None,
) -> List[dict]:
    """1 パス（または 1 番組）で必要な曲数を、ローテーション順に集める。

    候補は**正本カタログ**から選び、ローテーション順（未再生 → 最古再生）に
    並べる。旧実装は iTunes の年キーワード検索で曲数を確保しようと
    していたが、実測で 1965 年・1975 年は **0 件**しか返らず
    （2005 年でも 2004/2006 年の曲しか無い）、曲数制約に
    寄与していなかった。候補はカタログが担保するので、iTunes は
    「音源 URL を解決する」役割だけを持つ。
    """
    from ..core.song_selector import SongSelector

    wanted = count or max(1, settings.medley_song_count)
    logger.info("選曲開始: year=%s count=%s", year, wanted)

    records = SongSelector().select(year, wanted)
    deadline = time.monotonic() + _total_budget_seconds()
    enriched = enrich_songs(records, cache=cache, deadline=deadline)

    playable = sum(1 for song in enriched if song.get("previewUrl"))
    logger.info(
        "選曲完了: year=%s 要求=%d 採用=%d 音源あり=%d",
        year,
        wanted,
        len(enriched),
        playable,
    )
    return enriched


def select_song(year: int, songs: list[dict]) -> tuple:
    """楽曲選択（曲名, アーティスト, プレビューURL, フォールバックフラグ, ジャケットURL）

    後方互換用の単発版。新規コードは :func:`search_itunes_songs` を使う。
    """
    if not songs:
        from ..core.fallback import get_fallback_song

        title, artist = get_fallback_song(year)
        return title, artist, None, True, None
    song = songs[0]
    return (
        song.get("trackName", "不明"),
        song.get("artistName", "不明"),
        song.get("previewUrl"),
        song.get("previewUrl") is None,
        song.get("artworkUrl100"),
    )


__all__ = [
    "enrich_songs",
    "release_year",
    "resolve_preview",
    "search_itunes_songs",
    "select_song",
]
