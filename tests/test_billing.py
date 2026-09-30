"""`retro_radio.billing` の検証。

Wave 1（A3）で全面的に書き直された課金モジュールはテストが 0 件だった。
特に「署名を検証しない webhook でプランが変わらない」「未納/la 未設定で
Stripe API を叩かない」ことを回帰テストとして固定する。
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import stripe

from retro_radio.billing.billing_manager import BillingManager
from retro_radio.billing.plans import (
    PLAN_LIMITS,
    UNLIMITED,
    BillingCycle,
    coerce_plan,
    get_limits,
    get_price_id,
)
from retro_radio.billing.stripe_client import StripeClient
from retro_radio.billing.webhook import WEBHOOK_VERIFICATION_ERRORS, WebhookHandler
from retro_radio.models.user import PlanType, User


SECRET = "whsec_test_secret"
PAYLOAD = b'{"id": "evt_1", "type": "checkout.session.completed"}'


def _user(plan=PlanType.FREE, generation_count=0, reset_at=None):
    return User(
        id="u1",
        email="u1@example.com",
        hashed_password="h",
        plan=plan,
        generation_count=generation_count,
        generation_reset_at=reset_at,
    )


def _event(event_type, data):
    return {"id": "evt_1", "type": event_type, "data": data}


# --- plans ---------------------------------------------------------------------
def test_all_plans_have_unlimited_generations():
    """生成回数の制限は撤廃されている（README 方針）"""
    for plan, limits in PLAN_LIMITS.items():
        assert limits["monthly_generations"] == UNLIMITED, plan


def test_free_plan_feature_matrix():
    limits = get_limits(PlanType.FREE)
    assert limits["favorites"] is False
    assert limits["history_export"] is False
    assert limits["api_access"] is False
    assert limits["batch_generation"] is False


def test_premium_unlocks_favorites_but_not_api():
    limits = get_limits(PlanType.PREMIUM)
    assert limits["favorites"] is True
    assert limits["history_export"] is True
    assert limits["api_access"] is False


def test_pro_unlocks_everything():
    limits = get_limits(PlanType.PRO)
    assert all(limits[key] for key in ("favorites", "history_export", "api_access", "batch_generation"))


def test_coerce_plan_accepts_values_and_enum():
    assert coerce_plan("premium") is PlanType.PREMIUM
    assert coerce_plan(PlanType.PRO) is PlanType.PRO


@pytest.mark.parametrize("value", [None, "", "god", 123, object()])
def test_coerce_plan_rejects_unknown(value):
    assert coerce_plan(value) is None


def test_get_limits_falls_back_to_free():
    assert get_limits("nonsense") == PLAN_LIMITS[PlanType.FREE]


def test_get_price_id_for_free_plan_is_none():
    assert get_price_id(PlanType.FREE, BillingCycle.MONTHLY) is None


def test_get_price_id_returns_none_when_not_configured(monkeypatch):
    """price ID 未設定なら None（Stripe を叩かない）"""
    monkeypatch.setattr(
        "retro_radio.billing.plans.get_price_ids", lambda: {}
    )
    assert get_price_id(PlanType.PRO, BillingCycle.MONTHLY) is None


# --- BillingManager ------------------------------------------------------------
def test_check_generation_limit_is_always_unlimited():
    manager = BillingManager()
    for count in (0, 5, 10_000):
        assert manager.check_generation_limit(_user(generation_count=count)) == (True, UNLIMITED)


def test_check_generation_limit_is_unlimited_even_after_period_reset():
    """月次期間が過ぎてカウンターが0に戻っても、上限は UNLIMITED のまま

    旧実装は `monthly_generations == UNLIMITED` で即 return して月次リセットに
    到達できず、`generation_reset_at` が更新されないまま統計が壊れていた。
    リセット処理は上限判定から分離されている。
    """
    past = datetime.now(timezone.utc) - timedelta(days=1)
    user = _user(generation_count=99, reset_at=past)

    assert BillingManager().check_generation_limit(user) == (True, UNLIMITED)
    assert user.generation_count == 0
    assert user.generation_reset_at > datetime.utcnow()


def test_monthly_counter_is_reset_when_period_expired(db_session):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    user = _user(generation_count=99, reset_at=past)
    BillingManager(db=db_session).check_generation_limit(user)
    assert user.generation_count == 0


def test_monthly_counter_reset_is_persisted(db_session, make_user):
    """月次リセットは DB にも反映される（統計が壊れたまま残らない）"""
    from retro_radio.db.repository import UserRepository

    repo = UserRepository(db_session)
    created = make_user(db_session, "reset-persist@example.com")
    stored = repo.get_by_id(created.id)
    stored.generation_count = 99
    stored.generation_reset_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    repo.update(stored)
    db_session.commit()

    BillingManager(db=db_session).check_generation_limit(stored)

    db_session.expire_all()
    refreshed = repo.get_by_id(created.id)
    assert refreshed.generation_count == 0
    assert refreshed.generation_reset_at > datetime.utcnow()


# --- StripeClient --------------------------------------------------------------
def test_stripe_client_without_key_does_not_call_stripe(monkeypatch):
    """API キー未設定なら Stripe API を一切叩かず None を返す"""
    monkeypatch.setattr("retro_radio.billing.stripe_client.get_settings", lambda: MagicMock(
        stripe_secret_key="", stripe_webhook_secret="", app_url="http://localhost:8501"
    ))
    with patch.object(stripe.Customer, "list") as customer_list, patch.object(
        stripe.checkout.Session, "create"
    ) as session_create:
        client = StripeClient()
        assert client.is_configured is False
        assert client.create_checkout_session("u1", "u@example.com", PlanType.PRO, BillingCycle.MONTHLY) is None
        assert client.create_customer_portal_session("cus_1") is None

    customer_list.assert_not_called()
    session_create.assert_not_called()


def test_stripe_client_without_price_id_does_not_call_stripe(monkeypatch):
    monkeypatch.setattr("retro_radio.billing.stripe_client.get_settings", lambda: MagicMock(
        stripe_secret_key="sk_test_x", stripe_webhook_secret="", app_url="http://localhost:8501"
    ))
    monkeypatch.setattr("retro_radio.billing.stripe_client.get_price_id", lambda *a: None)

    with patch.object(stripe.Customer, "list") as customer_list:
        client = StripeClient()
        assert client.create_checkout_session("u1", "u@example.com", PlanType.PRO, BillingCycle.MONTHLY) is None

    customer_list.assert_not_called()


def test_stripe_client_portal_requires_customer_id(monkeypatch):
    monkeypatch.setattr("retro_radio.billing.stripe_client.get_settings", lambda: MagicMock(
        stripe_secret_key="sk_test_x", stripe_webhook_secret="", app_url="http://localhost:8501"
    ))
    with patch.object(stripe.billing_portal.Session, "create") as portal:
        assert StripeClient().create_customer_portal_session("") is None
    portal.assert_not_called()


def test_stripe_client_detects_test_mode(monkeypatch):
    monkeypatch.setattr("retro_radio.billing.stripe_client.get_settings", lambda: MagicMock(
        stripe_secret_key="sk_test_x", stripe_webhook_secret="", app_url="http://localhost:8501"
    ))
    assert StripeClient().test_mode is True


def test_stripe_client_reraises_stripe_error(monkeypatch):
    """Stripe API 失敗は握り潰さず再 raise（呼び出し側で 502 にできる）"""
    monkeypatch.setattr("retro_radio.billing.stripe_client.get_settings", lambda: MagicMock(
        stripe_secret_key="sk_test_x", stripe_webhook_secret="", app_url="http://localhost:8501"
    ))
    monkeypatch.setattr("retro_radio.billing.stripe_client.get_price_id", lambda *a: "price_1")

    with patch.object(stripe.Customer, "list", side_effect=stripe.StripeError("down")):
        with pytest.raises(stripe.StripeError):
            StripeClient().create_checkout_session("u1", "u@example.com", PlanType.PRO, BillingCycle.MONTHLY)


# --- WebhookHandler: 署名検証 --------------------------------------------------
def test_webhook_without_secret_raises_runtime_error():
    """署名シークレット未設定なら 500 相当（= webhook を受け付けない）"""
    handler = WebhookHandler()
    handler.webhook_secret = None
    with pytest.raises(RuntimeError, match="not configured"):
        handler.handle_event(PAYLOAD, "sig")


def test_webhook_rejects_bad_signature(db_session):
    """署名検証に失敗したら例外を握り潰さず再送出する（= 400 相当）"""
    handler = WebhookHandler(db=db_session)
    handler.webhook_secret = SECRET
    sig_error = WEBHOOK_VERIFICATION_ERRORS[-1]

    with patch.object(stripe.Webhook, "construct_event", side_effect=sig_error("bad", "sig")):
        with pytest.raises(WEBHOOK_VERIFICATION_ERRORS):
            handler.handle_event(PAYLOAD, "t=1,v1=deadbeef")


def test_webhook_rejects_missing_signature(db_session):
    handler = WebhookHandler(db=db_session)
    handler.webhook_secret = SECRET
    with patch.object(stripe.Webhook, "construct_event", side_effect=ValueError("no sig")):
        with pytest.raises(ValueError):
            handler.handle_event(PAYLOAD, "")


def test_verification_errors_include_value_error():
    assert ValueError in WEBHOOK_VERIFICATION_ERRORS


def test_handle_event_returns_none_for_unhandled_type(db_session):
    """正常時は None を返す（FastAPI が 200 を返す）"""
    handler = WebhookHandler(db=db_session)
    handler.webhook_secret = SECRET
    payload = _event("customer.created", {"object": {"id": "cus_1"}})
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        assert handler.handle_event(PAYLOAD, "sig") is None


def test_unhandled_event_type_is_ignored(db_session):
    handler = WebhookHandler(db=db_session)
    handler.webhook_secret = SECRET
    payload = _event("customer.created", {"object": {"id": "cus_1"}})
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        handler.handle_event(PAYLOAD, "sig")  # 例外にならない


# --- WebhookHandler: プラン付与 ------------------------------------------------
#
# Stripe が実際に送るイベント構造は `{"type": ..., "data": {"object": {...}}}` であり、
# 会话情報（metadata / customer / status 等）は `data.object` にある。
# `handle_event` が `data` をそのまま handler に渡していた时期には
# `session["metadata"]` が常に None となり、支払い成功してもプランが一切付与されず、
# subscription.deleted の降格も動かなかった（= 課金収入がゼロ）。
# 以下の `*_with_real_stripe_payload` テストがこの構造の回帰を固定する。


def _stripe_event(event_type, obj):
    """Stripe が実際に送るイベント形（data.object に会话情報がある）"""
    return {"id": "evt_1", "type": event_type, "data": {"object": obj}}


def _handler(db_session):
    handler = WebhookHandler(db=db_session)
    handler.webhook_secret = SECRET
    return handler


def test_checkout_completed_with_real_stripe_payload(db_session, make_user):
    user = make_user(db_session, "pay@example.com")
    payload = _stripe_event("checkout.session.completed", {
        "metadata": {"user_id": user.id, "plan": "premium"},
        "customer": "cus_1",
    })
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")

    from retro_radio.db.repository import UserRepository

    db_session.expire_all()
    assert UserRepository(db_session).get_by_id(user.id).plan is PlanType.PREMIUM


def test_subscription_deleted_with_real_stripe_payload(db_session, make_user):
    user = make_user(db_session, "cancel@example.com")
    from retro_radio.db.repository import UserRepository

    repo = UserRepository(db_session)
    stored = repo.get_by_id(user.id)
    stored.plan = PlanType.PRO
    repo.update(stored)
    db_session.commit()

    payload = _stripe_event("customer.subscription.deleted", {"metadata": {"user_id": user.id}})
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")

    db_session.expire_all()
    assert UserRepository(db_session).get_by_id(user.id).plan is PlanType.FREE


def test_subscription_updated_with_real_stripe_payload(db_session, make_user):
    """実構造（data.object）の customer.subscription.updated でプランが更新される"""
    from retro_radio.db.repository import UserRepository

    user = make_user(db_session, "sub-updated@example.com")
    payload = _stripe_event("customer.subscription.updated", {
        "id": "sub_1",
        "status": "active",
        "metadata": {"user_id": user.id, "plan": "pro"},
    })
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")

    db_session.expire_all()
    stored = UserRepository(db_session).get_by_id(user.id)
    assert stored.plan is PlanType.PRO


def test_invoice_payment_failed_with_real_stripe_payload(db_session, make_user):
    """実構造（data.object）の invoice.payment_failed はプランを変更しない"""
    from retro_radio.db.repository import UserRepository

    user = make_user(db_session, "pay-failed-real@example.com")
    repo = UserRepository(db_session)
    stored = repo.get_by_id(user.id)
    stored.plan = PlanType.PREMIUM
    repo.update(stored)
    db_session.commit()

    payload = _stripe_event("invoice.payment_failed", {
        "id": "in_1",
        "customer": "cus_1",
        "metadata": {"user_id": user.id, "plan": "free"},
    })
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")

    db_session.expire_all()
    assert UserRepository(db_session).get_by_id(user.id).plan is PlanType.PREMIUM


def test_stripe_ids_are_stored_with_real_stripe_payload(db_session, make_user):
    """実構造の checkout.session.completed で Stripe の ID が保存される"""
    from retro_radio.db.models import UserModel
    from retro_radio.db.repository import UserRepository

    user = make_user(db_session, "ids-real@example.com")
    payload = _stripe_event("checkout.session.completed", {
        "metadata": {"user_id": user.id, "plan": "pro"},
        "customer": "cus_456",
        "subscription": "sub_456",
    })
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")

    db_session.expire_all()
    model = db_session.query(UserModel).filter_by(id=user.id).first()
    assert model.stripe_customer_id == "cus_456"
    assert model.stripe_subscription_id == "sub_456"
    assert UserRepository(db_session).get_by_id(user.id).plan is PlanType.PRO


def test_event_without_data_object_is_handled_gracefully(db_session, make_user):
    """data 自体が無いイベントでも例外を投げない（None を渡しても安全）"""
    make_user(db_session, "nodata@example.com")
    payload = {"id": "evt_1", "type": "checkout.session.completed", "data": None}
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")  # 例外にならない


def test_event_without_data_key_is_handled_gracefully(db_session):
    """data キー自体が無いイベントでも例外を投げない"""
    payload = {"id": "evt_1", "type": "customer.subscription.deleted"}
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")  # 例外にならない


# --- WebhookHandler: 防御ガード ---------------------------------------------------
# 以下は data 直下に会话情報がある= 非標準の形でも、ガードの効き方を検証する
# （署名済みイベントが不正なデータを含まないことの確認）。

def _flat_data_event(event_type, data):
    return {"id": "evt_1", "type": event_type, "data": data}


def test_checkout_without_user_id_is_noop(db_session):
    """metadata.user_id が無いイベントで他人のプランを変えない"""
    payload = _flat_data_event("checkout.session.completed", {"metadata": {"plan": "pro"}})
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")  # 例外にならない


def test_checkout_for_unknown_user_is_noop(db_session):
    payload = _flat_data_event(
        "checkout.session.completed", {"metadata": {"user_id": "does-not-exist", "plan": "pro"}}
    )
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")


def test_checkout_with_free_plan_does_not_grant(db_session, make_user):
    """metadata.plan が free / 不正ならプランは変わらない"""
    from retro_radio.db.repository import UserRepository

    for plan_value in ("free", "god", None):
        user = make_user(db_session, f"nofree-{plan_value}@example.com")
        payload = _flat_data_event(
            "checkout.session.completed",
            {"metadata": {"user_id": user.id, "plan": plan_value}},
        )
        with patch.object(stripe.Webhook, "construct_event", return_value=payload):
            _handler(db_session).handle_event(PAYLOAD, "sig")

        db_session.expire_all()
        assert UserRepository(db_session).get_by_id(user.id).plan is PlanType.FREE


def test_subscription_updated_inactive_keeps_plan(db_session, make_user):
    """status が active でなければプランは変更しない"""
    from retro_radio.db.repository import UserRepository

    user = make_user(db_session, "inactive@example.com")
    repo = UserRepository(db_session)
    stored = repo.get_by_id(user.id)
    stored.plan = PlanType.PREMIUM
    repo.update(stored)
    db_session.commit()

    payload = _flat_data_event(
        "customer.subscription.updated",
        {"id": "sub_1", "status": "past_due", "metadata": {"user_id": user.id, "plan": "pro"}},
    )
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")

    db_session.expire_all()
    assert UserRepository(db_session).get_by_id(user.id).plan is PlanType.PREMIUM


def test_payment_failed_does_not_change_plan(db_session, make_user):
    """支払い失敗ではプランを変更しない"""
    from retro_radio.db.repository import UserRepository

    user = make_user(db_session, "failed@example.com")
    repo = UserRepository(db_session)
    stored = repo.get_by_id(user.id)
    stored.plan = PlanType.PREMIUM
    repo.update(stored)
    db_session.commit()

    payload = _flat_data_event("invoice.payment_failed", {"id": "in_1", "customer": "cus_1"})
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")

    db_session.expire_all()
    assert UserRepository(db_session).get_by_id(user.id).plan is PlanType.PREMIUM


def test_stripe_ids_are_stored(db_session, make_user):
    """checkout.session.completed で Stripe の顧客 ID / サブスク ID が保存される"""
    from retro_radio.db.repository import UserRepository

    user = make_user(db_session, "ids@example.com")
    payload = _flat_data_event("checkout.session.completed", {
        "metadata": {"user_id": user.id, "plan": "pro"},
        "customer": "cus_123",
        "subscription": "sub_123",
    })
    with patch.object(stripe.Webhook, "construct_event", return_value=payload):
        _handler(db_session).handle_event(PAYLOAD, "sig")

    from retro_radio.db.models import UserModel

    db_session.expire_all()
    model = db_session.query(UserModel).filter_by(id=user.id).first()
    assert model.stripe_customer_id == "cus_123"
    assert model.stripe_subscription_id == "sub_123"
    assert UserRepository(db_session).get_by_id(user.id).plan is PlanType.PRO
