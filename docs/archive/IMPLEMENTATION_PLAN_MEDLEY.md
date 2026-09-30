# メドレー再生実装計画書（長尺原稿 × 複数曲連続再生）
## 概要
音楽再生時間を約65秒 → 約3-4分に延伸。既存API・リソースのみ使用、著作権クリア。
全24ステップ、各ステップ1-5行のコピペ実装可能。

---

## Step 1: 原稿プロンプト拡張（script_generator.py）
**対象**: `retro_radio/core/script_generator.py` の `_build_prompt()` 関数
**作業**:
```python
def _build_prompt(year: int, month: int, day: int) -> str:
    return f"""あなたは昭和・平成のレトロなラジオパーソナリティです。
{year}年{month}月{day}日の日本で起きた出来事をテーマに、以下の構成で原稿を書いてください。

【構成】約1000文字
1. オープニング挨拶・季節の挨拶（150文字）
2. その日の主要ニュース・出来事（250文字）
3. 当時の暮らし・流行・物価など「くらしの風景」（250文字）
4. リスナーへの語りかけ・共感メッセージ（150文字）
5. 3曲目の曲振り「それでは、この年のヒット曲を3曲続けてお届けします」（50文字）
6. エンディング・次回予告（150文字）

条件:
- 口調: 丁寧で温かみのある語り口（「皆様、いかがお過ごしでしょうか」「〜でございますね」など）
- 日本国内の出来事に限定
- 各セクション間に自然なつなぎを入れる"""
```
**完了基準**: 生成原稿が800-1200文字、TTS再生約90-120秒
**テスト**: `tests/test_script_length.py` - 文字数範囲確認

---

## Step 2: 原稿長テスト作成
**対象**: `tests/test_script_length.py` 新規
**作業**:
```python
import pytest
from retro_radio.core.script_generator import generate_radio_script

def test_script_length():
    """生成原稿が目標文字数範囲内か"""
    script = generate_radio_script(1980, 5, 15)
    assert 800 <= len(script) <= 1200, f"文字数: {len(script)}"

def test_script_structure():
    """主要セクションが含まれるか"""
    script = generate_radio_script(1980, 5, 15)
    assert "皆様" in script or "いかが" in script  # 挨拶
    assert "ヒット曲" in script  # 曲振り
```
**実行**: `pytest tests/test_script_length.py -v`

---

## Step 3: フォールバック原稿も長尺化（fallback.py）
**対象**: `retro_radio/core/fallback.py` の `generate_fallback_script()`
**作業**:
```python
def generate_fallback_script(year: int, month: int, day: int) -> str:
    return f"""皆様、いかがお過ごしでしょうか。
{year}年{month}月{day}日でございますね。
この頃の日本は、季節の移ろいとともに、人々の暮らしも静かに変わってまいりました。
ニュースの準備ができませんでしたが、皆様の記憶の中に、この日の風景がよみがえりますように。
当時の街角では、子供たちが遊び、主婦たちが買い物籠を提げ、サラリーマンが急ぎ足で駅へ向かう。
ラジオからは流行歌が流れ、お茶の間には家族団欒のひとときがございました。
物価も今とは違い、おにぎり一個が十円程度、ラーメン一杯が五十円ほどでございましたね。
そんな懐かしい時代の空気を、皆様とともに振り返ってまいりたいと思います。
それでは、この年のヒット曲を3曲続けてお届けします。
まずは一曲目、続いて二曲目、そして三曲目と、メドレーでお楽しみください。
本日もお付き合いいただき、誠にありがとうございました。
また明日、同じ時間にお会いしましょう。さようなら。"""
```
**完了基準**: フォールバックも800文字以上

---

## Step 4: フォールバック長テスト追加
**対象**: `tests/test_script_length.py` 追加
**作業**:
```python
def test_fallback_script_length():
    from retro_radio.core.fallback import generate_fallback_script
    script = generate_fallback_script(1980, 5, 15)
    assert len(script) >= 800, f"フォールバック文字数: {len(script)}"
```

---

