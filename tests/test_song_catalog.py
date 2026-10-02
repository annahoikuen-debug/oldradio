"""曲カタログ（正本）と選曲ローテーションの回帰テスト。

このファイルが固定しているのは 3 つの要件。

1. **正本に無い曲が开播らない**（``songs.json`` が唯一の事実源）
2. **iTunes のあいまい検索で別の曲を鳴らさない**（一致検証）
3. **同じ年を連続して選んでも曲名が交集しない**（ローテーション）

3 番目は旧実装が満たせていなかったもので、``random.shuffle`` だけだと
1 年 50 曲・1 番組 18 曲でも 2 回目の放送が 1 曲も被らない確率は
数千分の 1 程度しかない。再生履歴を持つことで決定的にできる。
"""

import random

import pytest

from retro_radio.core import songs as songs_mod
from retro_radio.core.preview_resolver import _artist_matches, _pick_matching
from retro_radio.core.song_selector import SongSelector
from retro_radio.core.songs import (
    PROGRAM_SONGS_PER_BROADCAST,
    REQUIRED_FIELDS,
    TARGET_SONGS_PER_YEAR,
    load_songs,
    normalize_song_text,
    pool_for_year,
    song_key,
    songs_for_year,
    year_coverage,
)
from retro_radio.services.song_store import PreviewCache, SongHistoryStore

ALL_YEARS = list(range(1950, 2026))


# ==============================================================================
# 1. 正本の形
# ==============================================================================
def test_every_record_has_the_required_fields():
    for record in load_songs():
        for field in REQUIRED_FIELDS:
            assert record.get(field) not in (None, ""), (record.get("id"), field)


def test_every_record_declares_a_release_year_inside_the_supported_range():
    for record in load_songs():
        year = record["release_year"]
        assert 1950 <= year <= 2025, (record["id"], year)


def test_song_ids_are_unique():
    ids = [record["id"] for record in load_songs()]
    assert len(set(ids)) == len(ids)


def test_no_two_records_normalize_to_the_same_song():
    """正規化後に同じ「曲名 + アーティスト」が 2 つ無いこと

    重複があると「1 番組で 1 回」の保証とローテーションが壊れる。
    """
    seen = {}
    for record in load_songs():
        key = song_key(record["title"], record["artist"])
        assert key not in seen, (record["id"], seen.get(key), record["title"])
        seen[key] = record["id"]


def test_ranks_are_dense_and_start_at_one_within_each_year():
    by_year = {}
    for record in load_songs():
        by_year.setdefault(record["release_year"], []).append(record["rank"])
    for year, ranks in by_year.items():
        assert sorted(ranks) == list(range(1, len(ranks) + 1)), year


def test_json_is_in_sync_with_the_authoring_tsv():
    """``songs.tsv`` を直して再生成し忘れると、編集内容がアプリに届かない。

    CI ゲート（``scripts/validate_songs.py --check``）と同じ検査を
    pytest 側でも行い、pytest 単体でも落ちるようにする。
    """
    from scripts.build_song_catalog import SOURCE_TSV, parse_tsv, render_json

    if not SOURCE_TSV.exists():
        pytest.skip("編集用 TSV が無い")

    target = songs_mod.SONGS_DIR / "songs.json"
    assert target.read_text(encoding="utf-8") == render_json(parse_tsv(SOURCE_TSV)), (
        "songs.json が songs.tsv と同期していません。"
        "`python scripts/build_song_catalog.py` を実行してください。"
    )


def test_validator_reports_no_fatal_finding():
    """静的 CI ゲートが fail を出さないこと（ネットワーク不要の範囲）"""
    from scripts.validate_songs import validate_all

    fails, _warns = validate_all(check_tsv=True)
    assert fails == [], fails


# ==============================================================================
# 2. 年の解決
# ==============================================================================
@pytest.mark.parametrize("year", ALL_YEARS)
def test_every_supported_year_can_produce_at_least_one_song(year):
    """1950〜2025 のどの年でも、選曲プールが空にならないこと

    ここで空になると、1 番組の曲スロットが埋まらずトークが連続する。
    """
    assert pool_for_year(year, wanted=1), year


