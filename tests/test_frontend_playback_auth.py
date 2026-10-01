"""static/app.js の**認証表示 / エラー分類 / ジョブ(SSE) 後始末**を検証する。

tests/test_frontend_playback.py が再生ステートマシンを検証するのに対し、
ここでは「サーバーが何もできない状態で、どのエラー表示と一連の操作が成立するか」を検証する。

1. `/health` の `auth_enforced` / `auth_ready` / `auth_mode` ごとに
   状態表示が「嘘の緑」にならないこと
2. 401 / 403 / 503（混雑・資格情報・APIキー）の文言が原因ごとに違うこと
3. 受信成功後に `#generationPanel`（=「⏹ 受信を中止する」）が隠れること
4. 中止時に EventSource が閉じ、`DELETE /api/jobs/{id}` が飛ぶこと

app.js は IIFE なので、内部関数を見るために末尾にフックを注入する
（tests/test_frontend_playback.py と同じ手法）。
"""
import json
from pathlib import Path

import pytest

esprima = pytest.importorskip("esprima", reason="esprima がないため構文検査をスキップ")
dukpy = pytest.importorskip("dukpy", reason="dukpy がないため JS 実行テストをスキップ")

TESTS_DIR = Path(__file__).resolve().parent
APP_JS = TESTS_DIR.parent / "static" / "app.js"
HARNESS_JS = TESTS_DIR / "js_harness.js"

# --- app.js 内部をテストから覗くためのフック -----------------------------------
HOOK = r"""
    /* ==== テスト専用フック ==== */
    window.__describeHealth = function (raw) {
        return JSON.stringify(describeHealth(JSON.parse(raw)));
    };
    window.__applyHealth = function (raw) {
        var view = applyHealth(JSON.parse(raw));
        return JSON.stringify({
            kind: view.kind,
            text: view.text,
            authNeeded: view.authNeeded,
            serviceText: dom.serviceStatus ? dom.serviceStatus.textContent : '',
            serviceClass: dom.serviceStatus ? dom.serviceStatus.className : '',
            authHidden: dom.authPanel ? !!dom.authPanel.hidden : null
        });
    };
    window.__describeError = function (status, rawDetail) {
        var payload = rawDetail ? { detail: rawDetail } : null;
        return JSON.stringify(describeError(status, '', payload, ''));
    };
    window.__renderSuccess = function (raw) {
        renderSuccess({ year: 1975, month: 9, day: 24, mode: 'normal', targetName: '' },
            JSON.parse(raw));
        return JSON.stringify({
            panelHidden: dom.generationPanel ? !!dom.generationPanel.hidden : null,
            trackCount: state.queue.length,
            programYear: state.programYear
        });
    };
    window.__panelHidden = function () {
        return !!(dom.generationPanel && dom.generationPanel.hidden);
    };
    window.__showProgress = function () {
        startProgress(1975, 'normal');
        return window.__panelHidden();
    };
    window.__showPanel = function () {
        dom.generationPanel.hidden = false;
    };
    window.__submitLogin = function (email, password) {
        if (dom.authEmail) { dom.authEmail.value = String(email || ''); }
        if (dom.authPassword) { dom.authPassword.value = String(password || ''); }
        submitLogin();
    };
    window.__startGeneration = function () {
        startGeneration();
        return JSON.stringify({ jobId: state.jobId, loading: state.isLoading });
    };
    window.__jobState = function () {
        return JSON.stringify({
            jobId: state.jobId,
            hasStream: !!state.jobStream,
            streamJob: state.jobStreamJob,
            lastSeq: state.jobLastSeq
        });
    };
    window.__cancel = function () {
        cancelGeneration(false);
        return JSON.stringify({ jobId: state.jobId, hasStream: !!state.jobStream });
    };
    window.__applyConsentGate = function (declined) {
        applyConsentGate(!!declined);
        return 'null';
    };
    window.__consentState = function () {
        var b = dom.btnPlayRadio;
        if (!b) { return 'null'; }
        return JSON.stringify({
            disabled: !!b.disabled,
            ariaDisabled: b.getAttribute('aria-disabled'),
            title: b.getAttribute('title') || '',
            declined: state.consentDeclined === true,
            recorded: state.consentRecorded === true
        });
    };
    /* app.js は IIFE なので、内部関数を外から呼ぶにはフックが必要。 */
    window.__recordConsent = function (accepted) {
        recordConsent(!!accepted);
        return 'null';
    };
    window.__checkConsentOnStartup = function () {
        checkConsentOnStartup();
        return 'null';
    };
    window.__setLoading = function (loading) {
        setLoading(!!loading);
        return 'null';
    };
    window.__fireEvent = function (name, data) {
        var stream = state.jobStream;
        if (!stream) { return false; }
        var handlers = stream.__listeners[name] || [];
        for (var i = 0; i < handlers.length; i += 1) {
            handlers[i]({ data: JSON.stringify(data || {}), lastEventId: '' });
        }
        return true;
    };
    window.__steps = function () {
        return JSON.stringify({ eventsSeen: state.jobEventsSeen, percent: state.progressPercent });
    };
    /* ==== フック終わり ==== */
"""

