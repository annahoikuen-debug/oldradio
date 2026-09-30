# T3: 本番運用・アクセシビリティ・国際化実装計画書
## 概要
改善案9を実装し、アクセシビリティ・国際化・モバイルUX・本番運用準備を完了する
各ステップは極小単位に分割し、コピペベースで実装可能

---

## Step 1: アクセシビリティ基盤実装 (ui/accessibility.py)
**対象ファイル**: `retro_radio/ui/accessibility.py` 新規
**作業最小単位**:
1. ファイル作成: `touch retro_radio/ui/accessibility.py`
2. 基本構造追加: 
   ```python
   """アクセシビリティユーティリティ"""
   
   def apply_accessibility() -> None:
       pass
   ```
3. ダークモード対応CSS追加:
   ```python
   def apply_accessibility() -> None:
       st.markdown("""
       <style>
       @media (prefers-color-scheme: dark) {
           .main-title { color: #ffffff; }
           .subtitle { color: #cccccc; }
           .script-text { background-color: #2b2b2b; color: #f0f0f0; border-color: #444; }
           .year-label { color: #cccccc; }
           .stButton > button { background-color: #4a90e2; color: #ffffff; }
           .stButton > button:hover { background-color: #357ab8; }
       }
       </style>
       """, unsafe_allow_html=True)
   ```
4. テストファイル作成: `touch tests/test_accessibility.py`
5. テスト実行: `pytest tests/test_accessibility.py -v`
**完了基準**: ダークモード時に色が変わること（手動確認）

---

## Step 2: フォーカス表示強化
**対象ファイル**: `retro_radio/ui/accessibility.py` 更新
**作業最小単位**:
1. フォーカス表示CSS追加:
   ```python
   def apply_accessibility() -> None:
       st.markdown("""
       <style>
       @media (prefers-color-scheme: dark) {
           .main-title { color: #ffffff; }
           .subtitle { color: #cccccc; }
           .script-text { background-color: #2b2b2b; color: #f0f0f0; border-color: #444; }
           .year-label { color: #cccccc; }
           .stButton > button { background-color: #4a90e2; color: #ffffff; }
           .stButton > button:hover { background-color: #357ab8; }
       }
       /* フォーカス表示強化 */
       *:focus-visible {
           outline: 3px solid #ffd700 !important;
           outline-offset: 2px !important;
       }
       /* フォーカス時のボタン強調 */
       .stButton > button:focus-visible {
           box-shadow: 0 0 0 3px rgba(255, 215, 0, 0.5);
       }
       </style>
       """, unsafe_allow_html=True)
   ```
2. テスト追加: フォーカスCSSが存在するか確認
**完了基準**: Tabキーでフォーカス移動時に金色輪郭表示

---

## Step 3: モーション削減対応
**対象ファイル**: `retro_radio/ui/accessibility.py` 更新
**作業最小単位**:
1. モーション削減CSS追加:
   ```python
   def apply_accessibility() -> None:
       st.markdown("""
       <style>
       @media (prefers-color-scheme: dark) {
           .main-title { color: #ffffff; }
           .subtitle { color: #cccccc; }
           .script-text { background-color: #2b2b2b; color: #f0f0f0; border-color: #444; }
           .year-label { color: #cccccc; }
           .stButton > button { background-color: #4a90e2; color: #ffffff; }
           .stButton > button:hover { background-color: #357ab8; }
       }
       *:focus-visible {
           outline: 3px solid #ffd700 !important;
           outline-offset: 2px !important;
       }
       .stButton > button:focus-visible {
           box-shadow: 0 0 0 3px rgba(255, 215, 0, 0.5);
       }
       /* モーション削減 */
       @media (prefers-reduced-motion: reduce) {
           * { animation: none !important; transition: none !important; }
       }
       </style>
       """, unsafe_allow_html=True)
   ```
2. コメント追加: 「アニメーション・トランジションを無効にする」
**完了基準**: システム設定で「アニメーションを減らす」ON時、フェード等消失

---

## Step 4: タッチターゲット最小サイズ
**対象ファイル**: `retro_radio/ui/accessibility.py` 更新
**作業最小単位**:
1. タッチターゲットCSS追加:
   ```python
   def apply_accessibility() -> None:
       st.markdown("""
       <style>
       @media (prefers-color-scheme: dark) {
           .main-title { color: #ffffff; }
           .subtitle { color: #cccccc; }
           .script-text { background-color: #2b2b2b; color: #f0f0f0; border-color: #444; }
           .year-label { color: #cccccc; }
           .stButton > button { background-color: #4a90e2; color: #ffffff; }
           .stButton > button:hover { background-color: #357ab8; }
       }
       *:focus-visible {
           outline: 3px solid #ffd700 !important;
           outline-offset: 2px !important;
       }
       .stButton > button:focus-visible {
           box-shadow: 0 0 0 3px rgba(255, 215, 0, 0.5);
       }
       @media (prefers-reduced-motion: reduce) {
           * { animation: none !important; transition: none !important; }
       }
       /* タッチターゲット最小44x44px */
       .stButton > button {
           min-height: 44px !important;
           min-width: 44px !important;
       }
       /* スライダーのつまみも最小サイズ */
       [data-testid="stThumbValue"] {
           min-height: 20px !important;
           min-width: 20px !important;
       }
       </style>
       """, unsafe_allow_html=True)
   ```
