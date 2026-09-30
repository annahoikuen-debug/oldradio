# P0 Critical 実装計画書

## 概要
- 対象課題数: 4件（Criticalレベル）
- 総ステップ数: 24ステップ
- 推定工数: 2.5時間

## 前提条件
- Python 3.10+
- 依存パッケージ: 既存requirements.txt準拠
- テスト実行: `pytest tests/ -v`

## ステップ一覧

### Step 1: XSS脆弱性修正 - sanitize_text関数のインポート
- **対象ファイル**: `app.py`
- **変更内容**: 
  - 1-20行目のインポートセクションに `from retro_radio.utils.validators import sanitize_text` を追加
- **確認方法**: ファイル保存後に構文エラーがないことを確認
- **所要時間目安**: 2分

### Step 2: XSS脆弱性修正 - script_text変数のサニタイズ
- **対象ファイル**: `app.py:1325-1339`
- **変更内容**: 
  - 1327行の `{script_text.replace(chr(10), '<br>')}` を `{sanitize_text(script_text).replace(chr(10), '<br>')}` に変更
- **確認方法**: 変更後、ファイルが保存でき、Python構文エラーがないこと
- **所要時間目安**: 3分

### Step 3: XSS脆弱性修正 - サニタイズ結果の確認用ログ追加（デバッグ用）
- **対象ファイル**: `app.py:1320-1325`付近
- **変更内容**: 
  - `current_gen = st.session_state.get("current_generation")` の直後にログ追加:
    ```python
    if current_gen:
        logger.debug(f"Rendering script for user {user.id if user else 'anon'}")
    ```
- **確認方法**: ログが追加されていることを目視確認
- **所要時間目安**: 2分

### Step 4: localStorage APIキー保存削除 - JavaScript削除1
- **対象ファイル**: `app.py:743-763`
- **変更内容**: 
  - 751-759行の `loadApiKey()` 関数定義を完全に削除
- **確認方法**: 該当行が削除されていることを目視確認
- **所要時間目安**: 2分

### Step 5: localStorage APIキー保存削除 - JavaScript削除2
- **対象ファイル**: `app.py:764-766`
- **変更内容**: 
  - `loadApiKey();` 呼び出し行を削除
- **確認方法**: 該当行が削除されていることを目視確認
- **所要時間目安**: 1分

### Step 6: localStorage APIキー保存削除 - カスタムイベント削除
- **対象ファイル**: `app.py:761-763`
- **変更内容**: 
  - `window.dispatchEvent(new CustomEvent('geminiApiKeyLoaded', {detail: {key: key}}));` 行を削除
- **確認方法**: 該当行が削除されていることを目視確認
- **所要時間目安**: 1分

### Step 7: localStorage APIキー保存削除 - APIキー保存JavaScript削除
- **対象ファイル**: `app.py:792-796`
- **変更内容**: 
  - 793-795行の 
    ```javascript
    <script>
    localStorage.setItem('gemini_api_key', '{api_key_input.strip()}');
    </script>
    ``` 
    を削除
- **確認方法**: 該当ブロックが削除されていることを目視確認
- **所要時間目安**: 2分

### Step 8: localStorage APIキー保存削除 - スキップボタンのロジック変更
- **対象ファイル**: `app.py:801-804`
- **変更内容**: 
  - 「スキップ（フォールバックで利用）」ボタンの処理を変更
  - 現在: `st.session_state.gemini_api_key = ""` 
  - 変更後も同じ（APIキー空文字列でフォールバック動作）
- **確認方法**: 変更不要だが、該当箇所が存在することを確認
- **所要時間目安**: 1分

### Step 9: MagicMock削除 - テスト用ラップ削除1
- **対象ファイル**: `retro_radio/auth/authenticator.py:48-52`
- **変更内容**: 
  - 48-52行の 
    ```python
    # Wrap session_mgr methods with mock spy for unit tests
    orig_set_user = self.session_mgr.set_user
    orig_clear_user = self.session_mgr.clear_user
    self.session_mgr.set_user = MagicMock(side_effect=orig_set_user)
    self.session_mgr.clear_user = MagicMock(side_effect=orig_clear_user)
    ```
    を完全に削除
- **確認方法**: 該当ブロックが削除されていることを目視確認
- **所要時間目安**: 3分

### Step 10: MagicMock削除 - MagicMockインポート削除
- **対象ファイル**: `retro_radio/auth/authenticator.py:25`
- **変更内容**: 
  - `from unittest.mock import MagicMock` を削除
- **確認方法**: インポート行が削除されていることを目視確認
- **所要時間目安**: 1分

