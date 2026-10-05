"""原稿（トーク）と曲の対応、およびタイミングずれを回帰テストで固定する。

実測されていた不具合
--------------------
Gemini が指示どおりにトークだけを書いても、`### 曲1` `### 曲2` `### 曲3`
という「ここに曲が入ります」だけの架空セグメントを足してきた。
結果として:

  1. トーク数が 5 ではなく 8 に膨らむ
  2. 16 文字の「（※音楽が流れる）」が司会の声で読み上げられる
  3. その 16 文字のトークが曲と曲の間にはさまり、**タイミングがずれる**
  4. プレイリストが 17 トラックになり、1 パスの構成が崩れる
  5. 原稿（script 基準）とキュー（segments 基準）の行数がずれて
     「読み上げ中」ハイライトが別の段落に付く
"""

import pytest

from retro_radio.core.fallback import generate_fallback_script
from retro_radio.core.script_generator import (
    _build_segmented_prompt,
    _drop_song_marker_segments,
    parse_script_segments,
)
from retro_radio.models.radio import ScriptSegment
from retro_radio.utils.text_cleaner import clean_script_for_tts

# 実測された Gemini の出力（曲マーカーで 8 セグメントに分断された原稿）
GEMINI_WITH_SONG_MARKERS = """### オープニング
（番組テーマ曲のイントロがフェードアウト）

皆様、こんばんは。深夜のラジオの時間です。
本日は昭和50年、1975年9月30日でございます。

---

### 曲1
（※音楽が流れる）

---

### トーク1_ニュース
心地よいメロディに身を委ねていただきましたが。
それでは、この年のヒット曲をお届けします。

---

### 曲2
（※音楽が流れる）

---

### トーク2_くらし
お届けした曲、いかがでしたでしょうか。
続いて、また懐かしい一曲をお届けします。

---

### 曲3
（※音楽が流れる）

---

### トーク3_共感
お聴きいただきました曲、皆様はどうでしたか。
では、さらにもう一曲、お楽しみください。

---

### エンディング
（※音楽が流れる）

皆様、いかがでしたでしょうか。
それでは、おやすみなさいませ。
"""


# ==============================================================================
# 1. 曲マーカーセグメントの除去
# ==============================================================================
def test_song_marker_segments_are_dropped():
    """曲の見出しと演出指示だけのセグメントがトークとして残らない"""
    titles = [s.title for s in parse_script_segments(GEMINI_WITH_SONG_MARKERS)]
    assert titles == ["オープニング", "トーク1_ニュース", "トーク2_くらし",
                      "トーク3_共感", "エンディング"], titles


def test_segment_count_is_five_not_eight():
    """トーク数が 5（8 ではない）に戻る"""
    assert len(parse_script_segments(GEMINI_WITH_SONG_MARKERS)) == 5


def test_segment_order_and_id_are_reindexed():
    """除去後に order / id が連番になり原稿の表示順と一致する"""
    segments = parse_script_segments(GEMINI_WITH_SONG_MARKERS)
    assert [s.order for s in segments] == [0, 1, 2, 3, 4]
    assert [s.id for s in segments] == ["seg_0", "seg_1", "seg_2", "seg_3", "seg_4"]


@pytest.mark.parametrize(
    "title,body,dropped",
    [
        ("曲1", "（※音楽が流れる）", True),
        ("曲2", "（音楽が流れる）", True),
        ("曲3", "※SE を入れる", True),
        ("主題歌", "（※主題歌が流れる）", True),
        ("テーマ曲", "（テーマがフェードアウト）", True),
        # 本文に地の文があれば残す（曲への感想を語る段落は原稿の一部）
        ("曲1", "この曲は本当に素敵ですね。", False),
        ("トーク1_ニュース", "ニュース原稿です。", False),
        ("オープニング", "あいさつ原稿です。", False),
    ],
)
def test_marker_detection_is_scoped(title, body, dropped):
    """曲マーカーと実際のトークを混同しない"""
    seg = ScriptSegment(id="s", title=title, content=body,
                        estimated_duration=1.0, order=0)
    kept = _drop_song_marker_segments([seg])
    assert (len(kept) == 0) is dropped, (title, body, kept)


