"""正本カタログと事実レジストリの堅牢性（提案 8 の hardening）の契約テスト。

このファイルが守るもの:

1. 曲カタログの実測サイズが監視できる（``catalog_health``）ことと、
   要求数を満たせないときは **ERROR で可視化される**こと。
2. バッチ経路（``core.pipeline``）が**選曲を 1 回だけ**行い、その結果を
   原稿とプレイリストの**両方**へ渡していること。
3. 事実レジストリのレコードが壊れていても**import が落ちない**こと。
4. 事実レジストリの ``confidence`` が ``ALLOWED_CONFIDENCE`` で
   縛られていること。
"""

import json
import logging

import pytest

from retro_radio.core import facts as facts_mod
from retro_radio.core import songs as songs_mod
from retro_radio.core.songs import (
    PROGRAM_SONGS_PER_BROADCAST,
    TARGET_SONGS_PER_YEAR,
    catalog_health,
    load_songs,
    pool_for_year,
    year_coverage,
)

START_YEAR = 1950
END_YEAR = 2025
ALL_YEARS = list(range(START_YEAR, END_YEAR + 1))


# ---------------------------------------------------------------------------
# 1. カタログの大きさ vs ドキュメント上の契約
# ---------------------------------------------------------------------------
def test_catalog_health_reports_the_real_size():
    """契約値（目標）と実測値が同じ出口から読めること"""
    health = catalog_health()
    assert health["total"] == len(load_songs())
    assert health["years"] == len(year_coverage())
    assert health["target"] == TARGET_SONGS_PER_YEAR
    assert health["program_songs"] == PROGRAM_SONGS_PER_BROADCAST
    assert health["coverage"] == dict(year_coverage())
    assert health["thin_year_count"] == len(health["thin_years"])
    # 「1 番組（18 曲）を対象年の曲だけで埋められる年」は多数在る。
    # カタログ拡充前は 0 個だったが、拡充により増加した。この値が 0 に
    # 戻ったら**カタログが壊れた**（読み込み失敗など）。
    assert len(health["sufficient_years_for_program"]) >= 55, (
        f"1 番組分を揃えられる年が "
        f"{len(health['sufficient_years_for_program'])} 個しか無い"
    )
    # ``ok`` は「1 番組を**対象年の曲だけで**埋められる年があるか」。
    # カタログ拡充前は 0 個（= False）だったが、拡充により True になった。
    # False に戻ったら**後退**なので落とす。
    assert health["ok"] is True, (
        "1 番組を対象年の曲だけで埋められる年が 0 個になった。"
        "カタログが読めていない可能性がある"
    )
    # 目標（1 年 50 曲）自体は未達。thin_year_count で追える。
    assert health["thin_year_count"] > 0, (
        "『1 年 50 曲』契約を達成した。TARGET_SONGS_PER_YEAR を達成済みとして"
        "このテストと docs/song_catalog.md の数値を更新すること。"
    )


def test_catalog_health_reports_missing_years():
    health = catalog_health()
    assert set(health["missing_years"]).isdisjoint(health["coverage"])
    # 1 曲も無い年が黙って存在しない（= 1950-2025 の全部ではない）。
    assert health["missing_years"]


def test_shortfall_is_loud_not_silent(caplog, monkeypatch):
    """要求数を満たせない ``pool_for_year`` は ERROR を出す（黙って繰り返さない）

    **曲カタログを意図的に薄く差し替えて**検証する。出荷時の曲カタログを
    拡張した後はどの年も 18 曲以上集められるため、実データではこの
    不足が再現しなくなった（最小プールがちょうど 18 曲）。
    それでも「不足は黙って埋められない」保証は残したいので、
    データ量ではなく **``pool_for_year`` の挙動**として固定する。
    """
    from retro_radio.core import songs as songs_mod

    records = tuple(
        {
            "id": "s1950-%03d" % (i + 1),
            "title": "架空の曲%02d" % (i + 1),
            "artist": "架空の歌手",
            "release_year": 1950,
            "rank": i + 1,
            "source": "test",
            "confidence": "verified",
        }
        for i in range(2)
    )
    monkeypatch.setattr(songs_mod, "_load_cached", lambda: records)
    for cache in (
        songs_mod._by_year_cached,
        songs_mod._by_id_cached,
        songs_mod._by_key_cached,
    ):
        cache.cache_clear()
    songs_mod._WARNED_COVERAGE.clear()
    try:
        with caplog.at_level(logging.ERROR, logger="retro_radio.core.songs"):
            pool = songs_mod.pool_for_year(1950, PROGRAM_SONGS_PER_BROADCAST)
        assert len(pool) < PROGRAM_SONGS_PER_BROADCAST, "前提が崩れている"
        assert any(
            "選曲プールが不足しています" in record.getMessage()
            for record in caplog.records
        ), [record.getMessage() for record in caplog.records]
    finally:
        for cache in (
            songs_mod._by_year_cached,
            songs_mod._by_id_cached,
            songs_mod._by_key_cached,
        ):
            cache.cache_clear()
        songs_mod._WARNED_COVERAGE.clear()


