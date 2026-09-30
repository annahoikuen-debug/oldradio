from retro_radio.db.session import get_db_sync
from retro_radio.db.repository import FavoriteRepository, GenerationRepository
from typing import List, Dict
import time
import logging
from ..utils.errors import handle_error

logger = logging.getLogger(__name__)

class FavoriteService:
    def __init__(self):
        self.db = get_db_sync()
        self.fav_repo = FavoriteRepository(self.db)
        self.gen_repo = GenerationRepository(self.db)
        # キャッシュを追加（パフォーマンス最適化）
        self._favorites_cache = {}
        self._cache_timeout = 300  # 5分
        self._last_cleanup = time.time()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def _cleanup_cache(self):
        """古いキャッシュエントリをキャッシュから削除"""
        now = time.time()
        if (now - self._last_cleanup) > 60:  # 1分に1回しかクリーンアップしない
            expired_keys = [
                key for key, (_, timestamp) in self._favorites_cache.items()
                if (now - timestamp) > self._cache_timeout
            ]
            for key in expired_keys:
                del self._favorites_cache[key]
            self._last_cleanup = now

    def _retry_db_operation(self, operation, max_retries=3, delay=1):
        """データベース操作をリトライする"""
        for attempt in range(max_retries):
            try:
                return operation()
            except Exception as e:
                if attempt == max_retries - 1:  # 最後の試行
                    logger.error(f"データベース操作が{max_retries}回失敗しました: {e}")
                    handle_error(e, "Favorite DB")
                    raise  # 最後の試行でも失敗したら例外を再送出
                else:
                    logger.warning(f"データベース操作が失敗しました（{attempt + 1}/{max_retries}）: {e}. {delay}秒後にリトライします。")
                    self._rollback()
                    time.sleep(delay)

    def _rollback(self):
        try:
            self.db.rollback()
        except Exception as e:
            logger.error(f"ロールバックに失敗しました: {e}")

    def add_favorite(self, user_id: str, generation_id: str) -> bool:
        def _add():
            result = self.fav_repo.add(user_id, generation_id)
            if result:
                self.db.commit()
                # キャッシュを無効化
                if user_id in self._favorites_cache:
                    del self._favorites_cache[user_id]
            return result

        try:
            return self._retry_db_operation(_add)
        except Exception as e:
            # リトライしても失敗した場合はFalseを返す
            logger.error(f"お気に入りの追加に失敗しました: {e}")
            return False

    def remove_favorite(self, user_id: str, generation_id: str) -> bool:
        def _remove():
            result = self.fav_repo.remove(user_id, generation_id)
            if result:
                self.db.commit()
                # キャッシュを無効化
                if user_id in self._favorites_cache:
                    del self._favorites_cache[user_id]
            return result

        try:
            return self._retry_db_operation(_remove)
        except Exception as e:
            logger.error(f"お気に入りの削除に失敗しました: {e}")
            return False

    def is_favorite(self, user_id: str, generation_id: str) -> bool:
        # ここはリアルタイムでチェックする必要があるためキャッシュしない
        def _is_fav():
            return self.fav_repo.is_favorite(user_id, generation_id)

        try:
            return self._retry_db_operation(_is_fav)
        except Exception as e:
            logger.error(f"お気に入りの判定に失敗しました: {e}")
            return False

    def get_favorites(self, user_id: str) -> List[Dict]:
        """お気に入りの生成履歴を取得"""
        def _get_favorites():
            # キャッシュクリーンアップ
            self._cleanup_cache()

            # キャッシュチェック
            now = time.time()
            if user_id in self._favorites_cache:
                favorites, timestamp = self._favorites_cache[user_id]
                if now - timestamp < self._cache_timeout:
                    return favorites

            fav_ids = self.fav_repo.get_user_favorites(user_id)
            favorites = []
            for fid in fav_ids:
                gen = self.gen_repo.get_by_id(fid)
                if gen:
                    favorites.append(gen)

            # キャッシュに保存
            self._favorites_cache[user_id] = (favorites, now)
            return favorites

        try:
            return self._retry_db_operation(_get_favorites)
        except Exception as e:
            # リトライしても失敗した場合は空リストを返す
            logger.error(f"お気に入りの取得に失敗しました: {e}")
            return []

    def close(self):
        self.db.close()
        self._favorites_cache.clear()
