"""番組に「無音の溝（歯抜け）」を作らないことを固定する回帰テスト。

実測されていた不具合
--------------------
1. プレビューキャッシュの**否定結果に TTL がなく**、一時的な失敗が
   恒久的に固定され、その曲が永久に間奏になっていた。
2. 音源解決の**時間予算が尽きた末の曲が無言で None** になっていた。
3. 選曲した曲のうち iTunes プレビューが取れない曲が混ざると、
   パス 2〜3 の曲スロットが**丸ごと無音**になっていた
   （静的カタログ曲＝無音で埋めていたため）。

3 つとも「ラジオ番組として everyday 聴ける」状態になっていなかった。

検証する層は `_step_playlist`（可聴曲优先でパスを組む実装の本体）。
`build_playlist` 単体は「既に十分な曲educations 渡されたら何もしない」仕様なので、
「無音のスロットを入れない」保証はstep 側の責務。
"""
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "retro_radio"


# ==============================================================================
# 1. 否定キャッシュの TTL
# ==============================================================================
def test_negative_cache_entries_expire(tmp_path):
    """「音源なし」の記録にも TTL があって、時間切れで再解決される

    TTL が無いと、ネットワークの一時的な失敗・iTunes 側の障害・
    実装の不具合で入った「音源なし」が**恒久的に固定**される。
    実測で iTunes に存在するはずの曲がこれで無音になり続けていた。
    """
    from retro_radio.services.song_store import PreviewCache

    cache = PreviewCache(str(tmp_path / "preview.db"), negative_ttl_seconds=0.0)
    cache.put("曲名\x00アーティスト", None, None)

    # TTL = 0 なので「記録した瞬間から期限切れ」= 再解決される
    assert cache.get("曲名\x00アーティスト") is None, "否定結果に TTL が効いていない"


def test_negative_cache_is_honoured_within_ttl(tmp_path):
    """TTL 内では「音源なし」を覚えていて叩き続けない"""
    from retro_radio.services.song_store import PreviewCache

    cache = PreviewCache(str(tmp_path / "preview.db"), negative_ttl_seconds=3600.0)
    cache.put("曲名\x00アーティスト", None, None)

    got = cache.get("曲名\x00アーティスト")
    assert got is not None, "TTL 内の否定結果は記憶されるべき"
    assert got["preview_url"] is None


def test_positive_cache_expires(tmp_path):
    """肯定結果の TTL は従来どおり効く"""
    from retro_radio.services.song_store import PreviewCache

    cache = PreviewCache(str(tmp_path / "preview.db"), ttl_seconds=0.0)
    cache.put("曲名\x00アーティスト", "https://example.test/a.m4a", None)
    assert cache.get("曲名\x00アーティスト") is None


# ==============================================================================
# 2. 予算切れで無音にしない
# ==============================================================================
def test_enrich_songs_never_silences_the_tail(monkeypatch):
    """時間予算が尽きても、末尾の曲を**無音にしない**"""
    import time as time_mod

    from retro_radio.core import preview_resolver as pr

    records = [
        {"id": "s1", "title": "曲A", "artist": "歌A", "release_year": 1975},
        {"id": "s2", "title": "曲B", "artist": "歌B", "release_year": 1975},
        {"id": "s3", "title": "曲C", "artist": "歌C", "release_year": 1975},
    ]
    calls = {"n": 0}

    def _fake_resolve(title, artist, cache=None):
        calls["n"] += 1
        if title == "曲A":
            return {"preview_url": "https://example.test/a.m4a",
                    "artwork_url": None}
        raise AssertionError("deadline 後は解決しないこと")

    monkeypatch.setattr(pr, "resolve_preview", _fake_resolve)
    # 既に deadline を過ぎている状態を再現
    out = pr.enrich_songs(records, cache=None, deadline=time_mod.monotonic() - 1)

    assert len(out) == 3, out
    assert calls["n"] == 1, "deadline 超過でも 1 曲めは必ず解決を試みるはず"
    for item in out[1:]:
        assert item["previewUrl"] == "https://example.test/a.m4a", item
        assert item["trackName"] == "曲A", item


# ==============================================================================
# 2.5 レート制限を「音源なし」に倒さない
# ==============================================================================
def _stub_itunes_response(monkeypatch, status, payload=None):
    """`requests.get` を固定ステータスcode の応答に差し替える。"""
    import requests

    from retro_radio.core import preview_resolver as pr

    class _Resp:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code}", response=self)

        def json(self):
            return payload if payload is not None else {"results": []}

    monkeypatch.setattr(pr.requests, "get", lambda *a, **k: _Resp(status))
    pr._reset_breaker()