2. 寸法値を変数化（後で調整しやすくする準備）
**完了基準**: スマートフォンで指先で正確にタップ可能

---

## Step 5: コントラスト比強化
**対象ファイル**: `retro_radio/ui/accessibility.py` 更新
**作業最小単位**:
1. ハイコントラストモードCSS追加:
   ```python
   def apply_accessibility() -> None:
       st.markdown("""
       <style>
       @media (prefers-color-scheme: dark) {
           .main-title { color: #ffffff; }
           .subtitle { color: #cccccc; }
           .script-text { background-color: #2b2b2b; color: #f0f0f0; border-color: #444; }
           .year-label { color: #cccccc; }
           .stButton > button { background-color: #4a90e2; color: #ffffff; }
           .stButton > button:hover { background-color: #357ab8; }
       }
       *:focus-visible {
           outline: 3px solid #ffd700 !important;
           outline-offset: 2px !important;
       }
       .stButton > button:focus-visible {
           box-shadow: 0 0 0 3px rgba(255, 215, 0, 0.5);
       }
       @media (prefers-reduced-motion: reduce) {
           * { animation: none !important; transition: none !important; }
       }
       .stButton > button {
           min-height: 44px !important;
           min-width: 44px !important;
       }
       [data-testid="stThumbValue"] {
           min-height: 20px !important;
           min-width: 20px !important;
       }
       /* ハイコントラストモード */
       @media (prefers-contrast: high) {
           .script-text { border-width: 3px !important; }
           .main-title { text-shadow: 0 0 2px #000; }
           .subtitle { text-shadow: 0 0 1px #000; }
           .year-label { text-shadow: 0 0 1px #000; }
       }
       </style>
       """, unsafe_allow_html=True)
   ```
2. WCAG AA基準達成をコメントに追加
**完了基準**: ハイコントラスト設定時、境界が太くなる

---

## Step 6: スクリーンリーダー向けラベル改善
**対象ファイル**: `retro_radio/ui/components.py` 更新
**作業最小単位**:
1. スライダーにaria-label追加（コンポーネント関数内）:
   ```python
   def render_year_selector(current_month: int, current_day: int) -> int:
       st.markdown(f'<div class="year-label">今日の日付: {current_month}月{current_day}日</div>', unsafe_allow_html=True)
       year = st.slider(
           "西暦を選んでください", 
           1950, 2025, 1960, 1,
           label_visibility="visible"  # ラベルを常に表示
       )
       st.markdown(f'<div class="year-label">選択された年: <strong>{year}年</strong></div>', unsafe_allow_html=True)
       return year
   ```
2. ボタンにアクセシブル名確保（既にテキストありなのでOKとマーク）
**完了基準**: スクリーンリーダーが「西暦を選んでください スライダー 1960」等と読み上げ

---

## Step 7: エラーメッセージのロール属性追加
**対象ファイル**: `retro_radio/ui/components.py` 更新
**作業最小単位**:
1. エラー・警告・情報メッセージにrole追加:
   ```python
   def render_error(message: str) -> None:
       st.markdown(f'<div role="alert" class="error-message">{message}</div>', unsafe_allow_html=True)
   
   def render_info(message: str) -> None:
       st.markdown(f'<div role="status" class="info-message">{message}</div>', unsafe_allow_html=True)
   ```
2. 既存st.error/st.warning/st.infoをラッパー関数に置換（段階的）
3. まずはst.infoの1箇所から置換テスト
**完了基準**: エラー発生時にスクリーンリーダーが自動通知

---

## Step 8: キーボードナビゲーション順序最適化
**対象ファイル**: `retro_radio/ui/components.py` 更新
**作業最小単位**:
1. tabindex属性追加でナビゲーション順序明示:
   ```python
   def render_year_selector(current_month: int, current_day: int) -> int:
       st.markdown(f'<div class="year-label">今日の日付: {current_month}月{current_day}日</div>', unsafe_allow_html=True)
       year = st.slider(
           "西暦を選んでください", 
           1950, 2025, 1960, 1,
           label_visibility="visible"
       )
       # JavaScriptで tabindex 設定（Streamlit制限回避）
       st.markdown("""
       <script>
       setTimeout(() => {
           const slider = window.parent.document.querySelector('input[type="range"]');
           if (slider) slider.tabIndex = 1;
       }, 100);
       </script>
       """, unsafe_allow_html=True)
       st.markdown(f'<div class="year-label">選択された年: <strong>{year}年</strong></div>', unsafe_allow_html=True)
       return year
   ```
