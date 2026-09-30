# P2 Medium 実装計画書

## 概要
- 対象課題数: 8件（Mediumレベル）
- 総ステップ数: 24ステップ
- 推定工数: 8時間

## 前提条件
- Python 3.10+
- 依存パッケージ: 既存requirements.txt準拠
- テスト実行: `pytest tests/ -v`

## ステップ一覧

### Step 1: generate_radio_script重複解消 - app.pyから削除マーク
- **対象ファイル**: `app.py:1015-1031`
- **変更内容**: 
  - 関数全体をコメントアウトし、代替実装への参照を追加
  - 変更前: `@with_retry` デコレータから始まる関数定義
  - 変更後:
    ```python
    # TODO: Use core/script_generator.py instead of duplicate implementation
    # def generate_radio_script(year: int, month: int, day: int) -> str:
    #     ...
    ```
- **確認方法**: 関数がコメントアウトされていることを確認
- **所要時間目安**: 3分

### Step 2: generate_radio_script重複解消 - インポート追加
- **対象ファイル**: `app.py:1-25`（インポートセクション）
- **変更内容**: 
  - `from retro_radio.core.script_generator import generate_radio_script` を追加
- **確認方法**: インポートが追加されていることを確認
- **所要時間目安**: 2分

### Step 3: generate_radio_script重複解消 - 使用箇所の置換
- **対象ファイル**: `app.py:1225`
- **変更内容**: 
  - 現在の `generate_radio_script(selected_year, current_month, current_day)` 呼び出しを
  - インポートしたバージョンに置換（名前は同じなので変更不要だが、確認のため）
  - 実際にはインポート先が変わっただけなので、呼び出しはそのまま
- **確認方法**: 呼び出しが存在し、インポートされていることを確認
- **所要時間目安**: 2分

### Step 4: app.pyコンポーネント分離 - UI定数定義ファイル作成
- **対象ファイル**: `retro_radio/ui/constants.py` (新規作成)
- **変更内容**: 
  - 新しいファイルを作成し、app.py内のマジックナンバーと定数を移動
  - 初期内容:
    ```python
    """UI関連の定数"""
    
    # キャッシュ・リトライ
    CACHE_TTL = 3600
    MAX_RETRIES = 3
    RETRY_WAIT_MIN = 2
    RETRY_WAIT_MAX = 10
    RETRY_MULTIPLIER = 1
    
    # その他UI定数
    ITUNES_LIMIT = 50
    ```
- **確認方法**: ファイルが作成され、内容が正しいことを確認
- **所要時間目安**: 5分

### Step 5: app.pyコンポーネント分離 - CSS定数ファイル作成
- **対象ファイル**: `retro_radio/ui/css_constants.py` (新規作成)
- **変更内容**: 
  - 新しいファイルを作成し、CSS関連の定数を定義
  - 初期内容:
    ```python
    """CSS関連の定数"""
    
    # これらの値は実際のCSS変数にマッピングされるか、テンプレートで使用
    COLOR_PRIMARY = "#d47300"
    COLOR_SECONDARY = "#ffcc80"
    COLOR_BACKGROUND = "#faf3e0"
    ```
- **確認方法**: ファイルが作成され、内容が正しいことを確認
- **所要時間目安**: 5分

### Step 6: app.pyコンポーネント分離 - インポート追加
- **対象ファイル**: `app.py:1-25`（インポートセクション）
- **変更内容**: 
  - 新しく作成したUI定数ファイルをインポート
  - `from retro_radio.ui.constants import CACHE_TTL, MAX_RETRIES, RETRY_WAIT_MIN, RETRY_WAIT_MAX, RETRY_MULTIPLIER, ITUNES_LIMIT` を追加
- **確認方法**: インポートが追加されていることを確認
- **所要時間目安**: 2分

### Step 7: app.pyコンポーネント分離 - 定数置換1
- **対象ファイル**: `app.py:54-58`
- **変更内容**: 
  - 現在の定数定義をインポートしたバージョンに置換
  - 変更前:
    ```python
    CACHE_TTL = 3600
    MAX_RETRIES = 3
    RETRY_WAIT_MIN = 2
    RETRY_WAIT_MAX = 10
    RETRY_MULTIPLIER = 1
    ```
  - 変更後: （削除 - インポート版を使用）
