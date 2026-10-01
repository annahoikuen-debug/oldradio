"""CheckList（Ribeiro et al. 2020 相当）の 8 項目。

提案 469 行が挙げた 6 項目に、S2 が追加した 2 項目を足す:

| # | id | 内容 |
|---|---|---|
| a | ``heading_order`` | ``###`` 見出しの出現順（オープニング → エンディング） |
| b | ``required_segments`` | 必須セグメント（オープニング・エンディング）の存在 |
| c | ``song_match`` | 言及された曲名が選曲リストに含まれるか（一致率） |
| d | ``era_words`` | 年代語が対象年以内か（``future_year_mentions`` を再利用） |
| e | ``prompt_leftover`` | プロンプト残骸の混入 |
| f | ``japanese_ratio`` | 日本語文字比率 > 0.9 |
| g | ``song_duplication`` | 重複検出（同一曲が 1 番組内で 2 回以上） |
| h | ``unfulfilled_preannounce`` | 「予告したのに配信しない」（**S2 が追加**） |

## 設計方針

- **各項目は独立した公開関数**として切り出す。1 項目だけ壊した文字列を
  作れば、その項目だけ違反になる（テストの「何を壊せば落ちるか」に対応する）。
- ``prompt_leftover`` は ``retro_radio/utils/text_cleaner.py`` の
  :func:`clean_script_for_tts` と**衝突しない**。cleaner は「行全体が
  プロンプトの指示であるもの」を除去するが、本項目は**行の途中や
  行間に混入した残骸**を検出する（cleaner が取り逃す領域）。
  つまり「cleaner を始めて도検出できる」関係にあり、
  「cleaner の努力を壊す」関係にはない。
- ``era_words`` は S1 の :func:`retro_radio.core.facts.future_year_mentions`
  を**そのまま使う**（4 桁西暦と「○年代」の両方を走査するあの実装）。
  線引きも S1 と揃える: **台本由来の違反は warn 相当**（ゲートは落とさない）。
  ただし CheckList 側は**検出結果を報告する**だけで、
  ``scripts/validate_facts.py`` の fail/warn と重複してゲートを落とさない。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - 直接実行時の保険
    sys.path.insert(0, str(_REPO_ROOT))

from retro_radio.core.facts import future_year_mentions

from .length import JAPANESE_RATIO_MIN
from .preannounce import detect_unfulfilled_preannounce
from .songs import SongMatchResult, song_match_rate

#: 必須セグメント
REQUIRED_SEGMENTS: Tuple[str, ...] = ("オープニング", "エンディング")

#: 見出しの正規表現
_HEADING = re.compile(r"^\s*###\s*(?P<title>.+?)\s*$", re.MULTILINE)

#: 日本語（ひらがな・カタカナ・漢字・CJK 記号。全角括弧類は「日本語の記号」扱い）
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff01-\uff60]")

#: **言語の手がかりになる文字**（分母に使う）。
#: ASCII 英字 + 日本語/CJK 文字のみ。数字・記号・空白は**言語衔无关**なので
#: 分母から外す。理由:
#:
#: - ``1995年5月15日`` / ``（19:30 放送開始）`` の数字は正常な日本語原稿に必ず出る。
#: - ``###`` のような Markdown 記号は構造であって言語ではない。
#:
#: 英語原稿なら ASCII 英字だけが分母に残るため比率は 0 になり、
#: 検出は効く（中国語原稿も同様に比率が下がる）。
_LANGUAGE_SIGNAL = re.compile(r"[A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff01-\uff60]")

#: 引用符（固有名の囲み）。日本語比率の**分母から除外する**。
#: 曲名や番組名にラテン文字が含まれるのは正常（例: ``「Zion」``、``「TSUNAMI」``）ため、
#: 固有名を「非日本語の混入」と数えると正しい原稿を誤って落とす。
#: **分母から外すのは引用符の中だけ**であり、地の文に英字が混ざれば検出は効く。
_PROPER_NOUN = re.compile(r"[「『]([^」』]*)[」』]")

#: プロンプト残骸。text_cleaner が落とす「行指示」に加えて、
#: 行の途中・行間に混入する残骸（API 応答のラベルなど）を捕まえる。
_PROMPT_LEFTOVER: Tuple[re.Pattern, ...] = (
    re.compile(r"^\s*以下のセグメント構成で原稿を書いてください"),
    re.compile(r"^\s*以上のセグメント構成で原稿を書いてください"),
    re.compile(r"^\s*あなたは.*ラジオパーソナリティです。"),
    re.compile(r"曲とトークを交互に配置するラジオ番組です"),
    re.compile(r"^\s*最後は「.*」で締めくくる"),
    re.compile(r"^\s*条件\s*[:：]"),
    re.compile(r"^\s*口調\s*[:：]"),
    re.compile(r"^\s*重要度\s*[:：]"),
    re.compile(r"^\s*-\s*口調\s*[:：]"),
    # API 応答のラベル（text_cleaner は行全体しか見ないので取り逃す）
    re.compile(r"\bLag\s*[:：]"),
    re.compile(r"\bレスポンス\s*[:：]"),
    re.compile(r"\bResponse\s*[:：]"),
    re.compile(r"\bプロンプト\s*[:：]"),
    re.compile(r"\bシステム\s*[:：]"),
    re.compile(r"\bThinking\s*[:：]"),
    re.compile(r"\bDraft\s*[:：]"),
    re.compile(r"^\s*\[/?\w+\]\s*$"),
    re.compile(r"^\s*<\s*/?\s*(?:system|user|assistant)\s*>\s*$"),
)

#: 曲名一致率のゲート。
#:
#: **閾値 0.9 の意味**: 「台本が名ざした曲名 10 件のうち、少なくとも 9 件が
#: 照合先に存在する」こと。**言及 0 件なら一致率は定義できない**
#: （:attr:`eval.metrics.songs.SongMatchResult.applicable` が ``False``）ため、
#: ゲートは発火しない。その場合は :attr:`ChecklistResult.not_applicable` に
#: 出て「満点ではない」ことが分かる。
SONG_MATCH_RATIO_MIN = 0.9


@dataclass(frozen=True)
class ChecklistViolation:
    """1 件の CheckList 違反。"""

    item: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 表示のみ
        return f"{self.item}: {self.detail}"


@dataclass(frozen=True)
class ChecklistResult:
    """CheckList 8 項目の結果。

    Attributes
    ----------
    japanese_ratio_applicable:
        日本語比率の分母が存在したか。``False``（原稿が空）なら
        :attr:`japanese_ratio` の ``1.0`` は「満点」ではなく「**測れない**」である。
    song_match_applicable:
        曲名の主張が 1 件以上あったか。``False`` なら一致率は「**測れない**」。
    """

    violations: Tuple[ChecklistViolation, ...] = ()
    song_match: Optional[SongMatchResult] = None
    japanese_ratio: float = 1.0
    era_mentions: Tuple[str, ...] = ()
    japanese_ratio_applicable: bool = True
    song_match_applicable: bool = True

    @property
    def not_applicable(self) -> Tuple[str, ...]:
        """**定義できなかった**項目の一覧（違反ではないが、隠さない）。"""
        items: List[str] = []
        if not self.song_match_applicable:
            items.append("song_match: 曲名の主張が 0 件（一致率を定義できない）")
        if not self.japanese_ratio_applicable:
            items.append("japanese_ratio: 言語の手がかりになる文字が 0 個（比率を定義できない）")
        return tuple(items)

    @property
    def passed(self) -> bool:
        """違反 0 件なら ``True``（提案 490 行の CI ゲート）。"""
        return not self.violations

    @property
    def items_checked(self) -> Tuple[str, ...]:
        return CHECKLIST_ITEMS

    @property
    def items_violated(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for violation in self.violations:
            if violation.item not in seen:
                seen.append(violation.item)
        return tuple(seen)

    def describe(self) -> str:
        if not self.violations:
            return "CheckList 違反 0 件"
        return "CheckList 違反 " + str(len(self.violations)) + " 件 / " + ", ".join(
            self.items_violated
        )


def japanese_ratio_applicable(script: str) -> bool:
    """日本語比率の**分母が存在するか**。

    ``False`` は「分母の文字が 0 個」= 原稿が空（または引用符内だけ）という意味で、
    :func:`japanese_char_ratio` が返す ``1.0`` は**満点ではなく「測れない」**である。
    """
    text = _PROPER_NOUN.sub("", script or "")
    return any(_LANGUAGE_SIGNAL.match(ch) for ch in text)


#: 走査する 8 項目の ID（順序は表示順）。
CHECKLIST_ITEMS: Tuple[str, ...] = (
    "heading_order",
    "required_segments",
    "song_match",
    "era_words",
    "prompt_leftover",
    "japanese_ratio",
    "song_duplication",
    "unfulfilled_preannounce",
)


# ---------------------------------------------------------------------------
# 個別項目
# ---------------------------------------------------------------------------
def extract_headings(script: str) -> List[str]:
    """``###`` 見出しのタイトルを**出現順**で返す。"""
    return [m.group("title").strip() for m in _HEADING.finditer(script or "")]


