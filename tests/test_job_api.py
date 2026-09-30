"""`/api/jobs` の HTTP テスト（提案④・S5）。

## このファイルが固定すること

1. **【最重要】クライアント abort 後にサーバのスロットが確実に 0 に戻ること。**
   `GenerationSlots` の acquire / release を**スパイ**して
   「abort 前 → acquire=1 release=0 / abort 後 → acquire=1 release=1」を
   **sleep 無しで決定的に**検証する。
2. **SSE を実際に購読してイベント列を受信できること。**
3. `POST /api/jobs` の 202 契約、`GET`（ポーリング）、`DELETE`（明示キャンセル）。
4. 認証配線（`/health` の `auth_required` / `auth_ready`、テナント付き音声 URL）。
5. `select_songs` が **1 回だけ**呼ばれること、`songs` と `playlist` が
   **同一の曲リスト**から構成されること。
6. 監査ログの完全率（生成イベント数に対するログ行数 = 100%）。
"""

from __future__ import annotations

import json
import threading

import pytest

import retro_radio.server as server_module
from retro_radio import jobs
from retro_radio.jobs import GenerationSlots

PAYLOAD = {"year": 1975, "month": 9, "day": 24, "mode": "normal"}

SCRIPT = (
    "### オープニング\n"
    "1975年の秋ですConditions。\n"
    "### ヒット曲\n"
    "此时的ヒット曲をお届けします。\n"
    "### エンディング\n"
    "また会いましょう。\n"
)


@pytest.fixture
def clean_registry():
    """テストごとにジョブ台帳を空にする（並列実行でも混ざらないように）。"""
    jobs.registry.clear()
    yield jobs.registry
    jobs.registry.clear()


# ==============================================================================
# 1. 【最重要】クライアント abort 後のスロット解放
# ==============================================================================
def test_client_abort_releases_generation_slot(client, monkeypatch, clean_registry):
    """**クライアントが abort した後は、サーバのスロットが確実に 0 に戻る。**

    検証の仕組み（sleep ベースの実測ではない）:

    * `server._generation_slots` を `GenerationSlots(1)` へ差し替え、
      acquire / release の**成功回数**を数える。
    * ワーカーは `generate_radio_script` の差し替え関数の内で
      `cancellable_wait` により**cancellable な待ち**に入る
      （実際の gTTS 間隔待ちと同じ仕組み）。
    * `entered` イベントで「スロット取得済み」を**同期**して確かめる
      （タイミングを sleep で当てない）。
    * クライアント abort を `DELETE /api/jobs/{id}` で発生させる。
    * `job.worker.join()` で**終了を同期**してから回数を観測する。
    """
    slots = GenerationSlots(1)
    monkeypatch.setattr(server_module, "_generation_slots", slots)
    slots.reset_spies()

    entered = threading.Event()
    release = threading.Event()

    def blocking_script(*args, **kwargs):
        """cancellable な待ちに入る（gTTS の間隔待ちと同じ経路）。"""
        entered.set()
        while not release.is_set():
            # キャンセルされるとここで JobCancelled が飛ぶ
            jobs.cancellable_wait(jobs.current_event(), 0.05)
        return SCRIPT

    monkeypatch.setattr(server_module, "generate_radio_script", blocking_script)

    response = client.post("/api/jobs", json=PAYLOAD)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    job = jobs.registry.get(job_id)
    assert job is not None
    try:
        # --- abort 前: スロットを 1 個掴んでいる --------------------------------
        assert entered.wait(10), "ワーカーが cancellable な待ちに入らなかった"
        before = slots.snapshot()
        assert before["acquire_count"] == 1
        assert before["release_count"] == 0
        assert before["in_use"] == 1
        assert before["free"] == 0

        # --- クライアント abort（= DELETE /api/jobs/{id}）--------------------
        cancel = client.delete(f"/api/jobs/{job_id}")
        assert cancel.status_code == 200
        assert cancel.json()["cancel_requested"] is True

        # --- ワーカーの終了を同期してから観測 ---------------------------------
        job.worker.join(20)
        assert not job.worker.is_alive(), "ワーカーがキャンセルで止まらなかった"

        after = slots.snapshot()
        assert after["acquire_count"] == 1
        assert after["release_count"] == 1, "abort 後に release() が呼ばれていない"
        assert after["in_use"] == 0, "スロットが占有されたまま（第三者が 503 を grocer する状態）"
        assert after["free"] == 1

        assert job.state == jobs.STATE_CANCELLED
        names = [e.name for e in job.events_after(0)]
        assert jobs.EVENT_CANCELLED in names
    finally:
        release.set()


