# 実装計画書 P3：堅牢性・フォールバック・運用機能（優先度：低）
## 対象改善：6. リトライ機構完成 / 8. 履歴機能完成 / 9. エラーフォールバック / 運用監視

---

### ステップ 1：共通リトライ設定を定数化
- ファイル冒頭（定数ブロック）に追加：
  ```python
  RETRY_ATTEMPTS = 3
  RETRY_WAIT_MIN = 2
  RETRY_WAIT_MAX = 10
  RETRY_MULTIPLIER = 1
  ```

### ステップ 2：共通リトライデコレータ関数を作成
- インポートブロック後に関数定義：
  ```python
  def with_retry(func):
      @retry(
          stop=stop_after_attempt(RETRY_ATTEMPTS),
          wait=wait_exponential(multiplier=RETRY_MULTIPLIER, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
          reraise=True
      )
      def wrapper(*args, **kwargs):
          return func(*args, **kwargs)
      return wrapper
  ```

### ステップ 3：`search_itunes_songs` に共通リトライを適用
- `@st.cache_data` の内側（関数定義直上）に `@with_retry` を追加

### ステップ 4：`generate_radio_script` に共通リトライを適用
- 同上

### ステップ 5：`text_to_speech` にもリトライ適用（gTTS失敗対策）
- 104行目関数定義直上に `@with_retry` を追加

### ステップ 6：フォールバック原稿生成関数を作成
- `generate_radio_script` 関数の後（88行目以降）に追加：
  ```python
  def generate_fallback_script(year: int, month: int, day: int) -> str:
      return f"""皆様、いかがお過ごしでしょうか。
{year}年{month}月{day}日でございますね。
この頃の日本は、季節の移ろいとともに、人々の暮らしも静かに変わってまいりました。
ニュースの準備ができませんでしたが、皆様の記憶の中に、この日の風景がよみがえりますように。
それでは、この年のヒット曲を少しだけお聴きください。"""
  ```

### ステップ 7：Gemini失敗時にフォールバック原稿を使用
- 117-120行目（原稿生成tryブロック）を以下に変更：
  ```python
  try:
      script = generate_radio_script(selected_year, current_month, current_day)
  except Exception:
      st.warning("AI原稿生成に失敗しました。定型原稿でお届けします。")
      script = generate_fallback_script(selected_year, current_month, current_day)
  ```

### ステップ 8：iTunes検索失敗時のフォールバック（代表曲データをローカル保持）
- ファイル冒頭に代表曲辞書を定義：
  ```python
  FALLBACK_SONGS = {
      1950: [("リンゴの唄", "並木路子"), ("銀座カンカン娘", "高峰秀子")],
      1960: [("上を向いて歩こう", "坂本九"), ("見上げてごらん夜の星を", "坂本九")],
      1970: [("いい日旅立ち", "山口百恵"), ("君よ抱かれて熱くなれ", "西城秀樹")],
      1980: [("ルビーの指環", "寺尾聰"), ("シルエット・ロマンス", "大橋純子")],
      1990: [("世界に一つだけの花", "SMAP"), ("LA·LA·LA LOVE SONG", "久保田利伸")],
      2000: [("ハナミズキ", "一青窈"), ("TSUNAMI", "サザンオールスターズ")],
      2010: [("前前前世", "RADWIMPS"), ("Lemon", "米津玄師")],
      2020: [("夜に駆ける", "YOASOBI"), ("ドライフラワー", "優里")],
  }
  ```

### ステップ 9：iTunes失敗時にフォールバック曲からランダム選択
- 122-137行目（楽曲検索tryブロック）を以下に変更：
  ```python
  try:
      songs = search_itunes_songs(selected_year)
      if not songs:
          raise ValueError("No songs with preview")
      song = random.choice(songs)
      song_title = song.get("trackName", "不明")
      artist_name = song.get("artistName", "不明")
      preview_url = song.get("previewUrl")
      use_fallback_song = False
  except Exception:
      st.warning("楽曲検索に失敗しました。代表曲から選曲します（試聴はできません）。")
      fallback_list = FALLBACK_SONGS.get(selected_year, [("未登録", "未登録")])
      song_title, artist_name = random.choice(fallback_list)
      preview_url = None
      use_fallback_song = True
  ```

### ステップ 10：フォールバック曲時は試聴プレーヤーを非表示・案内表示
- 156-158行目（楽曲表示ブロック）を以下に変更：
  ```python
  st.markdown("### 🎵 今日の一曲")
  st.write(f"**{song_title}** / {artist_name}")
  if preview_url and not use_fallback_song:
      st.audio(preview_url, format="audio/mp3")
  else:
      st.info("💡 この曲の試聴音源はご用意できませんでした。歌詞やメロディを思い出してお楽しみください。")
  ```

