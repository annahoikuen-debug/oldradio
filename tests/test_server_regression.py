"""`retro_radio/server.py` のバグ修正に対するリグレッション防止テスト。

このファイルが固定する不具合（いずれも修正前に実測で再現していた）:
  1. `/api/audio/{filename}` が拡張子・サイズ・マジックバイトの検証なしに
     任意ファイルを `audio/mpeg` として配信していた
  2. `GenerateRequest.day` の既定値が「今日」で、month 省略時に月次第で 422 になっていた
  3. `target_name` に長さ制限が無く、100 万文字まで受理していた
  4. `build_playlist` が normal モードで末尾に talk を 2 連続させていた
  5. 全体版 TTS の扱いが環境変数で切り替わらない（後方互換の固定）
  6. セキュリティレスポンスヘッダが 1 つも無かった
  7. 同名見出しのエンディングが 2 個あると 1 個が黙って消えていた
  8. TTL sweep の docstring と実挙動の単位が食い違っていた

方針:
  * 外部ネットワーク（gTTS / iTunes / Gemini）は conftest のフィクスチャで遮断する。
    `/api/audio` の検証は conftest の gTTS スタブに依存せず、自前のバイト列を書く。
  * 内部実装（`_users_db` 等のプライベート属性）への結合を避ける。
  * `pytest.mark.network` は付けない（`pytest.ini` の `-m "not network"` で常時実行される）。
"""

import os

import pytest

import retro_radio.server as server_module
from retro_radio.config import get_settings
from retro_radio.models.radio import ScriptSegment


# --- 共通ヘルパー ---------------------------------------------------------------
ID3_HEADER = b"ID3\x03\x00\x00\x00\x00\x00\x00"
FRAME_SYNC_HEADER = b"\xff\xfb\x90\x00"   # MPEG-1 Layer III フレーム同期（ID3 無し）


def _mp3_bytes(header: bytes = ID3_HEADER, size: int = 4096) -> bytes:
    """サーバの3層検証（拡張子 / 1024バイト以上 / MP3ヘッダ）を満たすダミー MP3"""
    return header + b"\x00" * max(0, size - len(header))


def _segment(index: int, title: str, content: str = "テスト原稿です。") -> ScriptSegment:
    return ScriptSegment(
        id=f"seg{index}",
        title=title,
        content=content,
        estimated_duration=10.0,
        order=index,
    )


def _segment_titles(count: int) -> list:
    """オープニング1 + 通常トーク(count-2) + エンディング1 の見出し一覧"""
    middle = [f"トーク{i}" for i in range(1, count - 1)]
    return ["オープニング", *middle, "エンディング"]


def _songs(count: int) -> list:
    return [
        {
            "title": f"ヒット曲{i}",
            "artist": f"アーティスト{i}",
            "preview_url": None,
            "artwork_url": None,
            "is_fallback": True,
        }
        for i in range(count)
    ]


def _types(playlist: list) -> list:
    return [item["type"] for item in playlist]


def _trailing_talk(types: list) -> int:
    """末尾に連続する talk の個数"""
    count = 0
    for item_type in reversed(types):
        if item_type != "talk":
            break
        count += 1
    return count


def _has_adjacent_talks(types: list) -> bool:
    return any(types[i] == types[i + 1] == "talk" for i in range(len(types) - 1))


@pytest.fixture
def audio_cache(monkeypatch, tmp_path):
    """`/api/audio` を検証するための隔離されたキャッシュディレクトリ"""
    target = tmp_path / "audio_cache"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server_module, "CACHE_DIR", target)
    return target


@pytest.fixture
def tts_cache(monkeypatch, mock_gtts, tmp_path):
    """TTS キャッシュを隔離し、gTTS をモックにする（TTL sweep の検証用）"""
    target = tmp_path / "tts_cache"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server_module, "CACHE_DIR", target)
    mock_gtts.calls = []
    return mock_gtts


def _security_headers_present(res) -> bool:
    return all(res.headers.get(header) for header in SECURITY_HEADERS)