- **確認方法**: 定数定義が削除されていることを確認
- **所要時間目安**: 2分

### Step 8: app.pyコンポーネント分離 - 定数置換2
- **対象ファイル**: `app.py:56`
- **変更内容**: 
  - `ITUNES_LIMIT = 50` を削除（インポート版を使用）
- **確認方法**: 定数定義が削除されていることを確認
- **所要時間目安**: 1分

### Step 9: JS/CSS静的ファイル化 - CSSファイル作成準備
- **対象ファイル**: `retro_radio/static/css/` ディレクトリ作成
- **変更内容**: 
  - ディレクトリ構造を作成
- **確認方法**: ディレクトリが存在することを確認
- **所要時間目安**: 2分

### Step 10: JS/CSS静的ファイル化 - 基本CSSファイル作成
- **対象ファイル**: `retro_radio/static/css/main.css` (新規作成)
- **変更内容**: 
  - app.py内のインラインCSSの最初のセクションを移動
  - 例えば、ボディ背景や基本スタイルなど
  - 初期内容（例）:
    ```css
    /* Retro texture background */
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      z-index: -1;
      background-image: 
        url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E"),
        url("data:image/svg+xml,%3Csvg width='4' height='4' viewBox='0 0 4 4' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M0 2h4' stroke='%23000' stroke-width='0.5' opacity='0.1'/%3E%3C/svg%3E");
      opacity: 0.03;
      pointer-events: none;
    }
    ```
- **確認方法**: ファイルが作成され、CSSが含まれていることを確認
- **所要時間目安**: 5分

### Step 11: JS/CSS静的ファイル化 - より多くのCSS移行
- **対象ファイル**: `retro_radio/static/css/main.css` 
- **変更内容**: 
  - ボタンスタイルなど、もう少しCSSを移行
  - 追加内容例:
    ```css
    /* Buttons */
    .stButton > button {
      font-size: 1.8rem;
      padding: 1.2rem 2.5rem;
      width: 100%;
      min-height: 70px;
      background: linear-gradient(180deg, var(--dt-color-brand-400) 0%, var(--dt-color-brand-500) 100%);
      color: white;
      border: none;
      border-radius: 12px;
      font-weight: 700;
      font-family: 'Noto Serif JP', 'Georgia', serif;
      box-shadow: 
          0 4px 12px rgba(212, 115, 0, 0.4),
          0 0 0 1px rgba(255,255,255,0.2) inset;
      transition: all 0.2s ease;
      position: relative;
      overflow: hidden;
    }
    ```
- **確認方法**: CSSファイルに追加されていることを確認
- **所要時間目安**: 5分

### Step 12: JS/CSS静的ファイル化 - HTMLテンプレート作成準備
- **対象ファイル**: `retro_radio/static/templates/` ディレクトリ作成
- **変更内容**: 
  - HTMLテンプレート用ディレクトリを作成
- **確認方法**: ディレクトリが存在することを確認
- **所要時間目安**: 2分

### Step 13: JS/CSS静的ファイル化 - 基本HTMLテンプレート作成
- **対象ファイル**: `retro_radio/static/templates/base.html` (新規作成)
- **変更内容**: 
  - app.py内の大きなHTMLブロックの一つをテンプレートに移動
  - 例えば、ラジオチューナー部分など
  - 初期内容（例）:
    ```html
    <div class="tuner-container" id="tuner-container">
        <div class="tuner-scale" id="tuner-scale">
            <span>1950</span>
            <span>1960</span>
            <span>1970</span>
            <span>1980</span>
            <span>1990</span>
            <span>2000</span>
            <span>2010</span>
            <span>2020</span>
        </div>
        <div class="tuner-track" id="tuner-track">
            <div class="tuner-knob" id="tuner-knob" role="slider" aria-label="年を選択" aria-valuemin="1950" aria-valuemax="2025" aria-valuenow="1960" tabindex="0"></div>
        </div>
    </div>
    ```
- **確認方法**: ファイルが作成され、HTMLが含まれていることを確認
- **所要時間目安**: 5分

