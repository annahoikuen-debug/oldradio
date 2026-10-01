"""監査ログの API（提案⑧・S4、タスク6）。

## 提供するもの

| エンドポイント | 目的 | 保護 |
|---|---|---|
| `GET /api/admin/audit` | 監査ログの一覧 | **`admin` ロール限定** |
| `GET /api/admin/audit/stats` | 完全率・件数サマリ | **`admin` ロール限定** |
| `POST /api/admin/audit` | 1 イベントを記録（内部用・生成/再生） | `admin` ロール限定 |

## なぜ admin 限定なのか

提案⑧ の要件:
> 「誰がいつ、誰の記念日を生成したか」が追えることが福祉導入の前提条件である。

その「誰が」を**利用者側から見えなくする**のが本エンドポイントの目的。
一般の利用者が自分の活動履歴を監査テーブル経由で覗けると、
テナントをまたいだ集計ができてしまい、テナント分離が壊れる。

## 完全率（= 100%）の定義と検証

> **生成イベント数に対する監査ログ行数 = 100%**

この比を計算する式を [`audit_coverage`] で公開し、
`tests/test_me_api.py::test_audit_log_completeness_is_100_percent` で固定する。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query, status
from pydantic import BaseModel, Field

from ..auth.tokens import Principal
from ..db.privacy_repository import AuditRepository
from ..db.session import get_db
from .deps import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["audit"])

#: 監査ログのアクション名。`server.py` 側（S5）と共有する。
ACTION_GENERATION = "generation"
ACTION_PLAYBACK = "playback"
ACTION_AUTH = "auth"
ACTION_TENANT_ACCESS = "tenant_access"


class AuditEvent(BaseModel):
    """1 つの監査イベント。"""

    action: str = Field(..., min_length=1, max_length=64)
    user_id: Optional[str] = Field(default=None, max_length=32)
    resource_type: Optional[str] = Field(default=None, max_length=64)
    resource_id: Optional[str] = Field(default=None, max_length=64)
    outcome: str = Field(default="success", pattern="^(success|denied|failure)$")
    #: **個人データ（氏名・生年・原稿本文など）を入れないこと。**
    meta: Dict[str, Any] = Field(default_factory=dict)


class AuditStats(BaseModel):
    """運用サマリ。SLA と完全率を観測する。"""

    tenant_id: str
    total: int
    by_action: Dict[str, int]
    generation_events: int
    playback_events: int
    #: 生成イベント 1 件あたりのログ行数。1.0 なら完全率 100%。
    coverage_ratio: float
    deletion_events: int
    consent_events: int
    #: 未完了の削除請求（`deletion_requested_at` は立つが `deleted_at` は空）。
    pending_deletions: int


def audit_coverage(log_count: int, generation_count: int) -> float:
    """監査ログの完全率を返す（0.0 から 1.0）。

    定義:
    - 生成が 0 件なら **1.0**（対象が無ければ「漏れ」も無い）。
    - 生成が 1 件以上なら ``log_count / generation_count``。
      1 を超える値は「1 イベントで複数行記録している」ことを意味するので、
      指標としては 1.0 に丸めない（**過不足を隠さない**）。

    Parameters
    ----------
    log_count:
        監査ログの行数。
    generation_count:
        実際に発生した生成イベント数。

    Returns
    -------
    float
        完全率（1.0 = 100%）。
    """
    if generation_count <= 0:
        return 1.0
    return float(log_count) / float(generation_count)


@router.get("/audit")
def list_audit(
    action: Optional[str] = Query(None, max_length=64, description="アクションで絞る"),
    target_user_id: Optional[str] = Query(
        None, alias="user_id", max_length=32, description="利用者で絞る"
    ),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_admin),
):
    """監査ログを一覧する。**管理者テナントの範囲だけ**。

    **テナントをまたいだ検索はしない**（`scope=global` は提供しない）。
    施設が複数ある場合、各施設の管理者は自分のテナントしか見られない。
    """
    with get_db() as db:
        entries = AuditRepository(db).list(
            tenant_id=principal.tenant_id,
            action=action,
            user_id=target_user_id,
            limit=limit,
            offset=offset,
        )
    return {
        "tenant_id": principal.tenant_id,
        "count": len(entries),
        "limit": limit,
        "offset": offset,
        "entries": entries,
    }


@router.get("/audit/stats", response_model=AuditStats)
def audit_stats(principal: Principal = Depends(require_admin)):
    """運用サマリ。完全率と未完了の削除請求を返す。

    未完了の削除請求は「削除要求から 24 時間以内」という
    提案⑧ の効果指標を運用で監視するための観測点。
    """
    with get_db() as db:
        repo = AuditRepository(db)
        total = repo.count(tenant_id=principal.tenant_id)
        by_action = {
            name: repo.count(tenant_id=principal.tenant_id, action=name)
            for name in (
                ACTION_GENERATION,
                ACTION_PLAYBACK,
                "consent",
                "deletion",
                "export",
                ACTION_AUTH,
                ACTION_TENANT_ACCESS,
            )
        }
        pending = _pending_deletions(db, principal.tenant_id)

    generation_events = by_action.get(ACTION_GENERATION, 0)
    return AuditStats(
        tenant_id=principal.tenant_id,
        total=total,
        by_action=by_action,
        generation_events=generation_events,
        playback_events=by_action.get(ACTION_PLAYBACK, 0),
        coverage_ratio=audit_coverage(total, generation_events),
        deletion_events=by_action.get("deletion", 0),
        consent_events=by_action.get("consent", 0),
        pending_deletions=pending,
    )


@router.post("/audit", status_code=status.HTTP_201_CREATED)
def record_audit(
    event: AuditEvent = Body(...),
    principal: Principal = Depends(require_admin),
):
    """1 イベントを記録する（生成 / 再生の記録用）。

    `server.py` 側は `require_admin` ではなく `require_tenant` で
    **テナント ID をパスから取り出す**（生成は管理者の操作ではない）。
    そのため、このエンドポイントは `admin` 限定にして**手動記録**に留める。
    アプリケーション内部からの記録は `AuditRepository.record()` を直接使う。
    """
    with get_db() as db:
        record = AuditRepository(db).record(
            tenant_id=principal.tenant_id,
            action=event.action,
            user_id=event.user_id or principal.user_id,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            outcome=event.outcome,
            meta=event.meta,
        )
    return record


# --- 内部ヘルパ -------------------------------------------------------------------
def _pending_deletions(db, tenant_id: str) -> int:
    """削除請求を受けて、まだ `deleted_at` が立っていない人数。

    テナント配下の削除要求のうち完了していないものを数える。
    `user_security` は `tenant_id` を持つので join せずに絞れる。
    """
    from ..db.privacy_models import UserSecurityModel

    return int(
        db.query(UserSecurityModel)
        .filter(
            UserSecurityModel.tenant_id == tenant_id,
            UserSecurityModel.deletion_requested_at.isnot(None),
            UserSecurityModel.deleted_at.is_(None),
        )
        .count()
    )


__all__ = [
    "router",
    "AuditEvent",
    "AuditStats",
    "audit_coverage",
    "ACTION_GENERATION",
    "ACTION_PLAYBACK",
    "ACTION_AUTH",
    "ACTION_TENANT_ACCESS",
]