def test_catalog_load_warns_once_about_thin_coverage(caplog):
    """正本の読み込み時に不足が 1 度だけ WARNING で出る"""
    songs_mod.clear_cache()
    with caplog.at_level(logging.WARNING, logger="retro_radio.core.songs"):
        first = load_songs()
        second = load_songs()
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "目標曲数に届いていません" in record.getMessage()
    ]
    assert first == second
    assert len(warnings) == 1, warnings


@pytest.mark.parametrize("year", ALL_YEARS)
def test_every_year_can_still_produce_at_least_one_song(year):
    """曲_pool が空になる年は無い（空だと番組の骨組みが崩れる）"""
    assert pool_for_year(year, wanted=1), year


# ---------------------------------------------------------------------------
# 2. 台本の許可リストが正本カタログから導出される
# ---------------------------------------------------------------------------
def test_allowlist_comes_from_the_authoritative_catalog():
    """``songs`` 無し時の許可リストは正本カタログの曲だけを含む"""
    from retro_radio.core.script_generator import _resolve_allowlist

    pairs = _resolve_allowlist(None, 1975)
    assert pairs, "正本カタログから許可リストを導出できない"
    catalog_ids = {
        (str(r["title"]), str(r["artist"])) for r in load_songs()
    }
    for title, artist in pairs:
        assert (title, artist) in catalog_ids, (title, artist)


def test_resolved_allowlist_is_used_for_both_prompt_and_check():
    """プロンプトの曲名と検査の許可リストが同じ一覧になること"""
    from retro_radio.core.script_generator import _song_allowance_block, _resolve_allowlist

    pairs = _resolve_allowlist(None, 1975)
    block = _song_allowance_block(None, year=1975)
    for title, artist in pairs:
        assert title in block
        assert artist in block


def test_known_song_titles_come_from_the_catalog():
    """曲名の引用判定に使う集合が正本カタログ由来であること"""
    from retro_radio.core import script_generator as sg

    known = sg._known_song_titles()
    catalog_titles = {str(r["title"]) for r in load_songs()}
    assert known == frozenset(catalog_titles)


# ---------------------------------------------------------------------------
# 3. バッチ経路が「選曲 1 回」を共有する
# ---------------------------------------------------------------------------
def test_batch_pipeline_binds_one_selection_to_script_and_playlist(monkeypatch):
    """バッチ経路: 選曲 1 回、その結果が原稿とプレイリストの両方に載る"""
    import asyncio

    from retro_radio.core import pipeline

    selection = [
        {"trackName": "木綿のハンカチーフ", "artistName": "太田裕美", "previewUrl": None},
        {"trackName": "いい日旅立ち", "artistName": "山口百恵", "previewUrl": None},
    ]
    calls = []

    def fake_select(year, count):
        calls.append((year, count))
        return list(selection)

    def fake_script(year, month, day, mode="normal", target_name=None, songs=None):
        # 台本には「渡された曲名」しか出ないこと（= 選曲結果に束ねられている）。
        assert songs, "バッチ経路が選曲結果を原稿へ渡していない"
        for title, _artist in songs:
            assert title in selection_titles
        return "### オープニング\n" + "".join(f"「{t}」（{a}）" for t, a in songs)

    selection_titles = {item["trackName"] for item in selection}
    monkeypatch.setattr(pipeline, "_select_songs", fake_select)
    monkeypatch.setattr(pipeline, "generate_radio_script", fake_script)
    monkeypatch.setattr(pipeline, "text_to_speech", lambda script: None)

    result = asyncio.run(pipeline.generate_all_async(1975, 9, 24))

    assert len(calls) == 1, calls
    assert result.all_songs == selection
    for item in result.all_songs:
        assert item["trackName"] in result.script


def test_batch_pipeline_fallback_path_also_shares_the_selection(monkeypatch):
    """選曲失敗時もフォールバックの 1 組を原稿とプレイリストで共有する"""
    import asyncio

    from retro_radio.core import pipeline

    seen = {}

    def boom(year, count):
        raise RuntimeError("iTunes unavailable")

    def fake_fallback(year, count=3, **kwargs):
        pair = ("卒業写真", "荒井由実")
        seen["pair"] = pair
        return [pair]

    def fake_script(year, month, day, mode="normal", target_name=None, songs=None):
        seen["songs"] = songs
        return "### オープニング\n原稿"

    monkeypatch.setattr(pipeline, "_select_songs", boom)
    monkeypatch.setattr(pipeline, "generate_radio_script", fake_script)
    monkeypatch.setattr(pipeline, "text_to_speech", lambda script: None)
    monkeypatch.setattr("retro_radio.core.fallback.get_fallback_songs", fake_fallback)

    result = asyncio.run(pipeline.generate_all_async(1975, 9, 24))

    assert seen["songs"] == [("卒業写真", "荒井由実")]
    assert result.all_songs[0]["trackName"] == "卒業写真"
    assert "music_fallback_all" in result.errors


