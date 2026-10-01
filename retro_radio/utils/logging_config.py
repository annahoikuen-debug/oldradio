"""アプリ全体のロガー設定。

`retro_radio.server` が起動時に 1 度だけ呼ぶ。
`RETRO_RADIO_LOG_LEVEL`（既定 INFO）でレベルを決められる。
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

#: ログ本文から**必ず**隠す値の直前のラベル。
#:
#: `db/session.py` の `hide_parameters=True` は SQLAlchemy の例外文字列から
#: バインドパラメータを隠すが、**それ以外の経路**（`logger.error(f"... {key}")` を
#: 1 箇所でも書いた瞬間、秘密が全文 JSON ログに残る）は防げない。
#: したがって formatter 側にも第 2 の防衛線を置く。
#:
#: `RETRO_RADIO_SECRET_KEY=deadbeef` のような**環境変数名**も対象。
#: 大文字の変数名には単語境界の空白が入らないため、`_` を境界として扱う。
_SECRET_LABEL_PATTERN = re.compile(
    r"(?i)"
    r"((?:[A-Z0-9]+_)*"  # RETRO_RADIO_ のような大文字プリフィックス
    r"(?:password|passwd|pwd|secret|api[_-]?key|token|authorization|cookie"
    r"|signature|webhook_secret|user_key|private_key))"
    r"(\s*[:=]\s*|\"\s*:\s*\")"
    r"([^\s,;\"'}\)]+)"
)

#: `RETRO_RADIO_SECRET_KEY` のような環境変数名（大文字＋`_`）。
#: (word|word_)*(SECRET|PASSWORD|...)(_WORD)* の形を許す
#: （`SECRET_KEY` / `WEBHOOK_SECRET` / `ACCESS_TOKEN` のように
#: 秘密の語が**前にも後ろにも**付くため）。
_ENV_SECRET_NAME_PATTERN = re.compile(
    r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_?"
    r"(?:PASSWORD|PASSWD|PWD|SECRET|SECRETS|API_KEY|APIKEY|TOKEN|TOKENS"
    r"|AUTHORIZATION|COOKIE|SIGNATURE|USER_KEY|PRIVATE_KEY)"
    r"(?:_[A-Z0-9]+)*)"
    r"(\s*=\s*)"
    r"([^\s,;\"'}\)]+)"
)

#: `Authorization: Bearer eyJ...` のように、**値**が 2 語（スキーム + 値）に分かれる場合。
#: `Bearer` の後ろだけを丸ごと隠す。
_AUTH_SCHEME_PATTERN = re.compile(
    r"(?i)\b(Bearer|Basic|Digest)\s+([A-Za-z0-9._\-+/=]+)"
)

_REDACTED = "***REDACTED***"


def redact_secrets(text: str) -> str:
    """ログ本文から `password=...` 形式.secret の**値**を `***REDACTED***` に換える。

    キー名だけを残し、値だけを隠す（障害調査で「どの変数が対象か」は見えるように）。
    `Authorization: Bearer <token>` はトークン部ごと隠す。
    """
    if not text:
        return text
    # 認証スキームを**先に**処理する。`Authorization: Bearer eyJ...` は
    # ラベル規則（`authorization` + `:`）が先にマッチすると
    # 「値」= `Bearer` だけを隠してトークンが残るため。
    redacted = _AUTH_SCHEME_PATTERN.sub(lambda m: f"{m.group(1)} {_REDACTED}", text)
    redacted = _SECRET_LABEL_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", redacted)
    redacted = _ENV_SECRET_NAME_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", redacted
    )
    return redacted


class JSONFormatter(logging.Formatter):
    """1件1行のJSONで出力するフォーマッタ（ログ集約側でそのまま解析できる）"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            log_entry["exception"] = redact_secrets(self.formatException(record.exc_info))
        return json.dumps(log_entry, ensure_ascii=False)


def resolve_level(level: Optional[str] = None) -> int:
    """レベル名（"INFO" 等）を数値に変換する。不正値は INFO にフォールバック"""
    name = (level if level is not None else os.getenv("RETRO_RADIO_LOG_LEVEL", "INFO"))
    resolved = logging.getLevelName(str(name).strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


_handler_marker = "__retro_radio_json__"


def setup_logging(level: Optional[str] = None) -> None:
    """ルートロガーに1行JSONのハンドラーを1つだけ取り付ける（冪等）

    他ライブラリ（pytest など）が取り付けたハンドラーは尊重し、
    自分が以前付けたものだけを外し直す。
    """
    root = logging.getLogger()
    root.setLevel(resolve_level(level))

    for handler in list(root.handlers):
        if getattr(handler, _handler_marker, False):
            root.removeHandler(handler)
            handler.close()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    setattr(handler, _handler_marker, True)
    root.addHandler(handler)
