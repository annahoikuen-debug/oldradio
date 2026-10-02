import logging
import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from datetime import datetime
from ..utils.validators import validate_year_range
from ..config import get_settings
from ..models.radio import ProgramSchedule, ProgramGuide
from .facts import (
    facts_health,
    future_year_mentions,
    programs_for_year,
    radio_programs_for_year,
)

#: 事実レジストリの状態をそのまま再輸出する（UI・ヘルスチェック用）。
#: 正本が壊れていて読み込めない場合、上の表は**空**になり、
#: ``facts_health()["degraded"]`` が True になる。
from .rng import module_rng

logger = logging.getLogger(__name__)
settings = get_settings()

# 年代別ヒット曲マスター
# 曲名・アーティストは正式表記で、リリース年は FALLBACK_SONG_YEARS に集約する。
# 「勝手にしやがれ」（沢田研二・1981年）のように_release年_と年代が食い違うと、
# 介護用途では事実誤認になるため、整合を tests/test_content_regression.py が検証する。
FALLBACK_SONGS: dict[int, List[Tuple[str, str]]] = {
    # 正本カタログ（core/songs/songs.json）に 1950 年の曲が入るようになったため、
    # マスターも**実際の 1950 年の曲**へ揃える。ここに 1951 年以降の曲を残すと、
    # 対象年の選択枠（``selection_window_titles``）から外れ、原稿品質のゲートを落とす。
    1950: [
        ("東京キッド", "藤原亮子・渡辺はま子・奈良光枝"),
        ("あざみの歌", "伊藤久男"),
        ("水色のワルツ", "二葉あき子"),
        ("悲しき口笛", "高峰秀子"),
    ],
    1960: [
        ("上を向いて歩こう", "坂本九"),
        ("いつでも夢を", "橋幸夫・吉永小百合"),
        ("こんにちは赤ちゃん", "梓みちよ"),
        ("六本木心中", "ゆり"),
    ],
    1970: [
        ("神田川", "南こうせつとかぐや姫"),
        ("木綿のハンカチーフ", "太田裕美"),
        ("いい日旅立ち", "山口百恵"),
        ("卒業写真", "荒井由実"),
    ],
    1980: [
        ("勝手にしやがれ", "沢田研二"),
        ("ルビーの指環", "寺尾聰"),
        ("赤いスイートピー", "松田聖子"),
        ("ワインレッドの心", "安全地帯"),
    ],
    1990: [
        ("LA・LA・LA LOVE SONG", "久保田利伸"),
        ("LOVE LOVE LOVE", "DEEN"),
        ("真夏の果実", "サザンオールスターズ"),
        ("LOVE PHANTOM", "B'z"),
    ],
    2000: [
        ("TSUNAMI", "サザンオールスターズ"),
        ("世界に一つだけの花", "モーニング娘。"),
        ("ハナミズキ", "一青窈"),
        ("さくらんぼ", "大塚愛"),
    ],
    2010: [
        ("ヘビーローテーション", "AKB48"),
        ("恋", "星野源"),
        ("Lemon", "米津玄師"),
        ("Pretender", "Official HIGE DANdism"),
    ],
    2020: [
        ("ドライフラワー", "優里"),
        ("アイドル", "YOASOBI"),
        ("KICK BACK", "米津玄師"),
        ("夜に駆ける", "YOASOBI"),
    ],
    2025: [
        ("Zion", "YOASOBI"),
        ("napori", "Vaundy"),
        ("monody", "milet"),
        ("唱", "Ado"),
    ],
}

# 曲ごと（曲名, アーティスト）のリリース年。
# FALLBACK_SONGS は「(曲名, アーティスト)」2要素タプルの公開契約なので構造を変えず、
# リリース年メタデータは本辞書で別に管理する。
FALLBACK_SONG_YEARS: Dict[Tuple[str, str], int] = {
    ("東京キッド", "藤原亮子・渡辺はま子・奈良光枝"): 1950,
    ("あざみの歌", "伊藤久男"): 1950,
    ("水色のワルツ", "二葉あき子"): 1950,
    ("悲しき口笛", "高峰秀子"): 1950,
    ("上を向いて歩こう", "坂本九"): 1960,
    ("いつでも夢を", "橋幸夫・吉永小百合"): 1961,
    ("こんにちは赤ちゃん", "梓みちよ"): 1963,
    ("六本木心中", "ゆり"): 1969,
    ("神田川", "南こうせつとかぐや姫"): 1971,
    ("木綿のハンカチーフ", "太田裕美"): 1975,
    ("いい日旅立ち", "山口百恵"): 1975,
    ("卒業写真", "荒井由実"): 1976,
    ("勝手にしやがれ", "沢田研二"): 1981,
    ("ルビーの指環", "寺尾聰"): 1983,
    ("赤いスイートピー", "松田聖子"): 1985,
    ("ワインレッドの心", "安全地帯"): 1985,
    ("LA・LA・LA LOVE SONG", "久保田利伸"): 1992,
    ("LOVE LOVE LOVE", "DEEN"): 1994,
    ("真夏の果実", "サザンオールスターズ"): 1995,
    ("LOVE PHANTOM", "B'z"): 1996,
    ("TSUNAMI", "サザンオールスターズ"): 2000,
    ("世界に一つだけの花", "モーニング娘。"): 2001,
    ("ハナミズキ", "一青窈"): 2002,
    ("さくらんぼ", "大塚愛"): 2003,
    ("ヘビーローテーション", "AKB48"): 2010,
    ("恋", "星野源"): 2013,
    ("Lemon", "米津玄師"): 2018,
    ("Pretender", "Official HIGE DANdism"): 2019,
    ("ドライフラワー", "優里"): 2020,
    ("アイドル", "YOASOBI"): 2020,
    ("KICK BACK", "米津玄師"): 2020,
    ("夜に駆ける", "YOASOBI"): 2021,
    ("Zion", "YOASOBI"): 2024,
    ("napori", "Vaundy"): 2024,
    ("monody", "milet"): 2024,
    ("唱", "Ado"): 2024,
}

# 各バケットの曲数を揃える（フォールバック時に曲不足が起きないように）
FALLBACK_SONGS_PER_BUCKET = 4

# 静的マスター全体の曲数。
# ``FALLBACK_SONGS_PER_BUCKET`` は「1 バケットあたりの曲数」であり総曲数ではない。
# 提案② タスク2 で指摘されていた誤用（``core/music_search.py`` が
# ``len(FALLBACK_SONGS) * FALLBACK_SONGS_PER_BUCKET`` を総曲数の上限として
# 流用していた箇所）を訂正するため、総数はこの定数を参照する。
FALLBACK_SONG_TOTAL = len(FALLBACK_SONG_YEARS)

#: ``release_year <= target_year`` を満たす曲で足せなかったときのログラベル
RELEASE_YEAR_RELAXED = "release_year_relaxed"


