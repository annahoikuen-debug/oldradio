# O1: 認証・ユーザー管理 実装計画書

## 目的
Streamlitアプリにメール/パスワード認証、セッション管理、ログイン・登録・ログアウト機能を追加する。

## 前提
- 既存コード: `app.py`, `retro_radio/utils/session.py`
- 新規作成: `retro_radio/auth/`, `retro_radio/models/user.py`
- 依存追加: `streamlit-authenticator` 不使用（自前実装で依存最小化）

---

## ステップ 1～24

### Step 1: ディレクトリ・ファイル雛形作成
**作業**: 以下のディレクトリと空ファイルを作成
```
retro_radio/
├── auth/
│   ├── __init__.py
│   ├── authenticator.py
│   └── models.py
└── models/
    └── __init__.py
```
**確認**: `ls -la retro_radio/auth/ retro_radio/models/`

---

### Step 2: ユーザーモデル定義
**ファイル**: `retro_radio/models/user.py`
**作業**: 以下のクラスを定義
```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

class PlanType(Enum):
    FREE = "free"
    PREMIUM = "premium"
    PRO = "pro"

@dataclass
class User:
    id: str
    email: str
    hashed_password: str
    plan: PlanType = PlanType.FREE
    generation_count: int = 0
    generation_reset_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
```
**テスト作成**: `tests/test_models_user.py` - Userインスタンス生成、PlanType列挙値確認

---

### Step 3: パスワードハッシュユーティリティ
**ファイル**: `retro_radio/auth/authenticator.py` (冒頭)
**作業**: 以下の関数を実装
```python
import hashlib
import secrets

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hash_val = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000).hex()
    return f"{hash_val}:{salt}"

def verify_password(password: str, hashed: str) -> bool:
    hash_val, salt = hashed.split(":")
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000).hex() == hash_val
```
**テスト作成**: `tests/test_auth_password.py` - ハッシュ化・検証の往復テスト、間違ったパスワードでFalse

---

### Step 4: Authenticatorクラス骨格
**ファイル**: `retro_radio/auth/authenticator.py`
**作業**: クラス定義とコンストラクタ
```python
import streamlit as st
from typing import Optional
from .models import User
from ..utils.session import SessionManager

class Authenticator:
    def __init__(self):
        self.session_mgr = SessionManager()
    
    def get_current_user(self) -> Optional[User]:
        return self.session_mgr.get_user()
    
    def logout(self):
        self.session_mgr.clear_user()
        st.rerun()
```
**テスト作成**: `tests/test_auth_authenticator.py` - インスタンス化、get_current_user初期値None

---

### Step 5: SessionManager拡張（ユーザー対応）
**ファイル**: `retro_radio/utils/session.py`
**作業**: 既存クラスに以下メソッド追加
```python
def set_user(self, user: User):
    st.session_state["auth_user"] = user

def get_user(self) -> Optional[User]:
    return st.session_state.get("auth_user")

def clear_user(self):
    st.session_state.pop("auth_user", None)
```
**テスト作成**: `tests/test_session_user.py` - セット/ゲット/クリアの動作確認

---

### Step 6: ログインフォーム実装
**ファイル**: `retro_radio/auth/authenticator.py`
**作業**: `login_form()` メソッド追加
```python
def login_form(self) -> Optional[User]:
    with st.form("login_form"):
        email = st.text_input("メールアドレス")
        password = st.text_input("パスワード", type="password")
        submitted = st.form_submit_button("ログイン")
        if submitted:
            user = self._get_user_by_email(email)
            if user and verify_password(password, user.hashed_password):
                self.session_mgr.set_user(user)
                return user
            st.error("メールアドレスまたはパスワードが正しくありません")
    return None
```
**テスト作成**: `tests/test_auth_login_form.py` - モックDBで正常/異常ケース確認

---

### Step 7: 登録フォーム実装
**ファイル**: `retro_radio/auth/authenticator.py`
**作業**: `signup_form()` メソッド追加
```python
def signup_form(self) -> Optional[User]:
    with st.form("signup_form"):
        email = st.text_input("メールアドレス")
        password = st.text_input("パスワード", type="password")
        confirm = st.text_input("パスワード確認", type="password")
        submitted = st.form_submit_button("無料で始める")
        if submitted:
            if password != confirm:
                st.error("パスワードが一致しません")
                return None
            if self._get_user_by_email(email):
                st.error("このメールアドレスは既に登録されています")
                return None
            user = User(
                id=secrets.token_urlsafe(16),
                email=email,
                hashed_password=hash_password(password),
                plan=PlanType.FREE,
                generation_count=0,
                generation_reset_at=datetime.now() + timedelta(days=30),
                created_at=datetime.now()
            )
            self._save_user(user)
            self.session_mgr.set_user(user)
            return user
    return None
```
**テスト作成**: `tests/test_auth_signup_form.py` - 重複登録拒否、パスワード不一致拒否、正常登録

---

### Step 8: ダミーDB実装（インメモリ）
**ファイル**: `retro_radio/auth/authenticator.py` (プライベートメソッド)
**作業**: 開発用インメモリDB
```python
# クラス内に追加
_users_db: dict[str, User] = {}  # email -> User
_users_by_id: dict[str, User] = {}  # id -> User

def _get_user_by_email(self, email: str) -> Optional[User]:
    return self._users_db.get(email.lower())

def _get_user_by_id(self, user_id: str) -> Optional[User]:
    return self._users_by_id.get(user_id)

def _save_user(self, user: User):
    self._users_db[user.email.lower()] = user
    self._users_by_id[user.id] = user
```
**テスト作成**: `tests/test_auth_db.py` - 保存・取得・重複チェック

---