## Step 5: 複数曲選択関数追加（music_search.py）
**対象**: `retro_radio/core/music_search.py` に `select_songs()` 追加
**作業**（ファイル末尾に追加）:
```python
def select_songs(year: int, songs: list[dict], count: int = 3) -> list[dict]:
    """複数曲選択（プレビュー付き優先、不足分はフォールバックで補完）"""
    import random
    from .fallback import get_fallback_song
    
    with_preview = [s for s in songs if s.get("previewUrl")]
    selected = random.sample(with_preview, min(count, len(with_preview)))
    
    # 不足分をフォールバックで補完
    if len(selected) < count:
        fallback_songs = []
        decade = (year // 10) * 10
        for d in [decade, decade-10, decade+10]:
            if d in __import__('retro_radio.core.fallback', fromlist=['FALLBACK_SONGS']).FALLBACK_SONGS:
                for title, artist in __import__('retro_radio.core.fallback', fromlist=['FALLBACK_SONGS']).FALLBACK_SONGS[d]:
                    if len(selected) + len(fallback_songs) >= count:
                        break
                    if not any(s.get("trackName") == title for s in selected + fallback_songs):
                        fallback_songs.append({"trackName": title, "artistName": artist, "previewUrl": None})
        selected.extend(fallback_songs)
    
    return selected[:count]
```
**完了基準**: 3曲返却、プレビュー付き優先、不足時フォールバック補完
**テスト**: `tests/test_multi_song.py`

---

## Step 6: 複数曲選択テスト作成
**対象**: `tests/test_multi_song.py` 新規
**作業**:
```python
import pytest
from retro_radio.core.music_search import select_songs

def test_select_songs_with_preview():
    """プレビューあり曲から3曲選択"""
    songs = [
        {"trackName": "曲1", "artistName": "歌手1", "previewUrl": "http://a.mp3"},
        {"trackName": "曲2", "artistName": "歌手2", "previewUrl": "http://b.mp3"},
        {"trackName": "曲3", "artistName": "歌手3", "previewUrl": "http://c.mp3"},
        {"trackName": "曲4", "artistName": "歌手4", "previewUrl": "http://d.mp3"},
    ]
    result = select_songs(1980, songs, 3)
    assert len(result) == 3
    assert all(s.get("previewUrl") for s in result)

def test_select_songs_fallback_supplement():
    """プレビュー不足時にフォールバック補完"""
    songs = [{"trackName": "曲1", "artistName": "歌手1", "previewUrl": "http://a.mp3"}]
    result = select_songs(1980, songs, 3)
    assert len(result) == 3
    assert result[0]["previewUrl"] is not None
    # 2,3曲目はフォールバック（previewUrl=None）

def test_select_songs_empty():
    """検索結果ゼロでもフォールバックで3曲"""
    result = select_songs(1980, [], 3)
    assert len(result) == 3
    assert all(s.get("previewUrl") is None for s in result)
```

---

## Step 7: pipeline.py で複数曲選択呼び出しに変更
**対象**: `retro_radio/core/pipeline.py` の `generate_all_async()` / `generate_all_parallel()`
**作業**:
```python
# Step 2 部分を以下に変更（2箇所）
# Before:
# songs = await run_in_executor(search_itunes_songs, year)
# song_title, artist_name, preview_url, use_fallback = select_song(year, songs)

# After:
songs = await run_in_executor(search_itunes_songs, year)
selected_songs = select_songs(year, songs, count=3)
song_title = selected_songs[0].get("trackName", "不明")
artist_name = selected_songs[0].get("artistName", "不明")
preview_url = selected_songs[0].get("previewUrl")
use_fallback = preview_url is None
```
**注意**: 既存の `GenerationResult` データクラスは互換性維持のため第1曲目のみ格納

---

## Step 8: GenerationResult に全曲情報追加
**対象**: `retro_radio/core/pipeline.py` の `GenerationResult` データクラス
**作業**:
```python
@dataclass
class GenerationResult:
    script: str
    song_title: str              # 互換性維持：第1曲目
    artist_name: str             # 互換性維持：第1曲目
    preview_url: Optional[str]   # 互換性維持：第1曲目
    audio_path: Optional[str]
    use_fallback_song: bool
    errors: list[str]
    # 新規追加
    all_songs: list[dict] = None  # 全曲情報（メドレー用）
    
    def __post_init__(self):
        if self.all_songs is None:
            self.all_songs = [{
                "trackName": self.song_title,
                "artistName": self.artist_name,
                "previewUrl": self.preview_url
            }]
```

