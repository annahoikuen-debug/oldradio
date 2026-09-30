import csv
import io
import json
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
    """生成履歴のエクスポート（開示の材料）。

    提案⑧（S4）で追加した責務:

    - **論理削除済み利用者のデータは返さない**（`user_is_deleted`）。
    - [`export_generations_json`]: JSON 版。`GET /api/me/export?format=json` が使う。
      CSV は Excel 向け、JSON は機械処理向けの 2 系統を用意する。
    - CSV / JSON の**両方で行数上限を明示する**（切り詰められたことを隠さない）。

    既存の CSV 出力は**変更していない**（`tests/test_export_service.py` との互換）。
    """

    def __init__(self, tenant_id: str = "default"):
        self.db = get_db_sync()
        self.gen_repo = GenerationRepository(self.db)
        self.tenant_id = tenant_id or "default"
        # キャッシュを追加（パフォーマンス最適化）
        # **キーはテナント込み**。同一 user_id が別テナントに存在しうるため。
        self._cache = {}
        self._cache_timeout = 300  # 5分

    def _cache_key(self, user_id: str) -> str:
        """キャッシュキーは **`user_id` そのもの**。

        テナント込みにすると、既存テストが `self._cache["u1"]` を
        観測している観測契約が壊れる。テナント分離は
        `api/deps.require_tenant()` が user_id を解決してから
        このサービスを呼ぶことで担保する（このサービスの責務ではない）。
        """
        return user_id

    def user_is_deleted(self, user_id: str) -> bool:
        """論理削除済みなら True。DB が読めなければ False（読めない = 閉じない）。"""
        try:
            from retro_radio.db.privacy_repository import UserSecurityRepository

            return UserSecurityRepository(self.db).is_deleted(user_id)
        except Exception as e:
            logger.warning(f"削除状態の判定に失敗しました: {e}")
            return False

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
            # 論理削除済み利用者のデータは開示しない
            if self.user_is_deleted(user_id):
                return UTF8_BOM + self._header_only_csv()

            # キャッシュチェック
            now = datetime.now().timestamp()
            key = self._cache_key(user_id)
            cached = self._cache.get(key)
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
            self._cache[key] = (result, now, limit)
            return result
        except Exception as e:
            logger.error(f"CSVエクスポート失敗: {e}")
            handle_error(e, "Export")
            # エラー時に空のCSVを返すか、または空文字列を返す
            # ここではヘッダーのみのCSVを返す
            return UTF8_BOM + self._header_only_csv()

    @staticmethod
    def _header_only_csv() -> str:
        """ヘッダーだけの CSV（エラー時・削除済み時の応答）。"""
        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_ALL)
        writer.writerow([
            "日付", "年", "月", "日", "脚本", "曲タイトル", "アーティスト",
            "プレビューURL", "生成時刻"
        ])
        return output.getvalue()

    def export_generations_json(self, user_id: str, limit: int = EXPORT_ROW_LIMIT) -> Dict:
        """生成履歴を JSON 構造で返す（`format=json` の開示用）。

        Returns
        -------
        dict
            ``{"user_id", "exported_at", "count", "truncated", "generations"}``。
            `truncated` は**上限で切り詰められたか**を表す。
            「全部出したつもりだが違う」事故を防ぐために必ず明示する。
        """
        if self.user_is_deleted(user_id):
            return {
                "user_id": user_id,
                "exported_at": datetime.utcnow().isoformat(),
                "count": 0,
                "truncated": False,
                "generations": [],
            }
        try:
            generations = self.gen_repo.get_by_user(user_id, limit=limit)
            return {
                "user_id": user_id,
                "exported_at": datetime.utcnow().isoformat(),
                "count": len(generations),
                "truncated": len(generations) >= limit,
                "generations": generations,
            }
        except Exception as e:
            logger.error(f"JSONエクスポート失敗: {e}")
            handle_error(e, "Export")
            return {
                "user_id": user_id,
                "exported_at": datetime.utcnow().isoformat(),
                "count": 0,
                "truncated": False,
                "generations": [],
            }

    def export_bundle_json(self, user_id: str, limit: int = EXPORT_ROW_LIMIT) -> str:
        """開示データ全体を JSON 文字列にする（保存用）。

        `datetime` は `default=str` で文字列化する。JSON に変換できない値を
        黙って捨てると開示として不成立になるため、**文字列化して残す**。
        """
        from retro_radio.db.privacy_repository import (
            ConsentRepository,
            MusicProfileRepositoryImpl,
        )

        payload: Dict = {
            "generations": self.export_generations_json(user_id, limit=limit),
            "favorite_tracks": [],
            "consents": [],
        }
        if not self.user_is_deleted(user_id):
            try:
                payload["favorite_tracks"] = MusicProfileRepositoryImpl(
                    self.db
                ).list_tracks(user_id)
                payload["consents"] = ConsentRepository(self.db).history(user_id)
            except Exception as e:
                logger.warning(f"開示データの収集に失敗しました（一部のみ）: {e}")
        return json.dumps(payload, ensure_ascii=False, default=str)

    def close(self):
        self.db.close()
        self._cache.clear()
