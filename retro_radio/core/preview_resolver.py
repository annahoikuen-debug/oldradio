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
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    「卒業写真」で「卒業 (YUKI)」を鳴らすより間奏にする。
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


class PreviewTransportError(RuntimeError):
    """**ネットワーク到達不能**で音源を解決できなかったことを表す。

    「この曲に音源が無い」（真のミス）とは**根本的に別物**。共有キャッシュに
    「音源なし」を書くと、iTunes の 1 回の 5xx や TLS タイムアウトが
    全テナントに TTL のあいだ固定され、宿主が曲名を読み上げても
    何も鳴らない状態になる。伝播させたくないため、
    この例外は「否定キャッシュを書かない」根拠になる。

    **レート制限（429 / 403）もこの例外に含める。**
    iTunes は同一 IP からの連打に対する拒否を 403 Forbidden として返す
    （実測: 並列 5 workers × 2 検索語 = 約 100 リクエストの連打で
    ブロックが始まり、45 秒後も 403 が継続した）。これを「音源なし」に
    倒すと、**実在する曲まで「音源なし」と判定**され、その結果が共有
    キャッシュに書き込まれる。実在する 51 曲のうち 44 曲が一斉に
    弃却された実測がある。したがって 429 / 403 は
    「問い合わせが届かなかった」側に倒す。
    """


#: サーバが「多すぎる」「権限が無い」の理由で拒んだことを示すステータス。
#: **曲が存在しない証拠にはならない**ため :class:`PreviewTransportError` に倒す。
_THROTTLE_STATUS_CODES = frozenset({403, 429})


# 短絡遮断（サーキットブレーカー）。
# 死んだ iTunes に対して 1 曲ごとに 2 検索語 × タイムアウトを待つと
# 時間予算が全て溶ける。連続失敗が閾値に達したら、クールダウン中は
# HTTP を一切行わずに「到達不能」だけ返す。
_BREAKER_FAILURES = 3
_BREAKER_COOLDOWN_SECONDS = 60.0
_breaker_lock = threading.Lock()
_breaker_failures = 0
_breaker_open_until = 0.0


def _breaker_allow() -> bool:
    """いま実際に問い合わせてよいか（遮断中なら ``False``）。"""
    with _breaker_lock:
        return time.monotonic() >= _breaker_open_until


def _breaker_success() -> None:
    global _breaker_failures, _breaker_open_until
    with _breaker_lock:
        _breaker_failures = 0
        _breaker_open_until = 0.0


def _breaker_failure(reason: str) -> None:
    global _breaker_failures, _breaker_open_until
    with _breaker_lock:
        _breaker_failures += 1
        if _breaker_failures >= _BREAKER_FAILURES:
            _breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECONDS
            logger.warning(
                "iTunes への接続に連続して失敗しました（%.0f 秒間は問い合わせを停止します）: %s",
                _BREAKER_COOLDOWN_SECONDS,
                reason,
            )


def _reset_breaker() -> None:
    """テスト用に遮断器を戻す。"""
    global _breaker_failures, _breaker_open_until
    with _breaker_lock:
        _breaker_failures = 0
        _breaker_open_until = 0.0


def _search_itunes(term: str) -> List[dict]:
    """iTunes Search API を 1 回叩く。失敗は空リスト（呼び出し側で扱う）。

    公開 API の互換のため、伝播エラーは空リストに畳む。**結果の区別**が
    音源の有無を確認したいだけの場合は :func:`_fetch_itunes` を使う。
    """
    try:
        return _fetch_itunes(term)
    except PreviewTransportError as exc:
        logger.warning("iTunes 検索に失敗しました (%s): %s", term, exc)
        return []


