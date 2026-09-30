# O1: 認証基盤実装計画書 (24ステップ)

## 目的
ユーザー登録・ログイン・セッション管理・ログアウト機能を実装する

## 前提
- 既存: `retro_radio/utils/session.py` (セッション管理ユーティリティ)
- 新規: `retro_radio/auth/` ディレクトリ配下に実装
- 依存追加: `streamlit-authenticator`, `bcrypt` (パスワードハッシュ)

---

## ステップ 1-6: ディレクトリ構造・モデル定義

### Step 1: ディレクトリ作成
```bash
mkdir -p retro_radio/auth retro_radio/models
```

### Step 2: `retro_radio/models/__init__.py` 作成
```python
# 空ファイル
```

### Step 3: `retro_radio/models/auth_models.py` 作成
```python
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

class PlanType(str, Enum):
    FREE = "free"
    PREMIUM = "premium"
    PRO = "pro"

class BillingCycle(str, Enum):
    MONTHLY = "monthly"
    YEARLY = "yearly"

@dataclass
class User:
    id: str
    email: str
    hashed_password: str
    plan: PlanType = PlanType.FREE
    billing_cycle: Optional[BillingCycle] = None
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    generation_count: int = 0
    generation_reset_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
```

### Step 4: `retro_radio/models/__init__.py` 更新（export追加）
```python
from .auth_models import User, PlanType, BillingCycle
__all__ = ["User", "PlanType", "BillingCycle"]
```

### Step 5: `retro_radio/auth/__init__.py` 作成
```python
from .authenticator import Authenticator
from .session_manager import SessionManager
__all__ = ["Authenticator", "SessionManager"]
```

### Step 6: `retro_radio/auth/session_manager.py` 作成（既存session.pyをラップ）
```python
import streamlit as st
from typing import Optional
from ..models.auth_models import User

class SessionManager:
    USER_KEY = "auth_user"
    
    def set_user(self, user: User):
        st.session_state[self.USER_KEY] = user
    
    def get_user(self) -> Optional[User]:
        return st.session_state.get(self.USER_KEY)
    
    def clear_user(self):
        if self.USER_KEY in st.session_state:
            del st.session_state[self.USER_KEY]
    
    def is_logged_in(self) -> bool:
        return self.get_user() is not None
```

---

## ステップ 7-12: パスワードハッシュ・認証ロジック

### Step 7: `requirements.txt` に依存追加
```
streamlit-authenticator==0.2.3
bcrypt==4.1.2
```

### Step 8: `retro_radio/auth/password.py` 作成
```python
import bcrypt

def hash_password(password: str) -> str:
    """パスワードをbcryptでハッシュ化"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode(), salt).decode()

def verify_password(password: str, hashed: str) -> bool:
    """パスワード検証"""
    return bcrypt.checkpw(password.encode(), hashed.encode())
```

### Step 9: `retro_radio/auth/authenticator.py` 作成（雛形）
```python
import streamlit as st
import secrets
from typing import Optional
from ..models.auth_models import User
from .session_manager import SessionManager
from .password import hash_password, verify_password

class Authenticator:
    def __init__(self):
        self.session_mgr = SessionManager()
    
    def login_form(self) -> Optional[User]:
        pass  # Step 10で実装
    
    def signup_form(self) -> Optional[User]:
        pass  # Step 11で実装
    
    def logout(self):
        self.session_mgr.clear_user()
        st.rerun()
    
    def get_current_user(self) -> Optional[User]:
        return self.session_mgr.get_user()
```

### Step 10: `login_form` 実装
```python
def login_form(self) -> Optional[User]:
    with st.form("login_form", clear_on_submit=False):
        st.subheader("ログイン")
        email = st.text_input("メールアドレス", key="login_email")
        password = st.text_input("パスワード", type="password", key="login_password")
        submitted = st.form_submit_button("ログイン", use_container_width=True)
        
        if submitted:
            user = self._get_user_by_email(email)
            if user and verify_password(password, user.hashed_password):
                self.session_mgr.set_user(user)
                st.success("ログインしました")
                st.rerun()
            else:
                st.error("メールアドレスまたはパスワードが違います")
        return None
```

