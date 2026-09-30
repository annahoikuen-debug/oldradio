# O5: プレミアム機能実装 実装計画書

## 目的
プレミアムプラン以上で利用可能な機能（高品質音声、履歴エクスポート、お気に入り、APIアクセス、バッチ生成）を実装する。

## 前提
- O1, O2, O3, O4完了済み（認証・DB・課金・プラン制御層）
- プラン制御は`PlanController`を通じて行われる
- 新規機能はプレミアムプラン以上でのみ利用可能

---

## ステップ 1～24

### Step 1: 高品質音声機能実装
**ファイル**: `retro_radio/core/tts.py`
**作業**: プラン別音声品質選択ロジック追加
```python
from retro_radio.app.plan_control import PlanController

def text_to_speech(text: str, force_quality: str = None) -> str:
    # プラン別デフォルト品質を決定
    plan_ctrl = PlanController()
    user = plan_ctrl.get_current_user()
    
    if force_quality:
        quality = force_quality
    elif user:
        plan = plan_ctrl.get_plan()
        quality_map = {
            PlanType.FREE: "standard",
            PlanType.PREMIUM: "high",
            PlanType.PRO: "premium"
        }
        quality = quality_map.get(plan, "standard")
    else:
        quality = "standard"  # ログインしていない場合
    
    # 既存のTTSロジックに品質分岐を追加
    if quality == "premium" and os.environ.get("ELEVENLABS_API_KEY"):
        return elevenlabs_tts(text)
    elif quality == "high":
        # gTTSの高品質設定（言語コード調整等）
        return gtts_tts(text, lang="ja", tld="co.jp", slow=False)
    else:
        # 標準品質
        return gtts_tts(text)
```
**テスト作成**: `tests/test_tts_plan_quality.py` - プラン別音声品質適用確認

---

### Step 2: 履歴エクスポート機能実装
**ファイル**: `retro_radio/services/export_service.py` (新規作成)
**作業**: 
```python
import csv
import io
from datetime import datetime
from typing import List, Dict
from retro_radio.db.session import get_db_sync
from retro_radio.db.repository import GenerationRepository

class ExportService:
    def __init__(self):
        self.db = get_db_sync()
        self.gen_repo = GenerationRepository(self.db)
    
    def export_generations_csv(self, user_id: str) -> str:
        """ユーザーの生成履歴をCSV形式でエクスポート"""
        generations = self.gen_repo.get_by_user(user_id, limit=1000)  # 制限十分大きな値
        
        output = io.StringIO()
        writer = csv.writer(output)
        
        # ヘッダー
        writer.writerow([
            "日付", "年", "月", "日", "脚本", "曲タイトル", "アーティスト",
            "プレビューURL", "生成時刻"
        ])
        
        # データ行
        for gen in generations:
            writer.writerow([
                f"{gen['month']}月{gen['day']}日",
                gen['year'],
                gen['month'],
                gen['day'],
                gen['script'][:100] + "..." if len(gen['script']) > 100 else gen['script'],
                gen['song_title'],
                gen['artist_name'],
                gen['preview_url'] or "",
                gen['created_at'].strftime("%Y-%m-%d %H:%M:%S")
            ])
        
        return output.getvalue()
    
    def close(self):
        self.db.close()
```
**テスト作成**: `tests/test_export_service.py` - CSVエクスポート内容確認

---

### Step 3: 履歴エクスポートUI実装
**ファイル**: `app.py` (サイドバー履歴表示部分)
**作業**: 
```python
# 履歴セクション
st.markdown("## 📜 再生履歴")
history_key = f"generation_history_{user.id}"
history = st.session_state.get(history_key, [])

if history:
    # エクスポートボタン（プレミアム以上）
    if plan_ctrl.is_feature_enabled("history_export"):
        col1, col2 = st.columns([3, 1])
        with col1:
            st.caption(f"{len(history)}件の履歴")
        with col2:
            if st.button("📥 エクスポート"):
                # エクスポート処理
                from retro_radio.services.export_service import ExportService
                export_service = ExportService()
                try:
                    csv_data = export_service.export_generations_csv(user.id)
                    st.download_button(
                        label="CSVダウンロード",
                        data=csv_data,
                        file_name=f"retro_radio_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                        mime="text/csv"
                    )
                finally:
                    export_service.close()
    else:
        st.caption(f"{len(history)}件の履歴（プレミアムプラン以上でエクスポート可能）")
        if st.button("プレミアムにアップグレードしてエクスポート機能を解放"):
            url = plan_ctrl.get_upgrade_url(PlanType.PREMIUM)
            st.markdown(f'[決済ページへ移動]({url})', unsafe_allow_html=True)
    
    # 履歴一覧表示（既存コード）
    for i, entry in enumerate(history):
        # ...
else:
    st.caption("まだ履歴がありません")
```
**テスト作成**: `tests/test_app_export_ui.py` - エクスポートボタン表示・ダウンロード機能

