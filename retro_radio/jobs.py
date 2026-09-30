"""非同期ジョブと協調的キャンセル（提案④・S5）。

## 何を解くのか

`POST /api/generate` は同期 `def` のため FastAPI のスレッドプールで走る。
クライアントは 180 秒で abort するが、**サーバ側の gTTS / Gemini / iTunes 呼び出しに
協調的キャンセルが無い**ため、

* クライアントが 408 を見た後もスレッドはバックグラウンドで走り続け、
* `_generation_slots`（既定 2）を占有し続ける。

結果として 3 台使うと 3 人目が 503「混雑しています」をもらう。本モジュールは
「**ジョブ側の明示的な cancel**」という正しい解を提供する。

## 中核の 3 点

1. [`GenerationSlots`] … `_generation_slots` のラッパ。`acquire` / `release` の
   **成功回数を数える**ので、テストが「クライアント abort 後にスロットが 0 に戻る」
   を sleep 無しで決定的に検証できる。
2. [`JobRegistry`] / [`Job`] … `job_id` に紐づく `threading.Event`（`cancel_event`）と
   SSE イベント列を管理する。**`threading.Lock` でスレッドセーフ**。
3. [`raise_if_cancelled`] / [`cancellable_wait`] / [`install_cancellable_sleep`] …
   **「cancellable な待ち」の途中だけ**でキャンセルを検知する仕組み。
   CPU バウンドな処理の合間にチェックを置かないため、検知点は
   「ブロックする待ち」に限定する。

## キャンセルを検知できる「待ち」

| 待ち | 差し込み方 |
|---|---|
| `_tts_throttle` の間隔待ち | `cancellable_wait` |
| 429 の backoff | `time.sleep` → [`install_cancellable_sleep`] 経由の [`cancellable_sleep`] |
| Gemini の tenacity リトライ境界 | `tenacity.nap.sleep` は `time.sleep` を呼ぶため、同じ仕組みで効く |
| iTunes の予算ループ | 曲ごとのチェックポイント（`server.py` 側が 1 曲ずつ呼ぶ） |
| `_build_generate_response` のステップ境界 | `Job.checkpoint()` |

[`install_cancellable_sleep`] は `time.sleep` を**プロセス全体で**差し替えるが、
差し替え後の関数は「ジョブスレッドなら cancellable、それ以外は通常の sleep」と
**挙動を保存する**。テストが `time.sleep` を monkeypatch するだけの操作は不要になる。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("retro_radio.jobs")

# --- ジョブ状態機械 ---------------------------------------------------------------
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

#: **終端状態**。ここに到達したジョブは SSE のストリームを有限に終了できる。
TERMINAL_STATES = frozenset({STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED})

# --- SSE イベント種別 -------------------------------------------------------------
# イベント名は UI の既存ラベル（static/index.html の 4 ステップ）へ 1 対 1 で写像できる。
EVENT_JOB_STARTED = "job.started"
EVENT_SCRIPT_STARTED = "script.started"
EVENT_SCRIPT_DONE = "script.done"
EVENT_TTS_SEGMENT = "tts.segment"
EVENT_TTS_DONE = "tts.done"
EVENT_MUSIC_STARTED = "music.started"
EVENT_MUSIC_DONE = "music.search.done"
EVENT_PLAYLIST_DONE = "playlist.done"
EVENT_ESTIMATE = "estimate"
EVENT_DONE = "done"
EVENT_FAILED = "failed"
EVENT_CANCELLED = "cancelled"

#: UI の既存ラベル（`static/index.html` の 4 ステップ表示）。
#: S9 はこの表をそのまま使って表示を差し替えるだけでよい。
UI_STEP_LABELS: Tuple[str, ...] = (
    "電波を受信中",   # 電波を受信中
    "原稿を書く",     # 原稿を書く
    "読み上げる",     # 読み上げる
    "ヒット曲を送る",  # ヒット曲を送る
)

#: イベント名 → UI ラベル。**ラベルが無いイベント（進捗・終端）は UI 側で無視する。**
UI_LABEL_BY_EVENT: Dict[str, str] = {
    EVENT_JOB_STARTED: "電波を受信中",
    EVENT_SCRIPT_STARTED: "原稿を書く",
    EVENT_SCRIPT_DONE: "原稿を書く",
    EVENT_TTS_SEGMENT: "読み上げる",
    EVENT_TTS_DONE: "読み上げる",
    EVENT_MUSIC_STARTED: "ヒット曲を送る",
    EVENT_MUSIC_DONE: "ヒット曲を送る",
    EVENT_PLAYLIST_DONE: "ヒット曲を送る",
}

#: 統計に記録するステップキー。
STEP_SCRIPT = "script"
STEP_TTS = "tts"
STEP_MUSIC = "music"

#: SSE の keep-alive コメント間隔（秒）。プロキシの idle timeout より短く取る。
SSE_KEEPALIVE_SECONDS = 15.0

#: SSE ストリームの最大寿命（秒）。**無限に開いたままにしない**ための上限。
SSE_MAX_SECONDS = 900.0

#: `estimated_ms` のポーリング推奨間隔（秒）。
DEFAULT_POLL_AFTER_SECONDS = 1.5

#: サンプルの取り方。「直近 N 回」を使い、古い観測の影響を切る。
LATENCY_WINDOW = 64


class JobCancelled(Exception):
    """ジョブが協調的にキャンセルされた（クライアント abort / `DELETE /api/jobs/{id}`）。"""


# --- スレッドローカル: 実行中のジョブのキャンセルイベント -------------------------
_local = threading.local()


def current_event() -> Optional[threading.Event]:
    """このスレッドで実行中のジョブの `cancel_event`（無ければ `None`）。"""
    return getattr(_local, "cancel_event", None)


def bind_event(event: Optional[threading.Event]) -> Optional[threading.Event]:
    """`cancel_event` をこのスレッドに紐付ける（ワーカースレッドの入口で使う）。"""
    previous = getattr(_local, "cancel_event", None)
    _local.cancel_event = event
    return previous


def unbind_event() -> None:
    """紐付けを解除する（ワーカースレッドの `finally`）。"""
    _local.cancel_event = None


def raise_if_cancelled(step: str = "") -> None:
    """キャンセルされていれば [`JobCancelled`] を送出する。

    Parameters
    ----------
    step:
        ログ用のステップ名。**例外扱いにした箇所が追える**ようにだけ使う。
    """
    event = current_event()
    if event is not None and event.is_set():
        raise JobCancelled(step or "unknown")


def cancellable_wait(event: Optional[threading.Event], timeout: float) -> bool:
    """キャンセル可能な待ち。

    Returns
    -------
    bool
        ``True`` = 時間が来て待つ蹭った / キャンセルされた。
        ``False`` = **キャンセルされた**（= 呼び出し側は直ちに中断すべき）。
    """
    if event is None:
        if timeout > 0:
            _original_sleep(timeout)
        return True
    if event.wait(timeout):
        return False
    raise_if_cancelled("wait")
    return True


_original_sleep = time.sleep
_sleep_installed = False
_sleep_lock = threading.Lock()


def cancellable_sleep(seconds: float) -> None:
    """`time.sleep` の差し替え先。**ジョブスレッドではキャンセル-aware**。

    - ジョブ线程以外: 普通の `time.sleep` と**完全に同じ**挙動。
    - ジョブスレッド: `cancel_event.wait()` で待ち、セットされたら [`JobCancelled`]。

    `tenacity.nap.sleep` は `time.sleep` を呼ぶため、Gemini の指数バックオフ
    （`core/script_generator.py` の `@retry`）も自動的にキャンセル可能になる。
    """
    if seconds <= 0:
        return
    event = current_event()
    if event is None:
        _original_sleep(seconds)
        return
    if event.wait(seconds):
        raise JobCancelled("sleep")


def install_cancellable_sleep() -> bool:
    """`time.sleep` を [`cancellable_sleep`] へ差し替える（冪等）。

    Returns
    -------
    bool
        今回初めて差し替えたなら `True`。
    """
    global _sleep_installed
    with _sleep_lock:
        if _sleep_installed:
            return False
        time.sleep = cancellable_sleep  # type: ignore[assignment]
        _sleep_installed = True
        return True


def sleep_is_cancellable() -> bool:
    """差し替え済みか（テストの観測点）。"""
    return time.sleep is cancellable_sleep


# --- 同時実行スロットのスパイ -----------------------------------------------------
class GenerationSlots(threading.BoundedSemaphore):
    """`_generation_slots` のラッパ。**acquire / release の成功回数を数える**。

    なぜ数えるのか（本提案の核心的な回帰防止）:

    > 「クライアントが abort した後に**サーバのスロットが確実に解放される**」ことを
    > sleep ベースの実測ではなく**決定的に**検証する必要がある。

    したがって本クラスは `release()` の呼び出し回数を公開し、テストが
    「abort 前 → acquire=N / release=N-1、abort 後 → acquire=N / release=N」
    を assert できるようにする。`threading.BoundedSemaphore` を継承しているため
    既存の `._value`（空き数）参照もそのまま通る。
    """

    def __init__(self, value: int = 1) -> None:
        super().__init__(value)
        self._spy_lock = threading.Lock()
        self._acquire_count = 0
        self._release_count = 0

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:  # type: ignore[override]
        acquired = super().acquire(blocking, timeout)
        if acquired:
            with self._spy_lock:
                self._acquire_count += 1
        return bool(acquired)

    def release(self) -> None:  # type: ignore[override]
        with self._spy_lock:
            self._release_count += 1
        super().release()

    @property
    def acquire_count(self) -> int:
        return self._acquire_count

    @property
    def release_count(self) -> int:
        return self._release_count

    @property
    def in_use(self) -> int:
        """現在保持されているスロット数（`取得数 - 解放数`）。"""
        with self._spy_lock:
            return self._acquire_count - self._release_count

    def snapshot(self) -> Dict[str, int]:
        """テスト用の観測点（値ではなく**回数**を返す）。"""
        with self._spy_lock:
            return {
                "acquire_count": self._acquire_count,
                "release_count": self._release_count,
                "in_use": self._acquire_count - self._release_count,
                "free": int(getattr(self, "_value", 0)),
            }

    def reset_spies(self) -> None:
        """観測値を 0 に戻す（テストの前後で挟む）。"""
        with self._spy_lock:
            self._acquire_count = 0
            self._release_count = 0


# --- 実測レイテンシの統計 ---------------------------------------------------------
class LatencyStats:
    """ステップごとの**実測**レイテンシを保持し、p50 / p95 を提供する。

    **ハードコードした p50 / p95 を 1 つの値として持ち回さない**ため、
    サンプルが無いときは `percentile()` が `None` を返し、
    呼び出し側が「既定値の出典」を明示して shoulder する。
    """

    def __init__(self, window: int = LATENCY_WINDOW) -> None:
        self._lock = threading.Lock()
        self._samples: Dict[str, Deque[float]] = {}
        self._window = max(1, int(window))

    def record(self, step: str, milliseconds: float) -> None:
        with self._lock:
            bucket = self._samples.setdefault(step, deque(maxlen=self._window))
            bucket.append(float(milliseconds))

    def samples(self, step: str) -> List[float]:
        with self._lock:
            return list(self._samples.get(step, ()))

    def count(self, step: str) -> int:
        with self._lock:
            return len(self._samples.get(step, ()))

    def percentile(self, step: str, quantile: float) -> Optional[float]:
        """実測サンプルの分位点。**サンプルが無ければ `None`**（既定値を捏造しない）。"""
        values = sorted(self.samples(step))
        if not values:
            return None
        if len(values) == 1:
            return values[0]
        position = quantile * (len(values) - 1)
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        weight = position - lower
        return values[lower] * (1.0 - weight) + values[upper] * weight

    def p50(self, step: str) -> Optional[float]:
        return self.percentile(step, 0.50)

    def p95(self, step: str) -> Optional[float]:
        return self.percentile(step, 0.95)

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()


class CacheStats:
    """TTS キャッシュの**状態**（ヒット / ミス）。推定の入力になる。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def record_hit(self) -> None:
        with self._lock:
            self._hits += 1

    def record_miss(self) -> None:
        with self._lock:
            self._misses += 1

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hits

    @property
    def misses(self) -> int:
        with self._lock:
            return self._misses

    def hit_rate(self) -> Optional[float]:
        """キャッシュヒット率。**観測が無ければ `None`**（= 全ミスとみなす）。"""
        with self._lock:
            total = self._hits + self._misses
            if total <= 0:
                return None
            return self._hits / float(total)

    def clear(self) -> None:
        with self._lock:
            self._hits = 0
            self._misses = 0


