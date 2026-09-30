# O4: プラン制御・アプリ統合 実装計画書

## 目的
認証・課金層と既存アプリ機能を統合し、プラン別機能制限・アップグレード導線・ユーザー体験を一貫させる。

## 前提
- O1, O2, O3完了済み（認証・DB・課金層）
- 既存アプリ機能はそのまま使用
- 新規・変更箇所: app.py の制御ロジック、一部コンポーネントのプラン別表示

---

## ステップ 1～24

### Step 1: プラン制御ユーティリティ作成
**ファイル**: `retro_radio/app/plan_control.py`
**作業**: アプリ全体で使用するプラン制御ロジックを集約
```python
from typing import Tuple, Optional
from retro_radio.auth import Authenticator
from retro_radio.billing import PlanType as BillingPlanType, BillingCycle
from retro_radio.models.user import PlanType as ModelPlanType

# モデルとBillingのPlanTypeをマッピング（同じなら直接使用可）
# ここでは同じEnumを使用していると仮定

class PlanController:
    def __init__(self):
        self.auth = Authenticator()
    
    def get_current_user(self) -> Optional[ModelPlanType]:
        return self.auth.get_current_user()
    
    def can_generate(self) -> Tuple[bool, int]:
        """生成可能かチェックし、(可能か, 残り回数) を返す"""
        user = self.get_current_user()
        if not user:
            return False, 0
        return self.auth.check_can_generate(user)
    
    def increment_generation(self):
        """生成回数をインクリメント"""
        user = self.get_current_user()
        if user:
            self.auth.increment_generation_count(user)
    
    def get_plan(self) -> ModelPlanType:
        """現在のプランを取得"""
        user = self.get_current_user()
        if not user:
            return ModelPlanType.FREE
        return self.auth.get_user_plan(user)
    
    def get_upgrade_url(self, plan: ModelPlanType, cycle: BillingCycle = BillingCycle.MONTHLY) -> str:
        """アップグレードURLを取得"""
        user = self.get_current_user()
        if not user:
            raise ValueError("User not logged in")
        return self.auth.upgrade_to_premium(user, plan, cycle)
    
    def is_feature_enabled(self, feature: str) -> bool:
        """機能がプランで有効かチェック"""
        user = self.get_current_user()
        if not user:
            return False
        plan = self.auth.get_user_plan(user)
        # プラン別機能マッピング
        feature_map = {
            "unlimited_generations": plan in [ModelPlanType.PREMIUM, ModelPlanType.PRO],
            "high_quality_audio": plan in [ModelPlanType.PREMIUM, ModelPlanType.PRO],
            "history_export": plan in [ModelPlanType.PREMIUM, ModelPlanType.PRO],
            "favorites": plan in [ModelPlanType.PREMIUM, ModelPlanType.PRO],
            "api_access": plan == ModelPlanType.PRO,
            "batch_generation": plan == ModelPlanType.PRO,
            "no_ads": plan in [ModelPlanType.PREMIUM, ModelPlanType.PRO],  # 今後の広告実装用
        }
        return feature_map.get(feature, False)
```
**テスト作成**: `tests/test_app_plan_control.py` - 各メソッドの動作確認

---

### Step 2: app.py にPlanController統合
**ファイル**: `app.py` (冒頭付近)
**作業**: 
```python
# 既存の認証コードの後、PlanControllerを追加
from retro_radio.auth import Authenticator
from retro_radio.app.plan_control import PlanController

auth = Authenticator()
plan_ctrl = PlanController()

# ページ設定の後
user = auth.get_current_user()
if not user:
    # ログイン/登録フロー（変更なし）
    ...
else:
    # ログイン済み処理
    # PlanControllerを使用して各種チェック
    pass
```

---

