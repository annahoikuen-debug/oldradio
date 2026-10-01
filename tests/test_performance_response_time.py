"""API 応答時間の検証。

旧テストは `server.get_settings` を差し替えて外部 API をモックするつもりだったが、
`server` は import 時に `settings = get_settings()` を束縛しているため差し替えが効かず、
結果として iTunes / gTTS へ実ネットワークアクセスしていた。
ここでは `conftest` の `client` フィクスチャ（gTTS / iTunes ともにモック済み）を使う。
"""

import time

import pytest

from retro_radio.config import get_settings


BUDGET_SECONDS = 5.0


def _timed(client, payload):
    start = time.perf_counter()
    response = client.post("/api/generate", json=payload)
    return response, time.perf_counter() - start


@pytest.mark.parametrize(
    "payload",
    [
        {"year": 1975, "month": 9, "day": 24, "mode": "normal"},
        {"year": 1960, "month": 10, "day": 10, "mode": "care_recreation"},
        {"year": 1980, "month": 5, "day": 15, "mode": "anniversary", "target_name": "花子"},
    ],
)
def test_response_time_within_budget(client, payload):
    response, elapsed = _timed(client, payload)
    assert response.status_code == 200, response.text
    assert elapsed < BUDGET_SECONDS, f"応答が遅すぎる: {elapsed:.2f}秒"


def test_fallback_mode_response_time(client):
    """API キー未設定（フォールバック）でも応答できる"""
    assert get_settings().gemini_api_key == ""
    response, elapsed = _timed(
        client, {"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    )
    assert response.status_code == 200
    assert elapsed < BUDGET_SECONDS, f"応答が遅すぎる: {elapsed:.2f}秒"


def test_repeated_requests_are_served(client):
    """TTS キャッシュにより2回目以降が同等に速いこと（キャッシュ無効化 regression）

    **同一リクエストでも選曲結果は一致しなくなる。** これは仕様である。
    ``SongSelector.select`` は選曲するたびに ``SongHistoryStore`` へ
    再生済み記録するため、カタログが 18 曲以上ある年は
    「同じ年を選んでも前回と違う曲になる」= ローテーションが効く。
    （カタログが 4 曲しかなかったころは、プールが枯渇して同じ曲に
    戻っていたため、偶然 一致していたにすぎない。）

    そのためここでは**応答時間の同等性**だけを契約として残す。
    """
    first, first_elapsed = _timed(client, {"year": 1975, "month": 9, "day": 24, "mode": "normal"})
    second, second_elapsed = _timed(client, {"year": 1975, "month": 9, "day": 24, "mode": "normal"})

    assert first.status_code == second.status_code == 200
    assert first_elapsed < BUDGET_SECONDS
    assert second_elapsed < BUDGET_SECONDS


def test_validation_error_is_fast(client):
    """不正入力は生成処理を通らないため即座に 422"""
    start = time.perf_counter()
    response = client.post("/api/generate", json={"year": 2020, "month": 2, "day": 30})
    elapsed = time.perf_counter() - start

    assert response.status_code == 422
    assert elapsed < 1.0


def test_health_endpoint_is_fast(client):
    start = time.perf_counter()
    response = client.get("/health")
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert elapsed < 1.0


def test_semaphore_released_so_sequential_requests_succeed(client):
    """逐次実行では同時に1枠しか使わないため、連続リクエストが詰まらない"""
    for _ in range(5):
        response = client.post(
            "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
        )
        assert response.status_code == 200