def check_heading_order(script: str) -> List[str]:
    """(a) 見出しの出現順が「オープニング → … → エンディング」か。

    1 つも見出しが無い場合も違反とする（**台本として構造が無い**）。
    """
    headings = extract_headings(script)
    if not headings:
        return ["見出し（### ）が 1 つもありません"]

    problems: List[str] = []
    open_index = next((i for i, h in enumerate(headings) if "オープニング" in h), None)
    end_index = next((i for i, h in enumerate(headings) if "エンディング" in h), None)

    if open_index is None:
        problems.append("### オープニング がありません")
    if end_index is None:
        problems.append("### エンディング がありません")
    if open_index is not None and end_index is not None and open_index >= end_index:
        problems.append(
            f"見出し順が反転しています: オープニング(idx={open_index}) >= エンディング(idx={end_index})"
        )
    if open_index is not None and open_index != 0:
        problems.append(f"### オープニング が先頭ではありません（idx={open_index}）")
    if end_index is not None and end_index != len(headings) - 1:
        problems.append(f"### エンディング が末尾ではありません（idx={end_index}）")
    return problems


def check_required_segments(script: str) -> List[str]:
    """(b) 必須セグメントの存在。順序は (a) が担当する。"""
    text = script or ""
    return [f"必須セグメントがありません: ### {name}" for name in REQUIRED_SEGMENTS if name not in text]


