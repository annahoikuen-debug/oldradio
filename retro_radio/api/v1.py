# retro_radio/api/v1.py
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Depends, Header

logger = logging.getLogger(__name__)

# 注意: この router は現在 server.py に include されていない（未実装の Pro プラン専用 API のスタブ）
router = APIRouter(prefix="/api/v1")

def get_current_user(authorization: Optional[str] = Header(default=None)):
    """Authorization ヘッダー（Bearer トークン）からユーザーを解決する

    トークン検証は auth/ 側の責務。auth/ に検証元が無い場合は未認証（None）を返し、
    Pro プランのゲートが拒否する。
    """
    from ..auth import Authenticator

    if not authorization or not authorization.lower().startswith("bearer "):
        return None

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None

    verifier = getattr(Authenticator, "verify_api_token", None)
    if verifier is None:
        logger.warning("APIトークン検証が auth/ に未実装のため未認証として扱います")
        return None
    return verifier(token)

def _is_pro_plan(user: Any) -> bool:
    if not user:
        return False
    plan = user.get("plan") if isinstance(user, dict) else getattr(user, "plan", None)
    return str(getattr(plan, "value", plan)).lower() == "pro"

@router.get("/generate")
async def generate_radio(
    year: int,
    current_user: Any = Depends(get_current_user)
):
    # プランチェック
    if not _is_pro_plan(current_user):
        raise HTTPException(status_code=403, detail="API access requires PRO plan")

    # 実際の生成処理（バックグラウンドタスク等）
    # ここではスタブ
    return {"status": "accepted", "message": "Generation started"}
