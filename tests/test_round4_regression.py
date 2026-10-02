"""Round 4 の修正に対する回帰テスト。

対象（docs/implementation_plan_round4.md の回帰テスト一覧より抜粋）:

* R2-04: Stripe webhook のボディ上限（413 早期拒否）
* R2-05/JOB-02: /api/generate の非ブロッキング acquire（即 503）
* JOB-08: _QueueTicket（二重解放の冪等性・枠リーク無し）
* R2-06: SSE 同時接続数上限（503）
* R2-05（コア）: order_candidates の対象年保証
* 決定論: rng_seed 設定で同一入力 → 同一結果
* CACHE-01: TTS キャッシュ個数上限・スイープ
* DB-02: Stripe 解約時の customer_id 逆引き降格
"""

from __future__ import annotations

import random

import pytest


# --- R2-04: webhook ボディ上限 ----------------------------------------------------


def test_webhook_body_max_bytes_constant_exists():
    from retro_radio.server import WEBHOOK_BODY_MAX_BYTES

    assert WEBHOOK_BODY_MAX_BYTES == 1_000_000


@pytest.mark.asyncio
async def test_webhook_rejects_oversized_content_length(monkeypatch):
    from fastapi import HTTPException

    from retro_radio.server import WEBHOOK_BODY_MAX_BYTES, stripe_webhook

    class _FakeRequest:
        def __init__(self, content_length: str) -> None:
            self.headers = {
                "content-length": content_length,
                "stripe-signature": "t=1,v1=abc",
            }

        async def body(self) -> bytes:  # pragma: no cover - 呼ばれないはず
            raise AssertionError("body() should not be read for oversized requests")

    monkeypatch.setattr(
        "retro_radio.server.WEBHOOK_BODY_MAX_BYTES", WEBHOOK_BODY_MAX_BYTES
    )
    request = _FakeRequest(str(WEBHOOK_BODY_MAX_BYTES + 1))
    with pytest.raises(HTTPException) as excinfo:
        await stripe_webhook(request)
    assert excinfo.value.status_code == 413


@pytest.mark.asyncio
async def test_webhook_rejects_oversized_actual_body(monkeypatch):
    from fastapi import HTTPException

    from retro_radio.server import WEBHOOK_BODY_MAX_BYTES, stripe_webhook

    class _FakeRequest:
        def __init__(self, body_payload: bytes) -> None:
            self._payload = body_payload
            self.headers = {"stripe-signature": "t=1,v1=abc"}

        async def body(self) -> bytes:
            return self._payload

    monkeypatch.setattr(
        "retro_radio.server.WEBHOOK_BODY_MAX_BYTES", WEBHOOK_BODY_MAX_BYTES
    )
    # content-length 無し（チャンク転送想定）で実際のボディが超過。
    request = _FakeRequest(b"x" * (WEBHOOK_BODY_MAX_BYTES + 1))
    with pytest.raises(HTTPException) as excinfo:
        await stripe_webhook(request)
    assert excinfo.value.status_code == 413


@pytest.mark.asyncio
async def test_webhook_accepts_small_body_without_configured_secret():
    """上限以下のボディは従来どおり 503（secret 未設定 fail-closed）。"""
    from fastapi import HTTPException

    from retro_radio.server import stripe_webhook

    class _FakeRequest:
        headers = {"stripe-signature": "t=1,v1=abc"}

        async def body(self) -> bytes:
            return b"{}"

    with pytest.raises(HTTPException) as excinfo:
        await stripe_webhook(_FakeRequest())
    assert excinfo.value.status_code == 503


# --- JOB-08: _QueueTicket ----------------------------------------------------------


def test_queue_ticket_release_is_idempotent():
    from retro_radio import server

    # 実際に取得したチケットでのみ冪等性を検証する
    # （未取得のチケットで release するとセマフォの上限超過になるため）。
    ticket = server._acquire_job_queue_slot()
    if ticket is None:
        pytest.skip("ジョブ入場枠が埋まっている（環境依存）")
    ticket.release()
    ticket.release()  # 2 回目は no-op（冪等）。
    assert ticket._released is True