def _song_bucket(year: int) -> int:
    """`year` に対応する静的マスターのバケットキーを返す。

    丸め（``year // 10 * 10``）だけでは 2025 が 2020 バケットへ落ち、
    「5年も前の曲」が返る。専用データを持つ年は完全一致を優先する
    （``NEWS_TOPICS_BY_DECADE`` と同じ方針）。
    """
    if year in FALLBACK_SONGS:
        return year
    decade = (year // 10) * 10
    if decade in FALLBACK_SONGS:
        return decade
    return min(FALLBACK_SONGS, key=lambda d: abs(d - decade))


# 同じ解決ルールを sibling モジュール（music_search）から使うための公開名
get_song_bucket = _song_bucket


def _bucket_walk(year: int) -> List[int]:
    """バケットを「対象年に近い順」に並べる（重複は畳む）。

    旧実装の ``[bucket, decade, decade - 10, decade + 10]`` は ``decade == bucket``
    の年に同じキーを 2 回 walk していた。ここでは順序を保ったまま畳む。
    """
    bucket = _song_bucket(year)
    decade = (bucket // 10) * 10
    order: List[int] = []
    for candidate in (bucket, decade, decade - 10, decade + 10):
        if candidate not in order:
            order.append(candidate)
    return order


def _release_year(song: Tuple[str, str]) -> Optional[int]:
    """静的マスターが把握しているリリース年（メタデータが無ければ ``None``）。"""
    return FALLBACK_SONG_YEARS.get(song)


def _bucket_pool(year: int, exclude: Optional[set] = None) -> List[Tuple[str, str]]:
    """バケット walk 順の候補プール（リリース年フィルタと重複排除の適用前）。"""
    skip = exclude or set()
    pool: List[Tuple[str, str]] = []
    seen: set = set()
    for bucket_key in _bucket_walk(year):
        for song in FALLBACK_SONGS.get(bucket_key, []):
            if song in seen or song in skip:
                continue
            seen.add(song)
            pool.append(song)
    return pool


#: :func:`_catalog_in_era_songs` が「より前の年」を遡る年数の上限。
#:
#: 正本カタログに**対象年の曲が無い年**がある（1953・1954 は 0 件、
#: 1955 は 1 件。``retro_radio.core.songs.thin_years`` が 29 年を報告している）。
#: その年に後年の曲を紹介するのが誤りなら、**前年以前の曲**を使うべきで、
#: 実際のラジオでもそのほうが普通である。この年数だけ遡れば 1953 年でも
#: 1950〜1952 年の 34 曲で足りる。それ以上は「arly 別の時代の番組」になる。
CATALOG_BACKFILL_YEARS = 10


def _catalog_in_era_songs(
    year: int,
    exclude: Optional[set] = None,
    wanted: Optional[int] = None,
) -> List[Tuple[str, str]]:
    """**正本カタログ**から、対象年またはそれ以前にリリースされた曲を返す。

    静的マスター（``FALLBACK_SONGS``）は 1 バケット 4 曲しかないため、
    対象年の番組に「その年以前にリリースされた曲」が 4 曲で足りず、
    :func:`partition_by_release_year` が**対象年より後の曲**（1960 年の
    「上を向いて歩こう」など）で埋めていた。1953 年の番組で 1960 年の曲を
    紹介するのは事実誤認であり、``eval`` の fact gate が fail にする。

    したがって次の順で正本カタログから取る（**後年の曲は見ない**）。

    1. ``release_year == year`` の曲
    2. 対象年に曲が無いときだけ、**前年以前**の曲（年が近い順）

    2 が必要なのは、正本に**対象年の曲が無い年**があるため
    （1953・1954 は 0 件、1955 は 1 件）。その年に 1960 年の曲を出すより
    1950 年の曲を出すほうが誤りは小さい。

    Parameters
    ----------
    year:
        対象年。
    exclude:
        すでに採用した ``(曲名, アーティスト)`` の集合。
    wanted:
        必要な曲数。足りたらそこで止める（``None`` なら全件）。

    Returns
    -------
    list[tuple[str, str]]
        ``release_year <= year`` の曲。**正本が読めない場合は空**
        （呼び出し側は緩和へ進む）。
    """
    skip = exclude or set()
    try:
        from .songs import songs_for_year
    except Exception:  # pragma: no cover - 正本モジュールが読めない場合
        logger.warning(
            "正本カタログ（core.songs）を import できないため、"
            "対象年の曲で埋めません: year=%s",
            year,
        )
        return []

    def _collect(records) -> List[Tuple[str, str]]:
        collected: List[Tuple[str, str]] = []
        for record in records:
            title = str(record.get("title") or "").strip()
            artist = str(record.get("artist") or "").strip()
            if not title:
                continue
            pair = (title, artist)
            if pair in skip:
                continue
            if pair in collected:
                continue
            collected.append(pair)
            if wanted is not None and len(collected) >= wanted:
                break
        return collected

    try:
        out = _collect(songs_for_year(year, tolerance=0))
        if wanted is None or len(out) < wanted:
            for back in range(1, CATALOG_BACKFILL_YEARS + 1):
                out.extend(_collect(songs_for_year(year - back, tolerance=0)))
                if wanted is not None and len(out) >= wanted:
                    break
    except Exception:
        logger.warning(
            "正本カタログの読み込みに失敗したため、対象年の曲で埋めません: year=%s",
            year,
            exc_info=True,
        )
        return []
    return out


def partition_by_release_year(
    year: int, *, exclude: Optional[set] = None
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """候補プールを「その年以前にリリースされた曲」と「それ以降」に分ける。

    Parameters
    ----------
    year:
        対象年。
    exclude:
        すでに採用した ``(曲名, アーティスト)`` の集合。1 番組内で
        同一曲が 2 回出ないための受け口（提案② タスク3）。

    Returns
    -------
    tuple[list[tuple[str, str]], list[tuple[str, str]]]
        ``(ok, future)``。``ok`` は ``release_year <= year`` を満たす曲、
        ``future`` は満たさない曲（リリース年の近い順に並べる）。
    """
    ok: List[Tuple[str, str]] = []
    future: List[Tuple[str, str]] = []
    for song in _bucket_pool(year, exclude):
        released = _release_year(song)
        # メタデータが無いものは除外しない（静的マスター自体が正本なので）
        if released is None or released <= year:
            ok.append(song)
        else:
            future.append(song)
    future.sort(key=lambda s: (_release_year(s) or year, s))
    return ok, future


def _relaxed_warning(year: int, songs: List[Tuple[str, str]]) -> None:
    """やむを得ず対象年より後の曲を足したときの構造化ログ。"""
    if not songs:
        return
    logger.warning(
        "選曲に release_year フィルタを緩和しました: year=%s relaxed=%d "
        "songs=%s（静的マスターに該当年の曲が無いため。空にはしません）",
        year,
        len(songs),
        [
            {"title": t, "artist": a, "release_year": _release_year((t, a))}
            for t, a in songs
        ],
    )


def get_fallback_song(
    year: int, *, exclude: Optional[set] = None, rng: Optional[Any] = None
) -> Tuple[str, str]:
    """年度に最も近い代表曲を返す。

    ``rng`` を注入できる（R2-03/04/06 コアの seed 設計）。
    ``None``（既定）は設定に応じた rng（``core.rng.module_rng()``）を使う。

    ``FALLBACK_SONG_YEARS``（提案② タスク2）を**実行時に初めて参照する**。
    バケット内で ``release_year <= year`` を優先するが、**該当が 0 件でも
    バケットの曲から必ず 1 曲返す**（ここが空になると台本も番組も作れないため）。

    バケットの外へは出ない。既存の「提示される代表曲がその年に対応する
    バケット由来であること」という保証を維持するため。
    """
    if not validate_year_range(year):
        year = settings.default_year
    bucket = FALLBACK_SONGS[_song_bucket(year)]
    candidates = [s for s in bucket if not exclude or s not in exclude]
    if not candidates:
        candidates = list(bucket)
    ok = [s for s in candidates if (_release_year(s) or year) <= year]
    chooser = rng if rng is not None else module_rng()
    if ok:
        return chooser.choice(ok)
    _relaxed_warning(year, candidates)
    return chooser.choice(candidates)


def _select_in_year_first(
    year: int,
    count: int,
    exclude: Optional[set],
) -> List[Tuple[str, str]]:
    """対象年の曲だけで ``count`` 曲を満たすよう埋め、**足りなければだけ**後年の曲で足す。

    順序が**この問題のすべて**である。静的マスター（``FALLBACK_SONGS``）は 1 バケット
    4 曲しかないので、対象年その一のバケットを持つ年（1950〜1959 など）では
    「リリース年が対象年以下」の候補が足りず、順序を逆にすると 1960 年の
    「上を向いて歩こう」を 1950 年の番組に紹介してしまう（事実誤認）。

    そのため次の順で埋める。

    1. 静的マスターのうち ``release_year <= year`` の曲
    2. 正本カタログの**対象年の曲**（``release_year == year``）
    3. それでも足りなければ静的マスターの後年の曲（``_relaxed_warning`` で記録）

    .. note::
       この順序を 1 か所で持つのは、`get_fallback_songs` と
       `select_program_songs` が同じ順序を保つようにするため。片方だけ
       変わると、`eval` の fact gate と `test_select_songs_never_returns_a
       _future_year_song` が落ちる。

    Parameters
    ----------
    year:
        対象年。
    count:
        必要な曲数。
    exclude:
        すでに採用した ``(曲名, アーティスト)`` の集合。

    Returns
    -------
    list[tuple[str, str]]
        採用した曲。シャッフルしない（呼び出し側が決める）。
    """
    ok, future = partition_by_release_year(year, exclude=exclude)
    songs = list(ok)
    if len(songs) < count:
        songs.extend(
            _catalog_in_era_songs(
                year,
                exclude=(exclude or set()) | set(songs),
                wanted=count - len(songs),
            )
        )
    if len(songs) < count:
        _relaxed_warning(year, future[: count - len(songs)])
        songs.extend(future[: count - len(songs)])
    return songs


def get_fallback_songs(
    year: int, count: int = 3, *, exclude: Optional[set] = None, rng: Optional[Any] = None
) -> List[Tuple[str, str]]:
    """指定年度のフォールバック曲を重複なしで複数返す。

    ``rng`` を注入できる（R2-03/04/06 コアの seed 設計）。
    ``None``（既定）は設定に応じた rng（``core.rng.module_rng()``）を使う。

    採用順は :func:`_select_in_year_first` に集約してある（対象年の曲 → 正本の
    対象年の曲 → やむを得ない後年の曲）。

    1 番組内で同一曲が 2 回出ないよう ``exclude`` を受け取れる。
    """
    if not validate_year_range(year):
        year = settings.default_year
    count = max(1, int(count))
    songs = _select_in_year_first(year, count, exclude)
    if not songs:
        # 最後の保険：exclude が候補をすべて除いても空リストは返さない
        songs = [get_fallback_song(year, rng=rng)]
    chooser = rng if rng is not None else module_rng()
    chooser.shuffle(songs)
    return songs[:count]


def select_program_songs(
    year: int, count: int = 3, *, exclude: Optional[set] = None
) -> List[Tuple[str, str]]:
    """**決定的な**選曲。台本とプレイリストが同じ 1 本の事実源を見るために使う。

    ``get_fallback_songs`` は毎回 ``random.shuffle`` するため、**台本生成側と
    再生側が別々に呼ぶと別の曲になる**（これが提案②が指した source monitoring
    error の正体）。この関数はシャッフルしないので、呼び出し側が 1 回だけ計算して
    ``generate_radio_script(songs=...)`` とプレイリストの両方に渡せば一致する。

    Parameters
    ----------
    year:
        対象年。
    count:
        必要曲数。
    exclude:
        すでに採用した ``(曲名, アーティスト)`` の集合。

    Notes
    -----
    :func:`pinned_songs` で差し込み中の選曲結果があれば、**それを返す**
    （提案② タスク1「台本と選曲を 1 本の事実源に束ねる」）。
    差し込みが無い場合（既存呼び出し）は従来どおり年代パレットから選ぶ。
    """
    if has_pinned_songs():
        pinned = [
            s for s in current_pinned_songs() if s not in (exclude or set())
        ]
        return pinned[: max(1, int(count))]
    if not validate_year_range(year):
        year = settings.default_year
    count = max(1, int(count))
    songs = _select_in_year_first(year, count, exclude)
    if not songs:
        songs = [get_fallback_song(year, exclude=exclude)]
    return songs[:count]


def song_titles(songs: Optional[List[Tuple[str, str]]]) -> List[str]:
    """``(曲名, アーティスト)`` の列から曲名だけを取り出す（台本の許可リスト用）。"""
    return [title for title, _artist in (songs or [])]


# --- 選曲結果の差し込み口（提案② タスク1「台本と選曲を 1 本の事実源に束ねる」）--------
# `generate_care_script` / `generate_anniversary_script` は `_decade_songs()` を
# 経由して曲を得る。`_decade_songs()` → `_script_songs()` → `select_program_songs()`
# という**1 本の経路**に収束しているため、受け渡し口.select_program_songs
# 1 箇所だけに設ければ 3 モードすべての台本を同じ選曲結果に束ねられる
# （生成器ごとの引数を増やさない／模板の中に選曲ロジックを書かないため）。
#
# スレッド locals を使う理由: `server.py` は `ThreadPoolExecutor` で
# 同時生成 2 件を走らせる。文脈が漏れると別リクエストの曲名が混入する。
_PINNED_SONGS = threading.local()


def current_pinned_songs() -> List[Tuple[str, str]]:
    """差し込み中の選曲結果を返す（無ければ空リスト）。"""
    pinned = getattr(_PINNED_SONGS, "songs", None)
    return list(pinned) if pinned is not None else []


def has_pinned_songs() -> bool:
    """差し込みが**行われているか**（空リストでも True）。

    `current_pinned_songs()` は「無ければ空リスト」を返すため、
戻り値の真係では「差し込みが無い（``None``）」と
「差し込みが空（``[]``）」を区別できない。
`select_program_songs` はこの違いで結論が変わるため、
判定はこの関数に集約する。

    Round 1 の修正では `select_program_songs` 側の `if pinned:` が
残っていたため、`care_recreation` / `anniversary` では
「音源ゼロ」のにカタログから曲を選び直し、
司会が髖らない曲を統介していた（Round 2 の実測）。
    """
    return getattr(_PINNED_SONGS, "songs", None) is not None


@contextmanager
def pinned_songs(
    songs: Optional[Sequence[Tuple[str, str]]],
) -> Iterator[List[Tuple[str, str]]]:
    """台本生成のあいだだけ、選曲結果を差し替える。

    Parameters
    ----------
    songs:
        呼び出し側が**すでに選んだ** ``(曲名, アーティスト)`` の列。
        ``None`` は「差し込みなし」。

        **空リスト ``[]`` は「差し込みあり・曲なし」**として扱う。
        ``None`` 潰し（``normalized or None``）していた以前は、音源が 1 曲も
        無いとき（`server._step_resolve_previews` が `[]` を渡す）に
        選曲結果の差し込みが解除され、台本生成側が改めて
        `select_program_songs` でカタログから曲を選んでしまっていた。
        結果、**鳴らない曲名を司会に紹介**していた。

        `server.py` は「`or None` で潰さない。空リストと None は別物」と
        明記していたのに、この関数でも同じ潰しが起きている。
        起きている。

    Yields
    ------
    list[tuple[str, str]]
        実際に差し込まれた一覧（未差し込み・空どちらも空リスト）。

    Notes
    -----
    例外が出ても必ず元の状態へ戻す（``try/finally``）。
    """
    previous = getattr(_PINNED_SONGS, "songs", None)
    if songs is None:
        normalized = None
    else:
        normalized = [(str(title), str(artist)) for title, artist in songs if title]
    _PINNED_SONGS.songs = normalized
    try:
        yield list(normalized or [])
    finally:
        _PINNED_SONGS.songs = previous

def get_reminiscence_quiz(year: int) -> List[Dict[str, str]]:
    """デイサービス回想法用のクイズデータを取得

    丸め（``year // 10 * 10``）だけでは 2011〜2025 が黙って 2000 バケットへ落ち、
    東日本大震災を体験された方に 2000年のクイズを出していた。
    """
    if not validate_year_range(year):
        year = settings.default_year
    decade = (year // 10) * 10
    if decade in REMINISCENCE_DATA:
        return REMINISCENCE_DATA[decade]
    closest = min(REMINISCENCE_DATA.keys(), key=lambda d: abs(d - decade))
    logger.warning(
        "回想法クイズに %d 年代のバケットがありません。最寄りの %d 年代を代用します。",
        decade,
        closest,
    )
    return REMINISCENCE_DATA[closest]

# 回想法・介護レクリエーション用：年代別思い出クイズ＆会話のタネ
# すべての question は「1970年代、」のように年代を冒頭に明示する。
# バケットの取り違えが起きても読み上げ原稿そのものから検出できるようにするため。
REMINISCENCE_DATA: Dict[int, List[Dict[str, str]]] = {
    1950: [
        {
            "question": "1950年代、人々が押し寄せて観戦して大騒ぎした「力道山」のスポーツは何でしょう？",
            "answer": "プロレス",
            "hint": "空手チョップで一躍した日本人プロレスラーが話題になりました",
        },
        {
            "question": "1950年代に急速に普及した「三種の神器」とは、白黒テレビ、洗濯機と、もうひとつは何でしょう？",
            "answer": "電気冷蔵庫",
            "hint": "それまでは氷を入れて冷やしていました",
        },
        {
            "question": "1950年代の子どもたちが夢中になった、拍子木の音で始まる紙芝居は何のお話でしたか？",
            "answer": "黄金バットや少年探偵団",
            "hint": "水飴や型抜きを食べながら見たものです",
        },
    ],
    1960: [
        {
            "question": "1960年代、東京で開催された世界的スポーツの祭典は何でしょう？",
            "answer": "東京オリンピック",
            "hint": "東洋の魔女（バレーボール）が大活躍しました",
        },
        {
            "question": "1960年代、東京オリンピックの開業に合わせて登場した「夢の超特急」と呼ばれた乗り物は何でしょう？",
            "answer": "東海道新幹線（0系）",
            "hint": "当初4時間で東京と新大阪を結びました",
        },
        {
            "question": "1960年代、日本武道館で熱狂的なコンサートを行ったイギリスの4人組バンドは何でしょう？",
            "answer": "ザ・ビートルズ",
            "hint": "マッシュルームカットが大流行しました",
        },
    ],
    1970: [
        {
            "question": "1970年代、大阪で開催された日本万国博覧会。岡本太郎が制作したシンボルとなる塔は何でしょう？",
            "answer": "太陽の塔",
            "hint": "テーマは「人類の進歩と調和」でした",
        },
        {
            # 旧データ: 「オールナイトニッポン」を挙げていたが、同番組は 1971 年開始の
            # ため 1970 年の原稿では「その年に放送されていた番組ではない」warn になる
            # （scripts/validate_facts.py: stale-fact-in-script）。
            # 同じ深夜放送の文脈で、**1970 年から放送された**番組に差し替えた。
            "question": "1970年代、若者たちが夜更かしに聴きながら勉強した深夜放送は何と呼ばれていましたか？",
            "answer": "深夜放送（パックイン・ミュージックなど）",
            "hint": "受験生の夜のお供でした",
        },
        {
            "question": "1970年代、街のおもちゃ屋やゲームセンターで大ヒットした、銀色の球を当てて遊ぶゲーム機や玩具は何でしょう？",
            "answer": "インベーダーゲーム / スペースインベーダー",
            "hint": "100円玉を積んでゲームセンターに通う人もいました",
        },
    ],
    1980: [
        {
            "question": "1980年代に登場し、若者たちが街中で短いメッセージを送り合った携帯通信機器は何でしょう？",
            "answer": "ポケベル（ポケットベル）",
            "hint": "数字の語呂合わせ（0840＝おはよう）でやりとりしました",
        },
        {
            "question": "1980年代に発売され、お茶の間のテレビを独占した家庭用ゲーム機は何でしょう？",
            "answer": "ファミリーコンピュータ（ファミコン）",
            "hint": "スーパーマリオブラザーズが大ブームになりました",
        },
        {
            "question": "1980年代の終わりから平成にかけて、ジュリ扇を振って踊った時代景気の名前は何でしょう？",
            "answer": "バブル景気",
            "hint": "夜の街でタクシーがつかまらないほどでした",
        },
    ],
    1990: [
        {
            "question": "1990年代に開幕し、日本中に空前のサッカーブームを巻き起こしたプロリーグは何でしょう？",
            "answer": "Jリーグ",
            "hint": "カズダンスやヴェルディ川崎が大人気でした",
        },
        {
            "question": "1990年代に女子高生を中心に大流行した、足元に履くダボッとした白い靴下は何ソックスでしょう？",
            "answer": "ルーズソックス",
            "hint": "コギャル文化の象徴でした",
        },
        {
            "question": "1990年代に横行した、お世話を怠ると死んでしまう大ヒット電子ペットは何でしょう？",
            "answer": "たまごっち",
            "hint": "白たまごっちが高値で取引きれました",
        },
    ],
    2000: [
        {
            "question": "2000年代、シドニー五輪女子マラソンで金メダルを獲得し「最高で金、最低でも金」などの流行語を生んだ選手は誰でしょう？",
            "answer": "高橋尚子選手（Qちゃん）",
            "hint": "小出監督との二人三脚で国民栄誉賞を受賞しました",
        },
        {
            "question": "2000年代、日韓共同で開催された世界的なサッカーの大会は何でしょう？",
            "answer": "日韓ワールドカップ",
            "hint": "ベッカムヘアが大ブームになりました",
        },
        {
            # 旧データ: 答えに「2001年」を直書きしており、2000 年の原稿では
            # 「対象年より後の年を言及している」warn になっていた
            # （scripts/validate_facts.py: future-year-in-script）。
            # **年を尋ねない問い**に変更し、事実（Suica が交通系ICカードであること）は
            # 残したまま 4 桁の年を台本から排除した。
            "question": "2000年代、駅の改札機にタッチするだけで通過できる交通系ICカードの代表は、何でしょう？",
            "answer": "Suica（スイカ）などの交通系ICカード",
            "hint": "切符を買う行列が激減しました",
        },
    ],
    2010: [
        {
            # 旧データ: 質問文に「2011年3月11日」を埋め込んでいたため、2010 年の
            # 原稿では「対象年より後の年を言及している」warn になっていた。
            # 年ではなく**出来事名で尋ねる**形に変更した（答えは変えない）。
            "question": "2010年代、マグニチュード9.0の大地震と、その直後の大津波で甚大な被害をもたらした出来事を何と呼びますか？",
            "answer": "東日本大震災（東北地方太平洋沖地震）",
            "hint": "義援金活動やボランティアが全国で広がりました",
        },
        {
            "question": "2010年代、2010年にNTTドコモが販売して日本の携帯電話の景色を変えた海外製の端末は何ですか？",
            "answer": "iPhone（アイフォーン）",
            "hint": "画面をタッチして操作する、ボタンのない端末でした",
        },
        {
            # 旧データ: 質問文に「2012年12月」を埋め込んでいたため、2010・2011 年の
            # 原稿で warn になっていた。年月ではなく高さを尋ねる形に変更。
            "question": "2010年代、634メートルの高さで当時の世界一を記録した展望塔は何ですか？",
            "answer": "東京スカイツリー",
            "hint": "隅田川沿いの押上に建てられました",
        },
    ],
    2020: [
        {
            "question": "2020年代、全世界に大きく流行し、生活様式まで変わった感染症は何ですか？",
            "answer": "新型コロナウイルス感染症（コロナウイルス感染症）",
            "hint": "マスクや手指の消毒が日常になりました",
        },
        {
            "question": "2020年代、仕事も学校も家で過ごすようになり、広くようになった働き方の名前は何ですか？",
            "answer": "テレワーク（リモートワーク）",
            "hint": "リビングが、そのまま仕事場になりました",
        },
        {
            "question": "2020年代、スマホを見て払う「非接触」の支払い方法として急速に広がったものは何ですか？",
            "answer": "キャッシュレス決済（QRコード決済）",
            "hint": "PayPay や d払いなどが一般家庭にも入りました",
        },
        {
            # 旧データ: 質問・ヒントに「2025年」を埋め込んでいたため、2020〜2024 年の
            # 原稿では未来年の warn になる可能性があった（現状は当番外）。
            # 開催時期を年和月を含まない表現に置き換えることで 4 桁の年を外した。

            "question": "2020年代、大阪・関西万博が開かれた年は、日本国際博覧会として何周年ですか？",
            "answer": "50周年（1970年の大阪万博から数えて）",
            "hint": "春から秋の半年間、会場が開かれました",
        },
    ],
}


#: 和暦の名前と、その和暦 1 年に対応する西暦。
#: **4 桁の西暦を含まない**ため ``retro_radio.core.facts.future_year_mentions``
#: の走査に掛からない（対象年より後の年を文言として主張しない）。
#: 新しい元号から順に並べる。
_ERA_BASE_YEAR: Tuple[Tuple[str, int], ...] = (
    ("令和", 2018),  # 令和1年 = 2019年
    ("平成", 1988),  # 平成1年 = 1989年
    ("昭和", 1925),  # 昭和1年 = 1926年
)


def _era_label(year: int) -> str:
    """``year`` の和曰表現を返す。

    例: 1975 年 -> ``昭和50年`` / 2019 年 -> ``令和元年``。

    介護回想法の利用者は和曰で年を記憶しているため、
    西曰だけの原稿より受け容しやすい。
    **年ごとに文字列が変わる**ため、同じテンプレートのまま
    「年情報がない」``normal`` モードの欠陷も同時に解消する。

    対象年以外の年を含みないため ``future_year_mentions`` の违反にはならない。
    """
    for name, base in _ERA_BASE_YEAR:
        era_year = year - base
        if era_year >= 1:
            return f"{name}{era_year}年"
    return f"{year}年"


def _script_songs(
    year: int, songs: Optional[List[Tuple[str, str]]], limit: int
) -> List[Tuple[str, str]]:
    """原稿に埋め込む（=名前を出す）曲を決める。**1 本の事実源**。

    Parameters
    ----------
    year:
        対象年。
    songs:
        呼び出し側が**すでに選曲した結果**。渡されたらそれだけを使う
        （提案② の核心: 台本と選曲が二重に選ばない）。
    limit:
        最大何曲まで名前を挙げるか。

    Notes
    -----
    ``songs`` を渡さない旧来の呼び出し（``generate_fallback_script(y, m, d)``）は
    決定的な :func:`select_program_songs` に落ちるため、**同じ入力なら同じ原稿**になる。

    **空リスト ``[]`` は「1 曲も鳴らない」と確定した状態**として扱い、
    カタログから選び直さない（`None` と区別する）。
    以前は `if songs:` で両者を同じ扱いにして、音源ゼロなのに
    `select_program_songs` がカタログから曲を選び直し、
    司会が**鳴らない曲を紹介**していた。
    """
    if songs is None:
        return select_program_songs(year, limit)
    picked = [(str(t), str(a)) for t, a in list(songs)[:limit] if t]
    if picked:
        return picked
    # 明示的に空を渡された = 音源ゼロ。カタログへはフォールバックしない。
    return []


def _song_phrase(song: Tuple[str, str]) -> str:
    # 「曲名」（アーティスト）の形にする。曲名は「」、アーティストは（）で
    # 囲むので eval.metrics.songs の曲名抽出ルールに載る。
    title, artist = song
    return f"「{title}」（{artist}）"


def _music_promise_sentence(year: int, song_count: int) -> str:
    """**実際に鳴る曲の数**に合わせて「何曲ご用意しました」かを書く（P0-5）。

    音源が 0 曲なのに「三つほどご用意しました」と約束すると、間奏しか
    流れない番組に未履行の約束をする。1〜2 曲しかないときも同様に嘘になる。
    """
    if song_count <= 0:
        return (
            f"本章では、{year}年のくらしの風景を、昔ながらの音とともに"
            "お過ごしいただきたいと思います。"
        )
    if song_count == 1:
        return f"本章では、{year}年のヒット曲をひとくちだけご用意しました。"
    if song_count == 2:
        return f"本章では、{year}年のヒット曲をふたつご用意しました。"
    return (
        f"本章では、{year}年のヒット曲と、当時のくらしの風景を三つほどご用意しました。"
    )


def _generate_no_music_script(year: int, month: int, day: int, *, era: str) -> str:
    """**音源ゼロ**のとき用の原稿（曲名を一切書かない）。

    `songs=[]` は「1 曲も鳴らせない」と呼び出し側が確定した状態を意味する
    （`server._step_resolve_previews` が iTunes で音源を取れなかった場合に渡す）。
    ここで曲名を挙げると、司会が「次は『○○』です」と**鳴らない曲を紹介**し、
    利用者は嘘を聞くことになる。

    曲スロットを持たない構成でも原稿としては成立するように、
    年の空気感と司会の会話を保つ（間奏が主体の番組になる）。
    """
    return f"""### オープニング
皆様、こんばんは。レトロラジオ・タイムマシンの時間でございます。ダイヤルを合わせていただき、誠にありがとうございます。
本日皆様とともに旅をする時代は、{year}年{month}月{day}日（{era}）でございます。
{_music_promise_sentence(year, 0)}どうぞ、お茶をお用意のうえで、ひとつ腰を落ち着けてお過ごしください。

### トーク1_ニュース
{year}年といえば、街のあちこちから活気あふれる声が響き渡り、人々の笑顔と希望に満ちあふれていた時代でございました。
当時の世相を少し振り返ってみますと、人々は日々ひたむきに働き、明日は今日よりもきっと良くなると信じて手を取り合い、助け合って前を向いて生きておりました。
その当時の田園には、トラクターのエンジン音と登校ベルが重なって聞こえていたものでございます。

### トーク2_くらし
夕暮れ時になりますと、どこか懐かしいお醤油の香ばしい匂いや、夕餉の支度をする台所の包丁の音が路地裏に優しく漂い、近所の子どもたちが「また明日遊ぼうね」と元気に手を振り合いながら家路を急いでおりました。
各家庭のお茶の間には、真空管ラジオや白黒・カラーテレビが家族の中心に置かれ、同じ番組を眺め、同じ話題で笑い合っていた温もりあるひとときを、昨日のことのように思い出されます。

### トーク3_共感
物価や生活様式こそ今とは大きく異なっておりますが、そうした日常のありふれた一コマ一コマすべてが、今となってはかけがえのない大切な青春と人生の思い出のアルバムでございます。
都会では電気があたりまえのように使われておりましたが、当時の村には、まだあたたかな灯りが残っていたものでございます。
各家庭の日記には天気や菜価、そして子どもの誕生日が書かれましょう。それは最もありふれた、しかし最も珍惜すべき日常の記録でございます。

### エンディング
今宵の余韻を胸に抱きながら、本日の放送を閉めくくります。
レトロラジオ・タイムマシン、{year}年の放送でありました。"""


def _cue_song(pinned: List[Tuple[str, str]], position: int) -> Optional[Tuple[str, str]]:
    """``position`` 番目のトークが告げる（= その直後に流れる）曲を取り出す。

    ``server.build_playlist`` は 曲0 → トーク0 → 曲1 → トーク1 → … と組むため、
    **``position`` 番目のトークの直後に流れるのは ``position + 1`` 番目の曲**。
    ここを 0 始まりで取るのが本関数の契約。

    曲が足りないときは**末尾の 1 曲で埋めず** ``None`` を返す。
    黙って別の曲名を告げると、司会が一度も紹介していない曲が流れるため。
    """
    index = position + 1
    if index < len(pinned):
        return pinned[index]
    return None


def _cue_line(prefix: str, song: Optional[Tuple[str, str]], suffix: str) -> str:
    """曲振り台詞 1 行を組み立てる。曲が無いときは**空行**を返す。"""
    if not song:
        return ""
    return f"{prefix}{_song_phrase(song)}{suffix}"


def generate_fallback_script(
    year: int, month: int, day: int, *, songs: Optional[List[Tuple[str, str]]] = None
) -> str:
    """通常モードの定型フォールバック原稿生成（セグメント構造）

    曲で始まり曲で終わる番組構成（server.build_playlist）に合わせ、
    オープニングとエンディングには「曲をお届けします」台詞を置かない
    （テーマ曲はこの読み上げの前後で既に鳴っているため）。

    Parameters
    ----------
    year, month, day:
        対象日。
    songs:
        **選曲済み**の曲リスト。渡すと、台本に名ざす曲名は
        このリストに含まれるものだけになる（提案② の source monitoring error 対策）。
        省略時は決定的な :func:`select_program_songs` が選曲する（後方互換）。

    Notes
    -----
    旧実装は「三つほどご用意しました」と予告してから**何も配らず**、
    ``normal`` モードの原稿が年ごとに 1 文字も変わらない状態だった
    （S2 が ``eval/`` で実測: 長さの標準偏差 0.0）。ここでは:

    1. 予告した 3 件を**実際の曲名として配信する**（未履行予告を解消）。
    2. 和暦と曲名を入れて**年ごとに原稿が変わる**ようにする。
    """
    pinned = _script_songs(year, songs, 5)
    if not pinned:
        # 音源ゼロ（``songs=[]`` が明示された）。曲名を一切書かない原稿を返す。
        # 以前はここで `picked[0]` を無条件に読んで IndexError になっていた。
        return _generate_no_music_script(year, month, day, era=_era_label(year))
    first = _cue_song(pinned, 1)
    second = _cue_song(pinned, 2)
    third = _cue_song(pinned, 3)
    era = _era_label(year)
    # 曲振りできるトーク数 = `first`/`second`/`third` が取れた個数。
    # 「三つほどご用意しました」はこの数と一致していなければ嘘になる。
    promise = _music_promise_sentence(year, 1 + sum(1 for s in (first, second, third) if s))

    return f"""### オープニング
皆様、こんばんは。レトロラジオ・タイムマシンの時間でございます。ダイヤルを合わせていただき、誠にありがとうございます。
いま鳴り響いているのは、この番組のテーマ曲でございます。古い受信機から立ちのぼるその音は、文字どおりあの時代の空の色をしております。
本日皆様とともに旅をする時代は、{year}年{month}月{day}日（{era}）でございます。
{promise}どうぞ、お茶をお用意のうえで、ひとつ腰を落ち着けてお過ごしください。
レトロラジオ・タイムマシン、{year}年の放送であります。

### トーク1_ニュース
{year}年といえば、街のあちこちから活気あふれる声が響き渡り、人々の笑顔と希望に満ちあふれていた時代でございました。
当時の世相を少し振り返ってみますと、人々は日々ひたむきに働き、明日は今日よりもきっと良くなると信じて手を取り合い、助け合って前を向いて生きておりました。
あの頃のご飯のにおいや、夕暮れの空の色は、いまでも鮮明に思い出せます。
{_cue_line("それでは、この年のヒット曲、", first, "をお届けいたします。")}

### トーク2_くらし
夕暮れ時になりますと、どこか懐かしいお醤油の香ばしい匂いや、夕餉の支度をする台所の包丁の音が路地裏に優しく漂い、近所の子どもたちが「また明日遊ぼうね」と元気に手を振り合いながら家路を急いでおりました。
各家庭のお茶の間には、真空管ラジオや白黒・カラーテレビが家族の中心に置かれ、同じ番組を眺め、同じ話題で笑い合っていた温もりあるひとときを、昨日のことのように思い出されます。
駅前の商店街には活気があふれ、八百屋さんや魚屋さんの威勢の良い掛け声が響き、駅前の純喫茶からは珈琲の香りと、流行りの音楽が静かに流れておりました。
{_cue_line("懐かしい一曲、", second, "もお届けいたします。")}

### トーク3_共感
物価や生活様式こそ今とは大きく異なっておりますが、そうした日常のありふれた一コマ一コマすべてが、今となってはかけがえのない大切な青春と人生の思い出のアルバムでございます。
現代の慌ただしい日常からほんの少しだけ離れて、あの頃の懐かしい風景と優しい空気感を、どうぞ心ゆくまで思い出していただければ幸いでございます。
{_cue_line("それでは、この年のもう一曲、", third, "をどうぞお聞きください。")}


### エンディング
さて、ここからは皆様お待ちかねの音楽の時間でございます。
あの輝かしい時代を鮮やかに彩った大ヒット曲を、レコードの温かみある音色とともにお届けいたしましょう。
今宵の余韻を胸に抱きながら、本日の放送を閉めくくります。
来年も、この周波数でお会いできることを楽しみにしております。
レトロラジオ・タイムマシン、{year}年の本日の放送はお開きでございます。
ありがとうございました。
"""


def _decade_songs(
    year: int, limit: int = 2, *, songs: Optional[List[Tuple[str, str]]] = None
) -> List[Tuple[str, str]]:
    """その年代の代表曲を決定的に（シャッフル無しで）取り出す（原稿への埋め込み用）


    ``songs``（選曲済み）が渡されたら**それだけを返す**。これが提案② の
    「二重選択の廃止」の中核で、台本とプレイリストが別の曲を見ることを防ぐ。
    """
    return _script_songs(year, songs, limit)


def _historical_content_health() -> Dict[str, object]:
    """歴史番組スロットの状態を返す（運用・ヘルスチェック用）。

    縮退している場合は ``degraded=True`` と理由を返す。UI はこれを見て
    「1975 年の番組情報」の一角に警告を出せる。
    """
    return facts_health()


def _decade_programs(year: int, limit: int = 2) -> List["ProgramSchedule"]:
    """対象年に放送されていた**ラジオ**歴史番組を決定的に取り出す（原稿への埋め込み用）

    バケット丸め（``year // 10 * 10``）ではなく、事実レジストリの
    ``valid_from`` / ``valid_to`` で「その年に放送されていたか」を判定する。
    1975 年の原稿に 1970 年で終了した番組を出さないための変更。

    R2-11 コア: ``programs_for_year`` は番組表用にテレビ番組も含むため、
    ここでは **``radio_program`` だけ**に絞る。テレビ番組（料理教室、
    ザ・ヒットパレード等）がラジオの読み上げ原稿に出ると時代錯誤になる。
    """
    return [
        _schedule_from_fact(record) for record in radio_programs_for_year(year)
    ][:limit]


def _program_sentence(year: int, limit: int = 2) -> str:
    """実在番組名を読み上げ用の一文に組み立てる。

    実在が確認できない架空番組名を「その頃のお茶の間で流れていた」と断定しない。
    """
    decade = (year // 10) * 10
    programs = _decade_programs(year, limit)
    if not programs:
        return (
            f"{decade}年代のお茶の間では、朝のニュース番組や深夜の音楽番組などが"
            "日々の小さな時間を過ごしていました"
        )
    joined = "、".join(f"「{p.title}」（{p.start_time} 放送開始）" for p in programs)
    detail = programs[0].description.strip().rstrip("。")
    return (
        f"{decade}年代には、{joined} といった番組がありました。"
        f"たとえば「{programs[0].title}」は、{detail}"
    )


def generate_care_script(year: int, month: int, day: int) -> str:
    """介護施設・デイサービス回想法向けのレク用原稿生成（セグメント構造）"""
    # 上限は **5**。トークが 3 個あるため「トークN の直後に流れる曲」= ``pinned[N+1]``
    # が 3 番目（index 4）まで存在しない Toe らない。4 だとトーク3 で曲振りが
    # 黙って消え、司会が最後に曲を紹介しない放送になる（実測）。
    songs = _decade_songs(year, 5)
    # 音源ゼロ（`pinned_songs([])`）なら曲名を持つない原稿にする。
    # 以前は `songs[0]` を無条件に読で IndexError になっていた。
    # Round 3: `_cue_line` を使うので、曲がないときは文ごと落とす。
    # 以前は `or ("", "")` で空ののみを入れ、原稿に
    # `「」（）」` という空の鉄括弧が出ていた。
    #
    # 共通番組フォーマット（オープニング + トーク3 + エンディング）により、
    # トークは 3 個になり「トークN の直後に流れる曲」= ``pinned[N]`` となる。
    # したがって参照するのは 1〜3 番目の曲（0 番目はオープニング曲）。
    first_song = _cue_song(songs, 1)
    second_song = _cue_song(songs, 2)
    third_song = _cue_song(songs, 3)
    q1, q2, q3 = (get_reminiscence_quiz(year) * 3)[:3]
    program_sentence = _program_sentence(year, 2)

    return f"""### オープニング
皆様、こんにちは。今日もデイサービスの皆様とお会いできて、大変嬉しく存じます。
本日の回想法レクリエーションのお時間は、時計の針をぐっと巻き戻しまして、{year}年{month}月{day}日へとタイムスリップしてまいります。
大きな歓声が飛び交う会場ではなく、静かで温かなレコードの音色が、その頃の空気をゆっくりと満たしてゆくようでございます。
賑やかさを抑えた、落ち着いたひとときをどうぞお過ごしください。
いま鳴っている曲こそ、この番組のテーマでございます。

### 思い出話1
{year}年当時、皆様はおいくつで、どんな毎日をお過ごしでしたでしょうか。
子育てに奮闘されていた方、お仕事に熱中されていた方、あるいは学生時代を謳歌されていた頃でしょうか。
当時の街並みには活気があふれ、夕方にはお豆腐屋さんのラッパの音や、八百屋さんの威勢の良い声が路地裏に響いておりました。
お茶の間には家族が自然と集まり、ひとつの歌番組を一緒に口ずさみながら、温かなご飯を囲んでいたものでございます。
{program_sentence}。
家族全員が同じソファーに座り、同じ画面を見つめ、同じ曲を口ずさむ。喧騒もない、穏やかなひとときでございました。
{_cue_line('それでは、この年の懐かしい名曲', first_song, 'を、どうぞご一緒に口ずさみながら、')}

### 思い出話2
さて、思い出のタネをひとつほど上げます。「{q1['question']}」
ヒントをひとつ。「{q1['hint']}」
この言葉をお控えいただき、引き出しのなかを探してみてください。
続いてもうひとつ、「{q2['question']}」——答えは『{q2['answer']}』でした。
お分かりになりますか。「{q2['hint']}」という言葉を思い出し、引き出しのなかを探してみてください。
{_cue_line('その頃の空気をまとった懐かしい一曲、', second_song, 'もお届けいたします。')}

### 思い出話3
そして最後は三つ目のタネ、「{q3['question']}」。
答えは『{q3['answer']}』でございます。
この三つを照らし合わせますと、その時代の生活感と、当時の道具や技術的な工夫までが具体的に見えてくるはずでございます。
この年のヒット曲が心を打つのは、派手な言葉が多いからではなく、日々の記憶が積み重なっているからだと存じます。
{_cue_line(f'そして最後に、{year}年の空気をまとったもうひと曲', third_song, 'をお届けいたします。')}
肩の力を抜いて、鼻歌とともにお楽しみくださいましたら結構です。

### エンディング
あの頃の温かな思い出が、皆様の心に灯りともりますように。
読み上げる標語ではなく、どうか胸の静けさの中でゆっくり記憶をたどってみてください。
またお会いできる日を楽しみにしております。
以上、本日の回想法レクリエーションを終わります。ご参加ありがとうございました。"""

def generate_anniversary_script(year: int, month: int, day: int, target_name: str = "大切なあなた") -> str:
    """誕生日・記念日ギフト用の特別祝福原稿生成（セグメント構造）"""
    # 上限は **5**（``generate_care_script`` と同じ理由。トーク3 の直後=5 番目）。
    songs = _decade_songs(year, 5)
    # 音源ゼロ（`pinned_songs([])`）なら曲名を持つない原稿にする。
    # 以前は `songs[0]` を無条件に読で IndexError になっていた。
    # Round 3: `_cue_line` を使うので、曲がないときは文ごと落とす。
    # 以前は `or ("", "")` で空ののみを入れ、原稿に
    # `「」（）」` という空の鉄括弧が出ていた。
    first_song = _cue_song(songs, 1)
    second_song = _cue_song(songs, 2)
    third_song = _cue_song(songs, 3)
    program_sentence = _program_sentence(year, 2)

    return f"""### オープニング
皆様、特別な日のラジオ放送へようこそ。
本日ダイヤルを合わせましたのは、かけがえのない記念日、{year}年{month}月{day}日でございます。
{target_name}様、特別な記念日を心よりお祝い申し上げます。
本日のダイヤルは、{year}年という年を辿るためのものです。
この番組のテーマ曲とともに、{year}年へお迎えいたします。

### 記念日のエピソード1
この日、この世界にあなたが誕生されたとき、あるいはこの記念の日を迎えたとき、日本はどのような時代を迎えていたのでしょうか。
{year}年、街には新しい時代の息吹が満ち、人々は希望と笑顔にあふれておりました。
{program_sentence}。
{target_name}様の青春と重なる記憶に、どうかこの音を重ねてください。
{_cue_line('それでは、この年の記念の一曲', first_song, 'をお届けします。')}

### 記念日のエピソード2
あなたがこれまで歩んでこられた日々のすべての瞬間が、周囲の皆様へのあたたかな光となり、素晴らしい歴史を紡いでこられました。
誰かの青春のBGMとして鳴りつづけていた年に、{target_name}様ご自身が生きてこられた道筋が息づいております。
{_cue_line('あの頃のヒット曲には、', second_song, 'のように、心にまっすぐ届く歌声がありました。')}
{year}年という年は、{target_name}様の歩みの背景音のようにずっと鳴りつづけております。

### 記念日のエピソード3
今日という特別な日に、あなたが生まれた{year}年に日本中で愛されていた大ヒット曲を、心からの祝福の気持ちを込めてお送りいたします。
懐かしいメロディーとともに、これまでの歩みと、これからの素晴らしい日々に乾杯いたしましょう。
この一年が、健康とよろこびにあふれたものでありますよう、また、来年の今日にもよい思い出を積み上げていけるものでありますよう、心よりお祈り申し上げます。
{_cue_line('この年の最後の一曲を、ここに', third_song, 'をお聞かせして、本日の放送を締めくくりましょう。')}

### エンディング
{target_name}様、改めましておめでとうございます。
素晴らしい一年となりますよう、心よりお祈り申し上げます。
年を数えるのではなく、{year}年のこの季節に思いをはせる時間こそ、かけがえのないものです。
レトロラジオ・タイムマシン、{year}年の本日の放送はお開きでございます。ありがとうございました。"""


def _hist(title: str, pid: str, start: str, duration: int, description: str, source: str) -> ProgramSchedule:
    """歴史番組の生成（説明文は decade 単位の表現に統一し、対象年より後の年を書かない）"""
    return ProgramSchedule(
        id=pid,
        title=title,
        start_time=start,
        duration=duration,
        description=description,
        is_historical=True,
        source=source,
    )


def _schedule_from_fact(record: Dict[str, object]) -> ProgramSchedule:
    """事実レジストリの 1 レコードを ``ProgramSchedule`` にする。

    ``description`` には ``description_ja``（対象年に対して常に安全な
    年代表現）を使う。``claim_ja`` は出典つきの正本の主張であり、
    読み上げ原稿には載せない（``description_ja`` がなければ代用する）。

    ``source`` にはレジストリの出典（文献名）を入れる。提案⑦の実装案 5
    「UI に出典を小さく出す」の根拠になるが、UI 側の配線は本モジュールの
    所有範囲外なので、ここでは値を渡すだけにする。
    """
    return ProgramSchedule(
        id=str(record["id"]),
        title=str(record["title"]),
        start_time=str(record["start_time"]),
        duration=int(record["duration_min"]),  # type: ignore[arg-type]
        description=str(record.get("description_ja") or record.get("claim_ja") or ""),
        is_historical=True,
        source=str(record.get("source") or "出典未記載"),
    )


# 後方互換のための非正規化ビュー。**正本は retro_radio/core/facts/*.json**。
#
# ここは「そのキーの年に放送されていた番組」の一覧であって、唯一の解決ルールでは
# ない。実際の解決は ``programs_for_year(year)``（``valid_from`` / ``valid_to`` で
# 判定する）で行い、本辞書は読み取り専用の互換層として残す（既存テストが参照する）。
#
# キー 2025 はかつて存在しなかった。バケットが 1 件だけだと ``year % 1 == 0`` となり
# 2020〜2025 年のすべてに同じ番組が出ていた。レジストリから生成することで、
# 対象年ごとに番組が変わる resolver に統合している。
_RADIO_PROGRAM_BUCKET_KEYS = (1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020, 2025)

# 歴史的なラジオ・テレビ番組データベース
# 介護施設の利用者は実際にその時代の番組を視聴していた。架空の番組名を
# 「その頃のお茶の間で流れていた」と断定的に語ると、時代の記憶と食い違う。
# そのため、事実レジストリには実在が確認できる番組だけを置く。
# **この辞書の構築で import が失敗してはいけない。** 正本
# （``core/facts/programs.json``）が壊れていると、アプリ全体が起動しなく
# なり、運用者はスタックトレースだけを見ることになる。
# ``programs_for_year`` は読み込み失敗時に空リストを返し、
# ``facts_health()["degraded"]`` が True になる（歴史番組スロットが空に
# なり、``_program_sentence`` が一般的な言い回しに落ちる）。
RADIO_PROGRAMS_BY_DECADE: dict[int, list["ProgramSchedule"]] = {
    key: [
        _schedule_from_fact(record)
        for record in programs_for_year(key)
    ]
    for key in _RADIO_PROGRAM_BUCKET_KEYS
}

# 表示・読み上げテキストから 4 桁の西暦を取り出すためのパターン（既存と同一）
_YEAR_IN_TEXT = re.compile(r"(1[5-9]\d{2}|20\d{2})")

# 「○年代」の表記パターン。4 桁の西暦の検索では同じ文字列の一部しか取れず、
# 「年代表記である」ことが検証できないため、別に評価する。
_DECADE_IN_TEXT = re.compile(r"(1[89]\d\d|20\d{2})\s*年代")


def _mentions_future_year(schedule: ProgramSchedule, year: int) -> bool:
    """タイトルまたは説明に ``year`` より後の年の記載があるか。

    **4 桁の西暦と「○年代」の両方**を見る。旧実装は 4 桁の西暦しか見て
    いなかったため、年代表記の穴が残っていた。年代表記の閾値は
    **年代バケットの開始年**（2020年代なら 2020）なので、``year=2015`` の
    番組に「2020年代」とあるのは violation である。
    """
    blob = f"{schedule.title} {schedule.description or ''}"
    return bool(future_year_mentions(blob, year))


class HistoricalRadioPrograms:
    """Historical radio program database"""

    # Access module-level data via class attribute（後方互換。実害は解決側）
    @classmethod
    def _get_radio_programs_by_decade(cls) -> dict[int, list["ProgramSchedule"]]:
        return RADIO_PROGRAMS_BY_DECADE

    @classmethod
    def _get_programs_for_year(cls, year: int) -> list["ProgramSchedule"]:
        """対象年に放送されていた歴史番組（正本の並び順のまま・決定的な順序）"""
        return [_schedule_from_fact(record) for record in programs_for_year(year)]

    @classmethod
    def _historical_pick(cls, year: int):
        """対象年に提示してよい歴史番組を1本、決定的に選ぶ

        バケット丸め（``year // 10 * 10``）ではなく、事実レジストリの
        ``valid_from`` / ``valid_to`` で「その年に放送されていたか」を判定する。
        ``year % len(eligible)`` による決定的な回転は旧実装から引き継ぐ
        （同じ入力なら常に同じ出力）。
        """
        eligible = [
            schedule
            for schedule in cls._get_programs_for_year(year)
            if not _mentions_future_year(schedule, year)
        ]
        if not eligible:
            return None
        return eligible[year % len(eligible)]

    @classmethod
    def _modern_schedules(cls, year: int) -> list["ProgramSchedule"]:
        """現代的な番組枠（年依存のタイトルは ``year`` を正確に反映する）"""
        return [
            ProgramSchedule(
                id="modern_1",
                title="モーニングニュース",
                start_time="06:00",
                duration=30,
                description="最新のニュースと天気予報",
            ),
            ProgramSchedule(
                id="modern_2",
                title="文化の窓",
                start_time="10:00",
                duration=60,
                description="日本の文化と芸術",
            ),
            ProgramSchedule(
                id="modern_3",
                title=f"特集: {year}年の回想",
                start_time="13:00",
                duration=90,
                description=f"{year}年を振り返る、時代の記憶をたどる特別番組をお届けします",
            ),
            ProgramSchedule(
                id="modern_4",
                title="ヒット曲メドレー",
                start_time="19:00",
                duration=120,
                description="その年の代表曲をお届けします",
            ),
        ]

    @classmethod
    def get_program_guide(cls, year: int, month: int, day: int) -> ProgramGuide:
        """Generate program guide for specific date

        同じ入力なら常に同じ出力になること、未来の年を提示しないこと、
        同じタイトルを2回出さないことを保証する。
        """
        weekday_names = ["日", "月", "火", "水", "木", "金", "土"]
        try:
            weekday = weekday_names[datetime(year, month, day).weekday()]
        except ValueError:
            logger.warning(f"存在しない日付のため曜日を判定できません: {year}-{month}-{day}")
            weekday = "不明"

        today_highlight = "レトロラジオ・タイムマシン"
        schedules: list["ProgramSchedule"] = []
        used_titles: set = set()

        def _accept(schedule: "ProgramSchedule") -> None:
            if _mentions_future_year(schedule, year):
                logger.debug("未来の年を含む番組は提示しません: %s", schedule.title)
                return
            if schedule.title in used_titles:
                logger.debug("タイトル重複のため提示しません: %s", schedule.title)
                return
            used_titles.add(schedule.title)
            schedules.append(schedule)

        historical = cls._historical_pick(year)
        if historical is not None:
            _accept(historical)

        for schedule in cls._modern_schedules(year):
            _accept(schedule)

        return ProgramGuide(
            date=f"{year:04d}-{month:02d}-{day:02d}",
            weekday=weekday,
            schedules=schedules,
            today_highlight=today_highlight,
            special_events=[],
        )
