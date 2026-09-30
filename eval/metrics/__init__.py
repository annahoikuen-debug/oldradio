"""評価ハーネスの集約（``run_case``）。

## 責務

- :func:`build_script` … ケースから台本を生成する（``generate_radio_script`` 経由）。
- :func:`run_case` … 1 ケースに 3 系統の metric を当てて :class:`CaseResult` を返す。
- :func:`run_all` … 全ケースを回して :class:`EvalReport` を返す。

## ゲート意識の設計

**warn と fail を混ぜない**。S1 の validator（``docs/facts_registry.md`` 5.3）が
決めた「番組表 = fail / 台本自由文 = warn」の線引きを、fact score の
``FactScoreResult.is_gate_ok`` がそのまま受け継ぐ。したがって:

- :attr:`CaseResult.failures` … ゲートを落とす違反だけ。
- :attr:`CaseResult.warnings` … 可視化だけしてゲートは落とさない違反。

現状のベースラインでは **``normal`` モード 8 ケースすべてに
未履行予告の違反が出る**（``core/fallback.py:341`` の既知の欠陥。
``core/fallback.py`` は S1/S3 所有なので**編集していない**）。
これは「失敗を隠さない」ための意図的な表示であり、
PR ゲートに載せるかは :mod:`eval.metrics.checklist` の
``unfulfilled_preannounce`` を**警告扱い**にするか**失敗扱い**にするかで決める
（``eval/README.md`` の CI ゲート表を参照）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - 直接実行時の保険
    sys.path.insert(0, str(_REPO_ROOT))

from ..cases.loader import load_cases
from .checklist import ChecklistResult, run_checklist
from .fact_score import DEFAULT_FACT_SCORE_THRESHOLD, FactScoreResult, fact_score
from .length import LengthResult, check_length, length_bounds
from .preannounce import detect_unfulfilled_preannounce

#: 3 モード。``generate_radio_script`` の ``mode`` 引数と一致させる。
MODES: Tuple[str, ...] = ("normal", "care_recreation", "anniversary")

#: ``anniversary`` モードで使用するお祝い対象者名（決定性のため固定）。
DEFAULT_TARGET_NAME = "花子"

#: ケースの生成に使う日付。
DEFAULT_MONTH = 5
DEFAULT_DAY = 15


def build_script(
    year: int,
    mode: str = "normal",
    *,
    month: int = DEFAULT_MONTH,
    day: int = DEFAULT_DAY,
    target_name: str = DEFAULT_TARGET_NAME,
) -> str:
    """ケースの台本を生成する。

    ``retro_radio.core.script_generator.generate_radio_script`` を使うため、
    Gemini キーが設定されていれば**LLM 経路**、空なら**フォールバック経路**
    のどちらでも同じ 1 行 through 評価できる。

    Parameters
    ----------
    year, mode, month, day, target_name:
        生成条件。

    Returns
    -------
    str
        生成された原稿。
    """
    from retro_radio.core.script_generator import generate_radio_script

    return generate_radio_script(year, month, day, mode=mode, target_name=target_name)


@dataclass(frozen=True)
class CaseResult:
    """1 ケースの評価結果。"""

    case_id: str
    year: int
    mode: str
    script: str
    fact: FactScoreResult
    checklist: ChecklistResult
    length: LengthResult
    preannounce: Tuple[str, ...] = ()

    # -- Shorthand ------------------------------------------------------------
    @property
    def failures(self) -> Tuple[str, ...]:
        """ゲートを落とす違反の一覧。"""
        problems: List[str] = []
        if not self.fact.is_gate_ok():
            problems.append(self.fact.describe())
            problems.extend(f"fact/{c.level}: {c.detail}" for c in self.fact.failures)
        if not self.length.ok:
            problems.append(f"length: {self.length.describe()}")
        problems.extend(f"checklist/{v.item}: {v.detail}" for v in self.checklist.violations)
        return tuple(problems)

    @property
    def warnings(self) -> Tuple[str, ...]:
        """可視化だけする違反（ゲートは落とさない）。"""
        problems: List[str] = [f"fact/warn: {c.detail}" for c in self.fact.warnings]
        problems.extend(f"era_words: {m}" for m in self.checklist.era_mentions)
        return tuple(problems)

    @property
    def passed(self) -> bool:
        """ゲートを通過したか。"""
        return not self.failures

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "year": self.year,
            "mode": self.mode,
            "fact_score": self.fact.score,
            "fact_supported": self.fact.supported,
            "fact_checkable": self.fact.checkable,
            "fact_failures": [c.detail for c in self.fact.failures],
            "fact_warnings": [c.detail for c in self.fact.warnings],
            "coverage": round(self.fact.coverage, 2),
            "checklist_violations": [str(v) for v in self.checklist.violations],
            "japanese_ratio": round(self.checklist.japanese_ratio, 4),
            "length": self.length.length,
            "length_lower": self.length.lower,
            "length_upper": self.length.upper,
            "length_ok": self.length.ok,
            "song_match_rate": (
                round(self.checklist.song_match.rate, 4) if self.checklist.song_match else None
            ),
            "preannounce": list(self.preannounce),
            "passed": self.passed,
        }

    def describe(self) -> str:
        return (
            f"{self.case_id}: {self.fact.describe()} / {self.checklist.describe()} / "
            f"length={self.length.describe()}"
        )


@dataclass(frozen=True)
class EvalReport:
    """全ケースの集計。"""

    results: Tuple[CaseResult, ...] = ()
    threshold: float = DEFAULT_FACT_SCORE_THRESHOLD

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def mean_fact_score(self) -> float:
        if not self.results:
            return 0.0
        return round(statistics.mean(r.fact.score for r in self.results), 2)

    @property
    def min_fact_score(self) -> float:
        if not self.results:
            return 0.0
        return round(min(r.fact.score for r in self.results), 2)

    @property
    def checklist_violation_total(self) -> int:
        return sum(len(r.checklist.violations) for r in self.results)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "threshold": self.threshold,
            "length_bounds": list(length_bounds()),
            "cases": [r.to_dict() for r in self.results],
            "summary": {
                "total": len(self.results),
                "passed": self.passed_count,
                "failed": self.failed_count,
                "mean_fact_score": self.mean_fact_score,
                "min_fact_score": self.min_fact_score,
                "checklist_violation_total": self.checklist_violation_total,
            },
        }


def run_case(
    case: Dict[str, Any],
    *,
    script: Optional[str] = None,
    month: int = DEFAULT_MONTH,
    day: int = DEFAULT_DAY,
    target_name: str = DEFAULT_TARGET_NAME,
) -> CaseResult:
    """1 ケースを評価する。

    Parameters
    ----------
    case:
        ケース定義（``{id, year, mode, expected_facts, expected_song_count, expected_segments}``）。
        ``eval.cases.load_cases()`` の要素をそのまま渡せる。
    script:
        評価対象の原稿。省略時は :func:`build_script` で生成する
        （CI では API キー未設定なので必ずフォールバック経路になる）。
    month, day, target_name:
        原稿生成の条件。

    Returns
    -------
    CaseResult
    """
    year = int(case["year"])
    mode = str(case["mode"])
    text = build_script(year, mode, month=month, day=day, target_name=target_name) if script is None else script

    return CaseResult(
        case_id=str(case["id"]),
        year=year,
        mode=mode,
        script=text,
        fact=fact_score(text, year),
        checklist=run_checklist(text, year),
        length=check_length(text),
        preannounce=tuple(detect_unfulfilled_preannounce(text)),
    )


def run_all(
    cases: Optional[Iterable[Dict[str, Any]]] = None,
    *,
    threshold: float = DEFAULT_FACT_SCORE_THRESHOLD,
    month: int = DEFAULT_MONTH,
    day: int = DEFAULT_DAY,
) -> EvalReport:
    """全ケースを評価する。

    Parameters
    ----------
    cases:
        ケース定義の列。省略時は ``eval/cases/case_defs.json`` の 24 ケース。
    threshold:
        fact score のゲート下限（報告にのみ使う。判定は
        :meth:`FactScoreResult.is_gate_ok` 側で行う）。

    Returns
    -------
    EvalReport
    """
    selected = list(cases) if cases is not None else load_cases()
    results = tuple(run_case(case, month=month, day=day) for case in selected)
    return EvalReport(results=results, threshold=threshold)


def main(argv: Optional[Sequence[str]] = None) -> int:  # noqa: C901 - CLI の入口
    parser = argparse.ArgumentParser(description="評価ハーネスの実行（提案⑨）")
    parser.add_argument("--json", action="store_true", help="機械向け JSON で出力する")
    parser.add_argument("--out", default=None, help="JSON の書き出し先")
    parser.add_argument("--verbose", action="store_true", help="違反を 1 件ずつ表示する")
    args = parser.parse_args(argv)

    report = run_all()

    if args.json or args.out:
        body = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(body + "\n", encoding="utf-8")
            print(f"書き出しました: {args.out}")
        else:
            sys.stdout.write(body)
            sys.stdout.write("\n")
    else:
        for result in report.results:
            flag = "OK  " if result.passed else "NG  "
            print(f"[{flag}] {result.describe()}")
            if args.verbose:
                for problem in result.failures:
                    print(f"        ! {problem}")
                for problem in result.warnings:
                    print(f"        ? {problem}")

    print()
    print(
        f"合計 {len(report.results)} ケース / 合格 {report.passed_count} / "
        f"不合格 {report.failed_count} / fact score 平均 {report.mean_fact_score} "
        f"（最小 {report.min_fact_score}）"
    )
    print(f"文字数の許容帯: {length_bounds()}")
    return 0 if report.failed_count == 0 else 1


__all__ = [
    "DEFAULT_DAY",
    "DEFAULT_MONTH",
    "DEFAULT_TARGET_NAME",
    "MODES",
    "CaseResult",
    "EvalReport",
    "build_script",
    "run_all",
    "run_case",
]

if __name__ == "__main__":  # pragma: no cover - 手動実行
    raise SystemExit(main())
