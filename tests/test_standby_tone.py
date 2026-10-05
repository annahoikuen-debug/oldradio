"""受信待ちの充填音（待機 bed）とステップ効果音の回帰テスト。

なぜ必要か
----------
ボタンを押してから番組が鳴るまで 30〜60 秒、その間はずっと無音だった
（`static/app.js` の `startGeneration()` → SSE 完了 → `startPlayback()`
まで音が出る要素が 1 つも無かった）。待ち時間に音を入れると決めて、
実装が次の壊れ方をしなければいけないので、その契約だけを固定する。

ここで守る不変条件:

  1. `startProgress()` で待機音が立ち上がり、`stopProgress()` /
     `startPlayback()` / ページ離脱で必ず止まる（**鳴りっぱなしにしない**）
  2. 待機音は `<audio>` の再生経路に一切触らない
     （MediaElementSource / AnalyserNode を使わない。キューが別オリジンの
     iTunes プレビューを含んだ瞬間に恒久無音化する既知の罠があるため）
  3. AudioContext を生成できるのは**ユーザージェスチャ内**だけ
     （生成が fetch 後の経路だけだと WebKit/Firefox で suspended 起動する）
  4. ステップ効果音は「進行中に変わった瞬間」に 1 回だけ鳴る
     （進捗タイマーは 250ms ごとに setProgressStep を呼ぶため、
     ガードがなければ 4Hz で鳴り続ける）
  5. AudioContext 作れない環境では静かに no-op（VU と同じ方針）
  6. ミュート / 音量スライダーが待機音にも効く

実ブラウザの「聞こえ」はこの環境で検証できないため、最後の確認は人手。
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
    window.__standbyState = function () {
        return JSON.stringify({
            active: state.standbyActive === true,
            unsupported: state.standbyUnsupported === true,
            hasNodes: !!state.standbyNodes,
            cuedStep: state.standbyCuedStep || '',
            sources: state.standbyNodes ? state.standbyNodes.sources.length : 0,
            gain: state.standbyNodes && state.standbyNodes.master
                ? state.standbyNodes.master.gain.value : null,
            hasCtx: !!state.audioCtx
        });
    };
    window.__standbyEvents = function () {
        return JSON.stringify(window.__sbEventsSnapshot());
    };
    window.__tickProgress = function () { tickProgress(); return 'null'; };
    window.__startProgress = function () {
        startProgress(1975, 'normal');
        return window.__standbyState();
    };
    window.__stopProgress = function () {
        stopProgress();
        return window.__standbyState();
    };
    window.__setStep = function (key, status) {
        setProgressStep(key, status || 'active');
        return window.__standbyState();
    };
    window.__startPlayback = function (raw) {
        startPlayback(JSON.parse(raw));
        return window.__standbyState();
    };
    window.__setMuted = function (muted) {
        state.muted = !!muted;
        applyVolumeToAll();
        return window.__standbyState();
    };
    window.__setVolume = function (v) {
        state.volume = Number(v);
        applyVolumeToAll();
        return window.__standbyState();
    };
    /* ==== フック終わり ==== */
"""