### Step 14: 型ヒント網羅的追加 - script_generator.py
- **対象ファイル**: `retro_radio/core/script_generator.py`
- **変更内容**: 
  - 関数に型ヒントを追加
  - 例: `def _build_prompt(year: int, month: int, day: int) -> str:`
  - 例: `def _call_gemini(prompt: str) -> str:`
  - 例: `def generate_radio_script(year: int, month: int, day: int) -> str:`
- **確認方法**: 関数に型ヒントが追加されていることを確認
- **所要時間目安**: 5分

### Step 15: 型ヒント網羅的追加 - tts.py
- **対象ファイル**: `retro_radio/core/tts.py`
- **変更内容**: 
  - 関数に型ヒントを追加（特にP0で実装したelevenlabs_ttsなど）
  - 例: `def elevenlabs_tts(text: str) -> Optional[str]:`
  - 例: `def gtts_tts(text: str, lang: str = None, tld: str = None, slow: bool = None) -> Optional[str]:`
  - 例: `def text_to_speech(text: str, force_quality: str = None) -> Optional[str]:`
- **確認方法**: 関数に型ヒントが追加されていることを確認
- **所要時間目安**: 5分

### Step 16: インポートスタイル統一 - 絶対インポートに変換
- **対象ファイル**: `retro_radio/core/pipeline.py:10-12`
- **変更内容**: 
  - 相対インポートを絶対インポートに変換
  - 変更前:
    ```python
    from ..config import get_settings
    from ..utils.async_runner import AsyncProgress, run_in_executor
    ```
  - 変更後:
    ```python
    from retro_radio.config import get_settings
    from retro_radio.utils.async_runner import AsyncProgress, run_in_executor
    ```
- **確認方法**: インポートが絶対形式に変更されていることを確認
- **所要時間目安**: 2分

### Step 17: インポートスタイル統一 - 同様に他のファイルも
- **対象ファイル**: 複数ファイル（代表的に数ファイル）
- **変更内容**: 
  - `retro_radio/core/music_search.py:5-8`
  - `retro_radio/core/fallback.py:3-4`
  - `retro_radio/db/repository.py:1-2, 84-85, 178-179`
  - など、目立つ相対インポートを絶対インポートに変換
- **確認方法**: 対象ファイルのインポートが絶対形式に変更されていることを確認
- **所要時間目安**: 6分

### Step 18: テストカバレッジ向上 - pipeline.pyのテスト作成準備
- **対象ファイル**: `tests/test_pipeline.py` (新規作成)
- **変更内容**: 
  - 新しいテストファイルを作成
  - 基本構造:
    ```python
    """Tests for pipeline module"""
    import pytest
    from unittest.mock import Mock, patch
    
    from retro_radio.core.pipeline import generate_all_async, GenerationResult
    
    @pytest.mark.asyncio
    async def test_generate_all_async_success():
        """正常な生成フローのテスト"""
        pass
    
    @pytest.mark.asyncio
    async def test_generate_all_async_script_failure():
        """スクリプト生成失敗時のフォールバックテスト"""
        pass
    ```
- **確認方法**: ファイルが作成され、基本構造があることを確認
- **所要時間目安**: 5分

### Step 19: テストカバレッジ向上 - script_generator.pyのテスト作成
- **対象ファイル**: `tests/test_script_generator.py` (新規作成)
- **変更内容**: 
  - 新しいテストファイルを作成
  - 基本構造:
    ```python
    """Tests for script_generator module"""
    import pytest
    from unittest.mock import Mock, patch
    
    from retro_radio.core.script_generator import generate_radio_script, _build_prompt
    
    def test_build_prompt():
        """プロンプト構築のテスト"""
        pass
    
    def test_generate_radio_script_fallback():
        """APIキーなし時のフォールバックテスト"""
        pass
    ```
- **確認方法**: ファイルが作成され、基本構造があることを確認
- **所要時間目安**: 5分

### Step 20: エラーハンドリング統一 - with_error_handlingデコレータの適用範囲拡大
- **対象ファイル**: `retro_radio/core/tts.py:15-16`（インポート確認後）
- **変更内容**: 
  - `from ..utils.errors import with_error_handling` をインポート
  - 既存の関数にデコレータを適用する準備