def test_exact_year_pool_comes_first_before_neighbouring_years():
    """``tolerance`` を広げても、対象年の曲が先に全部並ぶ"""
    exact = songs_for_year(1975, tolerance=0)
    widened = songs_for_year(1975, tolerance=1)
    assert exact, "1975 年の曲が無い"
    assert widened[: len(exact)] == exact


def test_widening_happens_only_when_the_exact_year_is_short():
    """対象年だけで足りていれば、隣接年・10 年帯の曲に混ざらない"""
    assert pool_for_year(1975, wanted=1) == songs_for_year(1975, tolerance=0)


def test_year_coverage_matches_the_records():
    counts = {}
    for record in load_songs():
        counts[record["release_year"]] = counts.get(record["release_year"], 0) + 1
    assert year_coverage() == counts


# ==============================================================================
# 3. iTunes の一致検証（誤再生の防止）
# ==============================================================================
def test_pick_matching_accepts_an_exact_title_and_artist_match():
    results = [{
        "trackName": "Lemon",
        "artistName": "米津玄師",
        "previewUrl": "http://x/1.m4a",
    }]
    assert _pick_matching(results, "Lemon", "米津玄師") is results[0]


def test_pick_matching_rejects_a_different_song_by_the_same_artist():
    """実測の誤再生: 「卒業写真（荒井由実）」→ ルージュの伝言

    iTunes の候補から先頭 1 件を取る旧実装では、これで別の曲が流れていた。
    """
    results = [{
        "trackName": "ルージュの伝言",
        "artistName": "荒井由実",
        "previewUrl": "http://x/1.m4a",
    }]
    assert _pick_matching(results, "卒業写真", "荒井由実") is None


def test_pick_matching_rejects_a_different_artist():
    """実測の誤再生: 「六本木心中（ゆり）」→ 雪の華（Ms.OOJA）"""
    results = [{
        "trackName": "雪の華",
        "artistName": "Ms.OOJA",
        "previewUrl": "http://x/1.m4a",
    }]
    assert _pick_matching(results, "六本木心中", "ゆり") is None


def test_pick_matching_accepts_a_rerecording_of_the_same_song():
    """実測: 「神田川」→ 神田川(2014年新録音)

    方針は「**同じ曲なら鳴らす・別の曲なら黙る**」。同じ奏者による
    再録は同一の曲なので採用する（台本は録音年を語らないため、
    虚偽の主張にならない）。「別の曲」は拒否する（下のテスト）。
    """
    results = [{
        "trackName": "神田川(2014年新録音)",
        "artistName": "南こうせつ",
        "previewUrl": "http://x/1.m4a",
    }]
    assert _pick_matching(results, "神田川", "南こうせつとかぐや姫") is results[0]


def test_pick_matching_still_rejects_the_same_title_by_an_unrelated_artist():
    """曲名が同じでも奏者が違えば採用しない"""
    results = [{
        "trackName": "神田川(2014年新録音)",
        "artistName": "知らない歌手",
        "previewUrl": "http://x/1.m4a",
    }]
    assert _pick_matching(results, "神田川", "南こうせつとかぐや姫") is None


def test_pick_matching_still_accepts_a_reissue_with_the_same_title():
    """曲名もアーティストも一致する再録は採用する（別曲ではない）"""
    results = [{
        "trackName": "Pretender",
        "artistName": "Official髭男dism",
        "previewUrl": "http://x/1.m4a",
    }]
    assert _pick_matching(results, "Pretender", "Official HIGE DANdism") is results[0]


def test_pick_matching_ignores_candidates_without_a_preview():
    results = [{"trackName": "Lemon", "artistName": "米津玄師", "previewUrl": None}]
    assert _pick_matching(results, "Lemon", "米津玄師") is None