# --- 実行前に差し込むネットワーク / EventSource スタブ --------------------------
# app.js の init() は読み込み直後に走るため、スタブは app.js より**前**に評価する。
STUBS = r"""
(function () {
    var h = window.__harness;

    // js_harness の REQUIRED リストに無い ID について dom 参照を用意する。
    ['generationPanel', 'generationPanelTitle', 'generationElapsed',
     'progressTrack', 'progressFill', 'progressSteps', 'authPanel',
     'authEmail', 'authPassword', 'btnAuthLogin', 'authStatus',
     'modePanel', 'consentDialog', 'consentStatus'].forEach(function (id) {
        if (!h.byId[id]) { h.byId[id] = h.document.createElement('div'); }
    });
    /* 同意ゲートが操作する**ボタン**。`disabled` と `aria-disabled` を
       観測できる必要があるため `<button>` 相当で作る（div だと
       disabled が意味を持たない）。 */
    ['btnPlayRadio', 'btnEmptyPlay', 'btnRetry'].forEach(function (id) {
        h.byId[id] = h.document.createElement('button');
        h.byId[id].id = id;
    });
    h.byId.generationPanel.hidden = true;
    h.byId.authPanel.hidden = true;
    h.byId.progressSteps.querySelectorAll = function () { return []; };

    function mk(status, body) {
        var text = (body === null || body === undefined) ? '' : JSON.stringify(body);
        return {
            ok: status >= 200 && status < 300,
            status: status,
            statusText: 'S' + status,
            text: function () { return Promise.resolve(text); },
            json: function () { return Promise.resolve(JSON.parse(text)); }
        };
    }

    window.__mk = mk;
    window.__routes = [];
    window.__fetchLog = [];
    window.fetch = function (url, options) {
        var opts = options || {};
        var method = String(opts.method || 'GET').toUpperCase();
        var target = String(url);
        window.__fetchLog.push({ url: target, method: method, body: opts.body || null });
        for (var i = 0; i < window.__routes.length; i += 1) {
            var route = window.__routes[i];
            if (route.method === method && target.indexOf(route.match) === 0) {
                return Promise.resolve(mk(route.status, route.body));
            }
        }
        return Promise.resolve(mk(404, { detail: 'no route' }));
    };

    // EventSource の最小スタブ。close() されたかを観測する。
    window.__streams = [];
    window.EventSource = function (url) {
        var self = this;
        this.url = String(url);
        this.closed = false;
        this.onerror = null;
        this.__listeners = {};
        this.addEventListener = function (name, fn) {
            (self.__listeners[name] = self.__listeners[name] || []).push(fn);
        };
        this.close = function () { self.closed = true; };
        window.__streams.push(this);
    };
})();
"""


@pytest.fixture(scope="module")
def app_src() -> str:
    return APP_JS.read_text(encoding="utf-8")


class Runner:
    """app.js を dukpy で走らせるための薄いラッパ。"""

    def __init__(self, interp):
        self.i = interp

    def js(self, code):
        result = self.i.evaljs(code)
        if isinstance(result, str) and result[:1] in "{[":
            return json.loads(result)
        return result

    def flush(self):
        """保留中の Promise を消化させる（1 回評価するごとにジョブキューが回る）"""
        self.i.evaljs("void 0;")

    def route(self, method, match, status, body=None):
        self.js("window.__routes.push({ method: %s, match: %s, status: %d, body: %s });"
                % (json.dumps(method), json.dumps(match), status, json.dumps(body)))

    def fetch_log(self):
        return self.js("JSON.stringify(window.__fetchLog)")


@pytest.fixture
def app(app_src):
    marker = "\n}());\n"
    assert app_src.endswith(marker), "app.js の IIFE 末尾を探せない"
    patched = app_src[: -len(marker)] + "\n" + HOOK + marker
    interp = dukpy.JSInterpreter()
    interp.evaljs(HARNESS_JS.read_text(encoding="utf-8"))
    interp.evaljs(STUBS)
    interp.evaljs(patched)
    return Runner(interp)


