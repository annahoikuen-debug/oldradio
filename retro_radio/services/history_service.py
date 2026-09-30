"""生成履歴のサービス層（提案⑧・S4、タスク3）。

## なぜ作り直すのか

旧実装（484 B）は `save_generation_result()` だけを持ち、
`server.py` から **一度も呼ばれていない孤児コード**だった。
提案⑧ の要件「`/api/generate` は DB に一切書き込まない」を Satisfaction させるには、

1. 1 回の生成ごとに **1 行だけ**を `generations` に書く（冪等・例外安全）
2. その生成を **テナント ID 付きの監査ログ**に残す
3. 開示（`GET /api/me/export`）と削除（`DELETE /api/me`）从这个 に辿れる

という 3 つが揃っている必要がある。旧実装は 1 つも満たしていない。

## 呼び出し側の約束（S5）

`server.py:_build_generate_response` の**最後**で 1 回だけ呼ぶ:

```python
# 擬似 diff（server.py の編集は S5）
-from .services.history_service import record_generation
+from .services.history_service import record_generation

 @app.post("/api/generate", response_model=GenerateResponse)
-def generate_radio(req: GenerateRequest):
+def generate_radio(req: GenerateRequest, principal: Principal = Depends(require_tenant)):
     response = _build_generate_response(req)
+    record_generation(
+        user_id=principal.user_id,
+        tenant_id=principal.tenant_id,
+        year=req.year, month=req.month, day=req.day,
+        script=response.script,
+        song_title=..., artist_name=...,
+        audio_path=response.audio_url,
+    )
     return response
```

**失敗しても応答は壊さない**（`record_generation` は例外を握り潰す）。
履歴の書き込み失敗で利用者の节目生成が 500 になる Worse な UX になる。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..db.privacy_repository import AuditRepository
from ..db.repository import GenerationRepository
from ..db.session import get_db_sync

logger = logging.getLogger(__name__)

#: 監査ログのアクション名（`api/audit.py` と共有）。
ACTION_GENERATION = "generation"

#: 1 回の生成で書き込む `generations` 行に必要な項目。
REQUIRED_FIELDS = ("year", "month", "day", "script", "song_title", "artist_name")


def save_generation_result(entry: dict, user_id: str) -> Optional[str]:
    """生成履歴を保存する。戻り値は生成 ID（失敗時は `None`）。

    **旧シグネチャを維持している**（`tests/test_history_service.py` が使う）。
    旧実装と同じく、リポジトリは flush のみなので自分で commit する。

    Parameters
    ----------
    entry:
        `year` / `month` / `day` / `script` / `song_title` / `artist_name` を含む dict。
    user_id:
        所有者のユーザー ID。
    """
    if not user_id:
        raise ValueError("user_id は必須です（生成を利用者に紐付けないまま保存しない）")

    db = get_db_sync()
    try:
        model = GenerationRepository(db).create(user_id, entry)
        db.commit()
        return model.id
    except Exception:
        db.rollback()
        raise
    finally:
        # close が失敗しても、本命の例外を握り潰さない。
        # finally 内で投げると元の例外が上書きされ、原因特定できなくなる。
        try:
            db.close()
        except Exception as e:  # pragma: no cover - 防御のみ
            logger.warning(f"DB接続のクローズに失敗しました: {e}")


def record_generation(
    user_id: Optional[str],
    tenant_id: str,
    year: int,
    month: int,
    day: int,
    script: str,
    song_title: str,
    artist_name: str,
    preview_url: Optional[str] = None,
    audio_path: Optional[str] = None,
    mode: Optional[str] = None,
    all_songs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """1 回の生成を**履歴 + 監査ログ**に記録する（`server.py` の接着点）。

    Returns
    -------
    dict
        ``{"recorded": bool, "generation_id": str | None}``。
        **例外は投げない**。履歴の失敗が配信を妨げないことが優先。
    """
    entry = {
        "year": year,
        "month": month,
        "day": day,
        "script": script,
        "song_title": song_title,
        "artist_name": artist_name,
        "preview_url": preview_url,
        "audio_path": audio_path,
        "all_songs": all_songs or [],
    }
    missing = [f for f in REQUIRED_FIELDS if entry.get(f) in (None, "")]
    if missing:
        logger.warning("生成履歴の保存をスキップします（必須項目が欠けています）: %s", missing)
        return {"recorded": False, "generation_id": None}

    if not user_id:
        # 個人モード（`require_auth=0`）では利用者が特定できない。
        # 履歴に**紐付けifetimeの無い行**を作ると、後から「誰のものか不明」で
        # 削除請求を追えなくなる。そのため記録しない。
        logger.info("利用者が特定できないため生成履歴を記録しません（個人モード）")
        return {"recorded": False, "generation_id": None}

    generation_id: Optional[str] = None
    try:
        generation_id = save_generation_result(entry, user_id)
    except Exception:
        logger.exception("生成履歴の保存に失敗しました: year=%s", year)
        return {"recorded": False, "generation_id": None}

    # 監査ログは同じトランザクション境界内で別セッションを使う
    # （generation は既に commit 済み。失敗しても履歴は残る）。
    try:
        db = get_db_sync()
        try:
            AuditRepository(db).record(
                tenant_id=tenant_id,
                action=ACTION_GENERATION,
                user_id=user_id,
                resource_type="generation",
                resource_id=generation_id,
                outcome="success",
                # **個人データを入れない**（原稿本文も入れない）。
                meta={"year": year, "mode": mode} if mode else {"year": year},
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        # 監査ログの失敗は**黙らない**（完全率が崩れるため）。
        logger.exception("生成の監査ログ記録に失敗しました: generation_id=%s", generation_id)

    return {"recorded": True, "generation_id": generation_id}


def record_playback(
    user_id: Optional[str],
    tenant_id: str,
    generation_id: Optional[str] = None,
    audio_filename: Optional[str] = None,
) -> bool:
    """再生イベントを監査ログに記録する。

    「いつ誰のラジオが再生されたか」を追えるようにするため。
    **音声ファイル名そのものは記録しない**（ファイル名は内容から推測可能で、
    監査ログを長期保存すると「内容の一部」が保存 Triumphすることになるため）。
    """
    try:
        db = get_db_sync()
        try:
            AuditRepository(db).record(
                tenant_id=tenant_id,
                action="playback",
                user_id=user_id,
                resource_type="generation" if generation_id else "audio",
                resource_id=generation_id,
                outcome="success",
                meta={"has_audio": bool(audio_filename)},
            )
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        logger.exception("再生の監査ログ記録に失敗しました")
        return False


def list_for_user(user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """ある利用者の生成履歴を新しい順に返す（開示用）。"""
    db = get_db_sync()
    try:
        return GenerationRepository(db).get_by_user(user_id, limit=limit)
    finally:
        db.close()


def delete_for_user(user_id: str) -> int:
    """ある利用者の生成履歴を全削除する（削除請求の実行の一部）。

    監査ログは**消さない**（処理の証明のため）。
    戻り値は削除前の行数。
    """
    from ..db.models import GenerationModel

    db = get_db_sync()
    try:
        before = int(
            db.query(GenerationModel).filter(GenerationModel.user_id == user_id).count()
        )
        GenerationRepository(db).delete_old(user_id, keep=0)
        db.commit()
        return before
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


__all__ = [
    "save_generation_result",
    "record_generation",
    "record_playback",
    "list_for_user",
    "delete_for_user",
    "ACTION_GENERATION",
    "REQUIRED_FIELDS",
]