def test_pick_matching_absorbs_separator_differences():
    """正本「A・B」/ iTunes「A & B」を同一視する（実測で必要だった）"""
    results = [{
        "trackName": "いつでも夢を",
        "artistName": "橋幸夫 & 吉永小百合",
        "previewUrl": "http://x/1.m4a",
    }]
    assert _pick_matching(results, "いつでも夢を", "橋幸夫・吉永小百合") is results[0]


def test_pick_matching_absorbs_known_orthography_differences():
    """正本「Official HIGE DANdism」/ iTunes「Official髭男dism」"""
    results = [{
        "trackName": "Pretender",
        "artistName": "Official髭男dism",
        "previewUrl": "http://x/1.m4a",
    }]
    assert _pick_matching(results, "Pretender", "Official HIGE DANdism") is results[0]


def test_pick_matching_still_rejects_an_unrelated_artist_that_shares_a_prefix():
    """アーティスト名の緩い一致が、無関係なアーティストを通さないこと"""
    results = [{
        "trackName": "Lemon",
        "artistName": "米津玄師Official",
        "previewUrl": "http://x/1.m4a",
    }]
    assert _pick_matching(results, "Lemon", "米津玄師") is results[0], (
        "接頭辞一致は許容する（バンド名の略記があるため）"
    )
    other = _pick_matching(results, "Lemon", "米津玄子")
    assert other is None, "別アーティストを通してはいけない"


def test_normalize_song_text_removes_decoration_and_spacing():
    assert normalize_song_text("Lemon - Single Version") == "lemon"
    assert normalize_song_text("神田川 (2014年新録音)") == "神田川"
    assert normalize_song_text("大塚 愛") == normalize_song_text("大塚愛")


def test_normalize_song_text_keeps_legitimate_hyphens():
    """曲名に含まれるハイフンを無条件に切らないこと"""
    assert normalize_song_text("君だけを-Extradition-") == "君だけを-extradition"


def test_normalize_song_text_strips_version_suffixes():
    """版情報の注記は除去する（再録・ライブ版を同一の曲とみなす）"""
    from retro_radio.core.songs import normalize_song_text

    assert normalize_song_text("神田川(2014年新録)") == "神田川"
    assert normalize_song_text("Lemon (Live)") == "lemon"


def test_artist_matching_requires_a_non_empty_artist():
    assert _artist_matches("", "someone") is False
    assert _artist_matches("someone", "") is False


# ==============================================================================
# 4. ローテーション（前回放送と被らない）
# ==============================================================================
@pytest.fixture
def synthetic_catalog(monkeypatch):
    """1 年 50 曲のカタログを一時的に差し込む。

    実際の正本にはまだ 50 曲揃っていない年があるため、ローテーションの
    性質（プール 50 / 1 番組 18）を**実装の動作**で検証する。
    """
    records = [
        {
            "id": f"t-{index:03d}",
            "title": f"テスト曲{index:02d}",
            "artist": f"テスト歌手{index % 7}",
            "release_year": 1990,
            "rank": index + 1,
            "source": "test",
            "confidence": "verified",
        }
        for index in range(50)
    ]
    monkeypatch.setattr(songs_mod, "_load_cached", lambda: tuple(records))
    for cache in (
        songs_mod._by_year_cached,
        songs_mod._by_id_cached,
        songs_mod._by_key_cached,
    ):
        cache.cache_clear()
    yield records
    for cache in (
        songs_mod._by_year_cached,
        songs_mod._by_id_cached,
        songs_mod._by_key_cached,
    ):
        cache.cache_clear()