def test_abort_releases_slot_even_when_generation_fails(client, monkeypatch, clean_registry):
    """失敗しても `finally` を通るため、スロットは必ず戻る。"""
    slots = GenerationSlots(1)
    monkeypatch.setattr(server_module, "_generation_slots", slots)
    slots.reset_spies()

    entered = threading.Event()

    def boom(*args, **kwargs):
        entered.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(server_module, "generate_radio_script", boom)

    response = client.post("/api/jobs", json=PAYLOAD)
    job_id = response.json()["job_id"]
    job = jobs.registry.get(job_id)
    assert entered.wait(10)
    job.worker.join(20)

    assert job.state == jobs.STATE_FAILED
    assert slots.snapshot()["in_use"] == 0
    assert slots.snapshot()["release_count"] == 1


def test_next_job_can_take_the_released_slot(client, monkeypatch, clean_registry):
    """abort 後に**次のジョブが同じスロットを使える**こと（503 を出さない）。"""
    slots = GenerationSlots(1)
    monkeypatch.setattr(server_module, "_generation_slots", slots)
    slots.reset_spies()

    entered = threading.Event()
    release = threading.Event()

    def blocking_script(*args, **kwargs):
        entered.set()
        while not release.is_set():
            jobs.cancellable_wait(jobs.current_event(), 0.05)
        return SCRIPT

    monkeypatch.setattr(server_module, "generate_radio_script", blocking_script)
    first_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
    first = jobs.registry.get(first_id)
    assert entered.wait(10)

    client.delete(f"/api/jobs/{first_id}")
    first.worker.join(20)
    assert slots.snapshot()["in_use"] == 0

    # 2 つ目のジョブは塞がらない
    release.set()
    second_response = client.post("/api/jobs", json=PAYLOAD)
    assert second_response.status_code == 202
    second = jobs.registry.get(second_response.json()["job_id"])
    second.worker.join(20)
    assert second.state == jobs.STATE_SUCCEEDED
    assert slots.snapshot()["acquire_count"] == 2
    assert slots.snapshot()["release_count"] == 2
    assert slots.snapshot()["in_use"] == 0


# ==============================================================================
# 2. POST /api/jobs
# ==============================================================================
class TestCreateJob:
    def test_returns_202_with_estimate(self, client, clean_registry):
        response = client.post("/api/jobs", json=PAYLOAD)
        assert response.status_code == 202
        body = response.json()
        assert set(body) >= {"job_id", "estimated_ms", "poll_after_ms"}
        assert isinstance(body["job_id"], str) and body["job_id"]
        assert isinstance(body["estimated_ms"], int) and body["estimated_ms"] > 0
        assert body["poll_after_ms"] == 1500
        assert body["events_url"] == f"/api/jobs/{body['job_id']}/events"

        job = jobs.registry.get(body["job_id"])
        job.worker.join(20)
        assert job.state == jobs.STATE_SUCCEEDED

    def test_estimate_is_computed_from_cache_state(self, client, clean_registry):
        """`estimated_ms` が**そのキャッシュ状態**から計算されていること。"""
        cold = client.post("/api/jobs", json=PAYLOAD).json()
        # 同じ内容で 1 回走らせると TTS キャッシュヒットが蓄積する
        job = jobs.registry.get(cold["job_id"])
        job.worker.join(20)

        hits_before = jobs.registry.cache_stats.hits
        assert hits_before > 0, "TTS キャッシュヒットが観測されていない"
        assert jobs.registry.cache_stats.hit_rate() is not None

        warm = client.post("/api/jobs", json=PAYLOAD).json()
        warm_job = jobs.registry.get(warm["job_id"])
        warm_job.worker.join(20)
        assert warm["estimated_ms"] <= cold["estimated_ms"], (
            "キャッシュが効いたのに推定が増えていたら、キャッシュ状態を見ていない"
        )

    def test_validation_still_applies(self, client, clean_registry):
        assert client.post("/api/jobs", json={"year": 2020, "month": 2, "day": 30}).status_code == 422
        assert client.post("/api/jobs", json={"year": 1800}).status_code == 422

    def test_legacy_generate_still_works(self, client):
        """既存 API は削除しない（README の API 契約）。"""
        response = client.post("/api/generate", json=PAYLOAD)
        assert response.status_code == 200
        assert response.json()["script"]