---

### Step 4: お気に入り機能実装
**ファイル**: `retro_radio/services/favorite_service.py` (新規作成)
**作業**: 
```python
from retro_radio.db.session import get_db_sync
from retro_radio.db.repository import FavoriteRepository, GenerationRepository

class FavoriteService:
    def __init__(self):
        self.db = get_db_sync()
        self.fav_repo = FavoriteRepository(self.db)
        self.gen_repo = GenerationRepository(self.db)
    
    def add_favorite(self, user_id: str, generation_id: str) -> bool:
        return self.fav_repo.add(user_id, generation_id)
    
    def remove_favorite(self, user_id: str, generation_id: str) -> bool:
        return self.fav_repo.remove(user_id, generation_id)
    
    def is_favorite(self, user_id: str, generation_id: str) -> bool:
        return self.fav_repo.is_favorite(user_id, generation_id)
    
    def get_favorites(self, user_id: str) -> List[Dict]:
        """お気に入りの生成履歴を取得"""
        fav_ids = self.fav_repo.get_user_favorites(user_id)
        favorites = []
        for fid in fav_ids:
            gen = self.gen_repo.get_by_id(fid)
            if gen:
                favorites.append(gen)
        return favorites
    
    def close(self):
        self.db.close()
```
**テスト作成**: `tests/test_favorite_service.py` - お気に入り追加・削除・取得確認

---

### Step 5: お気に入りUI実装
**ファイル**: `app.py` (サイドバー・履歴アイテム表示)
**作業**: 
```python
# サイドバーに新規セクション追加
st.markdown("## ⭐ お気に入り")
if plan_ctrl.is_feature_enabled("favorites"):
    fav_service = FavoriteService()
    try:
        favorites = fav_service.get_favorites(user.id)
        if favorites:
            for fav in favorites[:5]:  # 最新5件表示
                with st.expander(f"⭐ {fav['year']}年{fav['month']}月{fav['day']}日 - {fav['song_title']}"):
                    st.caption(f"生成時刻: {fav['created_at'].strftime('%H:%M')}")
                    if st.button("再生", key=f"fav_replay_{fav['id']}"):
                        # 再生処理
                        st.session_state.replay_entry = fav
                        st.rerun()
                    if st.button("♡️ 解除", key=f"fav_remove_{fav['id']}"):
                        fav_service.remove_favorite(user.id, fav['id'])
                        st.rerun()
        else:
            st.caption("お気に入りがまだありません")
    finally:
        fav_service.close()
else:
    st.caption("⭐ お気に入り機能はプレミアムプラン以上")
    if st.button("プレミアムにアップグレード"):
        url = plan_ctrl.get_upgrade_url(PlanType.PREMIUM)
        st.markdown(f'[決済ページへ移動]({url})', unsafe_allow_html=True)
```

**履歴アイテム内お気に入りボタン**（Step 5の続き）：
```python
# 履歴アイテム表示内（O4で作成した場所の続き）
with col2:
    if plan_ctrl.is_feature_enabled("favorites"):
        fav_service = FavoriteService()
        try:
            is_fav = fav_service.is_favorite(user.id, entry["id"])
            if st.button("♥️ お気に入り" if not is_fav else "♡️ お気に入り解除", key=f"fav_{i}"):
                if is_fav:
                    fav_service.remove_favorite(user.id, entry["id"])
                    st.success("お気に入りから解除しました")
                else:
                    fav_service.add_favorite(user.id, entry["id"])
                    st.success("お気に入りに追加しました")
                st.rerun()
        finally:
            fav_service.close()
    else:
        st.caption("💡 お気に入り機能はプレミアムプラン以上")
        if st.button("プレミアムにアップグレード", key=f"upgrade_fav_{i}"):
            url = plan_ctrl.get_upgrade_url(PlanType.PREMIUM)
            st.markdown(f'[決済ページへ移動]({url})', unsafe_allow_html=True)
```
**テスト作成**: `tests/test_app_favorite_ui.py` - お気に入りボタン表示・状態遷移

---

