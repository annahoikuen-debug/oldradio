# retro_radio/api/__init__.py
# privacy / audit ルーター。
#
# `me` / `audit` は **`server.py` で include 済み**。
# Pro プラン API の `v1` スタブは死蔵コードのため削除した
# （コードレビュー D-7）。ルーターを追加する順序は、
# `deps` の依存関数が定義済みの後にすること。
from .me import router as me_router
from .audit import router as audit_router
from . import deps

__all__ = [
    "me_router",
    "audit_router",
    "deps",
    "all_routers",
]

#: S5 がまとめて include するための一覧。
#: 順序は依存関係（me / audit は deps を Depends で参照するだけ）。
all_routers = [me_router, audit_router]
