# O3: Stripe決済連携 実装計画書

## 目的
Stripeを使ったサブスクリプション課金（月額/年額）を実装し、プランに応じた機能制限をかける。

## 前提
- O1, O2完了済み（認証・DB層）
- Stripeアカウント取得済み（テストモードでも可）
- 依存追加: `stripe>=8.0.0`
- 環境変数: 
  - `STRIPE_SECRET_KEY`
  - `STRIPE_WEBHOOK_SECRET`
  - `STRIPE_PRICE_PREMIUM_MONTHLY`
  - `STRIPE_PRICE_PREMIUM_YEARLY`
  - `STRIPE_PRICE_PRO_MONTHLY`
  - `STRIPE_PRICE_PRO_YEARLY`
  - `APP_URL` (例: `https://your-app.streamlit.app`)

---

## ステップ 1～24

### Step 1: 依存関係追加・ディレクトリ作成
**作業**: 
```bash
pip install stripe==8.2.0
mkdir -p retro_radio/billing
```
**ファイル作成**: `retro_radio/billing/__init__.py`, `retro_radio/billing/stripe_client.py`, `retro_radio/billing/webhook.py`, `retro_radio/billing/plans.py`

---

### Step 2: プラン定義ファイル作成
**ファイル**: `retro_radio/billing/plans.py`
**作業**: プラン別機能と価格IDマッピング
```python
from enum import Enum
from typing import Dict, Optional

class PlanType(str, Enum):
    FREE = "free"
    PREMIUM = "premium"
    PRO = "pro"

class BillingCycle(str, Enum):
    MONTHLY = "monthly"
    YEARLY = "yearly"

# 価格IDマッピング（実際のStripe価格IDに置き換える）
PRICE_IDS: Dict[tuple[PlanType, BillingCycle], str] = {
    (PlanType.PREMIUM, BillingCycle.MONTHLY): os.environ.get("STRIPE_PRICE_PREMIUM_MONTHLY", "price_test_premium_monthly"),
    (PlanType.PREMIUM, BillingCycle.YEARLY): os.environ.get("STRIPE_PRICE_PREMIUM_YEARLY", "price_test_premium_yearly"),
    (PlanType.PRO, BillingCycle.MONTHLY): os.environ.get("STRIPE_PRICE_PRO_MONTHLY", "price_test_pro_monthly"),
    (PlanType.PRO, BillingCycle.YEARLY): os.environ.get("STRIPE_PRICE_PRO_YEARLY", "price_test_pro_yearly"),
}

# プラン別制限
PLAN_LIMITS: Dict[PlanType, Dict[str, any]] = {
    PlanType.FREE: {
        "monthly_generations": 5,
        "audio_quality": "standard",
        "history_export": False,
        "favorites": False,
        "api_access": False,
        "batch_generation": False,
    },
    PlanType.PREMIUM: {
        "monthly_generations": -1,  # 無制限
        "audio_quality": "high",
        "history_export": True,
        "favorites": True,
        "api_access": False,
        "batch_generation": False,
    },
    PlanType.PRO: {
        "monthly_generations": -1,
        "audio_quality": "premium",
        "history_export": True,
        "favorites": True,
        "api_access": True,
        "batch_generation": True,
    },
}

def get_limits(plan: PlanType) -> Dict[str, any]:
    return PLAN_LIMITS[plan]

def get_price_id(plan: PlanType, cycle: BillingCycle) -> Optional[str]:
    return PRICE_IDS.get((plan, cycle))
```
**テスト作成**: `tests/test_billing_plans.py` - 価格ID取得・プラン制限確認

---

