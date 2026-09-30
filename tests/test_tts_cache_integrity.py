"""TTS キャッシュの「隔離」と「モック音源の妥当性」のリグレッション防止テスト。

このファイルが固定する実測済みの不具合:
    `tests/conftest.py` の gTTS スタブが `%TEMP%\\retro_radio_audio_cache`（＝本番の
    TTS キャッシュ）へ直接書き込んでいた。`/api/generate` を使う6テストファイルが
    29バイトの偽 mp3 を本番キャッシュに残し、開発者が実アプリを起動して同じテキストで
    `/api/generate` すると `server.py` の `if filepath.exists(): return filename` が短絡し、
    本物の gTTS が一度も呼ばれず、`/api/audio/...` が 29バイトを `audio/mpeg` で返す。
    ブラウザはそれを mp3 として decode できず無音になる（＝再生不能）。

責務の分界:
    * D1（`tests/test_server_regression.py`）… `/api/audio` の3層防御（拡張子 / 最小サイズ /
      マジックバイト）そのものの検証。
    * D2（このファイル）… 「テストが本番キャッシュを汚さないこと」および
      「モックが*D1 の防御を素通りできる*妥当な mp3 を書くこと」だけを保証する。
      異常ファイルが 404 になることの assert は D1 側に置き、重複させない。

方針:
    * `pytest.mark.network` は付けない（`pytest.ini` の `-m "not network"` で常時実行される）。
    * 本番キャッシュは read-only でしか観測しない（`os.scandir` による stat のみ）。
    * 実行順・回数に依存しない（スナップショットは各テストの直前/直後で取る）。
"""

import os
import tempfile
from pathlib import Path

import pytest

import retro_radio.server as server_module
from conftest import PRODUCTION_CACHE_DIR_NAME, production_cache_dir

# D1 が公開している最小音声サイズのフォールバック（名前が変わっても D2 のテストは意味を失わない）
DEFAULT_AUDIO_MIN_BYTES = 1024

# 旧 conftest のモックが書いていたバイト列（29バイト）。サーバの最小サイズを下回っていた。
LEGACY_FAKE_MP3 = b"ID3" + bytes((3, 0, 0, 0, 0, 0, 0)) + b"-fake-audio-payload"


# --- ヘルパー -------------------------------------------------------------------
def snapshot_production_cache() -> dict:
    """本番 TTS キャッシュの `名前 -> (mtime_ns, size)` を返す（read-only）。

    ディレクトリが無い場合は空 dict。存在しないこと自体も「変化」として扱う。
    """
    root = production_cache_dir()
    if not root.is_dir():
        return {}
    snapshot = {}
    with os.scandir(root) as entries:
        for entry in entries:
            stat = entry.stat()
            snapshot[entry.name] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def describe_delta(before: dict, after: dict) -> str:
    """スナップショットの差分を失敗メッセージ向けに整形する。"""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    return f"追加={added} 削除={removed} 変更={changed}"


def server_audio_min_bytes() -> int:
    """サーバ側の最小音声サイズ（バイト）を返す。"""
    return int(getattr(server_module, "AUDIO_MIN_BYTES", DEFAULT_AUDIO_MIN_BYTES))


# --- フィクスチャ ----------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_production_cache_writes(request):
    """【最重要】本番 `%TEMP%` キャッシュが変化していないことを全テストで保証する。

    テストごとに「名前 -> (mtime_ns, size)」を撮って比較する。読み取り専用のみで、
    本番ディレクトリの作成・書き込み・削除のいずれも検知されない。
    他のテストが先に `%TEMP%` を汚していても、前後比較なので成立する（冪等）。
    """
    before = snapshot_production_cache()
    yield
    after = snapshot_production_cache()
    assert after == before, (
        f"テスト {request.node.name} が本番 TTS キャッシュ "
        f"({production_cache_dir()}) を変更した: {describe_delta(before, after)}"
    )


@pytest.fixture
def isolated_cache_dir():
    """テスト中に `retro_radio.server.CACHE_DIR` が指す隔離先ディレクトリ。"""
    return Path(server_module.CACHE_DIR)