### Step 6: APIアクセス機能準備（プロプラン）
**ファイル**: `retro_radio/api/__init__.py` (新規作成)
**作業**: 
```python
# retro_radio/api/__init__.py
from .v1 import router as v1_router
__all__ = ["v1_router"]

# retro_radio/api/v1.py
from fastapi import APIRouter, HTTPException, Depends
from typing import Optional
from retro_radio.auth import Authenticator
from retro_radio.models.user import PlanType
from retro_radio.db.session import get_db_sync
from retro_radio.db.repository import GenerationRouter

router = APIRouter(prefix="/api/v1")

def get_current_user(token: str = Depends(oauth2_scheme)):  # 簡易実装
    # 実際はJWTトークン検証等
    auth = Authenticator()
    # トークンからユーザー取得ロジック
    return auth.get_current_user()  # 簡易版

@router.get("/generate")
async def generate_radio(
    year: int,
    current_user: dict = Depends(get_current_user)
):
    # プランチェック
    if current_user.get("plan") != "pro":
        raise HTTPException(status_code=403, detail="API access requires PRO plan")
    
    # 実際の生成処理（バックグラウンドタスク等）
    # ここではスタブ
    return {"status": "accepted", "message": "Generation started"}
```
**注意**: 実際のAPIサーバーは別途FastAPIサーバーとして実装が必要
**テスト作成**: `tests/test_api_access_control.py` - PROプラン必須確認

---

### Step 7: バッチ生成機能準備（プロプラン）
**ファイル**: `retro_radio/core/batch_processor.py` (新規作成)
**作業**: 
```python
import asyncio
from typing import List, Dict
from retro_radio.core.pipeline import generate_all_parallel
from retro_radio.utils.async_runner import AsyncProgress

class BatchProcessor:
    def __init__(self):
        self.progress = AsyncProgress()
    
    async def process_batch(self, years: List[int], month: int, day: int) -> List[Dict]:
        """複数年の一括生成"""
        results = []
        total = len(years)
        
        for i, year in enumerate(years):
            self.progress.update(
                int((i / total) * 100),
                f"処理中: {year}年 ({i+1}/{total})"
            )
            
            try:
                result = await generate_all_parallel(year, month, day, self.progress)
                results.append({
                    "year": year,
                    "success": True,
                    "data": result
                })
            except Exception as e:
                results.append({
                    "year": year,
                    "success": False,
                    "error": str(e)
                })
        
        self.progress.complete("バッチ処理完了")
        return results
```
**テスト作成**: `tests/test_batch_processor.py` - バッチ処理基本動作

---

### Step 8: プレミアム機能UI統合（プロプラン機能）
**ファイル**: `app.py` (適切な場所にプロプラン機能案内追加)
**作業**: 
```python
# サイドバー・プロプラン案内セット
if plan_ctrl.get_plan() == PlanType.PREMIUM:
    st.markdown("---")
    st.markdown("### 💎 プロプランにアップグレードでさらにパワーアップ！")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**APIアクセス**")
        st.caption("・外部アプリから生成リクエスト可能")
        st.caption("・Webhook連携")
        st.caption("・自動化ワークフロー")
    with col2:
        st.markdown("**バッチ生成**")
        st.caption("・複数年を一括処理")
        st.caption("・年間カレンダー自動生成")
        st.caption("・定時実行スケジュール")
    
    if st.button("プロプランにアップグレード", type="primary"):
        url = plan_ctrl.get_upgrade_url(PlanType.PRO)
        st.markdown(f'[決済ページへ移動]({url})', unsafe_allow_html=True)
```

---

### Step 9: プレミアム機能テストユーティリティ作成
**ファイル**: `tests/conftest.py` テスト用フィクスチャー追加
**作業**: 
```python
import pytest
from retro_radio.app.plan_control import PlanController
from unittest.mock import Mock, patch

@pytest.fixture
def mock_user_free():
    user = Mock()
    user.id = "test_free_user"
    user.plan = PlanType.FREE
    return user

@pytest.fixture
def mock_user_premium():
    user = Mock()
    user.id = "test_premium_user"
    user.plan = PlanType.PREMIUM
    return user

@pytest.fixture
def mock_user_pro():
    user = Mock()
    user.id = "test_pro_user"
    user.plan = PlanType.PRO
    return user

@pytest.fixture
def plan_controller(mock_user_free):
    with patch('retro_radio.auth.Authenticator.get_current_user', return_value=mock_user_free):
        yield PlanController()
```
**テスト作成**: 各機能テストでこれらのフィクスチャーを使用

---

### Step 10: プレミアム機能単体テスト実行
**作業**: 
```bash
pytest tests/test_tts_plan_quality.py tests/test_export_service.py tests/test_favorite_service.py tests/test_batch_processor.py -v
```
**完了基準**: 全テストパス