# ==============================================================================
# 3. GET / DELETE
# ==============================================================================
class TestJobPollingAndCancel:
    def test_get_job_returns_snapshot_with_events(self, client, clean_registry):
        job_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
        jobs.registry.get(job_id).worker.join(20)

        body = client.get(f"/api/jobs/{job_id}").json()
        assert body["job_id"] == job_id
        assert body["state"] == jobs.STATE_SUCCEEDED
        assert body["event_count"] > 0
        names = [e["event"] for e in body["events"]]
        assert names[0] == jobs.EVENT_ESTIMATE
        assert names[-1] == jobs.EVENT_DONE
        # UI はこの label で既存 4 ステップ表示に差し替えられる
        for event in body["events"]:
            if event["label"] is not None:
                assert event["label"] in jobs.UI_STEP_LABELS
        # ポーリングだけで結果が取れる（SSE なしでも完結する）
        assert body["result"]["year"] == 1975
        assert body["estimate"]["total_ms"] == body["estimated_ms"]

    def test_unknown_job_is_404(self, client, clean_registry):
        assert client.get("/api/jobs/nope").status_code == 404
        assert client.delete("/api/jobs/nope").status_code == 404
        assert client.get("/api/jobs/nope/events").status_code == 404

    def test_delete_after_finish_is_reported_as_not_accepted(self, client, clean_registry):
        job_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
        jobs.registry.get(job_id).worker.join(20)
        body = client.delete(f"/api/jobs/{job_id}").json()
        assert body["cancel_requested"] is False
        assert body["state"] == jobs.STATE_SUCCEEDED

    def test_other_tenant_cannot_see_the_job(self, client, clean_registry, monkeypatch):
        """他テナントには 404（存在を漏らさない）。"""
        job_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
        job = jobs.registry.get(job_id)
        job.worker.join(20)

        original = server_module.tenant_principal

        def as_other_tenant(request, current_settings=None):
            from retro_radio.auth.tokens import Principal

            return Principal(tenant_id="facility-b", role="member",
                             auth_mode="disabled", authenticated=False)

        monkeypatch.setattr(server_module, "tenant_principal", as_other_tenant)
        assert client.get(f"/api/jobs/{job_id}").status_code == 404
        assert client.delete(f"/api/jobs/{job_id}").status_code == 404
        monkeypatch.setattr(server_module, "tenant_principal", original)


# ==============================================================================
# 4. SSE（実際に購読する）
# ==============================================================================
def _read_sse(client, job_id, stop_on=("done", "failed", "cancelled"), max_events=60):
    """SSE を実際に購読して `(event_name, data)` の列を返す。"""
    received = []
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert stream.headers["cache-control"] == "no-cache"
        assert stream.headers["x-accel-buffering"] == "no"

        name = None
        for line in stream.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: ") and name is not None:
                received.append((name, json.loads(line[len("data: "):])))
                name = None
                if name in stop_on or received[-1][0] in stop_on:
                    break
                if len(received) >= max_events:
                    break
    return received


