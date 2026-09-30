"""非同期ジョブと協調的キャンセルの単体テスト（提案④・S5）。

## このファイルが固定すること

1. **`GenerationSlots` の acquire / release スパイ** … 「クライアント abort 後に
   スロットが確実に 0 に戻る」ことを sleep 無しで検証するための観測点。
2. **`time.sleep` の差し替え** … Gemini の tenacity 指数バックオフ境界が
   キャンセル可能であること（実 tenacity を使う）。
3. **`JobRegistry` のスレッド安全性** … HTTP スレッドとワーカースレッドの競合。
4. **`estimated_ms` の計算** … 実測サンプルが無いときの既定値が
   **設定値から導出**されていること（＝ハードコードした p50/p95 を持ち回さないこと）。
5. **SSE イベントの直列化** … `id:` / `event:` / `data:` の 3 行と UI ラベルの写像。

HTTP 経由の検証（実際に SSE を購読するテスト）は `tests/test_job_api.py` にある。
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from tenacity import retry, stop_after_attempt, wait_exponential

from retro_radio import jobs
from retro_radio.jobs import (
    EVENT_TTS_SEGMENT,
    Job,
    JobCancelled,
    JobRegistry,
    GenerationSlots,
    estimate_generation_ms,
    install_cancellable_sleep,
    sleep_is_cancellable,
    sse_comment,
)


# ==============================================================================
# 1. GenerationSlots（スロット解放の観測点）
# ==============================================================================
class TestGenerationSlotsSpy:
    def test_counts_acquire_and_release(self):
        slots = GenerationSlots(2)
        assert slots.acquire(timeout=1) is True
        assert slots.acquire(timeout=1) is True
        snapshot = slots.snapshot()
        assert snapshot["acquire_count"] == 2
        assert snapshot["release_count"] == 0
        assert snapshot["in_use"] == 2
        assert snapshot["free"] == 0

        slots.release()
        slots.release()
        snapshot = slots.snapshot()
        assert snapshot["acquire_count"] == 2
        assert snapshot["release_count"] == 2
        assert snapshot["in_use"] == 0
        assert snapshot["free"] == 2

    def test_failed_acquire_is_not_counted(self):
        """取得できなかった acquire は回数に数えない（実測の精度のため）。"""
        slots = GenerationSlots(1)
        assert slots.acquire(timeout=1) is True
        assert slots.acquire(timeout=0.05) is False
        assert slots.acquire_count == 1
        assert slots.in_use == 1

    def test_is_a_bounded_semaphore(self):
        """既存テストが `._value` を読むため、BoundedSemaphore のままであること。"""
        import threading as _threading

        assert isinstance(GenerationSlots(1), _threading.BoundedSemaphore)
        slots = GenerationSlots(3)
        assert slots._value == 3

    def test_reset_spies(self):
        slots = GenerationSlots(1)
        slots.acquire(timeout=1)
        slots.release()
        slots.reset_spies()
        assert slots.snapshot() == {
            "acquire_count": 0,
            "release_count": 0,
            "in_use": 0,
            "free": 1,
        }


# ==============================================================================
# 2. 協調的キャンセル（cancellable な待ち）
# ==============================================================================
class TestCancellableSleep:
    def test_installed_once_and_idempotent(self):
        assert sleep_is_cancellable(), "time.sleep が cancellable 版へ差し替わっていない"
        assert install_cancellable_sleep() is False, "2 回目以降は差し替えない（冪等）"

    def test_plain_sleep_still_works_for_non_job_threads(self):
        """ジョブスレッド以外は**普通の sleep**として振る舞う（挙動の保存）。"""
        assert jobs.current_event() is None
        started = time.monotonic()
        jobs.cancellable_sleep(0.01)
        assert time.monotonic() - started >= 0.005

    def test_cancellable_sleep_raises_when_event_is_set(self):
        event = threading.Event()
        event.set()
        previous = jobs.bind_event(event)
        try:
            with pytest.raises(JobCancelled):
                jobs.cancellable_sleep(5)
        finally:
            jobs.bind_event(previous)

    def test_cancellable_sleep_returns_after_timeout(self):
        event = threading.Event()
        previous = jobs.bind_event(event)
        try:
            started = time.monotonic()
            jobs.cancellable_sleep(0.02)
            assert 0.01 <= time.monotonic() - started < 2.0
        finally:
            jobs.bind_event(previous)

    def test_cancellable_wait_returns_false_on_cancel(self):
        event = threading.Event()
        event.set()
        assert jobs.cancellable_wait(event, 0.01) is False

    def test_raise_if_cancelled_is_silent_without_event(self):
        jobs.unbind_event()
        jobs.raise_if_cancelled("no-event")  # 例外を送らない


class TestTenacityRetryBoundary:
    """**Gemini の指数バックオフ境界**がキャンセルできること（実 tenacity を使う）。"""

    def test_retry_backoff_is_interrupted_by_cancel(self):
        attempts = {"n": 0}
        entered = threading.Event()

        @retry(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=1),
            reraise=True,
        )
        def always_fails():
            attempts["n"] += 1
            entered.set()
            raise RuntimeError("boom")

        event = threading.Event()

        def worker():
            previous = jobs.bind_event(event)
            try:
                always_fails()
            except JobCancelled:
                attempts["cancelled"] = True
            finally:
                jobs.bind_event(previous)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert entered.wait(5), "1 回目の呼び出しに到達しなかった"
        # バックオフ待ち（1 秒）の最中にキャンセルする
        event.set()
        thread.join(10)

        assert not thread.is_alive(), "キャンセルがバックオフを止められなかった"
        # 5 回リトライされる前に打ち切られている
        assert attempts["n"] < 5, f"キャンセル後もリトライが続いた: {attempts}"
        assert attempts.get("cancelled") is True


# ==============================================================================
# 3. Job / JobRegistry
# ==============================================================================
class TestJobLifecycle:
    def test_terminal_states_stop_further_transitions(self):
        job = Job("facility-a")
        assert job.state == jobs.STATE_QUEUED
        job.mark_running()
        assert job.state == jobs.STATE_RUNNING
        job.succeed({"ok": True})
        assert job.state == jobs.STATE_SUCCEEDED
        assert job.is_finished
        # 終端後に fail しても状態は変わらない
        job.fail("late", True)
        assert job.state == jobs.STATE_SUCCEEDED

    def test_request_cancel_after_finish_returns_false(self):
        job = Job("facility-a")
        job.succeed({})
        assert job.request_cancel() is False

    def test_events_are_sequenced(self):
        job = Job("facility-a")
        job.emit("a", x=1)
        job.emit("b", y=2)
        assert [e.seq for e in job.events_after(0)] == [1, 2]
        assert [e.name for e in job.events_after(1)] == ["b"]

    def test_wait_for_events_returns_pending_immediately(self):
        job = Job("facility-a")
        job.emit("ready")
        assert [e.name for e in job.wait_for_events(0, 0.01)] == ["ready"]

    def test_wait_for_events_times_out(self):
        job = Job("facility-a")
        started = time.monotonic()
        assert job.wait_for_events(0, 0.05) == []
        assert time.monotonic() - started < 2.0

    def test_cancel_never_calls_succeed(self):
        job = Job("facility-a")
        assert job.request_cancel() is True
        assert job.is_cancel_requested is True
        with pytest.raises(JobCancelled):
            job.checkpoint("some-step")


class TestJobRegistry:
    def test_create_get_and_cancel(self):
        registry = JobRegistry()
        job = registry.create("facility-a", request_payload={"year": 1975})
        assert registry.get(job.job_id) is job
        assert registry.cancel(job.job_id) is True
        assert registry.cancel("unknown") is False

    def test_list_for_tenant(self):
        registry = JobRegistry()
        a = registry.create("facility-a")
        registry.create("facility-b")
        assert [j.job_id for j in registry.list_for_tenant("facility-a")] == [a.job_id]

    def test_is_thread_safe_under_concurrent_access(self):
        """HTTP スレッドとワーカースレッドが同時に触っても壊れない。"""
        registry = JobRegistry(max_jobs=4096)
        created = []
        errors = []

        def worker(index):
            try:
                job = registry.create(f"tenant-{index % 4}")
                job.emit("step", index=index)
                for _ in range(20):
                    registry.get(job.job_id)
                    registry.snapshot_stats()
                created.append(job.job_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)

        assert errors == []
        assert len(created) == 8
        assert len(set(created)) == 8
        assert len(registry) == 8

    def test_finished_jobs_are_evicted_by_ttl(self):
        registry = JobRegistry(max_jobs=10, finished_ttl_seconds=0)
        job = registry.create("facility-a")
        job.succeed({})
        time.sleep(0.01)
        registry.create("facility-b")  # 登録のタイミングで掃除が走る
        assert registry.get(job.job_id) is None

    def test_running_jobs_are_never_evicted_by_ttl(self):
        registry = JobRegistry(max_jobs=10, finished_ttl_seconds=0)
        job = registry.create("facility-a")
        time.sleep(0.01)
        registry.create("facility-b")
        assert registry.get(job.job_id) is job


class TestStartWorker:
    def test_worker_emits_terminal_failed_event(self):
        registry = JobRegistry()

        def boom(job):
            raise ValueError("bad request")

        job = registry.create("facility-a")
        thread = jobs.start_worker(job, boom)
        thread.join(10)
        assert job.state == jobs.STATE_FAILED
        names = [e.name for e in job.events_after(0)]
        assert jobs.EVENT_JOB_STARTED in names
        assert jobs.EVENT_FAILED in names
        last = job.events_after(0)[-1]
        assert last.data["retryable"] is False, "ValueError は再試行しても直らない"

    def test_worker_marks_cancelled_before_start(self):
        registry = JobRegistry()
        job = registry.create("facility-a")
        job.request_cancel()
        called = []
        thread = jobs.start_worker(job, lambda j: called.append(1))
        thread.join(10)
        assert called == []
        assert job.state == jobs.STATE_CANCELLED


# ==============================================================================
# 4. estimated_ms（定数持ち回りをしていないことの検証）
# ==============================================================================
class TestEstimate:
    def _settings(self, tmp_path):
        from retro_radio.config import Settings

        return Settings()

    def test_defaults_come_from_settings_when_no_samples(self, tmp_path):
        """**実測が無いときの既定は設定値から導出**されている。"""
        from retro_radio.config import Settings

        settings = Settings()
        stats = jobs.LatencyStats()
        cache = jobs.CacheStats()
        breakdown = estimate_generation_ms(settings, stats, cache)

        per_tts_expected = (
            float(settings.tts_min_interval_seconds) * 1000.0
            + jobs.default_tts_call_ms(settings)
        )
        assert breakdown.per_tts_ms == pytest.approx(per_tts_expected)
        assert breakdown.script_ms == pytest.approx(jobs.default_script_call_ms(settings))
        assert breakdown.music_ms == pytest.approx(jobs.default_music_call_ms(settings))
        # 観測が無いのでヒット率は None（＝全ミス見立て）
        assert breakdown.tts_hit_rate is None
        assert breakdown.tts_misses == breakdown.tts_calls

    def test_observed_samples_take_priority(self, tmp_path):
        """実測サンプルが 1 件でもあれば**それが必ず優先**される。"""
        from retro_radio.config import Settings

        settings = Settings()
        stats = jobs.LatencyStats()
        stats.record("tts", 111.0)
        stats.record("script", 2222.0)
        stats.record("music", 333.0)
        cache = jobs.CacheStats()
        breakdown = estimate_generation_ms(settings, stats, cache)
        assert breakdown.per_tts_ms == pytest.approx(
            float(settings.tts_min_interval_seconds) * 1000.0 + 111.0
        )
        assert breakdown.script_ms == pytest.approx(2222.0)
        assert breakdown.music_ms == pytest.approx(333.0)

    def test_full_cache_hit_reduces_estimate(self, tmp_path):
        """キャッシュが効いていると推定が下がる（＝キャッシュ状態から計算している）。"""
        from retro_radio.config import Settings

        settings = Settings()
        stats = jobs.LatencyStats()
        cold = jobs.CacheStats()
        warm = jobs.CacheStats()
        for _ in range(20):
            warm.record_hit()
        cold_total = estimate_generation_ms(settings, stats, cold).total_ms
        warm_total = estimate_generation_ms(settings, stats, warm).total_ms
        assert warm_total < cold_total

    def test_default_music_comes_from_itunes_timeouts(self):
        from retro_radio.config import Settings

        settings = Settings()
        assert jobs.default_music_call_ms(settings) == pytest.approx(
            (settings.itunes_timeout_connect + settings.itunes_timeout_read) * 1000.0
        )

    def test_default_script_comes_from_retry_settings(self):
        from retro_radio.config import Settings

        settings = Settings()
        assert jobs.default_script_call_ms(settings) == pytest.approx(
            (settings.max_retries * settings.retry_wait_max + 2) * 1000.0
        )

    def test_segment_hint_changes_call_count(self, tmp_path):
        from retro_radio.config import Settings

        settings = Settings()
        stats = jobs.LatencyStats()
        cache = jobs.CacheStats()
        small = estimate_generation_ms(settings, stats, cache, segment_hint=1)
        large = estimate_generation_ms(settings, stats, cache, segment_hint=20)
        assert large.tts_calls > small.tts_calls
        assert large.total_ms > small.total_ms

    def test_new_job_records_the_estimate(self, tmp_path):
        from retro_radio.config import Settings

        registry = JobRegistry()
        settings = Settings()
        job = jobs.new_job(registry, "facility-a", {"year": 1975}, settings)
        assert job.estimate is not None
        assert job.estimated_ms == job.estimate.total_ms
        assert job.poll_after_ms == 1500


class TestLatencyStats:
    def test_percentile_is_none_without_samples(self):
        stats = jobs.LatencyStats()
        assert stats.p50("tts") is None
        assert stats.p95("script") is None

    def test_percentile_interpolates(self):
        stats = jobs.LatencyStats()
        for value in (10.0, 20.0, 30.0, 40.0, 50.0):
            stats.record("tts", value)
        assert stats.p50("tts") == pytest.approx(30.0)
        assert stats.p95("tts") == pytest.approx(48.0)

    def test_window_limits_samples(self):
        stats = jobs.LatencyStats(window=3)
        for value in (1.0, 2.0, 3.0, 4.0, 5.0):
            stats.record("tts", value)
        assert stats.count("tts") == 3
        assert stats.samples("tts") == [3.0, 4.0, 5.0]


class TestCacheStats:
    def test_hit_rate_is_none_before_observation(self):
        cache = jobs.CacheStats()
        assert cache.hit_rate() is None

    def test_hit_rate(self):
        cache = jobs.CacheStats()
        cache.record_hit()
        cache.record_miss()
        cache.record_hit()
        assert cache.hit_rate() == pytest.approx(2 / 3)


# ==============================================================================
# 5. SSE イベントの直列化と UI ラベル写像
# ==============================================================================
class TestSseSerialization:
    def test_event_has_id_event_and_data_lines(self):
        job = Job("facility-a")
        event = job.emit(EVENT_TTS_SEGMENT, i=0, n=5, cached=False)
        text = event.to_sse()
        lines = text.split("\n")
        assert lines[0] == f"id: {event.seq}"
        assert lines[1] == f"event: {EVENT_TTS_SEGMENT}"
        assert lines[2].startswith("data: ")
        assert text.endswith("\n\n")
        payload = json.loads(lines[2][len("data: "):])
        assert payload == {"i": 0, "n": 5, "cached": False}

    def test_keep_alive_comment(self):
        assert sse_comment() == ": keep-alive\n\n"
        assert sse_comment("bye") == ": bye\n\n"

    def test_every_progress_event_maps_to_one_of_four_ui_labels(self):
        """UI の 4 ラベルへ 1 対 1 で写像できること。"""
        assert jobs.UI_STEP_LABELS == (
            "電波を受信中",
            "原稿を書く",
            "読み上げる",
            "ヒット曲を送る",
        )
        for name in (
            jobs.EVENT_JOB_STARTED,
            jobs.EVENT_SCRIPT_STARTED,
            jobs.EVENT_SCRIPT_DONE,
            jobs.EVENT_TTS_SEGMENT,
            jobs.EVENT_TTS_DONE,
            jobs.EVENT_MUSIC_STARTED,
            jobs.EVENT_MUSIC_DONE,
            jobs.EVENT_PLAYLIST_DONE,
        ):
            assert jobs.UI_LABEL_BY_EVENT[name] in jobs.UI_STEP_LABELS

    def test_terminal_events_have_no_ui_label(self):
        for name in (jobs.EVENT_DONE, jobs.EVENT_FAILED, jobs.EVENT_CANCELLED):
            assert jobs.UI_LABEL_BY_EVENT.get(name) is None
            assert name in jobs.TERMINAL_STATES