# ==============================================================================
# 0. 構文
# ==============================================================================
def test_app_js_parses(app_src):
    esprima.parseScript(app_src, tolerant=False)


# ==============================================================================
# 1. /health の認証状態ごとの表示
# ==============================================================================
def test_health_ok_when_auth_is_disabled(app):
    view = app.js("window.__describeHealth(" + json.dumps(json.dumps({
        "status": "healthy", "version": "1.0.0", "api_key_configured": True,
        "secret_key_configured": True, "auth_required": False,
        "auth_ready": True, "auth_mode": "disabled", "auth_enforced": False,
    })) + ");")
    assert view["kind"] == "ok"
    assert view["authNeeded"] is False
    assert "ログイン" not in view["text"]


def test_health_warns_when_auth_enforced_but_not_ready(app):
    """/health が緑でも、認証の資格情報が未設定なら緑にしてはいけない"""
    view = app.js("window.__describeHealth(" + json.dumps(json.dumps({
        "status": "healthy", "api_key_configured": True,
        "secret_key_configured": False, "auth_required": True,
        "auth_ready": False, "auth_mode": "session", "auth_enforced": True,
    })) + ");")
    assert view["kind"] == "error", view
    assert "503" in view["text"], view
    assert "RETRO_RADIO_SECRET_KEY" in view["text"], view


def test_health_asks_for_login_when_anonymous(app):
    view = app.js("window.__describeHealth(" + json.dumps(json.dumps({
        "status": "healthy", "api_key_configured": True,
        "auth_required": True, "auth_ready": True,
        "auth_mode": "anonymous", "auth_enforced": True,
    })) + ");")
    assert view["kind"] == "warn", view
    assert view["authNeeded"] is True
    assert "ログイン" in view["text"]


def test_health_warns_when_api_key_missing(app):
    view = app.js("window.__describeHealth(" + json.dumps(json.dumps({
        "status": "degraded", "api_key_configured": False,
        "auth_required": False, "auth_ready": True,
        "auth_mode": "disabled", "auth_enforced": False,
    })) + ");")
    assert view["kind"] == "warn", view
    assert "RETRO_RADIO_GEMINI_API_KEY" in view["text"]


def test_login_panel_only_shows_when_auth_enforced(app):
    app.js("window.__applyHealth(%s);" % json.dumps(json.dumps({
        "status": "healthy", "api_key_configured": True,
        "auth_required": False, "auth_enforced": False,
        "auth_ready": True, "auth_mode": "disabled",
    })))
    out = app.js("window.__applyHealth(%s);" % json.dumps(json.dumps({
        "status": "healthy", "api_key_configured": True,
        "auth_required": True, "auth_enforced": True,
        "auth_ready": True, "auth_mode": "anonymous",
    })))
    assert out["authHidden"] is False, out
    out = app.js("window.__applyHealth(%s);" % json.dumps(json.dumps({
        "status": "healthy", "api_key_configured": True,
        "auth_required": False, "auth_enforced": False,
        "auth_ready": True, "auth_mode": "disabled",
    })))
    assert out["authHidden"] is True, out


def test_login_404_degrades_to_no_login(app):
    """旧バックエンド（/api/auth/session が無い）ではログイン枠を隠す"""
    app.js("window.__routes.push({ method: 'POST', match: '/api/auth/session',"
           " status: 404, body: { detail: 'Not Found' } });")
    app.js("window.__submitLogin('a@example.com', 'secret');")
    app.flush()
    out = app.js("JSON.stringify({"
                 " hidden: !!__harness.byId.authPanel.hidden,"
                 " status: __harness.byId.authStatus.textContent });")
    assert out["hidden"] is True, out


# ==============================================================================
# 2. 401 / 403 / 503 の分類
# ==============================================================================
def test_401_says_authentication_is_required(app):
    msg = app.js("window.__describeError(401, '認証が必要です');")
    assert "認証が必要" in msg
    assert "ログイン" in msg
    assert "RETRO_RADIO_GEMINI_API_KEY" not in msg


def test_403_says_account_is_deleted(app):
    msg = app.js("window.__describeError(403, 'このアカウントは削除済みです');")
    assert "削除" in msg
    assert "ログイン" not in msg
    assert "RETRO_RADIO_GEMINI_API_KEY" not in msg


def test_503_concurrency_is_not_reported_as_api_key_problem(app):
    msg = app.js("window.__describeError(503, '混雑しています。しばらく待ってから再度お試しください。');")
    assert "混雑" in msg, msg
    assert "RETRO_RADIO_GEMINI_API_KEY" not in msg


