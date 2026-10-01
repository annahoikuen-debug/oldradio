"""FactScore の簡易実装（Kwong et al. 2023 / Pagnoni et al. 2021 相当）。

## 何を測るか

長文原稿を**原子的事実**に分解し、外部ソース（= S1 の事実レジストリ
``retro_radio/core/facts/programs.json``）と 1 件ずつ照合して
「支持された主張の割合」を 0〜100 で返す。 originals である
FactScore が数えるのは *output* の原子的事実が source で支持されるかであり、
本実装もそれだけを測る（source の網羅率 = coverage は別指標）。

## fail と warn の線引き（S1 の validator と一致させる）

`docs/facts_registry.md` 5.3 が決めた線引きを**ここでも維持する**:

- **番組表**由来の違反は ``fail``（生成源がレジストリだけなので機械的に直せる）。
- **台本の自由文**由来の違反は ``warn``（``REMINISCENCE_DATA`` のクイズや LLM 自由生成など
  レジストリ外のデータも混ざっており、fail にすると所有範囲外の修正を要求してしまう）。

したがって本モジュールは:

| 違反 | level | 根拠 |
|---|---|---|
| レジストリに無い番組名の断定 | ``fail`` | S1 の対応する fail 相当（``source`` 欠落と同じ扱い） |
| 対象年に入らない番組の言及 | ``warn`` | ``stale-fact-in-script`` と同じ線引き |
| 対象年より後の年・年代表記 | ``warn`` | ``future-year-in-script`` と同じ線引き |
| 対象年より後にリリースされた曲 | ``warn`` | 同上（曲は播出年を持つので同じ扱い） |

**ゲートを落とすのは ``fail`` だけ**（:meth:`FactScoreResult.is_gate_ok`）。
既存の warn 5 件（`docs/facts_registry.md` 191〜198 行）はゲートを落とさない。

## 検出できないもの（限界の明記）

1. **レジストリに載っていない完全な架空の番組名**は、名前が既知でなければ
   「番組の主張」であることが分からないため検出できない。検出できるのは
   「既知の正規タイトルが対象年の内容と整合するか」までである。
   これは S1 の validator と同じ限界であり、**正本（レジストリ）を増やす
   ことでのみ解消する**。捏造の検出のためにここでデータを作らない。
2. **歌詞・情景描写**（"夕暮れの空の色"）は検証不能なので分母に入れない
   （``kind == "atmosphere"``）。これは「水増しを罰する」のではなく
   **「水増しを fact score の分母に載せない」**ための設計である。
3. 知識ベース照合（Citation Verification 型の NLI）は**行っていない**。
   ネットアクセスがないためであり、照合先はレジストリに閉じてある。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - 直接実行時の保険
    sys.path.insert(0, str(_REPO_ROOT))

from retro_radio.core.facts import future_year_mentions, load_facts
from scripts.validate_facts import END_YEAR, build_fact_table

#: CI ゲートの既定下限。**ベースラインが確立するまでは観測用**であり、
#: プロンプト変更で ±2 点は意味を持たない（提案 489 行）。
DEFAULT_FACT_SCORE_THRESHOLD = 80.0

# --- 原子的事実の分解 -------------------------------------------------------
#: 文の切れ目。句点・改行で区切る（Pagnoni et al. 2021 の文単位分解に相当）。
_SENTENCE_SPLIT = re.compile(r"[。．！？!?]+|\n+")
#: 見出し行（``### オープニング``）
_HEADING = re.compile(r"^\s*#{1,6}\s*")
#: 4 桁の西暦
_YEAR = re.compile(r"(1[5-9]\d{2}|20\d{2})")
#: 「○年代」
_DECADE = re.compile(r"(1[89]\d\d|20\d{2})\s*年代")
#: 引用符で囲まれた固有名（``「番組名」（19:30 放送開始）`` の形を含む）
_QUOTED = re.compile(r"「([^」]{1,40})」\s*(?:（([^）]{1,40})）)?")
#: 「曲」「歌」「メロディ」を含む文中の引用は曲名の主張とみなす
#:
#: **文字クラスではなく選択肢**であること。`[曲歌メロディ]` と書くと
#: `メ` `ロ` `デ` `ィ` の 4 文字がカタカナ単体で一致し、`レトロラジオ` の
#: `ロ` や `空手チョップ…プロレスラー` の `ロ` のような**回想法のヒントまで**
#: 曲名の主張と誤検出していた（実測: 24 ケースの本文で 49 文が該当）。
_SONG_CONTEXT = re.compile(r"(?:[曲歌]|メロディ)")
#: 引用の直前に置かれる曲 foreshadow の語（``懐かしい名曲「X」`` の形）
_SONG_PREFIX = re.compile(r"(?:名曲|ヒット曲|歌|曲|メロディ)\s*$")
#: 節目の通った固有名は「番組名の主張」とみなす（捏造の検出に使う）
_PROGRAM_NAME_SHAPE = re.compile(r"(?:番組|チャンネル|放送局|テレビ局|ラジオ局)$")

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: 主張の種類
KIND_PROGRAM = "program"
KIND_FABRICATED = "fabricated_program"
KIND_YEAR = "year"
KIND_DECADE = "decade"
KIND_SONG = "song"
KIND_ATMOSPHERE = "atmosphere"

#: 文から記号だけが残ったとき（実質空文）とみなす文字
_TRIM = "　 \t、。「」『』（）()・—,.!?！？"


@dataclass(frozen=True)
class AtomicClaim:
    """原稿から抽出した 1 件の原子的事実。"""

    text: str
    kind: str
    level: str
    detail: str = ""

    @property
    def supported(self) -> bool:
        """外部ソースで支持されているか。"""
        return self.level == OK

    def __str__(self) -> str:  # pragma: no cover - 表示のみ
        return f"[{self.level}] {self.kind}: {self.detail} / {self.text}"


@dataclass(frozen=True)
class FactScoreResult:
    """FactScore の結果。"""

    year: int
    score: float
    supported: int
    checkable: int
    claims: Tuple[AtomicClaim, ...]
    expected_facts: Tuple[str, ...] = ()
    mentioned_expected_facts: Tuple[str, ...] = ()

    # --  Shorthand  ---------------------------------------------------------
    @property
    def unsupported(self) -> Tuple[AtomicClaim, ...]:
        """支持されていない主張（warn / fail 両方）。"""
        return tuple(c for c in self.claims if not c.supported)

    @property
    def failures(self) -> Tuple[AtomicClaim, ...]:
        """``fail`` レベルの主張のみ。**これがゲートを落とす**。"""
        return tuple(c for c in self.claims if c.level == FAIL)

    @property
    def warnings(self) -> Tuple[AtomicClaim, ...]:
        """``warn`` レベルの主張のみ。ゲートは落とさない（S1 の線引き）。"""
        return tuple(c for c in self.claims if c.level == WARN)

    @property
    def applicable(self) -> bool:
        """FactScore を**定義できる**か。

        検証可能な原子的事実（``kind != atmosphere``）が 1 件も無い場合、
        ``score`` は 0.0 になるが、それは「何も検証できなかった」のであって
        「全部間違えた」ではない。**黙って 0 点で通すのではなく**
        「適用外（N/A）」として :attr:`coverage_applicable` -DD と併用して
        報告させる。
        """
        return self.checkable > 0

    @property
    def coverage(self) -> float:
        """正解テーブルに載っている事実のうち、台本に言及した割合（0〜100）。

        FactScore ではなく**別指標**であり、FactScore へ混ざない。

        定義: ``100 * (台本に言及した正解 ID の数) / (その年の正解 ID の総数)``。
        値が大きいほど「その年の番組を落としている」。

        **限界の明記**: 正本には放送開始年しか載っていないため、これは
        「対象年を开播年とする番組が台本に何件出たか」の上界である。
        期間中の改名・後継番組を独立した事実として数えられるかは
        レジストリの粒度に依存する。したがって **ガードレール用の補助値**として
        扱い、ゲート条件にはしない。:attr:`coverage_applicable` が ``False``
        （対象年の正解が 0 件）のときは算出不能。
        """
        if not self.expected_facts:
            return 0.0
        return 100.0 * len(self.mentioned_expected_facts) / len(self.expected_facts)

    @property
    def coverage_applicable(self) -> bool:
        """coverage を算出できたか（対象年の正解テーブルが空なら ``False``）。"""
        return bool(self.expected_facts)

    def is_gate_ok(self, threshold: float = DEFAULT_FACT_SCORE_THRESHOLD) -> bool:
        """PR ゲートを通過するかどうか。

        ``fail`` が 1 件もなく、かつスコアが ``threshold`` 以上なら ``True``。
        ``warn`` は**ゲートを落とさない**（S1 の warn 5 件を壊さないため）。
        """
        return not self.failures and self.score >= threshold

    def describe(self) -> str:
        """失敗時に読むための 1 行サマリ。"""
        return (
            f"fact_score={self.score:.1f} "
            f"(支持 {self.supported}/{self.checkable}, "
            f"fail {len(self.failures)}, warn {len(self.warnings)}, "
            f"coverage={self.coverage:.1f}%)"
        )


# ---------------------------------------------------------------------------
# 外部ソース（レジストリ）の索引
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _registry_index() -> Dict[str, object]:
    """全レコードからタイトル → 放送期間の索引を作る（1 回だけ構築）。"""
    periods: Dict[str, List[Tuple[int, int]]] = {}
    for record in load_facts():
        low = int(record["valid_from"])
        valid_to = record.get("valid_to")
        high = int(valid_to) if isinstance(valid_to, int) else END_YEAR
        periods.setdefault(str(record["title"]), []).append((low, high))
    # 長いタイトルから先に探す（部分一致の誤検出を避ける）
    ordered = sorted(periods, key=len, reverse=True)
    return {"periods": periods, "ordered_titles": tuple(ordered)}


@lru_cache(maxsize=1)
def _song_index() -> Tuple[Dict[str, int], Dict[str, int]]:
    """正本カタログから曲名 → (最も早いリリース年, 出現回数) の索引。

    hermetic を保つため、**読み込み失敗時は空索引へ縮退**する
    （検証が緩くなるだけで、誤検出は増えない）。
    """
    from retro_radio.core.songs import load_songs

    years: Dict[str, int] = {}
    counts: Dict[str, int] = {}
    try:
        catalog = load_songs()
    except Exception:  # noqa: BLE001 - カタログが読めない場合は検証を縮退
        return years, counts
    for record in catalog:
        title = str(record.get("title", "")).strip()
        if not title:
            continue
        released = int(record.get("release_year") or 0)
        if title not in years or (0 < released < years[title]):
            years[title] = released
        counts[title] = counts.get(title, 0) + 1
    return years, counts


def known_song_titles() -> Dict[str, int]:
    """静的マスターが知っている曲名 → 最も早いリリース年。"""
    years, _ = _song_index()
    return years


def registry_titles() -> Tuple[str, ...]:
    """レジストリが知っている番組名のタプル（長い順）。"""
    return _registry_index()["ordered_titles"]  # type: ignore[return-value]


def is_valid_program(title: str, year: int) -> bool:
    """``title`` が ``year`` に放送されていたか。"""
    table = build_fact_table(year)
    return any(str(r["title"]) == title for r in table)


# ---------------------------------------------------------------------------
# 原子的事実への分解
# ---------------------------------------------------------------------------
def split_sentences(script: str) -> List[str]:
    """原稿を文（= 原子的事実の候補単位）に分解する。

    **見出し行（``### オープニング``）は内容ではないため、行ごと落とす**
    （見出し語の「オープニング」だけを文として数えると、主張ではないものを
    原子的事実に数えてしまう）。空行・記号だけの文も落とす。
    """
    sentences: List[str] = []
    for raw in (script or "").splitlines():
        line = raw.strip()
        if not line or _HEADING.match(line):
            continue
        for chunk in _SENTENCE_SPLIT.split(line):
            piece = chunk.strip()
            if not piece or piece.strip(_TRIM) == "":
                continue
            sentences.append(piece)
    return sentences


def is_song_reference(sentence: str, match: "re.Match[str]") -> bool:
    """引用が「曲名」の主張かを判定する。

    引用符 ``「…」`` はこのアプリでは曲名に限らない（クイズのヒント・答えも
    引用される）。したがって次の 3 条件のいずれかを満たすときだけ曲名とみなす:

    1. 静的マスターに載っている（呼び出し側で先に判定する）。
    2. 直後に ``（歌手）`` が付き、かつその文に「曲」「歌」「メロディ」を含む。
    3. 直前の語が ``名曲`` / ``ヒット曲`` / ``歌`` / ``曲`` / ``メロディ``。

    Parameters
    ----------
    sentence:
        引用を含む文。
    match:
        :data:`_QUOTED` のマッチ結果（``group(1)`` = 引用内容、
        ``group(2)`` = 括弧内の歌手名）。

    Returns
    -------
    bool
    """
    artist = (match.group(2) or "").strip()
    prefix = sentence[: match.start()]
    if artist and _SONG_CONTEXT.search(sentence):
        return True
    return bool(_SONG_PREFIX.search(prefix))


def _mentioned_titles(sentence: str) -> List[str]:
    """文中に現れる既知の正規タイトル（長い順、重複なし）。"""
    hits: List[str] = []
    for title in registry_titles():
        if title in sentence and title not in hits:
            hits.append(title)
    return hits


def extract_atomic_claims(
    script: str,
    year: int,
    *,
    source_titles: Optional[Iterable[str]] = None,
    allowed_song_titles: Optional[Iterable[str]] = None,
) -> List[AtomicClaim]:
    """原稿を原子的事実のリストに分解する（判定は :func:`fact_score` が行う）。

    Parameters
    ----------
    script:
        対象原稿。
    year:
        対象年。正当性の判定に使う。
    source_titles:
        正解のタイトル集合。省略時は :func:`scripts.validate_facts.build_fact_table`
        （S1 の ``build_fact_table``）をその場で呼ぶ。
    allowed_song_titles:
        **その番組の選択枠に入る曲名**の集合（`retro_radio.core.songs.pool_for_year`
        が返すもの）。ここに含まれる曲名は、リリース年が対象年より後でも
        正当な主張として扱う。``None`` のときは選択枠を見ず、リリース年のみで
        判定する（従来の厳格な挙動）。
    """
    valid = set(source_titles) if source_titles is not None else {
        str(r["title"]) for r in build_fact_table(year)
    }
    allowed = set(allowed_song_titles) if allowed_song_titles is not None else None
    all_titles = set(registry_titles())
    song_years = known_song_titles()
    periods = _registry_index()["periods"]  # type: ignore[index]

    claims: List[AtomicClaim] = []

    for sentence in split_sentences(script):
        for title in _mentioned_titles(sentence):
            if title in valid:
                claims.append(
                    AtomicClaim(sentence, KIND_PROGRAM, OK, f"対象年（{year}）に放送された番組: {title}")
                )
            else:
                spans = periods.get(title, [])
                span_text = "/".join(f"{lo}-{hi}" for lo, hi in spans)
                claims.append(
                    AtomicClaim(
                        sentence,
                        KIND_PROGRAM,
                        WARN,
                        f"対象年（{year}）に放送されていない番組（正本上の期間 {span_text}）: {title}",
                    )
                )

        for token in dict.fromkeys(_YEAR.findall(sentence)):
            value = int(token)
            if value > year:
                claims.append(
                    AtomicClaim(
                        sentence, KIND_YEAR, WARN, f"対象年より後の年: {value}年"
                    )
                )
            else:
                claims.append(AtomicClaim(sentence, KIND_YEAR, OK, f"対象年以前の年: {value}年"))

        for token in dict.fromkeys(_DECADE.findall(sentence)):
            value = int(token)
            if value > year:
                claims.append(
                    AtomicClaim(
                        sentence, KIND_DECADE, WARN, f"対象年より後の年代表記: {value}年代"
                    )
                )

        for match in _QUOTED.finditer(sentence):
            span = match.group(1).strip()
            if not span or span in all_titles:
                # 既知の番組名は上で処理済み
                continue
            if span in song_years:
                released = song_years[span]
                # 判定は「リリース年が対象年以下か」**ではない**。
                # 本番の選曲には、正本のカタログに該当年の曲が無いときに
                # 隣接年（最大 10 年幅）の曲も入れる方針がある
                # （`retro_radio.core.songs.pool_for_year` が空にしない）。
                # その方針を**原稿の欠陥として罰する**と、
                # カタログが薄い年の分だけ script quality が下がってしまい、
                # eval はカタログの充足率を測ってしまうことになる。
                #
                # したがって判定基準は「**その番組の選択枠に入るか**」。
                #
                #   選択枠内            -> OK   （その番組で鳴らしうる）
                #   選択枠外 / 後年の曲 -> FAIL （その番組では鳴らせない）
                #
                # 後者を FAIL にする理由: `warn` はこのモジュールの契約により
                # **ゲートを落とさない**（`warnings` の docstring）。
                # 1 件の時代錯誤が 9 件中の 1 件なら score は 88.89 に
                # なり、閾値 80 を超えて**素通り**していた（実測）。
                # 「その番組では鳴らせない曲名を断定している」は
                # 存在しない番組名を断定するのと既然（一）で、
                # ゲートを落とす价值的がある。
                if allowed is None or span in allowed:
                    level = OK
                    detail = f"正本カタログの曲（選択枠内 / リリース {released} 年）: {span}"
                elif released > year:
                    level = FAIL
                    detail = (
                        f"その番組の選択枠に無く、対象年より後の曲: "
                        f"{span}（{released} 年 / 対象 {year} 年）"
                    )
                else:
                    level = WARN
                    detail = f"その番組の選択枠に無い曲: {span}（{released} 年）"
                claims.append(AtomicClaim(sentence, KIND_SONG, level, detail))
                continue
            if _PROGRAM_NAME_SHAPE.search(span):
                claims.append(
                    AtomicClaim(
                        sentence,
                        KIND_FABRICATED,
                        FAIL,
                        f"正本に存在しない番組名を断定しています: 「{span}」",
                    )
                )
                continue
            if _SONG_CONTEXT.search(sentence) and len(span) <= 40:
                claims.append(
                    AtomicClaim(sentence, KIND_SONG, WARN, f"正本カタログに無い曲名: 「{span}」")
                )

        if not _mentioned_titles(sentence) and not _YEAR.search(sentence) and not _DECADE.search(
            sentence
        ):
            claims.append(AtomicClaim(sentence, KIND_ATMOSPHERE, OK, "検証対象の根拠なし（情景描写）"))

    return claims


# ---------------------------------------------------------------------------
# 公開エントリポイント
# ---------------------------------------------------------------------------
def fact_score(
    script: str, year: int, *, allowed_song_titles: Optional[Iterable[str]] = None
) -> FactScoreResult:
    """原稿の FactScore（0〜100）を返す。

    Parameters
    ----------
    script:
        対象原稿。
    year:
        対象年。正当性の判定に使う。
    allowed_song_titles:
        **その番組の選択枠に入る曲名**。省略時は選択枠を見ず、
        リリース年が対象年以下かどうかだけで曲名を判定する。

    Returns
    -------
    FactScoreResult
        ``score`` は ``100 * supported / checkable``。
        **検証可能な主張が 1 件も無い場合は 0.0**（何も語っていない原稿は
        事実として何も言っていないため、高く評価しない）。
    """
    table = build_fact_table(year)
    expected_ids = tuple(str(r["id"]) for r in table)
    valid_titles = {str(r["title"]) for r in table}

    claims = extract_atomic_claims(
        script, year, source_titles=valid_titles, allowed_song_titles=allowed_song_titles
    )
    checkable = [c for c in claims if c.kind != KIND_ATMOSPHERE]
    supported = [c for c in checkable if c.supported]
    score = (100.0 * len(supported) / len(checkable)) if checkable else 0.0

    # coverage は「その年のタイトル集合と台本の交集わり」から作る（別指標）。
    mentioned_titles = valid_titles & set(_mentioned_titles(script))
    mentioned = tuple(
        rid for rid, rec in zip(expected_ids, table) if str(rec["title"]) in mentioned_titles
    )

    return FactScoreResult(
        year=year,
        score=round(score, 2),
        supported=len(supported),
        checkable=len(checkable),
        claims=tuple(claims),
        expected_facts=expected_ids,
        mentioned_expected_facts=mentioned,
    )


def future_year_violations(script: str, year: int) -> List[str]:
    """対象年より後の年・年代表記の一覧（S1 の :func:`future_year_mentions` を再利用）。"""
    return list(future_year_mentions(script or "", year))


__all__ = [
    "DEFAULT_FACT_SCORE_THRESHOLD",
    "FAIL",
    "KIND_ATMOSPHERE",
    "KIND_DECADE",
    "KIND_FABRICATED",
    "KIND_PROGRAM",
    "KIND_SONG",
    "KIND_YEAR",
    "OK",
    "WARN",
    "AtomicClaim",
    "FactScoreResult",
    "extract_atomic_claims",
    "fact_score",
    "future_year_violations",
    "is_song_reference",
    "is_valid_program",
    "known_song_titles",
    "registry_titles",
    "split_sentences",
]

if __name__ == "__main__":  # pragma: no cover - 手動実行
    from retro_radio.core.fallback import generate_care_script, generate_fallback_script

    for label, builder in (("normal", generate_fallback_script), ("care", generate_care_script)):
        for probe_year in (1950, 1964, 1975, 1985, 1995, 2005, 2015, 2025):
            result = fact_score(builder(probe_year, 5, 15), probe_year)
            print(f"{label:>6}/{probe_year}: {result.describe()}")
