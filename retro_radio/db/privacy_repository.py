"""施設運用 readiness のリポジトリ群（提案⑧・S4）。

## 責務

- ここまでの `retro_radio.db.repository` と同じく **repository は `flush()` まで**。
  `commit` は `retro_radio.db.session.get_db()` の contextmanager が行う。
- 既存の `UserRepository` / `GenerationRepository` / `FavoriteRepository`
  とは **別の MetaData** のテーブルを扱うが、engine は同じなので
  同一の `get_db()` で扱える（同一トランザクション境界）。

## 特に注意している契約

`retro_radio.core.music_profile.MusicProfileRepository`（S3 の Protocol）の
2 つの契約を満たす:

1. `get()` は **空プロファイルを返す**（`None` を返さない）。
2. `record_rejection()` は **単一 UPDATE** で原子的に減算する。
   読み取り → 書き込みの 2 手順にしない。
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from .privacy_models import (
    ALL_ROLES,
    DEFAULT_FAMILIARITY,
    DEFAULT_REACTION,
    DEFAULT_TENANT_ID,
    MAX_FAMILIARITY,
    MIN_FAMILIARITY,
    ROLE_ADMIN,
    ROLE_MEMBER,
    AuditLogModel,
    ConsentModel,
    FavoriteTrackModel,
    MusicProfileModel,
    TenantModel,
    UserSecurityModel,
    utcnow,
)

logger = logging.getLogger(__name__)


class _Unset:
    """「引数が省略された」ことを表す番兵。`None` とは別の値として扱う。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - デバッグ表示のみ
        return "<unset>"


#: `save_profile_scalars` で「変化させない」を表す番兵。
_UNSET = _Unset()


if TYPE_CHECKING:  # pragma: no cover - 型検査時のみ（実行時は import しない）
    from ..core.music_profile import FavoriteTrack, MusicProfile


#: `audit_logs.resource_id` の列幅（`AuditLogModel` と対）。超えたら切り詰めずエラー。
_MAX_RESOURCE_ID_CHARS = 64


def _core_types():
    """`core.music_profile` の型を**遅延 import** で取り出す。

    ## なぜ遅延 import なのか
    `retro_radio.core.__init__` は `pipeline` -> `music_search` ->
    `preview_resolver` を輸入するため、`db` 層から `core` を **import 時に**
    触ると、コンテンツパイプライン全体の重量級 import に引きずられる。
    結果として:

    - `core/` のどれか 1 ファイルに構文エラーがあるだけで
      **DB リポジトリが使えなくなる**（`history_service` まで落ちる）。
    - リポジトリの単体テストが外部ネットワーク依存の import に巻き込まれる。

    `db` 層は `core` の**実装詳細**に依存せず、
    `MusicProfile` / `FavoriteTrack` の**契約**にだけ依存する。
    そのため import は実際に必要になった瞬間（`get` / `save` called）に留める。
    """
    from ..core.music_profile import FavoriteTrack, MusicProfile, Reaction

    return FavoriteTrack, MusicProfile, Reaction


