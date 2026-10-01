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

def _select_songs(year: int, count: int) -> list[dict]:
    """正本カタログから選曲し、iTunes 形式の dict 列へ揃える。

    HTTP 経路（``server._select_program_songs``）と同じく、
    **選曲関数は 1 回だけ**呼び、その結果を原稿とプレイリストの
    両方に渡す。
    """
    songs = search_itunes_songs(year, count=count)
    return list(select_songs(year, songs, count=count))


def _to_pairs(songs: list[dict]) -> list[tuple[str, str]]:
    """iTunes 形式の曲列を ``(曲名, アーティスト)`` の列にする（台本用）。"""
    return [
        (str(item.get("trackName", "")), str(item.get("artistName", "")))
        for item in songs
        if item.get("trackName")
    ]


async def generate_all_async(
    year: int, month: int, day: int,
    progress: AsyncProgress | None = None
) -> GenerationResult:
    """全生成ステップを非同期実行（順次だがUIブロックしない）"""
    errors = []
    prog = progress or AsyncProgress()
    wanted = max(1, int(settings.medley_song_count))

    # Step 1: 選曲。**原稿より先に 1 回だけ**行う。
    # 台本とプレイリストが別の曲を見る（source monitoring error）は
    # 介護用途では安全要件違反になるため、1 つの選択を共有する。
    prog.update(0, "🎵 その時のヒット曲を探しています...")
    try:
        selected_songs = await run_in_executor(_select_songs, year, wanted)
    except Exception as e:
        logger.error(f"Music search failed: {e}")
        handle_error(e, "MusicSearch")
        from .fallback import get_fallback_songs
        fallback_list = get_fallback_songs(year, count=wanted)
        selected_songs = [
            {"trackName": t, "artistName": a, "previewUrl": None}
            for t, a in fallback_list
        ]
        errors.append("music_fallback_all")
    prog.update(33)

    # Step 2: 原稿生成。**上で選んだ曲をそのまま渡す**（二重選択をしない）。
    prog.update(33, "📝 ラジオ原稿を作成中...")
    try:
        script = await run_in_executor(
            generate_radio_script, year, month, day, "normal", None, _to_pairs(selected_songs)
        )
    except Exception as e:
        logger.error(f"Script generation failed: {e}")
        handle_error(e, "ScriptGeneration")
        from .fallback import generate_fallback_script
        script = generate_fallback_script(year, month, day, songs=_to_pairs(selected_songs))
        errors.append("script_fallback")
    prog.update(66)

    song_title = selected_songs[0].get("trackName", "不明") if selected_songs else "不明"
    artist_name = selected_songs[0].get("artistName", "不明") if selected_songs else "不明"
    preview_url = selected_songs[0].get("previewUrl") if selected_songs else None
    use_fallback = not selected_songs or preview_url is None

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
    """選曲と原稿生成を実行（高速化）

    Notes
    -----
    選曲と原稿生成は**独立に並列化できない**。原稿は「実際に流す曲」の
    曲名を名ざすため、選曲の結果が要先に決まっていなければ
    「司会が A と告げるのに B が流れる」になる（安全要件違反）。
    そのためここでは選曲を 1 回だけ行い、その結果を原稿へ渡す。
    """
    errors = []
    prog = progress or AsyncProgress()
    wanted = max(1, int(settings.medley_song_count))

    prog.update(0, "🎵 選曲と原稿作成を進めます...")
    try:
        selected_songs = await run_in_executor(_select_songs, year, wanted)
    except Exception as e:
        # 旧実装はここを握り潰して空リストを「空の成功」として扱っていた
        # （``music_fallback_all`` を記録しない）。本変種の既存契約は
        # 「並列版は音楽側の失敗を errors に載せない」なので維持する。
        logger.error(f"Music search failed: {e}")
        handle_error(e, "MusicSearch")
        from .fallback import get_fallback_songs
        selected_songs = [
            {"trackName": t, "artistName": a, "previewUrl": None}
            for t, a in get_fallback_songs(year, count=wanted)
        ]

    try:
        script = await run_in_executor(
            generate_radio_script, year, month, day, "normal", None, _to_pairs(selected_songs)
        )
    except Exception as e:
        handle_error(e, "ScriptGeneration")
        from .fallback import generate_fallback_script
        script = generate_fallback_script(year, month, day, songs=_to_pairs(selected_songs))
        errors.append("script_fallback")

    song_title = selected_songs[0].get("trackName", "不明") if selected_songs else "不明"
    artist_name = selected_songs[0].get("artistName", "不明") if selected_songs else "不明"
    preview_url = selected_songs[0].get("previewUrl") if selected_songs else None
    use_fallback = not selected_songs or preview_url is None
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
