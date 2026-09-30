import logging
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import List, Mapping, Optional, TypedDict

from ..models.user import User

logger = logging.getLogger(__name__)


def _create_session_state() -> dict:
    """新しいセッション状態の器を作る（呼び出しごとに独立したオブジェクトを返す）"""
    return {
        "audio_cache": {},
        "generation_history": [],
        "error_voice_guidance": False,
        "language": "ja",
    }


# 実行コンテキスト（スレッド／リクエスト／async タスク）単位のストレージ。
# プロセスグローバルな辞書は使わない（複数ユーザー間で状態が混ざるため）。
#
# ContextVar は「束縛」だけを分離するため、可変 dict をそのまま持つと
# copy_context() で複製した子コンテキストからの書き込みが親へ漏れる。
# ここでは束縛を不変（MappingProxyType）に保ち、書き込みは
# 「複製 -> 変更 -> set()」の copy-on-write で行う。
# これにより copy_context() の子での変更は親に伝播しない。
_session_state = ContextVar("retro_radio_session_state", default=None)
_current_user = ContextVar("retro_radio_current_user", default=None)


def _get_session_state() -> Mapping[str, object]:
    """セッション状態（実行コンテキスト単位の不変ビュー）を返す"""
    state = _session_state.get()
    if state is None:
        state = _create_session_state()
        _session_state.set(state)
    return MappingProxyType(state)


def _update_session_state(mutator) -> None:
    """copy-on-write でセッション状態を更新する（親の値は書き換えない）"""
    current = dict(_get_session_state())
    mutator(current)
    _session_state.set(current)


@dataclass
class HistoryEntry:
    year: int
    date: str
    script: str
    song_title: str
    artist_name: str
    preview_url: Optional[str]
    audio_path: Optional[str]
    timestamp: str
    all_songs: Optional[list[dict]] = None
    def to_dict(self) -> dict: return asdict(self)
    @classmethod
    def from_dict(cls, d: dict) -> 'HistoryEntry': return cls(**d)

class SessionState(TypedDict):
    audio_cache: dict
    generation_history: List[HistoryEntry]
    error_voice_guidance: bool
    language: str

DEFAULT_STATE: SessionState = _create_session_state()
# DEFAULT_STATE 内の可変オブジェクトを直接共有しないための複製ヘルパー。
# （DEFAULT_STATE は「キーと既定値の説明」としてのみ使い、実データは常に 새로作る）
def _fresh_defaults() -> dict:
    return _create_session_state()

def init_session_state() -> None:
    """アプリ起動時に1回呼ぶ"""
    try:
        missing = {k: v for k, v in _fresh_defaults().items() if k not in _get_session_state()}
        if missing:
            _update_session_state(lambda state: state.update(missing))
    except Exception as e:
        logger.debug(f"セッション状態の初期化に失敗しました: {e}")

def get_history() -> List[HistoryEntry]:
    try:
        entries = list(_get_session_state().get("generation_history") or [])
        return [HistoryEntry.from_dict(h) if isinstance(h, dict) else h for h in entries]
    except Exception as e:
        logger.debug(f"履歴の取得に失敗しました: {e}")
        return []

def add_history(entry: HistoryEntry) -> None:
    try:
        history = get_history()
        history.insert(0, entry)
        trimmed = [h.to_dict() if hasattr(h, 'to_dict') else h for h in history[:10]]
        _update_session_state(lambda state: state.__setitem__("generation_history", trimmed))
    except Exception as e:
        logger.debug(f"履歴の保存に失敗しました: {e}")

def clear_history() -> None:
    try:
        _update_session_state(lambda state: state.__setitem__("generation_history", []))
    except Exception as e:
        logger.debug(f"履歴のクリアに失敗しました: {e}")

def get_audio_cache() -> dict:
    try:
        return dict(_get_session_state().get("audio_cache") or {})
    except Exception as e:
        logger.debug(f"オーディオキャッシュの取得に失敗しました: {e}")
        return {}

def set_audio_cache(key: str, value: bytes) -> None:
    try:
        def _mutate(state: dict) -> None:
            cache = dict(state.get("audio_cache") or {})
            cache[key] = value
            state["audio_cache"] = cache

        _update_session_state(_mutate)
    except Exception as e:
        logger.debug(f"オーディオキャッシュの保存に失敗しました: {e}")

def get_error_voice_guidance() -> bool:
    try:
        return bool(_get_session_state().get("error_voice_guidance", False))
    except Exception as e:
        logger.debug(f"音声案内設定の取得に失敗しました: {e}")
        return False

def set_error_voice_guidance(value: bool) -> None:
    try:
        _update_session_state(lambda state: state.__setitem__("error_voice_guidance", value))
    except Exception as e:
        logger.debug(f"音声案内設定の保存に失敗しました: {e}")


class SessionManager:
    """ログイン中のユーザーを実行コンテキスト単位で保持する（プロセス共有しない）"""

    def set_user(self, user: User):
        _current_user.set(user)

    def get_user(self) -> Optional[User]:
        return _current_user.get()

    def clear_user(self):
        _current_user.set(None)