@pytest.fixture
def all_previews_available(monkeypatch):
    """**全曲に音源がある**状況を再現する。

    ``conftest.mock_itunes`` は旧 ``music_search`` を差替るので
    現実の経路（``preview_resolver._search_itunes``）を通らない。
    プレイリストの曲スロットは ``preview_url`` が無いと間奏になる
    ため、ローテーションの性質を検証するには音源が要る。
    """
    from urllib.parse import parse_qs, urlparse

    from retro_radio.core import preview_resolver as pr

    def _fake_get(url, *args, **kwargs):
        # ``requests`` は ``params=`` でクエリ組み立てるため、URL には
        # クエリ文字列が含まれないことがある。両方を見る。
        raw = kwargs.get("params") or parse_qs(urlparse(url).query)
        if not isinstance(raw, dict):
            raw = {k: v[0] for k, v in raw.items()}
        terms = [raw.get("term")] if isinstance(raw, dict) else [""]
        words = (terms[0] or "").split(" ")
        title = words[0] if words else ""
        artist = words[1] if len(words) > 1 else ""
        result = {
            "trackName": title,
            "artistName": artist,
            "previewUrl": "http://example.test/a.m4a",
            "artworkUrl100": None,
        }
        return type(
            "R", (), {
                "status_code": 200,
                "raise_for_status": lambda s: None,
                "json": lambda s: {"resultCount": 1, "results": [result]},
            },
        )()

    monkeypatch.setattr(pr.requests, "get", _fake_get)
    pr._reset_breaker()
    yield
    pr._reset_breaker()


def _titles(records):
    return [song_key(r["title"], r["artist"]) for r in records]


def test_consecutive_broadcasts_never_share_a_song(synthetic_catalog, tmp_path):
    """同じ年を 3 回続けて作っても、曲が 1 曲も被らない

    これが今回の中心的な要件。1 番組 18 曲・1 年 50 曲なので、
    3 回で 54 枠 > 50 曲 となり **3 回目の中だけ**重複が許される。
    """
    selector = SongSelector(history=SongHistoryStore(str(tmp_path / "h.db")))
    batches = [_titles(selector.select(1990, 18)) for _ in range(3)]

    for first, second in zip(batches, batches[1:]):
        overlap = set(first) & set(second)
        assert not overlap, f"連続する放送で {len(overlap)} 曲が重複: {sorted(overlap)}"


def test_every_song_is_used_once_before_any_is_repeated(synthetic_catalog, tmp_path):
    """50 曲を使い切るまで、どの曲も 2 回目に回らない（偏って特定の曲だけ流さない）"""
    selector = SongSelector(history=SongHistoryStore(str(tmp_path / "h.db")))
    seen = []
    for _ in range(3):
        seen.extend(_titles(selector.select(1990, 10)))
    assert len(seen) == 30
    assert len(set(seen)) == 30, "50 曲プールなら 30 枠は重複してはいけない"


def test_history_survives_a_new_process(tmp_path):
    """ストアを作り直しても履歴が効く（プロセス内 random だけでは不十分）"""
    path = str(tmp_path / "h.db")
    first = SongSelector(history=SongHistoryStore(path)).select(1990, 2)
    second = SongSelector(history=SongHistoryStore(path)).select(1990, 2)
    assert not (set(_titles(first)) & set(_titles(second)))


def test_order_candidates_does_not_mutate_its_input(synthetic_catalog):
    """並び替えは新リストを返す（呼び出し側の再利用のため）"""
    selector = SongSelector(history=None, rng=random.Random(0))
    candidates = songs_for_year(1990)
    snapshot = list(candidates)
    selector.order_candidates(1990, candidates)
    assert candidates == snapshot


def test_select_never_returns_an_empty_list_even_with_a_tiny_pool(tmp_path):
    """プールが足りなくても**空は返さない**（曲スロットが埋まらないため）"""
    selector = SongSelector(history=SongHistoryStore(str(tmp_path / "h.db")))
    picked = selector.select(1950, 18)
    assert picked, "1 曲も返らないと番組の曲スロットが埋まらない"


def test_peek_does_not_consume_history(synthetic_catalog, tmp_path):
    """``peek`` は記録しない（プレビューや UI の確認用）"""
    history = SongHistoryStore(str(tmp_path / "h.db"))
    selector = SongSelector(history=history)
    selector.peek(1990, 5)
    assert history.last_played(1990) == {}
    selector.select(1990, 5)
    assert len(history.last_played(1990)) == 5


