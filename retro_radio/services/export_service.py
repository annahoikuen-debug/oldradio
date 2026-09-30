import csv
import io
import logging
from datetime import datetime
from typing import List, Dict
from retro_radio.db.session import get_db_sync
from retro_radio.db.repository import GenerationRepository
from ..utils.errors import handle_error

logger = logging.getLogger(__name__)

# UTF-8 BOM (Byte Order Mark)
UTF8_BOM = '\ufeff'

# 1回のエクスポートで書き出す最大件数（超えた分は切り詰められる）
EXPORT_ROW_LIMIT = 1000

class ExportService:
    def __init__(self):
        self.db = get_db_sync()
        self.gen_repo = GenerationRepository(self.db)
        # キャッシュを追加（パフォーマンス最適化）
        self._cache = {}
        self._cache_timeout = 300  # 5分

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def export_generations_csv(self, user_id: str, limit: int = EXPORT_ROW_LIMIT) -> str:
        """ユーザーの生成履歴をCSV形式でエクスポート（UTF-8 BOM付き）

        出力は最新 limit 件。limit 件以上の履歴がある場合は切り詰められるため、
        全件が必要なら呼び出し側で十分大きい limit を指定すること。
        """
        try:
            # キャッシュチェック
            now = datetime.now().timestamp()
            cached = self._cache.get(user_id)
            if cached is not None and cached[2] == limit:
                cached_data, timestamp = cached[0], cached[1]
                if now - timestamp < self._cache_timeout:
                    return cached_data

            generations = self.gen_repo.get_by_user(user_id, limit=limit)
            if len(generations) >= limit:
                logger.warning(f"履歴が上限（{limit}件）に達したため、古い履歴はエクスポートに含まれません: user_id={user_id}")

            output = io.StringIO()
            writer = csv.writer(output, quoting=csv.QUOTE_ALL)

            # ヘッダー
            writer.writerow([
                "日付", "年", "月", "日", "脚本", "曲タイトル", "アーティスト",
                "プレビューURL", "生成時刻"
            ])

            # データ行
            for gen in generations:
                created_at = gen.get('created_at')
                if hasattr(created_at, 'strftime'):
                    created_at_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    created_at_str = str(created_at) if created_at else ""

                date_str = f"{gen['month']}月{gen['day']}日"
                writer.writerow([
                    date_str,
                    str(gen['year']),
                    str(gen['month']),
                    str(gen['day']),
                    gen['script'][:100] + "..." if len(gen['script']) > 100 else gen['script'],
                    gen['song_title'],
                    gen['artist_name'],
                    gen['preview_url'] or "",
                    created_at_str
                ])

            result = UTF8_BOM + output.getvalue()
            # キャッシュに保存
            self._cache[user_id] = (result, now, limit)
            return result
        except Exception as e:
            logger.error(f"CSVエクスポート失敗: {e}")
            handle_error(e, "Export")
            # エラー時に空のCSVを返すか、または空文字列を返す
            # ここではヘッダーのみのCSVを返す
            output = io.StringIO()
            writer = csv.writer(output, quoting=csv.QUOTE_ALL)
            writer.writerow([
                "日付", "年", "月", "日", "脚本", "曲タイトル", "アーティスト",
                "プレビューURL", "生成時刻"
            ])
            return UTF8_BOM + output.getvalue()

    def close(self):
        self.db.close()
        self._cache.clear()