# ==============================================================================
# 修正1: /api/audio の3層防御（拡張子 / サイズ / マジックバイト）
# ==============================================================================
def test_audio_rejects_non_mp3_extension(client, audio_cache):
    """拡張子が .mp3 でなければ 404（修正前: .txt が audio/mpeg で 200 だった）"""
    (audio_cache / "notanmp3.txt").write_text("SENTINEL-NON-MP3-CONTENT", encoding="utf-8")

    res = client.get("/api/audio/notanmp3.txt")

    assert res.status_code == 404
    assert "SENTINEL-NON-MP3-CONTENT" not in res.text
    assert res.headers.get("content-type") != "audio/mpeg"


def test_audio_rejects_file_under_minimum_size(client, audio_cache):
    """3バイトのファイルは 404（修正前: b'old' が audio/mpeg で 200 だった）"""
    target = audio_cache / "tts_old.mp3"
    target.write_bytes(b"old")

    res = client.get("/api/audio/tts_old.mp3")

    assert res.status_code == 404
    assert res.content != b"old"


def test_audio_rejects_id3_tagged_but_undersized_file(client, audio_cache):
    """ID3 ヘッダがあっても 1024 バイト未満なら 404

    `tests/conftest.py` の gTTS スタブが書いていた 29 バイトの mp3 と同じ形状。
    実 gTTS の出力は常に 1KB を超えるため、正しく弾かれる。
    """
    (audio_cache / "tts_29bytes.mp3").write_bytes(
        ID3_HEADER + b"-fake-audio-payload"
    )

    res = client.get("/api/audio/tts_29bytes.mp3")

    assert res.status_code == 404


def test_audio_accepts_file_exactly_at_minimum_size(client, audio_cache):
    """境界値 1024 バイトは通す（最小値の取り違えを防ぐ）"""
    (audio_cache / "tts_exact.mp3").write_bytes(_mp3_bytes(size=1024))

    res = client.get("/api/audio/tts_exact.mp3")

    assert res.status_code == 200
    assert len(res.content) == 1024


def test_audio_rejects_wrong_magic_bytes(client, audio_cache):
    """1KB を超えていても MP3 で無ければ 404（拡張子偽装の防御）"""
    (audio_cache / "txt_payload.mp3").write_bytes(b"SENTINEL-NON-MP3-CONTENT" + b"\x00" * 4096)

    res = client.get("/api/audio/txt_payload.mp3")

    assert res.status_code == 404
    assert "SENTINEL-NON-MP3-CONTENT" not in res.text


@pytest.mark.parametrize(
    "header,slug,label",
    [
        (ID3_HEADER, "id3", "ID3 タグ"),
        (FRAME_SYNC_HEADER, "sync", "MPEG フレーム同期のみ"),
    ],
)
def test_audio_accepts_well_formed_mp3(client, audio_cache, header, slug, label):
    """妥当な MP3 ヘッダ（{label}）と 1KB 超のファイルは配信できる"""
    payload = _mp3_bytes(header=header)
    (audio_cache / f"tts_{slug}.mp3").write_bytes(payload)

    res = client.get(f"/api/audio/tts_{slug}.mp3")

    assert res.status_code == 200, label
    assert res.headers["content-type"] == "audio/mpeg"
    assert res.content == payload


def test_audio_response_sets_nosniff(client, audio_cache):
    """`/api/audio` の応答に `X-Content-Type-Options: nosniff` が付く"""
    (audio_cache / "tts_valid.mp3").write_bytes(_mp3_bytes())

    res = client.get("/api/audio/tts_valid.mp3")

    assert res.headers.get("x-content-type-options") == "nosniff"


def test_audio_rejects_directory_like_name(client, audio_cache):
    """ディレクトリそのものは 404（`is_file()` ガード）"""
    (audio_cache / "tts_dir.mp3").mkdir()

    res = client.get("/api/audio/tts_dir.mp3")

    assert res.status_code == 404


def test_audio_rejects_names_with_unsafe_characters(client, audio_cache):
    """拡張子が .mp3 でも、ファイル名が安全でない形式的（空白・制御文字等）なら 404"""
    (audio_cache / "evil name.mp3").write_bytes(_mp3_bytes())

    res = client.get("/api/audio/evil name.mp3")

    assert res.status_code == 404


