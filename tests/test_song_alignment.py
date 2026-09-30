"""台本と選曲の一本化（提案② タスク1）の契約テスト。

このファイルが守るもの:

1. ``songs=`` を渡すと、台本の曲名は**そのリストの曲だけ**になる。
2. ``songs=`` を渡さない既存呼び出しは、従来どおり年代パレットを使う。
3. 曲名に改行・制御文字・``###`` を混ぜると**拒否**される。
4. ``eval.metrics.songs.song_match_rate`` に実際の選曲結果を渡したとき、
   指標が「100% に張り付かない」ことを確かめられる（= 指標が起きてる）。
"""

import pytest

from eval.metrics.songs import extract_song_mentions, song_match_rate
from retro_radio.core.fallback import generate_fallback_script, select_program_songs
from retro_radio.core.script_generator import (
    SongTitleError,
    _build_prompt,
    enforce_song_allowlist,
    generate_radio_script,
)

# 1975 年の正本カタログから実在する 3 曲（静的マスターと一致）。
SELECTION = [
    ("木綿のハンカチーフ", "太田裕美"),
    ("いい日旅立ち", "山口百恵"),
    ("神田川", "南こうせつとかぐや姫"),
]
TITLES = [title for title, _artist in SELECTION]
# SELECTION に**含まれない**同じ年代の曲。指標の陰性材料。
OTHER_TITLE = "上を向いて歩こう"


def _titles_in(script, allowed=None):
    return [m.title for m in extract_song_mentions(script, allowed_titles=allowed)]


# ---------------------------------------------------------------------------
# 1. songs= を渡すと、台本の曲名はリストと 100% 一致する
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_script_names_only_the_selected_songs(mode):
    script = generate_radio_script(
        1975, 9, 24, mode=mode, target_name="花子", songs=SELECTION
    )
    mentioned = _titles_in(script, allowed=TITLES)
    assert mentioned, "台本に曲名の言及が無い（検証対象がない）"
    assert set(mentioned) <= set(TITLES), mentioned
    assert OTHER_TITLE not in script


def test_normal_mode_names_every_selected_song():
    """3 曲なら 3 曲分名ざす（1 番組の曲スロットと枚数が合う）"""
    script = generate_radio_script(1975, 9, 24, songs=SELECTION)
    mentioned = _titles_in(script, allowed=TITLES)
    assert len(mentioned) == 3, mentioned
    assert set(mentioned) == set(TITLES)


def test_care_script_uses_the_selection_too():
    script = generate_radio_script(
        1975, 9, 24, mode="care_recreation", songs=SELECTION
    )
    mentioned = _titles_in(script, allowed=TITLES)
    assert mentioned, "介護モードで曲名が名ざされていない"
    assert set(mentioned) <= set(TITLES), mentioned


def test_anniversary_script_uses_the_selection_too():
    script = generate_radio_script(
        1975, 9, 24, mode="anniversary", target_name="花子", songs=SELECTION
    )
    assert "花子" in script
    assert set(_titles_in(script, allowed=TITLES)) <= set(TITLES)


def test_dict_shaped_songs_are_accepted():
    """``{"title": ..., "artist": ...}`` 形式も受け付ける"""
    payload = [{"title": title, "artist": artist} for title, artist in SELECTION]
    script = generate_radio_script(1975, 9, 24, songs=payload)
    assert set(_titles_in(script, allowed=TITLES)) == set(TITLES)


def test_duplicate_titles_are_dropped():
    """同じ曲名を 2 回渡しても、台本は 1 回しか名ざさない"""
    script = generate_radio_script(
        1975, 9, 24, songs=SELECTION + [(SELECTION[0][0], "別の表記")]
    )
    mentioned = _titles_in(script, allowed=TITLES)
    assert len(mentioned) == len(set(mentioned)), mentioned


# ---------------------------------------------------------------------------
# 2. 一致率指標が「動く」ことの証明
# ---------------------------------------------------------------------------
def test_match_rate_is_perfect_for_the_pinned_selection():
    script = generate_radio_script(1975, 9, 24, songs=SELECTION)
    result = song_match_rate(script, 1975, allowed_titles=TITLES)
    assert result.total >= 3
    assert result.rate == 1.0, result.describe()
    assert not result.unmatched


def test_match_rate_detects_one_wrong_mention():
    """1 曲だけ別の曲に差し替えると ``unmatched`` が出る（指標は起きてる）"""
    script = generate_radio_script(1975, 9, 24, songs=SELECTION)
    broken = script.replace(SELECTION[1][0], OTHER_TITLE)
    assert broken != script, "差し替え対象が見つからない"

    result = song_match_rate(broken, 1975, allowed_titles=TITLES)
    assert [m.title for m in result.unmatched] == [OTHER_TITLE]
    assert result.rate < 1.0, result.describe()
    assert result.source == "caller"