### Step 3: Stripeクライアント実装
**ファイル**: `retro_radio/billing/stripe_client.py`
**作業**: 
```python
import stripe
import os
from typing import Optional
from .plans import PlanType, BillingCycle, get_price_id

class StripeClient:
    def __init__(self):
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
        self.webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        self.app_url = os.environ.get("APP_URL", "http://localhost:8501")
    
    def create_checkout_session(self, user_id: str, email: str, plan: PlanType, cycle: BillingCycle) -> str:
        price_id = get_price_id(plan, cycle)
        if not price_id:
            raise ValueError(f"No price ID for plan {plan} and cycle {cycle}")
        
        # 顧客取得または作成
        customers = stripe.Customer.list(email=email, limit=1)
        if customers.data:
            customer = customers.data[0]
        else:
            customer = stripe.Customer.create(
                email=email,
                metadata={"user_id": user_id}
            )
        
        # チェックアウトセッション作成
        session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{self.app_url}/?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{self.app_url}/pricing",
            metadata={
                "user_id": user_id,
                "plan": plan.value,
                "cycle": cycle.value
            }
        )
        return session.url
    
    def create_customer_portal_session(self, customer_id: str) -> str:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{self.app_url}/account"
        )
        return session.url
```
**テスト作成**: `tests/test_billing_stripe_client.py` - モックでセッション作成確認（Stripeモックまたはstub使用）

---

### Step 4: Webhookハンドラー実装
**ファイル**: `retro_radio/billing/webhook.py`
**作業**: 
```python
import stripe
import json
import os
from typing import Dict, Any
from ..db.session import get_db_sync
from ..db.repository import UserRepository
from ..models.user import PlanType

class WebhookHandler:
    def __init__(self):
        self.webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        self.db = get_db_sync()
        self.user_repo = UserRepository(self.db)
    
    def handle_event(self, payload: bytes, sig_header: str) -> Dict[str, str]:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, self.webhook_secret
            )
        except ValueError as e:
            # 無効なペイロード
            return {"status": "error", "message": "Invalid payload"}
        except stripe.error.SignatureVerificationError as e:
            # 無効な署名
            return {"status": "error", "message": "Invalid signature"}
        
        # イベントハンドリング
        if event["type"] == "checkout.session.completed":
            self._handle_checkout_session_completed(event["data"]["object"])
        elif event["type"] == "customer.subscription.updated":
            self._handle_subscription_updated(event["data"]["object"])
        elif event["type"] == "customer.subscription.deleted":
            self._handle_subscription_deleted(event["data"]["object"])
        elif event["type"] == "invoice.payment_failed":
            self._handle_payment_failed(event["data"]["object"])
        else:
            # 未処理のイベントタイプ
            pass
        
        return {"status": "success"}
    
    def _handle_checkout_session_completed(self, session):
        user_id = session["metadata"]["user_id"]
        plan = PlanType(session["metadata"]["plan"])
        stripe_customer_id = session["customer"]
        stripe_subscription_id = session["subscription"]
        
        # ユーザー情報更新
        user = self.user_repo.get_by_id(user_id)
        if user:
            user.plan = plan
            # Stripe IDは別途更新メソッドがある場合はここで呼び出し
            # ここでは簡易的にuserオブジェクトを直接更新（実際はrepository経由）
            # TODO: 実際の実装ではrepositoryのupdate_stripe_ids等を使用
            self.db.commit()
    
    def _handle_subscription_updated(self, subscription):
        # サブスクリプション更新（プラン変更等）
        user_id = subscription["metadata"]["user_id"]
        if subscription["status"] == "active":
            plan = PlanType(subscription["metadata"]["plan"])
            user = self.user_repo.get_by_id(user_id)
            if user:
                user.plan = plan
                self.db.commit()
        # 過去のキャンセル等は無視（deletedで処理）
    
    def _handle_subscription_deleted(self, subscription):
        user_id = subscription["metadata"]["user_id"]
        user = self.user_repo.get_by_id(user_id)
        if user:
            user.plan = PlanType.FREE
            # StripeサブスクリプションIDクリア
            self.db.commit()
    
    def _handle_payment_failed(self, invoice):
        # 支払い失敗時の処理（メール送信等は別途実装）
        # とりあえずログ出力
        print(f"Payment failed for customer {invoice['customer']}")
        # サブスクリプションは未払い状態になるが、即時キャンセルはしない
```
**テスト作成**: `tests/test_billing_webhook.py` - 各イベントタイプのハンドリング確認