# --- 推定値（estimated_ms）--------------------------------------------------------
#: **実測が無いときの既定値の出典を明記する。**
#:
#: * `DEFAULT_TTS_CALL_MS` … gTTS の 1 回あたりの実測在没有のときの保守値。
#:   出典は「429 backoff + スループット中等」を想定した保守値で、
#:   `settings.tts_retry_backoff_seconds` から導出できる値的上限として
#:   「backoff 1 回分 + 固定 1000ms」を使う。**実測サンプルが 1 件でもあれば
#:   そちらが必ず優先される**（`LatencyStats.p50` が `None` を返さない限り）。
#: * `DEFAULT_SCRIPT_CALL_MS` … Gemini の p95 相当。
#:   設定 `max_retries` と `retry_wait_max` から「 tenacity が全身待てる時間」を
#:   導出し、そこに 1 回の応答時間を足した保守値。
#:   **実測（`LatencyStats.p95("script")`）があればそちらが必ず使われる。**
#: * 楽曲検索は**設定からそのまま導出**できる（`itunes_timeout_connect` +
#:   `itunes_timeout_read` = 1 曲あたりの最悪時間）。
DEFAULT_TTS_CALL_MS = 1000.0 + 2500.0


def default_script_call_ms(settings: Any) -> float:
    """Gemini の既定 p95（**設定から導出**する。ハードコードした 1 つの値を持たない）。"""
    return float(
        settings.max_retries * settings.retry_wait_max + 2
    ) * 1000.0