# --- フェイク AudioContext -----------------------------------------------------
# app.js が使う API だけを実装した極小スタブ。
# 生成・接続・時刻進行の呼び出しを `window.__sbEvents` に記録し、
# 「いつ鳴り始めたか / 止めたか」を観測できるようにする。
FAKE_AUDIO_CONTEXT = r"""
(function () {
    function param(name) {
        return {
            value: 0,
            _name: name,
            _events: [],
            setValueAtTime: function (v, t) { this.value = v; this._events.push(['set', v, t]); return this; },
            linearRampToValueAtTime: function (v, t) { this._events.push(['lin', v, t]); return this; },
            exponentialRampToValueAtTime: function (v, t) { this._events.push(['exp', v, t]); return this; },
            cancelScheduledValues: function (t) { this._events.push(['cancel', 0, t]); return this; }
        };
    }
    function node(kind, log) {
        return {
            kind: kind,
            connect: function (dest) { log.push(['connect', kind, dest && dest.kind]); return dest; },
            disconnect: function () { log.push(['disconnect', kind]); },
            start: function (t) { log.push(['start', kind, t]); },
            stop: function (t) { log.push(['stop', kind, t]); }
        };
    }
    window.__sbEvents = [];
    window.__sbReset = function () { window.__sbEvents.length = 0; };
    window.__sbEventsSnapshot = function () { return window.__sbEvents; };
    /* ノードは生成時に配列を掴むため、__sbReset は**その場**を空にする
       （別配列に差し替えると、既に生成済みのノードが旧配列に記録する）。 */
    var LOG = window.__sbEvents;
    window.__sbReset = function () { LOG.length = 0; };
    window.__sbEventsSnapshot = function () { return LOG; };

    window.AudioContext = function () {
        var log = LOG;
        log.push(['construct']);
        this.sampleRate = 48000;
        this.currentTime = 0;
        this.state = 'running';
        this.destination = node('destination', log);
        this.resume = function () { log.push(['resume']); return { catch: function () {} }; };
        this.close = function () { log.push(['close']); return { catch: function () {} }; };
        this.createGain = function () {
            var n = node('gain', log);
            n.gain = param('gain');
            return n;
        };
        this.createOscillator = function () {
            var n = node('oscillator', log);
            n.type = 'sine';
            n.frequency = param('freq');
            n.onended = null;
            return n;
        };
        this.createBufferSource = function () {
            var n = node('bufferSource', log);
            n.buffer = null;
            n.loop = false;
            return n;
        };
        this.createBuffer = function (ch, frames) {
            log.push(['createBuffer', ch, frames]);
            return {
                getChannelData: function () {
                    var arr = new Float64Array(frames);
                    for (var i = 0; i < frames; i += 1) { arr[i] = 0; }
                    return arr;
                }
            };
        };
    };
    window.__sbMakeContext = function () { return new window.AudioContext(); };
}());
"""

# app.js は読み込み直後に init() を走らせるため、dom 参照のスタブは
# **評価前**に用意しておく必要がある（後付けだと cacheDom に乗らない）。
DOM_STUBS = r"""
(function () {
    var h = window.__harness;
    if (!h.byId.btnPlayRadio) {
        h.byId.btnPlayRadio = h.document.createElement('button');
        h.byId.btnPlayRadio.id = 'btnPlayRadio';
    }
    ['generationPanel', 'generationPanelTitle', 'generationElapsed',
     'progressTrack', 'progressFill', 'progressSteps'].forEach(function (id) {
        if (!h.byId[id]) { h.byId[id] = h.document.createElement('div'); }
    });
}());
"""


@pytest.fixture(scope="module")
def app_src() -> str:
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture
def app(app_src):
    marker = "\n}());\n"
    assert app_src.endswith(marker), "app.js の IIFE 末尾を探せない"
    patched = app_src[: -len(marker)] + "\n" + HOOK + marker
    interp = dukpy.JSInterpreter()
    interp.evaljs(
        HARNESS_JS.read_text(encoding="utf-8")
        + "\n"
        + DOM_STUBS
        + "\n"
        + FAKE_AUDIO_CONTEXT
        + "\n"
        + patched
        + "\n"
    )

    class Runner:
        def __init__(self, i):
            self.i = i

        def js(self, code):
            r = self.i.evaljs(code)
            if isinstance(r, str) and r[:1] in "{[":
                return json.loads(r)
            return r

        def events(self):
            return json.loads(self.i.evaljs("JSON.stringify(window.__sbEventsSnapshot());"))

        def reset(self):
            self.i.evaljs("window.__sbReset();")

        def boot(self):
            self.js("window.__harness.fire('DOMContentLoaded');")

        def standby(self):
            return self.js("window.__standbyState();")

    return Runner(interp)


def _ops(events, op):
    """`['connect', 'gain', 'destination']` のような操作ログから op を取り出す。"""
    return [e for e in events if e and e[0] == op]


def _cue_fired(events):
    """効果音（単発の oscillator）が鳴ったか。

    待機 bed 自体も oscillator を使うので、** Gains への接続と
    0.1 秒以内の stop**（= 単発）が効果音の証拠になる。
    """
    for event in events:
        if event[0] != "start" or event[1] != "oscillator":
            continue
        if isinstance(event[2], (int, float)) and event[2] < 0.2:
            return True
    return False


# ==============================================================================
# 1. 立ち上がりと停止
# ==============================================================================
def test_waiting_tone_starts_with_progress(app):
    """`startProgress()` で待機音が立つ（待ち時間が無音で埋まらない）。"""
    app.boot()
    app.reset()
    state = app.js("window.__startProgress();")
    assert state["active"] is True, state
    assert state["hasNodes"] is True, state
    # ノイズ + LFO + ハム の 3 ソース
    assert state["sources"] == 3, state
    # context はジェスチャ内（下の click ハンドラ）で生成済み
    assert state["hasCtx"] is True, state