class TestServerSentEvents:
    def test_event_sequence_is_observable(self, client, clean_registry):
        """SSE を実際に購読して、期待するイベント列を受信する。"""
        job_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
        job = jobs.registry.get(job_id)
        job.worker.join(20)
        assert job.state == jobs.STATE_SUCCEEDED

        received = _read_sse(client, job_id)
        names = [name for name, _ in received]

        assert names[0] == jobs.EVENT_ESTIMATE
        assert jobs.EVENT_SCRIPT_STARTED in names
        assert jobs.EVENT_SCRIPT_DONE in names
        assert jobs.EVENT_TTS_SEGMENT in names
        assert jobs.EVENT_TTS_DONE in names
        assert jobs.EVENT_MUSIC_STARTED in names
        assert jobs.EVENT_MUSIC_DONE in names
        assert jobs.EVENT_PLAYLIST_DONE in names
        assert names[-1] == jobs.EVENT_DONE, f"終端イベントが最後:\n{names}"

        payload = dict(received)
        assert payload[jobs.EVENT_SCRIPT_DONE]["chars"] > 0
        assert "playlist_len" in payload[jobs.EVENT_DONE]

        segment = payload[jobs.EVENT_TTS_SEGMENT]
        assert set(segment) == {"i", "n", "cached"}
        assert segment["i"] == 0
        assert segment["n"] >= 1
        assert isinstance(segment["cached"], bool)

        assert payload[jobs.EVENT_MUSIC_DONE]["count"] >= 1

    def test_stream_streams_while_running(self, client, monkeypatch, clean_registry):
        """**ジョブ実行中に購読**しても、終端までイベントが流れ続ける。"""
        entered = threading.Event()
        release = threading.Event()

        def slow_script(*args, **kwargs):
            entered.set()
            release.wait(15)
            return SCRIPT

        monkeypatch.setattr(server_module, "generate_radio_script", slow_script)
        job_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
        assert entered.wait(10)

        timer = threading.Timer(0.2, release.set)
        timer.start()
        try:
            received = _read_sse(client, job_id)
        finally:
            timer.cancel()
            release.set()

        names = [name for name, _ in received]
        assert names[0] == jobs.EVENT_ESTIMATE
        assert jobs.EVENT_SCRIPT_STARTED in names
        assert names[-1] == jobs.EVENT_DONE

    def test_stream_ends_after_terminal_event(self, client, clean_registry):
        """**ジョブ完了後のストリームは有限に終わる**（無限に開いたままにしない）。"""
        job_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
        job = jobs.registry.get(job_id)
        job.worker.join(20)

        with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
            body = "".join(stream.iter_text())
        # 2 度目の購読は同じイベント列を返すだけで、再び待ち続けない
        assert body.count(f"event: {jobs.EVENT_DONE}") == 1
        assert "keep-alive" not in body

    def test_cancelled_job_emits_cancelled_event(self, client, monkeypatch, clean_registry):
        entered = threading.Event()
        release = threading.Event()

        def blocking_script(*args, **kwargs):
            entered.set()
            while not release.is_set():
                jobs.cancellable_wait(jobs.current_event(), 0.05)
            return SCRIPT

        monkeypatch.setattr(server_module, "generate_radio_script", blocking_script)
        job_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
        job = jobs.registry.get(job_id)
        assert entered.wait(10)
        client.delete(f"/api/jobs/{job_id}")
        job.worker.join(20)
        release.set()

        received = _read_sse(client, job_id)
        names = [name for name, _ in received]
        assert names[-1] == jobs.EVENT_CANCELLED
        assert dict(received)[jobs.EVENT_CANCELLED]["reason"] == "client_abort"

    def test_last_event_id_resumes(self, client, clean_registry):
        """`Last-Event-ID` で途中から再開できる（欠けない）。"""
        job_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
        job = jobs.registry.get(job_id)
        job.worker.join(20)
        full = _read_sse(client, job_id)
        first_id = full[0][0]

        with client.stream(
            "GET", f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": "1"}
        ) as stream:
            tail = [
                line[len("event: "):]
                for line in stream.iter_lines()
                if line.startswith("event: ")
            ]
        assert first_id not in tail
        assert jobs.EVENT_DONE in tail