### Step 3: サイドバーUIをPlanControllerベースに変更
**ファイル**: `app.py` (サイドバー部分)
**作業**: 認証チェック・プラン表示・アップグレードボタンをPlanController経由に変更
```python
with st.sidebar:
    user = auth.get_current_user()
    if user:
        st.markdown(f"## 👤 {user.email}")
        st.caption(f"プラン: **{plan_ctrl.get_plan().value.upper()}**")
        
        # 生成制限表示
        can_gen, remaining = plan_ctrl.can_generate()
        current_plan = plan_ctrl.get_plan()
        if current_plan == PlanType.FREE:
            st.caption(f"今月の残り生成回数: **{remaining}回**")
            if remaining == 0:
                st.error("生成上限に達しました")
                if st.button("プレミアムにアップグレード", type="primary"):
                    url = plan_ctrl.get_upgrade_url(PlanType.PREMIUM)
                    st.markdown(f'[決済ページへ移動]({url})', unsafe_allow_html=True)
            elif remaining <= 2:
                st.warning(f"残り{remaining}回になりました")
        
        # プラン管理ボタン
        if current_plan != PlanType.FREE:
            if st.button("プラン管理"):
                # ポータルURL取得（実装は別途必要）
                st.info("プラン管理機能は準備中です")
        
        if st.button("ログアウト"):
            auth.logout()
            st.rerun()
    else:
        # 未ログイン時はログイン/登録タブ（既存コード）
        ...
    st.markdown("---")
```
**テスト作成**: `tests/test_app_sidebar_plan_control.py` - サイドバー表示・ボタン動作

---

### Step 4: 生成ボタン制御をPlanControllerベースに変更
**ファイル**: `app.py` (生成ボタン部分)
**作業**: 
```python
# 生成可能チェック
can_generate, remaining = plan_ctrl.can_generate()
button_disabled = not can_generate

if st.button("📻 ラジオを再生する", type="primary", disabled=button_disabled):
    if can_generate:
        # 既存の生成処理
        # ...
        # 生成成功後
        plan_ctrl.increment_generation()
        st.success("生成が完了しました！")
    else:
        st.error(f"生成上限に達しました。残り: {remaining}回")
        current_plan = plan_ctrl.get_plan()
        if current_plan == PlanType.FREE:
            col1, col2 = st.columns(2)
            with col1:
                if st.button("今すぐプレミアムにアップグレード"):
                    url = plan_ctrl.get_upgrade_url(PlanType.PREMIUM)
                    st.markdown(f'[決済ページへ移動]({url})', unsafe_allow_html=True)
            with col2:
                if st.button("プラン詳細を見る"):
                    st.info("プレミアム: 月額¥500、プロ: 月額¥2,000")
```
**テスト作成**: `tests/test_app_generation_button_control.py` - ボタン状態・制限動作・アップグレード導線

---

### Step 5: プラン別機能表示ロジック実装
**ファイル**: 複数箇所
**作業**: 各機能にプラン別表示・制御を追加

#### 音声品質表示例
**ファイル**: `app.py` (音声合成結果表示部分)
**作業**: 
```python
# 結果表示エリア
if 'script' in locals():
    # ...
    # ニュース音声表示
    st.subheader("🎙️ ニュース音声")
    if audio_path and os.path.exists(audio_path):
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        st.audio(audio_bytes, format="audio/mp3")
        
        # プラン別品質表示
        current_plan = plan_ctrl.get_plan()
        if current_plan == PlanType.PREMIUM:
            st.caption("🔊 高品質音声（プレミアム機能）")
        elif current_plan == PlanType.PRO:
            st.caption("🔊🔊 プレミアムプラス音声（プロ機能）")
        else:
            st.caption("🔊 標準品質音声")
    else:
        # ...
```

#### 履歴エクスポートボタン例
**ファイル**: `app.py` (サイドバー履歴表示部分)
**作業**: 
```python
# 履歴セクション
st.markdown("## 📜 再生履歴")
if plan_ctrl.is_feature_enabled("history_export"):
    if st.button("履歴をCSVでエクスポート"):
        # エクスポート処理
        st.success("履歴をエクスポートしました！（実装中）")
else:
    st.caption("💡 履歴エクスポートはプレミアムプラン以上で利用可能")
    if st.button("プレミアムにアップグレード", key="upgrade_history_export"):
        url = plan_ctrl.get_upgrade_url(PlanType.PREMIUM)
        st.markdown(f'[決済ページへ移動]({url})', unsafe_allow_html=True)
```

