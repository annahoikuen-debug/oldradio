"""TTS engine abstraction.

gTTS (Google Translate voice) sounds robotic because it is a concatenative
voice, and it also has a character limit plus Google's 429/CAPTCHA rate limit.
This module makes `edge-tts` (Microsoft Edge neural voices, free for
commercial use) the primary engine and falls back to gTTS when the library is
missing or a synthesis call fails.

`edge-tts` accepts rate / pitch / volume, so those settings become part of the
cache key. Without that, changing the voice settings would silently reuse the
previously generated file.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("retro_radio")

ENGINE_EDGE = "edge"
ENGINE_GTTS = "gtts"


class EdgeTTSUnavailable(RuntimeError):
    """edge-tts is not importable, or synthesis through it failed."""


@dataclass(frozen=True)
class EdgeTTSOptions:
    """Synthesis options handed to edge-tts, and part of the cache key."""

    voice: str = "ja-JP-NanamiNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"

    def cache_token(self) -> str:
        return f"{self.voice}_{self.rate}_{self.pitch}_{self.volume}"


# Japanese Edge voices that suit a listening-assistance radio programme.
SUPPORTED_JA_VOICES = (
    "ja-JP-NanamiNeural",
    "ja-JP-KeitaNeural",
    "ja-JP-MasaruNeural",
)


def edge_tts_available() -> bool:
    """Whether edge-tts can be imported (a harmless availability probe)."""
    try:
        import edge_tts  # noqa: F401
    except Exception:  # pragma: no cover - environment without the dependency
        return False
    return True


def resolve_engine(configured: str) -> str:
    """Pick the engine to use from the configured value.

    ``auto`` uses edge when edge-tts is installed, otherwise gTTS. ``edge``
    still falls back to gTTS when the library is absent so a missing optional
    dependency never breaks startup.
    """
    value = (configured or "auto").strip().lower()
    if value == ENGINE_GTTS:
        return ENGINE_GTTS
    if value == ENGINE_EDGE:
        return ENGINE_EDGE if edge_tts_available() else ENGINE_GTTS
    return ENGINE_EDGE if edge_tts_available() else ENGINE_GTTS


def _run(coro):
    """Run a coroutine on a dedicated event loop and close it afterwards.

    The server itself already runs inside a loop, but synthesis is called from
    job worker threads, so an explicit loop avoids interfering with it and
    leaves no loop behind on the thread.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def synthesize_to_file(text: str, path: str, options: EdgeTTSOptions) -> None:
    """Synthesize `text` with edge-tts and write the mp3 to `path`.

    Raises
    ------
    EdgeTTSUnavailable
        edge-tts is missing or the synthesis failed. Callers may fall back to
        another engine.
    """
    try:
        import edge_tts
    except Exception as exc:  # pragma: no cover - environment without the dependency
        raise EdgeTTSUnavailable(f"edge-tts is unavailable: {exc}") from exc

    async def _save() -> None:
        communicate = edge_tts.Communicate(
            text,
            options.voice,
            rate=options.rate,
            pitch=options.pitch,
            volume=options.volume,
        )
        await communicate.save(path)

    try:
        _run(_save())
    except Exception as exc:
        raise EdgeTTSUnavailable(f"edge-tts synthesis failed: {exc}") from exc
