# 実装計画書 P2：UX・アクセシビリティ強化（優先度：中）
## 対象改善：4. セッション内音声キャッシュ / 5. 高齢者向けアクセシビリティ / 7. ローディング詳細化

---

### ステップ 1：`st.session_state` 初期化ブロックを追加
- スライダー定義の前（70行目前後）に以下を追加：
  ```python
  if "audio_cache" not in st.session_state:
      st.session_state.audio_cache = {}
  if "generation_history" not in st.session_state:
      st.session_state.generation_history = []
  ```

### ステップ 2：音声ファイルパスをセッションステートに保存
- 141行目（`audio_path = text_to_speech(script)`）の直後に追加：
  ```python
  cache_key = f"{selected_year}_{current_month}_{current_day}"
  st.session_state.audio_cache[cache_key] = audio_path
  ```

### ステップ 3：キャッシュ済み音声がある場合は再合成をスキップ
- 139-144行目を以下で置換：
  ```python
  cache_key = f"{selected_year}_{current_month}_{current_day}"
  if cache_key in st.session_state.audio_cache:
      audio_path = st.session_state.audio_cache[cache_key]
      if os.path.exists(audio_path):
          st.info("前回の音声を再利用します")
      else:
          with st.spinner("音声を合成しています..."):
              audio_path = text_to_speech(script)
              st.session_state.audio_cache[cache_key] = audio_path
  else:
      with st.spinner("音声を合成しています..."):
          audio_path = text_to_speech(script)
          st.session_state.audio_cache[cache_key] = audio_path
  ```

### ステップ 4：進捗バー・ステータステキスト表示用のプレースホルダを作成
- ボタン押下直後（115行目前後）に追加：
  ```python
  progress_bar = st.progress(0)
  status_text = st.empty()
  ```

### ステップ 5：原稿生成フェーズで進捗 33%・ステータス更新
- 115-120行目を以下で置換：
  ```python
  status_text.text("📝 ラジオ原稿を作成中...")
  progress_bar.progress(33)
  try:
      script = generate_radio_script(selected_year, current_month, current_day)
  except Exception as e:
      st.error("原稿の生成に失敗しました。もう一度お試しください。")
      st.stop()
  ```

### ステップ 6：楽曲検索フェーズで進捗 66%・ステータス更新
- 122-137行目を以下で置換：
  ```python
  status_text.text("🎵 その時のヒット曲を探しています...")
  progress_bar.progress(66)
  try:
      songs = search_itunes_songs(selected_year)
      if not songs:
          st.error("試聴可能な楽曲が見つかりませんでした。別の年をお試しください。")
          st.stop()
      song = random.choice(songs)
      song_title = song.get("trackName", "不明")
      artist_name = song.get("artistName", "不明")
      preview_url = song.get("previewUrl")
  except Exception as e:
      st.error("楽曲の取得に失敗しました。もう一度お試しください。")
      st.stop()
  ```

### ステップ 7：音声合成フェーズで進捗 100%・ステータス更新
- 139-144行目（ステップ3で修正済み）の直後に追加：
  ```python
  status_text.text("🔊 音声を合成しています...")
  progress_bar.progress(100)
  ```

### ステップ 8：完了後にプログレスバー・ステータスをクリア
- 146行目（`st.success`）の直後に追加：
  ```python
  progress_bar.empty()
  status_text.empty()
  ```

### ステップ 9：CSS でフォントサイズをさらに拡大（高齢者対応）
- 29-36行目（`.script-text`）を以下に変更：
  ```css
  .script-text {
      font-size: 1.6rem;
      line-height: 2.0;
      background-color: #fefefe;
      padding: 2rem;
      border-radius: 12px;
      margin: 1rem 0;
      border: 2px solid #ddd;
      color: #1a1a1a;
  }
  ```

### ステップ 10：タイトル・サブタイトル・ラベルのフォントサイズ拡大
- 18-22行目（`.main-title`）を `font-size: 2.8rem;` に
- 23-27行目（`.subtitle`）を `font-size: 1.4rem;` に
- 42-46行目（`.year-label`）を `font-size: 1.5rem;` に

### ステップ 11：ボタンの最小高さ・コントラスト強化
- 37-41行目（`.stButton > button`）を以下に変更：
  ```css
  .stButton > button {
      font-size: 1.8rem;
      padding: 1.2rem 2.5rem;
      width: 100%;
      min-height: 70px;
      background-color: #1f6feb;
      color: white;
      border: none;
      border-radius: 8px;
      font-weight: bold;
  }
  .stButton > button:hover {
      background-color: #1558b8;
  }
  .stButton > button:focus {
      outline: 3px solid #ffd700;
      outline-offset: 2px;
  }
  ```

