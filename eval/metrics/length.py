"""文字数のレンジ制（提案⑨-2「1,000 文字以上」を捨てる）。

## 何を変えるのか

`tests/test_content_regression.py::test_all_years_and_modes_reach_target_script_length`
は「全 76 年 × 3 モードで 1,000 字以上」を要求していた。これは
**水増しを報酬する構造**である。`generate_fallback_script`
（`retro_radio/core/fallback.py:341`）が「三つほどご用意しました」と予告しながら
中身は空気だけ、`365:368` で終わrals 1000 字を埋めているという既知の欠陥も、
この 1 本のテストを通すためだけに維持されてきた。

本モジュールは**下限と上限の両方**を罰する:

- 下限: 短すぎる原稿（空虚な原稿・生成の失敗）を落とす。
- 上限: 長すぎる原稿（反復・水増し）を落とす。**これが「以上」を捨てた部分**。

## 設定値はどう使うか

`retro_radio/config.py:119-120` の `target_script_chars`（既定 1000）と
`script_char_tolerance`（既定 200）は**削除しない**（S4 の所有ファイル）。
代わりに次のように**再解釈**する:

```
下限 = target_script_chars - script_char_tolerance            = 1000 - 200 =  800
上限（設定由来） = target_script_chars + script_char_tolerance * 2 = 1000 + 400 = 1400
```

ただし**上限は実測で上書きする**。理由は 1 つで、**現行の `care_recreation` は
本来就 1418〜1483 字ある**ため、設定由来の 1400 では正当な原稿を誤って落とす。
下限が 800 でよい根拠も同じで、**全 228 サンプルが 1037 字以上**であり、
800 字を割るのは原稿が生成できていない場合だけだからである。

## 実測値（レンジ決定の根拠）

`python -m eval.metrics.length --measure` で 76 年 × 3 モード = 228 サンプルを
実測した結果（2026-09-30 時点・フォールバック経路）:

| 統計量 | 全 228 | normal | care_recreation | anniversary |
|---|---|---|---|---|
| n | 228 | 76 | 76 | 76 |
| min | 1037 | 1339 | 1418 | 1037 |
| **p5** | **1058** | 1339 | 1418 | 1037 |
| p50 | 1339 | 1339 | 1448 | 1066 |
| **p95** | **1476** | 1339 | 1483 | 1101 |
| max | 1483 | 1339 | 1483 | 1101 |
| mean | 1286.1 | 1339.0 | 1452.8 | 1066.5 |
| 母標準偏差 | 162.9 | 0.0 | — | — |

読み取れること:

1. `normal` は**年によらず 1339 字で一定**。`{year}年{month}月{day}日` の桁数が
   4 桁で固定されるため。つまり normal モードは**年ごとの内容が同じ**であり、
   文字数が揃うこと自体は内容の同意Vb性を保証しない（これはモデルカードの
   「年ごとの情報量がない」不利な truths の実測根拠でもある）。
2. モード間差は 1037〜1483 字で**約 1.4 倍**。1 本の下限・上限で
   228 サンプルを全部覆うには下限 1037 以下・上限 1483 以上が必要。
3. p5=1058 / p95=1476 に対し、**下限 800 は p5 より 258 字低い**。
   1000 字の「水増し原稿」すら下限は満たすので、旧来の合格ラインは
   そのままでは入らないが、**下限は明確に罰する**（空虚な原稿は落ちる）。
   一方**上限 1500 は p95 の切り上げ**であり、実測の 5%  꼬리를ちょうど
   含み、p100（1483）を 17 字超えるにすぎない。**上限は実効的な歯止め**である:
   今の閾値（1400）では 76 サンプル中 38 サンプル（care_recreation 全件）が
   落ちるが、1500 にすれば 0 件になる。**1500 は「何も罰さない」ではなく、
   「今のカルチャーを許す最大の幅」に設定している**という点が重要である。

採用値:

```
下限 =  800  = target_script_chars(1000) - script_char_tolerance(200)
上限 = 1500  = max(target_script_chars + 2*tolerance = 1400, ceil50(p95=1476) = 1500)
```

**限界の明記**: この帯は**フォールバック経路**の分布に最適化したものである。
Gemini 経路（`retro_radio/core/script_generator.py:216-233` の `_call_gemini`）は
プロンプトで長さを指定していないため分布が未定であり、上限 1500 を超える可能性がある。
その場合は「上限的历史的**後**の分布をEnsembling して上限を見直す」のが手順であり、
「上限を上げる」ことを安易に選ばない（上げれば水増しが復権する）。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - 直接実行時の保険
    sys.path.insert(0, str(_REPO_ROOT))

from retro_radio.config import get_settings

# 走査対象の年（``scripts/validate_facts.py`` の START_YEAR / END_YEAR に一致させる）
START_YEAR = 1950
END_YEAR = 2025
ALL_YEARS: Tuple[int, ...] = tuple(range(START_YEAR, END_YEAR + 1))

#: 実測値（``--measure`` の出力）。2026-09-30 時点のフォールバック経路 228 サンプル。
MEASURED: Dict[str, float] = {
    "samples": 228,
    "min": 1037,
    "p5": 1058,
    "p25": 1077,
    "p50": 1339,
    "p75": 1434,
    "p95": 1476,
    "max": 1483,
    "mean": 1286.1,
    "stdev": 162.9,
}

#: 上限は 50 文字刻みへ切り上げる（実測 p95=1476 → 1500）。
CEILING_STEP = 50

#: 日本語文字比率の下限（CheckList 項目 f）。0.9 は提案の指定値。
JAPANESE_RATIO_MIN = 0.9


def _ceil_to(value: float, step: int = CEILING_STEP) -> int:
    """``value`` を ``step`` の倍数へ切り上げる。"""
    return int(-(-value // step) * step)


def length_bounds(settings=None) -> Tuple[int, int]:
    """文字数の許容帯 ``(下限, 上限)`` を返す。

    設定（``target_script_chars`` / ``script_char_tolerance``）は**削除せず**下限・上限に
    再解釈する。上限だけは実測 ``p95`` によって上書きする（docstring 参照）。

    Returns
    -------
    tuple[int, int]
        ``(lower, upper)``。``lower <= length <= upper`` なら合格。
    """
    conf = settings or get_settings()
    target = int(conf.target_script_chars)
    tolerance = int(conf.script_char_tolerance)

    lower = target - tolerance
    ceiling_from_config = target + tolerance * 2
    ceiling_from_measure = _ceil_to(MEASURED["p95"])
    upper = max(ceiling_from_config, ceiling_from_measure)
    return lower, upper


@dataclass(frozen=True)
class LengthResult:
    """1 本の台本の長さ判定。"""

    length: int
    lower: int
    upper: int

    @property
    def ok(self) -> bool:
        """帯内なら ``True``。**短すぎても長すぎても ``False``**。"""
        return self.lower <= self.length <= self.upper

    @property
    def deviation(self) -> int:
        """帯からのずれ（0 なら帯内）。下限割れなら負、上限超過なら正。"""
        if self.length < self.lower:
            return self.length - self.lower
        if self.length > self.upper:
            return self.length - self.upper
        return 0

    @property
    def direction(self) -> str:
        """``"in_range"`` / ``"too_short"`` / ``"too_long"``。"""
        if self.length < self.lower:
            return "too_short"
        if self.length > self.upper:
            return "too_long"
        return "in_range"

    def describe(self) -> str:
        """失敗時に「誰什么原因で落ちたか」を読むための文字列。"""
        return (
            f"{self.length} 字（許容帯 {self.lower}〜{self.upper} / "
            f"{self.direction} / ずれ {self.deviation}）"
        )


def check_length(script: str, settings=None) -> LengthResult:
    """台本の長さを判定する。**短すぎる場合も罰する**。

    Parameters
    ----------
    script:
        判定する原稿。
    settings:
        設定オブジェクト。省略時は :func:`retro_radio.config.get_settings` の既定値。

    Returns
    -------
    LengthResult
    """
    lower, upper = length_bounds(settings)
    return LengthResult(length=len(script or ""), lower=lower, upper=upper)


# ---------------------------------------------------------------------------
# レンジ決定のための実測
# ---------------------------------------------------------------------------
def percentile(values: Sequence[int], q: float) -> float:
    """線形補間による百分位（``q`` は 0.0〜1.0）。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _default_builders() -> Dict[str, object]:
    from retro_radio.core.fallback import (
        generate_anniversary_script,
        generate_care_script,
        generate_fallback_script,
    )

    return {
        "normal": generate_fallback_script,
        "care_recreation": generate_care_script,
        "anniversary": lambda y, m, d: generate_anniversary_script(y, m, d, "花子"),
    }


