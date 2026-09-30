"""``case_defs.json`` の読み込み（正本は ``build_cases.py``）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

#: 生成物 ``case_defs.json`` の置き場所。
CASE_DEFS_PATH = Path(__file__).resolve().parent / "case_defs.json"

#: ケース定義の必須フィールド。
REQUIRED_CASE_FIELDS = ("id", "year", "mode", "expected_facts", "expected_song_count", "expected_segments")


def load_cases(path: Path = None) -> List[Dict[str, Any]]:
    """評価ケースのリストを返す。

    Parameters
    ----------
    path:
        ``case_defs.json`` のパス。省略時は同ディレクトリにある生成物。

    Returns
    -------
    list[dict]
        各要素は ``{id, year, mode, expected_facts, expected_song_count, expected_segments}``。

    Raises
    ------
    FileNotFoundError
        生成物がない場合。``python -m eval.cases.build_cases`` で生成する。
    """
    target = Path(path) if path is not None else CASE_DEFS_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"評価ケースの生成物が見つかりません: {target}\n"
            "python -m eval.cases.build_cases で生成してください。"
        )
    payload = json.loads(target.read_text(encoding="utf-8"))
    cases = payload["cases"] if isinstance(payload, dict) else payload

    for case in cases:
        missing = [f for f in REQUIRED_CASE_FIELDS if f not in case]
        if missing:
            raise ValueError(f"ケース {case.get('id')} に必須フィールドがありません: {missing}")
    return cases


def load_case(case_id: str, path: Path = None) -> Dict[str, Any]:
    """ID で 1 ケースを引く。"""
    for case in load_cases(path):
        if case["id"] == case_id:
            return case
    raise KeyError(f"評価ケースが見つかりません: {case_id}")


__all__ = ["CASE_DEFS_PATH", "REQUIRED_CASE_FIELDS", "load_case", "load_cases"]
