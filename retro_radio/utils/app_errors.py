"""`AppError` 階層（循環 import を起こさない**独立**モジュール）。

なぜ分離したか
--------------

`retro_radio/config.py` は `ConfigurationError` を `AppError` の subclass として
定義する必要がある（`server.app_error_handler` が `isinstance` で 500 に振り分けるため）。

一方 `retro_radio/utils/__init__.py` は `retro_radio.utils.errors` を import する。
`config` → `utils.errors` → `config` という経路を作ると、`retro_radio/__init__.py` の
`from .config import get_settings` が間に挟まって **circular import** になる
（実測: `ImportError: cannot import name 'ConfigurationError' from partially
initialized module 'retro_radio.config'`）。

したがって依存方向を 1 つに固定する:

```
    utils.app_errors   ← 誰も import しない（葉）
          ↑
    config.ConfigurationError
          ↑
    utils.errors  （re-export + handle_error 等のロジック）
```

`utils.errors` からは `AppError` / 各サブクラスを再エクスポートする。
既存の `from retro_radio.utils.errors import AppError, ...` はそのまま動く。
"""

from __future__ import annotations


class AppError(Exception):
    """ユーザー向けメッセージを持つ共通エラー。

    `user_message` は利用者に返し、`technical_message` は運用者向けの詳細。
    """

    def __init__(
        self,
        user_message: str,
        technical_message: str = "",
        original: Exception | None = None,
    ):
        self.user_message = user_message
        self.technical_message = technical_message or str(original or "")
        self.original = original
        super().__init__(self.technical_message)


class ScriptGenerationError(AppError):
    """原稿生成の失敗。"""


class MusicSearchError(AppError):
    """音源検索の失敗。"""


class TTSError(AppError):
    """音声合成の失敗。"""


class ValidationError(AppError):
    """入力検証の失敗。"""


__all__ = [
    "AppError",
    "ScriptGenerationError",
    "MusicSearchError",
    "TTSError",
    "ValidationError",
]
