"""個人音楽プロファイルと「親和性の検証」（提案② タスク5/6、S3）。

## 何を扱うか

このアプリの価値は「その人が**自分の**青春の音を聞くこと」だが、提案② 0-3 の
通り現行の選曲はその半分を壊している。ここでは**利用者ごとの好み**を表す
データモデルと、その選曲への反映（スコアリング）と、既知性の確認
（familiarity 検査）を純関数として用意する。

## 設計上の制約（本モジュールが守るもの）

1. **永続化を行わない。** DB には書かない。:class:`MusicProfileRepository` は
   ``typing.Protocol`` による**抽象**だけで、実装は持たない。
   実装は **S4（提案⑧ 認証）** の担当。``docs/music_profile.md`` に
    要求されるフィールドとテーブル設計の案を記した。
2. **プロファイルが空なら、既存の年代パレットにフォールバックする。**
   新規利用者の既定値は「既存の挙動」でなければならない。
   提案② は「機能改善」ではなく**安全要件**（source monitoring error の回避）
   として扱うため、既存挙動を壊してはならない。
3. **1 セッション 1 問。** 親和性チェックは心理的負荷を最小化するため
   1 セッションにつき 1 問だけとする（Groarke et al. 2018 の familiarity
   検査に対応）。:func:`build_familiarity_question` は 2 問目を要求されると
   例外を送出する（型と関数の両方で強制する）。

## スコアの根拠 [Tier D]

``score = 1.0 * familiarity + teenage_match - 2.0 * recency_years``

**この係数（1.0 / 1.0 / 2.0）は設計仮説であり、根拠は未検証である。**
記憶研究（Levy et al. 2013 / Brown et al. 2013）は
「思い出した音楽」と「実際に再生された音楽」が一致しないことを示しているが、
**具体的な重み付けを導いたものではない**。施設側で A/B して決めること。
値が確定したら、この定数だけを差し替えれば済む。

標準ライブラリのみ（DB / ネットワークに依存しない）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

# --- スコアの係数 [Tier D] --------------------------------------------------------
#: 既知性（familiarity 1〜5）の係数。
WEIGHT_FAMILIARITY = 1.0
#: 「利用者が 10〜20 代だった年代」に一致したときの加点。
WEIGHT_TEENAGE = 1.0
#: 最終再生からの経過年数の係数。**減点側**。
WEIGHT_RECENCY = 2.0

#: 1 セッションで許される親和性チェックの数。**1 に固定**（ Groarke 2018）。
MAX_FAMILIARITY_QUESTIONS_PER_SESSION = 1

#: familiarity の下限・上限。
MIN_FAMILIARITY = 1
MAX_FAMILIARITY = 5

#: 利用者が 10〜20 代だったと推定する年齢帯。
TEENAGE_AGE_RANGE = range(10, 21)


class Reaction:
    """利用者反応（肯定・中立・否定）。"""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"

    ALL = (POSITIVE, NEUTRAL, NEGATIVE)


class TooManyQuestionsError(ValueError):
    """1 セッションで 2 問以上を要求されたときに送出する。"""


@dataclass(frozen=True)
class FavoriteTrack:
    """favorite 1 曲。**不変**（更新は :func:`replace` で行う）。

    Attributes
    ----------
    title, artist:
        曲名とアーティスト名。静的一 master の表記に揃える。
    familiarity_score:
        1〜5 の既知性。5 が最も熟悉。
    last_played_at:
        最終再生時刻（timezone 付き aware）。未再生なら ``None``。
    reaction:
        :class:`Reaction` のいずれか。
    """

    title: str
    artist: str = ""
    familiarity_score: int = 3
    last_played_at: Optional[datetime] = None
    reaction: str = Reaction.NEUTRAL

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("title は空にできない")
        if not (MIN_FAMILIARITY <= self.familiarity_score <= MAX_FAMILIARITY):
            raise ValueError(
                "familiarity_score は %d〜%d の範囲（actual=%s）"
                % (MIN_FAMILIARITY, MAX_FAMILIARITY, self.familiarity_score)
            )
        if self.reaction not in Reaction.ALL:
            raise ValueError("reaction は %s のいずれか" % (Reaction.ALL,))

    @property
    def key(self) -> Tuple[str, str]:
        """静的マスターと突き合わせるためのキー。"""
        return (self.title, self.artist)

    def with_familiarity(self, value: int) -> "FavoriteTrack":
        """familiarity だけ差し替えたコピーを返す。"""
        return replace(self, familiarity_score=value)


@dataclass(frozen=True)
class MusicProfile:
    """利用者（施設ではグループ単位でも可）の音楽プロファイル。**不変**。"""

    #: 利用者を識別する ID（S4 がセッションと結びつける）。
    owner_id: str = ""
    #: favorite の曲。順序は意味を持たない。
    favorite_tracks: Tuple[FavoriteTrack, ...] = ()
    #: 利用者が 10〜20 代だった年代（例: ``(1970, 1980)``）。
    teenage_decades: Tuple[int, ...] = ()
    #: 施設側でグループ共有するための任意キー。
    group_id: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        """**プロファイルが空**か（= 既存年代パレットにフォールバックすべきか）。"""
        return not self.favorite_tracks and not self.teenage_decades

    def track(self, title: str, artist: str = "") -> Optional[FavoriteTrack]:
        """(曲名, アーティスト) から favorite を引く。無ければ ``None``。"""
        for item in self.favorite_tracks:
            if item.title == title and (not artist or item.artist == artist):
                return item
        return None

    def with_tracks(self, tracks: Sequence[FavoriteTrack]) -> "MusicProfile":
        return replace(self, favorite_tracks=tuple(tracks))

    def with_reduced_familiarity(self, title: str, artist: str = "") -> "MusicProfile":
        """「いいえ」= familiarity を 1 減算したプロファイルを返す。

        下限は :data:`MIN_FAMILIARITY`。既に下限なら変化しない（0 未満にならない）。

        Parameters
        ----------
        title, artist:
            対象の曲。``artist`` 空なら曲名だけで照合する。

        Returns
        -------
        MusicProfile
            差し替え後の**新しい**プロファイル。元のオブジェクトは変更しない。
        """
        updated: List[FavoriteTrack] = []
        changed = False
        for item in self.favorite_tracks:
            if item.title == title and (not artist or item.artist == artist):
                if item.familiarity_score > MIN_FAMILIARITY:
                    updated.append(item.with_familiarity(item.familiarity_score - 1))
                    changed = True
                    continue
            updated.append(item)
        if not changed:
            return self
        return self.with_tracks(updated)

    def rejects(self, title: str, artist: str = "") -> bool:
        """「該当の回を除外」すべきか（familiarity が下限に達したか）。"""
        item = self.track(title, artist)
        return item is not None and item.familiarity_score <= MIN_FAMILIARITY


@runtime_checkable
class MusicProfileRepository(Protocol):
    """プロファイルの永続化_IF。**S4 が実装する**（本モジュールは持たない）。

    実装時に満たすべき契約:

    * :meth:`get` は**プロファイルが無い利用者に対して空プロファイルを
      返さなければならない**（``None`` を返すと呼び出し側が
      「データが無い」錯誤と区別できなくなる）。:attr:`MusicProfile.is_empty`
      がそのまま「年代パレットへフォールバック」の判定になる。
    * :meth:`record_rejection` は familiarity を 1 減算する副作用を
      **原子的に**行う。読み取り→書き込みの 2 手順にしてはいけない
      （同じ利用者へ同時に 2 リクエストが来ると減算が失われる）。
    """

    def get(self, owner_id: str) -> MusicProfile:
        """``owner_id`` のプロファイル（無ければ空プロファイル）を返す。"""
        ...

    def save(self, profile: MusicProfile) -> None:
        """プロファイル全体を保存する（上書き）。"""
        ...

    def record_play(
        self,
        owner_id: str,
        title: str,
        artist: str,
        played_at: Optional[datetime] = None,
    ) -> None:
        """再生を履歴に記録する（familiarity の更新と最終再生時刻の更新）。"""
        ...

    def record_rejection(self, owner_id: str, title: str, artist: str = "") -> None:
        """「いいえ」を記録する（familiarity を 1 減算）。**原子的に**行うこと。"""
        ...


# --- 選曲スコアリング ------------------------------------------------------------
def _elapsed_years(since: Optional[datetime], now: datetime) -> float:
    """``since`` から ``now`` までの経過年数（未再生なら 0.0）。"""
    if since is None:
        return 0.0
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    delta = now - since
    return max(0.0, delta.total_seconds() / (365.25 * 24 * 3600))


def teenage_match(profile: MusicProfile, release_year: Optional[int]) -> float:
    """その曲が「利用者が 10〜20 代だった年代」に属するかを 0.0/1.0 で返す。

    ``teenage_decades`` が空なら判定しない（0.0）。年代が 10 年刻みで
    記録されていることを前提に、曲のリリース年から 10 年バケットを引く。
    """
    if not profile.teenage_decades or release_year is None:
        return 0.0
    decade = (release_year // 10) * 10
    return 1.0 if decade in profile.teenage_decades else 0.0


def selection_score(
    track: FavoriteTrack,
    profile: MusicProfile,
    release_year: Optional[int] = None,
    *,
    now: Optional[datetime] = None,
) -> float:
    """favorite 1 曲の選曲スコアを返す（大きいほど優先）。

    ``1.0 * familiarity + teenage_match - 2.0 * 経過年数``

    .. warning::
       **係数は設計仮説 [Tier D] で根拠は未検証。** 施設側の A/B で決めること。
    """
    moment = now or datetime.now(timezone.utc)
    return (
        WEIGHT_FAMILIARITY * track.familiarity_score
        + teenage_match(profile, release_year)
        - WEIGHT_RECENCY * _elapsed_years(track.last_played_at, moment)
    )


def rank_by_familiarity(
    candidates: Sequence[FavoriteTrack],
    profile: MusicProfile,
    release_years: Optional[Dict[Tuple[str, str], int]] = None,
    *,
    now: Optional[datetime] = None,
) -> List[FavoriteTrack]:
    """候補をスコア降順に並べる（**安定ソート**）。

    Parameters
    ----------
    candidates:
        並べ替える favorite の列。
    profile:
        ``teenage_decades`` の供給元。
    release_years:
        ``(曲名, アーティスト) -> リリース年`` の対応表。無ければ
        teenage 判定は行わない。
    now:
        経過年数の基準時刻（決定的テストのため差し込める）。既定は UTC 現在。

    Notes
    -----
    **プロファイルが空なら入力順をそのまま返す**（= 既存挙動を維持）。
    """
    if profile.is_empty:
        return list(candidates)
    years = release_years or {}
    moment = now or datetime.now(timezone.utc)
    return sorted(
        candidates,
        key=lambda t: -selection_score(
            t, profile, years.get(t.key), now=moment
        ),
    )


def select_by_profile(
    year: int,
    profile: MusicProfile,
    count: int = 3,
    release_years: Optional[Dict[Tuple[str, str], int]] = None,
) -> List[Tuple[str, str]]:
    """プロファイルから選曲する。**空なら既存の年代パレットにフォールバック**。

    Parameters
    ----------
    year:
        対象年。
    profile:
        利用者のプロファイル。
    count:
        必要曲数。
    release_years:
        静的マスターのリリース年対応表。

    Returns
    -------
    list[tuple[str, str]]
        ``(曲名, アーティスト)`` の列。
        **プロファイルが空なら :func:`retro_radio.core.fallback.
        select_program_songs` の結果にそのまま委ねる**（既存挙動）。
    """
    if profile.is_empty:
        from .fallback import select_program_songs  # 遅延 import（循環を避ける）

        return select_program_songs(year, count)

    # 否定された（familiarity が下限の）曲子を除外する。
    usable = [t for t in profile.favorite_tracks if not profile.rejects(*t.key)]
    ranked = rank_by_familiarity(usable, profile, release_years)
    selected: List[Tuple[str, str]] = []
    seen = set()
    for track in ranked:
        if track.key in seen:
            continue
        seen.add(track.key)
        selected.append(track.key)
        if len(selected) >= count:
            break

    if not selected:
        # 全滅したら既存パレット（安全側）。
        from .fallback import select_program_songs  # 遅延 import

        return select_program_songs(year, count)
    return selected[:count]


# --- 親和性チェック（familiarity 検査）-------------------------------------------
@dataclass(frozen=True)
class FamiliarityQuestion:
    """既知性を確認する 1 問。**1 セッション 1 問**。"""

    #: 対象の曲。
    track: FavoriteTrack
    #: 読み上げ用の問いかけ（例: 「この曲、聴きますか？」）。
    prompt: str = "この曲、聴きますか？"
    #: セッション ID。同一セッションで 2 問目を拒否するために使う。
    session_id: str = ""


def build_familiarity_question(
    track: FavoriteTrack,
    session_id: str = "",
    already_asked: Sequence[str] = (),
    *,
    prompt: str = "この曲、聴きますか？",
) -> FamiliarityQuestion:
    """親和性チェックの 1 問を組み立てる。

    Parameters
    ----------
    track:
        確認する曲。
    session_id:
        セッション ID。
    already_asked:
        既に質問したセッション ID の列。**このセッションが含まれていれば
        例外**（1 セッション 1 問の強制）。
    prompt:
        読み上げ用の問いかけ。

    Raises
    ------
    TooManyQuestionsError:
        同一セッションで既に質問済みだった場合。
    """
    if session_id and session_id in already_asked:
        raise TooManyQuestionsError(
            "1 セッションにつき親和性チェックは %d 問まで（session_id=%r は質問済み）"
            % (MAX_FAMILIARITY_QUESTIONS_PER_SESSION, session_id)
        )
    return FamiliarityQuestion(track=track, prompt=prompt, session_id=session_id)


def apply_familiarity_answer(
    profile: MusicProfile,
    track: FavoriteTrack,
    accepted: bool,
) -> MusicProfile:
    """回答をプロファイルへ反映した**新しい**プロファイルを返す。

    * ``accepted=True``（はい）: familiarity を 1 上げる（上限 5）。
    * ``accepted=False``（いいえ）: familiarity を 1 下げる（下限 1）。
      **下限に達した回は除外対象**になる（:meth:`MusicProfile.rejects`）。

    元の ``profile`` は変更しない（不変）。

    Notes
    -----
    **増減の基準は引数の ``track`` ではなくプロファイル側の現在値**。
    引数は画面が保持しているスナップショットで古くなっていることがあり、
    それを基準にすると 2 回目以降の「いいえ」が反映されなくなる
    （同じ値を 2 回書き戻すだけで終わる）。
    """
    existing = profile.track(*track.key)
    base = existing if existing is not None else track

    if accepted:
        if base.familiarity_score >= MAX_FAMILIARITY:
            return profile
        new_value = base.familiarity_score + 1
    else:
        new_value = max(MIN_FAMILIARITY, base.familiarity_score - 1)

    if existing is None:
        # プロファイルに無い曲が対象なら、記録として追加する。
        return profile.with_tracks(
            list(profile.favorite_tracks) + [track.with_familiarity(new_value)]
        )
    if existing.familiarity_score == new_value:
        return profile
    return profile.with_tracks(
        [
            item.with_familiarity(new_value) if item.key == existing.key else item
            for item in profile.favorite_tracks
        ]
    )


__all__ = [
    "MAX_FAMILIARITY",
    "MAX_FAMILIARITY_QUESTIONS_PER_SESSION",
    "MIN_FAMILIARITY",
    "TEENAGE_AGE_RANGE",
    "WEIGHT_FAMILIARITY",
    "WEIGHT_RECENCY",
    "WEIGHT_TEENAGE",
    "FavoriteTrack",
    "FamiliarityQuestion",
    "MusicProfile",
    "MusicProfileRepository",
    "Reaction",
    "TooManyQuestionsError",
    "apply_familiarity_answer",
    "build_familiarity_question",
    "rank_by_familiarity",
    "select_by_profile",
    "selection_score",
    "teenage_match",
]