def test_throttle_status_raises_instead_of_reporting_no_source(monkeypatch):
    """403 / 429 は「音源なし」ではなく**到達不能**として伝播させる

    実測: iTunes は同一 IP からの連打に 403 Forbidden で拒む。
    これが空リスト（＝真のミス）へ倒れると、**実在する曲が「音源なし」と
    判定され、共有キャッシュに否定結果が書き込まれる**。宿主が曲名を
    読み上げても鳴らない状態になる。
    """
    import pytest

    from retro_radio.core import preview_resolver as pr

    for status in (403, 429):
        _stub_itunes_response(monkeypatch, status)
        with pytest.raises(pr.PreviewTransportError):
            pr._fetch_itunes("何か 誰か")
        pr._reset_breaker()


def test_throttle_does_not_write_a_negative_cache_entry(monkeypatch, tmp_path):
    """レート制限で失敗しても「音源なし」をキャッシュに書かない"""
    from retro_radio.core import preview_resolver as pr
    from retro_radio.services.song_store import PreviewCache

    cache = PreviewCache(str(tmp_path / "preview.db"))
    _stub_itunes_response(monkeypatch, 403)

    assert pr.resolve_preview("実在する曲", "実在する歌手", cache=cache) is None

    # 否定キャッシュに「音源なし」が残っていないこと
    assert cache.get(pr.song_key("実在する曲", "実在する歌手")) is None, \
        "レート制限を「音源なし」としてキャッシュに留在させてはいけない"
    pr._reset_breaker()


def test_server_errors_still_raise(monkeypatch):
    """5xx も従来どおり到達不能として扱う"""
    import pytest

    from retro_radio.core import preview_resolver as pr

    _stub_itunes_response(monkeypatch, 503)
    with pytest.raises(pr.PreviewTransportError):
        pr._fetch_itunes("何か 誰か")
    pr._reset_breaker()


def test_genuine_empty_result_is_still_a_miss(monkeypatch):
    """200 で 0 件は「真のミス」のまま（否定キャッシュに記録してよい）"""
    from retro_radio.core import preview_resolver as pr

    _stub_itunes_response(monkeypatch, 200, {"results": []})
    assert pr._fetch_itunes("何か 誰か") == []


# ==============================================================================
# 3. 曲スロットを「鳴る曲」で埋める（_step_playlist の責務）
# ==============================================================================
def _make_ctx(year=1975, loop_count=3, per_pass=6):
    from retro_radio.models.radio import ScriptSegment
    from retro_radio.server import _GenerationContext, GenerateRequest

    ctx = _GenerationContext(GenerateRequest(year=year, month=9, day=30))
    ctx.loop_count = loop_count
    ctx.per_pass_song_count = per_pass
    segments = []
    for i, title in enumerate(["オープニング", "T1", "T2", "T3", "エンディング"]):
        seg = ScriptSegment(
            id="seg_%d" % i, title=title, content="本文",
            estimated_duration=1.0, order=i,
        )
        seg.metadata = {"audio_url": "/api/audio/t%d.mp3" % i}
        segments.append(seg)
    ctx.segments = segments
    return ctx


def _enriched(names_with_preview):
    return [
        {
            "trackId": "s%d" % i,
            "trackName": name,
            "artistName": "歌手",
            "releaseYear": 1975,
            "previewUrl": ("https://x/%s.m4a" % name) if preview else None,
            "artworkUrl100": None,
        }
        for i, (name, preview) in enumerate(names_with_preview)
    ]


def _silent_slots(ctx):
    out = []
    for pi, pl in enumerate(ctx.passes, 1):
        for item in pl:
            if item["type"] == "song" and not item.get("preview_url"):
                out.append((pi, item["title"]))
    return out


def test_all_passes_are_playable():
    """1 パス目の曲に音源があれば、2〜3 パス目も無音にならない"""
    from retro_radio import server as sv

    ctx = _make_ctx()
    # パス 1 だけ全曲に音源があり、パス 2/3 は 1 曲しかない
    ctx.enriched = _enriched(
        [(f"A{i}", True) for i in range(6)]
        + [("B0", True)]
        + [("C0", True)]
    )
    sv._step_playlist(ctx)

    assert _silent_slots(ctx) == [], "無音スロット: %s" % _silent_slots(ctx)
    assert len(ctx.passes) == 3


def test_silent_own_songs_are_replaced_by_playable_ones():
    """自分のパスに音源の無い曲が混ざっても、可聴曲で置き換えられる"""
    from retro_radio import server as sv

    ctx = _make_ctx()
    ctx.enriched = _enriched(
        [("可0", True), ("可1", True), ("可2", True), ("可3", True),
         ("無0", False), ("無1", False), ("無2", False), ("無3", False),
         ("可4", True), ("可5", True)]
    )
    sv._step_playlist(ctx)

    assert _silent_slots(ctx) == [], "無音スロット: %s" % _silent_slots(ctx)


def _played_song_titles(pass_):
    """1 パスから「実際に鳴る曲」の曲名だけを取り出す。

    ``type == "song"`` でも音源の無いスロット（間奏）を含むので、
    ``preview_url`` が無いものは間奏として除外する。
    間奏は曲名を持たない（``INTERMISSION_TITLE``）ため、
    重複判定に含めると「間奏 × 6」で常に重複してしまう。
    """
    return [
        item["title"] for item in pass_
        if item["type"] == "song" and item.get("preview_url")
    ]