def _fetch_itunes(term: str) -> List[dict]:
    """iTunes を実際に問い合わせる。**到達不能なら例外**を投げる。

    Returns
    -------
    list
        応答が正常に取れて解析できた候補（0 件でも「真のミス」）。

    Raises
    ------
    PreviewTransportError
        タイムアウト・接続失敗・5xx / 429。**音源が無いことではない**ため、
        呼び出し側は否定キャッシュを書いてはいけない。
    """
    if not _breaker_allow():
        raise PreviewTransportError("iTunes が到達不能のため問い合わせを省略しました")

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
    except requests.RequestException as exc:
        _breaker_failure(str(exc))
        raise PreviewTransportError(f"iTunes への接続に失敗しました: {exc}") from exc
    except ValueError as exc:
        # JSON の解析失敗は.iTunes 側の異常なので、無音扱いにしない。
        _breaker_failure(f"応答を解釈できません: {exc}")
        raise PreviewTransportError(f"iTunes の応答を解釈できません: {exc}") from exc

    status = getattr(response, "status_code", 200)
    if isinstance(status, int) and status >= 500:
        _breaker_failure(f"HTTP {status}")
        raise PreviewTransportError(f"iTunes がサーバエラーを返しました: HTTP {status}")
    # レート制限・アクセス拒否は「曲が無い」証拠にならない。
    # 空リスト（=真のミス）へ倒すと実在する曲まで「音源なし」と判定され、
    # 共有キャッシュに否定結果が残る。必ず例外にして伝播させない。
    if isinstance(status, int) and status in _THROTTLE_STATUS_CODES:
        _breaker_failure(f"HTTP {status}")
        raise PreviewTransportError(
            f"iTunes がレート制限で拒みました: HTTP {status}（{term}）"
        )
    try:
        response.raise_for_status()
        results = response.json().get("results", [])
    except requests.RequestException as exc:
        # 上の事前チェックで 403 / 429 は既に例外になっているため、ここに
        # 届くのは 400 / 404 など「この検索語に結果は無い」性質の応答。
        # それらは真のミスなので空リストでよい。
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in _THROTTLE_STATUS_CODES or code is None or code >= 500:
            _breaker_failure(str(exc))
            raise PreviewTransportError(f"iTunes がエラーを返しました: {exc}") from exc
        logger.debug("iTunes が HTTP %s を返しました（%s）", code, term)
        # 400 / 404 は「iTunes に到達して応答を解釈できた」ことの証明なので
        # **成功シグナル**として遮断器を戻す（R2-07 コアの修正）。
        # これが無いと「正常な 404 を挟んだ非連続失敗」で failures が積み上がり、
        # 全ホスト 60 秒無音になっていた。
        _breaker_success()
        return []
    except ValueError as exc:
        _breaker_failure(f"応答を解釈できません: {exc}")
        raise PreviewTransportError(f"iTunes の応答を解釈できません: {exc}") from exc

    _breaker_success()
    return [item for item in results if isinstance(item, dict)]


def _lookup(title: str, artist: str) -> Optional[dict]:
    """``曲名 + アーティスト`` で iTunes を引き、一致するものだけを返す。

    検索語は **2 通り試す**。iTunes の結果は語句の出現回数で並ぶため、
    曲名が一般名詞のときはアーティスト名の入った検索語の方が上位に来る。
    実測で「LOVE LOVE LOVE」（DEEN）は ``曲名 + アーティスト`` では
    DREAMS COME TRUE の同名曲が上位を占めたが、``アーティスト + 曲名``
    では DEEN の版が返った。

    Raises
    ------
    PreviewTransportError
        iTunes に到達できなかった場合（音源不在とは区別する）。
    """
    for term in (f"{title} {artist}", f"{artist} {title}"):
        matched = _pick_matching(_fetch_itunes(term), title, artist)
        if matched:
            return matched
    return None


