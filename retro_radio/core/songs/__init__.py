"""曲カタログ（正本）のローダ。

`core/facts/` と同じ方針で、**このアプリが流せる曲の正本は
`core/songs/songs.json` 1 ファイル**に置く。ここに無い曲はこのアプリでは
選曲しない。

なぜ正本化するのか
------------------
1 回の番組で 6 曲（既定 3 周なら 18 曲）流すため、1 年あたり 50 曲程度の
プールが無いと「同じ曲が繰り回される」ことになる。旧実装の
`FALLBACK_SONGS` は 9 バケット × 4 曲 = **全 36 曲**しか無く、76 年に対して
曲不足が構造的だった。

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

# 選曲プールとして見てよい 1 年あたりの目標曲数。
# 1 番組（既定 3 周 × 1 パス 6 曲 = 18 曲）を重複ゼロで埋めるのに、
# これより小さければ迟早「前の曲を繰り返す」ことになる。
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


def load_songs() -> List[Dict[str, Any]]:
    """正本の全レコードをリストで返す。

    並び順は正本 JSON の並び順（年 → ``rank``）をそのまま保つ。
    """
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
    """
    wanted = max(1, int(wanted))
    for tolerance in (0, 1, 2):
        pool = songs_for_year(year, tolerance=tolerance)
        if len(pool) >= wanted:
            return pool

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


__all__ = [
    "ALLOWED_CONFIDENCE",
    "REGISTRY_FILES",
    "REQUIRED_FIELDS",
    "SONGS_DIR",
    "TARGET_SONGS_PER_YEAR",
    "SongCatalogError",
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
