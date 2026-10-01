# retro_radio/api/__init__.py
# privacy / audit ルータと、Pro プラン API（`v1`）。
#
# `me` / `audit` は **`server.py` で include 済み**。`v1` は
# `tests/test_api_access_control.py` が「未 include であること」を固定して
# いるため意図的に外してある（Pro プランは導入しない判断）。
# ルータを追加する顺序は、`deps` の依存関数が定義済みの後にすること。
from .v1 import router as v1_router
from .me import router as me_router
from .audit import router as audit_router
from . import deps

__all__ = [
    "v1_router",
    "me_router",
    "audit_router",
    "deps",
    "all_routers",
]

#: S5 がまとめて include するための一覧。
#: 順序は依存関係（me / audit は deps を Depends で参照するだけ）。
all_routers = [me_router, audit_router, v1_router]