def default_tts_call_ms(settings: Any) -> float:
    """gTTS 1 回あたりの既定 p50（**設定から導出**する）。"""
    return float(settings.tts_retry_backoff_seconds) * 1000.0 + 1000.0


def default_music_call_ms(settings: Any) -> float:
    """iTunes 1 回あたりの既定 p50（**設定から導出**する = 1 曲あたり最悪時間）。"""
    return float(
        settings.itunes_timeout_connect + settings.itunes_timeout_read
    ) * 1000.0


@dataclass(frozen=True)
class EstimateBreakdown:
    """`estimated_ms` の内訳。UI が「なぜこのくらい」を説明できるようにする。"""

    total_ms: int
    tts_calls: int
    tts_misses: int
    tts_hit_rate: Optional[float]
    per_tts_ms: float
    script_ms: float
    music_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_ms": self.total_ms,
            "tts_calls": self.tts_calls,
            "tts_misses": self.tts_misses,
            "tts_hit_rate": self.tts_hit_rate,
            "per_tts_ms": round(self.per_tts_ms, 1),
            "script_ms": round(self.script_ms, 1),
            "music_ms": round(self.music_ms, 1),
        }


def estimate_generation_ms(
    settings: Any,
    stats: LatencyStats,
    cache_stats: CacheStats,
    *,
    segment_hint: Optional[int] = None,
) -> EstimateBreakdown:
    """**そのキャッシュ状態から** `estimated_ms` を計算する。

    計算式（計画書「4. 提案④」実装案 1）::

        estimated_ms = tts_cache_miss 数 × (tts_min_interval + p50 tts latency)
                     + gemini p95
                     + music p50

    各項の出典:

    * ``tts_cache_miss 数``
      ``tts_calls = セグメント数 + 1（全体版 TTS）``。
      セグメント数は生成前には未定のため、**直近の実測**（`segment_hint`）が無くば
      設定 `program_min_song_count`（1 パスの最小曲数）をトーク数の上限目安に使う。
      ミス数は ``tts_calls × (1 - ヒット率)``。**ヒット率の観測が無い場合は
      ヒット率 0.0（全ミス）**＝保守側。
    * ``p50 tts latency``
      `LatencyStats.p50("tts")`（実測）。**サンプルが 0 件なら**
      [`default_tts_call_ms`]（= ``tts_retry_backoff_seconds`` から導出）。
    * ``gemini p95``
      `LatencyStats.p95("script")`（実測）。**0 件なら** [`default_script_call_ms`]
      （= ``max_retries × retry_wait_max`` から導出）。
    * ``music p50``
      `LatencyStats.p50("music")`（実測）。**0 件なら** [`default_music_call_ms`]
      （= ``itunes_timeout_connect + itunes_timeout_read``、設定値そのもの）。

    **ハードコードした「実測 p50/p95」を 1 つの値として持ち回さない**:
    既定値はすべて上のとおり**設定値から導出**され、サンプル 只要 1 件があれば
    実測が必ず優先される。
    """
    interval_ms = float(settings.tts_min_interval_seconds) * 1000.0
    per_tts = stats.p50(STEP_TTS)
    if per_tts is None:
        per_tts = default_tts_call_ms(settings)
    else:
        per_tts = float(per_tts)

    script_ms = stats.p95(STEP_SCRIPT)
    if script_ms is None:
        script_ms = default_script_call_ms(settings)

    music_ms = stats.p50(STEP_MUSIC)
    if music_ms is None:
        music_ms = default_music_call_ms(settings)

    segments = int(segment_hint) if segment_hint else int(settings.program_min_song_count)
    tts_calls = max(1, segments) + 1  # セグメント TTS + 全体版 TTS

    hit_rate = cache_stats.hit_rate()
    if hit_rate is None:
        hit_rate = 0.0
    tts_misses = int(round(tts_calls * (1.0 - hit_rate)))
    if tts_misses < 1:
        # 全部ヒットでも gTTS は 1 回は呼ぶ（ファイル名の検証コストは残るため）。
        tts_misses = 1

    total = tts_misses * (interval_ms + per_tts) + float(script_ms) + float(music_ms)
    return EstimateBreakdown(
        total_ms=int(round(total)),
        tts_calls=tts_calls,
        tts_misses=tts_misses,
        tts_hit_rate=cache_stats.hit_rate(),
        per_tts_ms=interval_ms + per_tts,
        script_ms=float(script_ms),
        music_ms=float(music_ms),
    )


