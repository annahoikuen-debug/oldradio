import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple
import stripe
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..config import get_settings
from ..db.repository import StripeWebhookEventRepository, UserRepository
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


def _as_unix_seconds(value: Any) -> Optional[int]:
    """Stripe の `created`（Unix 秒）を int へ。取れないときは None。

    ``None`` は「順序を判定できない」を意味し、呼び出し側は
    遅延配送チェックを**無効**にする（適用してよい側に倒す）。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
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

        event_id = str(_field(event, "id") or "")
        event_type = _field(event, "type")
        # Stripe の `created`（Unix 秒）順序判定に使う。無いとき None。
        event_created = _as_unix_seconds(_field(event, "created"))
        logger.info("Stripe event received id=%s type=%s", event_id, event_type)

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

        obj = self._event_object(event)
        with self._repo() as repo:
            ledger = StripeWebhookEventRepository(repo.db)
            # --- 冪等化（M-7）-----------------------------------------------
            # Stripe は 2xx を返すまで同じイベントを**再送**する。さらに
            # 並行配送では**古いイベントが後から届く**ことがある（out-of-order）。
            # 再送は無視し、遅れて届いた古いイベントは適用しない:
            #   解約で FREE になった利用者に、遅れた active イベントが
            #   プレミアムを**勝手に戻す**のを防ぐ。
            if ledger.already_processed(event_id):
                logger.info(
                    "Stripe event already processed (duplicate delivery): id=%s type=%s",
                    event_id,
                    event_type,
                )
                return
            user_id = self._pre_resolve_user_id(obj, repo)
            if ledger.newer_event_exists(user_id, event_created):
                logger.warning(
                    "Discarding out-of-order Stripe event: id=%s type=%s "
                    "created=%s user_id=%s（より新しいイベントを処理済み）",
                    event_id,
                    event_type,
                    event_created,
                    user_id,
                )
                ledger.record(event_id, event_type, user_id, event_created)
                return

            try:
                handler(obj, repo)
            except IntegrityError:
                # 並行配送で**別のリクエストが先に記録した**（PK 衝突）。
                # こちらは二重適用していないので、再送として無視して 200 を返す。
                # flush が失敗したセッションはここで**必ず rollback** する
                # （放っておくと直後の commit が失敗し、Stripe に 500 を返して
                # 無限に再送させる）。
                repo.db.rollback()
                logger.warning(
                    "Stripe event was processed concurrently (treated as duplicate): "
                    "id=%s type=%s",
                    event_id,
                    event_type,
                )
                return
            # 処理済みの記録は**適用と同じトランザクション**に入れる。
            # 適用後に別トランザクションで記録すると、その間のクラッシュで
            # 記録だけが失われ、再送がもう 1 回適用される。
            ledger.record(event_id, event_type, user_id, event_created)
        self._commit()

    def _pre_resolve_user_id(
        self, obj: Dict[str, Any], repo: UserRepository
    ) -> Optional[str]:
        """**適用の前**に `user_id` を見つける（順序判定・台帳記録用）。

        `_resolve_user_id` と同じ手順（`metadata.user_id` → `customer` 逆引き）だが、
        こちらはハンドラごとには走らせない。イベントが既に処理済み／
        古い配送だったときにも台帳に `user_id` を残したい
        （次に届く新しいイベントとの比較材料になる）ため。
        """
        metadata = _field(obj, "metadata") or {}
        user_id = _field(metadata, "user_id")
        if user_id:
            return str(user_id)
        customer_id = _field(obj, "customer")
        if not customer_id:
            return None
        user = repo.find_by_stripe_customer(customer_id)
        return user.id if user is not None else None

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
        user_id = self._resolve_user_id(subscription, repo)
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

        subscription_metadata = _field(subscription, "metadata") or {}
        plan = coerce_plan(_field(subscription_metadata, "plan"))
        user = repo.get_by_id(user_id)
        if plan is None or plan == PlanType.FREE or user is None:
            logger.warning(
                "customer.subscription.updated ignored (user_id=%s plan=%r status=%s)",
                user_id,
                _field(subscription_metadata, "plan"),
                status,
            )
            return

        user.plan = plan
        repo.update(user)
        repo.update_stripe_ids(user_id, subscription_id=_field(subscription, "id"))
        logger.info("customer.subscription.updated: user_id=%s plan=%s", user_id, plan.value)

    def _resolve_user_id(
        self, obj: Dict[str, Any], repo: UserRepository
    ) -> Optional[str]:
        """subscription から `user_id` を確定する（DB-02）。

        `metadata.user_id` を正とし、無いとき **`customer_id` で逆引き**する
        （Billing Portal / ダッシュボードからの解約では metadata に user_id が
        無いため、逆引きが唯一の手がかりになる）。どちらも無ければ ``None``。
        """
        metadata = _field(obj, "metadata") or {}
        user_id = _field(metadata, "user_id")
        if user_id:
            return user_id
        customer_id = _field(obj, "customer")
        user = repo.find_by_stripe_customer(customer_id)
        if user is not None:
            logger.info(
                "metadata.user_id が無いため customer_id で逆引きしました: customer=%s user_id=%s",
                customer_id,
                user.id,
            )
            return user.id
        return None

    def _handle_subscription_deleted(
        self, subscription: Dict[str, Any], repo: UserRepository
    ) -> None:
        user_id = self._resolve_user_id(subscription, repo)
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