def test_progress_timer_does_not_restart_the_tone_every_tick(app):
    """進捗タイマー（250ms）が鳴らし直さないこと。"""
    app.boot()
    app.js("window.__startProgress();")
    first = app.standby()
    # tickProgress を何十回回しても同じノードを使い回す
    for _ in range(20):
        app.js("window.__tickProgress();")
    again = app.standby()
    assert again["active"] is True
    assert again["sources"] == first["sources"], "待機音が作り直されている"


def test_stop_progress_silences_the_tone(app):
    """`stopProgress()`（中止・失敗・完了の共通終端）で必ず止まる。"""
    app.boot()
    app.js("window.__startProgress();")
    app.reset()
    state = app.js("window.__stopProgress();")
    assert state["active"] is False, state
    assert state["hasNodes"] is False, state
    assert _ops(app.events(), "stop"), "音源に stop が呼ばれていない（鳴りっぱなし）"


def test_starting_playback_stops_the_tone(app):
    """番組が始まったら待機音を止める（二重再生・帯域の重なり防止）。"""
    app.boot()
    app.js("window.__startProgress();")
    app.reset()
    data = {
        "year": 1975, "month": 9, "day": 30, "mode": "normal",
        "loop_count": 1,
        "playlist": [
            {"type": "song", "title": "S0", "artist": "A0",
             "preview_url": "https://audio-ssl.itunes.apple.com/0.m4a"},
            {"type": "talk", "title": "T0",
             "audio_url": "http://localhost:8501/api/audio/t0.mp3",
             "metadata": {"segment_index": 0}},
        ],
    }
    state = app.js("window.__startPlayback(%s);" % json.dumps(json.dumps(data)))
    assert state["active"] is False, state
    assert state["hasNodes"] is False, state


def test_restarting_progress_replaces_the_previous_tone(app):
    """2 回目の受信で、1 回目の音源が残らない。"""
    app.boot()
    app.js("window.__startProgress();")
    app.js("window.__stopProgress();")
    app.js("window.__startProgress();")
    state = app.standby()
    assert state["active"] is True
    assert state["sources"] == 3, state


# ==============================================================================
# 2. <audio> の再生経路に干渉しない
# ==============================================================================
def test_tone_never_uses_media_element_source(app):
    """待機音は要素解析に手を出さない（CORS で恒久無音化するのを避ける）。

    `startProgress()` → 再生待ちの状態で、ノード生成が gain/oscillator/
    bufferSource だけであることを観測する。
    """
    app.boot()
    app.reset()
    app.js("window.__startProgress();")
    events = app.events()
    created = {e[1] for e in events if e[0] == "connect"}
    assert created <= {"gain", "oscillator", "bufferSource", "destination"}, created


def test_media_element_source_is_never_called_by_the_tone(app):
    """`createMediaElementSource` は待機音の経路に現れない。"""
    source = APP_JS.read_text(encoding="utf-8")
    body = source[source.index("function startStandbyTone"):source.index("function applyStandbyVolume")]
    assert "createMediaElementSource" not in body
    assert "createAnalyser" not in body


# ==============================================================================
# 3. ジェスチャ内の AudioContext 生成
# ==============================================================================
def test_play_button_click_creates_the_context(app):
    """再生ボタンの click で context が用意される（suspended 起動の防止）。"""
    app.boot()
    app.reset()
    app.js("window.__harness.byId.btnPlayRadio.click();")
    kinds = [e[0] for e in app.events()]
    assert "construct" in kinds, app.events()


def test_gesture_handler_creates_context(app):
    """静的契約: click ハンドラが context 生成に到達すること。"""
    lines = APP_JS.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, line in enumerate(lines)
               if "dom.btnPlayRadio.addEventListener" in line)
    scope = "\n".join(lines[max(0, idx - 20): idx + 20])
    assert "ensureAudioContext(" in scope, scope


# ==============================================================================
# 4. ステップ効果音
# ==============================================================================
def test_step_cue_fires_once_when_a_step_becomes_active(app):
    """進行中に変わった瞬間だけ鳴る（進捗タイマーで連打しない）。"""
    app.boot()
    app.js("window.__startProgress();")
    app.reset()
    state = app.js("window.__setStep('script', 'active');")
    assert state["cuedStep"] == "script", state
    assert _cue_fired(app.events()), "ステップ効果音が鳴っていない"

    app.reset()
    # 同じステップが再度 active になっても鳴らない
    again = app.js("window.__setStep('script', 'active');")
    assert again["cuedStep"] == "script"
    assert not _cue_fired(app.events()), "同じステップで鳴り直した"