# ---------------------------------------------------------------------------
# 4. 事実レジストリが壊れていても import で落ちない
# ---------------------------------------------------------------------------
MALFORMED_RECORD = {
    # valid_from が整数に変換できない値 → 正規化で落ちる必要がある
    "id": "broken-001",
    "kind": "tv_program",
    "title": "壊れた番組",
    "network": "NHK",
    "start_time": "19:00",
    "valid_from": "nineteen-seventy",
    "valid_to": None,
    "duration_min": 30,
    "claim_ja": "壊れたレコード",
    "description_ja": "壊れたレコード",
    "source": "test",
    "confidence": "unverified",
}

GOOD_RECORD = {
    "id": "good-001",
    "kind": "tv_program",
    "title": "(Projectの七人の刑事)",
    "network": "日本テレビ",
    "start_time": "21:00",
    "valid_from": 1974,
    "valid_to": None,
    "duration_min": 60,
    "claim_ja": "1974年放送開始",
    "description_ja": "1970年代に放送されていた番組",
    "source": "test",
    "confidence": "verified",
    "note_ja": "テスト用のレコード",
}


@pytest.fixture
def _isolated_facts_dir(tmp_path, monkeypatch):
    """``FACTS_DIR`` を tmp へ差し替え、キャッシュと縮退フラグを戻す"""
    monkeypatch.setattr(facts_mod, "FACTS_DIR", tmp_path)
    facts_mod.clear_cache()
    yield tmp_path
    facts_mod.clear_cache()


def _write(path, records):
    path.write_text(
        json.dumps({"records": records}, ensure_ascii=False), encoding="utf-8"
    )


def test_a_malformed_record_is_skipped_not_fatal(_isolated_facts_dir):
    """壊れたレコードが 1 件あっても、正本全体は読めてアプリも動く"""
    _write(
        _isolated_facts_dir / "programs.json",
        [MALFORMED_RECORD, GOOD_RECORD],
    )

    records = facts_mod.load_facts()
    assert [r["id"] for r in records] == ["good-001"]


def test_a_record_that_raises_during_normalization_is_skipped(_isolated_facts_dir):
    """正規化の中で例外が出るレコードでも read 全体が落ちない"""
    hostile = dict(GOOD_RECORD)
    hostile["id"] = "hostile-001"
    # ``valid_from`` を「比較不能な型」にする（dict は rich comparison で TypeError）。
    hostile["valid_to"] = {}
    _write(
        _isolated_facts_dir / "programs.json",
        [hostile, GOOD_RECORD],
    )

    records = facts_mod.load_facts()
    assert [r["id"] for r in records] == ["good-001"]


def test_unreadable_registry_degrades_instead_of_raising(_isolated_facts_dir, caplog):
    """JSON が壊れているときは空へ縮退し、状態を外へ出せる"""
    (_isolated_facts_dir / "programs.json").write_text("{ broken", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="retro_radio.core.facts"):
        assert facts_mod.programs_for_year(1975) == []
        health = facts_mod.facts_health()

    assert health["degraded"] is True
    assert health["records"] == 0
    assert health["reasons"], "縮退理由が空（運用者が診断できない）"
    assert any(
        "事実レジストリを読み込めません" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_healthy_registry_reports_no_degradation(_isolated_facts_dir):
    _write(_isolated_facts_dir / "programs.json", [GOOD_RECORD])
    health = facts_mod.facts_health()
    assert health == {
        "degraded": False,
        "reasons": [],
        "records": 1,
    }


def test_clear_cache_resets_degraded_state(_isolated_facts_dir):
    (_isolated_facts_dir / "programs.json").write_text("{ broken", encoding="utf-8")
    assert facts_mod.facts_health()["degraded"] is True
    _write(_isolated_facts_dir / "programs.json", [GOOD_RECORD])
    facts_mod.clear_cache()
    assert facts_mod.facts_health()["degraded"] is False


# ---------------------------------------------------------------------------
# 5. confidence の強制
# ---------------------------------------------------------------------------
def test_confidence_outside_the_allowed_set_is_rejected(_isolated_facts_dir):
    """``ALLOWED_CONFIDENCE`` は宣言だけでなく検査される"""
    bad = dict(GOOD_RECORD)
    bad["id"] = "bad-confidence"
    bad["confidence"] = "totally-made-up"
    _write(_isolated_facts_dir / "programs.json", [bad, GOOD_RECORD])

    assert [r["id"] for r in facts_mod.load_facts()] == ["good-001"]


def test_allowed_confidence_values_are_accepted(_isolated_facts_dir):
    records = [
        dict(GOOD_RECORD, id="a", confidence="verified"),
        dict(GOOD_RECORD, id="b", confidence="unverified"),
    ]
    _write(_isolated_facts_dir / "programs.json", records)
    assert [r["id"] for r in facts_mod.load_facts()] == ["a", "b"]
    assert facts_mod.facts_health()["degraded"] is False


def test_shipped_facts_registry_only_declares_allowed_confidence():
    """出荷済みの正本レコードが許可値だけであること"""
    for record in facts_mod.load_facts():
        assert record["confidence"] in facts_mod.ALLOWED_CONFIDENCE, record["id"]