### ステップ 11：音声合成失敗時のフォールバック（ブラウザ標準TTS案内）
- 139-144行目（音声合成tryブロック）を以下に変更：
  ```python
  try:
      if cache_key in st.session_state.audio_cache:
          audio_path = st.session_state.audio_cache[cache_key]
          if not os.path.exists(audio_path):
              raise FileNotFoundError
      else:
          audio_path = text_to_speech(script)
          st.session_state.audio_cache[cache_key] = audio_path
  except Exception:
      st.warning("音声合成に失敗しました。ブラウザの読み上げ機能をご利用ください。")
      audio_path = None
  ```

### ステップ 12：音声パスがNoneの場合の表示分岐
- 151-154行目（ニュース音声表示）を以下に変更：
  ```python
  st.markdown("### 🎙️ ニュース音声")
  if audio_path and os.path.exists(audio_path):
      with open(audio_path, "rb") as f:
          audio_bytes = f.read()
      st.audio(audio_bytes, format="audio/mp3")
  else:
      st.info("🔊 音声ファイルがありません。原稿テキストをブラウザの読み上げ機能（選択→右クリック→読み上げ）でお聴きいただけます。")
  ```

### ステップ 13：エラー発生時の詳細ログをサーバーサイドに出力
- 各 `except Exception as e:` ブロックに `print(f"[ERROR] {func_name}: {e}")` または `logging.error` を追加

### ステップ 14：構造化ログ用 `logging` 設定を追加
- ファイル冒頭に追加：
  ```python
  import logging
  logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
  logger = logging.getLogger(__name__)
  ```

### ステップ 15：各API呼び出し前後にログ出力
- `generate_radio_script` 開始・終了に `logger.info(f"Gemini生成開始/完了: year={year}")`
- `search_itunes_songs` 開始・終了に `logger.info(f"iTunes検索開始/完了: year={year}, results={len(results)}")`
- `text_to_speech` 開始・終了に `logger.info(f"TTS合成開始/完了: chars={len(text)}")`

### ステップ 16：パフォーマンス計測デコレータを作成
- `with_retry` と同様に `@measure_time` デコレータを作成し、主要関数に適用

### ステップ 17：サイドバーに「システム情報」表示エリアを追加
- サイドバー末尾に：
  ```python
  with st.sidebar:
      st.markdown("---")
      st.markdown("## ⚙️ システム情報")
      st.caption(f"キャッシュ件数: {len(st.session_state.audio_cache)}")
      st.caption(f"履歴件数: {len(st.session_state.generation_history)}")
      if st.button("全キャッシュクリア"):
          st.session_state.audio_cache.clear()
          st.cache_data.clear()
          st.success("クリアしました")
          st.rerun()
  ```

### ステップ 18：ヘルスチェックエンドポイント用関数を作成（将来の監視用）
- `def health_check() -> dict:` で `{"status": "ok", "gemini": bool(GEMINI_API_KEY), "cache": len(st.session_state.audio_cache)}` を返す

### ステップ 19：`.streamlit/config.toml` テンプレートを作成
- 別ファイル `config.toml.example` として出力：
  ```toml
  [server]
  maxUploadSize = 50
  maxMessageSize = 50
  
  [browser]
  gatherUsageStats = false
  ```

### ステップ 20：`.streamlit/secrets.toml.example` を作成
- 別ファイルとして出力：
  ```toml
  GEMINI_API_KEY = "your-api-key-here"
  DEBUG = false
  ```

### ステップ 21：README.md に環境構築手順を記載
- 別ファイルとして出力（インストール、APIキー設定、起動、トラブルシューティング）

### ステップ 22：単体テスト用のモック関数・テストケース雛形を作成
- `test_app.py` 雛形を作成（pytest + unittest.mock でAPIモック）

### ステップ 23：依存関係のバージョン固定（`requirements.txt` 更新）
- 全パッケージを `==` で固定（例: `streamlit==1.35.0`）

### ステップ 24：最終動作確認シナリオ実施
- [ ] 正常フロー（全API成功）
- [ ] Gemini失敗→フォールバック原稿
- [ ] iTunes失敗→フォールバック曲
- [ ] TTS失敗→ブラウザ読み上げ案内
- [ ] 全API失敗→最小限の情報表示
- [ ] 同一年2度目→キャッシュ高速化
- [ ] 履歴から再生→生成スキップ
- [ ] キャッシュクリア→再生成
- [ ] ダークモード・高コントラスト確認
- [ ] キーボードのみ操作確認