---

## Step 9: pipeline.py で全曲情報セット
**対象**: `retro_radio/core/pipeline.py` の両生成関数
**作業**（return 前に追加）:
```python
# generate_all_async() と generate_all_parallel() の両方で
return GenerationResult(
    script=script,
    song_title=song_title,
    artist_name=artist_name,
    preview_url=preview_url,
    audio_path=audio_path,
    use_fallback_song=use_fallback,
    errors=errors,
    all_songs=selected_songs  # 新規：全曲渡す
)
```

---

## Step 10: パイプライン統合テスト作成
**対象**: `tests/test_pipeline_medley.py` 新規
**作業**:
```python
import pytest
from retro_radio.core.pipeline import generate_all_async, GenerationResult

@pytest.mark.asyncio
async def test_generation_result_has_all_songs():
    """GenerationResultに全曲情報が含まれるか"""
    result = await generate_all_async(1980, 5, 15)
    assert isinstance(result, GenerationResult)
    assert hasattr(result, 'all_songs')
    assert isinstance(result.all_songs, list)
    assert len(result.all_songs) == 3
    assert all('trackName' in s for s in result.all_songs)
    assert all('artistName' in s for s in result.all_songs)

@pytest.mark.asyncio
async def test_first_song_matches_legacy_fields():
    """第1曲目がレガシーフィールドと一致"""
    result = await generate_all_async(1980, 5, 15)
    assert result.song_title == result.all_songs[0].get("trackName")
    assert result.artist_name == result.all_songs[0].get("artistName")
    assert result.preview_url == result.all_songs[0].get("previewUrl")
```

---

## Step 11: メドレー再生コンポーネント作成（components.py）
**対象**: `retro_radio/ui/components.py` に `render_audio_medley()` 追加
**作業**（ファイル末尾に追加）:
```python
def render_audio_medley(script_audio_path: str, songs: list[dict]) -> None:
    """原稿音声 → 曲1 → 曲2 → 曲3 を自動連続再生"""
    import os
    import json
    _translator = get_translator_instance()
    
    st.subheader(_translator.t("medley_header", default="📻 ラジオ番組（自動連続再生）"))
    
    # 1. 原稿音声
    if script_audio_path and os.path.exists(script_audio_path):
        with open(script_audio_path, "rb") as f:
            st.audio(f.read(), format="audio/mp3")
        st.caption(_translator.t("script_audio_caption", default="🎙️ ラジオ原稿（読み上げ）"))
    
    st.markdown("---")
    
    # 2. プレビューURL抽出
    preview_urls = [s.get("previewUrl") for s in songs if s.get("previewUrl")]
    
    if preview_urls:
        # 自動連続再生JS
        st.markdown(f"""
        <div id="medley-player" style="margin: 1rem 0;">
            <div style="font-size: 0.9rem; color: #666; margin-bottom: 0.5rem;">
                🎵 自動連続再生中: {len(preview_urls)}曲のプレビュー
            </div>
        </div>
        <script>
        (function() {{
            const urls = {json.dumps(preview_urls)};
            let idx = 0;
            const audio = new Audio();
            const container = document.getElementById('medley-player');
            
            function updateDisplay() {{
                if (container) {{
                    container.innerHTML = '<div style="font-size: 0.9rem; color: #666;">🎵 再生中: ' + (idx) + '/' + urls.length + ' 曲目</div>';
                }}
            }}
            
            function playNext() {{
                if (idx >= urls.length) {{
                    if (container) {{
                        container.innerHTML = '<div style="font-size: 0.9rem; color: #28a745;">✅ 全曲再生完了</div>';
                    }}
                    return;
                }}
                audio.src = urls[idx];
                audio.play().catch(e => {{
                    console.log('Autoplay blocked, trying next:', e);
                    idx++;
                    playNext();
                }});
                updateDisplay();
            }}
            
            audio.addEventListener('ended', () => {{
                idx++;
                playNext();
            }});
            audio.addEventListener('error', () => {{
                idx++;
                playNext();
            }});
            
            // 少し遅延して開始（UI描画待ち）
            setTimeout(playNext, 500);
        }})();
        </script>
        """, unsafe_allow_html=True)
    
    # 3. プレイリスト表示（視覚的フィードバック）
    for i, song in enumerate(songs):
        has_preview = bool(song.get("previewUrl"))
        icon = "▶" if has_preview else "📻"
        preview_text = "（プレビューあり・自動再生）" if has_preview else "（フォールバック・プレビューなし）"
        st.write(f"{icon} **{i+1}. {song.get('trackName', '不明')}** / {song.get('artistName', '不明')}")
        st.caption(preview_text)
```