# ==============================================================================
# 5. 認証配線
# ==============================================================================
class TestAuthWiring:
    def test_health_reports_auth_state(self, client):
        body = client.get("/health").json()
        assert "auth_required" in body
        assert "auth_ready" in body
        assert isinstance(body["auth_required"], bool)
        assert isinstance(body["auth_ready"], bool)

    def test_require_auth_default_is_safe(self):
        """`RETRO_RADIO_REQUIRE_AUTH` の既定は 1（認証必須）。"""
        from retro_radio.config import Settings

        assert Settings.model_fields["require_auth"].default is True

    def test_audio_url_contains_tenant_when_auth_is_enforced(self, monkeypatch, mock_gtts):
        """認証有効時は `relative_url_for` 相当のテナント付き URL になる。"""
        monkeypatch.setenv("RETRO_RADIO_REQUIRE_AUTH", "1")
        assert server_module._auth_enforced() is True
        filename = server_module.generate_tts_cached("テナント URL テスト", tenant_id="facility-a")
        url = server_module._audio_url_for("facility-a", filename)
        assert url == f"/api/audio/facility-a/{filename}"
        # テナントごとに別のディレクトリに書かれる（交差 0）
        tenant_a = server_module._tenant_cache_dir("facility-a")
        tenant_b = server_module._tenant_cache_dir("facility-b")
        assert tenant_a != tenant_b
        assert (tenant_a / filename).is_file()

    def test_audio_url_is_flat_in_personal_mode(self, monkeypatch):
        monkeypatch.setenv("RETRO_RADIO_REQUIRE_AUTH", "0")
        assert server_module._auth_enforced() is False
        assert server_module._audio_url_for("facility-a", "tts_x.mp3") == "/api/audio/tts_x.mp3"
        assert server_module._tenant_cache_dir("facility-a") == server_module.CACHE_DIR

    def test_tenant_audio_route_serves_tenant_file(self, client, monkeypatch, mock_gtts):
        """`/api/audio/{tenant}/{file}` は 3 層防御を通したうえで配信する。"""
        monkeypatch.setenv("RETRO_RADIO_REQUIRE_AUTH", "0")  # 個人モード = default テナント
        filename = server_module.generate_tts_cached("テナント配信テスト")
        ok = client.get(f"/api/audio/default/{filename}")
        assert ok.status_code == 200
        assert ok.headers["content-type"] == "audio/mpeg"
        assert ok.headers["x-content-type-options"] == "nosniff"
        # 3 層防御はテナント付きルートにも残っている
        assert client.get("/api/audio/default/notanmp3.txt").status_code == 404
        assert client.get(f"/api/audio/default/{filename}?x=1").status_code == 200

    def test_me_router_is_mounted(self):
        paths = {getattr(route, "path", None) for route in server_module.app.routes}
        assert "/api/me" in paths
        assert "/api/admin/audit" in paths

    def test_v1_router_is_still_not_mounted(self):
        """Pro プラン API のスタブは未 include のまま（既存契約を壊さない）。"""
        paths = {getattr(route, "path", None) for route in server_module.app.routes}
        assert "/api/v1" not in paths


