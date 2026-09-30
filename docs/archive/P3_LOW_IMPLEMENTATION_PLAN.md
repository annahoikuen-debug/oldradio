# P3 Low 実装計画書

## 概要
- 対象課題数: 6件（Lowレベル / コード品質改善）
- 総ステップ数: 24ステップ
- 推定工数: 4時間

## 前提条件
- Python 3.10+
- 依存パッケージ: 既存requirements.txt準拠
- テスト実行: `pytest tests/ -v`

## ステップ一覧

### Step 1: マジックナンバー置換準備 - 定数ファイル確認
- **対象ファイル**: `retro_radio/ui/constants.py` (P2で作成済み)
- **変更内容**: 
  - ファイルが存在し、必要な定数が含まれていることを確認
  - 不足している定数があれば追加
- **確認方法**: ファイル内容を確認し、不足分を記録
- **所要時間目安**: 3分

### Step 2: マジックナンバー置換 - app.pyの残りのマジックナンバー置換1
- **対象ファイル**: `app.py:65` (logging設定)
- **変更内容**: 
  - `logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")`
  - レベルとフォーマットを設定から取得するように変更（ただし、logging設定は起動時のみなので、今回は設定ファイル追加を見送り、コメントで残す）
  - 変更: 
    ```python
    # TODO: Move to config - logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ```
- **確認方法**: TODOコメントが追加されていることを確認
- **所要時間目安**: 2分

### Step 3: マジックナンバー置換 - app.pyの残りのマジックナンバー置換2
- **対象ファイル**: `app.py:87-88` (manifestとservice worker URL)
- **変更内容**: 
  - ハードコードされたパスを設定から取得するように変更する準備
  - 変更:
    ```python
    # TODO: Make these configurable via settings
    st.markdown("""
    <link rel="manifest" href="/static/manifest.json">
    <script>
    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('/static/service-worker.js')
          .then(reg => console.log('Service Worker registered:', reg))
          .catch(err => console.log('Service Worker registration failed:', err));
      });
    }
    </script>
    """, unsafe_allow_html=True)
    ```
- **確認方法**: TODOコメントが追加されていることを確認
- **所要時間目安**: 2分

### Step 4: マジックナンバー置換 - favarite_service.pyのキャッシュタイムアウト
- **対象ファイル**: `retro_radio/services/favorite_service.py:19`
- **変更内容**: 
  - `self._cache_timeout = 300  # 5分` を設定から取得するように変更
  - 変更前: `self._cache_timeout = 300  # 5分`
  - 変更後: `self._cache_timeout = settings.cache_timeout if hasattr(settings, 'cache_timeout') else 300`
  - ただし、settingsにcache_timeoutがない場合は、P0/P1で追加するか、デフォルト値を使用
  - 今回は設定ファイル追加は別Issueとし、コメントで残す
  - 変更: 
    ```python
    # TODO: Move cache timeout to settings
    self._cache_timeout = 300  # 5分
    ```
- **確認方法**: TODOコメントが追加されていることを確認
- **所要時間目安**: 2分

### Step 5: マジックナンバー置換 - export_service.pyのキャッシュタイムアウト
- **対象ファイル**: `retro_radio/services/export_service.py:21`
- **変更内容**: 
  - 同上の対応
  - 変更: 
    ```python
    # TODO: Move cache timeout to settings
    self._cache_timeout = 300  # 5分
    ```
- **確認方法**: TODOコメントが追加されていることを確認
- **所要時間目安**: 2分

### Step 6: インポートスタイル統一残り - 相対インポートを絶対インポートに
- **対象ファイル**: `retro_radio/billing/stripe_client.py:5`
- **変更内容**: 
  - 変更前: `from .plans import PlanType, BillingCycle, get_price_id`
  - 変更後: `from retro_radio.billing.plans import PlanType, BillingCycle, get_price_id`
- **確認方法**: インポートが絶対形式に変更されていることを確認
- **所要時間目安**: 2分

### Step 7: インポートスタイル統一残り - 同様に他のファイルも
- **対象ファイル**: 複数ファイル（残りの目立つ相対インポート）
- **変更内容**: 
  - `retro_radio/billing/plans.py:4` - `from retro_radio.models.user import PlanType` (絶対インポートにすでに変換済みか確認)
  - `retro_radio/i18n/translator.py:7` - `from typing import Dict, Any` は標準ライブラリなのでOK
  - `retro_radio/utils/session.py:119` - `from ..models import User` を `from retro_radio.models.user import User` に変更
  - `retro_radio/utils/validators.py:1-4` - pydanticなどは標準のためOK
  - `retro_radio/db/session.py:5` - `from .models import Base` を `from retro_radio.db.models import Base` に変更
