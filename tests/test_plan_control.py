"""`retro_radio.app.plan_control` のテスト。

PlanController は認証済みユーザーに対して「そのプランで使える機能か」と
「生成可能か」を裁决するゲート。 UI 側の表示制御がここを信頼している関係で、
未ログイン時の既定値（基本機能のみ）とプラン別の機能マッピングは
セキュリティ上の境界線にあたるため、値を明示して固定する。
"""

from unittest.mock import MagicMock

import pytest

from retro_radio.app.plan_control import PlanController
from retro_radio.billing import BillingCycle, PlanType


@pytest.fixture
def controller_with(monkeypatch):
    """認証済みユーザーを差し込んだ PlanController を返すヘルパー。"""

    def _make(user):
        auth = MagicMock(name="Authenticator")
        if user is None:
            auth.get_current_user.return_value = None
        else:
            auth.get_current_user.return_value = user
        monkeypatch.setattr("retro_radio.app.plan_control.Authenticator", lambda: auth)
        controller = PlanController()
        controller.auth = auth
        return controller, auth

    return _make


@pytest.fixture
def user_factory():
    def _make(plan):
        user = MagicMock(name="User")
        user.id = "u1"
        user.plan = plan
        return user

    return _make


# --- get_current_user ---------------------------------------------------------
def test_get_current_user_delegates_to_authenticator(controller_with):
    user = MagicMock(name="User")
    controller, _auth = controller_with(user)

    assert controller.get_current_user() is user


def test_get_current_user_returns_none_when_anonymous(controller_with):
    controller, _auth = controller_with(None)

    assert controller.get_current_user() is None


# --- can_generate -------------------------------------------------------------
def test_can_generate_is_false_for_anonymous(controller_with):
    controller, auth = controller_with(None)

    assert controller.can_generate() == (False, 0)
    auth.check_can_generate.assert_not_called()


def test_can_generate_returns_authenticator_verdict(controller_with):
    user = MagicMock(name="User")
    controller, auth = controller_with(user)
    auth.check_can_generate.return_value = (True, 5)

    assert controller.can_generate() == (True, 5)
    auth.check_can_generate.assert_called_once_with(user)


def test_can_generate_returns_zero_remaining_for_blocked_user(controller_with):
    user = MagicMock(name="User")
    controller, auth = controller_with(user)
    auth.check_can_generate.return_value = (False, 0)

    assert controller.can_generate() == (False, 0)


# --- increment_generation -----------------------------------------------------
def test_increment_generation_increments_authenticated_user(controller_with):
    user = MagicMock(name="User")
    controller, auth = controller_with(user)

    controller.increment_generation()

    auth.increment_generation_count.assert_called_once_with(user)


def test_increment_generation_is_noop_for_anonymous(controller_with):
    controller, auth = controller_with(None)

    controller.increment_generation()  # 例外を投げないこと

    auth.increment_generation_count.assert_not_called()


# --- get_plan ----------------------------------------------------------------
def test_get_plan_returns_free_for_anonymous(controller_with):
    controller, _auth = controller_with(None)

    assert controller.get_plan() is PlanType.FREE


def test_get_plan_returns_users_actual_plan(controller_with, user_factory):
    for plan in (PlanType.FREE, PlanType.PREMIUM, PlanType.PRO):
        controller, auth = controller_with(user_factory(plan))
        auth.get_user_plan.return_value = plan

        assert controller.get_plan() is plan
        auth.get_user_plan.assert_called_once()


# --- get_upgrade_url ---------------------------------------------------------
def test_get_upgrade_url_returns_checkout_url(controller_with):
    user = MagicMock(name="User")
    controller, auth = controller_with(user)
    auth.upgrade_to_premium.return_value = "https://checkout.stripe.com/session"

    url = controller.get_upgrade_url(PlanType.PREMIUM, BillingCycle.YEARLY)

    assert url == "https://checkout.stripe.com/session"
    auth.upgrade_to_premium.assert_called_once_with(
        user, PlanType.PREMIUM, BillingCycle.YEARLY
    )