---

### Step 5: 課金管理マネージャークラス実装
**ファイル**: `retro_radio/billing/__init__.py`
**作業**: 
```python
from .stripe_client import StripeClient
from .webhook import WebhookHandler
from .plans import PlanType, BillingCycle, get_limits
from ..db.session import get_db_sync
from ..db.repository import UserRepository, GenerationRepository
from ..models.user import User as UserDomain
from datetime import datetime, timedelta
import secrets

class BillingManager:
    def __init__(self):
        self.stripe_client = StripeClient()
        self.db = get_db_sync()
        self.user_repo = UserRepository(self.db)
        self.gen_repo = GenerationRepository(self.db)
    
    def create_checkout_url(self, user: UserDomain, plan: PlanType, cycle: BillingCycle) -> str:
        return self.stripe_client.create_checkout_session(
            user_id=user.id,
            email=user.email,
            plan=plan,
            cycle=cycle
        )
    
    def create_portal_url(self, user: UserDomain) -> str:
        # Stripe Customer IDを取得（userオブジェクトに持たせるか、別途取得）
        # ここでは簡易実装
        user_model = self.db.query(UserModel).filter(UserModel.id == user.id).first()
        if user_model and user_model.stripe_customer_id:
            return self.stripe_client.create_customer_portal_session(user_model.stripe_customer_id)
        # フォールバック：メールから顧客検索
        customers = stripe.Customer.list(email=user.email, limit=1)
        if customers.data:
            return self.stripe_client.create_customer_portal_session(customers.data[0].id)
        raise ValueError("Stripe customer not found")
    
    def check_generation_limit(self, user: UserDomain) -> tuple[bool, int]:
        limits = get_limits(user.plan)
        monthly_limit = limits["monthly_generations"]
        
        if monthly_limit == -1:
            return True, -1  # 無制限
        
        # 月またぎリセットチェック（実際はユーザーオブジェクトに持たせるかDBから取得）
        # 簡易実装: 30日経過でリセット
        # TODO: ユーザーオブジェクトにgeneration_reset_atフィールドを持たせる
        return user.generation_count < monthly_limit, max(0, monthly_limit - user.generation_count)
    
    def increment_generation(self, user: UserDomain):
        user.generation_count += 1
        # 実際はrepository経由でDB更新
        self.user_repo.update(user)
    
    def get_user_plan(self, user: UserDomain) -> PlanType:
        # 最新のプラン状態をDBから取得
        db_user = self.user_repo.get_by_id(user.id)
        return db_user.plan if db_user else user.plan
    
    def sync_user_from_stripe(self, stripe_customer_id: str) -> Optional[UserDomain]:
        """Stripeウェブhookから呼び出し、DBとStripeの状態を同期"""
        # 実装はwebhookハンドラー内にあり、ここではインターフェースのみ
        pass
```
**テスト作成**: `tests/test_billing_manager.py` - 制限チェック・インクリメント・URL生成

---

### Step 6: AuthenticatorにBillingManager連携
**ファイル**: `retro_radio/auth/authenticator.py`
**作業**: 
```python
# インポート追加
from retro_radio.billing import BillingManager, PlanType as BillingPlanType

# コンストラクタ修正
def __init__(self):
    self.session_mgr = SessionManager()
    self._db = get_db_sync()
    self._user_repo = UserRepository(self._db)
    self._billing_mgr = BillingManager()  # 追加

# メソッド追加
def upgrade_to_premium(self, user: User, cycle: BillingCycle = BillingCycle.MONTHLY) -> str:
    """プレミアムプランへのアップグレードURLを取得"""
    return self._billing_mgr.create_checkout_url(user, BillingPlanType.PREMIUM, cycle)

def check_can_generate(self, user: User) -> tuple[bool, int]:
    """生成可能かチェック"""
    return self._billing_mgr.check_generation_limit(user)

def increment_generation_count(self, user: User):
    """生成回数をインクリメント"""
    self._billing_mgr.increment_generation(user)

def get_user_plan(self, user: User) -> PlanType:
    """最新プラン状態を取得"""
    return self._billing_mgr.get_user_plan(user)
```
**テスト作成**: `tests/test_auth_billing_integration.py` - アップグレードURL・制限チェック連携

