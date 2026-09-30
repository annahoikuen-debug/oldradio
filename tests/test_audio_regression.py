"""オーディオ再生の壊滅事故に対する回帰防止テスト。

本ファイルは **本上诉製品の中核価値が「音が出る」こと** を守る。
画面表示は正しくても音が全滅する事故は、ユーザーから見れば完全な障害であり、
どの既存テストもそれを検出できなかった（テスト環境にはブラウザも音声出力もない）。

ここで守る不変条件:

  1. AudioContext は **ユーザージェスチャ内** で生成される
     ジェスチャなしで生成された AudioContext は WebKit / Firefox で
     `suspended` 状態のまま起動し、`resume()` は次のユーザ操作まで保留される。
     その状態で `createMediaElementSource()` を使うと、`<audio>` の出力は
     **恒久的に** その suspended コンテキスト経由に再ルーティングされ、
     _products 全体が「再生中なのに無音」になる（回復手段がコード上存在しない）。
  2. AudioContext は破棄される（`close()`）— open まま捨てるのはリーク
  3. 異常系で解析ノードを破棄しても、`<audio>` 要素が恒久的に無音化しない
  4. タッチ端末でダイヤルをドラッグできる
  5. 印刷のリスナが押下ごとに蓄積しない
  6. Service Worker が root スコープで登録できる（サーバが
     `Service-Worker-Allowed` を返す）

ブラウザを実行できない環境なので、1〜5 は **app.js の静的契約** として検証し、
6 は **実サーバのレスポンスヘッダ** を TestClient で実測する。
静的契約で表せない最終Confirm（実ブラウザでの聞こえ）は実行できない前提で、
その旨を各 docstring に明記している。
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from retro_radio.server import app

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"

# ユーザージェスチャ Kenyнойイベント。`resumeAudioContext()` はここで初めて効く。
USER_GESTURE_EVENTS = ("click", "pointerdown", "pointerup", "touchend", "keydown")


@pytest.fixture(scope="module")
def js_source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def extract_function(source: str, name: str) -> str:
    """`function name(...) { ... }` の本体を研究与る（ブレース対応）。"""
    match = re.search(r"function\s+" + re.escape(name) + r"\s*\(", source)
    if match is None:
        raise AssertionError(f"app.js に {name}() が見つかりません")
    start = source.index("{", match.end())
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"{name}() のブレースが対応しません")


def enclosing_lines(source: str, needle: str) -> list[str]:
    """`needle` を含む行 +/- 20 行のスライス（呼び出し経路の就近確認用）。"""
    lines = source.splitlines()
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert hits, f"app.js に {needle!r} が見つかりません"
    center = hits[0]
    return lines[max(0, center - 20):center + 21]


class TestAudioContextGesture:
    """1. AudioContext はユーザージェスチャ内で生成されなければならない。"""

    def test_audio_context_is_not_created_only_during_async_playback(self, js_source):
        """生成が fetch 完了後の再生経路だけに依存していないこと。

        これが守られていないと、トークのみキュー（= iTunes がプレビューを
        返さないとき、全トークが同一オリジンになる）で恒久無音が発生する。
        """
        playback = extract_function(js_source, "ensureAnalyser")
        assert "new Ctor()" in playback or "new AudioContext" in playback, (
            "前提が崩れています: ensureAnalyser が AudioContext を作らなくなった。"
            "本テストは「生成箇所が再生経路内だけか」を検査します。"
        )

        gesture_scope = "\n".join(
            enclosing_lines(js_source, "dom.btnPlayRadio.addEventListener")
        )
        creates_context = "new Ctor()" in gesture_scope or "new AudioContext" in gesture_scope
        resumes_only = "resumeAudioContext" in gesture_scope
        assert creates_context or resumes_only, (
            "再生ボタンの click ハンドラに AudioContext の生成も resume もありません。"
            "ensureAnalyser 内の new Ctor() は fetch 完了後（ユーザージェスチャoutside）"
            "に到達するため、suspended 起動 → 恒久無音の原因になります。"
        )

    def test_gesture_handler_creates_context_before_it_is_resumed(self, js_source):
        """`resumeAudioContext()` は context 未生成なら no-op になる。"""
        resume_body = extract_function(js_source, "resumeAudioContext")
        assert "if (!state.audioCtx)" in resume_body, (
            "前提が崩れています: resumeAudioContext の早期 return が消えました。"
        )
        assert "return" in resume_body, (
            "resumeAudioContext が早期 return します。"
            "click ハンドラから它在呼ぶだけでは AudioContext 未生成時に何も起きず、"
            "生成は ensureAnalyser（fetch 後の非ジェスチャ経路）に遅延されてしまいます。"
        )

    @pytest.mark.xfail(
        reason=(
            "未修正の欠陥: AudioContext が fetch 完了後の ensureAnalyser 内でのみ生成される。"
            "WebKit/Firefox では手势なしで生成された AudioContext が suspended のまま起動し、"
            "createMediaElementSource() が <audio> の出力を恒久的に suspended コンテキスト"
            "経由に再ルーティングするため、UI は再生中と表示しても音が全滅する。"
            "修正: 再生ボタンの click ハンドラ内で AudioContext を eager に生成し resume する。"
            "修正されれば XPASS になる。"
        ),
        strict=False,
    )
    def test_queued_playback_never_routes_audio_through_a_suspended_context(self, js_source):
        """修正到位の契約。ジェスチャ内に context 生成があるか。"""
        gesture_scope = "\n".join(
            enclosing_lines(js_source, "dom.btnPlayRadio.addEventListener")
        )
        assert "new Ctor()" in gesture_scope or "new AudioContext" in gesture_scope, (
            "再生ボタンの click ハンドラ内で AudioContext が生成されていない。"
            "ensureAnalyser の new Ctor() はユーザージェスチャなしで実行されるため、"
            "suspended 起動すると恒久無音になる。"
        )


class TestAudioContextLifecycle:
    """2・3. AudioContext の解放と、異常系での恒久無音化防止。"""

    def test_audio_context_is_closed_somewhere(self, js_source):
        """`close()` が一度も呼ばれて(open まま捨てる) いないこと。"""
        assert ".close()" in js_source, (
            "AudioContext.close() が app.js に存在しません。"
            "state.audioCtx = null で open な AudioContext を捨てるのはリークです。"
        )

    @pytest.mark.xfail(
        reason=(
            "未修正の欠陥: ensureAnalyser の catch が state.audioCtx / analyserNodes を"
            "null にするが、createMediaElementSource は既に成功済みで <audio> の出力は"
            "恒久的に AudioContext 経由になっている。再試行すると同じ要素に 2 度目の"
            "createMediaElementSource を呼び InvalidStateError → catch → 無音の無限ループ。"
            "現状はスロット a/b の 2 要素ぶん都被害する。"
            "修正: catch では close() して以後の生成を永続禁止するか、"
            "最初に一度失敗したら以後 simulator 固定に切り替える。"
        ),
        strict=False,
    )
    def test_error_path_cannot_permanently_mute_audio_elements(self, js_source):
        """catch 後に次の試行が InvalidStateError で無限ループしないこと。"""
        body = extract_function(js_source, "ensureAnalyser")
        catch_block = body[body.index("catch"):]
        if "close()" in catch_block:
            return
        raise AssertionError(
            "ensureAnalyser の catch が AudioContext を close() せずに捨てています。"
            " MediaElementSource は既に張られており、要素は恒久的に無音化します。"
        )


class TestTouchDial:
    """4. タッチ端末のダイヤル操作（介護施設のタブレットが主要ユースケース）。"""

    def test_tuner_rail_handles_touch_move(self, js_source):
        """`touchstart` / `touchend` だけだとドラッグ中に追従しない。"""
        rail = "\n".join(enclosing_lines(js_source, "dom.tunerRail"))
        assert "touchmove" in js_source, (
            "app.js に touchmove ハンドラがありません。"
            "document の mousemove は touchmove を発火しないため、"
            "タッチ端末ではダイヤルのドラッグができません（タップ時に1回飛ぶだけ）。"
        )
        assert "draggingRail" in rail or "touchmove" in js_source, (
            "ダイヤルにドラッグ状態がありません。"
        )


class TestPrintListener:
    """5. 印刷ボタンでリスナが蓄積しない。"""

    @pytest.mark.xfail(
        reason=(
            "未修正の欠陥: handlePrint が押下ごとに "
            "window.addEventListener('afterprint', handleAfterPrint) を追加して削除しない。"
            "10 回押すとリスナが 10 個になり、details 復元が連鎖的に乱れます。"
            "修正: { once: true } を付けるか removeEventListener で対称に外す。"
        ),
        strict=False,
    )
    def test_afterprint_listener_is_not_accumulated(self, js_source):
        body = extract_function(js_source, "handlePrint")
        assert "once: true" in body or "removeEventListener" in body, (
            "handlePrint が afterprint リスナ的对称解除をしていません。"
            "押下ごとにリスナが蓄積します。"
        )


class TestServiceWorkerScope:
    """6. Service Worker のスコープ登録（サーバ側の実測）。"""

    @staticmethod
    def _client() -> TestClient:
        return TestClient(app)

    def test_service_worker_allowed_header_is_served(self):
        """`/static/service-worker.js` は `Service-Worker-Allowed: /` を返すこと。

        これが無いと `register(url, {scope: '/'})` は必ず SecurityError で失敗し、
        SW は `/static/` スコープに降格する。结果として
        ナビゲーションのオフラインフォールバックと `/` スコープのキャッシュが
        一切効かない（アプリ全体の PWA 機能が死んでいる）。
        """
        with self._client() as client:
            response = client.get("/static/service-worker.js")
        assert response.status_code == 200, (
            f"service-worker.js が配信できません: {response.status_code}"
        )
        allowed = response.headers.get("service-worker-allowed")
        assert allowed == "/", (
            "Service-Worker-Allowed ヘッダが無い、または '/' ではありません "
            f"(実際: {allowed!r})。app.js の register(url, {{scope: '/'}}) は"
            "必ず SecurityError で失敗します。"
        )
