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
import re

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
    """5 トークなら 6 曲で「曲 トーク 曲 … 曲」になる"""
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

    assert kinds == ["SONG", "TALK", "SONG", "TALK", "SONG", "TALK",
                     "SONG", "TALK", "SONG", "TALK", "SONG"], kinds
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
