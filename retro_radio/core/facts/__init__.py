"""事実レジストリ（正本）のローダ（提案⑦）。

このアプリは「記憶の内容」を扱うため、誤った事実が一度読み上げられると
集団の中で反復再生され、自己伝記記憶に統合される。したがって事実の
管理は表示上のバグではなく安全要件として扱う
（``plans/evidence_based_improvement_proposals.md`` 324〜390 行）。

本パッケージの約束:

1. **1 事実 1 レコード**。正本は ``facts/*.json`` にあり、``core/fallback.py`` の
   ``_hist(...)`` 直書きではなくこのレジストリを参照する。
2. **台本・番組表に出してよい事実は、対象年に対して**
   ``valid_from <= year <= valid_to`` **を満たすレコードに限る**。
   10 年バケットによる丸め（``year // 10 * 10``）だけを唯一の解決ルールにしない。
3. **``source`` は必須**。``null`` は CI ゲート（``scripts/validate_facts.py``）で
   fail する。ただし本タスクはネットワークアクセスができないため、**URL を推測で
   書かない**。確実に書けないものは文献名のみを ``source`` に置き、
   ``confidence: "unverified"`` を立てる。

外部依存はこのモジュールに持たせない（``ProgramSchedule`` の生成は
``core/fallback.py`` 側の責務）。標準ライブラリだけを使う。
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# レジストリの置き場所（このファイルと同じディレクトリ）
FACTS_DIR = Path(__file__).resolve().parent

# 正本ファイル。増分したときは必ずこの一覧にも足す。
REGISTRY_FILES = ("programs.json",)

# レコードに必ず存在しなければならないフィールド。
# ``start_time`` / ``duration_min`` / ``description_ja`` は ``ProgramSchedule`` と
# 読み上げ原稿を組み立てるために必要なので必須にする。
REQUIRED_FIELDS = (
    "id",
    "kind",
    "title",
    "network",
    "start_time",
    "valid_from",
    "valid_to",
    "duration_min",
    "claim_ja",
    "description_ja",
    "source",
    "confidence",
)

# 取り違えを防ぐための許可値。
ALLOWED_KINDS = ("tv_program", "radio_program")
ALLOWED_CONFIDENCE = ("verified", "unverified")

_FACT_KINDS = set(ALLOWED_KINDS)

# 表示・読み上げテキストから 4 桁の西暦を取り出すためのパターン（既存と同一）
_YEAR_IN_TEXT = re.compile(r"(1[5-9]\d{2}|20\d{2})")

# 「○年代」の表記パターン。4 桁の西暦の検索とは別の取りこぼしなので、
# **年代バケットの閾値**として別に評価する。
_DECADE_IN_TEXT = re.compile(r"(1[89]\d\d|20\d{2})\s*年代")


class FactRegistryError(RuntimeError):
    """正本 JSON が読み込めないときに送出する。"""


def _normalize(raw: Dict[str, Any], origin: str) -> Optional[Dict[str, Any]]:
    """1 レコードを正規化する。構造が壊れている場合は ``None``（読み飛ばし）。

    ここで落とすのは**構造**の問題だけ（必須キーの欠落・型の不正）。
    ``source`` の有無や ``confidence`` の値-door は CI ゲート側の責務なので、
    ローダでは落とさない。
    """
    if not isinstance(raw, dict):
        logger.error("事実レジストリ %s にオブジェクトでないレコードがあります", origin)
        return None

    record = dict(raw)
    record.setdefault("valid_to", None)
    record.setdefault("source_url", None)
    record.setdefault("note_ja", "")
    record.setdefault("description_ja", record.get("claim_ja") or "")

    # ``valid_to`` だけは ``None``（= 継続中）が正当な値なので必須扱いしない。
    missing = [
        f
        for f in REQUIRED_FIELDS
        if f != "valid_to" and not record.get(f) and record.get(f) != 0
    ]
    if missing:
        logger.error(
            "事実レジストリ %s: レコード %r に必須フィールドがありません: %s",
            origin,
            record.get("id"),
            ", ".join(missing),
        )
        return None

    for numeric in ("valid_from", "valid_to", "duration_min"):
        value = record.get(numeric)
        if value is None or isinstance(value, int):
            continue
        try:
            record[numeric] = int(value)
        except (TypeError, ValueError):
            logger.error(
                "事実レジストリ %s: %s の %s が整数に変換できません: %r",
                origin,
                record.get("id"),
                numeric,
                value,
            )
            return None

    valid_from = record.get("valid_from")
    valid_to = record.get("valid_to")
    if not isinstance(valid_from, int):
        logger.error(
            "事実レジストリ %s: %s の valid_from が整数ではありません", origin, record.get("id")
        )
        return None
    if valid_to is not None and not isinstance(valid_to, int):
        logger.error(
            "事実レジストリ %s: %s の valid_to が整数でも None でもありません",
            origin,
            record.get("id"),
        )
        return None
    if isinstance(valid_to, int) and valid_to < valid_from:
        logger.error(
            "事実レジストリ %s: %s の valid_to(%d) が valid_from(%d) より小さい",
            origin,
            record.get("id"),
            valid_to,
            valid_from,
        )
        return None

    if record.get("kind") not in _FACT_KINDS:
        logger.error(
            "事実レジストリ %s: %s の kind が不正です: %r",
            origin,
            record.get("id"),
            record.get("kind"),
        )
        return None

    return record


@lru_cache(maxsize=1)
def _load_cached() -> tuple:
    """正本 JSON を読み込んでタプルで返す（不変なのでキャッシュしてよい）。"""
    records: List[Dict[str, Any]] = []
    seen_ids: set = set()

    for name in REGISTRY_FILES:
        path = FACTS_DIR / name
        if not path.exists():
            raise FactRegistryError(f"事実レジストリの正本が見つかりません: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # 読取不能 / JSON 壊れ
            raise FactRegistryError(f"事実レジストリを読み込めません: {path} ({exc})") from exc

        raw_records = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(raw_records, list):
            raise FactRegistryError(f"事実レジストリの形式が不正です: {path}")

        for raw in raw_records:
            record = _normalize(raw, name)
            if record is None:
                continue
            if record["id"] in seen_ids:
                # 重複 id は CI ゲートでも検出するが、応用も安全側（最初の 1 件）に倒す。
                logger.error(
                    "事実レジストリ %s: id %r が重複しています（2 件目以降は無視）", name, record["id"]
                )
                continue
            seen_ids.add(record["id"])
            records.append(record)

    return tuple(records)


def load_facts() -> List[Dict[str, Any]]:
    """正本の全レコードをリストで返す。

    台本・番組表に出してよい事実の集合はここからだけ取得する。
    """
    return [dict(record) for record in _load_cached()]


def facts_valid_for(year: int) -> List[Dict[str, Any]]:
    """``year`` に対して有効な（``valid_from <= year <= valid_to``）事実を返す。

    ``valid_to`` が ``null`` は「終了年未確定（継続中）」を意味する。
    並び順は正本 JSON の並び順をそのまま保ち、決定性を保証する。
    """
    valid: List[Dict[str, Any]] = []
    for record in _load_cached():
        if year < record["valid_from"]:
            continue
        valid_to = record.get("valid_to")
        if valid_to is not None and year > valid_to:
            continue
        valid.append(dict(record))
    return valid


def programs_for_year(year: int) -> List[Dict[str, Any]]:
    """``year`` に対して有効なテレビ/ラジオ番組の事実を返す。"""
    return [r for r in facts_valid_for(year) if r["kind"] in _FACT_KINDS]


def resolve_program(year: int, index: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """``year`` に対して有効な番組のうち ``index`` 番目を返す。

    ``index`` 省略時は ``year`` 自身を使う。旧 ``_historical_pick`` の
    ``eligible[year % len(eligible)]``（年によって決定的に回転させる）と同じ契約で、
    **同じ入力なら常に同じ出力**を保つ。候補が無いときは ``None``。
    """
    candidates = programs_for_year(year)
    if not candidates:
        return None
    slot = year if index is None else index
    return candidates[slot % len(candidates)]


def fact_by_id(fact_id: str) -> Optional[Dict[str, Any]]:
    """id から事実を引く（出典表示・デバッグ用）。"""
    for record in _load_cached():
        if record["id"] == fact_id:
            return dict(record)
    return None


def future_year_mentions(text: str, year: int) -> List[str]:
    """テキスト中の「``year`` より後」の年の記載を返す。

    **4 桁の西暦と「○年代」の両方**を見る。既存の実装は 4 桁の西暦しか見て
    いなかったため、「2020年代」のような年代表記をすり抜ける穴があった。
    年代表記は **年代バケットの閾値**（その年代の先頭年。2020年代なら 2020）と
    比較する。よって ``year=2015`` の原稿に「2020年代」とあるのは violation である。
    """
    mentions: List[str] = []

    for token in _YEAR_IN_TEXT.findall(text or ""):
        value = int(token)
        if value > year:
            mentions.append(f"{value}年")

    for token in _DECADE_IN_TEXT.findall(text or ""):
        decade_start = int(token)
        if decade_start > year:
            mentions.append(f"{decade_start}年代")

    return mentions


__all__ = [
    "ALLOWED_CONFIDENCE",
    "ALLOWED_KINDS",
    "FACTS_DIR",
    "REGISTRY_FILES",
    "REQUIRED_FIELDS",
    "FactRegistryError",
    "fact_by_id",
    "facts_valid_for",
    "future_year_mentions",
    "load_facts",
    "programs_for_year",
    "resolve_program",
]