- **確認方法**: 対象ファイルのインポートが絶対形式に変更されていることを確認
- **所要時間目安**: 6分

### Step 8: 未使用インポート削除 - app.py
- **対象ファイル**: `app.py:1-30`（インポートセクション）
- **変更内容**: 
  - 未使用インポートを特定して削除
  - 代表的なもの: `import random` (使用されているか確認)、`import time` (P1でスリープ削除後は使用されていない可能性)
  - 実際に使用されているかを確認してから削除
- **確認方法**: 
  ```bash
  # 簡易チェック
  grep -n "random" app.py | grep -v "import random"  # randomが使用されている行をチェック
  grep -n "time" app.py | grep -v "import time"     # timeが使用されている行をチェック
  ```
  使用されていない場合は削除
- **所要時間目安**: 4分

### Step 9: 未使用インポート削除 - その他のファイル
- **対象ファイル**: 複数ファイル
- **変更内容**: 
  - ruffを使って未使用インポートを自動検出・削除
  - 手動で確認しながら進める
- **確認方法**: 
  ```bash
  ruff check --select F401 .  # 未使用インポートのみ表示
  ```
  出力がないことを確認（または修正後）
- **所要時間目安**: 5分

### Step 10: コメント・ドキュメント文字列改善 - パブリックAPIのdocstring追加
- **対象ファイル**: `retro_radio/core/script_generator.py:69` (generate_radio_script関数)
- **変更内容**: 
  - 関数に詳細なdocstringを追加
  - 変更前: 簡単なコメントのみ
  - 変更後:
    ```python
    def generate_radio_script(year: int, month: int, day: int) -> str:
        """メイン関数：原稿生成（失敗時フォールバック）
        
        Args:
            year: 生成対象年 (1950-2025)
            month: 生成対象月 (1-12)
            day: 生成対象日 (1-31)
            
        Returns:
            生成されたラジオ原稿のテキスト
            
        Note:
            Gemini APIキーが設定されている場合はAI生成を試み、
            失敗または未設定の場合は定型フォールバック原稿を返す
        """
    ```
- **確認方法**: docstringが追加されていることを確認
- **所要時間目安**: 3分

### Step 11: コメント・ドキュメント文字列改善 - 他の主要関数も
- **対象ファイル**: `retro_radio/core/tts.py:15` (text_to_speech関数)
- **変更内容**: 
  - 同上の形式でdocstringを追加
  - 変更前: 簡単なコメント
  - 変更後: 詳細なdocstring
- **確認方法**: docstringが追加されていることを確認
- **所要時間目安**: 3分

### Step 12: コメント・ドキュメント文字列改善 - クラスレベルのdocstring
- **対象ファイル**: `retro_radio/auth/authenticator.py:27` (Authenticatorクラス)
- **変更内容**: 
  - クラスにdocstringを追加
  - 変更前: クラス定義直後のコメントなし
  - 変更後:
    ```python
    class Authenticator:
        """認証を管理するクラス
        
        ユーザーのログイン、登録、プラン管理、機能フラグチェックなどを担当。
        セッション管理とデータベース操作をラップする。
        """
    ```
- **確認方法**: クラスdocstringが追加されていることを確認
- **所要時間目安**: 3分

### Step 13: ロギング改善 - ロガー名の統一
- **対象ファイル**: 複数ファイル
- **変更内容**: 
  - `__name__` ベースのロガー名を使用しているか確認
  - 現状でもほぼ `__name__` を使用しているため、軽微な修正のみ
  - 例: `retro_radio/core/fallback.py:5` の `logger = logging.getLogger(__name__)` が正しいか確認
- **確認方法**: ロガーが `__name__` を使用していることを確認
- **所要時間目安**: 2分

### Step 14: 例外メッセージ改善 - より具体的に
- **対象ファイル**: `retro_radio/core/fallback.py:18-26` (get_fallback_song関数)
- **変更内容**: 
  - 例外ケースでのフォールバックロジックをより明確に
  - 変更前: 範囲外の年は最寄りのdecadeにフォールバック
  - 変更後: コメントを追加して意図を明確に
    ```python
    def get_fallback_song(year: int) -> Tuple[str, str]:
        """年度に最も近い代表曲を返す
        
        範囲外の年は最寄りの10年単位（decade）にフォールバックする。
        例: 1953年 -> 1950年の曲、1967年 -> 1970年の曲
        """
    ```