---

### Step 11: プレミアム機能統合テスト実行
**作業**: 
```bash
pytest tests/test_app_export_ui.py tests/test_app_favorite_ui.py tests/test_app_premium_features.py -v
```
**テスト作成**: `tests/test_app_premium_features.py` - プレミアム機能連携動作確認

---

### Step 12: エラーハンドリング・フォールバック実装
**作業**: 
- 高品質音声API利用不可時のフォールバック（標準品質に切替）
- エクスポート失敗時のユーザー通知
- お気に入りDBエラー時のリトライロジック

**例**: TTS関数にフォールバック追加
```python
def text_to_speech(text: str, force_quality: str = None) -> str:
    try:
        # ... 既存ロジック ...
        if quality == "premium" and os.environ.get("ELEVENLABS_API_KEY"):
            return elevenlabs_tts(text)
        # ... 他の品質 ...
    except Exception as e:
        # エラー時は標準品質にフォールバック
        st.warning(f"高品質音声の生成に失敗しました。標準品質で続行します。")
        return gtts_tts(text)  # 標準品質
```

---

### Step 13: パフォーマンス最適化
**作業**: 
- エクスポート処理のストリーミング対応（大量データ時）
- お気に入り取得のキャッシュ（短時間有効）
- バッチ生成の進捗リアルタイム更新

---

### Step 14: セキュリティレビュー
**作業**: 
- APIエンドポイントの認証必須確認
- エクスポートデータの機密情報マスキング（必要に応じて）
- お気に入りIDの予測不可能性確認（シーケンシャルIDではないこと）

---

### Step 15: ローディング状態・フィードバック改善
**作業**: 
- エクスポート処理中のスピンナー表示
- お気に入り追加/削除の即時フィードバック
- バッチ生成のプログレスバー詳細表示

---

### Step 16: 多言語対応確認
**作業**: プレミアム機能関連のUIテキストが多言語対応していること確認
- 「エクスポート」、「お気に入り」、「高品質音声」等の翻訳

---

### Step 17: ヘルプ・ツールチップ追加
**作業**: 各プレミアム機能の説明にツールチップ追加
```python
st.button("📥 エクスポート", help="プレミアムプラン以上で利用可能。生成履歴をCSV形式でダウンロードします。")
```

---

### Step 18: デモ・テストシナリオ作成
**ファイル**: `docs/premium_features_demo.md`
**作業**: 
- フリーミアム→プレミアムアップグレード後の機能解放手順
- プレミアム→プロアップグレード後のAPI・バッチ機能解放手順
- 各機能のテスト手順と期待結果

---

### Step 19: ドキュメント更新
**ファイル**: `README.md`, `FEATURES.md`
**作業**: 
- プレミアム機能一覧と説明
- 使い方ガイド
- トラブルシューティング（エクスポート失敗等）

---

### Step 20: パフォーマンステスト実行
**作業**: 
- 1000件履歴のエクスポート時間測定
- 100件お気に入りの取得時間測定
- 10年分のバッチ生成時間測定

---

### Step 21: 既存テスト回帰確認
**作業**: 
```bash
pytest tests/ -v --ignore=tests/test_tts_plan_quality.py --ignore=tests/test_export_service.py --ignore=tests/test_favorite_service.py --ignore=tests/test_batch_processor.py --ignore=tests/test_app_export_ui.py --ignore=tests/test_app_favorite_ui.py --ignore=tests/test_app_premium_features.py
```
**完了基準**: 既存テストリグレッションなし

---

### Step 22: エッジケーステスト
**作業**: 
- 空履歴のエクスポート
- 重複お気に入り追加
- APIレートリミットシミュレーション
- バッチ生成の一部失敗時の継続処理

---

### Step 23: ローカライズ・国際化対応
**作業**: 
- 日付フォーマットのローカライズ（MM月DD日 vs MM/DD）
- 音声言語の選択肢拡大準備
- 通貨表示のローカライズ（将来のマルチ通貨対応）

---

### Step 24: 最終確認・リリースチェックリスト
**作業**: 
- [ ] プレミアムプラン: 高品質音声・履歴エクスポート・お気に入り利用可能
- [ ] プロプラン: さらにAPIアクセス・バッチ生成利用可能
- [ ] フリーミアムプラン: これらの機能が制限されていること
- [ ] アップグレード・ダウングレード時の機能反映が正常
- [ ] すべてのプレミアム機能に適切なエラーハンドリングあり
- [ ] 全テストパス（リグレッションなし）
- [ ] 手動チェックリスト全項目クリア
- [ ] ドキュメントが最新状態