# ==============================================================================
# 2. TTS テキストに演出指示が残らない
# ==============================================================================
@pytest.mark.parametrize(
    "raw",
    [
        "（※音楽が流れる）",
        "（番組テーマ曲のイントロがフェードアウト）",
        "（SE）",
        "※BGM 再生",
        "---",
    ],
)
def test_stage_directions_are_not_read_aloud(raw):
    """演出指示が司会の声で読み上げられない"""
    cleaned = clean_script_for_tts(raw)
    assert "音楽" not in cleaned, cleaned
    assert "フェード" not in cleaned, cleaned
    assert "※" not in cleaned, cleaned
    assert "---" not in cleaned, cleaned


def test_spoken_text_is_preserved():
    """地の文は消さない（丸括弧を含む会話も残る）"""
    assert "本当に" in clean_script_for_tts("昨夜は（本当に）楽しかったですね。")


def test_song_cue_lines_are_preserved():
    """曲振り台詞は原稿の一部なので消さない"""
    assert "この年のヒット曲をお届けします" in clean_script_for_tts(
        "それでは、この年のヒット曲をお届けします。"
    )


def test_cleaning_a_real_gemini_script_leaves_no_direction():
    """実測原稿には演出指示が一切残らない"""
    leaked = []
    for segment in parse_script_segments(GEMINI_WITH_SONG_MARKERS):
        text = clean_script_for_tts(segment.content)
        for token in ("音楽が", "フェードアウト", "※", "---"):
            if token in text:
                leaked.append((segment.title, token))
    assert not leaked, leaked


# ==============================================================================
# 3. フォールバック原稿は影響を受けない
# ==============================================================================
@pytest.mark.parametrize("year", [1955, 1975, 1995, 2025])
def test_fallback_script_still_yields_five_segments(year):
    """定型原稿も 5 セグメントのまま（曲マーカー除去の副作用が無い）"""
    segments = parse_script_segments(generate_fallback_script(year, 5, 15))
    assert len(segments) == 5, [s.title for s in segments]
    assert [s.order for s in segments] == [0, 1, 2, 3, 4]


# ==============================================================================
# 4. プレイリスト側の対応
# ==============================================================================
def test_playlist_interleaves_each_talk_with_exactly_one_song():
    """5 トークなら 6 曲で「トーク 曲 トーク 曲 … 曲」になる"""
    from retro_radio import server as server_module

    titles = ["オープニング", "トーク1", "トーク2", "トーク3", "エンディング"]
    segments = []
    for i, title in enumerate(titles):
        seg = ScriptSegment(id="seg_%d" % i, title=title, content="本文",
                            estimated_duration=1.0, order=i)
        seg.metadata = {"audio_url": "/api/audio/t%d.mp3" % i}
        segments.append(seg)

    songs = [{"title": "S%d" % i, "artist": "A",
              "preview_url": "https://x/%d.m4a" % i, "is_fallback": False}
             for i in range(6)]
    playlist = server_module.build_playlist(segments, songs, year=1975)
    kinds = [str(item["type"]).upper() for item in playlist]

    assert kinds == ["TALK", "SONG", "TALK", "SONG", "TALK", "SONG",
                     "TALK", "SONG", "TALK", "SONG", "SONG"], kinds
    assert kinds.count("TALK") == 5
    assert kinds.count("SONG") == 6


def test_prompt_forbids_song_marker_headings():
    """プロンプトが曲マーカー見出しを禁止している（再発防止）"""
    for mode in ("normal", "care_recreation", "anniversary"):
        prompt = _build_segmented_prompt(1975, 5, 15, mode=mode)
        assert "厳禁" in prompt, mode
        assert "### 曲1" in prompt, mode


