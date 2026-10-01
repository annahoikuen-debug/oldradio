"""ユーティリティの公開 API。

`errors` / `validators` / `session` は `retro_radio.config` を import するため、
このパッケージを先に読み込むと循環 import になる（実測: `ImportError: cannot
import name 'ConfigurationError' from partially initialized module
'retro_radio.config'`）。

そこで `errors` / `validators` / `session` は**遅延 import**（`__getattr__`）で公開する。
利用側の `from retro_radio.utils import handle_error` はそのまま動き、
`import retro_radio.utils` だけでは循環しない。
"""

from .async_runner import run_in_executor, shutdown_executor, AsyncProgress, async_step

__all__ = [
    "run_in_executor",
    "shutdown_executor",
    "AsyncProgress",
    "async_step",
    "handle_error",
    "ConfigurationError",
    "GenerationRequest",
    "sanitize_text",
    "init_session_state",
]


def __getattr__(name: str):
    """`retro_radio.utils.errors` / `.validators` / `.session` を遅延公開する。

    PEP 562 のモジュールレベル `__getattr__`。属性が実際に参照された時点で
    import するので、`retro_radio.config` の import 順を壊さない。
    """
    if name == "handle_error":
        from .errors import handle_error

        return handle_error
    if name == "ConfigurationError":
        from ..config import ConfigurationError

        return ConfigurationError
    if name in ("GenerationRequest", "sanitize_text"):
        from . import validators

        return getattr(validators, name)
    if name == "init_session_state":
        from .session import init_session_state

        return init_session_state
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