### Step 11: `signup_form` 実装
```python
def signup_form(self) -> Optional[User]:
    with st.form("signup_form", clear_on_submit=False):
        st.subheader("新規登録（無料）")
        email = st.text_input("メールアドレス", key="signup_email")
        password = st.text_input("パスワード", type="password", key="signup_password")
        confirm = st.text_input("パスワード確認", type="password", key="signup_confirm")
        submitted = st.form_submit_button("無料で始める", use_container_width=True)
        
        if submitted:
            if password != confirm:
                st.error("パスワードが一致しません")
                return None
            if len(password) < 8:
                st.error("パスワードは8文字以上で入力してください")
                return None
            if self._get_user_by_email(email):
                st.error("このメールアドレスは既に登録されています")
                return None
            
            user = User(
                id=secrets.token_urlsafe(16),
                email=email,
                hashed_password=hash_password(password),
                created_at=__import__('datetime').datetime.now()
            )
            self._save_user(user)
            self.session_mgr.set_user(user)
            st.success("登録完了！")
            st.rerun()
        return None
```

### Step 12: DB操作スタブメソッド追加（後でO2で実装）
```python
    def _get_user_by_email(self, email: str) -> Optional[User]:
        # TODO: O2でDB実装時に置換
        return None
    
    def _save_user(self, user: User):
        # TODO: O2でDB実装時に置換
        pass
```

---

## ステップ 13-18: UI統合・表示コンポーネント

### Step 13: `retro_radio/auth/ui.py` 作成
```python
import streamlit as st
from .authenticator import Authenticator
from ..models.auth_models import User, PlanType

def render_auth_page(auth: Authenticator) -> Optional[User]:
    """未ログイン時の認証ページ表示"""
    st.markdown("""
    <div style="text-align: center; padding: 2rem;">
        <h1>📻 レトロラジオ・タイムマシン</h1>
        <p>懐かしのあの年へ、ラジオでタイムトラベル</p>
    </div>
    """, unsafe_allow_html=True)
    
    tab1, tab2 = st.tabs(["ログイン", "新規登録"])
    with tab1:
        auth.login_form()
    with tab2:
        auth.signup_form()
    return None

def render_user_sidebar(auth: Authenticator, user: User):
    """サイドバー: ユーザー情報・プラン・ログアウト"""
    with st.sidebar:
        st.markdown("---")
        st.markdown(f"## 👤 {user.email}")
        
        plan_labels = {
            PlanType.FREE: "🆓 フリー",
            PlanType.PREMIUM: "⭐ プレミアム",
            PlanType.PRO: "💎 プロ"
        }
        st.caption(f"プラン: **{plan_labels.get(user.plan, user.plan.value)}**")
        
        if user.plan == PlanType.FREE:
            st.caption("月5回まで生成可能")
        
        if st.button("ログアウト", use_container_width=True):
            auth.logout()
```

### Step 14: `retro_radio/auth/__init__.py` 更新（UI export追加）
```python
from .authenticator import Authenticator
from .session_manager import SessionManager
from .ui import render_auth_page, render_user_sidebar
__all__ = ["Authenticator", "SessionManager", "render_auth_page", "render_user_sidebar"]
```

---

## ステップ 19-24: 動作確認・テスト

### Step 15: `test_auth.py` 作成（簡易テスト）
```python
# retro_radio/auth/test_auth.py
import sys
sys.path.insert(0, '.')

from retro_radio.auth.password import hash_password, verify_password

def test_password_hash():
    pw = "testpassword123"
    hashed = hash_password(pw)
    assert verify_password(pw, hashed) == True
    assert verify_password("wrong", hashed) == False
    print("✅ password hash test passed")

def test_password_min_length():
    pw = "short"
    hashed = hash_password(pw)
    assert len(pw) < 8
    print("✅ length check test passed")

if __name__ == "__main__":
    test_password_hash()
    test_password_min_length()
    print("All tests passed!")
```

### Step 16: 手動動作確認用スクリプト `test_auth_manual.py`
```python
# 手動実行: streamlit run test_auth_manual.py
import streamlit as st
from retro_radio.auth import Authenticator, render_auth_page

st.set_page_config(page_title="Auth Test", layout="centered")
auth = Authenticator()

user = auth.get_current_user()
if user:
    st.success(f"ログイン中: {user.email}")
    if st.button("ログアウト"):
        auth.logout()
else:
    render_auth_page(auth)
```