# --- イベント ---------------------------------------------------------------------
@dataclass(frozen=True)
class JobEvent:
    """SSE 1 イベント。**順番（`seq`）が保証される**ので再接続しても欠けない。"""

    seq: int
    name: str
    data: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, default=str)
        return f"id: {self.seq}\nevent: {self.name}\ndata: {payload}\n\n"

    @property
    def ui_label(self) -> Optional[str]:
        return UI_LABEL_BY_EVENT.get(self.name)


def sse_comment(text: str = "keep-alive") -> str:
    """keep-alive コメント。プロキシがバッファで固まるのを防ぐ。"""
    return f": {text}\n\n"


# --- ジョブ -----------------------------------------------------------------------
class Job:
    """1 件の番組生成。**`cancel_event` と SSE イベント列を持つ**。

    スレッド安全性: 内部状態は `threading.Condition`（`RLock` 由来）で保護する。
    非同期ハンドラ（SSE 購読）とスレッドワーカー（生成）は別スレッドなので、
    イベント列と状態は必ずロック越しに触れる。
    """

    def __init__(
        self,
        tenant_id: str,
        *,
        request_payload: Optional[Dict[str, Any]] = None,
        estimated_ms: int = 0,
        poll_after_ms: int = 1500,
    ) -> None:
        self.job_id = secrets.token_urlsafe(12)
        self.tenant_id = tenant_id or "default"
        self.request_payload = request_payload or {}
        self.estimated_ms = int(estimated_ms)
        self.poll_after_ms = int(poll_after_ms)
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.cancel_event = threading.Event()
        #: クライアントが切断（SSE を閉じた）ことをワーカーに知らせる別イベント。
        self.client_gone = threading.Event()
        self._lock = threading.Condition(threading.Lock())
        self._events: List[JobEvent] = []
        self._seq = 0
        self._state = STATE_QUEUED
        self._result: Optional[Dict[str, Any]] = None
        self._error: Optional[Dict[str, Any]] = None
        self._estimate: Optional[EstimateBreakdown] = None

    # --- 状態 -----------------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def is_finished(self) -> bool:
        with self._lock:
            return self._state in TERMINAL_STATES

    @property
    def result(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._result

    @property
    def error(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._error) if self._error else None

    @property
    def estimate(self) -> Optional[EstimateBreakdown]:
        with self._lock:
            return self._estimate

    def set_estimate(self, breakdown: EstimateBreakdown) -> None:
        with self._lock:
            self._estimate = breakdown
            self.estimated_ms = breakdown.total_ms

    def mark_running(self) -> None:
        with self._lock:
            if self._state == STATE_QUEUED:
                self._state = STATE_RUNNING
                self.started_at = time.time()

    # --- イベント -------------------------------------------------------------
    def emit(self, name: str, **data: Any) -> JobEvent:
        """1 イベントを確定して購読者へ通知する。"""
        with self._lock:
            self._seq += 1
            event = JobEvent(
                seq=self._seq, name=name, data=dict(data), created_at=time.time()
            )
            self._events.append(event)
            self._lock.notify_all()
        return event

    def events_after(self, after_seq: int) -> List[JobEvent]:
        with self._lock:
            return [event for event in self._events if event.seq > after_seq]

    def wait_for_events(self, after_seq: int, timeout: float) -> List[JobEvent]:
        """`after_seq` より後のイベントを待つ。**無ければ timeout まで空で待つ**。"""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            while True:
                pending = [event for event in self._events if event.seq > after_seq]
                if pending:
                    return pending
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._lock.wait(remaining)

    # --- 終端 -----------------------------------------------------------------
    def _terminate(self, state: str) -> None:
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
            self._state = state
            self.finished_at = time.time()
            self._lock.notify_all()

    def succeed(self, result: Dict[str, Any]) -> None:
        with self._lock:
            self._result = result
        self._terminate(STATE_SUCCEEDED)

    def fail(self, reason: str, retryable: bool, detail: Optional[str] = None) -> None:
        with self._lock:
            self._error = {
                "reason": str(reason)[:200],
                "retryable": bool(retryable),
                "detail": (detail or "")[:500],
            }
        self._terminate(STATE_FAILED)

    def mark_cancelled(self, reason: str = "client_abort") -> None:
        with self._lock:
            self._error = {"reason": reason, "retryable": True, "detail": ""}
        self._terminate(STATE_CANCELLED)

    def request_cancel(self) -> bool:
        """キャンセルを**要求**する（ワーカーは次のチェックポイントで止まる）。

        既に終端なら `False`（完了済みなので何もしない）。
        """
        with self._lock:
            if self._state in TERMINAL_STATES:
                return False
        self.cancel_event.set()
        return True

    @property
    def is_cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    # --- チェックポイント -----------------------------------------------------
    def checkpoint(self, step: str) -> None:
        """**cancellable な待ちの直前**で呼ぶ。キャンセル済みなら [`JobCancelled`]。"""
        raise_if_cancelled(step)

    def wait(self, timeout: float) -> bool:
        """cancellable な待ち。キャンセルされたら [`JobCancelled`]。"""
        cancellable_wait(self.cancel_event, timeout)

    # --- 観測 -----------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            state = self._state
            events = [
                {"seq": e.seq, "event": e.name, "data": e.data, "label": e.ui_label}
                for e in self._events
            ]
            estimate = self._estimate
            return {
                "job_id": self.job_id,
                "tenant_id": self.tenant_id,
                "state": state,
                "cancel_requested": self.cancel_event.is_set(),
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_ms": int(
                    ((self.finished_at or time.time()) - self.created_at) * 1000
                ),
                "estimated_ms": self.estimated_ms,
                "poll_after_ms": self.poll_after_ms,
                "estimate": estimate.to_dict() if estimate else None,
                "event_count": len(events),
                "events": events,
                "result": self._result,
                "error": dict(self._error) if self._error else None,
            }