# ==============================================================================
# 5. フロント: segments を正とする（ハイライトのずれ防止）
# ==============================================================================
def _app_js() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )


def test_frontend_renders_manuscript_from_segments():
    """原稿は script の再パースではなく `segments` から描画する"""
    app = _app_js()
    assert "function renderSegmentsHtml" in app
    assert "renderSegmentsHtml(segments)" in app
    assert "Array.isArray(data.segments)" in app


def test_render_segments_html_uses_order_as_index():
    """data-segment-index はバックエンドの order を使う"""
    app = _app_js()
    body = app[app.index("function renderSegmentsHtml"):
               app.index("function renderManuscriptHtml")]
    assert 'data-segment-index="' in body
    assert "isFiniteNumber(seg.order) ? seg.order : i" in body


# ==============================================================================
# 6. 曲⇄トークの対応（実測の破綻の回帰防止）
# ==============================================================================
#
# 実測されていた不具合
# --------------------
# 番組は `server.build_playlist` が
#
#     トーク0（オープニング）→ 曲0 → トーク1 → 曲1 → トーク2 → 曲2 → …
#
# と組むため、**i 番目のトークの直後に流れるのは i 番目の曲**。
#
# 一方 `core/fallback.py` も `トーク1 → 曲0` / `トーク2 → 曲1` / … と
# 対応付けていたため、旧実装（曲 → トーク順）では実際に鳴る曲と
# 原稿が告知する曲がずれた位置にありました。
#
# ここでは 3 モードすべてで
# 「各トークが告げる曲 == そのトークの直後に流れる曲」を固定する。

_SONGS = [
    ("ヒット曲A", "歌手A"),
    ("ヒット曲B", "歌手B"),
    ("ヒット曲C", "歌手C"),
    ("ヒット曲D", "歌手D"),
    ("ヒット曲E", "歌手E"),
    ("ヒット曲F", "歌手F"),
]


def _song_dicts():
    return [
        {
            "title": title,
            "artist": artist,
            "preview_url": "https://example.test/%d.mp3" % index,
            "artwork_url": None,
            "is_fallback": False,
        }
        for index, (title, artist) in enumerate(_SONGS)
    ]


def _announced_titles(text):
    return [title for title, _artist in _SONGS if "「%s」" % title in text]


def _cue_mismatches(script):
    """原稿 → セグメント → プレイリストを通し、告知curveがずれるトークを返す。"""
    from retro_radio import server as server_module

    segments = parse_script_segments(script)
    playlist = server_module.build_playlist(segments, _song_dicts(), year=1975)

    mismatches = []
    for index, item in enumerate(playlist):
        if item["type"] != "talk":
            continue
        announced = _announced_titles(item.get("content") or "")
        if not announced:
            continue
        following = None
        if index + 1 < len(playlist) and playlist[index + 1]["type"] == "song":
            following = playlist[index + 1]["title"]
        if announced[0] != following:
            mismatches.append((item.get("title"), announced[0], following))
    return mismatches


def test_normal_script_cue_matches_the_song_that_follows():
    """通常モード: 各トークの告知 == その直後の曲"""
    script = generate_fallback_script(1975, 9, 24, songs=_SONGS)
    assert _cue_mismatches(script) == [], _cue_mismatches(script)


def test_care_script_cue_matches_the_song_that_follows():
    """介護モード: 各トークの告知 == その直後の曲"""
    from retro_radio.core import fallback as fallback_module

    with fallback_module.pinned_songs(_SONGS):
        script = fallback_module.generate_care_script(1975, 9, 24)
    assert _cue_mismatches(script) == [], _cue_mismatches(script)


