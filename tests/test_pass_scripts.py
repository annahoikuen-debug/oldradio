"""パスごとに「実際に鳴る曲」を告げる原稿であること（嘘の放送の防止）。

回帰の経緯
----------
2 パス目以降の原稿を作る実装は `_step_parse_script` に置かれていたが、
2 つの理由から期待どおりに動いておらず、 `passes_segments` は
どこからも読まれていなかった（デッドコード）。

1. `_step_music`（音源解決・並び替え・1 パス分の曲数確定）より**前**に走っていた。
   そのため 1 パスの区切りも音源の並びも確定前であり、
   「司会が告げる曲」と「実際に流れる曲」の対応は保証されなかった。
2. 生成結果が `ctx.passes_segments` に積まれるだけで、
   `_step_playlist` は全パスで 1 パス目の `ctx.segments` を使っていた。

修正: 生成を `_step_music` の後へ移動し（`_step_pass_scripts`）、
`_step_playlist` がパス別のトークを使うように配線した。
"""

from typing import Any, Dict, List

import pytest


def _make_ctx(loop_count: int = 3, per_pass: int = 3):
    from retro_radio.models.radio import ScriptSegment
    from retro_radio.server import GenerateRequest, _GenerationContext

    ctx = _GenerationContext(GenerateRequest(year=1975, month=9, day=30))
    ctx.loop_count = loop_count
    ctx.per_pass_song_count = per_pass
    ctx.segments = [
        ScriptSegment(
            id="seg_0", title="オープニング", content="本文0",
            estimated_duration=1.0, order=0,
        ),
        ScriptSegment(
            id="seg_1", title="トーク", content="本文1",
            estimated_duration=1.0, order=1,
        ),
        ScriptSegment(
            id="seg_2", title="エンディング", content="本文2",
            estimated_duration=1.0, order=2,
        ),
    ]
    return ctx


def _enriched(names: List[str]) -> List[Dict[str, Any]]:
    return [
        {
            "trackId": "t%d" % i,
            "trackName": name,
            "artistName": "歌手%s" % i,
            "releaseYear": 1975,
            "previewUrl": "https://example.test/%s.m4a" % name,
            "artworkUrl100": None,
        }
        for i, name in enumerate(names)
    ]


def _real_enriched(year: int = 1975, count: int = 7) -> List[Dict[str, Any]]:
    """正本カタログのOTAL曲（原稿の曲名許可リストを通過するために実在曲を使う）"""
    from retro_radio.core.songs import songs_for_year

    songs = songs_for_year(year, tolerance=0)[:count]
    assert len(songs) >= count, f"{year} 年のカタログが薄すぎます"
    return [
        {
            "trackId": song["id"],
            "trackName": song["title"],
            "artistName": song["artist"],
            "releaseYear": song.get("release_year", year),
            "previewUrl": "https://example.test/%s.m4a" % song["id"],
            "artworkUrl100": None,
        }
        for song in songs
    ]


@pytest.fixture
def stub_tts(monkeypatch):
    """パス別原稿の TTS をネットワーク無しで通す"""
    from retro_radio import server as srv

    def fake_tts(segments, tenant_id=None, job=None):
        return segments

    monkeypatch.setattr(srv, "generate_tts_for_segments", fake_tts)


def test_pass_two_announces_pass_two_songs(monkeypatch, stub_tts):
    """2 パス目のトークが「2 パス目で流れる曲」を名ざす"""
    from retro_radio import server as srv

    ctx = _make_ctx()
    ctx.enriched = _real_enriched()
    names = [item["trackName"] for item in ctx.enriched]

    srv._step_pass_scripts(ctx)

    assert len(ctx.passes_segments) == 3
    second = "\n".join(s.content for s in ctx.passes_segments[1])
    third = "\n".join(s.content for s in ctx.passes_segments[2])
    # 自分のパスの可聴曲から 1 曲以上は名ざす
    assert any(names[i] in second for i in (3, 4, 5))
    # 1 パスの曲を 2 パス目が名ざないこと（嘘の放送）
    assert not any(names[i] in second for i in (0, 1, 2))
    # 3 パス目は 2 パス目の曲を名ざさない
    assert not any(names[i] in third for i in (0, 1, 2, 3, 4, 5))


def test_playlist_uses_pass_specific_tokens(monkeypatch, stub_tts):
    """`_step_playlist` がパス別のトークを使う（1 パス目の原稿を使い回さない）"""
    from retro_radio import server as srv

    ctx = _make_ctx()
    ctx.enriched = _real_enriched()

    srv._step_pass_scripts(ctx)
    monkeypatch.setattr(
        srv, "_to_song_dicts", lambda records, *a, **k: [
            {
                "title": r["trackName"],
                "artist": r["artistName"],
                "preview_url": r["previewUrl"],
                "artwork_url": None,
            }
            for r in records
        ],
        raising=False,
    )
    srv._step_playlist(ctx)

    talk_2 = [item for item in ctx.passes[1] if item["type"] == "talk"]
    assert talk_2, "2 パス目にトークがありません"
    ids_2 = {s.id for s in ctx.passes_segments[1]}
    for item in talk_2:
        assert item["id"] in ids_2


def test_no_playable_songs_falls_back_to_pass_one(stub_tts):
    """2 パス目で鳴らせる曲が無いとき、曲名を告げない 1 パス目の原稿に委譲する"""
    from retro_radio import server as srv

    ctx = _make_ctx()
    ctx.enriched = _real_enriched(count=3)
    ctx.enriched[0]["previewUrl"] = None

    srv._step_pass_scripts(ctx)

    assert ctx.passes_segments[1] is ctx.segments
    assert ctx.passes_segments[2] is ctx.segments


def test_single_loop_skips_extra_scripts():
    """1 周だけならパス別原稿は作らない（1 パス == 全パス）"""
    from retro_radio import server as srv

    ctx = _make_ctx(loop_count=1)
    ctx.enriched = _real_enriched(count=3)

    srv._step_pass_scripts(ctx)

    assert ctx.passes_segments == [ctx.segments]


def test_generation_failure_falls_back_to_pass_one(monkeypatch, stub_tts):
    """追加パスの原稿生成が落ちても番組全体は落とさない"""
    from retro_radio import server as srv

    def boom(*args, **kwargs):
        raise RuntimeError("生成に失敗")

    monkeypatch.setattr(srv, "_deterministic_script", boom)
    ctx = _make_ctx()
    ctx.enriched = _real_enriched()

    srv._step_pass_scripts(ctx)

    assert ctx.passes_segments[1] is ctx.segments
    assert ctx.passes_segments[2] is ctx.segments


def test_pass_scripts_step_runs_after_music_step():
    """パイプライン順の固定: 音源解決 → パス別原稿 → プレイリスト"""
    from retro_radio import server as srv

    names = [f.__name__ for f in srv.GENERATION_STEPS]
    assert names.index("_step_music") < names.index("_step_pass_scripts")
    assert names.index("_step_pass_scripts") < names.index("_step_playlist")


def test_pass_song_pairs_skips_silent_slots_and_duplicates():
    from retro_radio.server import _pass_song_pairs

    items = _enriched(["A1", "A1", "A2", "A3"])
    items[1]["artistName"] = items[0]["artistName"]  # 同名同アーティスト
    items[2]["previewUrl"] = None  # 間奏

    assert _pass_song_pairs(items) == [("A1", items[0]["artistName"]), ("A3", "歌手3")]