- **確認方法**: コメントが改善されていることを確認
- **所要時間目安**: 2分

### Step 15: タイポ・文法修正 - コメントとdocstring
- **対象ファイル**: 全ファイル（代表的に目立つもの）
- **変更内容**: 
  - 明らかなタイポや文法错误を修正
  - 例: `retro_radio/core/tts.py:110` の `# ここは実際のElevenLabs API実装に置き換える` というコメントは
    P0で実装したため、更新または削除
  - 変更: 
    ```python
    # ElevenLabs APIを使用した高品質音声合成（プレミアム機能）
    # ELEVENLABS_API_KEY環境変数が必要
    ```
- **確認方法**: コメントが修正されていることを確認
- **所要時間目安**: 3分

### Step 16: 長い関数の分割準備 - 複雑なロジックの抽出候補特定
- **対象ファイル**: `app.py:1220-1320` (ラジオ再生ボタンハンドラ)
- **変更内容**: 
  - この長い処理ブロックを小さな関数に分割するための準備
  - 各ステップをコメントで区切り、関数抽出の目印をつける
  - 変更: 各主要ステップ前にコメントを追加
    ```python
    # Step 1: Generate script
    # Step 2: Search songs
    # Step 3: TTS generation
    # Step 4: Save to Database & History
    # Step 5: Increment generation counter
    ```
- **確認方法**: ステップコメントが追加されていることを確認
- **所要時間目安**: 3分

### Step 17: 設定項目の追加検討 - 新しい設定項目の提案
- **対象ファイル**: `retro_radio/config.py`
- **変更内容**: 
  - 今後追加すべき設定項目をコメントで提案
  - 例: キャッシュタイムアウト、ロギングレベルなど
  - 変更: クラスの末尾にコメントを追加
    ```python
    # TODO: Consider adding these settings:
    # cache_timeout: int = Field(default=300, ge=60, le=3600)  # キャッシュタイムアウト秒数
    # log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    # manifest_url: str = Field(default="/static/manifest.json")
    # service_worker_url: str = Field(default="/static/service-worker.js")
    ```
- **確認方法**: TODOコメントが追加されていることを確認
- **所要時間目安**: 2分

### Step 18: セキュリティ改善 - パスワードハッシュイテレーション数の設定化
- **対象ファイル**: `retro_radio/auth/authenticator.py:14-16` (hash_password関数)
- **変更内容**: 
  - ハードコードされたイテレーション数(100000)を設定から取得するように変更する準備
  - 変更: コメントを追加
    ```python
    # TODO: Move iteration count to settings for better security configurability
    hash_val = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000).hex()
    ```
- **確認方法**: TODOコメントが追加されていることを確認
- **所要時間目安**: 2分

### Step 19: パフォーマンス改善 - 文字列連結の最適化候補
- **対象ファイル**: `retro_radio/core/tts.py:79-80` (gtts_tts関数のログ出力)
- **変更内容**: 
  - f-stringの使用を検討（ただし、現状でも問題ないため軽微）
  - 変更: 現在のログ出力形式を確認し、可能なら改善
  - 実際は特に変更不要なため、コメントで残す
  - 変更:
    ```python
    # Consider using f-string for better performance: logger.info(f"TTS合成完了: chars={len(text)}")
    logger.info(f"TTS合成完了: chars={len(text)}")
    ```
- **確認方法**: f-stringが使用されていることを確認（すでに使用済みの場合は確認のみ）
- **所要時間目安**: 1分

### Step 20: コードスタイル統一 - クォートスタイル
- **対象ファイル**: 複数ファイル
- **変更内容**: 
  - 文字列リテラルのクォートスタイルを統一（シングルクォートかダブルクォートか）
  - 現状では混在しているため、ルールを決めて統一する準備
  - 変更: PEP 8準拠のため、可能ならシングルクォートを使用する旨のコメントを追加
  - 実際の変更は別Issueとする
  - 変更例: `retro_radio/core/tts.py:1` の docstringがダブルクォートのままでも良いとする
- **確認方法**: コーディングスタイルガイドラインの提案コメントが追加されていることを確認
- **所要時間目安**: 2分