# --- レジストリ -------------------------------------------------------------------
class JobRegistry:
    """`job_id` → [`Job`] の台帳。**`threading.Lock` でスレッドセーフ**。

    FastAPI の非同期ハンドラ（HTTP スレッド / イベントループ）と
    専用ワーカースレッドの両方から同時に触られるため、
    辞書の参照はすべてロック内で行う。
    """

    def __init__(
        self,
        max_jobs: int = 64,
        finished_ttl_seconds: int = 1800,
        latency_window: int = LATENCY_WINDOW,
    ) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._max_jobs = max(1, int(max_jobs))
        self._finished_ttl = max(0, int(finished_ttl_seconds))
        self.stats = LatencyStats(window=latency_window)
        self.cache_stats = CacheStats()

    # --- 作成 / 参照 ----------------------------------------------------------
    def create(
        self,
        tenant_id: str,
        *,
        request_payload: Optional[Dict[str, Any]] = None,
        estimated_ms: int = 0,
        poll_after_ms: int = int(DEFAULT_POLL_AFTER_SECONDS * 1000),
    ) -> Job:
        job = Job(
            tenant_id,
            request_payload=request_payload,
            estimated_ms=estimated_ms,
            poll_after_ms=poll_after_ms,
        )
        with self._lock:
            self._jobs[job.job_id] = job
            self._evict_locked()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """協調的キャンセルを要求する。**未知の ID なら `False`**。"""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        return job.request_cancel()

    def list_for_tenant(self, tenant_id: str) -> List[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.tenant_id == tenant_id]

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()

    def _evict_locked(self) -> None:
        """終端して TTL を過ぎたジョブと、上限を超えたときの最古の実行中ジョブを捨てる。"""
        now = time.time()
        expired = [
            jid
            for jid, job in self._jobs.items()
            if job.is_finished
            and job.finished_at is not None
            and (now - job.finished_at) > self._finished_ttl
        ]
        for jid in expired:
            self._jobs.pop(jid, None)
        overflow = len(self._jobs) - self._max_jobs
        if overflow > 0:
            ordered = sorted(
                (j for j in self._jobs.values() if j.is_finished),
                key=lambda j: j.finished_at or 0.0,
            )
            for job in ordered[:overflow]:
                self._jobs.pop(job.job_id, None)

    # --- 統計 -----------------------------------------------------------------
    def record_step(self, step: str, milliseconds: float) -> None:
        """ステップの実測レイテンシを蓄積する（推定の入力）。"""
        self.stats.record(step, milliseconds)

    def snapshot_stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for step in (STEP_SCRIPT, STEP_TTS, STEP_MUSIC):
            out[step] = {
                "count": self.stats.count(step),
                "p50": self.stats.p50(step),
                "p95": self.stats.p95(step),
            }
        out["tts_cache"] = {
            "hits": self.cache_stats.hits,
            "misses": self.cache_stats.misses,
            "hit_rate": self.cache_stats.hit_rate(),
        }
        return out


