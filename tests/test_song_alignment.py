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
from retro_radio.core.songs import load_songs
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


def _catalog_titles():
    """正本カタログに実在する曲名の集合。

    「音源ゼロの原稿がカタログの曲名を挙げていないか」の判定に使う。
    番組名（NHKラジオ第一など）は曲名ではないので含まれない。

    **遅延評価**にする: `load_songs()` は import 時に呼ぶと曲カタログの
    読み込みと警告出力を先に発生させ、このファイルがテストする
    「選曲結果の反映」自体に影響する。
    """
    return {song["title"] for song in load_songs()}


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
    """原稿が名ざすのは **各トークの直後に流れる曲**（= ``selection[2:]``）だけ。

    `build_playlist` は 曲0 → トーク0 → 曲1 → トーク1 → … と組むため、
    中間のトーク（1〜3）が告げるのは ``songs[2]`` 以降になる。
    旧テストが「3 曲なら 3 曲分名ざす」と置いていた前提がズレの原因で、
    結果として「次は『すでに鳴った曲』です」と読み上げられていた。
    """
    script = generate_radio_script(1975, 9, 24, songs=SELECTION)
    mentioned = _titles_in(script, allowed=TITLES)
    assert set(mentioned) == set(TITLES[2:]), mentioned


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
    assert set(_titles_in(script, allowed=TITLES)) == set(TITLES[2:])


def test_duplicate_titles_are_dropped():
    """同じ曲名を 2 回渡しても、台本は 1 回しか名ざさない"""
    script = generate_radio_script(
        1975, 9, 24, songs=SELECTION + [(SELECTION[2][0], "別の表記")]
    )
    mentioned = _titles_in(script, allowed=TITLES)
    assert mentioned, mentioned
    assert len(mentioned) == len(set(mentioned)), mentioned


# ---------------------------------------------------------------------------
# 2. 一致率指標が「動く」ことの証明
# ---------------------------------------------------------------------------
def test_match_rate_is_perfect_for_the_pinned_selection():
    script = generate_radio_script(1975, 9, 24, songs=SELECTION)
    result = song_match_rate(script, 1975, allowed_titles=TITLES)
    # 名ざされるのは `SELECTION[2:]`。
    assert result.total >= 1, result.describe()
    assert result.rate == 1.0, result.describe()
    assert not result.unmatched


def test_match_rate_detects_one_wrong_mention():
    """1 曲だけ別の曲に差し替えると ``unmatched`` が出る（指標は起きてる）"""
    script = generate_radio_script(1975, 9, 24, songs=SELECTION)
    # 名ざされるのは `SELECTION[2:]` なので差し替え対象もそこから取る。
    broken = script.replace(SELECTION[2][0], OTHER_TITLE)
    assert broken != script, "差し替え対象が見つからない"

    result = song_match_rate(broken, 1975, allowed_titles=TITLES)
    assert [m.title for m in result.unmatched] == [OTHER_TITLE]
    assert result.rate < 1.0, result.describe()
    assert result.source == "caller"


def test_match_rate_uses_the_actual_selection_not_the_static_master():
    """``allowed_titles`` を渡さないと、静的マスター（正本カタログ）との照合になる

    P3-2 で正本カタログと同期したため、静的マスターの source 名は
    ``"static-master"`` から ``"catalog"`` に変わった。照合先が
    呼び出し元（caller）ではなくカタログ側であることの保証は不変。
    """
    script = generate_radio_script(1975, 9, 24, songs=SELECTION)
    with_selection = song_match_rate(script, 1975, allowed_titles=TITLES)
    without = song_match_rate(script, 1975)
    assert without.source == "catalog"
    assert with_selection.source == "caller"
    assert with_selection.total == without.total


# ---------------------------------------------------------------------------
# 3. songs= を渡さない既存呼び出しは従来どおり
# ---------------------------------------------------------------------------
def test_without_songs_the_legacy_script_is_returned_unchanged():
    script = generate_radio_script(1975, 9, 24)
    assert script == generate_fallback_script(1975, 9, 24)
    # 名ざされるのは中間のトークの直後に流れる曲だけ。
    assert set(_titles_in(script)) <= {
        title for title, _a in select_program_songs(1975, 5)[2:]
    }