---

### Step 7: app.py に課金UI統合
**ファイル**: `app.py` (サイドバー・メイン領域)
**作業**: サイドバーにプラン情報・アップグレードボタン追加
```python
# サイドバー（ユーザー情報表示部分の後）
with st.sidebar:
    st.markdown(f"## 👤 {user.email}")
    st.caption(f"プラン: **{user.plan.value.upper()}**")
    
    # 生成制限表示
    can_gen, remaining = auth.check_can_generate(user)
    if user.plan == PlanType.FREE:
        st.caption(f"今月の残り生成回数: **{remaining}回**")
        if remaining == 0:
            st.error("生成上限に達しました")
            if st.button("プレミアムにアップグレード", type="primary"):
                url = auth.upgrade_to_premium(user)
                st.markdown(f'[決済ページへ移動]({url})', unsafe_allow_html=True)
        elif remaining <= 2:
            st.warning(f"残り{remaining}回になりました")
    
    # プラン管理ボタン（有料プランのみ）
    if user.plan != PlanType.FREE:
        if st.button("プラン管理"):
            try:
                url = auth.upgrade_to_premium(user, BillingCycle.YEARLY)  # ポータル用に別メソッド必要
                # 実際はポータル用メソッドを追加
                st.markdown('[Stripeポータルへ](https://billing.stripe.com/pseudo/test)', unsafe_allow_html=True)
            except Exception as e:
                st.error(f"エラー: {e}")
    
    if st.button("ログアウト"):
        auth.logout()
    st.markdown("---")
```
**テスト作成**: `tests/test_app_billing_ui.py` - サイドバー表示・ボタン動作確認

---

### Step 8: 生成ボタンに制限チェック統合
**ファイル**: `app.py` (生成ボタン部分)
**作業**: 
```python
# 生成ボタン
button_disabled, remaining = not auth.check_can_generate(user)[0], auth.check_can_generate(user)[1]
if st.button("📻 ラジオを再生する", type="primary", disabled=button_disabled):
    if not button_disabled:
        # 既存の生成処理
        # ...
        # 生成成功後にカウントインクリメント
        auth.increment_generation_count(user)
    else:
        st.error(f"生成上限に達しました。プレミアムプランで無制限に。残り: {remaining}回")
        if user.plan == PlanType.FREE:
            if st.button("今すぐアップグレード"):
                url = auth.upgrade_to_premium(user)
                st.markdown(f'[決済ページへ移動]({url})', unsafe_allow_html=True)
```
**テスト作成**: `tests/test_app_generation_limit.py` - ボタン制限・エラー表示・アップグレード導線

---

### Step 9: プラン別機能制限実装（音声品質・履歴エクスポート等）
**ファイル**: 複数箇所
**作業**: 
- TTS品質選択 (`retro_radio/core/tts.py`)
- 履歴エクスポート機能
- お気に入り機能
- バッチ生成（PROプラン）

#### TTS品質選択例
**ファイル**: `retro_radio/core/tts.py`
**作業**: 
```python
def text_to_speech(text: str, quality: str = "standard") -> str:
    from retro_radio.auth import Authenticator
    auth = Authenticator()
    user = auth.get_current_user()
    if user:
        effective_quality = auth.get_user_plan(user).value  # プラン別品質マッピング必要
        # またはquality引数で上書き可能に
    # 既存ロジックにquality分岐を追加
```
**テスト作成**: `tests/test_tts_quality_by_plan.py` - プラン別音声品質適用確認

---

