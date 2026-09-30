import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from ..db.models import utcnow
from ..db.repository import UserRepository
from ..db.session import get_db
from ..models.user import User
from .plans import UNLIMITED, get_limits

logger = logging.getLogger(__name__)


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class BillingManager:
    def __init__(self, db: Optional[Session] = None):
        self.db = db

    @contextmanager
    def _repo(self):
        """1操作分のリポジトリを開く。DBセッションは操作単位に作り、必ず解放する"""
        if self.db is not None:
            yield UserRepository(self.db)
            return
        with get_db() as db:
            yield UserRepository(db)

    def _commit(self) -> None:
        """外部セッションの利用時は自前でコミットする（get_db() 利用時は自動コミット）"""
        if self.db is not None:
            self.db.commit()

    def _reset_monthly_counter_if_due(self, user: User) -> None:
        """月次期間が過ぎていれば利用回数を0に戻し、次の30日間の起点を設定する。
        生成回数の制限が撤廃されていても `generation_count` /
        `generation_reset_at` は統計として使われ続けるため、上限判定から
        独立した処理として必ず実行する（早期 return で落とさない）。
        """
        if not user.generation_reset_at:
            return
        now = utcnow()
        if now < _as_naive_utc(user.generation_reset_at):
            return

        user.generation_count = 0
        user.generation_reset_at = now + timedelta(days=30)
        try:
            with self._repo() as repo:
                repo.update(user)
        except ValueError:
            # まだ永続化されていない（呼び出し側が保持している）ドメインユーザーの場合。
            # メモリ上の統計は正しく更新済みなので、上限判定は止めない。
            logger.debug("Monthly counter reset for unsaved user_id=%s", user.id)
        except SQLAlchemyError as e:
            # 統計のリセット失敗で生成を止めない（ユーザーIDは例外文字列に出さない）
            logger.warning("Monthly counter reset could not be persisted: %s", type(e).__name__)
        else:
            self._commit()
            logger.info("Monthly generation counter reset for user_id=%s", user.id)

    def check_generation_limit(self, user: User) -> Tuple[bool, int]:
        """(許可されるか, 残り回数) を返す。統計のリセットは上限判定と別に実行する"""
        self._reset_monthly_counter_if_due(user)

        limits = get_limits(user.plan)
        monthly_limit = limits.get("monthly_generations")

        if monthly_limit is None or monthly_limit == UNLIMITED:
            return True, UNLIMITED

        used = max(0, user.generation_count or 0)
        return used < monthly_limit, max(0, monthly_limit - used)