def test_selection_is_deterministic_for_a_fixed_seed(synthetic_catalog, tmp_path):
    """同じ乱数シードなら同じ曲順になる（テスト可能性）"""
    first = SongSelector(
        history=SongHistoryStore(str(tmp_path / "a.db")), rng=random.Random(42)
    ).select(1990, 6)
    second = SongSelector(
        history=SongHistoryStore(str(tmp_path / "b.db")), rng=random.Random(42)
    ).select(1990, 6)
    assert _titles(first) == _titles(second)


# ==============================================================================
# 4b. API を通した通し検証（受け渡しまで含めて重複ゼロ）
# ==============================================================================
def _songs_in_pass(playlist):
    """1 パスから「**実際に鳴る曲**」だけを取り出す。

    ``type == "song"`` は音源の無いスロット（間奏）も含む。サーバは
    音源が無いスロットを間奏として，但仍 ``type: "song"`` を返すので、
    「曲として流通する_slot」だけを数えるには ``preview_url`` が
    無いものを除外する必要がある（間奏は曲名を持たない）。
    """
    return [
        song_key(item["title"], item.get("artist") or "")
        for item in playlist
        if item.get("type") == "song" and item.get("preview_url")
    ]


def _intermission_slots_in_pass(playlist):
    """1 パスから音源の無いスロット（間奏）だけを取り出す。"""
    return [
        item for item in playlist
        if item.get("type") == "song" and not item.get("preview_url")
    ]


def test_api_returns_one_distinct_playlist_per_pass(synthetic_catalog, client, all_previews_available):
    """`passes` のパスごとに別の曲が入っており、パス内で重複しない"""
    from retro_radio.config import get_settings

    data = client.post(
        "/api/generate", json={"year": 1990, "month": 9, "day": 24, "mode": "normal"}
    ).json()

    passes = data["passes"]
    assert len(passes) == data["loop_count"] == get_settings().program_loop_count

    for index, playlist in enumerate(passes):
        kinds = [item["type"] for item in playlist]
        assert kinds[0] == "song", f"パス{index + 1} は曲で始まるべき"
        assert kinds[-1] == "song", f"パス{index + 1} は曲で終わるべき"
        songs = _songs_in_pass(playlist)
        assert len(set(songs)) == len(songs), f"パス{index + 1} 内で曲名が重複"

    # 1 パス = トーク数 + 1 曲
    for index, playlist in enumerate(passes):
        talks = sum(1 for item in playlist if item["type"] == "talk")
        assert len(_songs_in_pass(playlist)) == talks + 1, index


def test_api_broadcasts_do_not_repeat_songs_within_a_year(synthetic_catalog, client, all_previews_available):
    """同じ年で 3 回続けて作っても、曲が 1 曲も被らない（目標 50 曲/年）"""
    broadcasts = []
    for _ in range(3):
        data = client.post(
            "/api/generate", json={"year": 1990, "month": 9, "day": 24, "mode": "normal"}
        ).json()
        broadcasts.append(
            {song for playlist in data["passes"] for song in _songs_in_pass(playlist)}
        )

    for first, second in zip(broadcasts, broadcasts[1:]):
        overlap = first & second
        assert not overlap, f"連続する放送で {len(overlap)} 曲が重複: {sorted(overlap)}"
    # 50 曲プール・18 曲 x 3 回 = 54 枠なので、3 回目は一部を再利用する
    assert len(broadcasts[0]) == 18, broadcasts[0]


def test_api_playlist_field_stays_backward_compatible(synthetic_catalog, client):
    """旧クライアントは `playlist`（= passes[0]）だけを見る"""
    data = client.post(
        "/api/generate", json={"year": 1990, "month": 9, "day": 24, "mode": "normal"}
    ).json()
    assert data["playlist"] == data["passes"][0]


# ==============================================================================
# 5. プレビューキャッシュ
# ==============================================================================
def test_preview_cache_remembers_a_positive_result(tmp_path):
    cache = PreviewCache(str(tmp_path / "p.db"))
    cache.put(
        "key",
        "http://x/1.m4a",
        "http://x/1.jpg",
        "https://music.apple.com/jp/album/x/1",
    )
    assert cache.get("key") == {
        "preview_url": "http://x/1.m4a",
        "artwork_url": "http://x/1.jpg",
        # Apple への送客導線も一緒に覚える（次の番組で再解決しない）。
        "track_view_url": "https://music.apple.com/jp/album/x/1",
    }