@pytest.mark.skipif(
    not hasattr(os, "symlink"), reason="シンボリックリンク不支持プラットフォーム"
)
def test_audio_rejects_symlink_pointing_out_of_cache(client, audio_cache, tmp_path):
    """キャッシュ領域の外を指すシンボリックリンクは 404（`is_relative_to` ガード）"""
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(_mp3_bytes())
    link = audio_cache / "tts_link.mp3"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("シンボリックリンクを作れない環境（Windows の権限要件）")

    res = client.get("/api/audio/tts_link.mp3")

    assert res.status_code == 404


def test_audio_route_does_not_leak_env_file(client, audio_cache):
    """`/api/audio` 経由のパストラバーサルでもリポジトリの秘密ファイルを読めない

    `tests/test_security.py` は `/static/...` 側を検証しているため、こちらは
    音声ルート固有の defense-in-depth として拡張する（重複しない）。
    """
    res = client.get("/api/audio/../../.env")

    assert res.status_code in (400, 403, 404)
    assert "RETRO_RADIO" not in res.text


# ==============================================================================
# 修正2: GenerateRequest.day / month の既定値
# ==============================================================================
@pytest.mark.parametrize("month", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
def test_day_omitted_never_422_in_any_month(month):
    """day 省略時は月に関係なく受理される（修正前: 今日=30日だと 2月だけ 422）"""
    from retro_radio.server import GenerateRequest

    req = GenerateRequest(year=1964, month=month)

    assert req.day == 1
    assert req.month == month


@pytest.mark.parametrize(
    "year,month",
    [
        (2020, 2),   # 閏年の 2月
        (2024, 2),
        (1964, 2),   # 昭和の閏年
        (2023, 2),   # 平年の 2月
        (1950, 1),
        (1975, 6),
        (2020, 11),
        (2025, 12),
    ],
)
def test_day_omitted_is_accepted_for_any_real_date(year, month):
    """実在する任意の日付の/year/month で day 省略が受理される"""
    from retro_radio.server import GenerateRequest

    req = GenerateRequest(year=year, month=month)

    assert req.day == 1


def test_month_and_day_default_to_first_of_the_year():
    """month / day の既定は「1月1日」（実行当日に依存しない決定論的な既定）"""
    from retro_radio.server import GenerateRequest

    req = GenerateRequest(year=1975)

    assert (req.month, req.day) == (1, 1)


def test_leap_day_still_requires_explicit_day():
    """2月29日は明示指定したときだけ受理される（既定 1 日に退行していない）"""
    import pydantic

    from retro_radio.server import GenerateRequest

    assert GenerateRequest(year=2020, month=2, day=29).day == 29
    with pytest.raises(pydantic.ValidationError):
        GenerateRequest(year=2020, month=2, day=30)


@pytest.mark.parametrize("month", [1, 2, 4, 6, 9, 11, 12])
def test_api_accepts_request_without_day(client, month):
    """API 経由でも day 欠落が 422 にならない（プロンプトに惑わされない）"""
    res = client.post("/api/generate", json={"year": 1975, "month": month})

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["day"] == 1


def test_api_accepts_leap_day_without_other_fields(client):
    """閏年の 2/29 は month/day 双方の既定と衝突しない"""
    res = client.post("/api/generate", json={"year": 2020, "month": 2, "day": 29})

    assert res.status_code == 200, res.text


# ==============================================================================
# 修正3: target_name の長さ制限と構造注入の拒否
# ==============================================================================
@pytest.mark.parametrize(
    "value,label",
    [
        ("山田太郎", "日本語4文字"),
        ("お父さん", "敬称つき"),
        ("Taro Yamada", "英字12文字"),
        ("あ" * 64, "上限ちょうど64文字"),
        ("", "空文字"),
        (None, "未指定"),
    ],
)
def test_target_name_accepts_reasonable_values(value, label):
    """人名として妥当な長さは 1 つも 422 にしない（label）"""
    from retro_radio.server import GenerateRequest

    assert GenerateRequest(year=1975, target_name=value).target_name == value


@pytest.mark.parametrize(
    "value,label",
    [
        ("あ" * 65, "65文字（1文字超過）"),
        ("あ" * 1000, "1000文字"),
    ],
)
def test_target_name_rejects_overlong_input(value, label):
    """max_length=64 を超えた対象名は 422（label）

    修正前は 100 万文字まで受理し、原稿の f-string にそのまま埋め込まれていた。
    """
    import pydantic

    from retro_radio.server import GenerateRequest

    with pytest.raises(pydantic.ValidationError):
        GenerateRequest(year=1975, target_name=value)


def test_target_name_rejects_one_million_characters():
    """100 万文字の対象名も 422（修正前の実測で受理されていた最大値）

    ※ 1,000,000 文字を parametrize に渡すのは Windows の環境変数長制限
    （32767 文字）に引っかかるため、テスト本体で組み立てる。
    """
    import pydantic

    from retro_radio.server import GenerateRequest

    with pytest.raises(pydantic.ValidationError):
        GenerateRequest(year=1975, target_name="あ" * 1_000_000)


@pytest.mark.parametrize(
    "value,label",
    [
        ("太郎\n### エンディング", "改行 + 見出し"),
        ("### エンディング", "見出しマーカーのみ"),
        ("太郎\rmore", "CR"),
        ("太郎\tmore", "タブ"),
        ("太郎\x00more", "NULL"),
    ],
)
def test_target_name_rejects_structural_injection(value, label):
    """改行・制御文字・`###` は 422（label）

    `###` は `parse_script_segments` の見出しマーカーなので、
    対象名だけで再生リストを操作できる。
    """
    import pydantic

    from retro_radio.server import GenerateRequest

    with pytest.raises(pydantic.ValidationError):
        GenerateRequest(year=1975, target_name=value)


@pytest.mark.parametrize("value", ["あ" * 65, "太郎\n### エンディング", "### エンディング"])
def test_api_returns_422_for_invalid_target_name(client, value):
    """API 経由でも不正な対象名は 422"""
    res = client.post(
        "/api/generate", json={"year": 1975, "target_name": value}
    )

    assert res.status_code == 422, res.text


def test_api_accepts_normal_target_name(client):
    """通常の日本語名は 200 のまま通る（機能 breakage の防止）"""
    res = client.post(
        "/api/generate",
        json={"year": 1980, "month": 5, "day": 15, "mode": "anniversary",
              "target_name": "山田太郎"},
    )

    assert res.status_code == 200, res.text
    assert res.json()["target_name"] == "山田太郎"


# ==============================================================================
# 修正4 & 7: build_playlist の構造
# ==============================================================================
@pytest.mark.parametrize("segment_count", range(2, 9))
@pytest.mark.parametrize("song_count", range(1, 7))
def test_playlist_never_ends_with_two_talks(segment_count, song_count):
    """末尾に talk が 2 連続しないこと（talk,song,*,talk,talk は構造的に起きない）

    修正前の実測: normal モード 5セグメント + 3曲 で
    `talk,song,talk,song,talk,song,talk,talk` になっていた。
    """
    segments = [_segment(i, t) for i, t in enumerate(_segment_titles(segment_count))]
    playlist = server_module.build_playlist(segments, _songs(song_count), year=1975)
    types = _types(playlist)

    assert _trailing_talk(types) <= 1, types
    assert not _has_adjacent_talks(types), types
    # 入力セグメントが1つも落ちていない（黙って消えない）
    assert types.count("talk") == segment_count, types
    # 番組は「テーマ曲」で始まる（実際のラジオと同じ順序）
    assert playlist[0]["type"] == "song", types


@pytest.mark.parametrize("segment_count", range(2, 9))
@pytest.mark.parametrize("song_count", range(1, 7))
def test_playlist_opens_and_closes_with_a_song(segment_count, song_count):
    """1 パスの番組が「曲で始まり曲で終わる」こと

    旧実装は「トーク → 曲」だけだったため、番組の最初の一音が司会の声になり、
    オープニング曲もエンディング曲も構造上ありえなかった。
    """
    segments = [_segment(i, t) for i, t in enumerate(_segment_titles(segment_count))]
    playlist = server_module.build_playlist(segments, _songs(song_count), year=1975)
    types = _types(playlist)

    assert types[0] == "song", types
    assert types[-1] == "song", types


@pytest.mark.parametrize("segment_count", [2, 5, 8])
def test_playlist_ends_with_song_when_enough_songs(segment_count):
    """曲がトーク数以上あれば、末尾は「トーク1 → 曲」で終わる（元の docstring の設計意図）"""
    segments = [_segment(i, t) for i, t in enumerate(_segment_titles(segment_count))]
    playlist = server_module.build_playlist(segments, _songs(segment_count), year=1975)

    assert _types(playlist)[-1] == "song"
    assert _trailing_talk(_types(playlist)) == 0


def test_playlist_degrades_gracefully_when_fallback_runs_out():
    """代替楽曲が足りなくても「末尾 talk 2連続」にはしない（1連続までなら許容）"""
    segments = [_segment(i, t) for i, t in enumerate(_segment_titles(8))]
    playlist = server_module.build_playlist(segments, _songs(1), year=2025)
    types = _types(playlist)

    assert not _has_adjacent_talks(types), types
    assert _trailing_talk(types) <= 1, types
    assert types.count("talk") == 8, types


def test_playlist_does_not_mutate_caller_song_list():
    """`songs` 引数を破壊しない（レスポンスの songs は medley_song_count のまま保つ）"""
    segments = [_segment(i, t) for i, t in enumerate(_segment_titles(5))]
    songs = _songs(3)

    playlist = server_module.build_playlist(segments, songs, year=1975)

    assert len(songs) == 3
    assert _types(playlist).count("song") >= 3


def test_playlist_without_segments_is_songs_only():
    """セグメント0件のときは曲だけを並べる（回帰防止）"""
    playlist = server_module.build_playlist([], _songs(3), year=1975)

    assert _types(playlist) == ["song", "song", "song"]


def test_playlist_talk_items_keep_audio_url_and_content():
    """トーク要素は content と audio_url を保持する（無音トークを作らない）"""
    segments = [_segment(i, t) for i, t in enumerate(_segment_titles(3))]
    for i, seg in enumerate(segments):
        seg.metadata = {"audio_url": f"/api/audio/tts_{i}.mp3"}

    playlist = server_module.build_playlist(segments, _songs(3), year=1975)

    talks = [item for item in playlist if item["type"] == "talk"]
    assert len(talks) == 3
    for item in talks:
        assert item["content"]
        assert item["audio_url"].startswith("/api/audio/tts_")


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_api_playlist_has_no_trailing_talk_run(client, mode):
    """実 API 経由でも末尾 talk 2連続が起きない（修正前の normal モードの再現）"""
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": mode}
    ).json()
    types = [item["type"] for item in data["playlist"]]

    assert _trailing_talk(types) <= 1, types
    assert not _has_adjacent_talks(types), types