def store_link(track_view_url: Optional[str]) -> Optional[str]:
    """Apple のストア導線 URL を組み立てる（Apple への送客リンク）。

    iTunes Search API の応答に含まれる ``trackViewUrl``（Apple Music の
    楽曲ページ）を素通しするだけでは、アフィリエイト計測に必要な
    ``at`` / ``ct`` パラメータが無く、成果として計上されない。

    **登録済みトークンが設定に無いときはパラメータを一切付けない。**
    未登録トークンを推測で書くことは禁止する（Apple 側の計測が壊れる）。

    Parameters
    ----------
    track_view_url:
        iTunes が返した ``trackViewUrl``。``None`` / 空文字なら ``None``。

    Returns
    -------
    str | None
        アフィリエイトパラメータを付けたストア URL。無効な入力なら ``None``。
    """
    raw = (track_view_url or "").strip()
    if not raw:
        return None
    # 想定外のホストが混入しても、導線リンクにしない。
    if not raw.startswith("https://"):
        return None
    if not (
        raw.startswith("https://music.apple.com/")
        or raw.startswith("https://itunes.apple.com/")
    ):
        logger.warning(
            "Apple ストア以外の URL が返されたため導線として扱いません: %s",
            raw[:120],
        )
        return None

    parsed = urlsplit(raw)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    # 既存の at / ct を二重に入れないよう、先に落とす。
    params = [(k, v) for k, v in params if k not in {"at", "ct"}]

    token = (settings.itunes_affiliate_token or "").strip()
    if token:
        params.append(("at", token))
    campaign = (settings.itunes_affiliate_campaign or "").strip()
    if campaign:
        params.append(("ct", campaign))

    query = urlencode(params)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


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
        ``{"preview_url": str, "artwork_url": str | None,
        "track_view_url": str | None}``。``track_view_url`` は
        Apple Music への送客導線（アフィリエイトパラメータ付き）。
        **一致する音源が無いときは ``None``**（この場合はこの曲を
        鳴らさず、間奏として扱う）。キャッシュには
        「この曲には音源が無い」ことが記録されている場合も ``None``。

    Notes
    -----
    iTunes へ**到達できなかった**場合も ``None`` を返すが、その場合は
    **否定キャッシュを書かない**。書き込むと共有キャッシュに
    「音源なし」が残り、iTunes が復旧しても TTL のあいだ全テナントが
    無音になる。回復後は普通に再解決される。
    """
    key = song_key(title, artist)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            if not cached.get("preview_url"):
                return None
            return cached

    try:
        item = _lookup(title, artist)
    except PreviewTransportError as exc:
        # 音源が無いのではない。記録せず、次の曲（と次の番組）へ進む。
        logger.warning(
            "iTunes に到達できないため音源を解決できませんでした（キャッシュしません）: "
            "「%s」（%s）: %s",
            title,
            artist,
            exc,
        )
        return None

    resolved = (
        {
            "preview_url": item.get("previewUrl"),
            "artwork_url": item.get("artworkUrl100"),
            "track_view_url": store_link(item.get("trackViewUrl")),
        }
        if item
        else {"preview_url": None, "artwork_url": None, "track_view_url": None}
    )
    if cache is not None:
        cache.put(
            key,
            resolved["preview_url"],
            resolved["artwork_url"],
            resolved["track_view_url"],
        )
    if not resolved["preview_url"]:
        # 肯定的な「音源なし」だけを DEBUG に出す。INFO だと障害と区別できない。
        logger.debug(
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

    **歯抜け（無音の溝）を作らない**ための規則を 2 つ:

    1. 予算切れで**無音にしない**。時間予算を使い切った後のレコードには、
       既に解決できた可聴な曲を割り当てる。予算は「可聴な曲を解決する」
       ために使い切るものであって、番組を無音にするためではない。
    2. 1 曲目だけは**必ず**解決を試みる。ここが空くと番組の頭が無音になる。
    """
    out: List[Dict[str, Any]] = []
    first_playable: Optional[Dict[str, Any]] = None
    starved = 0

    for index, record in enumerate(records):
        title = str(record.get("title", "不明"))
        artist = str(record.get("artist", "不明"))

        out_of_budget = deadline is not None and time.monotonic() >= deadline
        if out_of_budget and index > 0:
            # 予算切れ。既に解決できた曲で埋め、**無音にはしない**。
            starved += 1
            source = first_playable or {}
            out.append({
                "trackId": record.get("id"),
                "trackName": source.get("trackName") or title,
                "artistName": source.get("artistName") or artist,
                "releaseYear": record.get("release_year"),
                "previewUrl": source.get("previewUrl"),
                "artworkUrl100": source.get("artworkUrl"),
                "trackViewUrl": source.get("trackViewUrl"),
            })
            continue

        resolved = resolve_preview(title, artist, cache=cache)
        item = {
            "trackId": record.get("id"),
            "trackName": title,
            "artistName": artist,
            "releaseYear": record.get("release_year"),
            "previewUrl": (resolved or {}).get("preview_url"),
            "artworkUrl100": (resolved or {}).get("artwork_url"),
            "trackViewUrl": (resolved or {}).get("track_view_url"),
        }
        out.append(item)
        if item["previewUrl"] and first_playable is None:
            first_playable = item

    if starved:
        logger.warning(
            "音源解決の時間予算が尽きたため %d 曲を、解決済み曲で代用しました"
            "（無音にはしていません）",
            starved,
        )
    if records and not any(item["previewUrl"] for item in out):
        if len(records) == 1:
            # `_enrich_with_checkpoints` は 1 曲ずつ呼ぶ。この呼び出しの
            # 失敗は「その曲」の失敗であって番組全体の判定ではない
            # （番組の集計は `server._step_resolve_previews` が行う）。
            logger.warning(
                "音源を解決できませんでした（この曲は間奏になります）: 「%s」（%s）",
                str(records[0].get("title", "不明")),
                str(records[0].get("artist", "不明")),
            )
        else:
            logger.warning(
                "1 曲も音源を解決できませんでした（番組は間奏のみになります）"
            )

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
    "PreviewTransportError",
    "enrich_songs",
    "release_year",
    "resolve_preview",
    "search_itunes_songs",
    "select_song",
]