#### お気に入りボタン例
**ファイル**: `app.py` (履歴アイテム表示内)
**作業**: 
```python
with st.expander(f"{entry['year']}年{entry['month']}月{entry['day']}日 - {entry['song_title']}"):
    # ... 既存表示 ...
    
    col1, col2 = st.columns([3, 1])
    with col1:
        if st.button(f"再生", key=f"replay_{i}"):
            st.session_state.replay_entry = entry
            st.rerun()
    with col2:
        if plan_ctrl.is_feature_enabled("favorites"):
            is_fav = False  # 実際はDBから取得
            if st.button("♥️ お気に入り" if not is_fav else "♡️ お気に入り解除", key=f"fav_{i}"):
                # お気に入り追加/削除処理
                st.success("お気に入りに追加しました！" if not is_fav else "お気に入りから解除しました")
                st.rerun()
        else:
            st.caption("💡 お気に入り機能はプレミアムプラン以上")
            if st.button("プレミアムにアップグレード", key=f"upgrade_fav_{i}"):
                url = plan_ctrl.get_upgrade_url(PlanType.PREMIUM)
                st.markdown(f'[決済ページへ移動]({url})', unsafe_allow_html=True)
```

---

### Step 6: プラン別コンテンツ・広告表示ロジック（今後の拡張用）
**ファイル**: `app.py` (適切な場所)
**作業**: プラン別メッセージ・プロモーション表示
```python
# メインエリア下部にプロモーションバナー
current_plan = plan_ctrl.get_plan()
if current_plan == PlanType.FREE:
    st.markdown("---")
    st.markdown("### 🚀 アップグレードでさらに快適に！")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("**プレミアム**")
        st.caption("・生成無制限")
        st.caption("・高品質音声")
        st.caption("・履歴エクスポート")
        if st.button("詳しく見る"):
            url = plan_ctrl.get_upgrade_url(PlanType.PREMIUM)
            st.markdown(f'[プレミアムプランへ]({url})', unsafe_allow_html=True)
    with col2:
        st.markdown("**プロ**")
        st.caption("・すべてのプレミアム機能")
        st.caption("・APIアクセス")
        st.caption("・バッチ生成")
        if st.button("詳しく見る"):
            url = plan_ctrl.get_upgrade_url(PlanType.PRO)
            st.markdown(f'[プロプランへ]({url})', unsafe_allow_html=True)
    with col3:
        if st.button("今すぐ試す"):
            st.info("7日間無料トライアル実装中")
```

---

### Step 7: モバイル・レスポンシブ対応確認
**作業**: 各プラン別UI要素がモバイルでも正常表示されること確認
- サイドバーの幅
- ボタンのサイズ
- テキストの折り返し

---

### Step 8: アクセシビリティ対応
**作業**: 
- プラン別表示に十分なコントラスト比
- スクリーンリーダー用ラベル
- キーボードナビゲーション対応

---

### Step 9: テストデータ作成・テスト実行
**作業**: 
```bash
# プランコントローラーユニットテスト
pytest tests/test_app_plan_control.py -v

# サイドバー統合テスト
pytest tests/test_app_sidebar_plan_control.py -v

# 生成ボタン制御テスト
pytest tests/test_app_generation_button_control.py -v

# プラン別機能表示テスト
pytest tests/test_app_feature_display.py -v  # 新規作成
```
**テスト作成**: `tests/test_app_feature_display.py` - プラン別機能表示・制御ロジック

---

### Step 10: 既存テスト回帰確認
**作業**: 
```bash
pytest tests/ -v --ignore=tests/test_app_plan_control.py --ignore=tests/test_app_sidebar_plan_control.py --ignore=tests/test_app_generation_button_control.py --ignore=tests/test_app_feature_display.py
```
**完了基準**: 既存テストリグレッションなし

---

### Step 11: エッジケーステスト
**作業**: 
- プラン変更中の状態遷移（アップグレード/downgrade）
- セッション切れ時の挙動
- 同時ログイン時の制限カウント
- 月またぎリセットの動作確認

---

