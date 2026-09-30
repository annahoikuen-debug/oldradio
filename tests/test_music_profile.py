"""個人音楽プロファイル（提案② タスク4/5）の契約テスト。

- 空プロファイルなら年代パレットへフォールバックする
- 選曲スコアの式が仕様どおりである
- 親和性チェックは 1 セッション 1 問（例外で強制）
- 「いいえ」で familiarity を 1 減算し、該当回を除外する
- ``MusicProfileRepository`` の Protocol が S4 の実装と一致する
"""

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from retro_radio.core.fallback import select_program_songs
from retro_radio.core.music_profile import (
    MAX_FAMILIARITY,
    MAX_FAMILIARITY_QUESTIONS_PER_SESSION,
    MIN_FAMILIARITY,
    WEIGHT_FAMILIARITY,
    WEIGHT_RECENCY,
    WEIGHT_TEENAGE,
    FavoriteTrack,
    MusicProfile,
    MusicProfileRepository,
    Reaction,
    TooManyQuestionsError,
    apply_familiarity_answer,
    build_familiarity_question,
    rank_by_familiarity,
    select_by_profile,
    selection_score,
    teenage_match,
)

NOW = datetime(2020, 1, 1, tzinfo=timezone.utc)
TEENAGE = MusicProfile(owner_id="u1", teenage_decades=(1970, 1980))


def _profile(*tracks, **kwargs):
    return MusicProfile(owner_id=kwargs.pop("owner_id", "u1"),
                        favorite_tracks=tracks, **kwargs)


# ---------------------------------------------------------------------------
# 1. 空プロファイルなら年代パレット
# ---------------------------------------------------------------------------
def test_empty_profile_falls_back_to_the_decade_palette():
    empty = MusicProfile(owner_id="new-user")
    assert empty.is_empty
    assert select_by_profile(1975, empty, 3) == select_program_songs(1975, 3)


def test_profile_with_only_teenage_decades_is_not_empty():
    assert not TEENAGE.is_empty


def test_rejected_tracks_are_removed_from_the_selection():
    keep = FavoriteTrack("いい日旅立ち", "山口百恵", familiarity_score=4)
    drop = FavoriteTrack("上を向いて歩こう", "坂本九", familiarity_score=MIN_FAMILIARITY)
    profile = _profile(keep, drop)
    assert profile.rejects("上を向いて歩こう")
    assert not profile.rejects("いい日旅立ち")
    selected = select_by_profile(1975, profile, 3)
    assert "上を向いて歩こう" not in [title for title, _ in selected]


def test_selection_falls_back_when_every_track_is_rejected():
    only = FavoriteTrack("上を向いて歩こう", "坂本九", familiarity_score=MIN_FAMILIARITY)
    profile = _profile(only)
    assert select_by_profile(1975, profile, 3) == select_program_songs(1975, 3)


# ---------------------------------------------------------------------------
# 2. スコアの式: 1.0 * familiarity + 十代年代一致 - 2.0 * 経過
# ---------------------------------------------------------------------------
def test_score_coefficients_are_the_documented_ones():
    assert (WEIGHT_FAMILIARITY, WEIGHT_TEENAGE, WEIGHT_RECENCY) == (1.0, 1.0, 2.0)


def test_score_is_familiarity_plus_teenage_match():
    track = FavoriteTrack("いい日旅立ち", "山口百恵", familiarity_score=4)
    assert teenage_match(TEENAGE, 1975) == 1.0
    assert selection_score(track, TEENAGE, 1975, now=NOW) == pytest.approx(5.0)


def test_score_has_no_teenage_bonus_outside_the_decades():
    track = FavoriteTrack("Lemon", "米津玄師", familiarity_score=3)
    assert teenage_match(TEENAGE, 2018) == 0.0
    assert selection_score(track, TEENAGE, 2018, now=NOW) == pytest.approx(3.0)


def test_score_penalises_elapsed_years():
    played = datetime(2015, 1, 1, tzinfo=timezone.utc)
    track = FavoriteTrack("いい日旅立ち", "山口百恵",
                          familiarity_score=4, last_played_at=played)
    elapsed = (NOW - played).total_seconds() / (365.25 * 24 * 3600)
    assert selection_score(track, TEENAGE, 1975, now=NOW) == pytest.approx(
        4.0 + 1.0 - 2.0 * elapsed
    )


def test_never_played_track_has_no_recency_penalty():
    track = FavoriteTrack("いい日旅立ち", "山口百恵", familiarity_score=5)
    assert selection_score(track, TEENAGE, 1975, now=NOW) == pytest.approx(6.0)


def test_ranking_is_descending_and_stable_for_an_empty_profile():
    tracks = [
        FavoriteTrack("A", "x", familiarity_score=2),
        FavoriteTrack("B", "y", familiarity_score=5),
    ]
    ranked = rank_by_familiarity(tracks, TEENAGE, {}, now=NOW)
    assert [t.title for t in ranked] == ["B", "A"]
    assert rank_by_familiarity(tracks, MusicProfile(), now=NOW) == tracks