### Step 11: ElevenLabs実装 - 環境変数チェック追加
- **対象ファイル**: `retro_radio/core/tts.py:108-125`
- **変更内容**: 
  - `elevenlabs_tts` 関数の実装を本物に変更
  - 現在のスタブを以下に置換:
    ```python
    def elevenlabs_tts(text: str) -> Optional[str]:
        """ElevenLabs APIを使用した高品質音声合成（プレミアム機能）"""
        try:
            api_key = os.environ.get("ELEVENLABS_API_KEY")
            if not api_key:
                logger.warning("ELEVENLABS_API_KEYが設定されていません。gTTSにフォールバックします。")
                return gtts_tts(text)
            
            # デフォルトボイス（Rachel）を使用
            voice_id = "21m00Tcm4TlvDq8ikWAM"  # Rachel
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            
            headers = {
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": api_key
            }
            
            data = {
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.5
                }
            }
            
            response = requests.post(url, json=data, headers=headers)
            response.raise_for_status()
            
            # 音声コンテンツを一時ファイルに保存
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            temp_file.write(response.content)
            temp_file.close()
            
            logger.info(f"ElevenLabs TTS完了: chars={len(text)}")
            return temp_file.name
        except Exception as e:
            logger.error(f"ElevenLabs TTS失敗: {e}")
            # フォールバックとして標準品質を使用
            return gtts_tts(text)
    ```
- **確認方法**: 関数が置換され、構文エラーがないことを確認
- **所要時間目安**: 8分

### Step 12: ElevenLabs実装 - 必要なインポート追加
- **対象ファイル**: `retro_radio/core/tts.py:1-13`
- **変更内容**: 
  - インポートセクションに `import requests` を追加（既にある場合はスキップ）
  - `import tempfile` は既にある
- **確認方法**: `requests` がインポートされていることを確認
- **所要時間目安**: 2分

### Step 13: XSS修正 - 代替案（Streamlitネイティブ）検証用コード追加
- **対象ファイル**: `app.py:1320-1325`付近
- **変更内容**: 
  - デバッグ用にコメントアウトで代替実装を追加
    ```python
    # 代替案: Streamlitネイティブ描画（XSS安全）
    # st.text(current_gen.get("script", ""))
    ```
- **確認方法**: コメントアウトで追加されていることを確認
- **所要時間目安**: 2分

### Step 14: localStorage削除 - APIキー取得ロジック簡略化
- **対象ファイル**: `app.py:807-810`
- **変更内容**: 
  - 現在の複雑なtry/exceptブロックを簡素化
  - 変更前:
    ```python
    try:
        GEMINI_API_KEY = st.session_state.get("gemini_api_key", "") or os.getenv("GEMINI_API_KEY", "")
    except Exception:
        GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    ```
  - 変更後:
    ```python
    GEMINI_API_KEY = st.session_state.get("gemini_api_key", "") or os.getenv("GEMINI_API_KEY", "")
    ```
- **確認方法**: 簡素化されたコードになっていることを確認
- **所要時間目安**: 3分

### Step 15: XSS修正 - サニタイズ関数のインポート確認
- **対象ファイル**: `app.py:1-25`（インポートセクション）
- **変更内容**: 
  - `from retro_radio.utils.validators import sanitize_text` が追加されていることを再確認
  - インポート順序を標準化（標準ライブラリ → サードパーティ → ローカル）
- **確認方法**: インポートが存在し、適切な位置にあることを確認
- **所要時間目安**: 2分

### Step 16: MagicMock削除 - テスト互換性のための代替案検討
- **対象ファイル**: `retro_radio/auth/authenticator.py:48-52`（削除済み場所）
- **変更内容**: 
  - テストで必要な場合は、テストファイル側でパッチを当てることをコメントで残す
  - 削除場所に以下を追加:
    ```python
    # Note: For testing, patch 'retro_radio.auth.authenticator.Authenticator.session_mgr' 
    # directly in test files instead of using MagicMock in production code
    ```
- **確認方法**: コメントが追加されていることを確認
- **所要時間目安**: 2分

### Step 17: ElevenLabs実装 - エラーケースのフォールバック動作確認
- **対象ファイル**: `retro_radio/core/tts.py:108-125`（ElevenLabs関数）
- **変更内容**: 
  - エラー時に `st.warning` でユーザー通知を追加（オプション）
  - 現在の実装でも十分だが、念のためフォールバック時に警告を出すように強化
  - 例外ハンドラ内に追加:
    ```python
    # ユーザーへの通知はStreamlitコンテキストでのみ
    try:
        import streamlit as st
        st.warning("高品質音声の生成に失敗しました。標準品質で続行します。")
    except ImportError:
        pass  # Streamlit以外のコンテキストでは無視
    ```
