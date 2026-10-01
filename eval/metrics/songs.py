"""曲名一致率（S3 / 提案② へ渡す中立 API）。

## 片側に依存しない設計

「言及された曲名が選曲リストに含まれるか」を測るとき、**照合先は呼び出し側が
渡す**。本モジュールは既定でも「静的マスター（``retro_radio.core.fallback`` の
``FALLBACK_SONGS``）」を使うが、これは選曲結果でもなく台本でもなく、
**第三の正本**である。したがって:

- S3（提案②「台本と選曲の一本化」）が未実装の现阶段でも
  ``allowed_titles`` を明示的に渡せば、その実装が自動的に反映される。
- 逆に S3 が「台本から曲名を抽出して選曲する」実装に変わっても、
  呼び出し側が渡す集合が変わるだけで**本 API は変更不要**である。

これは S3 への明示的な**API 契約**であり、依存の向きを固定する:

```python
from eval.metrics.songs import extract_song_mentions, song_match_rate

# S3 未実装（静的マスターと照合）
result = song_match_rate(script, year)

# S3 実装後（実際の選曲結果と照合）
result = song_match_rate(script, year, allowed_titles=selection_titles)
```

``allowed_titles`` の扱い:

- ``None``（既定）: 照合先は静的マスター。
- 空集合: 照合先なし。1 件も言及がなければ ``rate == 1.0``（言及なしは不一致ではない）。
- それ以外: ``Iterable[str]``。曲名の重複は集合になる。

## 誤検出を避ける抽出規則

引用符 ``「…」`` はこのアプリでは**固有名**に使うが、曲名に限らない
（クイズのヒント・答えも ``「…」`` に入る）。そのため引用を**曲名とみなす条件**を
3 つに絞っている（判定は :func:`eval.metrics.fact_score.is_song_reference`）:

1. 静的マスターに載っている曲名。
2. 直後に ``（歌手）`` が付き、かつその文に「曲」「歌」「メロディ」を含む
   （このアプリの曲振り台詞の形）。
3. 直前の語が ``名曲`` / ``ヒット曲`` / ``歌`` / ``曲`` / ``メロディ``
   （``懐かしい名曲「X」`` の形）。

1〜3 のいずれかを満たさない引用（クイズのヒント・答え、キーワード）は
**曲名ではない**として無視する。誤検出は検出漏れより有害である
（誤検出は正しい原稿を「不一致」と言い張るため）。
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - 直接実行時の保険
    sys.path.insert(0, str(_REPO_ROOT))

from retro_radio.core.facts import load_facts
from retro_radio.core.songs import load_songs

from .fact_score import _QUOTED, is_song_reference, split_sentences

#: 既知の番組タイトル（曲名と区別するため）。長い順。
_PROGRAM_TITLES: Tuple[str, ...] = tuple(
    sorted({str(r["title"]) for r in load_facts()}, key=len, reverse=True)
)

def _load_known_song_titles() -> Tuple[str, ...]:
    """正本カタログ（``core/songs/songs.json``）から曲名集合を組み立てる。

    hermetic を保つため、**読み込み失敗時は空集合へ縮退**する
    （曲名の検出は ``is_song_reference`` の規則で行われるため、
    空集合でも誤検出は増えない。不一致の判定だけが保守的になる）。
    """
    try:
        catalog = load_songs()
    except Exception:  # noqa: BLE001 - カタログが読めない場合は検証を縮退
        return ()
    return tuple(
        sorted({str(r.get("title", "")).strip() for r in catalog if r.get("title")}, key=len, reverse=True)
    )


#: 既知の曲タイトル（正本カタログの和集合）。長い順。
_KNOWN_SONG_TITLES: Tuple[str, ...] = _load_known_song_titles()

# 引用が曲名の主張かは :func:`eval.metrics.fact_score.is_song_reference` が
# 単一の判定点。ここに二重実装しない（fact score と曲名一致率で規則を揃える）。


@dataclass(frozen=True)
class SongMention:
    """原稿中に現れた 1 件の曲名の主張。"""

    title: str
    artist: Optional[str]
    matched: bool
    sentence: str

    def __str__(self) -> str:  # pragma: no cover - 表示のみ
        who = f"（{self.artist}）" if self.artist else ""
        flag = "一致" if self.matched else "不一致"
        return f"{flag}: {self.title}{who} / {self.sentence}"


@dataclass(frozen=True)
class SongMatchResult:
    """曲名一致率の結果。"""

    mentions: Tuple[SongMention, ...] = ()
    #: 照合先として使った集合の名前（記録用）。
    #: 既定は :func:`_resolve_allowed` が正本カタログを指すときの名前。
    #: 以前は ``static-master`` だったが、照合先は
    #: :func:`_load_known_song_titles` が読む正本カタログであり、
    #: 別の静的マスターは参照されていない。ラベルが実態とずれると
    #: レポートの読み手が「何と照合したのか」を誤読する。
    source: str = "catalog"

    @property
    def total(self) -> int:
        return len(self.mentions)

    @property
    def matched(self) -> Tuple[SongMention, ...]:
        return tuple(m for m in self.mentions if m.matched)

    @property
    def unmatched(self) -> Tuple[SongMention, ...]:
        return tuple(m for m in self.mentions if not m.matched)

    @property
    def applicable(self) -> bool:
        """一致率を**定義できる**か = 曲名の主張が 1 件以上あるか。

        言及 0 件のとき :attr:`rate` は 1.0 を返すが、それは
        「全部一致した」からではなく**分母が無い**からである。
        黙って 100% として扱わず、:attr:`ChecklistResult` 経由で
        「適用外（N/A）」として報告する。
        """
        return self.total > 0

    @property
    def rate(self) -> float:
        """一致率（0.0〜1.0）= ``一致した曲名の主張 / 全曲名の主張``。

        定義できないときは 1.0 を返す（言及なしは不一致ではないという
        CheckList 項目 c の線引きによる）。ただしその場合は
        :attr:`applicable` が ``False` であり、
        ``describe()`` は ``N/A`` を出す。**1.0 = 満点ではない**ことに注意。
        """
        if not self.mentions:
            return 1.0
        return len(self.matched) / len(self.mentions)

    @property
    def duplicates(self) -> Tuple[Tuple[str, int], ...]:
        """1 番組内で 2 回以上言及された曲（曲名, 回数）。"""
        counter = Counter(m.title for m in self.mentions)
        return tuple(sorted((t, c) for t, c in counter.items() if c >= 2))

    def describe(self) -> str:
        if not self.applicable:
            return "曲名一致率=N/A（曲名の主張 0 件 / 照合先=%s）" % self.source
        return (
            f"曲名一致率={self.rate * 100:.1f}% "
            f"(一致 {len(self.matched)}/{self.total}, 照合先={self.source})"
        )


def _resolve_allowed(allowed_titles: Optional[Iterable[str]]) -> Tuple[set, str]:
    """照合先の集合と、その由来を表す名前。"""
    if allowed_titles is None:
        return set(_KNOWN_SONG_TITLES), "catalog"
    return {str(t) for t in allowed_titles}, "caller"


def extract_song_mentions(
    script: str, *, allowed_titles: Optional[Iterable[str]] = None
) -> List[SongMention]:
    """原稿中の曲名の主張を抽出する。

    Parameters
    ----------
    script:
        対象原稿。
    allowed_titles:
        照合先の曲名集合。``None`` なら静的マスター。

    Returns
    -------
    list[SongMention]
        出現順に並んだ曲名の主張。
    """
    allowed, _ = _resolve_allowed(allowed_titles)
    mentions: List[SongMention] = []

    for sentence in split_sentences(script):
        for match in _QUOTED.finditer(sentence):
            title = (match.group(1) or "").strip()
            artist = (match.group(2) or "").strip() or None
            if not title or title in _PROGRAM_TITLES:
                continue
            if title not in _KNOWN_SONG_TITLES and not is_song_reference(sentence, match):
                # クイズのヒント・答えはここに来ない（曲名と誤認しない）
                continue
            mentions.append(
                SongMention(
                    title=title,
                    artist=artist,
                    matched=title in allowed,
                    sentence=sentence,
                )
            )
    return mentions


def song_match_rate(
    script: str,
    year: Optional[int] = None,
    *,
    allowed_titles: Optional[Iterable[str]] = None,
) -> SongMatchResult:
    """台本に言及された曲名が照合先に含まれる割合を測る。

    Parameters
    ----------
    script:
        対象原稿。
    year:
        対象年。**照合には使わない**（中立性を保つため）。
        S3 が「その年の選曲」を渡す場合は ``allowed_titles`` で渡すこと。
    allowed_titles:
        照合先の曲名集合。``None`` なら静的マスター（既定）。
        S3 実装後は実際の選曲結果をここに渡す。

    Returns
    -------
    SongMatchResult
    """
    allowed, source = _resolve_allowed(allowed_titles)
    mentions = extract_song_mentions(script, allowed_titles=allowed)
    return SongMatchResult(mentions=tuple(mentions), source=source)


def known_song_titles() -> Tuple[str, ...]:
    """静的マスターが知っている曲名（長い順）。"""
    return _KNOWN_SONG_TITLES


__all__ = [
    "SongMention",
    "SongMatchResult",
    "extract_song_mentions",
    "known_song_titles",
    "song_match_rate",
]