def test_anniversary_script_cue_matches_the_song_that_follows():
    """記念日モード: 各トークの告知 == その直後の曲"""
    from retro_radio.core import fallback as fallback_module

    with fallback_module.pinned_songs(_SONGS):
        script = fallback_module.generate_anniversary_script(1975, 9, 24, "花子")
    assert _cue_mismatches(script) == [], _cue_mismatches(script)


@pytest.mark.parametrize(
    "builder",
    [
        pytest.param(
            lambda songs: generate_fallback_script(1975, 9, 24, songs=songs), id="normal"
        ),
    ],
)
def test_cue_indexes_start_from_the_second_song(builder):
    """1 番目の曲（オープニング直後の曲）を『次は』として告げない。

    `build_playlist` は 1 番目の曲をオープニングトークの直後に置くため、
    これが中間のトークから「次は」と告げられるとずれた位置になる。
    """
    script = builder(_SONGS)
    opening = next(s for s in parse_script_segments(script) if "オープニング" in s.title)
    assert _announced_titles(opening.content) == [], opening.content


def test_prompt_tells_llm_which_song_follows_each_talk():
    """プロンプトの「曲N ← どの直後」の対応が `build_playlist` と一致すること

    曲名だけの羅列を渡すと LLM は 1 つずらして告知する（実測）。
    そのため「何番目の曲がどの直後に鳴るか」を明示する必要がある。

    実際の並び（``build_playlist``）は
    ``オープニング → 曲1 → トーク1 → 曲2 → トーク2 → …`` なので、

    ==============  ==========================
    一覧の番号      「直後に流れる」のは
    ==============  ==========================
    1              オープニング
    N (2 <= N)     トーク(N - 1)
    ==============  ==========================

    1 つずらすと LLM は「すでに鳴った曲」を『次は』として告げてしまう。
    """
    prompt = _build_segmented_prompt(1975, 9, 24, mode="normal", songs=_SONGS)
    assert "の直後に流れます" in prompt

    def _line_for(title):
        return [line for line in prompt.splitlines() if title in line][0]

    # 1 番目はオープニングの直後
    first = _line_for("ヒット曲A")
    assert "オープニング" in first and "直後に流れます" in first, first

    # 2〜4 番目は トーク1〜トーク3 の直後
    for title, talk in (("ヒット曲B", "トーク1"), ("ヒット曲C", "トーク2"),
                        ("ヒット曲D", "トーク3")):
        line = _line_for(title)
        assert talk in line and "直後に流れます" in line, line

    # 5〜6 番目はエンディングの直後（存在しない「トーク4」を指示しない）
    for title in ("ヒット曲E", "ヒット曲F"):
        line = _line_for(title)
        assert "エンディング" in line and "直後に流れます" in line, line
    assert "トーク4" not in prompt and "トーク5" not in prompt


def test_prompt_cue_map_matches_the_actual_playlist():
    """プロンプトの対応表と `build_playlist` の実際の並びが完全に一致すること

    上のテストは番号ごとの期待値をハードコードするが、ここは
    **実際のプレイリスト построいて**プロンプトの注記と突き合わせる。
    どちらかが1つずれても落ちる。
    """
    from retro_radio import server as server_module

    segments = parse_script_segments(generate_fallback_script(1975, 9, 24, songs=_SONGS))
    playlist = server_module.build_playlist(segments, _song_dicts(), year=1975)

    # 実際のプレイリストを「トーク, 直後に流れる曲」の対にする
    actual_after = {}
    for index, item in enumerate(playlist):
        if item["type"] != "talk" or index + 1 >= len(playlist):
            continue
        if playlist[index + 1]["type"] != "song":
            continue
        actual_after[playlist[index + 1]["title"]] = item["title"]

    assert actual_after, "プレイリストに曲とトークの対が無い"

    # 見出し名（トーク1_ニュース 等）はモード依存なので、番号だけを突き合わせる。
    # プロンプト側の注記は「オープニング / トークN / エンディング」で統一されているため、
    # 位置から期待値を導いて比較する。
    talk_titles = [i["title"] for i in playlist if i["type"] == "talk"]

    def _expected_label(talk_title):
        if talk_title == talk_titles[0]:
            return "オープニング"
        if talk_title == talk_titles[-1]:
            return "エンディング"
        return f"トーク{talk_titles.index(talk_title)}"

    prompt = _build_segmented_prompt(1975, 9, 24, mode="normal", songs=_SONGS)
    for title, talk_title in actual_after.items():
        line = [
            row for row in prompt.splitlines() if f"「{title}」" in row
        ][0]
        assert _expected_label(talk_title) in line, (title, talk_title, line)


