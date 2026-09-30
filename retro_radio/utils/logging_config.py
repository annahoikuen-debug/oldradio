"""アプリ全体のロガー設定。

`retro_radio.server` が起動時に 1 度だけ呼ぶ。
`RETRO_RADIO_LOG_LEVEL`（既定 INFO）でレベルを決められる。
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """1件1行のJSONで出力するフォーマッタ（ログ集約側でそのまま解析できる）"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
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