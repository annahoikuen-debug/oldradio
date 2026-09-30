import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
from .script_generator import generate_radio_script
from .music_search import search_itunes_songs, select_songs
from .tts import text_to_speech
from ..utils.errors import handle_error
from ..config import get_settings
from ..utils.async_runner import AsyncProgress, run_in_executor

logger = logging.getLogger(__name__)
settings = get_settings()

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
    all_songs: Optional[list[dict]] = None  # 全曲情報（メドレー用）
    
    def __post_init__(self):
        if self.all_songs is None:
            self.all_songs = [{
                "trackName": self.song_title,
                "artistName": self.artist_name,
                "previewUrl": self.preview_url
            }]

async def generate_all_async(
    year: int, month: int, day: int,
    progress: AsyncProgress | None = None
) -> GenerationResult:
    """全生成ステップを非同期実行（順次だがUIブロックしない）"""
    errors = []
    prog = progress or AsyncProgress()
    
    # Step 1: 原稿生成
    prog.update(0, "📝 ラジオ原稿を作成中...")
    try:
        script = await run_in_executor(generate_radio_script, year, month, day)
    except Exception as e:
        logger.error(f"Script generation failed: {e}")
        handle_error(e, "ScriptGeneration")
        from .fallback import generate_fallback_script
        script = generate_fallback_script(year, month, day)
        errors.append("script_fallback")
    prog.update(33)
    
    # Step 2: 楽曲検索
    prog.update(33, "🎵 その時のヒット曲を探しています...")
    try:
        songs = await run_in_executor(search_itunes_songs, year)
        selected_songs = select_songs(year, songs, count=settings.medley_song_count)
        song_title = selected_songs[0].get("trackName", "不明")
        artist_name = selected_songs[0].get("artistName", "不明")
        preview_url = selected_songs[0].get("previewUrl")
        use_fallback = preview_url is None
    except Exception as e:
        logger.error(f"Music search failed: {e}")
        handle_error(e, "MusicSearch")
        from .fallback import get_fallback_songs
        fallback_list = get_fallback_songs(year, count=settings.medley_song_count)
        selected_songs = [{"trackName": t, "artistName": a, "previewUrl": None} for t, a in fallback_list]
        song_title = selected_songs[0].get("trackName", "不明")
        artist_name = selected_songs[0].get("artistName", "不明")
        preview_url, use_fallback = None, True
        errors.append("music_fallback_all")
    prog.update(66)
    
    # Step 3: 音声合成
    prog.update(66, "🎙️ 音声を合成しています...")
    try:
        audio_path = await run_in_executor(text_to_speech, script)
    except Exception as e:
        logger.error(f"TTS failed: {e}")
        handle_error(e, "TTS")
        audio_path = None
        errors.append("tts_failed")
    prog.complete("生成完了")
    
    return GenerationResult(
        script=script,
        song_title=song_title,
        artist_name=artist_name,
        preview_url=preview_url,
        audio_path=audio_path,
        use_fallback_song=use_fallback,
        errors=errors,
        all_songs=selected_songs
    )

# 将来用: 並列実行版（原稿・楽曲検索を同時実行）
async def generate_all_parallel(
    year: int, month: int, day: int,
    progress: AsyncProgress | None = None
) -> GenerationResult:
    """原稿生成と楽曲検索を並列実行（高速化）"""
    errors = []
    prog = progress or AsyncProgress()
    
    prog.update(0, "📝🎵 原稿作成と楽曲検索を並行実行中...")
    script_task = run_in_executor(generate_radio_script, year, month, day)
    songs_task = run_in_executor(search_itunes_songs, year)
    
    script, songs = await asyncio.gather(script_task, songs_task, return_exceptions=True)
    
    if isinstance(script, Exception):
        handle_error(script, "ScriptGeneration")
        from .fallback import generate_fallback_script
        script = generate_fallback_script(year, month, day)
        errors.append("script_fallback")
    
    if isinstance(songs, Exception):
        handle_error(songs, "MusicSearch")
        songs = []
    
    selected_songs = select_songs(year, songs, count=settings.medley_song_count)
    song_title = selected_songs[0].get("trackName", "不明")
    artist_name = selected_songs[0].get("artistName", "不明")
    preview_url = selected_songs[0].get("previewUrl")
    use_fallback = preview_url is None
    prog.update(50)
    
    prog.update(50, "🎙️ 音声を合成しています...")
    try:
        audio_path = await run_in_executor(text_to_speech, script)
    except Exception as e:
        logger.error(f"TTS failed: {e}")
        handle_error(e, "TTS")
        audio_path = None
        errors.append("tts_failed")
    prog.complete("生成完了")
    
    return GenerationResult(
        script=script, song_title=song_title, artist_name=artist_name,
        preview_url=preview_url, audio_path=audio_path,
        use_fallback_song=use_fallback, errors=errors,
        all_songs=selected_songs
    )
