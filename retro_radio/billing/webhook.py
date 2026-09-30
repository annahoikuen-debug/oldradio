import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple
import stripe
from sqlalchemy.orm import Session
from ..config import get_settings
from ..db.repository import UserRepository
from ..db.session import get_db
from ..models.user import PlanType
from .plans import coerce_plan

logger = logging.getLogger(__name__)

_SIGNATURE_ERRORS = tuple(
    dict.fromkeys(
        err
        for err in (
            getattr(stripe, "SignatureVerificationError", None),
            getattr(getattr(stripe, "error", None), "SignatureVerificationError", None),
        )
        if isinstance(err, type) and issubclass(err, BaseException)
    )
)

WEBHOOK_VERIFICATION_ERRORS: Tuple[type, ...] = (ValueError,) + _SIGNATURE_ERRORS


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    try:
        return obj[name]
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


class WebhookHandler:
    def __init__(self, db: Optional[Session] = None):
        self.db = db
        settings = get_settings()
        self.webhook_secret: Optional[str] = (
            settings.stripe_webhook_secret or ""
        ).strip() or None

    @contextmanager
    def _repo(self):
        """1イベント分のリポジトリを開く。DBセッションはイベント単位に作り、必ず解放する"""
        if self.db is not None:
            yield UserRepository(self.db)
            return
        with get_db() as db:
            yield UserRepository(db)

    def _commit(self) -> None:
        """外部セッションの利用時は自前でコミットする（get_db() 利用時は自動コミット）"""
        if self.db is not None:
            self.db.commit()

    def handle_event(self, payload: bytes, sig_header: str) -> None:
        if not self.webhook_secret:
            raise RuntimeError("Stripe webhook secret is not configured")

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, self.webhook_secret
            )
        except WEBHOOK_VERIFICATION_ERRORS:
            logger.warning("Rejected Stripe webhook: signature/payload verification failed")
            raise

        event_type = _field(event, "type")
        logger.info("Stripe event received id=%s type=%s", _field(event, "id"), event_type)

        handlers = {
            "checkout.session.completed": self._handle_checkout_session_completed,
            "customer.subscription.updated": self._handle_subscription_updated,
            "customer.subscription.deleted": self._handle_subscription_deleted,
            "invoice.payment_failed": self._handle_payment_failed,
        }
        handler = handlers.get(event_type)
        if handler is None:
            logger.debug("Ignoring unhandled Stripe event type=%s", event_type)
            return

        with self._repo() as repo:
            handler(self._event_object(event), repo)
        self._commit()

    @staticmethod
    def _event_object(event: Any) -> Dict[str, Any]:
        """実イベント構造 `data.object` から対象オブジェクトを取り出す。

        Stripe が送るイベントは必ず `{"type": ..., "data": {"object": {...}}}` であり、
        `data` 直下に会话情報（metadata 等）は無い。`data` をそのまま渡すと
        プラン付与・降格が一切行われなくなる。
        """
        data = _field(event, "data")
        if data is None:
            return {}
        obj = _field(data, "object")
        if obj is None:
            # 想定外の形（data 直下に会话情報がある）でも判定を諦めない
            return data if hasattr(data, "keys") else {}
        return obj

    def _handle_checkout_session_completed(
        self, session: Dict[str, Any], repo: UserRepository
    ) -> None:
        metadata = _field(session, "metadata") or {}
        user_id = _field(metadata, "user_id")
        if not user_id:
            logger.warning("checkout.session.completed without metadata.user_id")
            return

        user = repo.get_by_id(user_id)
        if user is None:
            logger.warning("checkout.session.completed for unknown user_id=%s", user_id)
            return

        plan = coerce_plan(_field(metadata, "plan"))
        if plan is None or plan == PlanType.FREE:
            logger.warning(
                "checkout.session.completed with unusable plan=%r for user_id=%s",
                _field(metadata, "plan"),
                user_id,
            )
        else:
            user.plan = plan
            repo.update(user)
            logger.info("checkout.session.completed: user_id=%s plan=%s", user_id, plan.value)

        repo.update_stripe_ids(
            user_id,
            customer_id=_field(session, "customer"),
            subscription_id=_field(session, "subscription"),
        )

    def _handle_subscription_updated(
        self, subscription: Dict[str, Any], repo: UserRepository
    ) -> None:
        metadata = _field(subscription, "metadata") or {}
        user_id = _field(metadata, "user_id")
        status = _field(subscription, "status")
        if not user_id:
            logger.warning("customer.subscription.updated without metadata.user_id")
            return
        if status != "active":
            logger.info(
                "customer.subscription.updated status=%s for user_id=%s; plan unchanged",
                status,
                user_id,
            )
            return

        plan = coerce_plan(_field(metadata, "plan"))
        user = repo.get_by_id(user_id)
        if plan is None or plan == PlanType.FREE or user is None:
            logger.warning(
                "customer.subscription.updated ignored (user_id=%s plan=%r status=%s)",
                user_id,
                _field(metadata, "plan"),
                status,
            )
            return

        user.plan = plan
        repo.update(user)
        repo.update_stripe_ids(user_id, subscription_id=_field(subscription, "id"))
        logger.info("customer.subscription.updated: user_id=%s plan=%s", user_id, plan.value)

    def _handle_subscription_deleted(
        self, subscription: Dict[str, Any], repo: UserRepository
    ) -> None:
        metadata = _field(subscription, "metadata") or {}
        user_id = _field(metadata, "user_id")
        if not user_id:
            logger.warning("customer.subscription.deleted without metadata.user_id")
            return

        user = repo.get_by_id(user_id)
        if user is None:
            logger.warning("customer.subscription.deleted for unknown user_id=%s", user_id)
            return

        user.plan = PlanType.FREE
        repo.update(user)
        # update_stripe_ids は空値を無視するため stripe_subscription_id は残る
        logger.info("customer.subscription.deleted: user_id=%s downgraded to FREE", user_id)

    def _handle_payment_failed(
        self, invoice: Dict[str, Any], repo: UserRepository
    ) -> None:
        logger.error(
            "Stripe invoice.payment_failed id=%s customer=%s",
            _field(invoice, "id"),
            _field(invoice, "customer"),
        )