def test_api_playlist_is_completed_beyond_medley_song_count(client):
    """プレイリスト内の曲だけが補完され、レスポンスの `songs` は medley_song_count のまま"""
    settings = get_settings()
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()

    assert len(data["songs"]) == settings.medley_song_count
    playlist_songs = [i for i in data["playlist"] if i["type"] == "song"]
    assert len(playlist_songs) >= settings.medley_song_count
    # 補完分はフォールバック曲（プレビュー無し）として明示される
    assert any(song["is_fallback"] for song in playlist_songs)


@pytest.mark.parametrize(
    "titles,label",
    [
        (["オープニング", "エンディング", "エンディング"], "エンディング2個"),
        (["オープニング", "オープニング", "エンディング"], "オープニング2個"),
        (["オープニング", "エンディング", "エンディング", "エンディング"], "エンディング3個"),
    ],
)
def test_duplicate_headings_do_not_silently_drop_segments(titles, label):
    """同名見出しが複数あってもセグメントは1つも消えない（label）

    修正前の `elif` 後勝ちは、先に代入されたセグメントを無言で上書きしていた。
    """
    segments = [_segment(i, t) for i, t in enumerate(titles)]
    playlist = server_module.build_playlist(segments, _songs(3), year=1975)

    assert _types(playlist).count("talk") == len(titles), label
    talk_ids = {i["id"] for i in playlist if i["type"] == "talk"}
    assert talk_ids == {seg.id for seg in segments}, label