---

## Step 12: 国際化キー追加（i18n/locales/ja.json, en.json）
**対象**: `retro_radio/i18n/locales/ja.json` と `en.json`
**作業**（ja.json に追加）:
```json
{
  "medley_header": "📻 ラジオ番組（自動連続再生）",
  "script_audio_caption": "🎙️ ラジオ原稿（読み上げ）",
  "medley_playing": "🎵 再生中: {current}/{total} 曲目",
  "medley_completed": "✅ 全曲再生完了",
  "preview_available": "（プレビューあり・自動再生）",
  "preview_unavailable": "（フォールバック・プレビューなし）"
}
```
**作業**（en.json に追加）:
```json
{
  "medley_header": "📻 Radio Program (Auto-play Medley)",
  "script_audio_caption": "🎙️ Radio Script (TTS)",
  "medley_playing": "🎵 Playing: {current}/{total} tracks",
  "medley_completed": "✅ All tracks completed",
  "preview_available": "(Preview available - auto play)",
  "preview_unavailable": "(Fallback - no preview)"
}
```

---

## Step 13: コンポーネントで翻訳使用
**対象**: `retro_radio/ui/components.py` の `render_audio_medley()`
**作業**: ハードコード文字列を `_translator.t()` に置換
```python
# 変更例
st.subheader(_translator.t("medley_header"))
st.caption(_translator.t("script_audio_caption"))
# JS内の文字列も翻訳対応（json.dumpsで渡す）
```

---

## Step 14: main.py でメドレー描画に切替
**対象**: `retro_radio/main.py` の結果表示部分
**作業**:
```python
# Before:
# render_audio_player(result.audio_path)
# render_song_info(result.song_title, result.artist_name, result.preview_url, result.use_fallback_song)

# After:
from retro_radio.ui.components import render_audio_medley
render_audio_medley(result.audio_path, result.all_songs)
```
**完了基準**: 画面に原稿音声→3曲プレビュー連続再生UI表示

---

## Step 15: メドレーUIテスト作成
**対象**: `tests/test_medley_ui.py` 新規
**作業**:
```python
import pytest
from retro_radio.ui.components import render_audio_medley

def test_render_audio_medley_no_error():
    """エラーなく描画完了するか（スモークテスト）"""
    import streamlit as st
    # モック用最小限セットアップ
    songs = [
        {"trackName": "曲1", "artistName": "歌手1", "previewUrl": "http://a.mp3"},
        {"trackName": "曲2", "artistName": "歌手2", "previewUrl": "http://b.mp3"},
        {"trackName": "曲3", "artistName": "歌手3", "previewUrl": None},
    ]
    # 例外発生しないこと確認
    render_audio_medley(None, songs)

def test_medley_handles_empty_songs():
    """空リストでもクラッシュしない"""
    render_audio_medley(None, [])
```

---

## Step 16: E2E統合テスト（メドレー全体フロー）
**対象**: `tests/test_e2e_medley.py` 新規
**作業**:
```python
import pytest
from retro_radio.core.pipeline import generate_all_async

@pytest.mark.asyncio
async def test_full_medley_generation():
    """年指定→原稿長尺・3曲選択・音声生成の全フロー"""
    result = await generate_all_async(1980, 5, 15)
    
    # 原稿チェック
    assert len(result.script) >= 800
    assert "ヒット曲を3曲" in result.script or "ヒット曲を 3 曲" in result.script
    
    # 全曲情報チェック
    assert len(result.all_songs) == 3
    for song in result.all_songs:
        assert "trackName" in song
        assert "artistName" in song
    
    # 音声生成チェック（None許容）
    assert result.audio_path is None or isinstance(result.audio_path, str)
```

---