# ---------------------------------------------------------------------------
# 3. 親和性チェックは 1 セッション 1 問
# ---------------------------------------------------------------------------
def test_only_one_question_per_session_is_allowed():
    assert MAX_FAMILIARITY_QUESTIONS_PER_SESSION == 1
    track = FavoriteTrack("いい日旅立ち", "山口百恵")
    question = build_familiarity_question(track, session_id="s1")
    assert question.session_id == "s1"
    with pytest.raises(TooManyQuestionsError):
        build_familiarity_question(track, session_id="s1", already_asked=["s1"])


def test_a_different_session_may_ask_again():
    track = FavoriteTrack("いい日旅立ち", "山口百恵")
    build_familiarity_question(track, session_id="s2", already_asked=["s1"])


def test_question_prompt_is_readable_for_tts():
    question = build_familiarity_question(
        FavoriteTrack("いい日旅立ち", "山口百恵"), session_id="s1"
    )
    assert question.prompt
    assert "\n" not in question.prompt
    assert "###" not in question.prompt


# ---------------------------------------------------------------------------
# 4. 「いいえ」で familiarity を 1 減算し、その回を除外
# ---------------------------------------------------------------------------
def test_no_answer_decreases_familiarity_by_one():
    track = FavoriteTrack("いい日旅立ち", "山口百恵", familiarity_score=4)
    profile = _profile(track)
    updated = apply_familiarity_answer(profile, track, accepted=False)
    assert updated.track("いい日旅立ち").familiarity_score == 3
    assert profile.track("いい日旅立ち").familiarity_score == 4, "元は不変"


def test_familiarity_never_goes_below_the_minimum():
    track = FavoriteTrack("いい日旅立ち", "山口百恵",
                          familiarity_score=MIN_FAMILIARITY)
    profile = _profile(track)
    updated = apply_familiarity_answer(profile, track, accepted=False)
    assert updated.track("いい日旅立ち").familiarity_score == MIN_FAMILIARITY
    assert updated.rejects("いい日旅立ち")


def test_yes_answer_increases_familiarity_up_to_the_maximum():
    track = FavoriteTrack("いい日旅立ち", "山口百恵", familiarity_score=MAX_FAMILIARITY)
    profile = _profile(track)
    updated = apply_familiarity_answer(profile, track, accepted=True)
    assert updated.track("いい日旅立ち").familiarity_score == MAX_FAMILIARITY


def test_repeated_no_answers_eventually_exclude_the_track():
    track = FavoriteTrack("いい日旅立ち", "山口百恵", familiarity_score=4)
    profile = _profile(track)
    for expected in (3, 2, 1):
        profile = apply_familiarity_answer(profile, track, accepted=False)
        assert profile.track("いい日旅立ち").familiarity_score == expected
    # 下限に達した回から除外対象になる
    assert profile.rejects("いい日旅立ち")
    # 続けて「いいえ」しても下限は割らない
    profile = apply_familiarity_answer(profile, track, accepted=False)
    assert profile.track("いい日旅立ち").familiarity_score == MIN_FAMILIARITY


def test_reaction_values_are_limited():
    assert set(Reaction.ALL) == {"positive", "neutral", "negative"}
    with pytest.raises(ValueError):
        FavoriteTrack("曲名", "歌手", reaction="unknown")


@pytest.mark.parametrize("score", [0, 6, -1])
def test_familiarity_range_is_enforced(score):
    with pytest.raises(ValueError):
        FavoriteTrack("曲名", "歌手", familiarity_score=score)


# ---------------------------------------------------------------------------
# 5. Protocol と S4 の実装の一致
# ---------------------------------------------------------------------------
def _params(func):
    signature = inspect.signature(func)
    return [
        (p.name, p.kind.name, p.default is inspect.Parameter.empty)
        for p in signature.parameters.values()
        if p.name != "self"
    ]


def test_repository_protocol_signatures_match_the_s4_implementation():
    from retro_radio.db.privacy_repository import MusicProfileRepositoryImpl

    methods = ["get", "save", "record_play", "record_rejection"]
    for name in methods:
        protocol = getattr(MusicProfileRepository, name)
        impl = getattr(MusicProfileRepositoryImpl, name)
        assert _params(protocol) == _params(impl), name


def test_repository_impl_satisfies_the_runtime_checkable_protocol():
    from retro_radio.db.privacy_repository import MusicProfileRepositoryImpl

    assert isinstance(MusicProfileRepositoryImpl(db=object()), MusicProfileRepository)


def test_repository_impl_is_not_abstract_but_core_has_no_persistence():
    """``core`` は永続化を持たない（実装は db 層）。"""
    import retro_radio.core.music_profile as mp

    assert not hasattr(mp, "Session")
    assert MusicProfileRepository.__module__ == "retro_radio.core.music_profile"