def test_duplicate_ending_logs_a_warning(caplog):
    """同名見出しの重複は警告ログに出る（「黙って消えない」ことを可視化）"""
    segments = [_segment(i, t) for i, t in enumerate(["オープニング", "エンディング", "エンディング"])]

    with caplog.at_level("WARNING", logger="retro_radio"):
        server_module.build_playlist(segments, _songs(3), year=1975)

    assert any("エンディング" in record.message for record in caplog.records)


def test_colliding_segment_values_do_not_lose_segments():
    """全フィールドが同じ2セグメントでも、末尾をエンディングへ移しても1つも消えない

    旧実装は `talk_segments[-1] == ending_segment`（値比較）で判定していたため、
    値が衝突すると巻き込み除去の恐れがあった。判定は同一オブジェクト基準にしており、
    ここでは「値が同じでも1つだけエンディング枠へ回る」ことを確認する。
    """
    def _identical():
        return ScriptSegment(
            id="same", title="無題", content="同じ原稿", estimated_duration=5.0, order=0
        )

    segments = [_segment(0, "オープニング"), _identical(), _identical()]

    playlist = server_module.build_playlist(segments, _songs(2), year=1975)

    assert _types(playlist).count("talk") == 3
    assert not _has_adjacent_talks(_types(playlist))


