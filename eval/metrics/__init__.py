"""評価ハーネスの集約（``run_case`` / ``run_all``）。

## 責務

- :func:`build_script` … ケースから台本を生成する。
- :func:`run_case` … 1 ケースに 3 系統の metric を当てて :class:`CaseResult` を返す。
- :func:`run_all` … 全ケースを回して :class:`EvalReport` を返す。

## 契約 1: 「報告する閾値」と「強制する閾値」は同じ値

:meth:`CaseResult.passed` は **自分自身の :attr:`CaseResult.threshold`** で判定する。
:func:`run_all` が受け取った ``threshold`` は :class:`CaseResult` までそのまま
伝播し、:class:`EvalReport` の ``threshold`` と同じオブジェクトに由来する。
したがって「閾値 95 と報告しながら 80 の基準で合格させる」ような不一致は
構造的に起こりえない。

## 契約 2: 既定は閉路（hermetic）

台本の供給源は :mod:`eval.fixtures` が決める。既定の ``deterministic`` は
ネットワークを一切触らない。実 API を叩く ``gemini`` 経路は**明示指定**が必要で、
その場合は :attr:`EvalReport.hermetic` が ``False`` になる（報告書に載る）。

## 契約 3: 測れないものは「測れない」と書く

空の原稿・曲への言及 0 件のように**指標が定義できない**状況では、
黙って満点（1.0 / 100.0）ではなく :attr:`CaseResult.not_applicable` に列挙する。

## ゲート意識の設計

**warn と fail を混ぜない**。S1 の validator（``docs/facts_registry.md`` 5.3）が
決めた「番組表 = fail / 台本自由文 = warn」の線引きを、fact score の
``FactScoreResult.is_gate_ok`` がそのまま受け継ぐ。したがって:

- :attr:`CaseResult.failures` … ゲートを落とす違反だけ。
- :attr:`CaseResult.warnings` … 可視化だけしてゲートは落とさない違反。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - 直接実行時の保険
    sys.path.insert(0, str(_REPO_ROOT))

from ..cases.loader import load_cases
from ..fixtures import (
    DEFAULT_SCRIPT_SOURCE,
    NON_HERMETIC_SOURCE,
    SCRIPT_SOURCES,
    ScriptSourceError,
    build_script_for_source,
    load_fixture_scripts,
    resolve_fixture_path,
    resolve_source,
)
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
    source: str = DEFAULT_SCRIPT_SOURCE,
) -> str:
    """ケースの台本を生成する。

    Parameters
    ----------
    year, mode, month, day, target_name:
        生成条件。
    source:
        台本の供給源。``"deterministic"``（既定・ネットワーク不可）/
        ``"fixtures"``（ファイルを直接渡すので本関数では未対応）/
        ``"gemini"``（実サービス・**非決定的**）。詳細は :mod:`eval.fixtures`。

    Returns
    -------
    str
        生成された原稿。

    Raises
    ------
    ScriptSourceError
        ``"fixtures"`` を指定した場合（本関数はファイルを扱わない）。
    """
    return build_script_for_source(
        source, year, mode, month=month, day=day, target_name=target_name
    )


@dataclass(frozen=True)
class CaseResult:
    """1 ケースの評価結果。

    Attributes
    ----------
    threshold:
        この結果が**実際に判定に使う**fact score の下限。
        :attr:`passed` はこの値を参照する。``None`` は
        :data:`~eval.metrics.fact_score.DEFAULT_FACT_SCORE_THRESHOLD` 相当。
    """

    case_id: str
    year: int
    mode: str
    script: str
    fact: FactScoreResult
    checklist: ChecklistResult
    length: LengthResult
    preannounce: Tuple[str, ...] = ()
    threshold: float = DEFAULT_FACT_SCORE_THRESHOLD
    script_source: str = DEFAULT_SCRIPT_SOURCE

    # -- Shorthand ------------------------------------------------------------
    @property
    def failures(self) -> Tuple[str, ...]:
        """ゲートを落とす違反の一覧。

        fact の判定には **:attr:`threshold`**（報告値そのもの）を使う。
        """
        problems: List[str] = []
        if not self.fact.is_gate_ok(self.threshold):
            problems.append(
                f"{self.fact.describe()}（閾値 {self.threshold:g}）"
            )
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
    def not_applicable(self) -> Tuple[str, ...]:
        """**測れなかった**指標の一覧（ゲートは落とさないが、隠さない）。

        黙って満点にする代わりにここへ出す。該当例:

        - ``fact_coverage`` … 正解テーブルが空（対象年の番組が 1 つも無い）。
        - ``song_match`` … 台本に曲名の主張が 0 件。
        - ``japanese_ratio`` … 分母になる文字が 0 個（原稿が空）。
        """
        items: List[str] = []
        if not self.fact.expected_facts:
            items.append("fact_coverage: 正解テーブルが空（対象年の番組が 1 つも無い）")
        if not self.fact.applicable:
            items.append("fact_score: 検証可能な原子的事実が 0 件（score=0.0 は『未検証』を意味する）")
        if self.checklist.song_match is None or not self.checklist.song_match.applicable:
            items.append("song_match: 曲名の主張が 0 件（一致率は定義しない）")
        if not self.checklist.japanese_ratio_applicable:
            items.append("japanese_ratio: 言語の手がかりになる文字が 0 個（原稿が空）")
        return tuple(items)

    @property
    def passed(self) -> bool:
        """ゲートを通過したか。**報告した閾値そのものを強制する**。"""
        return not self.failures

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "year": self.year,
            "mode": self.mode,
            "script_source": self.script_source,
            "threshold": self.threshold,
            "fact_score": self.fact.score,
            "fact_applicable": self.fact.applicable,
            "fact_supported": self.fact.supported,
            "fact_checkable": self.fact.checkable,
            "fact_failures": [c.detail for c in self.fact.failures],
            "fact_warnings": [c.detail for c in self.fact.warnings],
            "coverage": round(self.fact.coverage, 2),
            "coverage_applicable": self.fact.coverage_applicable,
            "checklist_violations": [str(v) for v in self.checklist.violations],
            "japanese_ratio": round(self.checklist.japanese_ratio, 4),
            "japanese_ratio_applicable": self.checklist.japanese_ratio_applicable,
            "length": self.length.length,
            "length_lower": self.length.lower,
            "length_upper": self.length.upper,
            "length_ok": self.length.ok,
            "song_match_rate": (
                round(self.checklist.song_match.rate, 4) if self.checklist.song_match else None
            ),
            "song_match_applicable": bool(
                self.checklist.song_match and self.checklist.song_match.applicable
            ),
            "preannounce": list(self.preannounce),
            "not_applicable": list(self.not_applicable),
            "failures": list(self.failures),
            "passed": self.passed,
        }

    def describe(self) -> str:
        return (
            f"{self.case_id}: {self.fact.describe()}（閾値 {self.threshold:g}） / "
            f"{self.checklist.describe()} / length={self.length.describe()}"
        )


@dataclass(frozen=True)
class EvalReport:
    """全ケースの集計。

    Attributes
    ----------
    threshold:
        ゲートに使う fact score の下限。**全 :class:`CaseResult` と同一の値**。
    script_source:
        台本の供給源。
    hermetic:
        ネットワークに依存しない実行だったか。``False`` なら
        ``script_source == "gemini"``（非決定的）。
    """

    results: Tuple[CaseResult, ...] = ()
    threshold: float = DEFAULT_FACT_SCORE_THRESHOLD
    script_source: str = DEFAULT_SCRIPT_SOURCE

    @property
    def hermetic(self) -> bool:
        """ネットワークに依存せず再現可能な実行だったか。"""
        return self.script_source != NON_HERMETIC_SOURCE

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

    @property
    def not_applicable_total(self) -> int:
        return sum(len(r.not_applicable) for r in self.results)

    @property
    def gate_ok(self) -> bool:
        """CI ゲート判定。全ケースが合格なら ``True``。"""
        return self.failed_count == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "threshold": self.threshold,
            "script_source": self.script_source,
            "hermetic": self.hermetic,
            "length_bounds": list(length_bounds()),
            "cases": [r.to_dict() for r in self.results],
            "summary": {
                "total": len(self.results),
                "passed": self.passed_count,
                "failed": self.failed_count,
                "gate_ok": self.gate_ok,
                "mean_fact_score": self.mean_fact_score,
                "min_fact_score": self.min_fact_score,
                "checklist_violation_total": self.checklist_violation_total,
                "not_applicable_total": self.not_applicable_total,
            },
        }


def run_case(
    case: Dict[str, Any],
    *,
    script: Optional[str] = None,
    month: int = DEFAULT_MONTH,
    day: int = DEFAULT_DAY,
    target_name: str = DEFAULT_TARGET_NAME,
    threshold: float = DEFAULT_FACT_SCORE_THRESHOLD,
    script_source: str = DEFAULT_SCRIPT_SOURCE,
) -> CaseResult:
    """1 ケースを評価する。

    Parameters
    ----------
    case:
        ケース定義（``{id, year, mode, expected_facts, expected_song_count, expected_segments}``）。
        ``eval.cases.load_cases()`` の要素をそのまま渡せる。
    script:
        評価対象の原稿。省略時は :func:`build_script` で生成する。
    month, day, target_name:
        原稿生成の条件。
    threshold:
        fact score のゲート下限。**そのまま :class:`CaseResult` に入り、
        判定に使われる**（報告値と強制値が一致する）。
    script_source:
        ``script`` を省略したときだけ効く台本の供給源。

    Returns
    -------
    CaseResult
    """
    year = int(case["year"])
    mode = str(case["mode"])
    text = (
        script
        if script is not None
        else build_script(
            year, mode, month=month, day=day, target_name=target_name, source=script_source
        )
    )

    return CaseResult(
        case_id=str(case["id"]),
        year=year,
        mode=mode,
        script=text,
        fact=fact_score(text, year),
        checklist=run_checklist(text, year),
        length=check_length(text),
        preannounce=tuple(detect_unfulfilled_preannounce(text)),
        threshold=threshold,
        script_source=script_source,
    )


def run_all(
    cases: Optional[Iterable[Dict[str, Any]]] = None,
    *,
    threshold: float = DEFAULT_FACT_SCORE_THRESHOLD,
    month: int = DEFAULT_MONTH,
    day: int = DEFAULT_DAY,
    target_name: str = DEFAULT_TARGET_NAME,
    script_source: Optional[str] = None,
    scripts: Optional[Mapping[str, str]] = None,
    fixtures: Optional[Path] = None,
) -> EvalReport:
    """全ケースを評価する。

    Parameters
    ----------
    cases:
        ケース定義の列。省略時は ``eval/cases/case_defs.json`` の 24 ケース。
    threshold:
        fact score のゲート下限。**報告と判定の両方に使われる**（一致させる）。
    month, day, target_name:
        原稿生成の条件。
    script_source:
        台本の供給源。省略時は :func:`eval.fixtures.resolve_source` に委ねる
        （既定 ``deterministic`` = **ネットワーク不可**）。
    scripts:
        ``{ケースID: 原稿}``。渡したケースはこれで評価する（生成しない）。
        ``script_source="fixtures"`` と同じ用途。
    fixtures:
        fixture JSON のパス。``script_source="fixtures"`` かつ未指定のときに使う。

    Returns
    -------
    EvalReport

    Raises
    ------
    ScriptSourceError
        供給源の指定が不正、または fixture が読めない場合。

    Notes
    -----
    ``threshold`` は :class:`CaseResult` に伝播し、
    :meth:`CaseResult.passed` がそれを参照する。**別物の既定値で判定することは
    ない**（これが本関数の中心的な契約）。
    """
    selected = list(cases) if cases is not None else load_cases()
    source = resolve_source(script_source)

    injected: Dict[str, str] = {}
    if source == "fixtures":
        payload = scripts or load_fixture_scripts(resolve_fixture_path(fixtures))
        injected = dict(payload)
    elif scripts:
        injected = dict(scripts)

    results = tuple(
        run_case(
            case,
            script=injected.get(str(case["id"])),
            month=month,
            day=day,
            target_name=target_name,
            threshold=threshold,
            script_source=source,
        )
        for case in selected
    )
    return EvalReport(results=results, threshold=threshold, script_source=source)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval",
        description="時間機械コンテンツ品質の評価ハーネス",
    )
    parser.add_argument(
        "--script-source",
        choices=list(SCRIPT_SOURCES),
        default=None,
        help=(
            "台本の供給源。既定は EVAL_SCRIPT_SOURCE（未設定なら "
            f"'{DEFAULT_SCRIPT_SOURCE}' = ネットワーク不可）。"
            "'gemini' は実APIを叩く非決定的モード。"
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=f"'{DEFAULT_SCRIPT_SOURCE}'（ネットワーク不可）を強制する（CI 用）",
    )
    parser.add_argument("--fixtures", default=None, help="fixture JSON（--script-source fixtures 用）")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_FACT_SCORE_THRESHOLD,
        help=f"fact score のゲート下限（既定 {DEFAULT_FACT_SCORE_THRESHOLD:g}）",
    )
    parser.add_argument("--json", action="store_true", help="機械向け JSON で出力する")
    parser.add_argument("--out", default=None, help="JSON の書き出し先")
    parser.add_argument("--verbose", action="store_true", help="違反を 1 件ずつ表示する")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI エントリポイント。

    終了コード
    ----------
    0
        ゲート合格（または ``--help``）。
    1
        ゲート不合格。**1 ケースでも落ちれば 1**。CI はこれで停止できる。
    2
        供給源や fixture の指定が不正（gate の不合格とは区別する）。
    """
    args = _build_parser().parse_args(argv)

    source = "deterministic" if args.offline else args.script_source
    try:
        report = run_all(
            threshold=args.threshold,
            script_source=source,
            fixtures=Path(args.fixtures) if args.fixtures else None,
        )
    except ScriptSourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # JSON を標準出力に出す場合、要約は**標準エラー**に回す。
    # 標準出力に混ぜると `json.loads()` が "Extra data" で落ちるため。
    stream = sys.stderr if (args.json or args.out) else sys.stdout
    if args.json or args.out:
        body = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(body + "\n", encoding="utf-8")
            print(f"書き出しました: {args.out}", file=stream)
        else:
            sys.stdout.write(body)
            sys.stdout.write("\n")
    else:
        hermetic = "閉路（再現可能）" if report.hermetic else "非閉路（ネットワーク依存・非決定的）"
        print(f"台本供給源: {report.script_source} / {hermetic}")
        for result in report.results:
            flag = "OK  " if result.passed else "NG  "
            print(f"[{flag}] {result.describe()}")
            if args.verbose:
                for problem in result.failures:
                    print(f"        ! {problem}")
                for problem in result.warnings:
                    print(f"        ? {problem}")
                for problem in result.not_applicable:
                    print(f"        n/a {problem}")

    print(file=stream)
    print(
        f"合計 {len(report.results)} ケース / 合格 {report.passed_count} / "
        f"不合格 {report.failed_count} / fact score 平均 {report.mean_fact_score} "
        f"（最小 {report.min_fact_score}）",
        file=stream,
    )
    print(f"ゲート閾値: fact score >= {report.threshold:g}", file=stream)
    print(f"文字数の許容帯: {length_bounds()}", file=stream)
    return 0 if report.gate_ok else 1


__all__ = [
    "DEFAULT_DAY",
    "DEFAULT_MONTH",
    "DEFAULT_TARGET_NAME",
    "MODES",
    "SCRIPT_SOURCES",
    "CaseResult",
    "EvalReport",
    "build_script",
    "run_all",
    "run_case",
]

if __name__ == "__main__":  # pragma: no cover - 手動実行
    raise SystemExit(main())
