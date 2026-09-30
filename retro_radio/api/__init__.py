# retro_radio/api/__init__.py
# v1_router は未実装の Pro プラン専用 API 用。server.py では include していない
from .v1 import router as v1_router
__all__ = ["v1_router"]