### Step 10: 環境変数・Secrets設定ガイド
**ファイル**: `.streamlit/secrets.toml.example` 追加
**作業**: 
```toml
# Stripe設定
STRIPE_SECRET_KEY = "sk_test_..."
STRIPE_WEBHOOK_SECRET = "whsec_..."
STRIPE_PRICE_PREMIUM_MONTHLY = "price_1OxL..."
STRIPE_PRICE_PREMIUM_YEARLY = "price_1OxL..."
STRIPE_PRICE_PRO_MONTHLY = "price_1OxL..."
STRIPE_PRICE_PRO_YEARLY = "price_1OxL..."
APP_URL = "https://your-app.streamlit.app"
```

---

### Step 11: ダミーStripeモード実装（開発・テスト用）
**ファイル**: `retro_radio/billing/stripe_client.py`
**作業**: テストモードフラグ追加
```python
def __init__(self):
    stripe_api_key = os.environ.get("STRIPE_SECRET_KEY")
    if stripe_api_key and stripe_api_key.startswith("sk_test"):
        self.test_mode = True
    else:
        self.test_mode = False
    stripe.api_key = stripe_api_key
    # ... 既存コード

def create_checkout_session(self, ...):
    if self.test_mode:
        # テストモードではダミーURLを返す
        return f"{self.app_url}/?session_id=cs_test_{secrets.token_hex(8)}"
    # 既存のStripe呼び出し
```
**テスト作成**: `tests/test_billing_stripe_client_test_mode.py` - テストモードでのダミー動作確認

---

### Step 12: 単体テスト実行（Billing層）
**作業**: 
```bash
pytest tests/test_billing_plans.py tests/test_billing_stripe_client.py tests/test_billing_stripe_client_test_mode.py tests/test_billing_webhook.py tests/test_billing_manager.py -v
```
**完了基準**: 全テストパス

---

### Step 13: 統合テスト実行（Auth + Billing）
**作業**: 
```bash
pytest tests/test_auth_billing_integration.py tests/test_app_billing_ui.py tests/test_app_generation_limit.py -v
```
**完了基準**: 全テストパス

---

### Step 14: Webhookエンドポイント実装（Streamlit用）
**注意**: Streamlitは標準でWebhookエンドポイントを持たないため、別途実装が必要
**選択肢**:
1. Streamlit Cloud Functions（有料プラン必要）
2. 別途軽量サーバー（FastAPI）を立ち上げる
3. Streamlitの`st.experimental_rerun`を使わないパターンで擬似的に処理（推奨しない）

**ここではオプション3の代替案として、定期ポーリング方式を提案**（ただし推奨はしない）
**代わりに**: ドキュメントで外部サービス連携の必要性を明記

**ファイル**: `DEPLOYMENT.md` に追記
**作業**: 
```
## 本番デプロイでのStripe Webhook設定

Streamlitアプリ単体ではWebhookエンドポイントを持たないため、以下のいずれかの方法で実装してください：

1. **別途FastAPIサーバーを立ち上げる**
   - `scripts/webhook_server.py` 参照
   - `uvicorn scripts.webhook_server:app --host 0.0.0.0 --port 8000`

2. **Streamlit Cloudの場合**
   - 有料プランで「Streamlit Cloud Functions」を利用
   - または外部サービス（Zapier, Make.com等）を経由

3. **開発・テストのみ**
   - テストモードではWebhook不要（ダミー動作）
   - 本番テスト時はStrip CLIを使用: `stripe listen --forward-to localhost:8501/webhook`
```

**ファイル作成**: `scripts/webhook_server.py` (FastAPI実装例)
```python
from fastapi import FastAPI, Request, HTTPException
import stripe
import os
from retro_radio.billing.webhook import WebhookHandler

app = FastAPI()
webhook_handler = WebhookHandler()

@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing signature")
    
    result = webhook_handler.handle_event(payload, sig_header)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["message"])
    return result
```
**テスト作成**: `tests/test_webhook_server.py` - エンドポイント基本動作

---

