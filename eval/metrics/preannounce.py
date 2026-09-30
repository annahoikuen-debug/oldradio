"""「予告したのに配信しない」検出（提案⑨-2 の水増しテキスト検出）。

## 何を検出するか

``retro_radio/core/fallback.py:341``（**編集しない**。S1/S3 所有）は
``generate_fallback_script`` に次の 1 文を埋め込む:

    本章では、{year}年のニュースと、当時のくらしの風景を三つほどご用意しました。

ところがその後に続く ``### トーク1_ニュース`` 〜 ``### トーク3_共感`` には
**具体的な項目が 1 件も無い**。情景描写だけで、イベント名も商品名も人名も
語らない。つまり**予告して何も配らない**。

この欠陥は ``tests/test_content_regression.py`` の
「1,000 字以上」という目標のために維持されてきた。**S2 は検出だけを行い、
``core/fallback.py`` は編集しない**。検出結果は報告に載せる。

## 判定規則

1. 予告文（数量 + 「用意」「準備」「お届け」系）を**文単位**で探す。
2. 予告文より後ろに**名前付けできる具体的項目**が何件あるか数える。
   「名前付けできる」= ``「…」`` の引用か、4 桁の西暦。
3. **予告数 > 実配数**、または予告文が**単独で終端している**なら違反として報告する。

数量が明示されない予告（「いくつかご用意します」）は、単独終端のときだけ
報告する（過検出を避けるため）。数量のない予告を「0 件とみなす」規則を
採ると、弾んだ演出（"この後 quietly お届けします"）をすべて誤検出するため。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - 直接実行時の保険
    sys.path.insert(0, str(_REPO_ROOT))

#: 漢数字 → 数字（一桁と「十」まで。十二以上は本検出では扱わない）
_KANJI_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

#: 数量の読み（助数詞）
_COUNTERS = r"(?:つ|個|件|曲|本|枚|人|丁)"

#: 数量のあとに入る幅度（无所谓表现）
_FUZZY = r"(?:ほど|くらい|位|前後|ばかり|以上)?"

#: **予告**を表す動詞。数量の提示と「用意/準備」を伴うものだけを予告とみなす。
#: 「お届けします」「披露します」は**配信の宣言**であって予告ではないため
#: ここに含めない（``「…」（歌手）をお届けします`` を予告と誤検出するのを防ぐ）。
_PREP_VERB = r"(?:用意|準備|支度|お持ち|そろえ)"

#: 1) 「三つほどご用意しました」型（数量が明示されている予告）
_PREANNOUNCE_COUNTED = re.compile(
    r"(?P<n>[0-9０-９]+|[一二三四五六七八九十]+)"
    r"\s*" + _COUNTERS + _FUZZY +
    r"[^\n。！？]{0,24}?" + _PREP_VERB
)

#: 2) 「三つのニュースを用意しました」型（数量の後に固有名が来る予告）
_PREANNOUNCE_NAMED = re.compile(
    r"(?P<n>[0-9０-９]+|[一二三四五六七八九十]+)\s*(?:つ|個|件)?\s*の"
    r"(?:ニュース|話題|話|くらしの風景|ヒット曲|名曲|曲|思い出)"
    r"[^\n。！？]{0,16}?" + _PREP_VERB
)

#: 3) 数量なしの予告（「いくつかご用意します」）。
#: 数量が無いので「単独終端」のときだけ報告する（過検出を避けるため）。
_PREANNOUNCE_UNCOUNTED = re.compile(
    r"(?:いくつか|いくつもの|数つ|数件)[^\n。！？]{0,20}?" + _PREP_VERB
)

#: 走査順。数量ありを優先する。
_PREANNOUNCE_PATTERNS: Tuple[re.Pattern, ...] = (
    _PREANNOUNCE_COUNTED,
    _PREANNOUNCE_NAMED,
    _PREANNOUNCE_UNCOUNTED,
)

#: 名前付けできる具体的項目の印
_CONCRETE_QUOTE = re.compile(r"「([^」]{1,40})」")
_CONCRETE_YEAR = re.compile(r"(1[5-9]\d{2}|20\d{2})")

_HEADING = re.compile(r"^\s*#{1,6}\s*")

#: 全角数字 → 半角数字
_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")


def _parse_count(token: Optional[str]) -> Optional[int]:
    """数量トークンを int にする（読めなければ ``None``）。"""
    if not token:
        return None
    token = token.strip()
    if token.isdigit():
        return int(token)
    if all("０" <= ch <= "９" for ch in token):
        return int(token.translate(_FULLWIDTH))
    if token in _KANJI_NUMBERS:
        return _KANJI_NUMBERS[token]
    return None


def _flatten(script: str) -> List[str]:
    """原稿を「1 行 1 文」に平らにする（見出し行は落とす）。"""
    flat: List[str] = []
    for line in (script or "").splitlines():
        stripped = _HEADING.sub("", line).strip()
        if not stripped:
            continue
        for part in re.split(r"(?<=[。！？!?])", stripped):
            part = part.strip()
            if part:
                flat.append(part)
    return flat


def _target_year_hint(sentences: Sequence[str]) -> Optional[int]:
    """原稿中で最も多く現れる 4 桁の西暦を「対象年」のヒントとして返す。

    「対象年」は日付（例: 1975年5月15日）として繰り返し現れるだけで
    **中身の項目ではない**。したがって約束された項目の数には数えない。
    呼び出し側が ``target_year`` を渡せるのは、そのための上書き口である。
    """
    counter: Dict[str, int] = {}
    for sentence in sentences:
        for token in _CONCRETE_YEAR.findall(sentence):
            counter[token] = counter.get(token, 0) + 1
    if not counter:
        return None
    # 最多出現を安定させる（同数なら小さい年）
    return int(sorted(counter.items(), key=lambda kv: (-kv[1], int(kv[0])))[0][0])


def _concrete_items(sentences: Sequence[str], start_index: int, target_year: Optional[int]) -> int:
    """``start_index`` より後ろにある「名前付けできる項目」の数。

    数えるもの:

    - ``「…」`` で囲まれた固有名
    - 対象年以外の 4 桁の西暦

    数えないもの:

    - 対象年そのもの（日付の反復であって中身の項目ではない）
    - 「1975年のヒット曲 시리즈」のような**合成語**（「○年」の形）
    """
    seen = set()
    for sentence in sentences[start_index:]:
        for match in _CONCRETE_QUOTE.finditer(sentence):
            span = match.group(1).strip()
            if span:
                seen.add(("quote", span))
        for token in _CONCRETE_YEAR.findall(sentence):
            if target_year is not None and int(token) == int(target_year):
                continue
            seen.add(("year", token))
    return len(seen)


def _is_terminal(sentences: Sequence[str], index: int) -> bool:
    """``index`` 文が原稿の末尾で単独に終わっているか。"""
    return not any(s.strip() for s in sentences[index + 1 :])


def detect_unfulfilled_preannounce(script: str, *, target_year: Optional[int] = None) -> List[str]:
    """予告したのに配信していない箇所を報告する。

    Parameters
    ----------
    script:
        対象原稿。
    target_year:
        対象年。省略時は原稿中で最も多く現れる 4 桁の西暦を推定する。
        対象年そのものは「中身の項目」として数えない（日付の反復だから）。

    Returns
    -------
    list[str]
        違反の説明（空なら違反なし）。**検出のみを行い、原稿は変更しない。**
    """
    sentences = _flatten(script)
    if target_year is None:
        target_year = _target_year_hint(sentences)
    findings: List[str] = []
    seen: set = set()

    for position, sentence in enumerate(sentences):
        promised: Optional[int] = None
        matched = False
        for pattern in _PREANNOUNCE_PATTERNS:
            hit = pattern.search(sentence)
            if not hit:
                continue
            matched = True
            if promised is None:
                promised = _parse_count(hit.groupdict().get("n"))
            break
        if not matched:
            continue

        delivered = _concrete_items(sentences, position + 1, target_year)
        terminal = _is_terminal(sentences, position)

        if promised is not None and delivered < promised:
            reason = f"予告 {promised} 件に対し配信は {delivered} 件: 「{sentence[:60]}」"
        elif terminal:
            reason = f"予告文が単独で終端している（後続の具体項目 0 件）: 「{sentence[:60]}」"
        else:
            continue

        if reason not in seen:
            seen.add(reason)
            findings.append(reason)

    return findings


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - 手動実行
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
        for probe_year in (1950, 1975, 2025):
            findings = detect_unfulfilled_preannounce(builder(probe_year, 5, 15))
            print(f"{label}/{probe_year}: {len(findings)} 件")
            for item in findings:
                print(f"    - {item}")
    return 0


__all__ = ["detect_unfulfilled_preannounce"]

if __name__ == "__main__":  # pragma: no cover - 手動実行
    raise SystemExit(main())
