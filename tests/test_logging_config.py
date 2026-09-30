"""`retro_radio.utils.logging_config` の検証。

このモジュールは構文的に壊れた import 不能な状態で放置されていた（`"    "="INFO")`）。
アプリLoggerの唯一の設定経路として `retro_radio.server` から呼ばれるため、
import できること・出力形式・冪等性をここで固定する。
"""

import json
import logging

import pytest

from retro_radio.utils.logging_config import (
    JSONFormatter,
    resolve_level,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    for handler in before:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(before_level)


def test_module_is_importable():
    """構文的に壊れておらず import できること"""
    import retro_radio.utils.logging_config as module

    assert callable(module.setup_logging)


def _json_handlers():
    return [h for h in logging.getLogger().handlers if isinstance(h.formatter, JSONFormatter)]


def test_setup_logging_installs_exactly_one_json_handler():
    """JSON ハンドラーが常に1つだけになる（import 済みでも再呼び出しでも）"""
    setup_logging("DEBUG")
    assert len(_json_handlers()) == 1
    assert logging.getLogger().level == logging.DEBUG


def test_setup_logging_is_idempotent():
    """2回呼んでもJSONハンドラーが重複しない"""
    setup_logging("INFO")
    setup_logging("INFO")
    assert len(_json_handlers()) == 1


def test_setup_logging_preserves_foreign_handlers(caplog):
    """他ライブラリが取り付けたハンドラーは外さない"""
    logging.getLogger().addHandler(logging.NullHandler())
    setup_logging("INFO")
    assert any(isinstance(h, logging.NullHandler) for h in logging.getLogger().handlers)


def test_setup_logging_reads_env_level(monkeypatch):
    monkeypatch.setenv("RETRO_RADIO_LOG_LEVEL", "WARNING")
    setup_logging()
    assert logging.getLogger().level == logging.WARNING


@pytest.mark.parametrize("bad", ["NOT_A_LEVEL", "", "  ", "999"])
def test_resolve_level_falls_back_to_info(bad):
    assert resolve_level(bad) == logging.INFO


def test_resolve_level_is_case_insensitive():
    assert resolve_level("debug") == logging.DEBUG


def test_json_formatter_emits_one_json_line():
    record = logging.LogRecord(
        name="retro_radio.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=42,
        msg="日本語のメッセージ %s",
        args=("引数",),
        exc_info=None,
    )
    payload = json.loads(JSONFormatter().format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "retro_radio.test"
    assert payload["message"] == "日本語のメッセージ 引数"
    assert payload["line"] == 42
    assert "exception" not in payload


def test_json_formatter_includes_exception_text():
    try:
        raise ValueError("原因")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="retro_radio.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="失敗",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(JSONFormatter().format(record))
    assert "ValueError" in payload["exception"]


def test_server_uses_the_shared_logging_setup():
    """server.py は logging.basicConfig ではなく logging_config.setup_logging を使う"""
    import retro_radio.server as server_module
    from retro_radio.utils import logging_config

    assert server_module.setup_logging is logging_config.setup_logging
    assert len(_json_handlers()) == 1