# ==============================================================================
# 6. select_songs の 1 回畳みと songs/playlist の一致
# ==============================================================================
class TestSingleSelection:
    def test_select_songs_is_called_exactly_once(self, client, monkeypatch):
        calls = []
        original = server_module._select_program_songs

        def counting(year, count):
            calls.append((year, count))
            return original(year, count)

        monkeypatch.setattr(server_module, "_select_program_songs", counting)
        response = client.post("/api/generate", json=PAYLOAD)
        assert response.status_code == 200
        assert len(calls) == 1, f"選曲が {len(calls)} 回呼ばれている（1 回であるべき）"

    def test_songs_and_playlist_come_from_the_same_list(self, client):
        """互換フィールド `songs` と `playlist` は**同一の曲リスト**からできる。"""
        data = client.post("/api/generate", json=PAYLOAD).json()
        playlist_songs = [item for item in data["playlist"] if item["type"] == "song"]
        assert playlist_songs, "プレイリストに曲が入っていない"
        # songs は passes[0] の先頭 `medley_song_count` 件と一致する
        for compat, played in zip(data["songs"], playlist_songs):
            assert compat["title"] == played["title"]
            assert compat["artist"] == played["artist"]

    def test_passes_do_not_repeat_songs(self, client):
        data = client.post("/api/generate", json=PAYLOAD).json()
        assert len(data["passes"]) == data["loop_count"]
        first = {
            (item["title"], item["artist"])
            for item in data["passes"][0]
            if item["type"] == "song"
        }
        second = {
            (item["title"], item["artist"])
            for item in data["passes"][1]
            if item["type"] == "song"
        }
        assert not (first & second), "同じパスで曲が重複している"


# ==============================================================================
# 7. 監査ログの完全率
# ==============================================================================
class TestAuditCoverage:
    def test_every_generation_event_is_logged(self, client, clean_registry):
        """**生成イベント数に対する監査ログ行数 = 100%**（開始と終端で必ず 2 行）。"""
        from retro_radio.api.audit import ACTION_GENERATION, audit_coverage
        from retro_radio.db.privacy_repository import AuditRepository
        from retro_radio.db.session import get_db

        with get_db() as db:
            repo = AuditRepository(db)
            before = repo.count(tenant_id="default", action=ACTION_GENERATION)

        job_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
        jobs.registry.get(job_id).worker.join(20)

        with get_db() as db:
            repo = AuditRepository(db)
            after = repo.count(tenant_id="default", action=ACTION_GENERATION)
            entries = repo.list(tenant_id="default", action=ACTION_GENERATION, limit=50)

        assert after - before == 2, "開始 1 行 + 終端 1 行が記録されていない"
        phases = [e["meta"].get("phase") for e in entries if e["resource_id"] == job_id]
        assert phases == ["started", "completed"]
        assert audit_coverage(after - before, 1) >= 1.0

    def test_audit_failure_does_not_break_response(self, client, monkeypatch, clean_registry):
        """監査 DB が落ちていても**応答は壊さない**。"""

        def broken(*args, **kwargs):
            raise RuntimeError("db down")

        import retro_radio.api.audit as audit_module
        import retro_radio.db.privacy_repository as privacy_module

        monkeypatch.setattr(privacy_module.AuditRepository, "record", broken)
        response = client.post("/api/generate", json=PAYLOAD)
        assert response.status_code == 200
        assert audit_module.ACTION_GENERATION == "generation"

    def test_cancelled_job_is_logged(self, client, monkeypatch, clean_registry):
        entered = threading.Event()
        release = threading.Event()

        def blocking_script(*args, **kwargs):
            entered.set()
            while not release.is_set():
                jobs.cancellable_wait(jobs.current_event(), 0.05)
            return SCRIPT

        monkeypatch.setattr(server_module, "generate_radio_script", blocking_script)
        job_id = client.post("/api/jobs", json=PAYLOAD).json()["job_id"]
        job = jobs.registry.get(job_id)
        assert entered.wait(10)
        client.delete(f"/api/jobs/{job_id}")
        job.worker.join(20)
        release.set()

        from retro_radio.api.audit import ACTION_GENERATION
        from retro_radio.db.privacy_repository import AuditRepository
        from retro_radio.db.session import get_db

        with get_db() as db:
            entries = AuditRepository(db).list(
                tenant_id="default", action=ACTION_GENERATION, limit=50
            )
        phases = [e["meta"].get("phase") for e in entries if e["resource_id"] == job_id]
        assert "cancelled" in phases
