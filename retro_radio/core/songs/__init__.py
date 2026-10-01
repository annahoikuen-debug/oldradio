"""曲カタログ（正本）のローダ。

`core/facts/` と同じ方針で、**このアプリが流せる曲の正本は
`core/songs/songs.json` 1 ファイル**に置く。ここに無い曲はこのアプリでは
選曲しない。

なぜ正本化するのか
------------------
1 回の番組で 6 曲（既定 3 周なら 18 曲）流すため、1 年あたり 50 曲程度の
プールが無いと「同じ曲が繰り回される」ことになる。旧実装の
``FALLBACK_SONGS`` は 9 バケット × 4 曲 = **全 36 曲**しか無く、76 年に対して
曲不足が構造的だった。

現状（正直に）
--------------
**この契約は満たせていない。** 正本には現在 36 曲しか無く、1 年あたり
1〜4 曲である（:func:`catalog_health` が実測値を返す）。そのため
:func:`pool_for_year` は隣接年・同じ 10 年帯へ候補を**広げて** 18 曲を
確保しており、対象年の曲ではない曲も流れる。広げた範囲内では重複しないが、
「その年の曲だけ」というもう一つの条件は成立していない。

不足を黙って見せないため、

* :func:`catalog_health` が年ごとの曲数と不足量を外へ出す
* :func:`pool_for_year` が要求数を満たせなかったときに ``ERROR`` ログを出す
* 正本の読み込み時に 1 度だけ ``WARNING`` ログを出す

という 3 段階で可視化している。


さらに、このアプリは介護施設でも使われるため**「台本の曲名と実際の音源が
一致すること」**が安全要件になる。iTunes の曖昧検索で先頭 1 件を採用すると
「卒業写真（荒井由実）」と読み上げながら「ルージュの伝言」が流れるような
取り違えが起きる（実測）。正本に曲名・アーティスト・発表年を 1 レコードとして
固定することで、この取り違えを検出・排除できる。

本パッケージの約束
-----------------
1. **1 曲 1 レコード**。正本は ``songs.json``。
2. **発表年は必ず 1 つの年**。年バケット丸め（``year // 10 * 10``）で
   「その年ではない曲」を出さない。隣接年へ広げるのは
   :func:`songs_for_year` の ``tolerance`` だけ。
3. **``source`` は必須**。欠落は ``scripts/validate_songs.py`` の CI ゲートで
   fail する。URL を推測で書かない。

外部依存は持たない（標準ライブラリだけ）。iTunes との突き合わせは
``core/music_search.py``、選曲順序は ``core/song_selector.py`` の責務。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 正本ファイルの置き場（このファイルと同じディレクトリ）
SONGS_DIR = Path(__file__).resolve().parent

# 正本ファイル。増分したときは必ずこの一覧にも足す。
REGISTRY_FILES = ("songs.json",)

# レコードに必ず存在しなければならないフィールド。
REQUIRED_FIELDS = (
    "id",
    "title",
    "artist",
    "release_year",
    "rank",
    "source",
    "confidence",
)

# 取り違えを防ぐための許可値。
ALLOWED_CONFIDENCE = ("verified", "unverified")

# レコードの任意フィールド。
OPTIONAL_FIELDS = ("genre", "note_ja")

#: 1 回の放送（1 番組）で実際に再生する曲数。
#: ``settings.program_loop_count``（既定 3）× ``settings.program_min_song_count``
#: （既定 6）。**1 番組内で 1 曲も重複させないための最小要件**。
PROGRAM_SONGS_PER_BROADCAST = 18

#: 1 年あたりの**目標**曲数。
#:
#: これは目標であって現状ではない。現在の正本は 1 年あたり 1〜4 曲しかなく、
#: PROGRAM_SONGS_PER_BROADCAST に遠く及ばない。そのため
#: :func:`pool_for_year` は隣接年・同じ 10 年へ候補を**広げて**確保する。
#: 広げた範囲内では重複しないが、対象年の曲ではない曲も流れる。
#:
#: この不足は黙って見せない。:func:`catalog_health` が実際の年ごと曲数を
#: 外へ出し、:func:`pool_for_year` が要求数を満たせなかったときに ``ERROR`` を
#: 出し、正本の読み込み時に 1 度だけ ``WARNING`` を出す。
#:
#: ここを実測値に追随させない。実測値は :func:`catalog_health` が
#: 正本の状態として返す値であり、この定数は「目標」の宣言である。
TARGET_SONGS_PER_YEAR = 50

# 「(曲名, アーティスト)」の同一性判定用に落とす装飾。
# iTunes の ``trackName`` には " (2014年新録)" や "- Single Version" などが
# 混ざるため、比較の前にここで除去する。
_BRACKETED = re.compile(r"\((?:[^()]*)\)|\[[^\[\]]*\]|【[^【】]*】")

# ハイフン以降を落とすのは「その先が装飾である」場合だけ。
# 曲名にハイフンが含まれるものは「君だけを-Extradition-」のように正当な
# 表記があるため、無条件に切ると別の曲を 1 つに溶かしてしまう。
_SUFFIX_WORDS = (
    "single", "album", "version", "ver", "remix", "live", "edit", "remaster",
    "mono", "stereo", "instrumental", "karaoke", "orchestral", "piano",
    "bonus", "track", "mix", "acoustic", "demo", "cover", "off vocal",
    "新録", "再録", "復刻", "盤", "別disk",
)
_HYPHEN_SUFFIX = re.compile(
    r"\s*[-‐-―~]\s*(?:%s).*$" % "|".join(re.escape(word) for word in _SUFFIX_WORDS),
    re.IGNORECASE,
)
_TRAILING_JUNK = re.compile(r"[\s\-‐-―_~・]+$")


class SongCatalogError(RuntimeError):
    """正本 JSON が読み込めないときに送出する。"""


def _apply_decorations(text: str, *, strip_bracket_suffix: bool) -> str:
    """装飾を除去する。``strip_bracket_suffix`` で扱いが分かれる。"""
    if strip_bracket_suffix:
        text = _BRACKETED.sub("", text)
    text = _HYPHEN_SUFFIX.sub("", text)
    return _TRAILING_JUNK.sub("", text)


def normalize_song_text(value: str) -> str:
    """曲名・アーティスト名の比較用正規化。

    NFKC 正規化 → 小文字化 → 装飾の除去 → 空白の全除去、の順。
    「大塚愛」と「大塚 愛」、「モーニング娘。」と「モーニング娘」のような
    表記揺れを同じものとして扱える。**比較専用**で、表示用の文字列には
    戻さないこと（読み上げ原稿へ出るのは正本の表記である）。

    **括弧書きのバージョン注記は除去する。** iTunes の ``trackName`` には
    「神田川(2014年新録)」や「Lemon (Live)」のように版情報が付くため、
    曲名と一致させる必要がある。

    方針は「**同じ曲なら鳴らす・別の曲なら黙る**」:

    * 対象曲が iTunes に無い → 間奏（黙る）
    * 対象曲の再録・ライブ版がある → その音源を鳴らす
      （演奏者が同じなら同じ曲のことなので、同一の曲として扱う）
    * 別の曲を返している → 採用しない（間奏）

    再録を鳴らしても虚偽の主張は含まれない。台本は「この年のヒット曲
    「神田川」ということだけを言い、**録音年が語られることはない**。

    「黙るか鳴らすか」の判断材料に iTunes の年情報は使えない
    （``releaseDate`` は配信日で発表年ではない）。
    """
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    return re.sub(r"\s+", "", _apply_decorations(text, strip_bracket_suffix=True))


def song_key(title: str, artist: str) -> str:
    """曲の一意キー（正規化済み）。ローテーション履歴とキャッシュの主キー。"""
    return f"{normalize_song_text(title)}\u0000{normalize_song_text(artist)}"


#: 曲名・アーティスト名に使える最大文字数。
#:
#: ``core.script_generator.MAX_SONG_TITLE_LENGTH``（既定 40）と**同じ上限**。
#: 超えた曲名は正本gressiveとして読み上げられない。
MAX_TITLE_FIELD_LENGTH = 40

#: 曲名・アーティスト名に現れてはならない文字・並び。
#: ``script_generator._FORBIDDEN_TITLE_CHARS`` と引用符「」が該当。
#: 曲名は原稿で必ず「曲名」（歌手）」として埋め込まれるため、
#: 内側に「」があると読み上げ原稿の曲名一致率を抽出できない。
_FORBIDDEN_TITLE_SUBSTRINGS = ("\r", "\n", "\t", "\x00", "###", "\u300c", "\u300d")


def _title_field_problem(value: str) -> Optional[str]:
    """曲名・アーティスト名が読み上げ原稿に**入れられない**理由。

    ``None`` なら使える。文字列なら禁止理由（ログ用）。
    """
    text = value.strip()
    if not text:
        return "空文字"
    if len(text) > MAX_TITLE_FIELD_LENGTH:
        return f"{len(text)} 文字（上限 {MAX_TITLE_FIELD_LENGTH} 文字を超過）"
    for bad in _FORBIDDEN_TITLE_SUBSTRINGS:
        if bad in text:
            return f"禁止文字 {bad!r} を含む"
    if any(unicodedata.category(char) == "Cc" for char in text):
        return "制御文字を含む"
    return None


def _normalize(raw: Any, origin: str) -> Optional[Dict[str, Any]]:
    """1 レコードを正規化する。構造が壊れている場合は ``None``（読み飛ばし）。

    ここで落とすのは**構造**の問題だけ（必須キーの欠落・型の不正）。
    ``source`` 欠落や ``confidence`` の値は CI ゲート側の責務なので落とさない。
    """
    if not isinstance(raw, dict):
        logger.error("曲カタログ %s にオブジェクトでないレコードがあります", origin)
        return None

    record: Dict[str, Any] = dict(raw)
    record.setdefault("note_ja", "")
    record.setdefault("genre", "")

    missing = [
        field
        for field in REQUIRED_FIELDS
        if record.get(field) in (None, "")
    ]
    if missing:
        logger.error(
            "曲カタログ %s: レコード %r に必須フィールドがありません: %s",
            origin,
            record.get("id"),
            ", ".join(missing),
        )
        return None

    for numeric in ("release_year", "rank"):
        value = record.get(numeric)
        if isinstance(value, int):
            continue
        try:
            record[numeric] = int(value)
        except (TypeError, ValueError):
            logger.error(
                "曲カタログ %s: %s の %s が整数に変換できません: %r",
                origin,
                record.get("id"),
                numeric,
                value,
            )
            return None

    if record.get("confidence") not in ALLOWED_CONFIDENCE:
        logger.error(
            "曲カタログ %s: %s の confidence が不正です: %r",
            origin,
            record.get("id"),
            record.get("confidence"),
        )
        return None

    for field in ("title", "artist"):
        if not isinstance(record[field], str) or not record[field].strip():
            logger.error(
                "曲カタログ %s: %s の %s が空文字です", origin, record.get("id"), field
            )
            return None

    # 読み上げ原稿に入れられない曲名は**読み飛ばす**。
    #
    # これを書かないと、曲名 40 文字超（DJ ミックスや分裂盤のタイトルが
    # 実測で混ざっていた）や「」内包の曲が正本に入り、選曲しただけで
    # ``core.script_generator.SongTitleError`` が送出されて
    # ``/api/generate`` 全体が 500 になる（構造の不良が要求を壊す）。
    #
    # 構造不良と同じ扱い（ERROR ログを出して読み飛ばす）にして、
    # カタログの一部が壊れても放送は継続できるようにする。
    for field in ("title", "artist"):
        problem = _title_field_problem(record[field])
        if problem is not None:
            logger.error(
                "曲カタログ %s: %s の %s が読み上げ原稿に使えません（%s）: %r",
                origin, record.get("id"), field, problem, record[field],
            )
            return None

    return record


@lru_cache(maxsize=1)
def _load_cached() -> Tuple[Dict[str, Any], ...]:
    """正本 JSON を読み込んでタプルで返す（不変なのでキャッシュしてよい）。"""
    records: List[Dict[str, Any]] = []
    seen_ids: set = set()
    seen_keys: Dict[str, str] = {}

    for name in REGISTRY_FILES:
        path = SONGS_DIR / name
        if not path.exists():
            raise SongCatalogError(f"曲カタログの正本が見つかりません: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SongCatalogError(f"曲カタログを読み込めません: {path} ({exc})") from exc

        raw_records = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(raw_records, list):
            raise SongCatalogError(f"曲カタログの形式が不正です: {path}")

        for raw in raw_records:
            record = _normalize(raw, name)
            if record is None:
                continue
            if record["id"] in seen_ids:
                logger.error("曲カタログ %s: id %r が重複しています", name, record["id"])
                continue
            key = song_key(record["title"], record["artist"])
            if key in seen_keys:
                # 同じ曲が別 id で 2 回登録されると選曲時に「1 番組で 1 回」の
                # 保証が壊れるため、読み飛ばす。
                logger.error(
                    "曲カタログ %s: %s は %s と重複しています（id=%r）",
                    name,
                    record["title"],
                    seen_keys[key],
                    record["id"],
                )
                continue
            seen_ids.add(record["id"])
            seen_keys[key] = record["id"]
            records.append(record)

    return tuple(records)


#: 読み込み時の不足警告を 1 度だけ出すための印。
_WARNED_COVERAGE: set = set()


def _warn_if_thin() -> None:
    """正本の曲数が目標に届いていないことを、1 度だけ ``WARNING`` で出す。

    ログの出力順が不定でも 1 度で済むよう、年ごとの出済み印を持つ。
    """
    if _WARNED_COVERAGE:
        return
    coverage = year_coverage()
    if not coverage:
        return
    _WARNED_COVERAGE.update(coverage)
    thin = thin_years(TARGET_SONGS_PER_YEAR)
    if not thin:
        return
    logger.warning(
        "曲カタログが目標曲数に届いていません: 総 %d 曲 / %d 年、"
        "1 年 %d 曲未満の年が %d 個（目標 %d 曲）。"
        "1 番組（%d 曲）を重複ゼロで埋めるには隣接年・同一 10 年帯へ"
        "広げる必要があり、対象年以外の曲も流れます。"
        "core/songs/songs.json（編集は scripts/song_source/songs.tsv）を拡張してください",
        len(load_songs()),
        len(coverage),
        TARGET_SONGS_PER_YEAR,
        len(thin),
        TARGET_SONGS_PER_YEAR,
        PROGRAM_SONGS_PER_BROADCAST,
    )


def catalog_health(target: int = TARGET_SONGS_PER_YEAR) -> Dict[str, Any]:
    """カタログの実測サイズと目標との差分を返す（運用・監視用）。

    「1 年 50 曲」という契約と「実際に何曲あるか」を同じ場所で読める
    ようにする。UI・ヘルスチェック・テストが参照できる現状の読み取り口。

    Returns
    -------
    dict
        ``total`` / ``years`` / ``min_year`` / ``max_year`` /
        ``target`` / ``thin_years`` / ``missing_years`` /
        ``sufficient_years_for_program`` / ``ok`` を持つ。
    """
    coverage = year_coverage()
    span = sorted(coverage)
    missing = [
        year
        for year in range(span[0] - 1, span[-1] + 2)
        if year not in coverage
    ] if span else []
    thin = thin_years(target)
    sufficient = [
        year for year, count in coverage.items()
        if count >= PROGRAM_SONGS_PER_BROADCAST
    ]
    return {
        "total": len(load_songs()),
        "years": len(coverage),
        "min_year": span[0] if span else None,
        "max_year": span[-1] if span else None,
        "target": int(target),
        "program_songs": PROGRAM_SONGS_PER_BROADCAST,
        "thin_years": thin,
        "thin_year_count": len(thin),
        "missing_years": missing,
        "coverage": dict(coverage),
        "sufficient_years_for_program": sufficient,
        # 1 番組を「対象年の曲だけで」重複ゼロにできる年があるか。
        "ok": bool(sufficient),
    }


def load_songs() -> List[Dict[str, Any]]:
    """正本の全レコードをリストで返す。

    並び順は正本 JSON の並び順（年 → ``rank``）をそのまま保つ。

    Notes
    -----
    読み込み時に 1 度だけ、目標曲数に届いていないことを ``WARNING`` で出す
    （:func:`_warn_if_thin`）。
    """
    _warn_if_thin()
    return [dict(record) for record in _load_cached()]


@lru_cache(maxsize=1)
def _by_year_cached() -> Dict[int, Tuple[Dict[str, Any], ...]]:
    index: Dict[int, List[Dict[str, Any]]] = {}
    for record in _load_cached():
        index.setdefault(int(record["release_year"]), []).append(record)
    for year, bucket in index.items():
        bucket.sort(key=lambda r: (int(r["rank"]), r["id"]))
        index[year] = bucket
    return {year: tuple(bucket) for year, bucket in index.items()}


@lru_cache(maxsize=1)
def _by_id_cached() -> Dict[str, Dict[str, Any]]:
    return {record["id"]: record for record in _load_cached()}


@lru_cache(maxsize=1)
def _by_key_cached() -> Dict[str, str]:
    return {
        song_key(record["title"], record["artist"]): record["id"]
        for record in _load_cached()
    }


def songs_for_year(year: int, tolerance: int = 0) -> List[Dict[str, Any]]:
    """``year`` に出せる曲を「発表年が近い順・rank 順」で返す。

    Parameters
    ----------
    year:
        対象年。
    tolerance:
        発表年の許容差（±N 年）。``0`` なら厳密に ``release_year == year``。
        1 にすると隣接年の曲も候補に加わるが、**対象年の曲を先に全部返す**ため、
        対象年の分が十りていば連接年は混ざらない。

    Notes
    -----
    呼び出し側は「曲が足りなければ ``tolerance`` を広げる」ことを
    :func:`pool_for_year` に任せてもよい。直接 ``tolerance`` を渡すのは
    テストや検証で境界を明示したいとき。
    """
    tolerance = max(0, int(tolerance))
    if tolerance == 0:
        return [dict(record) for record in _by_year_cached().get(int(year), ())]

    exact: List[Dict[str, Any]] = []
    near: List[Tuple[int, Dict[str, Any]]] = []
    for candidate_year, bucket in _by_year_cached().items():
        delta = candidate_year - int(year)
        if delta == 0:
            exact.extend(dict(record) for record in bucket)
        elif abs(delta) <= tolerance:
            near.extend((abs(delta), dict(record)) for record in bucket)

    near.sort(key=lambda pair: (pair[0], int(pair[1]["rank"]), pair[1]["id"]))
    exact.sort(key=lambda record: (int(record["rank"]), record["id"]))
    return exact + [record for _delta, record in near]


def pool_for_year(year: int, wanted: int) -> List[Dict[str, Any]]:
    """選曲に必要な ``wanted`` 曲を満たす候補プールへ広げる。

    広げる順は「対象年 → ±1 年 → ±2 年 → 同じ 10 年の残りの年」。
    10 年へ広げると 1975 年の番組に 1971 年の曲が出るが、それは
    「間奏（音源なし）」よりはマシである上、最後段でしかない。

    **要求数を満たせなかったときは黙って足りない一件を返さない。**
    ``ERROR`` ログに要求数・実際の候補数・対象年の曲数を出して残す
    （1 曲も返さないと番組の曲スロットが埋まらないため、例外にはしない）。
    """
    wanted = max(1, int(wanted))
    for tolerance in (0, 1, 2):
        pool = songs_for_year(year, tolerance=tolerance)
        if len(pool) >= wanted:
            return pool

    if len(pool) < wanted:
        # 黙って短いプールを返すと、番組の中で同じ曲が繰り返される。
        # .Coordinator には差错が見えないので、少なくとも ERROR として残す
        # （1 曲も返さないと番組の曲スロットが埋まらないため、例外にはしない）。
        logger.error(
            "選曲プールが不足しています: year=%s 要求=%d 候補=%d（対象年の曲=%d 曲。"
            "隣接年へ広げても足りません）",
            int(year),
            wanted,
            len(pool),
            len(songs_for_year(int(year), tolerance=0)),
        )

    decade = (int(year) // 10) * 10
    pool = songs_for_year(year, tolerance=0)
    seen = {record["id"] for record in pool}
    for candidate_year in sorted(_by_year_cached()):
        if not (decade <= candidate_year <= decade + 9) or candidate_year == year:
            continue
        for record in _by_year_cached()[candidate_year]:
            if record["id"] in seen:
                continue
            seen.add(record["id"])
            pool.append(dict(record))

    if len(pool) < wanted:
        logger.error(
            "選曲プールが不足しています: year=%s 要求=%d 候補=%d"
            "（同じ 10 年帯へ広げても不足。番組内で同じ曲が繰り返されます）",
            int(year),
            wanted,
            len(pool),
        )

    return pool


def song_by_id(song_id: str) -> Optional[Dict[str, Any]]:
    """id から 1 レコードを引く（デバッグ・履歴の逆引き用）。"""
    record = _by_id_cached().get(song_id)
    return dict(record) if record else None


def song_id_for(title: str, artist: str) -> Optional[str]:
    """(曲名, アーティスト) → id。正本に無ければ ``None``。"""
    return _by_key_cached().get(song_key(title, artist))


def year_coverage() -> Dict[int, int]:
    """年ごとの曲数（検証・デバッグ用）。"""
    return {year: len(bucket) for year, bucket in sorted(_by_year_cached().items())}


def known_years() -> Sequence[int]:
    """1 件でも曲がある年（昇順）。"""
    return tuple(sorted(_by_year_cached()))


def thin_years(target: int = TARGET_SONGS_PER_YEAR) -> List[Tuple[int, int]]:
    """(年, 曲数) を「``target`` 未満の年」だけで返す（昇順）。"""
    return [(year, count) for year, count in year_coverage().items() if count < target]


def clear_cache() -> None:
    """正本を差し替えたあとにテストから呼ぶためのキャッシュ破棄。"""
    _load_cached.cache_clear()
    _by_year_cached.cache_clear()
    _by_id_cached.cache_clear()
    _by_key_cached.cache_clear()
    _WARNED_COVERAGE.clear()


__all__ = [
    "ALLOWED_CONFIDENCE",
    "PROGRAM_SONGS_PER_BROADCAST",
    "REGISTRY_FILES",
    "REQUIRED_FIELDS",
    "SONGS_DIR",
    "TARGET_SONGS_PER_YEAR",
    "SongCatalogError",
    "catalog_health",
    "clear_cache",
    "known_years",
    "load_songs",
    "normalize_song_text",
    "pool_for_year",
    "song_by_id",
    "song_id_for",
    "song_key",
    "songs_for_year",
    "thin_years",
    "year_coverage",
]
