"""FastAPI サーバのエンドポイント検証。

`client` フィクスチャが gTTS と iTunes をモックし、Gemini は API キー未設定の
フォールバック経路を通るため、このファイルはネットワークに一切依存しない。
"""

import pytest

from retro_radio.config import get_settings


def test_health_check(client):
    """/health は 200 を返し、キー未設定でも起動状態を返す"""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] in ("healthy", "degraded")
    assert "Retro Radio" in data["service"]


def test_serve_index_html(client):
    """index.html が配信される"""
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_get_decades(client):
    """年代一覧 API"""
    res = client.get("/api/decades")
    assert res.status_code == 200
    data = res.json()

    settings = get_settings()
    assert 1970 in data["decades"]
    assert data["default_year"] == settings.default_year
    assert data["decades"] == sorted(set(data["decades"]))
    assert settings.min_year in data["decades"]
    assert settings.max_year in data["decades"]


def test_generate_normal_mode(client):
    """通常モード: 応答に必要な主要フィールドが揃う"""
    res = client.post(
        "/api/generate",
        json={"year": 1975, "month": 9, "day": 24, "mode": "normal"},
    )
    assert res.status_code == 200
    data = res.json()

    assert data["year"] == 1975
    assert data["mode"] == "normal"
    assert len(data["script"]) > 50
    assert data["song"] is not None
    assert "title" in data["song"] and "artist" in data["song"]
    assert data["audio_url"] is not None
    assert data["audio_url"].startswith("/api/audio/tts_")
    assert data["songs"]


def test_generate_care_recreation_mode(client):
    """介護回想法モード: クイズが付く"""
    res = client.post(
        "/api/generate",
        json={"year": 1960, "month": 10, "day": 10, "mode": "care_recreation"},
    )
    assert res.status_code == 200
    data = res.json()

    assert data["mode"] == "care_recreation"
    assert data["reminiscence_quiz"]
    first = data["reminiscence_quiz"][0]
    assert {"question", "answer", "hint"} <= set(first)


def test_generate_anniversary_mode(client):
    """記念日モード: 対象者名が原稿に含まれる"""
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

    assert data["mode"] == "anniversary"
    assert data["target_name"] == "花子"
    assert "花子" in data["script"]


def test_generate_normal_mode_has_no_quiz(client):
    """通常モードではクイズを返さない"""
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()
    assert data["reminiscence_quiz"] is None


def test_generate_uses_fallback_script_without_api_key(client):
    """Gemini API キー未設定でもフォールバック原稿で 200 を返す"""
    from retro_radio.config import Settings

    assert Settings().gemini_api_key == ""
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()
    assert "1975年9月24日" in data["script"]


def test_generate_program_guide_in_response(client):
    """応答に program_guide が含まれる"""
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()

    guide = data["program_guide"]
    assert guide["date"] == "1975-09-24"
    assert guide["weekday"] in ["日", "月", "火", "水", "木", "金", "土"]
    assert guide["schedules"]


def test_generate_playlist_alternates_talk_and_song(client):
    """プレイリストはトークと曲を交互に並べる"""
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()

    playlist = data["playlist"]
    assert playlist
    types = [item["type"] for item in playlist]
    assert types[0] == "song", "最初の要素はオープニング曲でなければならない"
    assert types[-1] == "song", "最後の要素はエンディング曲でなければならない"
    assert "song" in types
    # トークには必ず content がある（無音トークを作らない）
    for item in playlist:
        if item["type"] == "talk":
            assert item["content"]


def test_generate_talk_items_carry_segment_index(client):
    """トーク要素が原稿セグメントの番号を持つ（フロント側_cursor sheet の紐付け用）"""
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()

    indexes = [
        item["metadata"]["segment_index"]
        for item in data["playlist"]
        if item["type"] == "talk"
    ]
    assert indexes == list(range(len(indexes))), indexes


def test_generate_exposes_loop_count(client):
    """応答が推奨ループ回数を持つ（クライアントが既定とする周回数）"""
    settings = get_settings()
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()

    assert data["loop_count"] == settings.program_loop_count


def test_generate_songs_are_filled_to_medley_count(client):
    """iTunes ヒット0件でも FALLBACK_SONGS で指定数まで補完される"""
    settings = get_settings()
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()
    assert len(data["songs"]) == settings.medley_song_count
    for song in data["songs"]:
        if song["preview_url"] is None:
            assert song["is_fallback"] is True


def test_audio_streaming(client):
    """生成済み音声を GET で取得できる"""
    data = client.post(
        "/api/generate", json={"year": 1970, "month": 1, "day": 1, "mode": "normal"}
    ).json()
    audio_url = data["audio_url"]
    assert audio_url

    audio_res = client.get(audio_url)
    assert audio_res.status_code == 200
    assert audio_res.headers["content-type"] == "audio/mpeg"
    assert len(audio_res.content) > 0


def test_audio_missing_returns_404(client):
    """存在しない音声ファイルは 404"""
    assert client.get("/api/audio/tts_deadbeef.mp3").status_code == 404


def test_audio_route_blocks_path_traversal(client):
    """`/api/audio/../../.env` でも CACHE_DIR 外へ出られない"""
    res = client.get("/api/audio/../../.env")
    assert res.status_code in (400, 403, 404)
    assert "RETRO_RADIO" not in res.text


@pytest.mark.parametrize(
    "payload",
    [
        {"year": 2020, "month": 2, "day": 30},   # 2月30日
        {"year": 2021, "month": 2, "day": 29},   # 平年の2月29日
        {"year": 2020, "month": 13, "day": 1},   # 13月
        {"year": 2020, "month": 0, "day": 1},    # 0月
        {"year": 2020, "month": 1, "day": 0},    # 0日
        {"year": 2020, "month": 1, "day": 32},   # 32日
        {"year": 1800, "month": 1, "day": 1},    # 範囲外
        {"year": 2020, "month": 1, "day": 1, "mode": "bogus"},  # 不正モード
        {"month": 1, "day": 1},                  # year 欠落
    ],
)
def test_generate_rejects_invalid_payloads(client, payload):
    """実日付・範囲・モードのバリデーションは 422"""
    res = client.post("/api/generate", json=payload)
    assert res.status_code == 422, res.text


@pytest.mark.parametrize("payload", [
    {"year": 2020, "month": 2, "day": 29},   # 閏年
    {"year": 1975, "month": 9, "day": 24},
    {"year": 1975, "month": 9, "day": 24, "mode": "care_recreation"},
    {"year": 1975, "month": 9, "day": 24, "mode": "anniversary", "target_name": "太郎"},
])
def test_generate_accepts_valid_payloads(client, payload):
    settings = get_settings()
    assert settings.min_year <= payload["year"] <= settings.max_year
    res = client.post("/api/generate", json=payload)
    assert res.status_code == 200, res.text
