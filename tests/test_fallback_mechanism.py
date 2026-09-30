"""フォールバック機構の検証。

旧テストは `class DummySettings(Settings): gemini_api_key = ""` で
Pydantic のフィールド定義を上書きしようとして `ValidationError` で落ちていた
（Pydantic v2 はクラス属性によるフィールド上書きを許さない）。
`conftest.py` が API キーを空にした状態で全経路を実行する。
"""

import pytest

from retro_radio.core.fallback import (
    FALLBACK_SONGS,
    REMINISCENCE_DATA,
    generate_anniversary_script,
    generate_care_script,
    generate_fallback_script,
    get_fallback_song,
    get_fallback_songs,
    get_reminiscence_quiz,
)
from retro_radio.config import Settings


# --- 静的マスター ---------------------------------------------------------------
def test_fallback_master_covers_decades():
    """1950年代〜2020年代のマスターが揃っている"""
    for decade in (1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020):
        assert FALLBACK_SONGS.get(decade), f"{decade}年代のマスターが無い"


def test_fallback_song_is_from_master():
    for year in (1950, 1965, 1980, 1995, 2010, 2024):
        decade = (year // 10) * 10
        expected = FALLBACK_SONGS.get(decade) or FALLBACK_SONGS[min(FALLBACK_SONGS, key=lambda d: abs(d - decade))]
        title, artist = get_fallback_song(year)
        assert (title, artist) in expected


def test_fallback_song_for_out_of_range_year_uses_default():
    """範囲外年は既定年に寄せる"""
    settings = Settings()
    title, artist = get_fallback_song(1800)
    assert (title, artist) in FALLBACK_SONGS[(settings.default_year // 10) * 10]


def test_get_fallback_songs_count():
    for year in (1960, 1980, 2000):
        songs = get_fallback_songs(year, count=3)
        assert len(songs) == 3
        for song in songs:
            assert isinstance(song, tuple) and len(song) == 2
            assert isinstance(song[0], str) and isinstance(song[1], str)


def test_get_fallback_songs_has_no_duplicates():
    songs = get_fallback_songs(1975, count=5)
    assert len(set(songs)) == len(songs)


# --- 回想法クイズ ---------------------------------------------------------------
def test_reminiscence_quiz_shape():
    quiz = get_reminiscence_quiz(1965)
    assert isinstance(quiz, list) and quiz
    for item in quiz:
        assert {"question", "answer", "hint"} <= set(item)
        assert all(isinstance(item[k], str) and item[k] for k in ("question", "answer", "hint"))


def test_reminiscence_quiz_uses_nearest_decade():
    """2010年代はデータが無いので直近の年代にマップされる"""
    quiz = get_reminiscence_quiz(2010)
    assert quiz
    assert quiz[0] in REMINISCENCE_DATA[min(REMINISCENCE_DATA, key=lambda d: abs(d - 2010))]


def test_reminiscence_quiz_2000s_is_not_silently_mapped_to_other_bucket():
    """2000年代Ownquiz が 2000 バケットのものを返す（黙って 1990 に落ちない）"""
    quiz = get_reminiscence_quiz(2005)
    assert quiz[0] in REMINISCENCE_DATA[2000]


def test_reminiscence_quiz_out_of_range_uses_default():
    settings = Settings()
    quiz = get_reminiscence_quiz(1800)
    assert quiz
    assert quiz[0] in REMINISCENCE_DATA[(settings.default_year // 10) * 10]


def test_reminiscence_data_is_complete():
    for decade, items in REMINISCENCE_DATA.items():
        assert len(items) >= 3, f"{decade}年代はクイズが足りない"


# --- フォールバック原稿 ---------------------------------------------------------
@pytest.mark.parametrize(
    "builder", [generate_fallback_script, generate_care_script, generate_anniversary_script]
)
def test_fallback_scripts_contain_date(builder):
    script = builder(1975, 9, 24)
    assert "1975年9月24日" in script


def test_fallback_script_is_segmented():
    script = generate_fallback_script(1975, 9, 24)
    assert "### オープニング" in script
    assert "### エンディング" in script


def test_care_script_is_segmented():
    script = generate_care_script(1975, 9, 24)
    assert "### オープニング" in script
    assert "### エンディング" in script
    assert "思い出話" in script


def test_anniversary_script_uses_target_name():
    script = generate_anniversary_script(1975, 9, 24, "花子")
    assert "花子" in script
    assert script.count("花子") >= 2


def test_anniversary_script_has_default_target_name():
    assert "大切なあなた" in generate_anniversary_script(1975, 9, 24)


# --- API 経路 -------------------------------------------------------------------
def test_fallback_flow_works_through_api(client):
    """API キー未設定でもフォールバック原稿で 200 を返す"""
    res = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    )
    assert res.status_code == 200
    data = res.json()
    assert "1975年9月24日" in data["script"]
    assert data["songs"]


def test_care_mode_flow_works_through_api(client):
    res = client.post(
        "/api/generate", json={"year": 1960, "month": 10, "day": 10, "mode": "care_recreation"}
    )
    assert res.status_code == 200
    data = res.json()
    assert "1960年10月10日" in data["script"]
    assert data["reminiscence_quiz"]


def test_anniversary_mode_flow_works_through_api(client):
    res = client.post(
        "/api/generate",
        json={
            "year": 1980,
            "month": 5,
            "day": 15,
            "mode": "anniversary",
            "target_name": "花子",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert "1980年5月15日" in data["script"]
    assert "花子" in data["script"]


def test_songs_are_fallback_when_itunes_misses(client, mock_itunes):
    """iTunes ヒット0件のとき全曲がフォールバック扱いになる

    旧契約: iTunes が 0 件なら曲リスト自体を空にして補完に任せる。
    新契約: 正本カタログ（``core/songs/songs.json``）から必ず曲を出す。
            「iTunes 検索が 0 件」と「iTunes に音源が無い」は別の事象で、
            前者は**選曲自体**を失敗させない、後者は**間奏**として扱う。
    したがってこのテストは「音源が無い」ことだけを表明し、
    「曲が出ない」ことは表明しない（``tests/test_regression.py`` の
    ``test_search_itunes_songs_keeps_catalog_songs_when_no_hit`` が保証する）。
    """
    mock_itunes.empty()
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()

    assert data["songs"]
    for song in data["songs"]:
        assert song["preview_url"] is None
        assert song["is_fallback"] is True


def test_miss_is_idempotent_for_the_same_input(client, mock_itunes):
    """同じ要求を 2 回投げても同じ結果になる（順序依存・状態漏れが無い）"""
    mock_itunes.empty()
    payload = {"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    first = client.post("/api/generate", json=payload).json()
    second = client.post("/api/generate", json=payload).json()

    assert first["songs"] == second["songs"]
    assert second["script"] == first["script"]


def test_miss_and_hit_are_separate_contracts(client, mock_itunes):
    """「0 件」と「一致する音源あり」は別の契約として両方成り立つ

    年を変えるのは、否定した音源が曲ストア（``services/song_store.py`` の
    ``PreviewCache``）に残るため。**同じ曲_year_で** 0 件 → 一致と変えると
    2 回目も間奏のままになる。これは「音源が無かった」ことを記憶するという
    実装仕様であり、「0 件」と「音源が無い」を混ぜないための仕様でもある。
    """
    from retro_radio.core.songs import pool_for_year

    mock_itunes.empty()
    missed = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()
    assert missed["songs"]
    assert all(song["preview_url"] is None for song in missed["songs"])
    assert all(song["is_fallback"] is True for song in missed["songs"])

    pool = pool_for_year(1985, 6)
    assert pool
    mock_itunes.match_many([(r["title"], r["artist"]) for r in pool])
    hit = client.post(
        "/api/generate", json={"year": 1985, "month": 9, "day": 24, "mode": "normal"}
    ).json()
    with_preview = [s for s in hit["songs"] if s["preview_url"]]
    assert with_preview, hit["songs"]
    assert all(song["is_fallback"] is False for song in with_preview)


def test_songs_use_itunes_results_when_available(client, mock_itunes):
    """**一致する**結果があればプレビューURLつきになる

    旧テストは汎用のダミー結果（曲名・アーティストが正本と無関係）を
    返し、それがそのまま採用される前提だった。現在の経路は
    「曲名もアーティストも一致するもの」だけを採用するため、
    ここでは正本の候補プールに一致を作る。
    """
    from retro_radio.core.songs import pool_for_year

    # セレクタは再生履歴でローテーションするため、どの曲が選ばれるかは
    # 事前には分からない。候補プール全体を一致させて結果に依存させない。
    pool = pool_for_year(1975, 6)
    assert pool, "1975 年の候補プールが空"
    mock_itunes.match_many([(r["title"], r["artist"]) for r in pool])

    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()

    # 1 曲でも一致が解ければプレビュー URL が付く。
    # 選曲と音源解決の候補集合が一致することは保証しないため、
    # 「全部 preview が付く」ではなく「1 曲でも付く」で見る。
    with_preview = [s for s in data["songs"] if s["preview_url"]]
    assert with_preview, data["songs"]
    for song in with_preview:
        assert song["is_fallback"] is False, song
        assert song["title"] != "ヒット曲0", "iTunes の無関係な結果は採用しない"


def test_itunes_results_are_ignored_when_they_do_not_match_the_catalog(client, mock_itunes):
    """**一致しない**iTunes 結果は採用しない（別曲を鳴らさない）

    これが本次変更の中心的な安全要件。旧実装は候補の先頭 1 件を
    無検証で採用しており、実測で「卒業写真（荒井由実）」に対して
    「ルージュの伝言」を再生していた。
    """
    mock_itunes.hit(3, preview=True)  # 曲名・アーティストが正本と無関係
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()

    assert data["songs"]
    for song in data["songs"]:
        assert song["preview_url"] is None, song
        assert song["title"] != "ヒット曲0"


def test_settings_subclass_cannot_override_fields():
    """旧テストの `class DummySettings(Settings): gemini_api_key = ""` は
    Pydantic v2 ではフィールド上書きとして拒否される（この異常を固定する）"""
    from pydantic.errors import PydanticUserError

    with pytest.raises(PydanticUserError):
        type("DummySettings", (Settings,), {"gemini_api_key": ""})