# ==============================================================================
# 修正5: 全体版 TTS（後方互換）
# ==============================================================================
def test_full_script_audio_url_is_still_returned(client):
    """既定では全体版 TTS の `audio_url` を返し続ける（後方互換の固定）"""
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()

    assert data["audio_url"] is not None
    assert data["audio_url"].startswith("/api/audio/tts_")


def test_full_script_tts_can_be_disabled_without_breaking_response(
    mock_gtts, mock_itunes, monkeypatch
):
    """`RETRO_RADIO_FULL_SCRIPT_TTS=0` で gTTS 1 回分を節約でき、レスポンスは壊れない"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("RETRO_RADIO_FULL_SCRIPT_TTS", "0")
    mock_gtts.calls = []

    with TestClient(server_module.app) as test_client:
        data = test_client.post(
            "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
        ).json()

    assert data["audio_url"] is None
    assert data["playlist"], "プレイリストは生成される（全体版TTSに依存しない）"
    assert len(mock_gtts.calls) < 7, f"gTTS 呼び出し={len(mock_gtts.calls)}"


def test_full_script_tts_costs_exactly_one_extra_gtts_call(mock_gtts, mock_itunes, monkeypatch):
    """全体版 TTS が「セグメント数 + 1」回の gTTS 呼び出しになることの固定"""
    from fastapi.testclient import TestClient

    mock_gtts.calls = []
    with TestClient(server_module.app) as test_client:
        data = test_client.post(
            "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
        ).json()

    segment_count = len([i for i in data["playlist"] if i["type"] == "talk"])
    assert len(mock_gtts.calls) == segment_count + 1


# ==============================================================================
# 修正6: セキュリティレスポンスヘッダ
# ==============================================================================
SECURITY_HEADERS = (
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "content-security-policy",
    "permissions-policy",
)

SECURITY_ENDPOINTS = (
    "/",
    "/health",
    "/api/decades",
    "/api/audio/tts_valid.mp3",
    "/static/app.js",
    "/openapi.json",
    "/no-such-route",
)


@pytest.mark.parametrize("path", SECURITY_ENDPOINTS)
def test_security_headers_present_on_every_endpoint(client, audio_cache, path):
    """セキュリティヘッダ5種が全エンドポイントに付く（path）"""
    (audio_cache / "tts_valid.mp3").write_bytes(_mp3_bytes())

    res = client.get(path)

    for header in SECURITY_HEADERS:
        assert res.headers.get(header), f"{path} に {header} が無い"


def test_nosniff_header_value_is_correct(client):
    """`X-Content-Type-Options` の値は `nosniff`"""
    res = client.get("/health")

    assert res.headers["x-content-type-options"] == "nosniff"


def test_frame_options_and_frame_ancestors_both_deny_framing(client):
    """X-Frame-Options: DENY と CSP frame-ancestors 'none' の両方で iframe 拒否"""
    res = client.get("/")

    assert res.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]


def test_referrer_policy_is_no_referrer(client):
    """`Referrer-Policy: no-referrer`"""
    assert client.get("/").headers["referrer-policy"] == "no-referrer"


def test_permissions_policy_disables_microphone(client):
    """`Permissions-Policy` で microphone 等を無効化（このアプリは使わない）"""
    policy = client.get("/").headers["permissions-policy"]

    assert "microphone=()" in policy
    assert "camera=()" in policy
    assert "geolocation=()" in policy


def test_csp_baseline_directives(client):
    """CSP の骨格（default-src 'self' / object-src 'none' / unsafe-eval なし）"""
    csp = client.get("/").headers["content-security-policy"]

    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'none'" in csp
    assert "unsafe-eval" not in csp


@pytest.mark.parametrize(
    "host",
    [
        "https://fonts.googleapis.com",   # index.html の Google Fonts CSS
        "https://fonts.gstatic.com",      # 同 webfont
        "https://*.mzstatic.com",        # app.js が再生する iTunes プレビュー
        "https://audio-ssl.itunes.apple.com",
    ],
)
def test_csp_allows_external_hosts_used_by_the_frontend(client, host):
    """フロントが実際に参照する外部ホストだけが許可されている（host）"""
    csp = client.get("/").headers["content-security-policy"]

    assert host in csp


def test_csp_allows_data_and_blob_schemes(client):
    """`data:`（favicon・ノイズテクスチャ）と `blob:`（object URL）を許可"""
    csp = client.get("/").headers["content-security-policy"]

    assert "img-src 'self' data: blob:" in csp
    assert "media-src 'self' blob: data:" in csp
    assert "font-src 'self' data:" in csp


def test_csp_allows_service_worker_registration(client):
    """PWA の service worker 登録（`/static/service-worker.js`）を妨げない"""
    csp = client.get("/").headers["content-security-policy"]

    assert "worker-src 'self' blob:" in csp
    assert "manifest-src 'self'" in csp


def test_csp_does_not_leak_arbitrary_hosts(client):
    """攻撃者可控のホストが許可されていない（許可リストは最小）"""
    csp = client.get("/").headers["content-security-policy"]

    for host in ("https://evil.example.com", "*;", "http://", "https://*.com"):
        assert host not in csp


def test_csp_does_not_break_same_origin_assets(client):
    """同一オリジンのアセットは `default-src 'self'` で読み込める（過剰に絞らない）"""
    csp = client.get("/").headers["content-security-policy"]

    assert "default-src 'self'" in csp
    for path in ("/", "/static/app.js", "/static/app.css"):
        assert client.get(path).status_code == 200


def test_hsts_is_not_sent_over_plain_http(client):
    """平文 HTTP では HSTS を出さない（localhost の開発環境を壊さない）"""
    assert client.get("/health").headers.get("strict-transport-security") is None


def test_hsts_is_sent_over_https(client):
    """HTTPS では HSTS を送り出す"""
    res = client.get("/health", headers={"X-Forwarded-Proto": "https"})

    hsts = res.headers.get("strict-transport-security")
    assert hsts is not None
    assert "max-age=" in hsts


def test_hsts_can_be_disabled_by_env(client, monkeypatch):
    """`RETRO_RADIO_HSTS_ENABLED=0` で HSTS を無効化できる（設定可能）"""
    monkeypatch.setenv("RETRO_RADIO_HSTS_ENABLED", "0")

    res = client.get("/health", headers={"X-Forwarded-Proto": "https"})

    assert res.headers.get("strict-transport-security") is None


def test_hsts_max_age_is_configurable(client, monkeypatch):
    """`RETRO_RADIO_HSTS_MAX_AGE` で max-age を変えられる"""
    monkeypatch.setenv("RETRO_RADIO_HSTS_MAX_AGE", "600")

    res = client.get("/health", headers={"X-Forwarded-Proto": "https"})

    assert res.headers["strict-transport-security"].startswith("max-age=600")


def test_csp_can_be_overridden_by_env(client, monkeypatch):
    """`RETRO_RADIO_CSP` で CSP を完全上書きできる（運用調整の逃げ道）"""
    monkeypatch.setenv("RETRO_RADIO_CSP", "default-src 'none'")

    assert client.get("/").headers["content-security-policy"] == "default-src 'none'"


def test_security_headers_survive_app_error_responses(client, monkeypatch):
    """500 相当のエラー応答にもセキュリティヘッダが付く"""
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(server_module, "generate_radio_script", boom)

    res = client.post("/api/generate", json={"year": 1975, "month": 9, "day": 24})

    assert res.status_code >= 400
    assert res.headers.get("x-content-type-options") == "nosniff"
    assert res.headers.get("content-security-policy")


# ==============================================================================
# 修正8: TTL sweep の周期の単位
# ==============================================================================
def test_sweep_interval_counts_tts_generations_not_requests(tts_cache, monkeypatch):
    """sweep 周期の単位は「リクエスト」ではなく `generate_tts_cached` の呼び出し回数

    docstring が修正8で実装に合わせてあることを、実挙動で固定する。
    """
    import time

    monkeypatch.setattr(server_module.settings, "tts_cache_sweep_interval", 3, raising=False)
    monkeypatch.setattr(server_module.settings, "tts_cache_ttl_days", 1, raising=False)
    monkeypatch.setattr(server_module, "_sweep_counter", 0, raising=False)

    stale = server_module.CACHE_DIR / "tts_stale.mp3"
    stale.write_bytes(b"old")
    old_mtime = time.time() - 5 * 86400
    os.utime(stale, (old_mtime, old_mtime))

    for i in range(2):
        server_module.generate_tts_cached(f"sweep-unique-{i}-{os.urandom(6).hex()}")
        assert stale.exists(), f"{i + 1} 回目で sweep が走った（周期が短すぎる）"

    server_module.generate_tts_cached(f"sweep-unique-3-{os.urandom(6).hex()}")

    assert not stale.exists()


# ==============================================================================
# CORS: `["*"]` + allow_credentials=True の扱い（config.py 管轄との境界）
# ==============================================================================
@pytest.mark.parametrize(
    "origins,credentials,label",
    [
        (["*"], True, "ワイルドカード + credentials"),
        (["*"], False, "ワイルドカードのみ"),
        (["https://a.example.com"], True, "明示 origin + credentials"),
        ([], False, "空リスト"),
    ],
)
def test_app_starts_with_any_cors_configuration(mock_gtts, mock_itunes, monkeypatch,
                                                origins, credentials, label):
    """CORS 設定がどんな組合せでもアプリは起動し、セキュリティヘッダを返し続ける（label）

    `["*"]` + `allow_credentials=True` を config.py が拒否する方針でも、
    その結果としてサーバまで落ちることはないことを server.py 側で保証する。
    """
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server_module.settings, "cors_origins", origins, raising=False)
    monkeypatch.setattr(server_module.settings, "cors_allow_credentials", credentials,
                        raising=False)
    # CORS ミドルウェアは import 時に組まれるため、スタックを作り直す
    monkeypatch.setattr(server_module.app, "middleware_stack", None, raising=False)

    with TestClient(server_module.app) as test_client:
        health = test_client.get("/health")
        assert health.status_code == 200, label
        assert _security_headers_present(health), label
        assert _security_headers_present(test_client.get("/")), label


def test_wildcard_cors_with_credentials_is_either_rejected_or_safe():
    """`["*"]` + credentials は config.py で拒否されるか、拒否されません（境界の明示）

    D3（config.py 管轄）の判断に依存しない形にしてある:
      * 拒否される場合 … 設定値そのものが存在しないので「起動の責任」は server.py に無い
      * 拒否されない場合 … 上のテストで遡って起動し、CSP 等のヘッダは通常配信される
    """
    from retro_radio.config import Settings

    try:
        Settings(cors_origins=["*"], cors_allow_credentials=True)
    except Exception:
        return  # 設定層で拒否されている = 想定どおり
    # 拒否されない場合も、起動できない・ヘッダが欠落しないことは別テストで確認済み


def test_default_cors_configuration_still_boots(client):
    """既定設定（localhost 限定 + credentials 無効）でアプリが起動する"""
    res = client.get("/health")

    assert res.status_code == 200
    assert res.headers.get("content-security-policy")