### Step 15: テストカバレッジ確認・足りないテスト追加
**作業**: 
```bash
pytest --cov=retro_radio.billing --cov=retro_radio.auth --cov-report=term-missing
```
**目標**: 80%以上カバレッジ

---

### Step 16: エラーハンドリング強化
**作業**: 
- Stripe APIエラー時のユーザーへの通知
- ネットワークエラー時のリトライ
- Webhookの冪等性確保（同じイベントの二重処理防止）

**Webhookに追加**: イベントIDベースの重複処理防止
```python
# WebhookHandler.__init__ に追加
self.processed_events = set()

# handle_event 内冒頭に追加
event_id = event["id"]
if event_id in self.processed_events:
    return {"status": "success", "message": "Already processed"}
self.processed_events.add(event_id)
# 古いイベントIDを削除（メモリ肥大防止）
if len(self.processed_events) > 1000:
    self.processed_events = set(list(self.processed_events)[-500:])
```

---

### Step 17: セキュリティチェック
**作業**: 
- Webhook署名検証の徹底
- 価格IDの環境変数由来確認（ハードコーディング禁止）
- ユーザー入力のサニタイズ（メールアドレス等）

---

### Step 18: パフォーマンステスト
**作業**: 
- 同時アクセス時の制限チェック性能
- キャッシュによるDBアクセス削減（オプション）

---

### Step 19: マニュアル・運用ドキュメント作成
**ファイル**: `docs/billing.md`
**作業**: 
- Stripeダッシュボードでの商品・価格設定手順
- テストカード番号一覧
- 本番移行チェックリスト
- トラブルシューティングガイド

---

### Step 20: 本番環境変数設定確認スクリプト
**ファイル**: `scripts/check_env.py`
**作業**: 必要な環境変数の存在確認と形式チェック
```python
required = [
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET", 
    "STRIPE_PRICE_PREMIUM_MONTHLY",
    "STRIPE_PRICE_PREMIUM_YEARLY",
    "STRIPE_PRICE_PRO_MONTHLY",
    "STRIPE_PRICE_PRO_YEARLY",
    "APP_URL"
]
missing = [var for var in required if not os.environ.get(var)]
if missing:
    print(f"Missing environment variables: {missing}")
    exit(1)
# 形式チェック（STRIPE_SECRET_KEYがsk_で始まる等）
```

---

### Step 21: 既存テスト回帰確認（O1,O2含む全テスト）
**作業**: 
```bash
pytest tests/ -v
```
**完了基準**: 全テストパス（リグレッションなし）

---

### Step 22: ロードマップ・将来拡張ポイント文書化
**ファイル**: `docs/billing_future.md`
**作業**: 
- 税金対応（VAT, GST等）
- クーポン・プロモーションコード
- 複数通貨対応
- 請求書ダウンロード
- アフィリエイトプログラム

---

### Step 23: デモ・プレゼン資料作成
**ファイル**: `docs/demo_script.md`
**作業**: 
- フリーミアム→プレミアムアップグレードデモ手順
- テストカード番号（4242 4242 4242 4242）での動作確認
- プラン制限の体験方法

---

### Step 24: 最終確認・リリースチェックリスト
**作業**: 
- [ ] すべての環境変数がドキュメントに記載されている
- [ ] テストモードと本番モードの切り替えが明確
- [ ] Webhookセキュリティ（署名検証）が実装済み
- [ ] エラーケースのユーザーへの表示が適切
- [ ] プラン変更時の状態遷移がテスト済み
- [ ] 既存機能にリグレッションがないこと
- [ ] デプロイ手順が文書化されている

---

## 完了定義
- [ ] 全24ステップ完了
- [ ] Stripeテストモードでのフルフロー動作確認（登録→アップグレード→生成制限解除）
- [ ] テストモードでダミーWebhookでも動作確認
- [ ] 全テストパス（リグレッションなし）
- [ ] 手動チェックリスト全項目クリア
- [ ] 本番環境変数ガイド完備