def test_get_upgrade_url_defaults_to_monthly_cycle(controller_with):
    user = MagicMock(name="User")
    controller, auth = controller_with(user)
    auth.upgrade_to_premium.return_value = "https://checkout.stripe.com/s"

    controller.get_upgrade_url(PlanType.PRO)

    auth.upgrade_to_premium.assert_called_once_with(
        user, PlanType.PRO, BillingCycle.MONTHLY
    )


def test_get_upgrade_url_requires_login(controller_with):
    controller, auth = controller_with(None)

    with pytest.raises(ValueError, match="User not logged in"):
        controller.get_upgrade_url(PlanType.PREMIUM)

    auth.upgrade_to_premium.assert_not_called()


# --- is_feature_enabled: 未ログイン時 ------------------------------------------
@pytest.mark.parametrize("feature", ["script_display", "audio_playback"])
def test_basic_features_available_without_login(controller_with, feature):
    """未ログインでも原稿表示と音声再生は通してよい契約。

    意図的に `unlimited_generations` はここには入っていない。
    認証は `can_generate` 側で行うもので、未ログイン時の許可リストは
    「表示・再生」2機能に限定されている（fail-closed な設計）。
    """
    controller, _auth = controller_with(None)

    assert controller.is_feature_enabled(feature) is True


@pytest.mark.parametrize(
    "feature",
    ["high_quality_audio", "history_export", "favorites", "api_access",
     "batch_generation", "no_ads", "unlimited_generations", "unknown_feature"],
)
def test_paid_features_blocked_without_login(controller_with, feature):
    """未ログイン時にプレミアム機能が漏れてはならない。"""
    controller, _auth = controller_with(None)

    assert controller.is_feature_enabled(feature) is False


# --- is_feature_enabled: プラン別マッピング ------------------------------------
@pytest.mark.parametrize("plan", [PlanType.FREE, PlanType.PREMIUM, PlanType.PRO])
def test_universal_features_enabled_for_every_plan(
    controller_with, user_factory, plan
):
    controller, auth = controller_with(user_factory(plan))
    auth.get_user_plan.return_value = plan

    assert controller.is_feature_enabled("script_display") is True
    assert controller.is_feature_enabled("audio_playback") is True
    assert controller.is_feature_enabled("unlimited_generations") is True


@pytest.mark.parametrize(
    ("feature", "expected_plans"),
    [
        ("high_quality_audio", {PlanType.PREMIUM, PlanType.PRO}),
        ("history_export", {PlanType.PREMIUM, PlanType.PRO}),
        ("favorites", {PlanType.PREMIUM, PlanType.PRO}),
        ("no_ads", {PlanType.PREMIUM, PlanType.PRO}),
        ("api_access", {PlanType.PRO}),
        ("batch_generation", {PlanType.PRO}),
    ],
)
def test_paid_feature_requires_the_right_plan(
    controller_with, user_factory, feature, expected_plans
):
    for plan in PlanType:
        controller, auth = controller_with(user_factory(plan))
        auth.get_user_plan.return_value = plan

        expected = plan in expected_plans
        assert controller.is_feature_enabled(feature) is expected, (
            f"{plan.value} で {feature} が {expected} になっていない"
        )


def test_unknown_feature_is_denied_for_paying_users(controller_with, user_factory):
    """未知的功能キーは課金ユーザーでも拒否（fail-closed）。"""
    controller, auth = controller_with(user_factory(PlanType.PRO))
    auth.get_user_plan.return_value = PlanType.PRO

    assert controller.is_feature_enabled("teleportation") is False


def test_is_feature_enabled_does_not_hit_db_for_anonymous(controller_with):
    """未ログイン時はプラン解決不要で即答すること。"""
    controller, auth = controller_with(None)

    controller.is_feature_enabled("history_export")

    auth.get_user_plan.assert_not_called()


# --- ilai Fees -----------------------------------------------------------------
def test_controller_builds_its_own_authenticator_by_default(monkeypatch):
    created = []

    class _Auth:
        def __init__(self):
            created.append(self)
            self.get_current_user = lambda: None

    monkeypatch.setattr("retro_radio.app.plan_control.Authenticator", _Auth)

    controller = PlanController()

    assert created == [controller.auth]
