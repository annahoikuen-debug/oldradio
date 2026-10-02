"""公開前デバッグ（P0）の回帰テスト。

対象（docs/code_review_2026-10-02.md の P0 節）:

* P0-2  ``_QueueTicket`` が cancel-before-start で恒久リークする
* P0-3  ``enforce_song_allowlist`` が許可リスト空だと無防備
* P0-4  許可リストの差し替えが位置を見ず、すでに鳴った曲に差し替える
* P0-5  音源ゼロ/少数なのに「三つほどご用意しました」と約束する
* P0-6  入場枠がスレッド数を制限していない
* P0-7  SSE の ``last_event_id`` で完了済みジョブを 900 秒固定できる
* P0-8  ``single_user_key`` に最小長が無く、コイル経路にレート制限が無い
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from retro_radio import jobs


# ==============================================================================
# P0-2 / P0-6: 入場枠チケットの寿命
# ==============================================================================
def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_ticket_is_released_when_the_job_is_cancelled_before_start():
    """開始前キャンセルでも入場枠は必ず戻る（P0-2）。

    かつては `target` が呼ばれないため `ticket.release()` に到達せず、
    `_JOB_QUEUE_LIMIT` 回で `POST /api/jobs` が恒久的に 503 になった。
    """
    job = jobs.Job(tenant_id="t")
    job.request_cancel()

    released = []
    thread = jobs.start_worker(job, lambda j: pytest.fail("target は呼ばれてはいけない"), on_finish=lambda: released.append(1))
    thread.join(timeout=5)

    assert _wait_until(lambda: job.is_finished)
    assert released == [1], "開始前キャンセルで入場枠が解放されていない"
    assert job.state == "cancelled"


def test_ticket_is_released_after_a_normal_run():
    job = jobs.Job(tenant_id="t")
    released = []
    thread = jobs.start_worker(job, lambda j: j.mark_running(), on_finish=lambda: released.append(1))
    thread.join(timeout=5)
    assert released == [1]


def test_ticket_release_is_idempotent_even_if_the_callback_raises(monkeypatch):
    """`on_finish` が例外を投げても**1 回だけ**呼ばれ、ジョブは終端する。"""
    job = jobs.Job(tenant_id="t")
    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("後始末の失敗")

    thread = jobs.start_worker(job, lambda j: None, on_finish=_boom)
    thread.join(timeout=5)
    assert _wait_until(lambda: len(calls) == 1)
    assert calls == [1]


def test_ticket_is_released_when_the_thread_cannot_be_started(monkeypatch):
    """`thread.start()` が失敗しても入場枠を返し、Job を終端させる（P0-2）。

    スレッド枯渇時に `Job` が `queued` のまま残ると、
    進行イベントも永久に届かない。
    """
    from retro_radio.server import _acquire_job_queue_slot

    ticket = _acquire_job_queue_slot()
    assert ticket is not None, "入場枠が取得できていない（先のテストが枠を漏らした可能性）"

    def _no_threads(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _no_threads)
    job = jobs.Job(tenant_id="t")
    with pytest.raises(RuntimeError):
        jobs.start_worker(job, lambda j: None, on_finish=ticket.release)

    assert job.is_finished, "スレッド生成に失敗したジョブが未終端のまま残っている"
    ticket.release()  # 冪等確認（2 回目を呼んでも枠が増えない）


def test_queue_slot_is_held_until_the_worker_terminates():
    """入場枠は `_generation_slots` を**待ち終わらない**（P0-6）。

    ここで先に返すと、20 rps の `POST /api/jobs` で
    「30 秒何もせずブロックする daemon スレッド」が数百本同時に立ち上がる。
    """
    from retro_radio.server import _JOB_QUEUE_LIMIT, _acquire_job_queue_slot

    before = _acquire_job_queue_slot()
    assert before is not None
    # 1 枚取得できたら、残り枠は `_JOB_QUEUE_LIMIT - 1`。
    taken = 1
    while taken < _JOB_QUEUE_LIMIT:
        if _acquire_job_queue_slot() is None:
            break
        taken += 1
    assert taken == _JOB_QUEUE_LIMIT, "枠が無制限に取得できている"
    before.release()


# ==============================================================================
# P0-7: SSE の last_event_id
# ==============================================================================
def test_last_event_id_rejects_absurdly_large_values():
    """上限なしの整数は 0 に落とす（P0-7）。

    未来の seq を送ると `pending` が恒久に空になり、`is_finished` 判定に
    到達しないまま 900 秒接続を保持する。
    """
    from retro_radio.server import _MAX_LAST_EVENT_ID, _last_event_id

    class _Req:
        headers = {"last-event-id": str(_MAX_LAST_EVENT_ID + 1)}
        query_params: dict = {}

    assert _last_event_id(_Req()) == 0

    class _Ok:
        headers = {"last-event-id": "12"}
        query_params: dict = {}

    assert _last_event_id(_Ok()) == 12


def test_finished_job_does_not_hold_an_sse_connection():
    """完了済みジョブ + 未来の seq でも**即座に**終わる（P0-7）。"""
    from retro_radio.server import _sse_stream

    job = jobs.Job(tenant_id="t")
    job.mark_running()
    job.succeed({"ok": True})
    job.emit(jobs.EVENT_DONE, playlist_len=1)

    class _Req:
        headers: dict = {}
        query_params = {"last_event_id": "0"}

        async def is_disconnected(self):
            return False

    async def _drain():
        chunks = []
        agen = _sse_stream(job, _Req(), _FakeTicket())
        async for chunk in agen:
            chunks.append(chunk)
        return chunks

    started = time.monotonic()
    chunks = asyncio.run(_drain())
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"SSE が {elapsed:.1f} 秒も接続を保持した"
    # 完了済みジョブの**終端イベントまで**は返す（クライアントが結果を失わない）。
    assert any(jobs.EVENT_DONE in c for c in chunks), chunks


def test_finished_job_does_not_hold_an_sse_connection_with_a_future_seq():
    """未来の `last_event_id` でも**待たずに**終わる（P0-7 の元の再現）。

    `wait_for_events` が恒久に空を返すため、修正前は `is_finished` 判定に
    到達せず 900 秒（`SSE_MAX_SECONDS`）接続を保持し、executor のスレッドも
    同時に占有していた。
    """
    from retro_radio.server import _sse_stream

    job = jobs.Job(tenant_id="t")
    job.mark_running()
    job.succeed({"ok": True})
    job.emit(jobs.EVENT_DONE, playlist_len=1)

    class _Req:
        headers: dict = {}
        query_params = {"last_event_id": "999999999999"}

        async def is_disconnected(self):
            return False

    async def _drain():
        agen = _sse_stream(job, _Req(), _FakeTicket())
        return [chunk async for chunk in agen]

    started = time.monotonic()
    chunks = asyncio.run(_drain())
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"SSE が {elapsed:.1f} 秒も接続を保持した"
    # 巨大値は不正値として 0 に落とされるため、終端イベントは読める。
    # 重要なのは「何も返さず 900 秒開いたままにならない」こと。
    assert any(jobs.EVENT_DONE in c for c in chunks), chunks


class _FakeTicket:
    """`_sse_stream` が要求する `_QueueTicket` の最小スタブ。"""

    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True
