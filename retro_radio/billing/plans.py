import logging
from enum import Enum
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple
from ..config import get_settings
from ..models.user import PlanType

logger = logging.getLogger(__name__)

UNLIMITED = -1


class BillingCycle(str, Enum):
    MONTHLY = "monthly"
    YEARLY = "yearly"


@lru_cache(maxsize=1)
def get_price_ids() -> Dict[Tuple[PlanType, BillingCycle], str]:
    settings = get_settings()
    return {
        (PlanType.PREMIUM, BillingCycle.MONTHLY): settings.stripe_price_premium_monthly,
        (PlanType.PREMIUM, BillingCycle.YEARLY): settings.stripe_price_premium_yearly,
        (PlanType.PRO, BillingCycle.MONTHLY): settings.stripe_price_pro_monthly,
        (PlanType.PRO, BillingCycle.YEARLY): settings.stripe_price_pro_yearly,
    }


PLAN_LIMITS: Dict[PlanType, Dict[str, Any]] = {
    PlanType.FREE: {
        "monthly_generations": UNLIMITED,
        "audio_quality": "standard",
        "history_export": False,
        "favorites": False,
        "api_access": False,
        "batch_generation": False,
    },
    PlanType.PREMIUM: {
        "monthly_generations": UNLIMITED,
        "audio_quality": "high",
        "history_export": True,
        "favorites": True,
        "api_access": False,
        "batch_generation": False,
    },
    PlanType.PRO: {
        "monthly_generations": UNLIMITED,
        "audio_quality": "premium",
        "history_export": True,
        "favorites": True,
        "api_access": True,
        "batch_generation": True,
    },
}


def coerce_plan(value: Any) -> Optional[PlanType]:
    if isinstance(value, PlanType):
        return value
    if value is None:
        return None
    try:
        return PlanType(value)
    except (TypeError, ValueError):
        logger.warning("Unknown plan value: %r", value)
        return None


def get_limits(plan: PlanType) -> Dict[str, Any]:
    key = coerce_plan(plan) or PlanType.FREE
    return dict(PLAN_LIMITS[key])


def get_price_id(plan: PlanType, cycle: BillingCycle) -> Optional[str]:
    key = coerce_plan(plan)
    if key is None:
        return None
    return get_price_ids().get((key, cycle))