#: プロセス全体で 1 個だけ持つレジストリ（ワーカーと HTTP が共有する）。
registry = JobRegistry()


def start_worker(
    job: Job,
    target: Callable[[Job], None],
    *,
    name: Optional[str] = None,
) -> threading.Thread:
    """**専用ワーカースレッド**を起動する。

    FastAPI の `BackgroundTasks` / スレッドプールは使わない。
    理由（計画書「現状の根拠」2）:

    * `BackgroundTasks` は **レスポンスを返してから**走るが、
      クライアント切断を検知する仕組みが無く、ジョブの寿命が制御できない。
    * スレッドプールは HTTP リクエストの処理で使い切られるため、
      「3 台分の平板が同時に走っている」状態で新しいジョブが**Starve** する。

    専用スレッドなら 1 ジョブ = 1 スレッドで、寿命と終了が明示的になる。
    ジョブの `cancel_event` はこのスレッドに [`bind_event`] で紐付ける。
    """
    thread_name = name or f"retro-radio-job-{job.job_id[:8]}"

    def _runner() -> None:
        bind_event(job.cancel_event)
        try:
            if job.is_cancel_requested:
                job.mark_cancelled("cancelled_before_start")
                job.emit(EVENT_CANCELLED, reason="cancelled_before_start")
                return
            job.mark_running()
            job.emit(EVENT_JOB_STARTED, tenant_id=job.tenant_id)
            target(job)
        except JobCancelled as exc:
            logger.info("ジョブをキャンセルしました: job_id=%s step=%s", job.job_id, exc)
            job.emit(EVENT_CANCELLED, reason="client_abort", step=str(exc))
            job.mark_cancelled("client_abort")
        except Exception as exc:  # noqa: BLE001 - ジョブの失敗はここで必ず終端させる
            logger.exception("ジョブが失敗しました: job_id=%s", job.job_id)
            job.emit(EVENT_FAILED, reason=type(exc).__name__, retryable=_is_retryable(exc))
            job.fail(type(exc).__name__, _is_retryable(exc), detail=str(exc))
        finally:
            unbind_event()

    thread = threading.Thread(target=_runner, name=thread_name, daemon=True)
    thread.start()
    return thread