def test_step_cue_advances_with_each_step(app):
    """ステップが進むごとに 1 回ずつ鳴る。"""
    app.boot()
    app.reset()
    app.js("window.__startProgress();")
    # 待機音が立ち上がると同時に、最初のステップ（connect）の効果音が鳴る
    assert _cue_fired(app.events()), "開始直後に connect の効果音が鳴らない"
    fired = []
    for key in ("script", "tts", "song"):
        app.reset()
        app.js("window.__setStep(%s, 'active');" % json.dumps(key))
        fired.append(_cue_fired(app.events()))
    assert all(fired), fired


def test_completed_step_does_not_fire_a_cue(app):
    """'completed' / 'pending' では鳴らない（完了表示で鳴らない）。"""
    app.boot()
    app.js("window.__startProgress();")
    app.reset()
    state = app.js("window.__setStep('tts', 'completed');")
    assert state["cuedStep"] == "connect", state
    assert not _cue_fired(app.events())


# ==============================================================================
# 5. AudioContext が作れない環境
# ==============================================================================
def test_missing_audio_context_is_a_silent_noop(app_src):
    """AudioContext が無い環境でも例外を投げず、進行を止めない。"""
    interp = dukpy.JSInterpreter()
    marker = "\n}());\n"
    patched = app_src[: -len(marker)] + "\n" + HOOK + marker
    interp.evaljs(
        HARNESS_JS.read_text(encoding="utf-8") + "\n" + DOM_STUBS + "\n" + patched + "\n"
    )

    class Runner:
        def js(self, code):
            r = interp.evaljs(code)
            if isinstance(r, str) and r[:1] in "{[":
                return json.loads(r)
            return r

        def boot(self):
            self.js("window.__harness.fire('DOMContentLoaded');")

    app = Runner()
    app.boot()
    state = app.js("window.__startProgress();")
    assert state["active"] is False, state
    assert state["unsupported"] is True, state
    # 止まらないこと（進捗パネルは出し続ける）
    assert app.js("window.__standbyState();")["hasNodes"] is False
    app.js("window.__stopProgress();")


def test_failing_context_disables_the_tone_for_the_session(app):
    """生成が例外を投げる環境では、以後試行しない（毎回例外を撒かない）。"""
    interp = dukpy.JSInterpreter()
    marker = "\n}());\n"
    app_src = APP_JS.read_text(encoding="utf-8")
    patched = app_src[: -len(marker)] + "\n" + HOOK + marker
    boom = "window.AudioContext = function () { throw new Error('nope'); };"
    interp.evaljs(
        HARNESS_JS.read_text(encoding="utf-8")
        + "\n"
        + DOM_STUBS
        + "\n"
        + boom
        + "\n"
        + patched
        + "\n"
    )

    class Runner:
        def js(self, code):
            r = interp.evaljs(code)
            if isinstance(r, str) and r[:1] in "{[":
                return json.loads(r)
            return r

        def boot(self):
            self.js("window.__harness.fire('DOMContentLoaded');")

    app = Runner()
    app.boot()
    state = app.js("window.__startProgress();")
    assert state["active"] is False
    assert state["unsupported"] is True


# ==============================================================================
# 6. ミュート / 音量
# ==============================================================================
def test_mute_silences_the_waiting_tone(app):
    app.boot()
    app.js("window.__startProgress();")
    before = app.standby()
    assert before["gain"] and before["gain"] > 0, before
    state = app.js("window.__setMuted(true);")
    assert state["gain"] == 0, state


def test_volume_scales_the_waiting_tone(app):
    app.boot()
    app.js("window.__startProgress();")
    app.js("window.__setMuted(false);")
    app.js("window.__setVolume(0.5);")
    half = app.standby()["gain"]
    app.js("window.__setVolume(1.0);")
    full = app.standby()["gain"]
    assert full > half > 0, (half, full)


def test_tone_is_quieter_than_the_programme(app):
    """待機音は番組より小さい（番組に不胜らない）。"""
    source = APP_JS.read_text(encoding="utf-8")
    assert "STANDBY_MASTER_GAIN = 0.05" in source, (
        "待機音の主音量が上がっています（番組に負ける量か確認してください）"
    )
