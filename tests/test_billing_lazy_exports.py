"""`retro_radio.billing` の遅延エクスポート（PEP 562 `__getattr__`）のテスト。

`billing/__init__.py` は `stripe` や `webhook` を起動時に読み込まないように
遅延公開している。 遅延ロードが壊れると、Stripe 未設定の環境でも
インポート時に巨大なライブラリを引くservicing いきCatch-all になる。
ここでは「遅延性」と「エラー契約」の両方を固定する。
"""

import importlib
import subprocess
import sys

import pytest

import retro_radio.billing as billing


@pytest.fixture(autouse=True)
def _clear_memo():
    """`__getattr__` は結果を `globals()` にメモ化するため、実行間で持ち越さない。"""
    for name in ("StripeClient", "WebhookHandler", "BillingManager"):
        billing.__dict__.pop(name, None)
    try:
        yield
    finally:
        for name in ("StripeClient", "WebhookHandler", "BillingManager"):
            billing.__dict__.pop(name, None)


# --- 遅延ロードが正しく解決されること -------------------------------------------
@pytest.mark.parametrize(
    ("name", "module_name"),
    [
        ("StripeClient", "retro_radio.billing.stripe_client"),
        ("WebhookHandler", "retro_radio.billing.webhook"),
        ("BillingManager", "retro_radio.billing.billing_manager"),
    ],
)
def test_lazy_attribute_resolves_to_real_class(name, module_name):
    module = importlib.import_module(module_name)

    assert getattr(billing, name) is getattr(module, name)


@pytest.mark.parametrize("name", ["StripeClient", "WebhookHandler", "BillingManager"])
def test_lazy_attribute_is_memoized_in_module_globals(name):
    first = getattr(billing, name)

    assert billing.__dict__[name] is first
    assert getattr(billing, name) is first


def test_eager_exports_are_always_available():
    """遅延対象でないPlanType / BillingCycle / get_limits は即時公開。"""
    from retro_radio.billing import BillingCycle, PlanType, get_limits

    assert PlanType is not None
    assert BillingCycle is not None
    assert callable(get_limits)


# --- エラー契約 -----------------------------------------------------------------
def test_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError) as exc:
        billing.NoSuchThing

    assert "NoSuchThing" in str(exc.value)
    assert "retro_radio.billing" in str(exc.value)


def test_dunder_attribute_also_raises_attribute_error():
    """`__getattr__` が dunder を横取りしないこと（copy/pickle 等が壊れる）。"""
    with pytest.raises(AttributeError):
        billing.__deepcopy__


# --- __dir__ -------------------------------------------------------------------
def test_dir_lists_eager_and_lazy_exports():
    listing = dir(billing)

    for eager in ("PlanType", "BillingCycle", "get_limits"):
        assert eager in listing
    for lazy in ("StripeClient", "WebhookHandler", "BillingManager"):
        assert lazy in listing


def test_dir_is_sorted_and_deduplicated():
    listing = dir(billing)

    assert listing == sorted(listing)
    assert len(listing) == len(set(listing))


# --- 遅延性（本当に lazy なのか） ------------------------------------------------
# 遅延対象は StripeClient / WebhookHandler / BillingManager の3つ。
# `plans` は `__init__` が eager に import する（PlanType の提供に必要なため）。
_LAZY_MODULE_NAMES = (
    "retro_radio.billing.stripe_client",
    "retro_radio.billing.webhook",
    "retro_radio.billing.billing_manager",
)


def test_importing_package_does_not_import_stripe_dependents():
    """パッケージ import だけでは遅延対象3つが読み込まれないこと。

    これらが import 時に展開されると、Stripe を触らないmere 開発環境でも
    起動コストと不要依存の load 増える。
    """
    code = (
        "import sys, retro_radio.billing as b;"
        f"lazy = {list(_LAZY_MODULE_NAMES)!r};"
        "print([m for m in lazy if m in sys.modules])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = eval(result.stdout.strip())

    assert loaded == [], f"遅延対象が import 時に読み込まれている: {loaded}"


def test_attribute_access_triggers_the_import():
    code = (
        "import sys, retro_radio.billing as b;"
        "_ = b.StripeClient;"
        "print('retro_radio.billing.stripe_client' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "True"