- **確認方法**: フォールバックパスに警告出力コードがあることを確認
- **所要時間目安**: 3分

### Step 18: 全体構文チェック
- **対象ファイル**: `app.py`, `retro_radio/auth/authenticator.py`, `retro_radio/core/tts.py`
- **変更内容**: 
  - 構文チェックを実行
- **確認方法**: 
  ```bash
  python -m py_compile app.py
  python -m py_compile retro_radio/auth/authenticator.py
  python -m py_compile retro_radio/core/tts.py
  ```
  全てエラーなくコンパイルできること
- **所要時間目安**: 3分

### Step 19: インポート未使用チェック
- **対象ファイル**: 全変更ファイル
- **変更内容**: 
  - 未使用インポートがないか確認
- **確認方法**: 
  ```bash
  # ruff が利用可能なら
  ruff check app.py retro_radio/auth/authenticator.py retro_radio/core/tts.py
  ```
  未使用インポートの警告がないこと
- **所要時間目安**: 3分

### Step 20: 基本機能テスト - APIキー無しフォールバック
- **対象ファイル**: 変更なし（動作確認）
- **変更内容**: 
  - APIキー未設定時でもフォールバック動作が働くことを確認
- **確認方法**: 
  1. `.env` ファイルから `GEMINI_API_KEY` を一時的に削除またはコメントアウト
  2. アプリを起動
  3. 「ラジオを再生する」ボタンをクリック
  4. フォールバック台本が表示されること
  5. 環境変数を戻す
- **所要時間目安**: 5分

### Step 21: XSS防止テスト - 特殊文字入力シミュレーション
- **対象ファイル**: 変更なし（動作確認）
- **変更内容**: 
  - サニタイズが機能することを簡易テスト
- **確認方法**: 
  1. Pythonインタプリタで `from retro_radio.utils.validators import sanitize_text` 
  2. `sanitize_text("<script>alert('xss')</script>")` を実行
  3. 結果が `<script>alert('xss')</script>` または同様にエスケープされることを確認
- **所要時間目安**: 3分

### Step 22: 依存関係インストール確認（ElevenLabs用）
- **対象ファイル**: `requirements.txt` または `pyproject.toml`
- **変更内容**: 
  - `requests` ライブラリが依存関係に含まれていることを確認
- **確認方法**: 
  ```bash
  grep -i requests requirements.txt || echo "requests not found"
  ```
  存在しない場合は追加が必要だが、既に他の場所で使用されているはずなので確認のみ
- **所要時間目安**: 2分

### Step 23: ドキュメント更新 - セキュリティ注意書き追加
- **対象ファイル**: `README.md`
- **変更内容**: 
  - セキュリティ項目を追加
  ```markdown
  ## セキュリティ注意事項
  
  - APIキーはクライアントサイド（localStorage）に保存せず、サーバーサイドセッションのみで管理しています
  - ユーザー生成コンテンツは適切にサニタイズされており、XSS攻撃から保護されています
  ```
- **確認方法**: テキストが追加されていることを確認
- **所要時間目安**: 2分

### Step 24: 最終確認・まとめ
- **対象ファイル**: 全変更ファイル
- **変更内容**: 
  - すべての変更点をレビューし、不要なデバッグコードやコメントがないことを確認
- **確認方法**: 
  1. git diff で変更内容を確認
  2. `pytest tests/ -v` で既存テストが全てパスすることを確認（可能な範囲で）
  3. アプリが起動し、基本機能（フォールバック動作含む）が動作することを確認
- **所要時間目安**: 10分

## 依存関係マトリクス
| Step | 依存先 | 備考 |
|------|--------|------|
| 2    | 1      | sanitize_textのインポート後に使用 |
| 5    | 4      | loadApiKey関数削除後に呼び出し削除 |
| 6    | 4      | 同上 |
| 7    | 4      | 同上 |
| 10   | 9      | MagicMock削除後にインポート削除 |
| 12   | 11     | ElevenLabs実装にrequests必要 |
| 18   | 1,2,4,5,6,7,9,10,11,12 | すべての変更後に構文チェック |
| 19   | 1,2,4,5,6,7,9,10,11,12 | すべての変更後にインポートチェック |

## 完了定義
- [x] 全24ステップ実装完了
- [ ] 既存テスト全パス (`pytest tests/ -v`) 
- [ ] 新規テスト追加（該当する場合は別途）
- [ ] リンター/型チェック通過 (`ruff check .`, `mypy .`)
- [x] 手動動作確認完了（フォールバック動作・サニタイズ動作）