@pytest.fixture
def pytest_base_tmp(tmp_path_factory):
    """pytest 自身の一時ルート（隔離先がここ配下にあることの検証に使う）。"""
    return Path(tmp_path_factory.getbasetemp())


# ==============================================================================
# 1. 本番キャッシュを汚さないこと（最重要）
# ==============================================================================
def test_api_generate_never_touches_production_cache(client, isolated_cache_dir):
    """【最重要】`/api/generate` は本番 `%TEMP%` キャッシュを1バイトも変更しない。

    隔離前は、6テストファイルの1実行だけで本番キャッシュに 41ファイル
    （うち33個が29バイトの偽 mp3）が新規作成された。
    """
    before = snapshot_production_cache()

    res = client.post("/api/generate", json={"year": 1971, "month": 3, "day": 3})
    assert res.status_code == 200, res.text

    # 隔離は「無効化」ではない。隔離先には実際に生成が起きている。
    assert list(isolated_cache_dir.glob("tts_*.mp3")), "隔離先に音声が生成されていない"

    assert snapshot_production_cache() == before, (
        f"本番キャッシュが変更された: {describe_delta(before, snapshot_production_cache())}"
    )


def test_repeated_generation_keeps_production_cache_untouched(client):
    """同じテキストで繰り返し生成しても本番キャッシュは無変化（キャッシュヒット含む）。"""
    before = snapshot_production_cache()
    payload = {"year": 1963, "month": 7, "day": 7}

    first = client.post("/api/generate", json=payload)
    second = client.post("/api/generate", json=payload)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["audio_url"] == second.json()["audio_url"], "2回目がキャッシュヒットしてない"
    assert snapshot_production_cache() == before, (
        f"本番キャッシュが変更された: {describe_delta(before, snapshot_production_cache())}"
    )


def test_audio_endpoint_never_touches_production_cache(client, isolated_cache_dir):
    """`/api/audio` の読み取りも本番キャッシュを変更しない（TTL sweep の誤爆も含む）。"""
    filename = client.post(
        "/api/generate", json={"year": 1958, "month": 4, "day": 4}
    ).json()["audio_url"].rsplit("/", 1)[-1]
    assert (isolated_cache_dir / filename).is_file()

    before = snapshot_production_cache()
    res = client.get(f"/api/audio/{filename}")

    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/mpeg"
    assert snapshot_production_cache() == before, (
        f"本番キャッシュが変更された: {describe_delta(before, snapshot_production_cache())}"
    )


def test_lifespan_creates_only_the_isolated_cache_dir(client, isolated_cache_dir):
    """`TestClient` の lifespan（`_ensure_cache_dir`）が作るのは隔離先だけ。

    修正前は lifespan が `%TEMP%\\retro_radio_audio_cache` を `mkdir` していた。
    """
    assert isolated_cache_dir.is_dir(), "lifespan が隔離先を作っていない"

    before = snapshot_production_cache()
    assert snapshot_production_cache() == before
    assert Path(server_module.CACHE_DIR) != production_cache_dir()


# ==============================================================================
# 2. 隔離そのものが機能していること
# ==============================================================================
def test_cache_dir_is_isolated_from_production_temp_dir(isolated_cache_dir):
    """テスト中の `server.CACHE_DIR` は本番 `%TEMP%` パスではない。"""
    production = production_cache_dir()

    assert production.name == PRODUCTION_CACHE_DIR_NAME
    assert production == Path(tempfile.gettempdir()) / "retro_radio_audio_cache"
    assert isolated_cache_dir != production
    assert production not in isolated_cache_dir.parents, "隔離先が本番キャッシュ配下にある"


def test_isolated_cache_dir_lives_under_pytest_tmp_root(isolated_cache_dir, pytest_base_tmp):
    """隔離先は pytest の一時ルート配下（後始末が pytest に任される）。"""
    assert pytest_base_tmp in isolated_cache_dir.parents, (
        f"{isolated_cache_dir} は pytest の一時ルート {pytest_base_tmp} の配下ではない"
    )


def test_isolated_cache_dir_is_unique_per_test(isolated_cache_dir, tmp_path):
    """隔離先はテストごとに独立（他テストの残骸が混ざらない）。"""
    assert isolated_cache_dir.parent == tmp_path