def check_song_match(
    script: str, year: Optional[int] = None, *, allowed_titles: Optional[Iterable[str]] = None
) -> Tuple[SongMatchResult, List[str]]:
    """(c) 言及された曲名が照合先に含まれるか（一致率）。

    Returns
    -------
    tuple[SongMatchResult, list[str]]
        結果と違反理由。``rate`` が閾値未満なら違反。**言及 0 件なら違反しない**
        （原稿が曲に言及しないこと自体は CheckList の違反ではない）。
    """
    result = song_match_rate(script, year, allowed_titles=allowed_titles)
    problems: List[str] = []
    if result.mentions and result.rate < SONG_MATCH_RATIO_MIN:
        problems.append(
            f"曲名一致率が {result.rate * 100:.1f}%（閾値 {SONG_MATCH_RATIO_MIN * 100:.0f}%）。"
            f"不一致: {[m.title for m in result.unmatched]}"
        )
    return result, problems


def check_era_words(script: str, year: int) -> List[str]:
    """(d) 年代語が対象年以内か。S1 の ``future_year_mentions`` を再利用する。

    **warn 相当**（ゲートは落とさない）: 台本の自由文は
    ``REMINISCENCE_DATA`` などレジストリ外のデータを含むため。
    """
    return [f"対象年（{year}）より後の年を言及: {mention}" for mention in future_year_mentions(script or "", year)]


def check_prompt_leftover(script: str) -> List[str]:
    """(e) プロンプト残骸の混入。

    ``retro_radio/utils/text_cleaner.py`` の :func:`clean_script_for_tts` は
    「行全体がプロンプト指示であるもの」を除去する。ここでは**行内混入**と
    **API 応答ラベル**を検出する。cleaner の努力を壊すものではない
    （cleaner を通しても残るものを対象にしている）。
    """
    problems: List[str] = []
    for index, line in enumerate((script or "").splitlines(), start=1):
        for pattern in _PROMPT_LEFTOVER:
            if pattern.search(line):
                problems.append(f"{index} 行目にプロンプト残骸: {line.strip()[:60]}")
                break
    return problems


def japanese_char_ratio(script: str) -> float:
    """(f) 日本語文字の比率を返す。

    提案の「日本語比率 > 0.9」を、誤検出が 0 になるように 2 点だけ精密化する。

    1. **引用符の中は分母から除外する。** 曲名・番組名はラテン文字を含むことが
       正常であり（``「Zion」`` ``「TSUNAMI」``）、それを「非日本語の混入」と
       数えると正しい原稿を誤って落とす。引用符の外に英字が混ざった場合
       （英語が混じった原稿）は検出される。
    2. **数字・記号・空白は分母に入れない。** ``1995年5月15日`` の数字や
       ``###`` は日本語原稿に必ず現れる正常な構造であって、言語の混入ではない。

    分母は「言語の手がかりになる文字」（ASCII 英字 + CJK）だけになるため、
    英語原稿なら比率は 0 になり、検出は効いたままになる。

    空文字は ``1.0``（比率の分母が無い＝違反ではない）。
    """
    text = _PROPER_NOUN.sub("", script or "")
    signal = [ch for ch in text if _LANGUAGE_SIGNAL.match(ch)]
    if not signal:
        return 1.0
    japanese = sum(1 for ch in signal if _JAPANESE.match(ch))
    return japanese / len(signal)