def _loads_int_list(raw: Optional[str]) -> Tuple[int, ...]:
    """壊れた JSON でも全体訪問が失敗しないよう、decode 失敗は空にフォールバックする。"""
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    out: List[int] = []
    for item in parsed:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def _dumps(value: Sequence[int]) -> str:
    return json.dumps([int(v) for v in value])


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """DB（naive UTC）をドメイン側（aware UTC）へ寄せる。

    `FavoriteTrack.last_played_at` は「timezone 付き aware」を要求する。
    DB は naive UTC で返すため、ここに tzinfo を付けないと
    `core.music_profile._elapsed_years` の aware/naive 混在比較で例外になる。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _to_utc(value: Optional[datetime]) -> Optional[datetime]:
    """aware datetime を DB 存入の naive UTC へ落とす。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class TenantRepository:
    """テナント（= デプロイ単位 / 施設単位）の読み書き。"""

    def __init__(self, db: Session):
        self.db = db

    def get(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        model = self.db.get(TenantModel, tenant_id)
        if not isinstance(model, TenantModel):
            return None
        return {
            "id": model.id,
            "name": model.name,
            "is_active": bool(model.is_active),
            "created_at": model.created_at,
        }

    def ensure(self, tenant_id: str, name: Optional[str] = None) -> Dict[str, Any]:
        """テナントが無ければ作る。**冪等**。"""
        existing = self.get(tenant_id)
        if existing is not None:
            return existing
        model = TenantModel(
            id=tenant_id,
            name=name or tenant_id,
            is_active=True,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self.db.add(model)
        self.db.flush()
        return {
            "id": model.id,
            "name": model.name,
            "is_active": True,
            "created_at": model.created_at,
        }

    def list_active(self) -> List[Dict[str, Any]]:
        rows = (
            self.db.query(TenantModel)
            .filter(TenantModel.is_active.is_(True))
            .order_by(TenantModel.id)
            .all()
        )
        return [
            {"id": r.id, "name": r.name, "is_active": True, "created_at": r.created_at}
            for r in rows
        ]


class UserSecurityRepository:
    """既存 `users` 行に付随するロール / テナント / 論理削除フラグの読み書き。"""

    def __init__(self, db: Session):
        self.db = db

    # --- 読み取り ---------------------------------------------------------------
    def get(self, user_id: str) -> Optional[Dict[str, Any]]:
        """セキュリティ行を 1 件読む。無ければ `None`。

        **型を保証する理由**: 呼び出し側（`is_deleted` など）は
        「行が有ったら True」を OR で束ねるため、想定外の値が返ると
        事実とは無関係に「削除済み」と判定してしまう。
        セッションが想定外の値を返した場合（テストのモックを含む）は
        **「行が無い」= 生存** として扱う（fail-open ではなく
        「判断材料が無い = 削除の根拠も無い」）。
        """
        model = self.db.get(UserSecurityModel, user_id)
        if not isinstance(model, UserSecurityModel):
            return None
        return {
            "user_id": model.user_id,
            "role": model.role,
            "tenant_id": model.tenant_id or DEFAULT_TENANT_ID,
            "deletion_requested_at": model.deletion_requested_at,
            "deleted_at": model.deleted_at,
        }

    def resolve(self, user_id: str) -> Dict[str, Any]:
        """セキュリティ行が無い利用者にも既定値を返す（`None` を返さない）。

        既定は「ロール未設定の一般利用者」= `ROLE_MEMBER` かつ
        暗黙の `DEFAULT_TENANT_ID` テナント。
        ここで `None` を返すと呼び出し側が毎回 None 判定を書かないといけなくなる。
        """
        found = self.get(user_id)
        if found is not None:
            return found
        return {
            "user_id": user_id,
            "role": ROLE_MEMBER,
            "tenant_id": DEFAULT_TENANT_ID,
            "deletion_requested_at": None,
            "deleted_at": None,
        }

    def is_admin(self, user_id: str) -> bool:
        """DB 側のロールが admin か。"""
        found = self.get(user_id)
        return bool(found and found["role"] == ROLE_ADMIN)

    def is_deleted(self, user_id: str) -> bool:
        """論理削除済みか。**判定材料が無い場合は False**。

        `deleted_at is not None` で判定する（truthiness ではなく）。
        """
        found = self.get(user_id)
        if not found:
            return False
        return found["deleted_at"] is not None

    @staticmethod
    def is_bootstrap_admin_email(email: str, admin_emails: Sequence[str] = ()) -> bool:
        """`RETRO_RADIO_ADMIN_EMAILS` に含まれるか（**bootstrap 用**）。

        恒久的な管理権限は DB 側 `user_security.role` が正。
        こちらは「初回デプロイ時に手動で当てる」ための補助であり、
        運用で管理者が交代したときは DB 側を更新してリストから外すこと。
        """
        if not email:
            return False
        normalized = str(email).strip().lower()
        return normalized in {str(e).strip().lower() for e in admin_emails}

    # --- 書き込み ---------------------------------------------------------------
    def ensure(
        self,
        user_id: str,
        tenant_id: Optional[str] = None,
        role: str = ROLE_MEMBER,
    ) -> Dict[str, Any]:
        """セキュリティ行が無ければ作る。**冪等**。"""
        found = self.get(user_id)
        if found is not None:
            return found
        model = UserSecurityModel(
            user_id=user_id,
            role=role if role in ALL_ROLES else ROLE_MEMBER,
            tenant_id=tenant_id or DEFAULT_TENANT_ID,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self.db.add(model)
        self.db.flush()
        return self.resolve(user_id)

    def _require(self, user_id: str) -> UserSecurityModel:
        """行が無ければ作り、**必ず** `UserSecurityModel` を返す。"""
        self.ensure(user_id)
        model = self.db.get(UserSecurityModel, user_id)
        if not isinstance(model, UserSecurityModel):  # pragma: no cover
            self.ensure(user_id)
            model = self.db.get(UserSecurityModel, user_id)
        if not isinstance(model, UserSecurityModel):  # pragma: no cover
            raise RuntimeError("user_security 行を作成できませんでした")
        return model

    def set_role(self, user_id: str, role: str) -> Dict[str, Any]:
        """ロールを設定する。未知のロールは `ValueError`。"""
        if role not in ALL_ROLES:
            raise ValueError(f"未知のロールです: {role!r}（{ALL_ROLES} のいずれか）")
        model = self._require(user_id)
        model.role = role
        model.updated_at = utcnow()
        self.db.flush()
        return self.resolve(user_id)

    def set_tenant(self, user_id: str, tenant_id: str) -> Dict[str, Any]:
        """所属テナントを設定する。"""
        model = self._require(user_id)
        model.tenant_id = tenant_id or DEFAULT_TENANT_ID
        model.updated_at = utcnow()
        self.db.flush()
        return self.resolve(user_id)

    def mark_deletion_requested(self, user_id: str) -> Dict[str, Any]:
        """削除請求の受付日時を記録する（purge 前の受付印）。"""
        model = self._require(user_id)
        model.deletion_requested_at = utcnow()
        model.updated_at = utcnow()
        self.db.flush()
        return self.resolve(user_id)

    def mark_deleted(self, user_id: str) -> Dict[str, Any]:
        """論理削除の完了印洩れる。"""
        model = self._require(user_id)
        now = utcnow()
        if model.deletion_requested_at is None:
            model.deletion_requested_at = now
        model.deleted_at = now
        model.updated_at = now
        self.db.flush()
        return self.resolve(user_id)


class MusicProfileRepositoryImpl:
    """`retro_radio.core.music_profile.MusicProfileRepository` の実装（S3 の契約に一致）。

    ### 契約 1: `get()` は空プロファイルを返す
    プロファイル行が無い利用者に対しても `MusicProfile(owner_id=...)` を返す。
    `None` を返すと呼び出し側の `profile.is_empty` 判定が成立せず、
    「年代パレットへフォールバック」の分岐が壊れる。

    ### 契約 2: `record_rejection()` は原子的に減算する
    **読み取り → 書き込みの 2 手順にしない。** DB 側の 1 UPDATE
    （`SET familiarity_score = familiarity_score - 1 WHERE ... AND familiarity_score > 1`）
    で完結させるため、同時リクエストで減算が失われない。
    """

    def __init__(self, db: Session):
        self.db = db

    # --- MusicProfileRepository 契約 --------------------------------------------
    def get(self, owner_id: str) -> "MusicProfile":
        """``owner_id`` のプロファイル（**無ければ空プロファイル**）を返す。

        ## 「空プロファイル」の正しい意味
        S3 の契約は「`None` を返すな」= **呼び出し側が None 判定を書かなくて済む**、
        という意味である。したがって:

        - **プロファイル行が無い**場合: `teenage_decades=()` `group_id=None` の
          `MusicProfile(owner_id=...)` を返す（`is_empty` が True になる）。
        - **favorite_tracks は常に DB から読む**。
          `favorite_tracks` の行だけ，已有して `music_profiles` の行が
          壊れている（orphan）状態で「曲.Compose が消えた」ように見えると
          選曲スコアリングが黙って壊れるため。
        """
        _FavoriteTrack, MusicProfile, _Reaction = _core_types()
        model = self._find_profile(owner_id)
        rows = (
            self.db.query(FavoriteTrackModel)
            .filter(FavoriteTrackModel.owner_id == owner_id)
            .order_by(FavoriteTrackModel.id)
            .all()
            if owner_id
            else []
        )
        tracks = tuple(self._to_track(t) for t in rows)
        if model is None:
            return MusicProfile(owner_id=owner_id, favorite_tracks=tracks)
        return MusicProfile(
            owner_id=model.owner_id,
            favorite_tracks=tracks,
            teenage_decades=_loads_int_list(model.teenage_decades),
            group_id=model.group_id,
        )

    def save(self, profile: "MusicProfile") -> None:
        """プロファイル全体を保存する（**favorite は MERGE = 差分更新**）。

        ## なぜ既定が merge か
        かつては「`save()` は `profile.favorite_tracks` を**完全な一覧**として扱い、
        そこに無い行を `DELETE` していた（全置換）。`set_group()` / `set_teenage_decades()`
        は `MusicProfile` 的一部分だけを組み立てて `save()` を呼ぶため、
        この全置換により**利用者の favorite が全件消え**、`teenage_decades` も
        空になっていた（成功レスポンスを返したまま破壊される、最悪のデータ損失）。
        よって既定を **差分更新**（`(owner_id, title, artist)` をキーに
        「有るものを更新 / 無ければ追加」だけを行う）に変更した。
        `uq_favorite_tracks_owner_title_artist` がその自然キーの担保である。

        全置換が**本当に**必要な呼び出し元だけが `replace_tracks=True` を明示する。
        全置換が**本当に**必要な呼び出し元は `replace_tracks()`（本リポジトリ内では
        呼び出し元なし）。`save()` の呼び出し元は `set_teenage_decades` / `set_group`
        とテストのみで、全置換に依存していた呼び出し元はない。
        Protocol（`core.music_profile.MusicProfileRepository`）のシグネチャに
        合わせるため、`save(profile)` の形は変えていない。


        `teenage_decades` は `profile` に載っている値をそのまま書く
        （呼出し側が意図して決めているため）。`group_id` は `None` が
        「unset」ではなく「共有解除」を意味しうるので、明示されたときだけ更新する。
        """
        model = self._find_profile(profile.owner_id)
        if model is None:
            model = MusicProfileModel(
                id=secrets.token_urlsafe(16),
                owner_id=profile.owner_id,
                group_id=profile.group_id,
                teenage_decades=_dumps(profile.teenage_decades),
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            self.db.add(model)
            self.db.flush()
        else:
            model.teenage_decades = _dumps(profile.teenage_decades)
            if profile.group_id is not None:
                model.group_id = profile.group_id
            model.updated_at = utcnow()
            self.db.flush()

        existing = {
            (t.title, t.artist): t
            for t in self.db.query(FavoriteTrackModel)
            .filter(FavoriteTrackModel.owner_id == profile.owner_id)
            .all()
        }
        wanted = {(t.title, t.artist): t for t in profile.favorite_tracks}
        for key, track in wanted.items():
            current = existing.get(key)
            if current is None:
                self.db.add(self._new_track(profile.owner_id, track))
            else:
                current.familiarity_score = int(track.familiarity_score)
                current.reaction = track.reaction
                current.last_played_at = _to_utc(track.last_played_at)
                current.updated_at = utcnow()
        # merge: existing に無い曲だけを足す。既存の favorite は消さない。
        self.db.flush()

    def replace_tracks(self, profile: "MusicProfile") -> None:
        """`save()` と同じ保存だが、favorite を**完全な一覧として全置換**する。

        「この profile の favorite_tracks が全件である」ことが呼び出し側で
        確定している場合だけ使う。`save()` は差分更新なので、呼び出し側が
        一覧を一部だけ持っていても既存の favorite を消さない。
        """
        self.save(profile)
        existing = {
            (t.title, t.artist): t
            for t in self.db.query(FavoriteTrackModel)
            .filter(FavoriteTrackModel.owner_id == profile.owner_id)
            .all()
        }
        wanted = {(t.title, t.artist) for t in profile.favorite_tracks}
        for key, current in existing.items():
            if key not in wanted:
                self.db.delete(current)
        self.db.flush()

    def save_profile_scalars(
        self,
        owner_id: str,
        teenage_decades: Optional[Sequence[int]] = None,
        group_id: Any = _UNSET,
    ) -> Dict[str, Any]:
        """`teenage_decades` / `group_id` だけを差し替える（favorite は触らない）。

        `set_teenage_decades` / `set_group` の実装本体。プロファイルを
        `get()` -> `save()` の経路をやめることで、
        「片方だけ更新したいのに他方のデータが消える」事故を**構造的に**なくす。
        `group_id` は省略（`_UNSET`）すれば現状維持、明示的な `None` なら共有解除。
        """
        model = self._ensure_profile(owner_id)
        if teenage_decades is not None:
            model.teenage_decades = _dumps(tuple(int(d) for d in teenage_decades))
        if group_id is not _UNSET:
            model.group_id = group_id
        model.updated_at = utcnow()
        self.db.flush()
        return {
            "owner_id": owner_id,
            "group_id": model.group_id,
            "teenage_decades": list(_loads_int_list(model.teenage_decades)),
        }

    def record_play(
        self,
        owner_id: str,
        title: str,
        artist: str,
        played_at: Optional[datetime] = None,
    ) -> None:
        """再生を履歴に記録する（最終再生時刻の更新）。

        「最終再生時刻」だけが更新対象。familiarity は**変えない**
        （親和性チェックの結果だけを `record_rejection` / `save` が書き換える）。
        """
        stamp = _to_utc(played_at) or utcnow()
        self._ensure_profile(owner_id)
        model = self._find_track(owner_id, title, artist)
        if model is None:
            self.db.add(
                FavoriteTrackModel(
                    id=secrets.token_urlsafe(16),
                    owner_id=owner_id,
                    title=title,
                    artist=artist or "",
                    familiarity_score=DEFAULT_FAMILIARITY,
                    last_played_at=stamp,
                    reaction=DEFAULT_REACTION,
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
        else:
            model.last_played_at = stamp
            model.updated_at = utcnow()
        self.db.flush()

    def record_rejection(self, owner_id: str, title: str, artist: str = "") -> None:
        """「いいえ」を記録する（familiarity を 1 減算）。**原子的に**行う。

        実装は **単一の UPDATE**。読み取り → 書き込みの 2 手順だと
        同一利用者への同時リクエストで減算が失われるため避ける。
        `familiarity_score > 1` を WHERE に含めることで下限も同時に守る
        （0 にならない）。

        対象行が無い場合、**行は作らない**。「利用者がまだ favorite として
        登録していない曲」に familiarity を与えると、
        「その人が聴いた記憶がある」という事実でないデータになり、
        選曲スコアリングが意味を失うため。
        """
        self.db.execute(
            update(FavoriteTrackModel)
            .where(
                FavoriteTrackModel.owner_id == owner_id,
                FavoriteTrackModel.title == title,
                FavoriteTrackModel.artist == (artist or ""),
                FavoriteTrackModel.familiarity_score > MIN_FAMILIARITY,
            )
            .values(
                familiarity_score=FavoriteTrackModel.familiarity_score - 1,
                updated_at=utcnow(),
            )
        )
        self.db.flush()

    # --- 補助（施設運用・API 層から使う） ---------------------------------------
    def upsert_track(
        self,
        owner_id: str,
        title: str,
        artist: str = "",
        familiarity_score: int = DEFAULT_FAMILIARITY,
        reaction: str = DEFAULT_REACTION,
    ) -> Dict[str, Any]:
        """利用者が「好きな曲」として登録する（UI 側の操作に対応）。"""
        if not MIN_FAMILIARITY <= int(familiarity_score) <= MAX_FAMILIARITY:
            raise ValueError(f"familiarity_score は {MIN_FAMILIARITY}〜{MAX_FAMILIARITY} です")
        _FavoriteTrack, _MusicProfile, Reaction = _core_types()
        if reaction not in Reaction.ALL:
            raise ValueError(f"reaction は {Reaction.ALL} のいずれかです")
        # 選曲スコアリングは `get()` の favorite_tracks を見るため、
        # トラックだけBalancer ある状態で「消えた」ように見せないよう
        # プロファイル行を必ず用意する。
        self._ensure_profile(owner_id)
        model = self._find_track(owner_id, title, artist)
        if model is None:
            model = FavoriteTrackModel(
                id=secrets.token_urlsafe(16),
                owner_id=owner_id,
                title=title,
                artist=artist or "",
                familiarity_score=int(familiarity_score),
                last_played_at=None,
                reaction=reaction,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            self.db.add(model)
        else:
            model.familiarity_score = int(familiarity_score)
            model.reaction = reaction
            model.updated_at = utcnow()
        self.db.flush()
        return self._track_dict(model)

    def remove_track(self, owner_id: str, title: str, artist: str = "") -> bool:
        model = self._find_track(owner_id, title, artist)
        if model is None:
            return False
        self.db.delete(model)
        self.db.flush()
        return True

    def set_teenage_decades(self, owner_id: str, decades: Sequence[int]) -> Dict[str, Any]:
        """利用者が 10〜20 代だった年代を設定する。

        **favorite と `group_id` は保持する**（部分更新）。
        """
        _FavoriteTrack, _MusicProfile, _Reaction = _core_types()
        values = tuple(int(d) for d in decades)
        self.save_profile_scalars(owner_id, teenage_decades=values)
        return {"owner_id": owner_id, "teenage_decades": list(values)}

    def set_group(self, owner_id: str, group_id: Optional[str]) -> Dict[str, Any]:
        """施設グループ共有の切り替え。

        **favorite と `teenage_decades` は保持する**（部分更新）。
        `group_id=None` は「共有解除」として明示的に書き込む。
        """
        self.save_profile_scalars(owner_id, group_id=group_id)
        return {"owner_id": owner_id, "group_id": group_id}

    def list_tracks(self, owner_id: str) -> List[Dict[str, Any]]:
        """開示（エクスポート）用。这个利用者の favorite 全件。"""
        rows = (
            self.db.query(FavoriteTrackModel)
            .filter(FavoriteTrackModel.owner_id == owner_id)
            .order_by(FavoriteTrackModel.title, FavoriteTrackModel.artist)
            .all()
        )
        return [self._track_dict(r) for r in rows]

    def delete_owner(self, owner_id: str) -> int:
        """論理削除時に呼ぶ：这个利用者の個人データ行を消す。戻り値は削除行数。

        `favorite_tracks` は `music_profiles` 行への FK を持たない
        （`owner_id` でのみ紐づく）。そのため profile を消しても favorite は
        orphan にならない — が、残すと**削除済み利用者の選好が読み出せる**ため
        このメソッドでは両方消す。行を消しても `get()` は空プロファイルを返すので
        読み取り側の破綻はない。
        """
        tracks = (
            self.db.query(FavoriteTrackModel)
            .filter(FavoriteTrackModel.owner_id == owner_id)
            .delete(synchronize_session=False)
        )
        profiles = (
            self.db.query(MusicProfileModel)
            .filter(MusicProfileModel.owner_id == owner_id)
            .delete(synchronize_session=False)
        )
        consents = (
            self.db.query(ConsentModel)
            .filter(ConsentModel.user_id == owner_id)
            .delete(synchronize_session=False)
        )
        self.db.flush()
        return int(tracks) + int(profiles) + int(consents)

    # --- 内部 -------------------------------------------------------------------
    def _ensure_profile(self, owner_id: str) -> MusicProfileModel:
        """プロファイル行が無ければ作る（冪等）。

        `get()` は「行が無ければ空プロファイル」を返す契約だが、
        **空だと favorite_tracks が見えない**。
        選曲は `profile.is_empty` によって「年代パレットへフォールバック」する
        ため、曲だけBalancer ある状態で空に見えると既存挙動に落ち、
        利用者の選好が使われない（= source monitoring error）。
        """
        existing = self._find_profile(owner_id)
        if existing is not None:
            return existing
        model = MusicProfileModel(
            id=secrets.token_urlsafe(16),
            owner_id=owner_id,
            group_id=None,
            teenage_decades="[]",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self.db.add(model)
        self.db.flush()
        return model

    def _find_profile(self, owner_id: str) -> Optional[MusicProfileModel]:
        if not owner_id:
            return None
        model = (
            self.db.query(MusicProfileModel)
            .filter(MusicProfileModel.owner_id == owner_id)
            .first()
        )
        # 想定外の型が返ったら「行が無い」とみなす。呼び出し側が
        # 必ず文字列を受け取れるようにするため（`get()` の空契約の前提）。
        return model if isinstance(model, MusicProfileModel) else None

    def _find_track(
        self, owner_id: str, title: str, artist: str = ""
    ) -> Optional[FavoriteTrackModel]:
        if not owner_id or not title:
            return None
        model = (
            self.db.query(FavoriteTrackModel)
            .filter(
                FavoriteTrackModel.owner_id == owner_id,
                FavoriteTrackModel.title == title,
                FavoriteTrackModel.artist == (artist or ""),
            )
            .first()
        )
        return model if isinstance(model, FavoriteTrackModel) else None

    def _new_track(self, owner_id: str, track: "FavoriteTrack") -> FavoriteTrackModel:
        return FavoriteTrackModel(
            id=secrets.token_urlsafe(16),
            owner_id=owner_id,
            title=track.title,
            artist=track.artist or "",
            familiarity_score=int(track.familiarity_score),
            last_played_at=_to_utc(track.last_played_at),
            reaction=track.reaction,
            created_at=utcnow(),
            updated_at=utcnow(),
        )

    @staticmethod
    def _to_track(model: FavoriteTrackModel) -> "FavoriteTrack":
        _FavoriteTrack, _MusicProfile, _Reaction = _core_types()
        return _FavoriteTrack(
            title=model.title,
            artist=model.artist or "",
            familiarity_score=int(model.familiarity_score),
            last_played_at=_aware(model.last_played_at),
            reaction=model.reaction or DEFAULT_REACTION,
        )

    @staticmethod
    def _track_dict(model: FavoriteTrackModel) -> Dict[str, Any]:
        return {
            "id": model.id,
            "owner_id": model.owner_id,
            "title": model.title,
            "artist": model.artist or "",
            "familiarity_score": int(model.familiarity_score),
            "last_played_at": model.last_played_at.isoformat() if model.last_played_at else None,
            "reaction": model.reaction,
        }


class ConsentRepository:
    """同意記録の読み書き。"""

    def __init__(self, db: Session):
        self.db = db

    def record(
        self,
        user_id: str,
        terms_version: str,
        accepted: bool,
        tenant_id: str = DEFAULT_TENANT_ID,
        accepted_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        model = ConsentModel(
            id=secrets.token_urlsafe(16),
            user_id=user_id,
            tenant_id=tenant_id or DEFAULT_TENANT_ID,
            terms_version=terms_version,
            accepted=bool(accepted),
            accepted_at=accepted_at or utcnow(),
            withdrawn_at=None,
            created_at=utcnow(),
        )
        self.db.add(model)
        self.db.flush()
        return self._dict(model)

    def withdraw(
        self,
        user_id: str,
        terms_version: str,
        withdrawn_at: Optional[datetime] = None,
    ) -> int:
        """同意の撤回。該当版の**未撤回**行をすべて撤回済みにする。"""
        stamp = withdrawn_at or utcnow()
        updated = (
            self.db.query(ConsentModel)
            .filter(
                ConsentModel.user_id == user_id,
                ConsentModel.terms_version == terms_version,
                ConsentModel.withdrawn_at.is_(None),
            )
            .update({ConsentModel.withdrawn_at: stamp}, synchronize_session=False)
        )
        self.db.flush()
        return int(updated)

    def latest_for_version(self, user_id: str, terms_version: str) -> Optional[Dict[str, Any]]:
        """指定版の最新の同意記録（無ければ `None`）。"""
        model = (
            self.db.query(ConsentModel)
            .filter(
                ConsentModel.user_id == user_id,
                ConsentModel.terms_version == terms_version,
            )
            .order_by(ConsentModel.accepted_at.desc(), ConsentModel.id.desc())
            .first()
        )
        return self._dict(model) if model is not None else None

    def has_consented(self, user_id: str, terms_version: str) -> bool:
        """現行版に「撤回されていない同意」があるか。"""
        latest = self.latest_for_version(user_id, terms_version)
        if latest is None:
            return False
        if latest["withdrawn_at"] is not None:
            return False
        return bool(latest["accepted"])

    def history(self, user_id: str) -> List[Dict[str, Any]]:
        rows = (
            self.db.query(ConsentModel)
            .filter(ConsentModel.user_id == user_id)
            .order_by(ConsentModel.accepted_at.desc())
            .all()
        )
        return [self._dict(r) for r in rows]

    @staticmethod
    def _dict(model: ConsentModel) -> Dict[str, Any]:
        return {
            "id": model.id,
            "user_id": model.user_id,
            "tenant_id": model.tenant_id,
            "terms_version": model.terms_version,
            "accepted": bool(model.accepted),
            "accepted_at": model.accepted_at,
            "withdrawn_at": model.withdrawn_at,
        }


class AuditRepository:
    """監査ログの書き込みと読み取り（`/api/admin/audit` の裏側）。"""

    def __init__(self, db: Session):
        self.db = db

    def record(
        self,
        tenant_id: str,
        action: str,
        user_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        outcome: str = "success",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """1 イベントを記録する。**tenant_id は必須**（空文字は default に寄せる）。

        `meta` に**個人データ（氏名・生年・原稿本文など）を入れないこと**。
        監査ログは長期保存されるため、ここに識別情報を書くと
        「開示・削除請求」時に消す対象に Derivative が広がる。

        ## `resource_id` は切り詰めない
        以前は `str(resource_id)[:64]` で黙って切り詰めており、
        64 文字を超える ID が**別物**に変わっていた（監査ログと資源の
        突き合わせが静かに壊れる）。列幅は 64 のままなので、
        収まらない場合は**黙って縮めず `ValueError`** を上げる
        （呼び出し側が ID を短くするか、列を広げるかを判断する）。
        """
        resource_id_text = None if resource_id is None else str(resource_id)
        if resource_id_text is not None and len(resource_id_text) > _MAX_RESOURCE_ID_CHARS:
            raise ValueError(
                "resource_id が長すぎます（%d 文字 > %d 文字）: %r"
                % (len(resource_id_text), _MAX_RESOURCE_ID_CHARS, resource_id_text[:32] + "...")
            )
        model = AuditLogModel(
            id=secrets.token_urlsafe(16),
            tenant_id=tenant_id or DEFAULT_TENANT_ID,
            user_id=user_id or None,
            action=str(action)[:64],
            resource_type=(str(resource_type)[:64] if resource_type else None),
            resource_id=resource_id_text,
            outcome=str(outcome)[:16],
            meta_json=json.dumps(meta or {}, ensure_ascii=False, default=str),
            created_at=utcnow(),
        )
        self.db.add(model)
        self.db.flush()
        return self._dict(model)

    def count(self, tenant_id: Optional[str] = None, action: Optional[str] = None) -> int:
        """**監査ログの完全率テスト用**。生成イベント数に対するログ行数を数える。"""
        query = self.db.query(func.count(AuditLogModel.id))
        if tenant_id is not None:
            query = query.filter(AuditLogModel.tenant_id == tenant_id)
        if action is not None:
            query = query.filter(AuditLogModel.action == action)
        return int(query.scalar() or 0)

    def list(
        self,
        tenant_id: Optional[str] = None,
        action: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        query = self.db.query(AuditLogModel)
        if tenant_id is not None:
            query = query.filter(AuditLogModel.tenant_id == tenant_id)
        if action is not None:
            query = query.filter(AuditLogModel.action == action)
        if user_id is not None:
            query = query.filter(AuditLogModel.user_id == user_id)
        rows = (
            query.order_by(AuditLogModel.created_at.desc(), AuditLogModel.id.desc())
            .limit(max(1, min(int(limit), 1000)))
            .offset(max(0, int(offset)))
            .all()
        )
        return [self._dict(r) for r in rows]

    @staticmethod
    def _dict(model: AuditLogModel) -> Dict[str, Any]:
        try:
            meta = json.loads(model.meta_json or "{}")
        except (TypeError, ValueError):
            meta = {}
        return {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "user_id": model.user_id,
            "action": model.action,
            "resource_type": model.resource_type,
            "resource_id": model.resource_id,
            "outcome": model.outcome,
            "meta": meta if isinstance(meta, dict) else {},
            "created_at": model.created_at,
        }


def purge_user_personal_data(db: Session, user_id: str) -> Dict[str, Any]:
    """削除請求の実行本体（`DELETE /api/me` から呼ばれる）。

    方針:
    - **消す**: favorite_tracks / music_profiles / consents（S4 が持つ個人データ）。
    - **匿名化して残す証拠**: `users.email` / `users.hashed_password` は
      `UserRepository.anonymize` 側で書き換える（`users` の行は消さない。
      消すと生成履歴の `user_id` FK が宙ingaるため）。
    - **消さない**: `audit_logs`。削除請求があったという事実は
      利用者のデータではなく**処理の証明**。
      ただし「監査ログ自体の長期保持が開示対象になるか」は
      counsel 確認が必要（`docs/privacy_and_tenancy.md` に明記）。
    """
    repo = MusicProfileRepositoryImpl(db)
    removed = repo.delete_owner(user_id)
    sec = UserSecurityRepository(db).mark_deleted(user_id)
    return {"s4_rows_removed": removed, "deleted_at": sec["deleted_at"]}


__all__ = [
    "TenantRepository",
    "UserSecurityRepository",
    "MusicProfileRepositoryImpl",
    "ConsentRepository",
    "AuditRepository",
    "purge_user_personal_data",
    "ROLE_ADMIN",
    "ROLE_MEMBER",
    "DEFAULT_TENANT_ID",
]