def test_preview_cache_remembers_a_negative_result(tmp_path):
    """音源が無いことも覚える（毎回 HTTP し直さないため）"""
    cache = PreviewCache(str(tmp_path / "p.db"))
    cache.put("key", None, None)
    assert cache.get("key") == {
        "preview_url": None,
        "artwork_url": None,
        "track_view_url": None,
    }


def test_preview_cache_expires_positive_results_but_not_negative_ones(tmp_path):
    stale = PreviewCache(str(tmp_path / "p.db"), ttl_seconds=-1)
    stale.put("pos", "http://x/1.m4a", None)
    assert stale.get("pos") is None, "古い肯定結果は再解決させる"
    stale.put("neg", None, None)
    assert stale.get("neg") == {
        "preview_url": None,
        "artwork_url": None,
        "track_view_url": None,
    }


def test_preview_cache_returns_none_for_an_unknown_key(tmp_path):
    assert PreviewCache(str(tmp_path / "p.db")).get("nope") is None


# ==============================================================================
# 6. 目標曲数（現状の不足を可視化し続ける）
# ==============================================================================
def test_catalog_documents_how_far_it_is_from_the_target():
    """1 年 50 曲という目標との差分を、テストとして残す

    MusicBrainz から 1950〜2025 年の候補を取り込んだ結果、**50 曲に達した年
    が出た**（1953・1954・1972・2005 の 4 年が未収録）。このテストは
    「進みも後退も無いこと」を見えるようにするためのもので、
    データが増えたときは数値を更新する（上の docstring のとおり）。

    ここで守るべきは「目標年_Ominous の曲数が静かに減っていないこと」と
    「1 番組（18 曲）を目標年の曲だけで埋められる年が
    50 個以上あること」の 2 点。
    """
    coverage = year_coverage()
    at_target = sorted(y for y, c in coverage.items() if c >= TARGET_SONGS_PER_YEAR)
    enough = sorted(y for y, c in coverage.items() if c >= PROGRAM_SONGS_PER_BROADCAST)

    # 1 番組を「対象年の曲だけで」埋められる年が 50 年以上あること。
    # 0 曲のまま放置されている年があると、読み上げ原稿が間奏だらけになる。
    assert len(enough) >= 50, (
        f"1 番組分（{PROGRAM_SONGS_PER_BROADCAST} 曲）を集められる年が "
        f"{len(enough)} 個しかない。カタログが壊れていないか確認すること"
    )

    # 目標到達の年が「以前より減っている」なら後退。
    assert len(at_target) >= 40, (
        f"50 曲到達の年が {len(at_target)} 個まで減った: {at_target}"
    )

    # 未収録の年。ログに出続けるだけなので、記録に残す。
    #
    # 1972 / 1996 / 2005 は 2026-10 に MusicBrainz から取り直し、iTunes
    # playable 照合を通過した分だけ合流させた。残る 1953 / 1954 は
    # `lang:jpn` で 1 件も返らず（`country:JP` にするとジャズ・洋楽の
    # リリースしか出ない）、日本の歌謡カタログとして不適切なので
    # 空のまま残す。`docs/song_catalog.md` 6.2 に理由を書いている。
    missing = [y for y in range(1950, 2026) if y not in coverage]
    assert missing == [1953, 1954], (
        f"未収録の年が変わった: {missing}（新規取得ならここを基準に更新する）"
    )


# --------------------------------------------------------------------------- #
# 重み付け選曲（上位ヒットを優先）
# --------------------------------------------------------------------------- #
def _ranked(year: int, count: int, confidence: str) -> list:
    """rank 1..count の合成レコードを作る（重み付けの検証用）。"""
    return [
        {
            "title": f"S{index:02d}",
            "artist": "A",
            "release_year": year,
            "rank": index,
            "confidence": confidence,
        }
        for index in range(1, count + 1)
    ]


