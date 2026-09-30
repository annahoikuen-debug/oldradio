"""テスト共通フィクスチャ。

方針:
1. 外部ネットワーク（iTunes / gTTS / Gemini / ElevenLabs）を conftest レベルで全面遮断する。
   外部依存のあるテストは必ず `mock_gtts` / `mock_itunes` などのフィクスチャで差し替える。
   （遮断は `requests.sessions.Session.request` と `socket.create_connection` に行う。
   `requests.get` などの上位 API を patch すれば迂回できるため、モックは従来どおり可能。）
2. `RETRO_RADIO_GEMINI_API_KEY` に依存するテストを禁止するため、API キーは必ず空にする。
   Gemini 未設定時のフォールバック原稿のみを検証する。
3. テスト用 SQLite DB はリポジトリ直下を作らず一時ディレクトリに置く。
4. TTS キャッシュディレクトリは本番 `%TEMP%` ではなく pytest の一時ディレクトリへ隔離する。
   隔離しないと pytest が本番キャッシュへ偽の mp3 を書き込み、開発者の実アプリが
   「キャッシュヒット」短絡で再生不能な mp3 を返してしまう。
5. 曲ストア（選曲履歴・プレビュー解決キャッシュ）も実行ごとに隔離する。
   隔離しないと、前回実行で解決した本物のプレビュー URL が残り、
   ネットワーク非依存のテストが実行履歴に依存してしまう。
"""