# ==============================================================================
# 3. モック gTTS が書くバイト列の妥当性
# ==============================================================================
def test_mock_gtts_writes_a_plausible_mp3(mock_gtts, tmp_path):
    """モックは 1KB 以上の `ID3` ヘッダ付き mp3 を書く。

    29バイトの `b"ID3...-fake-audio-payload"` のままだと、サーバの最小サイズ防御に
    弾かれて `res.content` が空になり、`/api/audio` の正常系を検証できなくなる。
    """
    target = tmp_path / "sample.mp3"
    mock_gtts(text="隔離テスト用の原稿", lang="ja", tld="co.jp", slow=False).save(target)

    data = target.read_bytes()

    assert len(data) >= DEFAULT_AUDIO_MIN_BYTES, f"モックが {len(data)} バイトしか書かない"
    assert data[:3] == b"ID3", "ID3v2 タグが無い（実 gTTS と同じ形になっていない）"
    assert bytes((0xFF, 0xFB)) in data, "MPEG フレーム同期バイトが無い"


def test_mock_gtts_payload_meets_server_minimum_audio_size(mock_gtts, tmp_path):
    """モックの出力はサーバの `AUDIO_MIN_BYTES` を下回らない（下回るなら作り直す）。"""
    target = tmp_path / "sample.mp3"
    mock_gtts(text="最小サイズの下限確認").save(target)

    minimum = server_audio_min_bytes()

    assert target.stat().st_size >= minimum, (
        f"モック出力 {target.stat().st_size} バイト < サーバ最小値 {minimum} バイト"
    )


def test_legacy_fake_payload_would_be_rejected_by_server_minimum():
    """旧 29 バイトペイロードが弾かれる理由を固定する（モックの縮小を禁じる）。

    サーバの防御そのものは D1 の `tests/test_server_regression.py` が検証する。
    ここは「モックを 29 バイトへ戻してはいけない」根拠だけを明文化する。
    """
    assert len(LEGACY_FAKE_MP3) == 29
    assert len(LEGACY_FAKE_MP3) < server_audio_min_bytes(), (
        "サーバの最小サイズが 29 バイト以下になった。恒久的に妥当なら conftest を更新すること"
    )


# ==============================================================================
# 4. 隔離と読み取り（`/api/audio`）の整合
# ==============================================================================
def test_audio_endpoint_serves_the_file_from_isolated_cache(client, isolated_cache_dir):
    """`/api/audio/{filename}` は隔離先のファイルとバイト単位で一致する。"""
    audio_url = client.post(
        "/api/generate", json={"year": 1969, "month": 7, "day": 20}
    ).json()["audio_url"]
    filename = audio_url.rsplit("/", 1)[-1]

    stored = isolated_cache_dir / filename
    assert stored.is_file(), "生成音声が隔離先に無い"

    res = client.get(audio_url)

    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "audio/mpeg"
    assert res.content == stored.read_bytes()


def test_generated_audio_passes_the_server_guards(client, isolated_cache_dir):
    """生成直後のモック音声はサーバの防御を通過できる（防御が「前身」にならない）。

    `/api/audio` が生成直後でも 200 でなければ、それは「防御が正しい」のではなく
    「モック音源が壊れている」状態であり、D1 の防御テストが無意味になる。
    異常ファイルが 404 になることの検証は D1 側の責務。
    """
    audio_url = client.post(
        "/api/generate", json={"year": 1974, "month": 1, "day": 1}
    ).json()["audio_url"]

    stored = isolated_cache_dir / audio_url.rsplit("/", 1)[-1]
    minimum = server_audio_min_bytes()

    assert stored.stat().st_size >= minimum
    assert client.get(audio_url).status_code == 200


def test_no_orphan_tmp_files_in_isolated_cache(client, isolated_cache_dir):
    """atomic install により `.tmp` が隔離先にも残らない（後始末の健全性）。"""
    client.post("/api/generate", json={"year": 1966, "month": 9, "day": 9})

    leftovers = [p.name for p in isolated_cache_dir.iterdir() if p.suffix == ".tmp"]

    assert leftovers == []