2. 再生ボタンに tabindex=2 を設定
**完了基準**: Tab → スライダー → Tab → ボタン の順でフォーカス移動

---

## Step 9: PWAインストール促進バナー実装
**対象ファイル**: `retro_radio/ui/components.py` 新規関数
**作業最小単位**:
1. インストールバナー関数作成:
   ```python
   def render_pwa_install_banner() -> None:
       st.markdown("""
       <div id="pwa-install-banner" style="
           display: none;
           position: fixed;
           bottom: 20px;
           left: 0;
           right: 0;
           background: #333;
           color: white;
           text-align: center;
           padding: 12px;
           z-index: 1000;
           border-radius: 8px;
           font-size: 14px;
       ">
           このアプリをホーム画面に追加してオフラインでも使えます！<br>
           <button onclick="installPWA()" style="
               margin-left: 10px;
               padding: 8px 16px;
               background: #1f6feb;
               color: white;
               border: none;
               border-radius: 4px;
               cursor: pointer;
           ">インストール</button>
       </div>
       
       <script>
       let deferredPrompt;
       window.addEventListener('beforeinstallprompt', (e) => {
           e.preventDefault();
           deferredPrompt = e;
           const banner = document.getElementById('pwa-install-banner');
           banner.style.display = 'block';
       });
       
       function installPWA() {
           const banner = document.getElementById('pwa-install-banner');
           banner.style.display = 'none';
           if (deferredPrompt) {
               deferredPrompt.prompt();
               deferredPrompt.userChoice.then((choiceResult) => {
                   if (choiceResult.outcome === 'accepted') {
                       console.log('User accepted the install prompt');
                   }
                   deferredPrompt = null;
               });
           }
       }
       </script>
       """, unsafe_allow_html=True)
   ```
2. テストファイル作成: `touch tests/test_pwa_banner.py`
3. 最初は表示ロジックのみテスト（JSは手動確認）
**完了基準**: 最初のアクセス時にバナー表示、クリックで非表示

---

## Step 10: アクセシビリティコンポーネント統合
**対象ファイル**: `retro_radio/ui/components.py` 更新
**作業最小単位**:
1. accessibility.py から関数インポート:
   ```python
   from .accessibility import apply_accessibility
   ```
2. main.py の冒頭でapply_accessibility()呼び出し:
   ```python
   # main.py ファイル先頭付近
   from retro_radio.ui.accessibility import apply_accessibility
   from retro_radio.ui.components import render_pwa_install_banner
   
   # st.set_page_config の直後
   apply_accessibility()
   ```
3. コンポーネント関数内で PWA バナー表示:
   ```python
   def render_sidebar() -> None:
       # ... 既存サイドバー処理 ...
       render_pwa_install_banner()
   ```
**完了基盤**: アクセシビリティCSS適用、PWAバナー表示機能統合

---

## Step 11: 国際化基盤実装 (i18n/)
**対象ファイル**: 新規ディレクトリ構造作成
**作業最小単位**:
1. ディレクトリ作成: `mkdir -p retro_radio/i18n/locales`
2. 基本構造作成:
   ```
   retro_radio/
   └── i18n/
       ├── __init__.py
       ├── locales/
       │   ├── ja.json
       │   └── en.json
       └── translator.py
   ```
3. ja.json 作成（既存文字列の抽出）:
   ```json
   {
       "app_title": "レトロラジオ・タイムマシン",
       "app_subtitle": "懐かしのあの年へ、ラジオでタイムトラベル",
       "year_label_today": "今日の日付: {month}月{day}日",
       "year_label_selected": "選択された年: <strong>{year}年</strong>",
       "slider_label": "西暦を選んでください",
       "button_generate": "ラジオを再生する",
       "success_complete": "完成しました！",
       "sidebar_clear_cache": "キャッシュクリア",
       "sidebar_clear_history": "履歴をクリア",
       "sidebar_system_info": "システム情報",
       "sidebar_replay_history": "再生履歴",
       "checkbox_error_voice": "エラー時も音声でお知らせ",
       "info_no_audio": "🔊 音声ファイルがありません。原稿テキストをブラウザの読み上げ機能（選択→右クリック→読み上げ）でお聴きいただけます。",
       "info_no_preview": "💡 この曲の試聴音源はご用意できませんでした。歌詞やメロディを思い出してお楽しみください。",
       "warning_ai_failed": "AI原稿生成に失敗しました。定型原稿でお届けします。",
       "warning_music_failed": "楽曲検索に失敗しました。代表曲から選曲します（試聴はできません）。",
       "warning_tts_failed": "音声合成に失敗しました。ブラウザの読み上げ機能をご利用ください。",
       "info_cache_cleared": "キャッシュをクリアしました",
       "info_all_cleared": "クリアしました",
       "info_no_history": "まだ履歴がありません"
   }
   ```