def test_503_missing_credentials_is_not_reported_as_api_key_problem(app):
    msg = app.js("window.__describeError(503, '認証の資格情報が未設定です');")
    assert "RETRO_RADIO_SECRET_KEY" in msg, msg
    assert "混雑" not in msg
    assert "RETRO_RADIO_GEMINI_API_KEY" not in msg


def test_503_api_key_still_mentions_gemini_key(app):
    msg = app.js("window.__describeError(503, 'Gemini APIキーが設定されていません');")
    assert "RETRO_RADIO_GEMINI_API_KEY" in msg, msg


def test_503_without_detail_is_generic(app):
    msg = app.js("window.__describeError(503, '');")
    assert "503" in msg
    assert "RETRO_RADIO_GEMINI_API_KEY" not in msg


def test_422_explains_the_actual_cause(app):
    msg = app.js("window.__describeError(422, 'body -> year: Input should be less than or equal to 2025');")
    assert "422" in msg


# ==============================================================================
# 3. 受信成功後は #generationPanel を隠す
# ==============================================================================
def test_generation_panel_is_hidden_after_success(app):
    assert app.js("window.__showProgress();") is False, "受信パネルが表示されていない"
    app.js("window.__showPanel();")
    app.js("window.__renderSuccess(%s);" % json.dumps(json.dumps({
        "year": 1975, "month": 9, "day": 24, "mode": "normal",
        "script": "### オープニング\nテスト。\n",
        "segments": [{"order": 0, "title": "オープニング", "content": "テスト。"}],
        "songs": [{"title": "S", "artist": "A", "preview_url": "http://localhost:8501/api/audio/s.mp3"}],
        "playlist": [],
        "passes": [],
        "loop_count": 3,
    })))
    out = app.js("JSON.stringify({ hidden: __harness.byId.generationPanel.hidden });")
    assert out["hidden"] is True, "#generationPanel が放送中も表示されたまま"


# ==============================================================================
# 4. ジョブ(SSE) の後始末
# ==============================================================================
def _stub_job(app, job_id="job-1"):
    app.route("POST", "/api/jobs", 202, {"job_id": job_id, "estimated_ms": 1000,
                                         "poll_after_ms": 1500, "state": "queued",
                                         "events_url": "/api/jobs/%s/events" % job_id})


def test_job_request_opens_exactly_one_stream(app):
    _stub_job(app)
    app.js("window.__startGeneration();")
    app.flush()
    st = app.js("window.__jobState();")
    assert st["jobId"] == "job-1", st
    assert st["hasStream"] is True, st
    assert app.js("window.__streams.length;") == 1
    post = [r for r in app.fetch_log() if r["method"] == "POST" and "/api/jobs" in r["url"]]
    assert len(post) == 1, app.fetch_log()


def test_sse_events_drive_the_progress_steps(app):
    _stub_job(app)
    app.js("window.__startGeneration();")
    app.flush()
    assert app.js("window.__fireEvent('script.started', { year: 1975 });") is True
    after = app.js("window.__steps();")
    assert after["eventsSeen"] is True, after
    assert after["percent"] > 0, after


def test_cancel_closes_the_stream_and_cancels_the_server_job(app):
    _stub_job(app)
    app.js("window.__startGeneration();")
    app.flush()
    assert app.js("window.__streams[0].closed;") is False

    st = app.js("window.__cancel();")
    assert st["jobId"] == "", st
    assert st["hasStream"] is False, st
    assert app.js("window.__streams[0].closed;") is True, "EventSource が閉じられていない"
    app.flush()
    deletes = [r for r in app.fetch_log() if r["method"] == "DELETE"]
    assert deletes, "DELETE /api/jobs/{id} が飛んでいない: %s" % app.fetch_log()
    assert deletes[0]["url"].endswith("/api/jobs/job-1"), deletes


def test_terminal_event_closes_the_stream(app):
    _stub_job(app)
    app.route("GET", "/api/jobs/job-1", 200, {
        "job_id": "job-1", "state": "succeeded", "events": [],
        "result": {"year": 1975, "month": 9, "day": 24, "mode": "normal",
                   "script": "### オープニング\nテスト。\n", "segments": [],
                   "songs": [], "playlist": [], "passes": [], "loop_count": 3},
        "error": None,
    })
    app.js("window.__startGeneration();")
    app.flush()
    app.js("window.__fireEvent('done', { playlist_len: 4 });")
    app.flush()
    st = app.js("window.__jobState();")
    assert st["hasStream"] is False, st
    assert app.js("window.__streams[0].closed;") is True
    assert app.js("window.__panelHidden();") is True