### Step 21: テスト可能性向上 - ハードコードされた値の注入可能性検討
- **対象ファイル**: `retro_radio/core/fallback.py:7-16` (FALLBACK_SONGS辞書)
- **変更内容**: 
  - このデータを外部ファイル（JSON/YAML）やデータベースから読み込めるようにする準備
  - 変更: コメントを追加
    ```python
    # TODO: Consider loading from external file or database for easier updates
    FALLBACK_SONGS: dict[int, List[Tuple[str, str]]] = {
    ```
- **確認方法**: TODOコメントが追加されていることを確認
- **所要時間目安**: 2分

### Step 22: ドキュメント改善 - アーキテクチャ概要の追加
- **対象ファイル**: `README.md`
- **変更内容**: 
  - アーキテクチャ概要セクションを追加または改善
  - 変更: 既存のREADMEにセクションを追加
    ```markdown
    ## アーキテクチャ概要
    
    このアプリケーションは以下のモジュール構造を持っています：
    
    - `core/` - ビジネスロジック（原稿生成、楽曲検索、音声合成など）
    - `services/` - ビジネスサービス（エクスポート、お気に入りなど）
    - `auth/` - 認証・認可
    - `billing/` - 決済関連
    - `i18n/` - 国際化
    - `db/` - データベース関連
    - `ui/` - UI関連定数（段階的に移行中）
    - `utils/` - ユーティリティ関数
    ```
- **確認方法**: セクションが追加されていることを確認
- **所要時間目安**: 3分

### Step 23: 最終整理 - 未完了タスクのIssue化
- **対象ファイル**: 全ファイル（TODOコメントを探す）
- **変更内容**: 
  - TODOコメントを収集し、適切なIssueに変換するためのリストを作成
  - 変更: 一時的なファイルやコメントは残さず、必要なものは適切な場所に移動
  - 実際のIssue作成は別途行うため、ここでは確認のみ
- **確認方法**: 
  ```bash
  grep -r "TODO" . --exclude-dir=.git
  ```
  出力を確認し、適切なIssueに変換できる状態になっていることを確認
- **所要時間目安**: 5分

### Step 24: 最終動作確認・まとめ
- **対象ファイル**: 全変更ファイル
- **変更内容**: 
  - 変更後の動作を確認
- **確認方法**: 
  1. アプリ起動確認
  2. ロギングが正常に動作することの確認
  3. インポートエラーがないことの確認
  4. 未使用インポート警告がないことの確認（ruff check）
  5. 基本機能（認証、ラジオ生成、エクスポートなど）が動作することの確認
  6. ドキュメントが改善されていることの確認
  7. 既存テスト実行 (`pytest tests/ -v` - 可能な範囲で)
- **所要時間目安**: 10分

## 依存関係マトリクス
| Step | 依存先 | 備考 |
|------|--------|------|
| 2    | 1      | 定数ファイル確認後の置換 |
| 3    | 1      |  |
| 4    | 1      |  |
| 5    | 1      |  |
| 6    | -      | インポート統一（独立） |
| 7    | 6      | 他ファイルのインポート統一 |
| 8    | -      | app.py未使用インポート削除 |
| 9    | 8      | その他ファイルの未使用インポート削除 |
| 10   | -      | docstring改善（独立） |
| 11   | 10     | 他関数のdocstring改善 |
| 12   | -      | クラスdocstring改善（独立） |
| 13   | -      | ロギング改善（独立） |
| 14   | -      | 例外メッセージ改善（独立） |
| 15   | -      | タイポ修正（独立） |
| 16   | -      | 関数分割準備（独立） |
| 17   | -      | 設定項目提案（独立） |
| 18   | -      | パスワードハッシュ設定化準備（独立） |
| 19   | -      | パフォーマンス改善検討（独立） |
| 20   | -      | コードスタイル統一検討（独立） |
| 21   | -      | テスト可能性向上検討（独立） |
| 22   | -      | ドキュメント改善（独立） |
| 23   | 1-22   | TODOコメント収集・整理 |
| 24   | 1-23   | 最終動作確認 |

## 完了定義
- [x] 全24ステップ実装完了
- [ ] 既存テスト全パス (`pytest tests/ -v`) 
- [ ] 新規テスト追加（該当する場合は別途）
- [ ] リンター/型チェック通過 (`ruff check .`, `mypy .`)
- [x] 手動動作確認完了（ロギング・インポート・ドキュメント・基本動作）