def test_no_song_repeats_within_a_single_pass():
    """可聴曲太少でも、**1 パス内で同じ曲を 2 回流さない**"""
    from retro_radio import server as sv

    ctx = _make_ctx()
    # 可聴 2 曲だけ。6 スロットは循環で埋められるが、それは禁止。
    ctx.enriched = _enriched(
        [("可1", True), ("可2", True)] + [("無%d" % i, False) for i in range(16)]
    )
    sv._step_playlist(ctx)

    for pi, pl in enumerate(ctx.passes, 1):
        titles = _played_song_titles(pl)
        assert len(titles) == len(set(titles)), \
            f"パス{pi} で同じ曲が重複: {titles}"


def test_no_song_repeats_when_widened_pool_is_tiny():
    """可聴 1 曲しかない極端な場合も、1 パス内で 2 回出さない"""
    from retro_radio import server as sv

    ctx = _make_ctx()
    ctx.enriched = _enriched(
        [("可1", True)] + [("無%d" % i, False) for i in range(17)]
    )
    sv._step_playlist(ctx)

    for pi, pl in enumerate(ctx.passes, 1):
        titles = _played_song_titles(pl)
        assert len(titles) == len(set(titles)), \
            f"パス{pi} で同じ曲が重複: {titles}"


def test_unplayable_slots_do_not_show_a_song_title():
    """音源が無いスロットは曲名を晒さず「間奏」として出す

    借り物の曲名をそのまま出すと、**司会が一度も紹介していない曲が
    番組表に載る**（利用者から見て嘘になる）。
    """
    from retro_radio import server as sv
    from retro_radio.server import INTERMISSION_TITLE

    ctx = _make_ctx()
    ctx.enriched = _enriched(
        [("可1", True)] + [("無%d" % i, False) for i in range(17)]
    )
    sv._step_playlist(ctx)

    for pi, pl in enumerate(ctx.passes, 1):
        silent = [
            item for item in pl
            if item["type"] == "song" and not item.get("preview_url")
        ]
        assert silent, f"パス{pi} に音源が無いスロットが無い（前提が崩れている）"
        for item in silent:
            assert item["title"] == INTERMISSION_TITLE, (pi, item["title"])
            assert not item.get("artist"), (pi, item)


def test_borrowed_song_name_is_kept_for_diagnosis():
    """借りた曲名は ``metadata.borrowed_song`` に内側だけ残る"""
    from retro_radio import server as sv

    ctx = _make_ctx()
    ctx.enriched = _enriched(
        [("可1", True)] + [("原本%d" % i, False) for i in range(17)]
    )
    sv._step_playlist(ctx)

    borrowed = [
        item["metadata"].get("borrowed_song")
        for pl in ctx.passes
        for item in pl
        if item["type"] == "song" and not item.get("preview_url")
    ]
    assert any(b and b.get("title") for b in borrowed), borrowed


def test_playlist_still_starts_and_ends_with_a_song():
    """曲で始まり曲で終わる構造は維持する（間奏が増えても骨格は保つ）"""
    from retro_radio import server as sv

    ctx = _make_ctx()
    ctx.enriched = _enriched([("可1", True)] + [("無%d" % i, False) for i in range(17)])
    sv._step_playlist(ctx)

    for pi, pl in enumerate(ctx.passes, 1):
        kinds = [i["type"] for i in pl]
        assert kinds[0] == "song" and kinds[-1] == "song", (pi, kinds)
        assert not any(kinds[i] == kinds[i + 1] == "talk"
                       for i in range(len(kinds) - 1)), (pi, kinds)


def test_talks_never_become_adjacent():
    """歯抜け潰しの過程で、トーク同士が連続しないこと"""
    from retro_radio import server as sv

    ctx = _make_ctx()
    ctx.enriched = _enriched([("可%d" % i, True) for i in range(3)])
    sv._step_playlist(ctx)

    for pi, pl in enumerate(ctx.passes, 1):
        kinds = [item["type"] for item in pl]
        assert not any(kinds[i] == kinds[i + 1] == "talk"
                       for i in range(len(kinds) - 1)), (pi, kinds)
        assert kinds[0] == "song" and kinds[-1] == "song", (pi, kinds)
        assert kinds.count("talk") == 5, (pi, kinds)


def test_script_is_told_only_about_playable_songs():
    """プロンプトへ渡す曲一覧に**音源の無い曲**が混ざらない"""
    src = (APP / "server.py").read_text(encoding="utf-8")
    assert "_step_resolve_previews" in src
    steps_at = src.index("GENERATION_STEPS = (")
    block = src[steps_at:steps_at + 500]
    assert block.index("_step_resolve_previews") < block.index("_step_generate_script"), \
        "音源解決は原稿生成より前である必要がある"
    assert 'if item.get("previewUrl")' in src, "可聴曲だけを原稿へ渡していない"