4. en.json 作成（英語翻訳）:
   ```json
   {
       "app_title": "Retro Radio Time Machine",
       "app_subtitle": "Travel back in time with retro radio",
       "year_label_today": "Today's date: {month}/{day}",
       "year_label_selected": "Selected year: <strong>{year}</strong>",
       "slider_label": "Select Year",
       "button_generate": "Play Radio",
       "success_complete": "Completed!",
       "sidebar_clear_cache": "Clear Cache",
       "sidebar_clear_history": "Clear History",
       "sidebar_system_info": "System Info",
       "sidebar_replay_history": "Replay History",
       "checkbox_error_voice": "Audio error guidance",
       "info_no_audio": "🔊 No audio file available. You can use browser's read-aloud feature (select text → right-click → read aloud).",
       "info_no_preview": "💡 Preview audio not available. Try to remember the lyrics or melody.",
       "warning_ai_failed": "AI script generation failed. Using fallback script.",
       "warning_music_failed": "Music search failed. Using fallback song.",
       "warning_tts_failed": "TTS generation failed. Use browser read-aloud.",
       "info_cache_cleared": "Cache cleared",
       "info_all_cleared": "All cleared",
       "info_no_history": "No history yet"
   }
   ```
5. translator.py 作成:
   ```python
   import json
   import os
   from typing import Dict, Any
   import streamlit as st
   
   class Translator:
       def __init__(self, language: str = "ja"):
           self.language = language
           self.translations: Dict[str, Any] = {}
           self._load_translations()
       
       def _load_translations(self) -> None:
           try:
               path = f"retro_radio/i18n/locales/{self.language}.json"
               with open(path, 'r', encoding='utf-8') as f:
                   self.translations = json.load(f)
           except FileNotFoundError:
               # フォールバック: 日本語
               with open("retro_radio/i18n/locales/ja.json", 'r', encoding='utf-8') as f:
                   self.translations = json.load(f)
       
       def t(self, key: str, **kwargs) -> str:
           text = self.translations.get(key, key)
           if kwargs:
               return text.format(**kwargs)
           return text
   
   @st.cache_resource
   def get_translator(language: str = "ja") -> Translator:
       return Translator(language)
   ```
**テスト**: `tests/test_i18n.py` - JSONロード・プレースホルダー置換・フォールバック確認
**完了基準**: JA/EN切替で文字列が変わること（手動確認）

---

## Step 12: 国際化コンポーネント統合
**対象ファイル**: `retro_radio/ui/components.py` 更新
**作業最小単位**:
1. translator インポート・初期化:
   ```python
   from ..i18n.translator import get_translator
   
   # モジュールレベルで初期化（言語は環境変数またはセッション状態から）
   _translator = get_translator(st.session_state.get("language", "ja"))
   ```
2. 全ハードコード文字列を t() 呼び出しに置換（1つずつ）:
   ```python
   # Before
   st.markdown('<div class="main-title">📻 レトロラジオ・タイムマシン</div>', unsafe_allow_html=True)
   
   # After
   st.markdown(f'<div class="main-title">📻 {_translator.t("app_title")}</div>', unsafe_allow_html=True)
   ```
3. 最初はapp_titleのみ置換テスト
4. 順次他の文字列も置換（ボタン、ラベル、メッセージ等）
**テスト**: `tests/test_components_i18n.py` - 各文字列が正しく翻訳されるか
**完了基準**: 全UI文字列が国際化対応、言語切替可能

---

## Step 13: 言語選択UI実装
**対象ファイル**: `retro_radio/ui/components.py` 更新
**作業最小単位**:
1. サイドバーに言語選択追加:
   ```python
   def render_sidebar() -> None:
       with st.sidebar:
           # ... 既存処理 ...
           
           # 言語選択（一番下に追加）
           st.markdown("---")
           language_options = {"ja": "日本語", "en": "English"}
           current_lang = st.session_state.get("language", "ja")
           selected_lang = st.selectbox(
               "Language / 言語",
               options=list(language_options.keys()),
               format_func=lambda x: language_options[x],
               index=list(language_options.keys()).index(current_lang)
           )
           if selected_lang != current_lang:
               st.session_state.language = selected_lang
               st.rerun()
   ```
2. セッション状態初期化に language 追加:
   ```python
   # utils/session.py の DEFAULT_STATE に追加
   DEFAULT_STATE: SessionState = {
       "audio_cache": {},
       "generation_history": [],
       "error_voice_guidance": False,
       "language": "ja",  # デフォルト日本語
   }
   ```
**テスト**: `tests/test_language_switch.py` - 選択変更→再実行→言語変化確認
**完了基準**: ドロップダウンで言語切替可能、即座に反訳

---