def test_match_rate_uses_the_actual_selection_not_the_static_master():
    """``allowed_titles`` を渡さないと、静的マスターとの照合になる"""
    script = generate_radio_script(1975, 9, 24, songs=SELECTION)
    with_selection = song_match_rate(script, 1975, allowed_titles=TITLES)
    without = song_match_rate(script, 1975)
    assert without.source == "static-master"
    assert with_selection.source == "caller"
    assert with_selection.total == without.total


# ---------------------------------------------------------------------------
# 3. songs= を渡さない既存呼び出しは従来どおり
# ---------------------------------------------------------------------------
def test_without_songs_the_legacy_script_is_returned_unchanged():
    script = generate_radio_script(1975, 9, 24)
    assert script == generate_fallback_script(1975, 9, 24)
    assert set(_titles_in(script)) <= {
        title for title, _a in select_program_songs(1975, 3)
    }


def test_empty_songs_behaves_like_none():
    assert generate_radio_script(1975, 9, 24, songs=[]) == generate_radio_script(
        1975, 9, 24
    )


def test_same_selection_gives_the_same_script():
    first = generate_radio_script(1975, 9, 24, songs=SELECTION)
    second = generate_radio_script(1975, 9, 24, songs=SELECTION)
    assert first == second


# ---------------------------------------------------------------------------
# 4. 曲名のバリデーション（target_name と同じ規則）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_title",
    [
        "改行\n入り",
        "復帰\r入り",
        "タブ\t入り",
        "NULL\0入り",
        "ベル\x07入り",
        "見出し###入り",
        "   ",
    ],
)
def test_invalid_titles_are_rejected(bad_title):
    with pytest.raises(SongTitleError):
        generate_radio_script(1975, 9, 24, songs=[(bad_title, "歌手")])


@pytest.mark.parametrize("bad_artist", ["歌手\n入り", "###歌手"])
def test_invalid_artists_are_rejected(bad_artist):
    with pytest.raises(SongTitleError):
        generate_radio_script(1975, 9, 24, songs=[("曲名", bad_artist)])


def test_too_long_title_is_rejected():
    """抽出窓（40 字）を超える曲名は指標が見られないため拒否する"""
    with pytest.raises(SongTitleError):
        generate_radio_script(1975, 9, 24, songs=[("あ" * 41, "歌手")])


@pytest.mark.parametrize("payload", ["曲名", 123, [("曲名",)]])
def test_malformed_entries_are_rejected(payload):
    with pytest.raises(SongTitleError):
        generate_radio_script(1975, 9, 24, songs=[payload])


def test_empty_artist_is_allowed():
    """アーティスト不明でも台本には出せる（曲名は必ず名前を出す）"""
    script = generate_radio_script(1975, 9, 24, songs=[("木綿のハンカチーフ", "")])
    assert "木綿のハンカチーフ" in script


# ---------------------------------------------------------------------------
# 5. LLM 経路も同じ制約に縛る
# ---------------------------------------------------------------------------
def test_prompt_lists_the_selection_and_nothing_else():
    prompt = _build_prompt(1975, 9, 24, "normal", songs=SELECTION)
    for title, _artist in SELECTION:
        assert title in prompt
    assert OTHER_TITLE not in prompt
    assert "厳禁" in prompt


def test_prompt_does_not_freeze_a_year():
    """``songs`` が無いとき、候補は対象年だけを見る"""
    prompt = _build_prompt(1985, 9, 24, "normal")
    assert "卒業写真" not in prompt


def test_llm_mentions_outside_the_list_are_rewritten(monkeypatch):
    import retro_radio.core.script_generator as sg

    monkeypatch.setattr(sg.settings, "gemini_api_key", "test-key", raising=False)
    generated = (
        "### オープニング\n"
        "USP-093 のヒット曲「上を向いて歩こう」（坂本九）をお届けします。\n"
        "### エンディング\nおやすみ。"
    )
    monkeypatch.setattr(sg, "_call_gemini", lambda prompt: generated)

    script = generate_radio_script(1975, 9, 24, songs=SELECTION)
    assert OTHER_TITLE not in script
    mentioned = _titles_in(script, allowed=TITLES)
    assert mentioned and set(mentioned) <= set(TITLES)


def test_enforce_allowlist_keeps_quotes_that_are_not_song_mentions():
    """クイズの答えなど、曲ではない引用は書き換えない"""
    script = "### 思い出話1\n1970年代、深夜放送の質問です。「答え」（ヒント）をお聞かせください。"
    assert enforce_song_allowlist(script, SELECTION) == script


def test_enforce_allowlist_is_a_noop_without_selection():
    script = "### オープニング\n「上を向いて歩こう」（坂本九）"
    assert enforce_song_allowlist(script, None) == script
    assert enforce_song_allowlist(script, []) == script