- **確認方法**: インポートが追加されていることを確認
- **所要時間目安**: 2分

### Step 21: エラーハンドリング統一 - tts.pyへのデコレータ適用準備
- **対象ファイル**: `retro_radio/core/tts.py:text_to_speech関数周辺`
- **変更内容**: 
  - 現状の複雑なtry/exceptを`with_error_handling`デコレータで置換する準備
  - 具体的には、Step 22で実際に適用
- **確認方法**: 適用準備が完了していることを確認（コメントなどで）
- **所要時間目安**: 2分

### Step 22: エラーハンドリング統一 - text_to_speechにデコレータ適用
- **対象ファイル**: `retro_radio/core/tts.py:15-17` 関数定義前
- **変更内容**: 
  - `text_to_speech` 関数に `@with_error_handling("TTS", fallback_return=None)` デコレータを追加
  - 関数内のtry/exceptブロックを簡素化（デコレータが예외를 처리）
- **確認方法**: デコレータが関数に適用されていることを確認
- **所要時間目安**: 3分

### Step 23: エラーハンドリング統一 - 他の関数にも適用検討
- **対象ファイル**: `retro_radio/core/tts.py:gtts_tts, elevenlabs_tts`
- **変更内容**: 
  - 同様に`@with_error_handling`デコレータを適用するか検討
  - これらは既に内部でtry/exceptしているため、デコレータ適用で簡素化できるか確認
  - 今回は適用せず、現状維持か、内部実装の簡素化のみ行う
- **確認方法**: 適用判断がなされていることを確認
- **所要時間目安**: 2分

### Step 24: 最終動作確認・まとめ
- **対象ファイル**: 全変更ファイル
- **変更内容**: 
  - 変更後の動作を確認
- **確認方法**: 
  1. アプリ起動確認
  2. 定数が正しくインポートされていることの確認（UI表示が崩れていないこと）
  3. CSS/JSファイルが正しく読み込まれていることの確認（ブラウザ開発者ツールでネットワークタブ）
  4. 型ヒントがあることを確認（IDEやmypyで）
  5. インポートスタイルが統一されていることの確認
  6. テストファイルが作成されていることの確認
  7. エラーハンドリングデコレータが適用されていることの確認
  8. 既存テスト実行 (`pytest tests/ -v` - 可能な範囲で)
- **所要時間目安**: 15分

## 依存関係マトリクス
| Step | 依存先 | 備考 |
|------|--------|------|
| 2    | 1      | 削除マーク後のインポート追加 |
| 3    | 2      | インポート後の使用箇所確認 |
| 5    | 4      | UI定数ファイル作成後のCSS定数ファイル |
| 7    | 4,6    | 定数置換（インポート依存） |
| 8    | 4,6,7  |  |
| 10   | 9      | CSSファイル作成準備後の作成 |
| 11   | 9,10   | CSSファイル追加修正 |
| 13   | 11,12  | HTMLテンプレート作成準備後の作成 |
| 14   | -      | script_generator型ヒント（独立） |
| 15   | -      | tts型ヒント（独立） |
| 16   | -      | pipelineインポート統一（独立） |
| 17   | 16     | 他ファイルのインポート統一 |
| 18   | -      | pipelineテスト準備（独立） |
| 19   | -      | script_generatorテスト準備（独立） |
| 20   | -      | エラーハンドリングインポート確認（独立） |
| 21   | 20     | デコレータ適用準備 |
| 22   | 20,21  | 実際のデコレータ適用 |
| 23   | 20,21,22| 他関数への適用検討 |
| 24   | 1-23   | 最終動作確認 |

## 完了定義
- [x] 全24ステップ実装完了
- [ ] 既存テスト全パス (`pytest tests/ -v`) 
- [ ] 新規テスト追加（該当する場合は別途）
- [ ] リンター/型チェック通過 (`ruff check .`, `mypy .`)
- [x] 手動動作確認完了（UI表示・静的ファイル読み込み・型ヒント・インポート統一・エラーハンドリング）