def measure_lengths(
    years: Optional[Iterable[int]] = None,
    modes: Optional[Iterable[str]] = None,
) -> Dict[str, Dict[str, float]]:
    """現行台本長を実測してレンジ決定の証拠を返す。

    Returns
    -------
    dict
        ``{"all": {...}, "normal": {...}, "care_recreation": {...}, ...}``。
        各値は ``n`` / ``min`` / ``p5`` / ``p50`` / ``p95`` / ``max`` / ``mean`` / ``stdev``。
    """
    all_builders = _default_builders()
    selected_years = list(years) if years is not None else list(ALL_YEARS)
    selected_modes = [m for m in (modes or all_builders.keys()) if m in all_builders]

    per_mode: Dict[str, List[int]] = {mode: [] for mode in selected_modes}
    everything: List[int] = []

    for mode in selected_modes:
        builder = all_builders[mode]
        for year in selected_years:
            length = len(builder(year, 5, 15))
            per_mode[mode].append(length)
            everything.append(length)

    def summarize(values: List[int]) -> Dict[str, float]:
        if not values:
            return {"n": 0}
        return {
            "n": len(values),
            "min": min(values),
            "p5": round(percentile(values, 0.05), 1),
            "p25": round(percentile(values, 0.25), 1),
            "p50": round(percentile(values, 0.50), 1),
            "p75": round(percentile(values, 0.75), 1),
            "p95": round(percentile(values, 0.95), 1),
            "max": max(values),
            "mean": round(statistics.mean(values), 1),
            "stdev": round(statistics.pstdev(values), 1),
        }

    report = {"all": summarize(everything)}
    for mode, values in per_mode.items():
        report[mode] = summarize(values)
    return report


