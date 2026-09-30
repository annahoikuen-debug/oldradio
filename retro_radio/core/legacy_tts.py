"""旧来の Google Translate TTS（`GET /translate_tts`）で音声を合成する。

なぜこれが要るのか
------------------
`gTTS` 2.5.4 は Google 翻訳 Web UI と同じ RPC に POST する:

    POST /_/TranslateWebserverUi/data/batchexecute

このエンドポイントには Google のボット対策が架かっていて、
スクリプトからのアクセスには CAPTCHA 応答が返り HTTP 429 になる
（実測: "Our systems have detected unusual traffic from your computer network"）。
`gTTS` は CAPTCHA を解けないので、何度リトライしても必ず失敗する。

一方、旧来の GET エンドポイント

    GET /translate_tts?ie=UTF-8&client=tw-ob&tl=<lang>&q=<text>

は別のコードパスで comparatively 保護が弱く、**現時点では 200 が返り
有効な MP3 が得られる**（実測: 5 セグメント / 1269 文字 / 138 秒分）。
`tld` や User-Agent / Referer を変えても挙動は変わらないため、
ブロック有理的不是「IP bans」也不是「User-Agent」，而是端点そのものの防护。

したがってこれは**代替経路**であって正解ではない。
Google がこのエンドポイントも塞げば動かなくなる。
"""

import logging
import urllib.parse
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

# Google の TTS は 1 リクエストあたり 100 文字まで。旧 gTTS もこれで分割していた。
MAX_CHARS_PER_REQUEST = 100

_HEADERS = {
    "Referer": "https://translate.google.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

TIMEOUT = 20


class LegacyTTSError(Exception):
    """旧エンドポイントでの合成に失敗した"""


def _chunk(text: str) -> List[str]:
    """Google の 1 リクエスト上限（100 文字）ごとに分割する。"""
    return [text[i:i + MAX_CHARS_PER_REQUEST] for i in range(0, len(text), MAX_CHARS_PER_REQUEST)]


def _fetch_chunk(part: str, lang: str, tld: str) -> bytes:
    query = urllib.parse.quote(part)
    url = (
        "https://translate.google.%s/translate_tts"
        "?ie=UTF-8&client=tw-ob&tl=%s&q=%s" % (tld, lang, query)
    )
    try:
        response = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise LegacyTTSError("旧 TTS エンドポイントへの接続に失敗しました: %s" % exc) from exc

    if response.status_code != 200:
        raise LegacyTTSError(
            "旧 TTS エンドポイントが %s を返しました" % response.status_code
        )

    audio = response.content
    if not audio:
        raise LegacyTTSError("旧 TTS エンドポイントが空の応答を返しました")
    #  Google's MP3 は必ず 0xFF で始まる MPEG 同期バイトを持つ
    if not audio.startswith(b"\xff"):
        raise LegacyTTSError(
            "旧 TTS エンドポイントが MP3 ではない応答を返しました"
        )
    return audio


def synthesize(text: str, lang: str = "ja", tld: str = "com") -> bytes:
    """テキストを MP3 バイト列へ合成する。

    Args:
        text: 読み上げるテキスト
        lang: IETF 言語タグ
        tld: translate.google.<tld> の TLD

    Raises:
        LegacyTTSError: 合成に失敗した場合
    """
    text = (text or "").strip()
    if not text:
        raise LegacyTTSError("読み上げるテキストが空です")

    parts = _chunk(text)
    audio = bytearray()
    for index, part in enumerate(parts, start=1):
        audio += _fetch_chunk(part, lang, tld)
        if len(parts) > 1:
            logger.debug(
                "旧 TTS 取得 %d/%d チャンク（%d 文字）", index, len(parts), len(part)
            )
    return bytes(audio)


def save(text: str, path: str, lang: str = "ja", tld: str = "com") -> Optional[str]:
    """`path` へ書き出してそのパスを返す。失敗したら None。

    `_save_tts_to_temp` と同じ「`save()` を持つオブジェクト」を期待されるため、
    `gTTS` と差し替え可能な形にしている。
    """
    audio = synthesize(text, lang=lang, tld=tld)
    with open(path, "wb") as handle:
        handle.write(audio)
    return path
