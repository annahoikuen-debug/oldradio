from importlib import import_module
from .plans import BillingCycle, PlanType, get_limits

__all__ = ["PlanType", "BillingCycle", "get_limits"]

_LAZY_EXPORTS = {
    "StripeClient": ".stripe_client",
    "WebhookHandler": ".webhook",
    "BillingManager": ".billing_manager",
}


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