### Step 12: パフォーマンス最適化
**作業**: 
- PlanControllerのインスタンス作成を最小化（シングルトンまたはキャッシュ）
- プランチェック結果のキャッシュ（短時間有効）
- DBアクセスの最小化

---

### Step 13: ローディング状態・エラー状態UI改善
**作業**: 
- プランチェック中のローディングインジケータ
- 課金APIエラー時のフォールバックメッセージ
- ネットワークオフライン時のオフラインモード案内（PWA活用）

---

### Step 14: 多言語対応確認（i18n）
**作業**: プラン別UIテキストが多言語対応していること確認
- 日本語・英語対応
- プラン名の翻訳（「プレミアム」→"Premium"）

---

### Step 15: ヘルプ・ツールチップ追加
**作業**: プラン制限・機能説明にツールチップ追加
```python
st.caption("残り生成回数", help="プレミアムプランにアップグレードすると無制限になります")
```

---

### Step 16: デモ用テストアカウント作成ガイド
**ファイル**: `docs/demo_accounts.md`
**作業**: 
- フリーミアムアカウント（制限あり）
- プレミアムアカウント（制限なし）
- プロアカウント（全機能）
- 各アカウントでのテスト手順

---

### Step 17: アナリティクス・イベントトラッキング準備
**作業**: 重要なイベントにトラッキングポイント追加（将来の拡張用）
- プラン変更イベント
- アップグレードボタンクリック
- 生成制限到達イベント
- 機能利用イベント（プレミアム機能使用時）

---

### Step 18: 法規制・表示義務確認
**作業**: 
- 有料プランの明確な表示
- 自動更新の説明
- キャンセル方法の案内
- 特定商取引法に基づく表示（必要に応じて）

---

### Step 19: フィードバックループ実装準備
**作業**: プラン変更後のアンケート表示準備（将来の実装用）
- アップグレード後: 満足度アンケート
- ダウングレード後: 改善点アンケート

---

### Step 20: パフォーマンステスト・ロードテスト
**作業**: 
- 100ユーザー同時アクセス時のプランチェック応答時間
- 月またぎリセット処理の負荷
- キャッシュ効果測定

---

### Step 21: セキュリティレビュー
**作業**: 
- プランによって機能制限がバイパスできないこと確認
- ダイレクトアクセス防止（URL直打ちでの機能利用不可）
- セッション固定攻撃対策

---

### Step 22: ドキュメント更新
**ファイル**: `README.md`, `OPERATIONS.md`
**作業**: 
- プラン構成の説明
- アップグレード手順
- トラブルシューティング（プランが反映されない等）

---

### Step 23: 最終統合テスト
**作業**: エンド・ツー・エンドシナリオテスト
```python
# tests/test_e2e_plan_flow.py
def test_free_to_premium_upgrade_flow():
    # 1. フリーアカウントで登録
    # 2. 5回生成で上限到達
    # 3. アップグレードボタンクリック
    # 4. テストモードで決済シミュレーション
    # 5. プレミアムプランに反映
    # 6. 無制限生成可能
    # 7. プレミアム機能（高音質・エクスポート等）利用可能
    pass
```

---

### Step 24: リリース準備・チェックリスト
**作業**: 
- [ ] すべてのプラン別機能制限が正常動作
- [ ] アップグレード導線がすべての適切な場所に存在
- [ ] ダウングレード時の状態遷移が正常
- [ ] 月またぎリセットが正常動作
- [ ] テストモードと本番モードの切り替えが明確
- [ ] 既存機能にリグレッションがないこと
- [ ] ドキュメントが最新状態
- [ ] デモスクリプトが動作確認済み

---

## 完了定義
- [ ] 全24ステップ完了
- [ ] プラン別機能制限が正しく動作（Free: 5回制限、Premium: 無制限、Pro: 全機能）
- [ ] アップグレード・ダウングレードフローがテスト済み
- [ ] サイドバー・メインエリアのすべてのプラン別UIが正常表示
- [ ] 全テストパス（リグレッションなし）
- [ ] 手動チェックリスト全項目クリア
- [ ] エンド・ツー・エンドフローテスト合格