### Step 9: auth/__init__.py エクスポート
**ファイル**: `retro_radio/auth/__init__.py`
**作業**: 公開API定義
```python
from .authenticator import Authenticator
from .models import User, PlanType

__all__ = ["Authenticator", "User", "PlanType"]
```

---

### Step 10: models/__init__.py エクスポート
**ファイル**: `retro_radio/models/__init__.py`
**作業**: 
```python
from .user import User, PlanType

__all__ = ["User", "PlanType"]
```

---

### Step 11: app.py に認証フロー統合（未ログイン時）
**ファイル**: `app.py` (ページ設定直後)
**作業**: 以下を挿入
```python
from retro_radio.auth import Authenticator, PlanType

auth = Authenticator()
user = auth.get_current_user()

if not user:
    st.markdown('<div class="main-title">📻 レトロラジオ・タイムマシン</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtitle">懐かしのあの年へ、ラジオでタイムトラベル</div>', unsafe_allow_html=True)
    
    tab1, tab2 = st.tabs(["ログイン", "新規登録（無料）"])
    with tab1:
        user = auth.login_form()
    with tab2:
        user = auth.signup_form()
    st.stop()
```
**テスト作成**: `tests/test_app_auth_flow.py` - 未ログイン時タブ表示、ログイン後メイン画面遷移

---

### Step 12: サイドバーにユーザー情報・ログアウト表示
**ファイル**: `app.py` (サイドバー冒頭)
**作業**: 既存サイドバーの上部に追加
```python
with st.sidebar:
    st.markdown(f"## 👤 {user.email}")
    st.caption(f"プラン: **{user.plan.value.upper()}**")
    if st.button("ログアウト", use_container_width=True):
        auth.logout()
    st.markdown("---")
```
**テスト作成**: `tests/test_app_sidebar_user.py` - 表示確認、ログアウトでセッションクリア

---

### Step 13: 既存生成履歴をユーザー別管理に変更
**ファイル**: `app.py` (セッション状態初期化部分)
**作業**: グローバル履歴 → ユーザー別履歴
```python
# 変更前: st.session_state.generation_history = []
# 変更後:
if f"generation_history_{user.id}" not in st.session_state:
    st.session_state[f"generation_history_{user.id}"] = []

history_key = f"generation_history_{user.id}"
```
履歴参照箇所すべて `st.session_state[history_key]` に変更

**テスト作成**: `tests/test_app_user_history.py` - ユーザーA/Bで履歴が分離されること確認

---

### Step 14: 既存履歴保存関数をユーザーID付きに変更
**ファイル**: `retro_radio/services/history_service.py`
**作業**: `save_generation_result(entry, user_id)` に変更、DB保存時にuser_idカラム追加
**テスト作成**: `tests/test_history_service_user.py` - user_id付き保存・取得

---

### Step 15: 履歴表示をユーザー別に変更
**ファイル**: `app.py` (サイドバー履歴表示部分)
**作業**: `st.session_state[history_key]` を使用して表示
**テスト作成**: `tests/test_app_history_display.py` - 自分の履歴のみ表示

---

### Step 16: 再生機能をユーザー別履歴対応
**ファイル**: `app.py` (replay_entry処理)
**作業**: ユーザー別履歴キーから取得するよう修正
**テスト作成**: `tests/test_app_replay.py` - 再生ボタンで正しいデータ復元

---

### Step 17: 単体テスト実行・修正
**作業**: 
```bash
pytest tests/test_models_user.py tests/test_auth_password.py tests/test_auth_authenticator.py tests/test_session_user.py tests/test_auth_db.py -v
```
**完了基準**: 全テストパス

---

### Step 18: 統合テスト実行・修正
**作業**: 
```bash
pytest tests/test_auth_login_form.py tests/test_auth_signup_form.py tests/test_app_auth_flow.py tests/test_app_sidebar_user.py tests/test_app_user_history.py tests/test_app_history_display.py tests/test_app_replay.py -v
```
**完了基準**: 全テストパス

---

### Step 19: 既存テスト回帰確認
**作業**: 
```bash
pytest tests/ -v --ignore=tests/test_auth_*.py --ignore=tests/test_session_user.py --ignore=tests/test_app_auth_flow.py --ignore=tests/test_app_sidebar_user.py --ignore=tests/test_app_user_history.py --ignore=tests/test_app_history_display.py --ignore=tests/test_app_replay.py --ignore=tests/test_history_service_user.py
```
**完了基準**: 既存全テストパス（リグレッションなし）

---

### Step 20: 手動動作確認チェックリスト
- [ ] 未ログインでアクセス → ログイン/登録タブ表示
- [ ] 登録 → 自動ログイン → メイン画面表示
- [ ] ログアウト → 再度ログインタブ表示
- [ ] 複数ユーザーで履歴分離
- [ ] 再生ボタンで履歴から復元
- [ ] サイドバーにプラン表示

---

### Step 21: エラーハンドリング強化
**作業**: 
- DB接続エラー時のフォールバック（インメモリ継続）
- 同一メール重複登録時のユーザーフレンドリーなエラー
- セッション切れ時の自動リダイレクト

---

### Step 22: セッション永続化（オプション・後回し可）
**作業**: `streamlit-cookies-manager` または `st.query_params` でブラウザリロード後もログイン維持
**優先度**: 低（MVP後）

---

### Step 23: パスワードリセット機能（オプション・後回し可）
**作業**: メール送信不要版（管理者がDB直接書き換え）またはトークン方式
**優先度**: 低

---

### Step 24: ドキュメント更新
**ファイル**: `README.md` に認証フロー説明追加
**作業**: 環境変数不要・ローカル即動作を強調

---

## 完了定義
- [ ] 全24ステップ完了
- [ ] 全単体・統合テストパス
- [ ] 既存テストリグレッションなし
- [ ] 手動チェックリスト全項目クリア