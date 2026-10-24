import logging
from typing import Any, Optional
import stripe
from ..config import get_settings
from .plans import BillingCycle, PlanType, get_price_id

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30

_STRIPE_ERRORS = tuple(
    err
    for err in (
        getattr(stripe, "StripeError", None),
        getattr(getattr(stripe, "error", None), "StripeError", None),
    )
    if isinstance(err, type) and issubclass(err, BaseException)
)


class StripeClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.api_key: Optional[str] = (settings.stripe_secret_key or "").strip() or None
        self.test_mode = bool(self.api_key) and self.api_key.startswith("sk_test")
        self.webhook_secret: Optional[str] = (settings.stripe_webhook_secret or "").strip() or None
        self.app_url = (settings.app_url or "").rstrip("/")
        if self.api_key:
            stripe.api_key = self.api_key
        else:
            logger.warning("Stripe secret key is not configured; Stripe API calls are disabled")

    @property
    def is_configured(self) -> bool:
        return self.api_key is not None

    def _list_customers(self, email: str) -> list:
        """メールアドレスで既存の Stripe Customer をリストする（スタブ化可能）。

        テストでこのメソッドをモックすることで、実際の Stripe API を叩かずに
        「ユーザー別 customer の再利用・新規作成」のロジックだけを検証する。
        """
        customers = stripe.Customer.list(email=email, limit=100, timeout=DEFAULT_TIMEOUT)
        return customers.data or []

    def _create_customer(self, email: str, user_id: str) -> Any:
        """新規 Stripe Customer を作成する（スタブ化可能）。"""
        return stripe.Customer.create(
            email=email,
            metadata={"user_id": user_id},
            timeout=DEFAULT_TIMEOUT,
        )

    def create_checkout_session(
        self, user_id: str, email: str, plan: PlanType, cycle: BillingCycle
    ) -> Optional[str]:
        if not self.is_configured:
            return None

        price_id = get_price_id(plan, cycle)
        if not price_id:
            logger.error("No Stripe price ID configured for plan=%s cycle=%s", plan, cycle)
            return None

        try:
            # メールアドレス一致だけで customer を再利用すると、
            # 別ユーザーが同じメール（またはメールの表記ゆれ）を持ったときに
            # 2 つのアカウントが 1 つの Stripe Customer を共有し、
            # 解約・プラン変更が他人に波及する（P1-4）。
            # metadata.user_id で現在の利用者に属する customer を探し、
            # 無ければ新規作成する。
            customer = None
            for c in self._list_customers(email):
                c_metadata = c.get("metadata") if isinstance(c, dict) else getattr(c, "metadata", None) or {}
                if c_metadata.get("user_id") == user_id:
                    customer = c
                    break
            if customer is None:
                customer = self._create_customer(email, user_id)

            session = stripe.checkout.Session.create(
                customer=customer.id,
                payment_method_types=["card"],
                line_items=[{"price": price_id, "quantity": 1}],
                mode="subscription",
                success_url=f"{self.app_url}/?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{self.app_url}/pricing",
                metadata={
                    "user_id": user_id,
                    "plan": plan.value if isinstance(plan, PlanType) else str(plan),
                    "cycle": cycle.value,
                },
                timeout=DEFAULT_TIMEOUT,
            )
        except _STRIPE_ERRORS:
            logger.exception("Failed to create Stripe checkout session for user_id=%s", user_id)
            raise

        logger.info(
            "Stripe checkout session created user_id=%s plan=%s cycle=%s", user_id, plan, cycle
        )
        return session.url

    def create_customer_portal_session(self, customer_id: str) -> Optional[str]:
        if not self.is_configured:
            return None
        if not customer_id:
            logger.error("Cannot create Stripe customer portal session without a customer ID")
            return None

        try:
            session = stripe.billing_portal.Session.create(
                customer=customer_id,
                return_url=f"{self.app_url}/account",
                timeout=DEFAULT_TIMEOUT,
            )
        except _STRIPE_ERRORS:
            logger.exception("Failed to create Stripe portal session for customer=%s", customer_id)
            raise

        return session.url