### Step 17: 既存 `app.py` に認証ガード追加（仮・O4で本実装）
```python
# app.py の冒頭に追加（一時的）
# from retro_radio.auth import Authenticator, render_auth_page
# auth = Authenticator()
# user = auth.get_current_user()
# if not user:
#     render_auth_page(auth)
#     st.stop()
```

### Step 18: パスワードリセット機能（雛形のみ）
```python
# retro_radio/auth/password_reset.py
import streamlit as st
from typing import Optional

def render_password_reset_form() -> Optional[str]:
    """パスワードリセット申請フォーム（メール送信はO5で実装）"""
    with st.form("password_reset_form"):
        st.subheader("パスワードリセット")
        email = st.text_input("登録済みメールアドレス")
        if st.form_submit_button("リセットリンクを送信"):
            st.info("機能準備中です（O5で実装予定）")
    return None
```

### Step 19: メールアドレス検証ユーティリティ
```python
# retro_radio/auth/validators.py
import re

EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

def is_valid_email(email: str) -> bool:
    return bool(EMAIL_REGEX.match(email))

def is_strong_password(password: str) -> tuple[bool, str]:
    if len(password) < 8:
        return False, "8文字以上で入力してください"
    if not re.search(r'[A-Z]', password):
        return False, "大文字を含めてください"
    if not re.search(r'[a-z]', password):
        return False, "小文字を含めてください"
    if not re.search(r'\d', password):
        return False, "数字を含めてください"
    return True, "OK"
```

### Step 20: `signup_form` にバリデーション追加
```python
# Step 11のsignup_form内で使用
from .validators import is_valid_email, is_strong_password

if not is_valid_email(email):
    st.error("有効なメールアドレスを入力してください")
    return None

ok, msg = is_strong_password(password)
if not ok:
    st.error(msg)
    return None
```

### Step 21: セッション永続化対応（ブラウザ再起動後も維持）
```python
# retro_radio/auth/persistent_session.py
import streamlit as st
import json
from pathlib import Path
from ..models.auth_models import User

SESSION_FILE = Path(".streamlit/user_session.json")

def save_session_to_file(user: User):
    data = {
        "id": user.id,
        "email": user.email,
        "plan": user.plan.value,
        "generation_count": user.generation_count,
        # ハッシュは保存しない（セキュリティ）
    }
    SESSION_FILE.parent.mkdir(exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data))

def load_session_from_file() -> dict | None:
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text())
    return None
```

### Step 22: `SessionManager` に永続化統合
```python
# session_manager.py の get_user/set_user を更新
def set_user(self, user: User):
    st.session_state[self.USER_KEY] = user
    save_session_to_file(user)  # 追加

def get_user(self) -> Optional[User]:
    # まずセッションステートから
    user = st.session_state.get(self.USER_KEY)
    if user:
        return user
    # なければファイルから復元試行
    data = load_session_from_file()
    if data:
        # 完全なUserオブジェクト復元はDBから行うため、ここではIDのみ返す
        return data
    return None
```

### Step 23: 認証状態デバッグ表示（開発用）
```python
# auth/ui.py に追加
def render_auth_debug(user: User):
    if st.secrets.get("DEBUG", False):
        with st.sidebar.expander("🔧 デバッグ: 認証状態"):
            st.json({
                "id": user.id,
                "email": user.email,
                "plan": user.plan.value,
                "generation_count": user.generation_count,
                "generation_reset_at": str(user.generation_reset_at) if user.generation_reset_at else None,
            })
```

### Step 24: O1完了チェックリスト
- [ ] ディレクトリ・ファイル構造作成完了
- [ ] パスワードハッシュ（bcrypt）動作確認
- [ ] ログインフォーム表示・送信動作
- [ ] 新規登録フォーム表示・送信動作
- [ ] ログアウト動作
- [ ] サイドバー ユーザー情報表示
- [ ] セッション永続化（ファイルベース）動作
- [ ] 単体テスト通過

---

## 次ステップ: O2 データベース層実装へ
O1で作成した `_get_user_by_email`, `_save_user` のスタブを、O2でSQLAlchemy実装に置換します。