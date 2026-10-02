"""音源が 3 曲未満のとき原稿に空の鉤括弧が出ないこと（Round 3 / NEW-A）。

Round 2 のガード `first_song = songs[0] if songs else ("", "")` は
`pinned_songs([])`（0 曲）を防いだが、**1〜2 曲**のときは
`_cue_song` が `None` を返し、`or ("", "")` が空のタプルを渡した。
結果として原稿に:

- ``この年の懐かしい名曲「」（）を、``   （care）
- ``記念の一曲「」（）をお届けします。``  （anniversary）
- ``とをつなぎますと``                 （anniversary）

という文法破綻と空の曲名が出ていた。`clean_script_for_tts` は曲名のある行だけを
対象にするので除去されず、**TTS がそのまま読み上げていた**。
"""

from __future__ import annotations

import re

import pytest

from retro_radio.core.fallback import (
    generate_anniversary_script,
    generate_care_script,
    generate_fallback_script,
    pinned_songs,
)

SONGS = [("木綿のハンカチーフ", "太田裕美"), ("いい日旅立ち", "山口百恵")]

#: 空の鉤括弧 / 文法破綻の検出パターン。
EMPTY_QUOTE = re.compile(r"「」（）|とをつなぎますと|「」")


@pytest.mark.parametrize("count", [0, 1, 2, 3])
@pytest.mark.parametrize(
    "generate",
    [generate_care_script, generate_anniversary_script],
    ids=["care_recreation", "anniversary"],
)
def test_never_emit_empty_quotes_when_songs_are_short(count, generate):
    """音源が 0〜3 曲でも、空の曲名や文法破綻を出さないこと。"""
    pool = SONG_POOL[:count]
    with pinned_songs(pool):
        if generate is generate_care_script:
            script = generate(1975, 5, 15)
        else:
            script = generate(1975, 5, 15, "花子")

    assert not EMPTY_QUOTE.search(script), (
        f"{count} 曲で原稿に空の曲名/文法破綻が出ています: "
        f"{EMPTY_QUOTE.findall(script)}"
    )


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_normal_mode_never_emits_empty_quotes(count):
    """通常モードも同様（`_cue_line` 経由に統一している）。"""
    with pinned_songs(SONG_POOL[:count]):
        script = generate_fallback_script(1975, 5, 15, songs=SONG_POOL[:count])
    assert not EMPTY_QUOTE.search(script), EMPTY_QUOTE.findall(script)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_cue_sentences_disappear_entirely_when_there_is_no_song(count):
    """曲が無いときは曲振り文が**文ごと消える**（空の文が残らない）こと。"""
    with pinned_songs(SONG_POOL[:count]):
        care = generate_care_script(1975, 5, 15)
        anniv = generate_anniversary_script(1975, 5, 15, "花子")

    if count == 0:
        # 0 曲なら曲振り台詞は出ない。
        assert "懐かしい名曲" not in care
        assert "記念の一曲" not in anniv
        assert "をつなぎますと" not in anniv


@pytest.mark.parametrize(
    "generate",
    [generate_care_script, generate_anniversary_script],
    ids=["care_recreation", "anniversary"],
)
def test_scripts_keep_their_structure_without_songs(generate):
    """曲が無くても原稿の構造（見出し・年）は保たれること。"""
    with pinned_songs([]):
        if generate is generate_care_script:
            script = generate(1975, 5, 15)
        else:
            script = generate(1975, 5, 15, "花子")

    assert "### オープニング" in script
    assert "### エンディング" in script
    assert "1975" in script
    assert len(script) >= 800, f"原稿が短すぎます: {len(script)} 字"


SONG_POOL = [
    ("木綿のハンカチーフ", "太田裕美"),
    ("いい日旅立ち", "山口百恵"),
    ("神田川", "南こうせつとかぐや姫"),
]