### ステップ 12：オーディオプレイヤーのスタイル拡大
- CSS に以下を追加：
  ```css
  audio {
      width: 100%;
      height: 50px;
  }
  ```

### ステップ 13：原稿表示を `st.text_area` (readonly) に変更しスクリーンリーダー対応
- 148-149行目を以下に変更：
  ```python
  st.markdown("### 📝 ラジオ原稿")
  st.text_area("", value=script, height=200, disabled=True, label_visibility="collapsed")
  ```

### ステップ 14：見出しに `aria-level` 相当の役割を持たせるため `st.subheader` 使用
- 148, 151, 156行目の `st.markdown("### ...")` を `st.subheader("...")` に変更

### ステップ 15：カラーコントラスト確認用のダークモード対応CSS追加
- `@media (prefers-color-scheme: dark)` ブロックをCSS末尾に追加

### ステップ 16：履歴保存処理を追加（完了直後）
- 146行目（`st.success`）の直後に追加：
  ```python
  history_entry = {
      "year": selected_year,
      "date": f"{current_month}月{current_day}日",
      "script": script,
      "song_title": song_title,
      "artist_name": artist_name,
      "preview_url": preview_url,
      "audio_path": audio_path,
      "timestamp": datetime.now().strftime("%H:%M:%S")
  }
  st.session_state.generation_history.insert(0, history_entry)
  if len(st.session_state.generation_history) > 10:
      st.session_state.generation_history.pop()
  ```

### ステップ 17：サイドバーに履歴表示エリアを作成
- `st.sidebar` ブロックをタイトル下（51行目以降）に追加：
  ```python
  with st.sidebar:
      st.markdown("## 📜 再生履歴")
      if st.session_state.generation_history:
          for i, entry in enumerate(st.session_state.generation_history):
              with st.expander(f"{entry['year']}年{entry['date']} - {entry['song_title']}"):
                  st.caption(f"生成時刻: {entry['timestamp']}")
                  if st.button(f"再生", key=f"replay_{i}"):
                      st.session_state.replay_entry = entry
      else:
          st.caption("まだ履歴がありません")
  ```

### ステップ 18：履歴からの再生処理をボタン押下直後に追加
- 110行目（`if st.button...`）の直前に追加：
  ```python
  if "replay_entry" in st.session_state:
      entry = st.session_state.replay_entry
      del st.session_state.replay_entry
      # 入力値を履歴から復元して処理を継続
      script = entry["script"]
      song_title = entry["song_title"]
      artist_name = entry["artist_name"]
      preview_url = entry["preview_url"]
      audio_path = entry["audio_path"]
      selected_year = entry["year"]
      # 表示処理へジャンプ（フラグで制御）
      st.session_state.skip_generation = True
  ```

### ステップ 19：スキップフラグによる生成処理バイパス
- ボタン押下ブロックの冒頭に追加：
  ```python
  if st.session_state.get("skip_generation", False):
      st.session_state.skip_generation = False
      # 表示処理のみ実行（生成スキップ）
  else:
      # 既存の生成処理
  ```

### ステップ 20：履歴再生時もプログレスバー表示（即完了）
- スキップ時にもプログレスバーを100%で表示し即クリア

### ステップ 21：履歴クリアボタンをサイドバーに追加
- サイドバー履歴エリア末尾に：
  ```python
  if st.button("履歴をクリア", use_container_width=True):
      st.session_state.generation_history = []
      st.rerun()
  ```

### ステップ 22：スライダーのステップ表示を大きく（年号のみ大きく）
- CSS に `.stSlider [data-testid="stTickBar"]` 等のスタイル追加

### ステップ 23：エラー時の音声案内（gTTS でエラー文読み上げ）をオプションで追加
- `st.checkbox("エラー時も音声でお知らせ", value=False)` をサイドバーに配置
- エラー catch ブロックで有効時のみ `gTTS` で読み上げ

### ステップ 24：動作確認チェックリスト
- 同一年2回目→音声再利用確認
- 履歴から再生→生成スキップ確認
- フォントサイズ・コントラスト視認確認
- キーボードのみ操作（Tab/Enter）確認
- ダークモード表示確認