## Step 14: レスポンシブデザイン強化
**対象ファイル**: `retro_radio/ui/styles.py` 更新
**作業最小単位**:
1. モバイルファーストブレイクポイント追加:
   ```python
   STYLES = """
   <style>
   /* 基本スタイル（モバイルファースト） */
   .main-title { font-size: 2.2rem; text-align: center; margin-bottom: 0.8rem; }
   .subtitle { font-size: 1.2rem; text-align: center; color: #666; margin-bottom: 1.5rem; }
   .script-text { font-size: 1.4rem; line-height: 1.8; background: #fefefe; padding: 1.5rem; border-radius: 10px; margin: 0.8rem 0; border: 2px solid #ddd; color: #1a1a1a; }
   .stButton > button { font-size: 1.6rem; padding: 1rem 2rem; width: 100%; min-height: 48px; background: #1f6feb; color: white; border: none; border-radius: 8px; font-weight: bold; }
   .stButton > button:hover { background: #1558b8; }
   .stButton > button:focus { outline: 3px solid #ffd700; outline-offset: 2px; }
   .year-label { font-size: 1.3rem; text-align: center; margin: 0.8rem 0; }
   audio { width: 100%; height: 40px; }
   
   /* タブレット以上 (768px) */
   @media (min-width: 768px) {
       .main-title { font-size: 2.8rem; margin-bottom: 1rem; }
       .subtitle { font-size: 1.4rem; margin-bottom: 2rem; }
       .script-text { font-size: 1.6rem; line-height: 2.0; padding: 2rem; border-radius: 12px; margin: 1rem 0; }
       .stButton > button { font-size: 1.8rem; padding: 1.2rem 2.5rem; min-height: 70px; }
       .year-label { font-size: 1.5rem; }
   }
   
   /* デスクトップ以上 (1024px) */
   @media (min-width: 1024px) {
       .main-title { font-size: 3.0rem; }
       .subtitle { font-size: 1.5rem; }
   }
   
   /* ダークモード */
   @media (prefers-color-scheme: dark) {
       .main-title { color: #ffffff; }
       .subtitle { color: #cccccc; }
       .script-text { background-color: #2b2b2b; color: #f0f0f0; border-color: #444; }
       .year-label { color: #cccccc; }
       .stButton > button { background-color: #4a90e2; color: #ffffff; }
       .stButton > button:hover { background-color: #357ab8; }
   }
   
   /* フォーカス表示 */
   *:focus-visible {
       outline: 3px solid #ffd700 !important;
       outline-offset: 2px !important;
   }
   .stButton > button:focus-visible {
       box-shadow: 0 0 0 3px rgba(255, 215, 0, 0.5);
   }
   
   /* モーション削減 */
   @media (prefers-reduced-motion: reduce) {
       * { animation: none !important; transition: none !important; }
   }
   
   /* タッチターゲット */
   .stButton > button {
       min-height: 44px !important;
       min-width: 44px !important;
   }
   
   /* ハイコントラスト */
   @media (prefers-contrast: high) {
       .script-text { border-width: 3px !important; }
       .main-title { text-shadow: 0 0 2px #000; }
       .subtitle { text-shadow: 0 0 1px #000; }
       .year-label { text-shadow: 0 0 1px #000; }
   }
   </style>
   """
   ```
2. フォントサイズ・間隔を段階的に調整
**テスト**: 各ブレイクポイントでレイアウト崩れないか確認
**完了基準**: 320px（スマホ）〜 1920px（大画面）まで快適表示

---

## Step 15: ローディング状態のアクセシビリティ向上
**対象ファイル**: `retro_radio/ui/components.py` 更新
**作業最小単位**:
1. スピナーに説明テキスト追加・ロール属性:
   ```python
   def render_loading_spinner(message: str) -> None:
       with st.spinner(message):
           # スクリーンリーダー向けに状態を定期更新
           empty = st.empty()
           # 実際のアプリではここで何らかの処理をする
           empty.text(f"{message}... (お待ちください)")
   ```
2. 既存のwith st.spinner(...)をラップ
3. まずは1箇所から置換テスト
**完了基準**: スクリーンリーダーが「処理中... お待ちください」等と案内

---

## Step 16: エラー復旧ガイダンス改善
**対象ファイル**: `retro_radio/ui/components.py` 更新
**作業最小単位**:
1. エラー時の復旧提案を具体化:
   ```python
   def render_error_with_guidance(error_type: str, details: str = "") -> None:
       guidance_map = {
           "api_key": "🔑 APIキーを設定してください：\n1. https://makersuite.google.com/app/apikey でキー取得\n2. 環境変数 GEMINI_API_KEY を設定\n3. アプリを再起動",
           "network": "🌐 ネットワーク接続を確認してください：\n1. Wi-Fiまたはモバイルデータの接続状況を確認\n2. ページを更新して再試行\n3. 時間をおいてからもう一度お試しください",
           "tts": "🔊 音声生成に失敗しました：\n1. 原稿テキストを選択してください\n2. 右クリック → 「読み上げ」 を選択\n3. ブラウザの読み上げ機能をご利用ください",
           "music": "🎵 楽曲情報取得に失敗しました：\n1. 別の年を選択してみてください\n2. 時間をおいてから再試行\n3. インターネット接続を確認してください"
       }
       
       guidance = guidance_map.get(error_type, "時間をおいてから再試行してください")
       if details:
           guidance += f"\n\n詳細: {details}"
       
       st.error(f"❌ {error_type} エラー\n\n{guidance}")
   ```