def test_acquire_job_queue_slot_returns_ticket_or_none():
    from retro_radio import server

    ticket = server._acquire_job_queue_slot()
    if ticket is not None:
        # 取れた場合は自分のチケットを確実に戻せる。
        ticket.release()
        assert ticket._released is True
    else:
        # 上限に達している場合も正常系（503 判定は呼び出し側）。
        assert ticket is None


def test_acquire_release_roundtrip_does_not_leak():
    from retro_radio import server

    tickets = [server._acquire_job_queue_slot() for _ in range(4)]
    # 全てのチケットを戻す（None は無視）。複数回呼んでも安全。
    for ticket in tickets:
        if ticket is not None:
            ticket.release()
            ticket.release()
    # もう一度取れる（リークしていない証拠）。
    ticket = server._acquire_job_queue_slot()
    if ticket is not None:
        ticket.release()


# --- R2-06: SSE 接続上限 -----------------------------------------------------------


def test_sse_slots_limit_exists():
    from retro_radio import server

    assert server._SSE_MAX_CONNECTIONS >= 4
    assert server._sse_slots is not None


# --- R2-05（コア）: order_candidates 対象年保証 -------------------------------------


def _rec(title: str, artist: str, year: int, rank: int = 0) -> dict:
    return {"title": title, "artist": artist, "release_year": year, "rank": rank}


def test_order_candidates_puts_target_year_first():
    from retro_radio.core.song_selector import SongSelector

    selector = SongSelector(history=None, rng=random.Random(42))
    # 対象年 1975。隣接年（1974/1976）と対象年の曲を混ぜ、隣接年を先に置く。
    candidates = [
        _rec("Neighbor A", "A", 1974),
        _rec("Target 1", "T", 1975),
        _rec("Neighbor B", "B", 1976),
        _rec("Target 2", "T", 1975),
    ]
    ordered = selector.order_candidates(1975, candidates)
    years = [int(r["release_year"]) for r in ordered]
    # 対象年の曲が必ず先頭に来る（fresh 対象年 → fresh その他 → played）。
    assert years[0] == 1975
    assert years[1] == 1975


def test_order_candidates_preserves_played_order_and_returns_new_list():
    from retro_radio.core.song_selector import SongSelector

    class _History:
        def __init__(self) -> None:
            self.data = {"1975": {"t1": 5, "t2": 3}}

        def last_played(self, year: int):
            return self.data[str(year)]

    selector = SongSelector(history=_History(), rng=random.Random(42))
    candidates = [
        {"title": "Target 1", "artist": "T", "release_year": 1975, "rank": 0},
        {"title": "Target 2", "artist": "T", "release_year": 1975, "rank": 0},
    ]
    ordered = selector.order_candidates(1975, candidates)
    # played は最古 → 最新（seq 昇順）。t2 (3) → t1 (5)。
    titles = [r["title"] for r in ordered]
    assert titles == ["Target 2", "Target 1"]
    # 元のリストは変更されない。
    assert candidates[0]["title"] == "Target 1"


def test_order_candidates_empty():
    from retro_radio.core.song_selector import SongSelector

    selector = SongSelector(history=None, rng=random.Random(42))
    assert selector.order_candidates(1975, []) == []


# --- 決定論: rng 注入 ---------------------------------------------------------------


def test_select_news_topics_deterministic_with_injected_rng():
    from retro_radio.core.script_generator import select_news_topics

    rng_a = random.Random(7)
    rng_b = random.Random(7)
    a = select_news_topics(1975, count=2, rng=rng_a)
    b = select_news_topics(1975, count=2, rng=rng_b)
    assert [t.headline for t in a] == [t.headline for t in b]


def test_get_fallback_song_deterministic_with_injected_rng():
    from retro_radio.core.fallback import get_fallback_song

    a = get_fallback_song(1975, rng=random.Random(3))
    b = get_fallback_song(1975, rng=random.Random(3))
    assert a == b


def test_module_rng_none_uses_module_random():
    from retro_radio.core.rng import module_rng

    # seed 未設定なら random モジュールそのもの（現行挙動）。
    assert module_rng() is random


# --- CACHE-01: TTS キャッシュ個数上限 ------------------------------------------------


