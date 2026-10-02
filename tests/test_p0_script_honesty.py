"""公開前デバッグ（P0）の回帰テスト — 原稿の誠実性と認証（第 2 部）。

* P0-3  許可リストが空だと `enforce_song_allowlist` が無防備になる
* P0-4  許可リストの差し替えが位置を見ず、すでに鳴った曲に差し替える
* P0-5  音源ゼロなのに「三つほどご用意しました」と約束する
* P0-8  `single_user_key` に最小長が無く、ベアラー経路にレート制限が無い
"""

from __future__ import annotations

import pytest

from retro_radio.core.script_generator import enforce_song_allowlist
from retro_radio.core.fallback import (
    _music_promise_sentence,
    generate_anniversary_script,
    generate_care_script,
    generate_fallback_script,
)


# ==============================================================================
# P0-3 / P0-4: enforce_song_allowlist
# ==============================================================================
_SONGS = [("Alpha", "A1"), ("Beta", "B2"), ("Gamma", "C3"), ("Delta", "D4")]


def test_empty_allowlist_removes_every_song_claim():
    """音源ゼロなら曲名を 1 つも口にしてはならない（P0-3）。"""
    script = (
        "### オープニング\n"
        "USP-093 のヒット曲「上を向いて歩こう」（坂本九）をお届けします。\n"
        "おやすみ。"
    )
    for empty in (None, []):
        cleaned = enforce_song_allowlist(script, empty)
        assert "上を向いて歩こう" not in cleaned
        assert "坂本九" not in cleaned
        assert "USP-093" in cleaned, "見出しや本文まで消している"
        assert cleaned.strip(), "空原稿に潰さない"


def test_out_of_list_song_is_stripped_not_replaced_with_a_positional_pick():
    """許可リスト外の曲名は**除去**する（P0-4）。

    置き換えると `build_playlist` の「トーク i → 曲 i+1」対応に対して位置がずれ、
    すでに鳴ったオープニング曲を次の曲として告げることになる。
    ここでは「曲名の主張」と確実に判定させるため、正本カタログに載る
    実在の曲名を混入させる。
    """
    # 「上を向いて歩こう」は正本カタログに載っている（曲名主張として判定される）。
    script = (
        "### トーク1\n"
        "この年のヒット曲「上を向いて歩こう」（坂本九）をお届けいたします。\n"
        "続いて「東京キッド」（藤原亮子）もお届けいたします。"
    )
    cleaned = enforce_song_allowlist(script, _SONGS)
    assert "上を向いて歩こう" not in cleaned
    assert "坂本九" not in cleaned
    # round-robin で `allowed[0]` / `allowed[1]` が差し込まれていない。
    assert "Alpha" not in cleaned
    assert "Beta" not in cleaned
    # 会話の本文は残る（文ごと消さない）。
    assert "お届けいたします" in cleaned


def test_allowlist_songs_survive_untouched():
    script = "### トーク1\n「Alpha」（A1）をお届けします。"
    assert enforce_song_allowlist(script, _SONGS) == script


# ==============================================================================
# P0-5: 音源数と約束文の一致
# ==============================================================================
def test_promise_sentence_matches_the_song_count():
    assert "三つほど" in _music_promise_sentence(1975, 3)
    assert "三つほど" in _music_promise_sentence(1975, 5)
    assert "ふたつ" in _music_promise_sentence(1975, 2)
    assert "ひとくち" in _music_promise_sentence(1975, 1)
    zero = _music_promise_sentence(1975, 0)
    assert "三つほど" not in zero
    assert "ヒット曲" not in zero


def test_no_music_script_never_promises_songs():
    script = generate_fallback_script(1975, 9, 24, songs=[])
    assert "三つほどご用意しました" not in script
    assert "music の時間" not in script
    assert "お待ちかねの音楽の時間" not in script


def test_short_selection_never_promises_three_songs():
    """音源が 1〜2 曲なら「三つほど」とは言わない。"""
    for n in (1, 2):
        songs = _SONGS[:n]
        script = generate_fallback_script(1975, 9, 24, songs=songs)
        assert "三つほどご用意しました" not in script, f"{n} 曲で三つと言っている"


def test_care_and_anniversary_have_no_empty_song_brackets():
    """音源ゼロでも空の鉤括弧が出ない（Round 3 NEW-A の回帰防止）。"""
    for script in (
        generate_care_script(1975, 9, 24),
        generate_anniversary_script(1975, 9, 24, "KT"),
    ):
        assert "「」（" not in script
        assert "「」（）" not in script
        assert "ををつなぎます" not in script


# ==============================================================================
# P0-8: 認証
# ==============================================================================
def test_bearer_mode_requires_a_long_single_user_key():
    """短い `single_user_key` で `bearer` モードが成立してはならない。"""
    from retro_radio.config import ConfigurationError, Settings

    s = Settings(require_auth=True, secret_key="", single_user_key="x")
    assert s.require_auth_config() == "unavailable"
    assert s.auth_ready is False
    with pytest.raises(ConfigurationError):
        s.require_single_user_key()


def test_bearer_failures_are_throttled():
    """ベアラー認証の連打が**同じ枠**に記録される（P0-8）。

    429 のブロックは「email + password」の内側にしか無く、
    token だけを叩く総当たりには一切効いていなかった。
    さらにimal な罠として、`presented` をキーにすると毎回別キーになり
    記録がaccumulateするだけで制限にならない。固定枠であることも固定する。
    """
    from retro_radio.auth.authenticator import LoginThrottle, throttle_key

    throttle = LoginThrottle()
    # ベアラー経路が使う固定枠（`server.create_session` と同じ）。
    key = throttle_key("bearer:personal-mode", "127.0.0.1")
    for _ in range(throttle.max_failures + 2):
        throttle.record_failure(key)
    assert throttle.delay_for(key) > 0, "連続失敗が記録されていない"

    # presented（推測値）をキーにすると毎回別枠になり、制限にならない。
    for guess in ("guess-0", "guess-1", "guess-2"):
        other = throttle_key(f"bearer:{guess}", "127.0.0.1")
        assert throttle.delay_for(other) == 0


def test_authenticator_exposes_the_shared_throttle():
    """ベアラー経路がパスワード経路と**同じ**記録を使うこと（P0-8）。"""
    from retro_radio.auth.authenticator import Authenticator

    a1 = Authenticator()
    a2 = Authenticator()
    assert a1.throttle is a2.throttle