2. 既存エラー処理をこの関数に置換（1つずつ）
**完了基準**: エラー発生時に具体的な復旧手順が表示される

---

## Step 17: 本番運用向けヘルスチェックエンドポイント
**対象ファイル**: `retro_radio/main.py` 更新
**作業最小単位**:
1. Streamlitのクエリパラメータでヘルスチェック実装:
   ```python
   # main.py ファイル末尾付近、サイドバー描画後
   # ヘルスチェック用クエリパラメータ対応
   query_params = st.query_params
   if query_params.get("health") == "check":
       from retro_radio.utils.health import health_check
       import json
       health_data = health_check()
       st.json(health_data)
       st.stop()  # 他の描画をしない
   ```
2. ヘルスチェックモジュール作成: `retro_radio/utils/health.py`
   ```python
   import streamlit as st
   from ..config import get_settings
   from ..utils.session import get_audio_cache, get_history
   
   def health_check() -> dict:
       """Kubernetes/Liveness probe 用"""
       settings = get_settings()
       return {
           "status": "healthy" if settings.gemini_api_key else "degraded",
           "timestamp": st.time.time() if hasattr(st, 'time') else 0,
           "version": "0.1.0",
           "components": {
               "gemini": "configured" if bool(settings.gemini_api_key) else "missing",
               "cache_entries": len(get_audio_cache()),
               "history_entries": len(get_history()),
               "uptime_seconds": 0  # 実装は省略
           }
       }
   ```
3. テスト: `?health=check` で JSON が返るか確認
**完了基準**: ヘルスチェックエンドポイントが稼働、コンテナオーケストレーション対応

---

## Step 18: ログレベル・出力形式改善
**対象ファイル**: `retro_radio/utils/logging_config.py` 新規
**作業最小単位**:
1. 構造化ロギング実装:
   ```python
   import logging
   import sys
   from datetime import datetime
   import json
   
   class JSONFormatter(logging.Formatter):
       def format(self, record):
           log_entry = {
               "timestamp": datetime.fromtimestamp(record.created).isoformat(),
               "level": record.levelname,
               "logger": record.name,
               "message": record.getMessage(),
               "module": record.module,
               "function": record.funcName,
               "line": record.lineno
           }
           if record.exc_info:
               log_entry["exception"] = self.formatException(record.exc_info)
           return json.dumps(log_entry, ensure_ascii=False)
   
   def setup_logging(level: str = "INFO") -> None:
       logger = logging.getLogger()
       logger.setLevel(getattr(logging, level.upper()))
       
       # コンソールハンドラー
       handler = logging.StreamHandler(sys.stdout)
       handler.setFormatter(JSONFormatter())
       logger.addHandler(handler)
       
       # 既存のハンドラーをクリア
       logger.handlers = [handler]
   ```
2. main.py ファイル先頭で setup_logging() 呼び出し
3. 既存 logging.basicConfig を置換
**完了基準**: ログがJSON形式で出力、ELK stack 等に送信可能

---

## Step 19: 環境別設定ファイルテンプレート
**対象ファイル**: 新規作成複数
**作業最小単位**:
1. .env.example 作成:
   ```
   # .env.example
   RETRO_RADIO_CACHE_TTL=3600
   RETRO_RADIO_MAX_RETRIES=3
   RETRO_RADIO_GEMINI_MODEL=gemini-1.5-flash
   RETRO_RADIO_TTS_LANGUAGE=ja
   RETRO_RADIO_DEFAULT_YEAR=1960
   RETRO_RADIO_MIN_YEAR=1950
   RETRO_RADIO_MAX_YEAR=2025
   # GEMINI_API_KEYは別途設定してください（セキュリティのため.gitignore推奨）
   ```
2. .gittemplate作成:
   ```
   # 開発用
   RETRO_RADIO_DEBUG=true
   
   # 本番用（例）
   # RETRO_RADIO_CACHE_TTL=7200
   # RETRO_RADIO_MAX_RETRIES=5
   ```
3. README.md に設定方法追加
**完了基準**: 新規開発者が .env.example をコピーして即座に開発開始可能

---