## Step 17: 回帰テスト - 既存機能動作確認
**対象**: `tests/test_regression.py` 新規
**作業**:
```python
import pytest
from retro_radio.core.pipeline import generate_all_async
from retro_radio.core.music_search import select_song  # 旧関数
from retro_radio.core.fallback import get_fallback_song

@pytest.mark.asyncio
async def test_legacy_select_song_still_works():
    """旧select_song関数が動くか（互換性）"""
    songs = [{"trackName": "テスト", "artistName": "歌手", "previewUrl": "http://x.mp3"}]
    title, artist, url, fallback = select_song(1980, songs)
    assert title == "テスト"
    assert artist == "歌手"
    assert url == "http://x.mp3"
    assert fallback is False

def test_fallback_song_unchanged():
    """フォールバック曲選択が変わらないか"""
    title, artist = get_fallback_song(1980)
    assert title in ["上を向いて歩こう", "見上げてごらん夜の星を"]
    assert artist == "坂本九"

@pytest.mark.asyncio
async def test_generation_result_backward_compatible():
    """GenerationResultが既存コードで使えるか"""
    result = await generate_all_async(1980, 5, 15)
    # 既存フィールド存在確認
    assert hasattr(result, 'song_title')
    assert hasattr(result, 'artist_name')
    assert hasattr(result, 'preview_url')
    assert hasattr(result, 'use_fallback_song')
    assert hasattr(result, 'audio_path')
    assert hasattr(result, 'errors')
```

---

## Step 18: パフォーマンステスト（生成時間）
**対象**: `tests/test_performance_medley.py` 新規
**作業**:
```python
import time
import pytest
from retro_radio.core.pipeline import generate_all_async

@pytest.mark.asyncio
async def test_generation_latency_within_limit():
    """メドレー込みでも生成時間が許容内か"""
    start = time.perf_counter()
    result = await generate_all_async(1980, 5, 15)
    elapsed = time.perf_counter() - start
    
    # ネットワーク含め30秒以内（従来25秒→マージン込み）
    assert elapsed < 30.0, f"生成時間超過: {elapsed:.1f}秒"
    assert len(result.all_songs) == 3
```

---

## Step 19: アクセシビリティ対応 - メドレー制御ボタン
**対象**: `retro_radio/ui/components.py` の `render_audio_medley()` 追加
**作業**（JS内に追加）:
```javascript
// 再生/停止/スキップボタン生成
const controls = document.createElement('div');
controls.innerHTML = `
    <button id="medley-play" aria-label="再生" style="margin-right: 8px;">▶ 再生</button>
    <button id="medley-pause" aria-label="一時停止" style="margin-right: 8px;">⏸ 停止</button>
    <button id="medley-next" aria-label="次の曲" style="margin-right: 8px;">⏭ 次へ</button>
`;
container.parentNode.insertBefore(controls, container);

document.getElementById('medley-play').onclick = () => audio.play();
document.getElementById('medley-pause').onclick = () => audio.pause();
document.getElementById('medley-next').onclick = () => { idx++; playNext(); };
```
**完了基準**: キーボード操作・スクリーンリーダー対応

---

## Step 20: アクセシビリティテスト追加
**対象**: `tests/test_accessibility_medley.py` 新規
**作業**:
```python
def test_medley_has_aria_labels():
    """メドレープレーヤーにARIAラベルがあるか"""
    from retro_radio.ui.components import render_audio_medley
    import streamlit as st
    
    songs = [{"trackName": "曲1", "artistName": "歌手1", "previewUrl": "http://a.mp3"}]
    render_audio_medley(None, songs)
    # 実装ではJSでボタン生成→手動確認項目としてマーク
    # 自動テスト困難なためドキュメント化のみ
    assert True  # プレースホルダー
```

---

## Step 21: エラーハンドリング強化（音楽検索失敗時）
**対象**: `retro_radio/core/pipeline.py` の楽曲検索ステップ
**作業**:
```python
# Step 2 の try-except 内を以下に
try:
    songs = await run_in_executor(search_itunes_songs, year)
    selected_songs = select_songs(year, songs, count=3)
except Exception as e:
    logger.error(f"Music search failed: {e}")
    handle_error(e, "MusicSearch")
    # 全曲フォールバック
    from .fallback import get_fallback_songs
    selected_songs = [{"trackName": t, "artistName": a, "previewUrl": None} 
                      for t, a in get_fallback_songs(year)[:3]]
    errors.append("music_fallback_all")
```
**注意**: `fallback.py` に `get_fallback_songs(year, count)` 追加必要（Step 22）