def check_japanese_ratio(script: str, threshold: float = JAPANESE_RATIO_MIN) -> Tuple[float, List[str]]:
    """(f) 日本語文字比率が閾値を超えているか。

    Parameters
    ----------
    script:
        対象原稿。
    threshold:
        比率の**下限**。既定は :data:`~eval.metrics.length.JAPANESE_RATIO_MIN` = 0.9。
        意味は「言語の手がかりになる文字のうち、日本語文字が 9 割以上」。
        **等号は違反**（``ratio <= threshold``）。境界値でも混在を許さないため。

    Returns
    -------
    tuple[float, list[str]]
        比率と違反理由。分母が存在しない（原稿が空）場合は比率 ``1.0`` と
        違反 0 件だが、それは**「測れない」**ので
        :attr:`ChecklistResult.not_applicable` に現れる。
    """
    ratio = japanese_char_ratio(script)
    if ratio <= threshold:
        return ratio, [f"日本語文字比率が {ratio:.3f}（閾値 {threshold}）。英語・中国語の混在が疑われます"]
    return ratio, []


def check_song_duplication(script: str, *, allowed_titles: Optional[Iterable[str]] = None) -> List[str]:
    """(g) 同一曲が 1 番組内で 2 回以上言及されていないか。

    閾値の意味: 同一曲の出現回数が **2 回**で違反（1 回までは許容）。
    照合先に無い未知の曲も重複として数える。
    """
    result = song_match_rate(script, None, allowed_titles=allowed_titles)
    return [
        f"同一曲が 1 番組内で {count} 回言及されています: {title}" for title, count in result.duplicates
    ]


def check_preannounce(script: str) -> List[str]:
    """(h) 予告したのに配信していない箇所がないか。"""
    return list(detect_unfulfilled_preannounce(script))


# ---------------------------------------------------------------------------
# 集約
# ---------------------------------------------------------------------------
def run_checklist(
    script: str,
    year: int,
    *,
    allowed_song_titles: Optional[Iterable[str]] = None,
) -> ChecklistResult:
    """CheckList 8 項目をすべて走査する。

    Parameters
    ----------
    script:
        対象原稿。
    year:
        対象年。
    allowed_song_titles:
        曲名照合先の集合。``None`` なら静的マスター。
        S3（提案②）実装後は実際の選曲結果を渡す。

    Returns
    -------
    ChecklistResult
    """
    violations: List[ChecklistViolation] = []

    def add(item: str, problems: Sequence[str]) -> None:
        for problem in problems:
            violations.append(ChecklistViolation(item, problem))

    add("heading_order", check_heading_order(script))
    add("required_segments", check_required_segments(script))

    song_result, song_problems = check_song_match(script, year, allowed_titles=allowed_song_titles)
    add("song_match", song_problems)

    era_problems = check_era_words(script, year)
    add("era_words", era_problems)

    add("prompt_leftover", check_prompt_leftover(script))

    ratio, ratio_problems = check_japanese_ratio(script)
    add("japanese_ratio", ratio_problems)

    add("song_duplication", check_song_duplication(script, allowed_titles=allowed_song_titles))
    add("unfulfilled_preannounce", check_preannounce(script))

    return ChecklistResult(
        violations=tuple(violations),
        song_match=song_result,
        japanese_ratio=ratio,
        era_mentions=tuple(era_problems),
        japanese_ratio_applicable=japanese_ratio_applicable(script),
        song_match_applicable=song_result.applicable,
    )


__all__ = [
    "CHECKLIST_ITEMS",
    "JAPANESE_RATIO_MIN",
    "REQUIRED_SEGMENTS",
    "SONG_MATCH_RATIO_MIN",
    "ChecklistResult",
    "ChecklistViolation",
    "check_era_words",
    "check_heading_order",
    "check_japanese_ratio",
    "check_preannounce",
    "check_prompt_leftover",
    "check_required_segments",
    "check_song_duplication",
    "check_song_match",
    "detect_unfulfilled_preannounce",
    "extract_headings",
    "japanese_char_ratio",
    "japanese_ratio_applicable",
    "run_checklist",
]

if __name__ == "__main__":  # pragma: no cover - 手動実行
    from retro_radio.core.fallback import (
        generate_anniversary_script,
        generate_care_script,
        generate_fallback_script,
    )

    builders = {
        "normal": generate_fallback_script,
        "care_recreation": generate_care_script,
        "anniversary": lambda y, m, d: generate_anniversary_script(y, m, d, "花子"),
    }
    for label, builder in builders.items():
        for probe_year in (1950, 1964, 1975, 1985, 1995, 2005, 2015, 2025):
            text = builder(probe_year, 5, 15)
            outcome = run_checklist(text, probe_year)
            print(f"{label}/{probe_year}: {outcome.describe()}")
            for violation in outcome.violations:
                print(f"    - {violation}")