def test_empty_songs_never_mentions_a_song_title():
    """**音源ゼロ**（``songs=[]``）なら原稿に曲名を一切書かないこと。

    以前は `validate_song_pairs` が `return pairs or None` で空リストを
    `None` に潰していたため、`script_generator` がカタログから曲名を導出し直し、
    司会が「次は『○○』です」と**鳴らない曲を紹介**していた。
    `server.py` は「`or None` で潰さない。空リストと None は別物」と
    明記していたのに、その事故が実際に起きていた。

    したがって ``songs=[]`` と ``songs=None`` は**結果が異なる**のが正しい:
    前者は「曲名を一切書かない」、後者は「カタログから導出する」。
    """
    script = generate_radio_script(1975, 9, 24, songs=[])
    mentioned = set(_titles_in(script))
    assert not mentioned, (
        f"音源ゼロなのに原稿が曲名を挙げています: {sorted(mentioned)}"
    )


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_empty_songs_never_mentions_a_song_title_in_any_mode(mode):
    """**3 モードすべて**で、音源ゼロなら正本カタログの曲名を挙げないこと。

    Round 1 の修正は `normal` モードしか直していなかった。
    `care_recreation` / `anniversary` は `pinned_songs([])` を通りますが、
    `select_program_songs` が `if pinned:` で真偽判定していたため
    空リストを「未差し込み」と误解し、カタログから曲を選び直していた。
    その結果、介護・記念日の原稿が**鳴らない曲を紹介**していた
    （Round 2 の実測）。`IndexError` になる経路もあった。
    """
    script = generate_radio_script(1975, 5, 15, mode=mode, songs=[])
    mentioned = set(_titles_in(script))
    songs_mentioned = sorted(mentioned & _catalog_titles())
    assert not songs_mentioned, (
        f"{mode} で音源ゼロなのに正本カタログの曲名を挙げています: {songs_mentioned}"
    )


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_empty_songs_keeps_the_script_structure(mode):
    """音源ゼロでも原稿の構造（見出し・年・長さ）は保たれること。

    曲名だけを取り除いた設計であり、原稿全体が空になるわけではない。
    長さの下限は `eval` ゲート（800〜1500 字）の下限に合わせる
    （Round 2 の実測: 専用原稿は 737 字でゲートが赤になっていた）。
    """
    script = generate_radio_script(1975, 5, 15, mode=mode, songs=[])
    assert "### オープニング" in script, mode
    assert "### エンディング" in script, mode
    assert "1975" in script, mode
    assert len(script) >= 800, f"{mode} の原稿が短すぎます: {len(script)} 字"


def test_songs_none_still_derives_from_the_catalog():
    """``songs`` 未指定（``None``）は従来どおりカタログから導出する。"""
    derived = generate_radio_script(1975, 9, 24, songs=None)
    catalog_titles = {title for title, _a in select_program_songs(1975, 5)[2:]}
    assert set(_titles_in(derived)) <= catalog_titles

    # 空リストとは**結果が違う**ことの明示（潰していないことの証明）。
    assert generate_radio_script(1975, 9, 24, songs=[]) != derived


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
    # 名ざされる範囲（`selection[2:]`）に入れてから検証する。
    script = generate_radio_script(
        1975, 9, 24, songs=SELECTION[:2] + [("神田川", "")]
    )
    assert "神田川" in script


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


def test_llm_mentions_outside_the_list_are_stripped(monkeypatch):
    """許可リスト外の曲名は**置換ではなく除去**する（P0-4）。

    置き換えると、`build_playlist` の「トーク i → 曲 i+1」対応に対して
    位置がずれ、**すでに鳴った曲**を次の曲として告げることになる。
    嘘を別の嘘に置き換えるのであって、正しくない。
    """
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
    # 除去したあとに**残った曲名はすべて許可リスト内**。
    assert set(mentioned) <= set(TITLES)


def test_enforce_allowlist_keeps_quotes_that_are_not_song_mentions():
    """クイズの答えなど、曲ではない引用は書き換えない"""
    script = "### 思い出話1\n1970年代、深夜放送の質問です。「答え」（ヒント）をお聞かせください。"
    assert enforce_song_allowlist(script, SELECTION) == script


def test_enforce_allowlist_drops_song_names_when_selection_is_empty():
    """**許可リストが空なら曲名を一切残さない**（P0-3）。

    空のときに原稿を素通しすると、音源が 1 曲も無い（=全部間奏）のとき
    LLM が実在しない曲名を告げたまま間奏が流れる。
    `_resolve_previews` が iTunes から 1 曲も取れなかった場合に
    この経路を通る（Round 1 の `songs=[]` と同じ危険）。
    """
    script = "### オープニング\n「上を向いて歩こう」（坂本九）をお届けします。"
    for empty in (None, []):
        cleaned = enforce_song_allowlist(script, empty)
        assert "上を向いて歩こう" not in cleaned
        assert "坂本九" not in cleaned
        # 見出しは原文ごと残る（台本の構造を壊さない）。
        assert "オープニング" in cleaned
        assert cleaned.strip(), "空原稿に潰さない"


def test_enforce_allowlist_is_a_noop_when_the_script_is_empty():
    assert enforce_song_allowlist("", SELECTION) == ""
    assert enforce_song_allowlist("", []) == ""