## Step 20: デプロイメントドキュメント・チェックリスト
**対象ファイル**: `DEPLOYMENT.md` 新規
**作業最小単位**:
1. デプロイ手順文書作成:
   ```markdown
   # レトロラジオ・タイムマシン デプロイメントガイド
   
   ## 前提条件
   - Python 3.11以上
   - インターネット接続（初回セットアップ時）
   - ストリームlitクラウドアカウント（クラウドデプロイの場合）
   
   ## ローカルデプロイ
   1. リポジトリクローン
   2. `pip install -r requirements.txt`
   3. `.env.example` を `.env` にコピーし、必要な値を設定
   4. `streamlit run retro_radio/main.py`
   5. ブラウザで http://localhost:8501 アクセス
   
   ## ストリームlitクラウドデプロイ
   1. リポジトリをGitHubにPush
   2. streamlit.io で新規アプリ作成
   3. リポジトリを連携
   4. シークレットに GEMINI_API_KEY を設定
   5. デプロイボタンをクリック
   
   ## Dockerデプロイ（オプション）
   1. Dockerfile作成（テンプレートあり）
   2. `docker build -t retro-radio .`
   3. `docker run -p 8501:8501 retro-radio`
   
   ## ヘルスチェック
   - http://[host]:8501/?health=check
   
   ## モニタリング項目
   - レイテンシ（目標: 5秒以内）
   - エラー率（目標: 1%未満）
   - キャッシュヒット率
   - 同時接続数
   ```
2. Dockerfileテンプレート作成: `Dockerfile.template`
   ```dockerfile
   FROM python:3.11-slim
   
   WORKDIR /app
   
   COPY requirements.txt .
   RUN pip install --no-cache-dir -r requirements.txt
   
   COPY . .
   
   EXPOSE 8501
   
   ENV STREAMLIT_SERVER_PORT=8501
   ENV STREAMLIT_SERVER_HEADLESS=true
   
   CMD ["streamlit", "run", "retro_radio/main.py"]
   ```
**完了基盤**: 新規メンバーが5分でデプロイ可能、運用チームが監視項目把握

---

## Step 21: 最終統合テスト・シナリオ検証
**対象ファイル**: `tests/test_final_integration.py` 新規
**作業最小単位**:
1. アクセシビリティ統合テスト:
   ```python
   def test_accessibility_features(streamlit_server):
       """主要アクセシビリティ機能動作確認"""
       page = streamlit_server.new_page()
       page.goto(streamlit_server.url)
       
       # ダークモードシミュレート
       page.emulate_media(color_scheme="dark")
       expect(page.locator('.main-title')).to_have_css('color', 'rgb(255, 255, 255)')
       
       # フォーカス順序確認
       page.locator('input[type="range"]').focus()
       expect(page.locator('input[type="range"]')).to_be_focused()
       page.keyboard.press("Tab")
       expect(page.locator('button:has-text("ラジオを再生する")')).to_be_focused()
       
       # コントラスト確認（簡易）
       expect(page.locator('.script-text')).to_have_css('border-width', '2px')
       
       # タッチターゲットサイズ確認
       button = page.locator('button:has-text("ラジオを再生する")')
       box = button.bounding_box()
       assert box['height'] >= 44 and box['width'] >= 44
   
   def test_internationalization(streamlit_server):
       """多言語対応動作確認"""
       # 日本語デフォルト
       page = streamlit_server.new_page()
       page.goto(streamlit_server.url)
       expect(page.locator('text="レトロラジオ・タイムマシン"')).to_be_visible()
       
       # 英語に切替
       page.locator('text="English"').click()
       page.wait_for_timeout(1000)  # 再描画待機
       expect(page.locator('text="Retro Radio Time Machine"')).to_be_visible()
   
   def test_pwa_features(streamlit_server):
       """PWA基本機能確認"""
       page = streamlit_server.new_page()
       page.goto(streamlit_server.url)
       
       # マニフェスト存在確認
       expect(page.locator('link[rel="manifest"]')).to_have_attribute('href', '/static/manifest.json')
       
       # サービスワーカー登録確認（コンソールログ確認は困難なので存在確認のみ）
       expect(page.locator('script:has-text("service-worker.js")')).to_be_attached()
   ```
2. 全シナリオ実行: `pytest tests/test_final_integration.py -v`
**完了基準**: アクセシビリティ・国際化・PWA・基本機能すべて正常動作

---

## Step 22: パフォーマンスベースライン測定
**対象ファイル**: `tests/test_performance_baseline.py` 新規
**作業最小単位**:
1. レイテンシ測定:
   ```python
   import time
   import pytest
   
   def test_generation_latency_baseline(streamlit_server):
       """エンドツーエンドレイテンシ基準測定"""
       page = streamlit_server.new_page()
       page.goto(streamlit_server.url)
       
       start = time.perf_counter()
       
       # 年選択
       page.locator('input[type="range"]').fill("1980")
       
       # 生成実行
       page.locator('button:has-text("ラジオを再生する")').click()
       
       # 完了待機
       expect(page.locator('text="完成しました！"')).to_be_visible(timeout=30000)
       
       elapsed = time.perf_counter() - start
       assert elapsed < 25.0  # ネットワーク含めて25秒以内（保守的に）
   
   def test_memory_usage_baseline():
       """メモリ使用量ベースライン"""
       import psutil
       import os
       
       process = psutil.Process(os.getpid())
       initial_memory = process.memory_info().rss
       
       # アプリ初期化シミュレート
       from retro_radio.main import main  # 実際にはStreamlit実行が必要
       
       final_memory = process.memory_info().rss
       memory_increase = final_memory - initial_memory
       
       # 50MB以下増加を目標（Streamlit本体含むので generous）
       assert memory_increase < 50 * 1024 * 1024
   ```