import importlib
import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# --- retro_radio を import する前に環境変数を確定させる -------------------------
# Settings は import 時に 1 度だけ生成されるため、ここより下に retro_radio を書かない。
os.environ["RETRO_RADIO_GEMINI_API_KEY"] = ""
os.environ["RETRO_RADIO_ELEVENLABS_API_KEY"] = ""
os.environ["RETRO_RADIO_STRIPE_SECRET_KEY"] = ""
os.environ.setdefault("RETRO_RADIO_SECRET_KEY", "pytest-secret-key-not-for-production")
# 実行ごとに一意な DB ファイルを使い、前回の実行の残骸を inheriting しない
_TEST_DB_PATH = (
    Path(tempfile.gettempdir()) / f"retro_radio_pytest_{os.getpid()}.db"
).as_posix()
os.environ["RETRO_RADIO_DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
# 曲ストア（再生履歴 + プレビュー解決キャッシュ）も実行ごとに隔離する。
# ここを隔離しないと、前回の実行で解決した本物の iTunes プレビュー URL が
# 残っており、「iTunes が 0 件を返す」テストがキャッシュヒットで
# `preview_url` ありになって失敗する（ネットワーク非依存のテストが
# 実行履歴に依存してしまう）。
os.environ["RETRO_RADIO_SONG_STORE_PATH"] = (
    Path(tempfile.gettempdir()) / f"retro_radio_songstore_pytest_{os.getpid()}.db"
).as_posix()

import pytest  # noqa: E402
import requests  # noqa: E402

from retro_radio.models.user import PlanType  # noqa: E402


class _FakeResponse:
    """requests.Response の最小スタブ（iTunes 検索のモック用）"""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# --- 本番 TTS キャッシュディレクトリの所在 ---------------------------------------
# `retro_radio.server.CACHE_DIR` の既定値。テストは必ずこのパスから隔離すること。
PRODUCTION_CACHE_DIR_NAME = "retro_radio_audio_cache"


def production_cache_dir() -> Path:
    """本番（テスト以外）で使われる TTS キャッシュディレクトリのパスを返す。"""
    return Path(tempfile.gettempdir()) / PRODUCTION_CACHE_DIR_NAME


# --- モック gTTS が書き出すダミー MP3 -------------------------------------------
# 実 gTTS の出力は常に 1KB を超える。旧来の 29 バイト `b"ID3...-fake-audio-payload"` は
# サーバ側の最小サイズ / マジックバイト防御に弾かれ、`/api/audio` まで到達できない。
# 防御をテストできる状態を保つため、ID3v2 タグ + MPEG フレームの 2048 バイトに増やす。
MOCK_MP3_SIZE = 2048
_MP3_ID3_HEADER = b"ID3" + bytes((3, 0, 0))  # ID3v2.3 ヘッダ（ID3 + バージョン + フラグ）
_MP3_FRAME = bytes((0xFF, 0xFB, 0x90, 0x00)) + b"\x00" * 508  # MPEG-1 L3 128kbps 44.1kHz 相当


def _synchsafe(value: int) -> bytes:
    """ID3v2 の syncsafe integer（各バイトの上位 1bit が必ず 0 になる）"""
    return bytes(((value >> shift) & 0x7F) for shift in (21, 14, 7, 0))


def build_dummy_mp3(total: int = MOCK_MP3_SIZE) -> bytes:
    """テスト用のダミー MP3 バイト列を生成する（ID3v2.3 タグ + MPEG フレーム）。"""
    title = bytes((3,)) + b"retro radio dummy tts"  # 先頭 0x03 = UTF-8
    frame = b"TIT2" + len(title).to_bytes(4, "big") + b"\x00\x00" + title
    tag_body = frame + b"\x00" * 16
    tag = _MP3_ID3_HEADER + _synchsafe(len(tag_body)) + tag_body
    payload = bytearray(tag)
    while len(payload) < total:
        payload += _MP3_FRAME
    return bytes(payload[:total])


class _FakeGTTS:
    """ネットワークを一切使わない gTTS スタブ。save() が実際の mp3 形式を書き込む。"""

    calls: list = []

    def __init__(self, text="", lang=None, tld=None, slow=False, **kwargs):
        self.text = text
        self.lang = lang
        self.tld = tld
        self.slow = slow
        type(self).calls.append(self)

    def save(self, path):
        Path(path).write_bytes(build_dummy_mp3())


# --- 本番 TTS キャッシュディレクトリの隔離 ---------------------------------------
@pytest.fixture(autouse=True)
def _isolate_tts_cache(monkeypatch, tmp_path):
    """TTS キャッシュディレクトリを pytest の一時ディレクトリへ隔離する。

    `retro_radio.server.CACHE_DIR` はモジュールレベル変数だが、その参照は全て
    関数本体（`_ensure_cache_dir` / `_sweep_tts_cache` / `generate_tts_cached` /
    `get_audio`）の実行時にグローバル名を解決する。そのため lifespan やルート
    ハンドラを書き換えなくても monkeypatch がそのまま効く。
    `core.tts.TTS_CACHE_DIR` も同様に隔離し、%TEMP% への書き込みを総ざらいで防ぐ。

    隔離先は必ず `mkdir` しておく。`core.tts` は import 時に一度だけ
    `TTS_CACHE_DIR.mkdir()` を実行する（`retro_radio/core/tts.py` の55行目）ため、
    import 後にパスを差し替えるとディレクトリが存在せず、キャッシュ書き込みが
    必ず失敗する。结果として TTS のキャッシュ経路がテストで一度も通らないまま
    「通っているように見えていた」。
    """
    import retro_radio.core.tts as core_tts
    import retro_radio.server as server_module

    cache_dir = tmp_path / "audio_cache"
    core_tts_cache_dir = tmp_path / "core_tts_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    core_tts_cache_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(server_module, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(core_tts, "TTS_CACHE_DIR", core_tts_cache_dir, raising=False)

    production = production_cache_dir()
    assert cache_dir != production, "TTS キャッシュが本番 %TEMP% ディレクトリと重なっている"
    assert production not in cache_dir.parents, "隔離先が本番キャッシュディレクトリ配下にある"


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """外部ネットワークへの到達を遮断する（想定外のHTTP呼び出しを失敗として可視化する）。"""

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "テスト中の外部ネットワーク通信は禁止されています。"
            "mock_gtts / mock_itunes フィクスチャで差し替えてください。"
        )

    monkeypatch.setattr(requests.sessions.Session, "request", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture(autouse=True)
def _isolate_song_store(monkeypatch, tmp_path):
    """曲ストアを**テスト単位**で隔離する。

    プロセス単位の一時ファイル（`RETRO_RADIO_SONG_STORE_PATH`）だけを
    隔離先にしてると、同じ実行の中で先に走ったテストの「音源なし」判定が
    次のテストへ漏れる。テストの結果が実行順序に依存してはいけない。

    隔離対象は ``get_settings()`` だけ insuficiente。各モジュールは
    import 時に ``settings = get_settings()`` として**オブジェクトを
    掴んでおり**、``test_auth_wiring.py`` の ``get_settings.cache_clear()``
    で新しいオブジェクトが作られると、掴まえた古いオブジェクトは
    隔離されないまま残る（実測でテストの順序依存になっていた）。
    よって**掴まれている全ての settings オブジェクト**を差し替える。
    """
    from retro_radio.config import get_settings

    target = str(tmp_path / "song_store.db")
    patched = set()

    def _patch(holder):
        if holder is not None and id(holder) not in patched:
            monkeypatch.setattr(holder, "song_store_path", target)
            patched.add(id(holder))

    _patch(get_settings())

    # import 時に `settings = get_settings()` として掴むモジュール
    for module_name in (
        "retro_radio.server",
        "retro_radio.core.music_search",
        "retro_radio.core.preview_resolver",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover - 循環参照など
            continue
        _patch(getattr(module, "settings", None))


@pytest.fixture
def mock_gtts(monkeypatch):
    """gTTS をネットワーク不要のスタブに差し替える（生成回数と lang/tld を検証できる）。"""
    import retro_radio.core.tts as core_tts
    import retro_radio.server as server_module

    _FakeGTTS.calls = []
    monkeypatch.setattr(server_module, "gTTS", _FakeGTTS)
    monkeypatch.setattr(core_tts, "gTTS", _FakeGTTS)
    return _FakeGTTS


@pytest.fixture
def mock_itunes(monkeypatch):
    """iTunes Search API を差し替える。返り値は ``mock_itunes.results`` で調整する。"""

    class _Itunes:
        def __init__(self):
            self.results: list = []
            self.calls: list = []

        def __call__(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return _FakeResponse({"resultCount": len(self.results), "results": self.results})

        def hit(self, count=3, preview=True):
            self.results = [
                {
                    "trackName": f"ヒット曲{i}",
                    "artistName": f"アーティスト{i}",
                    "previewUrl": f"http://example.com/{i}.mp3" if preview else None,
                    "artworkUrl100": f"http://example.com/{i}.jpg",
                }
                for i in range(count)
            ]
            return self

        def empty(self):
            self.results = []
            return self

        def match(self, track_name, artist_name, preview=True):
            """正本の 1 曲と**一致する**結果だけを返す。

            選曲経路は「曲名もアーティストも一致するもの」だけを採用する
            （曖昧一致で別の曲を鳴らすのを防ぐため）。そのため
            「ヒットした」ことを検証するテストは、返り値をこの形で
            作る必要がある。旧来の `hit()`（汎用ダミー）は一致しないので
            音源なしになる。
            """
            return self.match_many([(track_name, artist_name)], preview=preview)

        def match_many(self, songs, preview=True):
            """複数の正本曲すべてに一致する結果を返す。

            選曲セレクタは再生履歴でローテーションするため、**どの曲が
            選ばれるかは事前には分からない**。特定の 1 曲だけ一致させて
            居ると、履歴の順番次第でその曲が選ばれず flaky になる。
            候補プール全体を一致させて結果に依存しないようにする。
            """
            self.results = [
                {
                    "trackName": title,
                    "artistName": artist,
                    "previewUrl": f"http://example.com/{index}.mp3" if preview else None,
                    "artworkUrl100": f"http://example.com/{index}.jpg",
                }
                for index, (title, artist) in enumerate(songs)
            ]
            return self

    import retro_radio.core.music_search as music_search

    fake = _Itunes()
    monkeypatch.setattr(music_search.requests, "get", fake)
    return fake


@pytest.fixture
def client(mock_gtts, mock_itunes):
    """外部依存を全て遮断した TestClient（lifespan も実行する）。"""
    from fastapi.testclient import TestClient

    from retro_radio.server import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_session():
    """テーブルを1度だけ作成し、テストごとに全行を消した SQLAlchemy Session。

    DB ファイルはセッション共有のため、テスト間でデータが残ると
    email の一意制約などに引っかかる。DDL の drop/create は並行接続と競合し
    "no such table" を招くため、delete によるクリーンアップを行う。
    """
    from retro_radio.db.models import Base
    from retro_radio.db.session import get_db_sync, get_engine

    engine = get_engine()
    Base.metadata.create_all(bind=engine)  # checkfirst=True なので2回目以降は no-op
    session = get_db_sync()
    try:
        # 子テーブルから順に全行削除する（FK 制約違反を避ける）
        with session.begin():
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(table.delete())
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def make_user():
    """UserRepository 経由でユーザーを作るヘルパー。"""

    def _make(session, email="user@example.com", hashed_password="hashed", plan=PlanType.FREE):
        from retro_radio.db.repository import UserRepository

        user = UserRepository(session).create(email, hashed_password)
        if plan is not PlanType.FREE:
            user.plan = plan
            UserRepository(session).update(user)
        return user

    return _make


@pytest.fixture
def make_generation():
    """GenerationRepository 経由で世代レコードを生成するヘルパー。"""

    def _make(session, user_id, **overrides):
        from retro_radio.db.repository import GenerationRepository

        entry = {
            "year": 1980,
            "month": 5,
            "day": 15,
            "script": "テスト原稿",
            "song_title": "テスト曲",
            "artist_name": "テストアーティスト",
        }
        entry.update(overrides)
        return GenerationRepository(session).create(user_id, entry)

    return _make


@pytest.fixture
def mock_user_free():
    user = MagicMock()
    user.id = "test_free_user"
    user.plan = PlanType.FREE
    return user


@pytest.fixture
def mock_user_premium():
    user = MagicMock()
    user.id = "test_premium_user"
    user.plan = PlanType.PREMIUM
    return user


@pytest.fixture
def mock_user_pro():
    user = MagicMock()
    user.id = "test_pro_user"
    user.plan = PlanType.PRO
    return user


@pytest.fixture
def plan_controller(mock_user_free):
    from retro_radio.app.plan_control import PlanController

    return PlanController()
