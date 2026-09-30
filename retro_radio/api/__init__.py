# retro_radio/api/__init__.py
# S4（提案⑧）で追加した privacy / audit ルータと、既存の Pro プラン API。
#
# **重要**: これらの router は **`server.py` では include されていない**。
# `server.py` は S5 の管轄であり、S4 はファイルを触らない。
# S5 は下の `all_routers` を `app.include_router()` するだけでよい。
#
# なぜ include しないまま残すのか:
# `/api/generate` と `/api/audio/*` を認証付きにするには、
# 先に S5 が `require_tenant()` をianus.Option へ差し込む必要がある。
# include だけ先に行-publishedと、**認証の無い endpoints が公開される**。
# そのため「依存関数（S4）と include（S5）」の順序を意図的に分ける。
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