# ==============================================================================
# 7. 共通番組フォーマット（オープニング + トーク3 + エンディング）
# ==============================================================================
#
# 3 モードの構成を 1 つに揃えるための契約。
#
#   オープニング → 曲 → トーク1 → 曲 → トーク2 → 曲 → トーク3 →
#   曲 → エンディング → 曲
#
# したがって 1 パスの音源スロットは **6 曲**、トークは **5 個**であり、
# 中間の 3 トークはそれぞれ「その直後に流れる曲」を告げしなければならない。
#
# 実測されていた不具合
# --------------------
# `care_recreation` / `anniversary` の `_decade_songs(year, 4)` は 4 曲しか
# 見ない。「トークN の直後に流れる曲」= ``pinned[N]`` なので、トーク3 は
# ``pinned[3]`` を要求し、範囲外になって `_cue_line`` が空文字を返した。
# 結果として**最後のトークが曲を紹介しないまま終わる**放送になっていた。


def _mode_scripts():
    """3 モードの原稿を、同じ 6 曲の差し込みで生成する。"""
    from retro_radio.core import fallback as fallback_module

    with fallback_module.pinned_songs(_SONGS):
        return {
            "normal": generate_fallback_script(1975, 9, 24, songs=_SONGS),
            "care_recreation": fallback_module.generate_care_script(1975, 9, 24),
            "anniversary": fallback_module.generate_anniversary_script(
                1975, 9, 24, "花子"
            ),
        }


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_every_mode_has_three_talks_between_opening_and_ending(mode):
    """3 モードとも「オープニング + トーク3 + エンディング」になる"""
    segments = parse_script_segments(_mode_scripts()[mode])
    titles = [s.title for s in segments]

    assert len(segments) == 5, titles
    assert "オープニング" in titles[0], titles
    assert "エンディング" in titles[-1], titles


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_every_talk_announces_the_song_that_follows_it(mode):
    """中間の 3 トークがすべて曲を紹介し、告知 == 直後の曲である"""
    assert _cue_mismatches(_mode_scripts()[mode]) == [], _cue_mismatches(
        _mode_scripts()[mode]
    )


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_third_talk_actually_announces_a_song(mode):
    """トーク3 が曲振りを持たない退化（実測）を検出する"""
    from retro_radio import server as server_module

    segments = parse_script_segments(_mode_scripts()[mode])
    playlist = server_module.build_playlist(segments, _song_dicts(), year=1975)
    kinds = [item["type"] for item in playlist]

    # トークで始まり曲で終わる、かつトークが連続しない
    assert kinds[0] == "talk", kinds
    assert kinds[-1] == "song", kinds
    assert kinds.count("song") == 6, kinds
    assert kinds.count("talk") == 5, kinds

    # 末尾（エンディング）を除く 4 つのトークのうち、
    # 「オープニング以外」の 3 つが曲を紹介している
    talks = [item for item in playlist if item["type"] == "talk"]
    middle = talks[1:-1]
    assert len(middle) == 3, [t["title"] for t in talks]
    for talk in middle:
        announced = _announced_titles(talk.get("content") or "")
        following = playlist[playlist.index(talk) + 1]["title"]
        assert announced == [following], (talk["title"], announced, following)