2. ベースライン値を記録して将来のリグレッション検出に使用
**完了基準**: 性能基準値取得、将来の変更で著しい劣化がないか監視可能

---

## Step 23: 運用マニュアル・トラブルシューティングガイド
**対象ファイル**: `OPERATIONS.md` 新規
**作業最小単位**:
1. トラブルシューティングセクション作成:
   ```markdown
   # トラブルシューティングガイド
   
   ## よくある問題と解決策
   
   ### Q: 「Gemini APIキーが設定されていません」と表示される
   A: 
   1. https://makersuite.google.com/app/apikey でAPIキーを取得
   2. 環境変数を設定:
      - Windows: `set GEMINI_API_KEY=your-key-here`
      - Linux/Mac: `export GEMINI_API_KEY=your-key-here`
   3. または `.streamlit/secrets.toml` を作成:
      ```toml
      GEMINI_API_KEY = "your-key-here"
      ```
   4. アプリを再起動
   
   ### Q: 音声が再生されない
   A:
   1. ブラウザの音量を確認
   2. タブのミュート状況を確認（右クリック → ミュート解除）
   3. 原稿テキストを選択 → 右クリック → 「読み上げ」 を試す
   4. 別のブラウザで試す（Chrome推奨）
   
   ### Q: ページが読み込み途中で止まる
   A:
   1. ページを更新（F5またはCtrl+R）
   2. ブラウザのキャッシュをクリア（Ctrl+Shift+R）
   3. 拡張機能を無効にして試す（特に広告ブロッカー）
   4. ネットワーク接続を確認
   
   ### Q: スマートフォンでボタンが小さくて押しづらい
   A: 
   このバージョンではタッチターゲットを44x44px以上に最適化済みです。
   それでも小さいと感じる場合は、ブラウズのズーム機能を使用してください。
   
   ### Q: ダークモードにならない
   A:
   1. オペレーティングシステムの設定を確認
   2. Windows: 設定 → 個人用設定 → 色 → モードを「暗い」
   3. Mac: システム設定 → 外観 → ダーク
   4. Android: 設定 → アクセシビリティ → テーマを暗く
   5. iOS: 設定 → 表示と明るさ → ダークモード
   ```
2. 運用チェックリスト追加
**完了基盤**: 運用チームが問題を5分で切り分け可能、ユーザーが自力で解決可能な案内

---

## Step 24: 最終品質ゲート・リリース準備
**対象ファイル**: 総合チェック
**作業最小単位**:
1. リリース前チェックリスト実行:
   - [ ] `pytest -v` 全テスト通過
   - [ ] `pytest --cov=retro_radio --cov-report=term-missing` カバレッジ80%以上
   - [ ] `ruff check .` エラー0
   - [ ] `ruff format --check .` フォーマット違反0
   - [ ] `mypy retro_radio/` エラー0
   - [ ] `bandit -r retro_radio/` セキュリティIssueなし
   - [ ] `safety check` 依存関係脆弱性なし
   - [ ] ビルド成功: `python -m build`
   - [ ] Dockerビルド成功: `docker build -t retro-radio .`
   - [ ] デプロイテスト成功: ストリームlitクラウドテストデプロイ
   - [ ] ドキュメント更新: README.md, DEPLOYMENT.md, OPERATIONS.md
   - [ ] チェンジログ更新: gitcliff --tag v0.1.0 --output CHANGELOG.md
2. タグ打ち・リリース:
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
**完了基準**: リリース候補バージョンv0.1.0が品質ゲート全クリア、デプロイ可能状態

---

## T3 リグレッション防止チェックリスト（極小単位版）
- [ ] ダークモード色変更確認
- [ ] フォーカス金色輪郭表示
- [ ] モーション減少設定対応
- [ ] タッチターゲット44px以上確認
- [ ] ハイコントラスト境界太線
- [ ] スクリーンリーダー基本動作
- [ ] キーボードナビゲーション順序
- [ ] PWAインストールバナー表示
- [ ] 英語表示切替可能
- [ ] 言語選択ドロップダウン動作
- [ ] モファイルブレイクポイント対応
- [ ] ローディング状態案内
- [ ] エラー時具体的復旧ガイダンス
- [ ] ヘルスチェックエンドポイント稼働
- [ ] JSONログ出力確認
- [ ] 環境別設定テンプレート利用可能
- [ ] デプロイドキュメント参照可能
- [ ] 最終統合テスト全通過
- [ ] パフォーマンスベースライン取得
- [ ] 運用マニュアル作成完了
- [ ] 品質ゲート全クリア
- [ ] リリース準備完了

各ステップは literally 1-5行のコード変更またはテスト追加で完了可能。低性能LLMでもコピペベースで実装できます。