def test_a_new_generation_does_not_leak_the_previous_stream(app):
    _stub_job(app, "job-1")
    app.js("window.__startGeneration();")
    app.flush()
    app.js("window.__streams = [];")
    app.js("window.__startGeneration();")
    app.flush()
    st = app.js("window.__jobState();")
    assert st["hasStream"] is True
    # 新しい受信で 1 本だけ。前のストリームは閉じられている
    assert app.js("window.__streams.length;") == 1


def test_falls_back_to_sync_generate_when_jobs_api_is_missing(app):
    """旧バックエンド（/api/jobs が 404）では POST /api/generate に落ちる"""
    app.route("POST", "/api/jobs", 404, {"detail": "Not Found"})
    app.route("POST", "/api/generate", 200, {
        "year": 1975, "month": 9, "day": 24, "mode": "normal",
        "script": "### オープニング\nテスト。\n", "segments": [],
        "songs": [], "playlist": [], "passes": [], "loop_count": 3,
    })
    app.js("window.__startGeneration();")
    app.flush()
    urls = [r["url"] for r in app.fetch_log() if r["method"] == "POST"]
    assert "/api/generate" in urls, urls
    assert app.js("window.__streams.length;") == 0, "SSE を開いてはいけない"


# ==============================================================================
# 同意ゲート（P1-2）
# ==============================================================================
def _consent_state(app) -> dict:
    return app.js("window.__consentState();")


def test_consent_gate_can_be_unblocked_after_declining(app):
    """拒否 → 同意の順に操作すると、生成ボタンが**復活する**こと。

    かつては `button.disabled = blocked || button.disabled` だったため、
    blocked=false のとき `button.disabled` を自分自身へ代入するだけになり、
    拒否した直後に同意してもボタンが disabled のまま残っていた。
    しかも aria-disabled だけが外れていたので、支援技術は
       無効なボタンを「有効」と読み上げていた。復帰には再読み込みしか
       なかった。ここでは**両方向**を固定する。
    """
    app.js("window.__applyConsentGate(true);")
    blocked = _consent_state(app)
    assert blocked["disabled"] is True, blocked
    assert blocked["ariaDisabled"] == "true", blocked
    assert blocked["title"], "拒否時は理由=title で伝える"

    app.js("window.__applyConsentGate(false);")
    released = _consent_state(app)
    assert released["disabled"] is False, (
        "拒否 -> 同意でボタンが復活していない（ページ再読み込みしか解除できない）: "
        + str(released)
    )
    assert released["ariaDisabled"] is None, released
    assert not released["title"], released


def test_consent_gate_survives_a_loading_cycle(app):
    """同意ゲート解除の直後に「受信中」を入れても、状態が壊れないこと。

    `setLoading` と `applyConsentGate` は同じボタンを管理するため、
    どちらのフラグが勝つかで分岐しないことを固定する。
    """
    app.js("window.__applyConsentGate(true);")
    app.js("window.__setLoading(true);")
    assert _consent_state(app)["disabled"] is True
    app.js("window.__applyConsentGate(false);")
    assert _consent_state(app)["disabled"] is True, "受信中は有効化してはいけない"
    app.js("window.__setLoading(false);")
    assert _consent_state(app)["disabled"] is False, "受信終了後に復活しない"


def test_consent_recording_reports_failure_on_401(app):
    """**401 でも「同意を記録しました」と言わない**こと。

    `fetchJson` は非 2xx でも reject しない（`{ok, status, data}` を返す）
    ため、`.then()` の中で `res.ok` を見ないと 401 でも成功と表示していた。
    """
    app.route("POST", "/api/me/consent", 401, {"detail": "unauthorized"})
    app.js("window.__recordConsent(true);")
    app.flush()
    status = app.js("document.getElementById('consentStatus').textContent;")
    assert "同意を記録しました" not in status, status
    assert "認証" in status, status
    assert _consent_state(app)["recorded"] is False


def test_consent_startup_surfaces_an_auth_failure(app):
    """起動時の同意確認が 401 で**黙って成功しない**こと。

    状態表示も出なかった。`require_consent` が意味を持つのは認証有効な
    運用だけなので、認可エラー時は同意ゲートに到達できないのが通常だった。
    """
    app.route("GET", "/api/me/consent", 401, {"detail": "unauthorized"})
    app.js("window.__checkConsentOnStartup();")
    app.flush()
    status = app.js("document.getElementById('consentStatus').textContent;")
    assert status, "401 でも状態表示が一切出ない"
    assert "認証" in status, status
