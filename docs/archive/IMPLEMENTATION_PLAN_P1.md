# 実装計画書 P1：コア品質向上（優先度：高）
## 対象改善：1. iTunesプレビュー事前フィルタ / 2. Streamlit Secrets対応 / 3. キャッシュ機能導入

---

### ステップ 1：`requirements.txt` に `tenacity` を追加
- ファイル末尾に `tenacity>=8.2.0` を追記する

### ステップ 2：`app.py` 先頭に `tenacity` インポートを追加
- `from tenacity import retry, stop_after_attempt, wait_exponential` をインポートブロックに追加

### ステップ 3：`GEMINI_API_KEY` 取得ロジックを Secrets 対応に変更
- 53行目を以下に変更：
  ```python
  GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY", "")
  ```

### ステップ 4：`search_itunes_songs` 関数に `@st.cache_data` デコレータを追加
- 90行目の関数定義直前に `@st.cache_data(ttl=3600, show_spinner=False)` を追加

### ステップ 5：`generate_radio_script` 関数に `@st.cache_data` デコレータを追加
- 75行目の関数定義直前に `@st.cache_data(ttl=3600, show_spinner=False)` を追加

### ステップ 6：iTunes検索結果から `previewUrl` ありのみを抽出するヘルパー関数を作成
- `search_itunes_songs` 関数の内部で、以下の処理を追加：
  ```python
  results = data.get("results", [])
  return [s for s in results if s.get("previewUrl")]
  ```

### ステップ 7：楽曲選択ロジックを「フィルタ済みリストからランダム選択」に変更
- 124-134行目を以下に変更：
  ```python
  songs = search_itunes_songs(selected_year)
  if not songs:
      st.error("試聴可能な楽曲が見つかりませんでした。別の年をお試しください。")
      st.stop()
  song = random.choice(songs)
  song_title = song.get("trackName", "不明")
  artist_name = song.get("artistName", "不明")
  preview_url = song.get("previewUrl")
  ```

### ステップ 8：`search_itunes_songs` にリトライデコレータを追加
- 関数定義の上に `@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))` を追加

### ステップ 9：`generate_radio_script` にリトライデコレータを追加
- 関数定義の上に `@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))` を追加

### ステップ 10：`requests.get` のタイムアウトを tuple 形式に変更
- 99行目を `timeout=(5, 10)` に変更（接続5秒、読み取り10秒）

### ステップ 11：キャッシュキーに年・月・日を含めるよう明示
- `generate_radio_script` の引数に `month, day` が含まれていることを確認（現状OK）

### ステップ 12：iTunes検索パラメータの `term` を最適化
- 93行目を `"term": f"{year}年 日本 ヒット曲 ランキング",` に変更（検索精度向上）

### ステップ 13：`limit` を 50 に増やし、ヒット曲ヒット率を上げる
- 97行目を `"limit": 50,` に変更

### ステップ 14：`country` パラメータを確実に "JP" に固定
- 94行目を確認（現状OK）

### ステップ 15：エラーメッセージをユーザー向けに簡潔化
- 119, 126, 133, 136, 143行目のエラー文言を「○○に失敗しました。もう一度お試しください。」形式に統一

### ステップ 16：`st.warning` を `st.info` に変更（APIキー未設定時）
- 56行目を `st.info("ℹ️ Gemini APIキーが未設定です。環境変数またはSecretsで設定してください。")` に変更

### ステップ 17：`genai.configure` を APIキー存在時のみ実行
- 58行目を以下で囲む：
  ```python
  if GEMINI_API_KEY:
      genai.configure(api_key=GEMINI_API_KEY)
  ```

### ステップ 18：ボタン押下時の APIキー チェックを関数化
- `check_api_key()` 関数を作成し、111-113行目を呼び出しに置換

### ステップ 19：キャッシュクリアボタンをサイドバーに追加
- サイドバーに `st.button("キャッシュクリア", on_click=clear_cache)` を配置
- `clear_cache()` で `st.cache_data.clear()` を実行

### ステップ 20：デバッグ用キャッシュ情報表示（開発時のみ）
- `if st.secrets.get("DEBUG", False):` ブロックでキャッシュ統計を表示

### ステップ 21：関数の型ヒントを完全化
- `search_itunes_songs` 戻り値を `list[dict]` に、`generate_radio_script` 戻り値を `str` に明記

### ステップ 22：定数化（マジックナンバー排除）
- ファイル冒頭に `CACHE_TTL = 3600`, `MAX_RETRIES = 3`, `ITUNES_LIMIT = 50` 等を定義

### ステップ 23：動作確認チェックリスト作成
- 同一年で2回ボタン押下→2回目は高速化確認
- APIキー未設定時→適切なメッセージ確認
- previewUrlなし楽曲のみの年→エラー確認

### ステップ 24：コミット・プッシュ前の最終確認
- `streamlit run app.py` でエラーなし起動確認
- 1950, 1960, 1980, 2000, 2025 年で動作確認