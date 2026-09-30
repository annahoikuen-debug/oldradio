"""``case_defs.json`` の生成（正本は ``retro_radio/core/facts/programs.json``）。

**ハードコードしない**という原則を守るために、このスクリプトは
``scripts.validate_facts.build_fact_table(year)``（S1 の公開 API）を呼んで
``expected_facts`` を導出する。台本・番組表に出してよい事実は
レジストリだけが決める（``docs/facts_registry.md`` 第 2 章）ため、
評価ケースの正解データも**同じ供給点**から取る。

``expected_segments`` は ``retro_radio/core/script_generator.py`` の
``_build_segmented_prompt``（S1/S3 所有・**読み取りのみ**）から導出する。
プロンプトを変えれば自動追従するため、S3 がセグメント構造を変えれば
ケース定義を再生成するだけでよい。

## 使い方

```bash
python -m eval.cases.build_cases            # case_defs.json を生成（上書き）
python -m eval.cases.build_cases --check    # 再生成しても差分ゼロかを検査
python -m eval.cases.build_cases --print    # 標準出力に JSON を出す
```

``--check`` は ``tests/test_eval_harness.py`` から使う（CI が
「正本と生成物の乖離」を検出する）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - 直接実行時の保険
    sys.path.insert(0, str(_REPO_ROOT))

from retro_radio.config import get_settings
from retro_radio.core.script_generator import _build_segmented_prompt
from scripts.validate_facts import build_fact_table

from .loader import CASE_DEFS_PATH, REQUIRED_CASE_FIELDS

#: 層別抽出する 8 年（提案 465 行）。各 10 年バケットの代表＋両端。
SAMPLED_YEARS: Sequence[int] = (1950, 1964, 1975, 1985, 1995, 2005, 2015, 2025)

#: 3 モード。順序は結果を安定させるため固定する。
MODES: Sequence[str] = ("normal", "care_recreation", "anniversary")

#: ケース定義に使う日付（決定性のため固定）。
#: 実際の放送日に意味はないが、**、台本に「5月15日」が入る**ので
#: 季節が内容と矛盾しない 5 月を採る。
CASE_MONTH = 5
CASE_DAY = 15

#: 生成物のスキーマ版
SCHEMA_VERSION = 1


def expected_segments(year: int, mode: str) -> List[str]:
    """その年・そのモードで期待される ``###`` 見出し名を導出する。

    ``_build_segmented_prompt``（S1/S3 所有・読み取りのみ）を唯一の供給点とする。
    取得に失敗した場合は**空リスト**を返し、テスト側で検出できるようにする
    （黙って既定値を返すより、壊れていることを見えるようにする）。
    """
    try:
        prompt = _build_segmented_prompt(year, CASE_MONTH, CASE_DAY, mode=mode)
    except Exception:  # pragma: no cover - プロンプト構築の破綻時の証拠
        return []
    return [
        line[4:].strip() for line in prompt.splitlines() if line.startswith("### ")
    ]


def build_case(year: int, mode: str) -> Dict[str, Any]:
    """1 ケースの定義を作る。

    Parameters
    ----------
    year:
        対象年。
    mode:
        ``normal`` / ``care_recreation`` / ``anniversary``。

    Returns
    -------
    dict
        ``{id, year, mode, expected_facts, expected_song_count, expected_segments}``。
    """
    table = build_fact_table(year)
    return {
        "id": f"{mode}-{year}",
        "year": int(year),
        "mode": mode,
        # 正本どおりの並び順を保つ（``build_fact_table`` の並びは決定的）。
        "expected_facts": [str(record["id"]) for record in table],
        "expected_song_count": int(get_settings().program_min_song_count),
        "expected_segments": expected_segments(year, mode),
    }


def build_all(
    years: Optional[Sequence[int]] = None, modes: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """全ケースの定義オブジェクトを返す（``case_defs.json`` の雛形）。"""
    selected_years = list(years) if years is not None else list(SAMPLED_YEARS)
    selected_modes = list(modes) if modes is not None else list(MODES)

    cases = [build_case(year, mode) for mode in selected_modes for year in selected_years]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "python -m eval.cases.build_cases",
        "note_ja": (
            "生成物。正本は retro_radio/core/facts/programs.json（S1 所有）と "
            "retro_radio/core/script_generator.py::_build_segmented_prompt。"
            "手で編集しないこと（--check で乖離を検出する）。"
        ),
        "sampled_years": selected_years,
        "modes": selected_modes,
        "case_date": {"month": CASE_MONTH, "day": CASE_DAY},
        "cases": cases,
    }


def render(payload: Dict[str, Any]) -> str:
    """``case_defs.json`` の本文（改行は LF、末尾改行あり）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="評価ケースの生成")
    parser.add_argument("--check", action="store_true", help="再生成しても差分ゼロか検査する")
    parser.add_argument("--print", dest="to_stdout", action="store_true", help="標準出力に出す")
    parser.add_argument("--out", default=str(CASE_DEFS_PATH), help="出力先")
    args = parser.parse_args(argv)

    payload = build_all()
    body = render(payload)

    for case in payload["cases"]:
        missing = [f for f in REQUIRED_CASE_FIELDS if f not in case]
        if missing:
            print(f"ERROR: ケース {case.get('id')} に必須フィールドがありません: {missing}")
            return 1

    if args.to_stdout:
        sys.stdout.write(body)
        return 0

    target = Path(args.out)
    if args.check:
        if not target.exists():
            print(f"FAIL: {target} がありません。先に生成してください。")
            return 1
        current = target.read_text(encoding="utf-8")
        if current != body:
            print(f"FAIL: {target} は正本と乖離しています。`python -m eval.cases.build_cases` を実行。")
            return 1
        print(f"OK: {target} は正本と一致（{len(payload['cases'])} ケース）")
        return 0

    target.write_text(body, encoding="utf-8")
    print(f"書き出しました: {target}（{len(payload['cases'])} ケース）")
    return 0


__all__ = [
    "CASE_DAY",
    "CASE_MONTH",
    "MODES",
    "SAMPLED_YEARS",
    "SCHEMA_VERSION",
    "build_all",
    "build_case",
    "expected_segments",
    "render",
]

if __name__ == "__main__":  # pragma: no cover - 手動実行
    raise SystemExit(main())
