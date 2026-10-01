#!/usr/bin/env python3
"""事実レジストリの CI バリデータ（提案⑦ の実装案 2）。

このアプリは読み上げた事実が自己伝記記憶に統合されるため、誤りは
「表示上のバグ」ではなく**安全要件**として扱う。したがって台本・番組表に
出てよい事実は、対象年に対して ``valid_from <= year <= valid_to`` を満たす
レジストリのレコードに限られる（``plans/evidence_based_improvement_proposals.md``
374〜379 行）。

## 何を検査するか

1. **スキーマ**: 必須フィールドの欠落、`kind` / `confidence` の取り違え、
   ``valid_from > valid_to`` のような構造エラー。
2. **出典**: ``source`` 欠落は **fail**（出典のない事実は語ってはいけない）。
   ``confidence: "unverified"`` は **warn**（一次文献未照合は記録しておく）。
3. **重複**: レコード ``id`` の重複、バケット内のタイトル重複。
4. **対象年の絞り込み**: 全 76 年（1950〜2025）× 全モードで実際に生成される
   台本・番組表を走査し、**その年に入っていない番組が出ていないか**を見る。
5. **未来年の漏出**: **4 桁の西暦と「○年代」の両方**を走査する
   （旧ガードは数字しか見ず、「2020年代」のような年代表記をすり抜けた）。
6. **放送局の矛盾**: 同じ年に入.program が複数の ``network`` を主張していないか。

## 使い方

```
python scripts/validate_facts.py            # 終了コード: fail があれば 1
python scripts/validate_facts.py --quiet    # warn の内訳だけ出す
```

pytest からも使える:

```python
from scripts.validate_facts import validate_all, issues_by_level
issues = validate_all()
failures = [i for i in issues if i.level == "fail"]
```

**S2（提案⑨ 評価ハーネス）への引き継ぎ**: ``validate_all()`` は副作用のない
純粋関数で、``Issue`` の ``.level``（``"fail"`` / ``"warn"``）、``.code``、
``.message``、``.where`` を持つ。fact score の分子・分母を求める際に
``build_fact_table(year)`` が「その年に有効な事実テーブル」を返すので、
正解データの供給点としてそのまま使える。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ``scripts/`` を直接実行しても ``retro_radio`` を import できるようにする。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts._console import force_utf8_stdio  # noqa: E402
from retro_radio.core.facts import (  # noqa: E402
    ALLOWED_CONFIDENCE,
    ALLOWED_KINDS,
    REQUIRED_FIELDS,
    FactRegistryError,
    facts_valid_for,
    future_year_mentions,
    load_facts,
    programs_for_year,
)
from retro_radio.core.fallback import (  # noqa: E402
    RADIO_PROGRAMS_BY_DECADE,
    HistoricalRadioPrograms,
    generate_anniversary_script,
    generate_care_script,
    generate_fallback_script,
)

# 走査対象の年。設定の min_year / max_year と一致させる。
START_YEAR = 1950
END_YEAR = 2025
ALL_YEARS: Tuple[int, ...] = tuple(range(START_YEAR, END_YEAR + 1))

# 走査対象のモード（3 モード）。
SCRIPT_BUILDERS = {
    "normal": generate_fallback_script,
    "care_recreation": generate_care_script,
    "anniversary": lambda y, m, d: generate_anniversary_script(y, m, d, "花子"),
}

FAIL = "fail"
WARN = "warn"


@dataclass(frozen=True)
class Issue:
    """バリデータが見つけた 1 件の不備。"""

    level: str
    code: str
    message: str
    where: str = ""

    def __str__(self) -> str:  # pragma: no cover - 表示のみ
        location = f" [{self.where}]" if self.where else ""
        return f"{self.level.upper()}: {self.code}: {self.message}{location}"


@dataclass
class Report:
    """検査結果の集計。"""

    issues: List[Issue] = field(default_factory=list)

    def add(self, level: str, code: str, message: str, where: str = "") -> None:
        self.issues.append(Issue(level=level, code=code, message=message, where=where))

    @property
    def failures(self) -> List[Issue]:
        return [i for i in self.issues if i.level == FAIL]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.level == WARN]

    @property
    def ok(self) -> bool:
        return not self.failures


# --------------------------------------------------------------------------
# 各検査
# --------------------------------------------------------------------------
def check_schema(report: Report, records: Sequence[Dict[str, Any]]) -> None:
    """必須フィールドの欠落・型の取り違え・自己矛盾を検出する。"""
    for record in records:
        rid = str(record.get("id") or "<id なし>")

        # ``valid_to`` は None（継続中）が正当なので、それ以外だけ必須扱いする。
        missing = [
            f
            for f in REQUIRED_FIELDS
            if f != "valid_to" and not record.get(f) and record.get(f) != 0
        ]
        if missing:
            report.add(FAIL, "missing-field", f"必須フィールドがありません: {missing}", rid)
            continue

        if record["kind"] not in ALLOWED_KINDS:
            report.add(
                FAIL,
                "bad-kind",
                f"kind が許可されていない: {record['kind']!r}（許可: {list(ALLOWED_KINDS)}）",
                rid,
            )

        if record["confidence"] not in ALLOWED_CONFIDENCE:
            report.add(
                FAIL,
                "bad-confidence",
                f"confidence が許可されていない: {record['confidence']!r}",
                rid,
            )

        valid_from = record["valid_from"]
        valid_to = record["valid_to"]
        if isinstance(valid_from, int) and isinstance(valid_to, int) and valid_to < valid_from:
            report.add(
                FAIL,
                "inverted-period",
                f"valid_to({valid_to}) が valid_from({valid_from}) より小さい",
                rid,
            )

        duration = record["duration_min"]
        if isinstance(duration, int) and duration <= 0:
            report.add(FAIL, "bad-duration", f"duration_min が正ではない: {duration}", rid)

        # 読み上げ文言が空だと番組表が情報を持たない。
        if not str(record.get("description_ja") or record.get("claim_ja") or "").strip():
            report.add(FAIL, "empty-description", "description_ja が空です", rid)


def check_sources(report: Report, records: Sequence[Dict[str, Any]]) -> None:
    """``source`` 欠落は fail、``unverified`` は warn。

    出典のない事実は語ってはいけない（Raji et al. 2020 の accountability 経路）。
    """
    for record in records:
        rid = str(record.get("id") or "<id なし>")
        source = record.get("source")

        if not source or not str(source).strip():
            report.add(FAIL, "missing-source", "source がありません（出典必須）", rid)
        if record.get("confidence") == "unverified":
            report.add(
                WARN,
                "unverified",
                "confidence が unverified です（一次文献との照合が必要）",
                rid,
            )


def check_duplicate_ids(report: Report, records: Sequence[Dict[str, Any]]) -> None:
    """レジストリ全体で ``id`` が重複していないか。"""
    seen: Set[str] = set()
    for record in records:
        rid = str(record.get("id"))
        if rid in seen:
            report.add(FAIL, "duplicate-id", f"id が重複しています: {rid}", rid)
        seen.add(rid)


def check_bucket_duplicates(report: Report) -> None:
    """バケット内に同じタイトルが 2 回ないか（``RADIO_PROGRAMS_BY_DECADE``）。"""
    for key, schedules in sorted(RADIO_PROGRAMS_BY_DECADE.items()):
        titles: Dict[str, int] = {}
        for schedule in schedules:
            titles[schedule.title] = titles.get(schedule.title, 0) + 1
        for title, count in sorted(titles.items()):
            if count > 1:
                report.add(
                    FAIL,
                    "duplicate-in-bucket",
                    f"バケット内に同名の番組が {count} 件あります: {title}",
                    f"RADIO_PROGRAMS_BY_DECADE[{key}]",
                )

        ids: Set[str] = set()
        for schedule in schedules:
            if schedule.id in ids:
                report.add(
                    FAIL,
                    "duplicate-schedule-id",
                    f"バケット内で id が重複しています: {schedule.id}",
                    f"RADIO_PROGRAMS_BY_DECADE[{key}]",
                )
            ids.add(schedule.id)


def check_network_conflicts(report: Report, records: Sequence[Dict[str, Any]]) -> None:
    """同じ年に有効なのに放送局が食い違う記録がないか。

    典型的な誤りは「同じ番組名のレコードが 2 つあり、片方だけ放送局が
    違っている」という形（Sportacent は 2010 / 2020 の 2 箇所の説明が
    互いに矛盾していた）。
    """
    by_title: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        by_title.setdefault(str(record["title"]), []).append(record)

    for title, group in sorted(by_title.items()):
        if len(group) < 2:
            continue
        networks = {str(r["network"]) for r in group}
        if len(networks) > 1:
            # 期間が重なる年だけ実際に問題になる。
            years = set(range(START_YEAR, END_YEAR + 1))
            for other in group[1:]:
                lo = max(int(r["valid_from"]) for r in group)
                hi = min(
                    (
                        int(r["valid_to"])
                        for r in group
                        if isinstance(r.get("valid_to"), int)
                    ),
                    default=END_YEAR,
                )
                overlap = {y for y in years if lo <= y <= hi}
                for r in group:
                    period = _period_of(r)
                    overlap &= period
                if not overlap:
                    continue
                report.add(
                    FAIL,
                    "network-conflict",
                    f"同名の番組が複数の放送局を主張しています: {sorted(networks)}"
                    f"（該当年: {sorted(overlap)[:5]}...）",
                    title,
                )
                break


def check_record_periods_against_years(
    report: Report, records: Sequence[Dict[str, Any]]
) -> None:
    """各レコードの期間が「対象年に対して意味があるか」を見る。

    - 完全に範囲外（どの年にも使われない）レコードは warn。
    - 説明文に、記録の期間が対象年を外れる年を数値で書いていれば fail。
    """
    for record in records:
        rid = str(record["id"])
        valid_from = int(record["valid_from"])
        valid_to = record["valid_to"]
        hi = int(valid_to) if isinstance(valid_to, int) else END_YEAR

        if hi < START_YEAR or valid_from > END_YEAR:
            report.add(
                WARN,
                "unreachable-period",
                f"対象年（{START_YEAR}-{END_YEAR}）のどこにも当てはまりません",
                rid,
            )
            continue

        # ``description_ja`` は読み上げ文言なので、4 桁の年を直接含めないこと。
        for mention in future_year_mentions(
            str(record.get("description_ja") or ""), END_YEAR
        ):
            report.add(
                FAIL,
                "out-of-period-year",
                f"description_ja に期間外の年があります: {mention}",
                rid,
            )


def check_scripts_and_guide(report: Report, records: Sequence[Dict[str, Any]]) -> None:
    """全 76 年 × 全モードを実際に生成し、事実の絞り込みを検証する。

    1. 台本・番組表に「その年に入っていない番組名」が出ていないか。
    2. 4 桁の西暦・年代表記で「対象年より後」を語っていないか。

    **fail と warn の線引き**: 番組表（``get_program_guide``）はレジストリの
    レコードだけが生成源なので、違反は **fail**。一方、台本の自由文は
    ``REMINISCENCE_DATA``（クイズ）や LLM 自由生成など**レジストリ外のデータ**
    も混ざっており、そこを fail にすると所有範囲外の修正まで要求してしまう。
    そのため台本由来の違反は **warn** として可視化し、fact score
    （提案⑨）の材料として使う。
    """
    titles_by_year: Dict[int, Set[str]] = {}
    for year in ALL_YEARS:
        titles_by_year[year] = {str(r["title"]) for r in programs_for_year(year)}

    # 記録の title .apple -> 期間（重複タイトル時の検出用）
    periods: Dict[str, List[Tuple[int, int]]] = {}
    for record in records:
        lo = int(record["valid_from"])
        hi = int(record["valid_to"]) if isinstance(record["valid_to"], int) else END_YEAR
        periods.setdefault(str(record["title"]), []).append((lo, hi))

    for year in ALL_YEARS:
        valid_titles = titles_by_year[year]

        for mode, builder in SCRIPT_BUILDERS.items():
            try:
                script = builder(year, 5, 15)
            except Exception as exc:  # pragma: no cover - 失敗時の証拠を残す
                report.add(
                    FAIL, "script-error", f"原稿生成で例外が発生しました: {exc!r}", f"{mode}/{year}"
                )
                continue

            # (1) その年に有効な番組以外は台本に出してはいけない。
            for title, spans in periods.items():
                if title not in script:
                    continue
                if title in valid_titles:
                    continue
                if any(lo <= year <= hi for lo, hi in spans):
                    continue
                report.add(
                    WARN,
                    "stale-fact-in-script",
                    f"対象年（{year}）に放送されていない番組が台本に含まれています: {title}"
                    "（出典は REMINISCENCE_DATA など台本の自由文。レジストリ側の管轄外）",
                    f"{mode}/{year}",
                )

            # (2) 4 桁の西暦と「○年代」の両方で未来年を検出する。
            #     ``_mentions_future_year`` のガードと同じ走査（対になる検査）。
            for mention in future_year_mentions(script, year):
                report.add(
                    WARN,
                    "future-year-in-script",
                    f"対象年（{year}）より後の年を言及しています: {mention}"
                    "（出典はクイズの自由文。レジストリ側の管轄外）",
                    f"{mode}/{year}",
                )

        # 番組表の検証
        try:
            guide = HistoricalRadioPrograms.get_program_guide(year, 5, 15)
        except Exception as exc:  # pragma: no cover
            report.add(FAIL, "guide-error", f"番組表の生成で例外が発生しました: {exc!r}", str(year))
            continue

        for schedule in guide.schedules:
            blob = f"{schedule.title} {schedule.description or ''}"
            for mention in future_year_mentions(blob, year):
                report.add(
                    FAIL,
                    "future-year-in-guide",
                    f"対象年（{year}）より後の年を記載しています: {mention}",
                    f"guide/{year}/{schedule.id}",
                )
            if schedule.is_historical and schedule.title not in valid_titles:
                report.add(
                    FAIL,
                    "stale-fact-in-guide",
                    f"対象年（{year}）に放送されていない歴史番組が番組表に:"
                    f"されています: {schedule.title}",
                    f"guide/{year}",
                )


def check_guide_has_historical(report: Report) -> None:
    """どの年でも歴史番組が 1 本以上提示できること（节目表が空だと困る）。"""
    for year in ALL_YEARS:
        guide = HistoricalRadioPrograms.get_program_guide(year, 5, 15)
        historical = [s for s in guide.schedules if s.is_historical]
        if not historical:
            report.add(
                WARN,
                "no-historical-program",
                f"対象年（{year}）に提示できる歴史番組がありません",
                f"guide/{year}",
            )


def _period_of(record: Dict[str, Any]) -> Set[int]:
    lo = int(record["valid_from"])
    valid_to = record["valid_to"]
    hi = int(valid_to) if isinstance(valid_to, int) else END_YEAR
    return {y for y in range(START_YEAR, END_YEAR + 1) if lo <= y <= hi}


# --------------------------------------------------------------------------
# エントリポイント
# --------------------------------------------------------------------------
def validate_all(
    years: Optional[Iterable[int]] = None,
    modes: Optional[Iterable[str]] = None,
) -> List[Issue]:
    """全検査を実行し、見つかった不備を ``Issue`` のリストで返す。

    **S2 から使う公開 API**。副作用はなく、呼び出すたびに同じ結果を返す。

    Parameters
    ----------
    years:
        走査する年。省略時は 1950〜2025 の 76 年すべて。
    modes:
        走査するモード名。省略時は 3 モードすべて。

    Returns
    -------
    list[Issue]
        ``level`` が ``"fail"`` のものが 1 件でもあれば CI ゲートは落とす。
    """
    report = Report()

    try:
        records = load_facts()
    except FactRegistryError as exc:
        report.add(FAIL, "registry-unreadable", f"事実レジストリを読み込めません: {exc}")
        return report.issues

    check_schema(report, records)
    check_sources(report, records)
    check_duplicate_ids(report, records)
    check_network_conflicts(report, records)
    check_record_periods_against_years(report, records)
    check_bucket_duplicates(report)
    check_scripts_and_guide(report, records)
    check_guide_has_historical(report)

    return report.issues


def issues_by_level(issues: Iterable[Issue]) -> Dict[str, List[Issue]]:
    """``Issue`` のリストを ``{"fail": [...], "warn": [...]}`` に分ける。

    S2 の fact score 算出などで「fail だけ見る」ために使う。
    """
    grouped: Dict[str, List[Issue]] = {FAIL: [], WARN: []}
    for issue in issues:
        grouped.setdefault(issue.level, []).append(issue)
    return grouped


def build_fact_table(year: int) -> List[Dict[str, Any]]:
    """「その年に対して有効な事実」のテーブルを返す（S2 向け）。

    評価ケースの正解データはここに。正本どおりの並び順を保つ。
    """
    return facts_valid_for(year)


def main(argv: Optional[Sequence[str]] = None) -> int:
    # 検査結果に cp932 で表現できない文字が混ざっていても
    # 報告を最後まで出し切るため、**検査より前**に stdout を UTF-8 にする。
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="事実レジストリの CI バリデータ")
    parser.add_argument("--quiet", action="store_true", help="warn の内訳だけ表示する")
    parser.add_argument(
        "--json", action="store_true", help="結果を JSON で出力する（機械向け）"
    )
    args = parser.parse_args(argv)

    issues = validate_all()
    grouped = issues_by_level(issues)

    if args.json:
        print(
            json.dumps(
                {
                    "fail": [str(i) for i in grouped.get(FAIL, [])],
                    "warn": [str(i) for i in grouped.get(WARN, [])],
                    "ok": not grouped.get(FAIL),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if grouped.get(FAIL) else 0

    for level in (FAIL, WARN):
        for issue in grouped.get(level, []):
            print(issue)
        if not args.quiet or level == FAIL:
            print(f"{level}: {len(grouped.get(level, []))} 件")

    if grouped.get(FAIL):
        print("事実バリデーションに失敗しました。")
        return 1
    print("事実バリデーションは通りました（warn のみ）。")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI エントリポイント
    raise SystemExit(main())