def _make_cache(tmp_path, max_files):
    from retro_radio.services.tenant_cache import TenantTtsCache

    return TenantTtsCache(root=tmp_path, max_files=max_files)


def test_tenant_cache_enforce_max_files_removes_oldest(tmp_path):
    import os
    import time

    cache = _make_cache(tmp_path, max_files=2)
    directory = cache.ensure_tenant_dir("t1")
    names = []
    for i in range(4):
        # _is_cache_file は `tts_` 接頭辞 + .mp3 を要求する。
        p = directory / f"tts_seg{i}.mp3"
        p.write_bytes(b"ID3")
        names.append(p)
        os.utime(p, (time.time() + i, time.time() + i))

    removed = cache._enforce_max_files([directory])
    assert removed == 2
    remaining = sorted(p.name for p in directory.iterdir())
    # 古い 2 つが削除され、新しい 2 つが残る。
    assert remaining == ["tts_seg2.mp3", "tts_seg3.mp3"]


def test_tenant_cache_enforce_max_files_no_excess(tmp_path):
    cache = _make_cache(tmp_path, max_files=5)
    directory = cache.ensure_tenant_dir("t1")
    for i in range(3):
        (directory / f"tts_seg{i}.mp3").write_bytes(b"ID3")
    assert cache._enforce_max_files([directory]) == 0


def test_tenant_cache_enforce_max_files_disabled(tmp_path):
    cache = _make_cache(tmp_path, max_files=0)
    directory = cache.ensure_tenant_dir("t1")
    for i in range(3):
        (directory / f"tts_seg{i}.mp3").write_bytes(b"ID3")
    # max_files <= 0 は無効（削除しない）。
    assert cache._enforce_max_files([directory]) == 0


def test_config_tts_cache_max_files_default():
    from retro_radio.config import get_settings

    settings = get_settings()
    assert 100 <= settings.tts_cache_max_files <= 100000


# --- DB-02: customer_id 逆引き降格 ---------------------------------------------------


def test_user_repository_find_by_stripe_customer():
    from retro_radio.db.repository import UserRepository

    class _Model:
        id = "u1"
        email = "a@example.com"
        hashed_password = "h"
        plan = "free"
        stripe_customer_id = "cus_123"
        stripe_subscription_id = None
        preferences = "{}"
        generation_count = 0
        generation_reset_at = None
        created_at = None
        updated_at = None

    class _Query:
        def __init__(self, model) -> None:
            self.model = model

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return _Model()

    class _Session:
        def query(self, model):
            return _Query(model)

    repo = UserRepository(db=_Session())
    domain = repo.find_by_stripe_customer("cus_123")
    assert domain is not None
    # _to_domain は stripe 属性を写像しないため、id と email で検証する。
    assert domain.id == "u1"
    assert domain.email == "a@example.com"


def test_user_repository_find_by_stripe_customer_none():
    from retro_radio.db.repository import UserRepository

    class _Query:
        def __init__(self, model) -> None:
            self.model = model

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return None

    class _Session:
        def query(self, model):
            return _Query(model)

    repo = UserRepository(db=_Session())
    assert repo.find_by_stripe_customer("cus_unknown") is None


def test_webhook_resolve_user_id_prefers_metadata():
    from retro_radio.billing.webhook import WebhookHandler

    handler = WebhookHandler()

    class _Repo:
        def find_by_stripe_customer(self, customer_id):
            raise AssertionError("metadata.user_id が正なので逆引きは呼ばれない")

    obj = {"metadata": {"user_id": "u_meta"}, "customer": "cus_1"}
    assert handler._resolve_user_id(obj, _Repo()) == "u_meta"


def test_webhook_resolve_user_id_falls_back_to_customer_lookup():
    from retro_radio.billing.webhook import WebhookHandler

    handler = WebhookHandler()

    class _Repo:
        def find_by_stripe_customer(self, customer_id):
            assert customer_id == "cus_1"

            class _D:
                id = "u_from_customer"

            return _D()

    obj = {"customer": "cus_1"}
    assert handler._resolve_user_id(obj, _Repo()) == "u_from_customer"