def _top_rank_counts(year: int, confidence: str, limit: int, trials: int = 300) -> float:
    """先頭 ``limit`` 曲に入る上位 10 曲（rank<=10）の平均本数を返す。"""
    pool = _ranked(year, 50, confidence)
    total = 0
    for seed in range(trials):
        selector = SongSelector(history=None, rng=random.Random(seed))
        ordered = selector.order_candidates(year, list(pool))[:limit]
        total += sum(1 for record in ordered if int(record["rank"]) <= 10)
    return total / trials


def test_hit_weight_ignores_rank_for_unverified_records():
    """照合済みでない曲に rank で重みを掛けない（出典の無い順位を使わない）"""
    from retro_radio.core.song_selector import _hit_weight

    assert _hit_weight({"confidence": "unverified", "rank": 1}) == 1.0
    assert _hit_weight({"confidence": "unverified", "rank": 50}) == 1.0


def test_hit_weight_scales_with_rank_for_verified_records():
    """照合済みなら rank が小さいほど重みが大きい"""
    from retro_radio.core.song_selector import _hit_weight

    top = _hit_weight({"confidence": "verified", "rank": 1})
    middle = _hit_weight({"confidence": "verified", "rank": 25})
    bottom = _hit_weight({"confidence": "verified", "rank": 50})
    assert top > middle > bottom > 0


def test_hit_weight_falls_back_when_fields_are_broken():
    """``confidence`` / ``rank`` が欠けていても 1.0 に落ちる（選曲が止まらない）"""
    from retro_radio.core.song_selector import _hit_weight

    assert _hit_weight({}) == 1.0
    assert _hit_weight({"confidence": "verified"}) == 1.0
    assert _hit_weight({"confidence": "verified", "rank": 0}) == 1.0
    assert _hit_weight({"confidence": "verified", "rank": "not-a-number"}) == 1.0


def test_verified_ranks_pull_top_hits_into_the_program():
    """照合済みの年では上位ヒットが等確率より多く選ばれる"""
    baseline = PROGRAM_SONGS_PER_BROADCAST * 10 / 50
    actual = _top_rank_counts(1990, "verified", PROGRAM_SONGS_PER_BROADCAST)
    assert actual > baseline * 1.5, (
        f"上位 10 曲のうち、1 番組に入るのは平均 {actual:.2f} 本しかない"
        f"（等確率なら {baseline:.2f} 本）"
    )


def test_unverified_ranks_stay_uniform():
    """正本の現状（全て unverified）では等確率のままである

    rank が MusicBrainz の検索順である現状で、この重み付けを使うと
    「ヒット成績ではない並び」を「成績」として扱うことになるため、
    ``confidence`` が ``verified`` のときだけ効くこと。
    """
    baseline = PROGRAM_SONGS_PER_BROADCAST * 10 / 50
    actual = _top_rank_counts(1990, "unverified", PROGRAM_SONGS_PER_BROADCAST)
    assert abs(actual - baseline) < baseline * 0.25, (
        f"未照合の曲にも偏りが現れている: 平均 {actual:.2f} 本（等確率 {baseline:.2f} 本）"
    )


def test_weighted_order_is_deterministic_for_a_fixed_seed():
    """同じ乱数シードなら同じ曲順になる（重み付けが入っても決定論を保つ）"""
    pool = _ranked(1990, 50, "verified")
    first = SongSelector(
        history=None, rng=random.Random(1234)
    ).order_candidates(1990, list(pool))
    second = SongSelector(
        history=None, rng=random.Random(1234)
    ).order_candidates(1990, list(pool))
    assert [record["title"] for record in first] == [
        record["title"] for record in second
    ]


def test_weighted_order_keeps_every_song_reachable():
    """重みが最も低い曲も脱落しない（選曲から外れ続けるのを防ぐ）"""
    pool = _ranked(1990, 50, "verified")
    seen = set()
    for seed in range(50):
        selector = SongSelector(history=None, rng=random.Random(seed))
        seen.update(record["title"] for record in selector.order_candidates(1990, list(pool)))
    assert len(seen) == 50