def _is_retryable(exc: BaseException) -> bool:
    """失敗が再試行で成功し得る種別か（UI の「もう一度”按钮を出し分ける）。"""
    return not isinstance(exc, (ValueError, TypeError, JobCancelled))


def new_job(
    registry_obj: JobRegistry,
    tenant_id: str,
    request_payload: Dict[str, Any],
    settings: Any,
) -> Job:
    """[`registry_obj`] に登録した新しいジョブを、推定値つきで作る。"""
    breakdown = estimate_generation_ms(
        settings, registry_obj.stats, registry_obj.cache_stats
    )
    return registry_obj.create(
        tenant_id,
        request_payload=request_payload,
        estimated_ms=breakdown.total_ms,
        poll_after_ms=int(DEFAULT_POLL_AFTER_SECONDS * 1000),
    )


__all__ = [
    "Job",
    "JobRegistry",
    "JobEvent",
    "JobCancelled",
    "GenerationSlots",
    "LatencyStats",
    "CacheStats",
    "EstimateBreakdown",
    "estimate_generation_ms",
    "default_script_call_ms",
    "default_tts_call_ms",
    "default_music_call_ms",
    "registry",
    "new_job",
    "start_worker",
    "current_event",
    "bind_event",
    "unbind_event",
    "raise_if_cancelled",
    "cancellable_wait",
    "cancellable_sleep",
    "install_cancellable_sleep",
    "sleep_is_cancellable",
    "sse_comment",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "STATE_SUCCEEDED",
    "STATE_FAILED",
    "STATE_CANCELLED",
    "TERMINAL_STATES",
    "STEP_SCRIPT",
    "STEP_TTS",
    "STEP_MUSIC",
    "UI_STEP_LABELS",
    "UI_LABEL_BY_EVENT",
    "EVENT_JOB_STARTED",
    "EVENT_SCRIPT_STARTED",
    "EVENT_SCRIPT_DONE",
    "EVENT_TTS_SEGMENT",
    "EVENT_TTS_DONE",
    "EVENT_MUSIC_STARTED",
    "EVENT_MUSIC_DONE",
    "EVENT_PLAYLIST_DONE",
    "EVENT_ESTIMATE",
    "EVENT_DONE",
    "EVENT_FAILED",
    "EVENT_CANCELLED",
    "SSE_KEEPALIVE_SECONDS",
    "SSE_MAX_SECONDS",
    "DEFAULT_POLL_AFTER_SECONDS",
    "LATENCY_WINDOW",
]