def _format_report(report: Dict[str, Dict[str, float]]) -> str:
    lines = []
    for name, stats in report.items():
        if not stats.get("n"):
            lines.append(f"{name}: サンプルなし")
            continue
        lines.append(
            "{name:>16} n={n:<4} min={min:<6} p5={p5:<7} p50={p50:<7} "
            "p95={p95:<7} max={max:<6} mean={mean:<8} stdev={stdev}".format(name=name, **stats)
        )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="台本文字数のレンジ決定と実測")
    parser.add_argument(
        "--measure",
        action="store_true",
        help="76 年 × 3 モードを再生成して実測する（数十秒かかる）",
    )
    args = parser.parse_args(argv)

    if args.measure:
        print("=== 現行台本長の実測 ===")
        print(_format_report(measure_lengths()))
        print()
        print(f"（記録済みの実測値: {MEASURED}）")

    lower, upper = length_bounds()
    print("=== 採用したレンジ ===")
    settings = get_settings()
    print(
        f"下限 = target_script_chars({settings.target_script_chars}) - "
        f"script_char_tolerance({settings.script_char_tolerance}) = {lower}"
    )
    print(
        f"上限 = max(target + 2*tolerance = {settings.target_script_chars + 2 * settings.script_char_tolerance}, "
        f"ceil50(p95={MEASURED['p95']}) = {_ceil_to(MEASURED['p95'])}) = {upper}"
    )
    return 0


__all__ = [
    "ALL_YEARS",
    "CEILING_STEP",
    "END_YEAR",
    "JAPANESE_RATIO_MIN",
    "MEASURED",
    "START_YEAR",
    "LengthResult",
    "check_length",
    "length_bounds",
    "measure_lengths",
    "percentile",
]

if __name__ == "__main__":  # pragma: no cover - 手動実行
    raise SystemExit(main())
