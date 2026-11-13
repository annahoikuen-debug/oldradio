import logging
from contextlib import contextmanager
from typing import Optional

from sqlalchemy.orm import Session

from ..db.repository import UserRepository
from ..db.session import get_db

logger = logging.getLogger(__name__)


class BillingManager:
    """課金まわりの DB 操作を束ねる入口（webhook の台帳・プラン更新等）。

    月次生成回数制限（`check_generation_limit` / `_reset_monthly_counter_if_due`）
    は**撤廃済み**のため削除した。生成回数制限は README で撤廃が宣言されており、
    `authenticator.check_can_generate` が常に `(True, -1)` を返すのと矛盾する
    二重定義になっていた（加算経路が死蔵コードで 429 経路も常時通過する死コード
    だったこと、`/api/jobs` にチェックが無く迂回可能だったことも含めて
    コードレビュー H-6 / prev M-2）。
    """

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