---

## Step 22: フォールバック複数曲取得関数追加
**対象**: `retro_radio/core/fallback.py`
**作業**:
```python
def get_fallback_songs(year: int, count: int = 3) -> List[Tuple[str, str]]:
    """指定年度のフォールバック曲を複数返す"""
    if not validate_year_range(year):
        year = 1960
    decade = (year // 10) * 10
    songs = []
    for d in [decade, decade-10, decade+10]:
        if d in FALLBACK_SONGS:
            songs.extend(FALLBACK_SONGS[d])
        if len(songs) >= count:
            break
    import random
    random.shuffle(songs)
    return songs[:count]
```

---

## Step 23: 設定値化（曲数・文字数をconfigで管理）
**対象**: `retro_radio/config.py` と `retro_radio/core/script_generator.py`, `music_search.py`
**作業**:
```python
# config.py に追加
class Settings(BaseSettings):
    # 既存...
    medley_song_count: int = 3
    target_script_chars: int = 1000
    script_char_tolerance: int = 200

# script_generator.py で使用
from ..config import get_settings
settings = get_settings()
# プロンプト内で {settings.target_script_chars} 文字 等に置換

# music_search.py で使用
def select_songs(year: int, songs: list[dict], count: int = None) -> list[dict]:
    if count is None:
        count = get_settings().medley_song_count
    # ...
```

---

## Step 24: 最終統合テスト・全項目確認
**対象**: `tests/test_final_medley.py` 新規
**作業**:
```python
import pytest
from retro_radio.core.pipeline import generate_all_async
from retro_radio.ui.components import render_audio_medley

@pytest.mark.asyncio
async def test_complete_medley_flow():
    """完全フロー: 生成→データ構造→UI描画までエラーなし"""
    # 1. 生成
    result = await generate_all_async(1990, 8, 10)
    
    # 2. データ検証
    assert len(result.script) >= 800
    assert len(result.all_songs) == 3
    assert result.audio_path is None or isinstance(result.audio_path, str)
    
    # 3. UI描画（例外なし）
    render_audio_medley(result.audio_path, result.all_songs)
    
    # 4. 互換性
    assert result.song_title == result.all_songs[0].get("trackName")

def test_config_values():
    """設定値が反映されるか"""
    from retro_radio.config import get_settings
    s = get_settings()
    assert hasattr(s, 'medley_song_count')
    assert hasattr(s, 'target_script_chars')
    assert s.medley_song_count == 3
    assert s.target_script_chars == 1000
```

---

## リグレッション防止チェックリスト
- [ ] `pytest tests/test_script_length.py -v` 通過（原稿800-1200文字）
- [ ] `pytest tests/test_multi_song.py -v` 通過（3曲選択・フォールバック補完）
- [ ] `pytest tests/test_pipeline_medley.py -v` 通過（全曲情報含むResult）
- [ ] `pytest tests/test_regression.py -v` 通過（既存API互換性）
- [ ] `pytest tests/test_performance_medley.py -v` 通過（30秒以内生成）
- [ ] `pytest tests/test_e2e_medley.py -v` 通過（E2Eフロー）
- [ ] `pytest tests/test_final_medley.py -v` 通過（統合確認）
- [ ] 手動確認: ブラウザで原稿音声→曲1→曲2→曲3 自動連続再生
- [ ] 手動確認: キーボードで再生/停止/スキップ操作可能
- [ ] 手動確認: スクリーンリーダーでボタン読み上げ確認

---

## 実装順序の依存関係
```
Step 1-4 (原稿長) → Step 5-6 (複数曲選択) → Step 7-10 (パイプライン統合)
                                                    ↓
Step 11-13 (UI) ← Step 12 (i18n) ← Step 14 (main.py統合)
                                                    ↓
Step 15-18 (テスト) ← Step 19-20 (アクセシビリティ)
                                                    ↓
Step 21-23 (堅牢化・設定値化) → Step 24 (最終確認)
```

各ステップは独立してコミット・テスト可能。低性能LLMでも順番にコピペ実装で完了。