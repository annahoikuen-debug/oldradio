"""static/app.js を**実際に JS エンジンで実行**して検証する回帰テスト。

なぜ必要か
----------
esprima による構文チェックは通ってしまうのに、実行時には
`ReferenceError` になり再生が丸ごと死ぬ関数が存在した。

実害: `buildPlaylistItem()` が未定義の `onPlaylistItemClick` を
`addEventListener` に渡したため `renderPlaylist()` が例外を投げ、
`startPlayback()` が中断し、`playIndex(0)` に到達しなかった。
結果として**何も鳴らないまま番組が止まる**という状态的征求了。

このファイルは dukpy（Duktape）にダミー DOM を食わせて
`startPlayback` から `onTrackEnded` までを一気通走で再現し、
「最後まで進むこと」と「1 つのイベント欠落で凍らないこと」を保証する。
"""
import io
import json
from pathlib import Path

import pytest

esprima = pytest.importorskip("esprima", reason="esprima がないため構文検査をスキップ")
dukpy = pytest.importorskip("dukpy", reason="dukpy がないため JS 実行テストをスキップ")

TESTS_DIR = Path(__file__).resolve().parent
APP_JS = TESTS_DIR.parent / "static" / "app.js"
HARNESS_JS = TESTS_DIR / "js_harness.js"

# --- 検証用に IIFE 内部へ差し込むフック ---------------------------------------
# app.js は IIFE なので、外部から内部関数を直接呼べない。
HOOK = r"""
    /* ==== テスト専用フック（js_harness からの観測用） ==== */
    window.__probe = function () {
        return JSON.stringify({
            index: state.index, len: state.queue.length, finished: state.finished,
            xfadeBusy: state.xfadeBusy, needGesture: state.needGesture,
            userPaused: state.userPaused, endedCount: state.endedCount,
            activeSlot: state.activeSlot,
            audioPaused: state.audio ? state.audio.paused : null,
            kinds: state.queue.map(function (t) { return t.kind; }),
            titles: state.queue.map(function (t) { return t.title; }),
        });
    };
    window.__start = function (d) { startPlayback(d); };
    window.__drive = function (i) { playIndex(i, false); };
    window.__tick = function () { updateSeekBar(); };
    window.__checkWatchdog = function () { checkWatchdog(); };
    window.__stall = function () { state.lastProgressAt = 0; };
    window.__actives = function () {
        return __harness.audioElements
            .filter(function (e) { return !e.paused; })
            .map(function (e) { return { slot: e.dataset.slot, err: !!e.error }; });
    };
    window.__advance = function (dur) {
        __harness.audioElements
            .filter(function (e) { return !e.paused; })
            .forEach(function (e) { e.duration = dur; e.currentTime = dur - 0.4; e.__advance(dur); });
    };
    window.__finish = function () {
        __harness.audioElements.filter(function (e) { return !e.paused; })
            .forEach(function (e) { e.__finish(); });
    };
    window.__drain = function () {
        // クロスフェードのタイマーは 30ms 程度、watchdog は 1000ms。
        // 後者は明細に干渉するので短周期のものだけ回す。
        for (var i = 0; i < 80; i += 1) {
            var short = __harness.timers.interval.filter(function (t) { return t.ms <= 60; });
            if (!short.length) { break; }
            short.forEach(function (t) { t.fn(); });
        }
    };
    window.__clickPlaylist = function (i) {
        var btn = __harness.byId('playlistList');
        return btn ? true : false;
    };
    /* ==== フック終わり ==== */
"""


def _response(talks: int = 5, songs: int = 6, loop: int = 3) -> str:
    """バックエンドが返す 1 パス分の playlist を模した JSON 文字列。"""
    return json.dumps(
        {
            "year": 1975,
            "month": 9,
            "day": 30,
            "mode": "normal",
            "script": "### オープニング\nテスト原稿。\n",
            "audio_url": "http://localhost:8501/api/audio/full.mp3",
            "songs": [
                {
                    "title": "S%d" % i,
                    "artist": "A%d" % i,
                    "preview_url": "https://audio-ssl.itunes.apple.com/%d.m4a" % i,
                }
                for i in range(3)
            ],
            "segments": [],
            "program_guide": {"date": "1975-09-30", "schedules": []},
            "playlist": (
                [{"type": "song", "title": "S%d" % i, "artist": "A%d" % i,
                  "preview_url": "https://audio-ssl.itunes.apple.com/%d.m4a" % i}
                 for i in range(songs)]
            )[:0] or _interleave(talks, songs),
            "loop_count": loop,
        },
        ensure_ascii=False,
    )


def _interleave(talks: int, songs: int) -> list:
    out, si = [], 0
    for i in range(talks):
        out.append({"type": "song", "title": "S%d" % si, "artist": "A%d" % si,
                    "preview_url": "https://audio-ssl.itunes.apple.com/%d.m4a" % si})
        si += 1
        out.append({
            "type": "talk", "title": "T%d" % i,
            "audio_url": "http://localhost:8501/api/audio/t%d.mp3" % i,
            "metadata": {"audio_url": "http://localhost:8501/api/audio/t%d.mp3" % i,
                         "segment_index": i},
        })
    out.append({"type": "song", "title": "S%d" % si, "artist": "A%d" % si,
                "preview_url": "https://audio-ssl.itunes.apple.com/%d.m4a" % si})
    return out


@pytest.fixture(scope="module")
def app_src() -> str:
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture
def app(app_src):
    """app.js を実行できるdukpy インタプリタ（フック注入済み）を返す。"""
    marker = "\n}());\n"
    assert app_src.endswith(marker), "app.js の IIFE 末尾を探せない"
    patched = app_src[: -len(marker)] + "\n" + HOOK + marker
    interp = dukpy.JSInterpreter()
    interp.evaljs(HARNESS_JS.read_text(encoding="utf-8") + "\n" + patched + "\n")

    class Runner:
        def __init__(self, i):
            self.i = i

        def js(self, code):
            r = self.i.evaljs(code)
            if isinstance(r, str) and r[:1] in "{[":
                return json.loads(r)
            return r

        def boot(self, **kwargs):
            self.js("window.__harness.fire('DOMContentLoaded');")
            self.js("window.__DATA = %s;" % _response(**kwargs))
            self.js("window.__start(window.__DATA);")
            return self.probe()

        def probe(self):
            return self.js("window.__probe()")

        def one_track(self, duration=10.0):
            """1 トラックを最後まで再生して次へ進める。"""
            self.js("window.__advance(%f);" % duration)
            self.js("window.__tick();")
            self.js("window.__advance(%f);" % duration)
            self.js("window.__drain();")
            self.js("window.__finish();")
            return self.probe()

    return Runner(interp)


# ==============================================================================
# 1. 構文と「未定義識別子」の静的検出
# ==============================================================================
def test_app_js_parses(app_src):
    esprima.parseScript(app_src, tolerant=False)


def _declared_and_referenced(tree):
    skip = {"type", "loc", "range", "raw", "regex", "operator", "kind",
            "computed", "optional", "shorthand", "prefix"}

    def children(node):
        out = []
        for key in dir(node):
            if key.startswith("_") or key in skip:
                continue
            v = getattr(node, key, None)
            if hasattr(v, "type"):
                out.append(v)
            elif isinstance(v, list):
                out.extend(x for x in v if hasattr(x, "type"))
        return out

    declared, referenced, props = set(), {}, set()

    def scan(node):
        t = getattr(node, "type", None)
        if t == "FunctionDeclaration" and getattr(node, "id", None):
            declared.add(node.id.name)
        if t in ("FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"):
            for p in getattr(node, "params", []) or []:
                if getattr(p, "type", None) == "Identifier":
                    declared.add(p.name)
                elif getattr(p, "type", None) == "AssignmentPattern" and \
                        getattr(getattr(p, "left", None), "type", None) == "Identifier":
                    declared.add(p.left.name)
        if t == "VariableDeclarator" and getattr(getattr(node, "id", None), "name", None):
            declared.add(node.id.name)
        if t == "CatchClause":
            par = getattr(node, "param", None)
            if par is not None and getattr(par, "type", None) == "Identifier":
                declared.add(par.name)
        if t in ("MemberExpression", "Property") and not node.computed:
            key = getattr(node, "property", None) or getattr(node, "key", None)
            if key is not None and getattr(key, "type", None) == "Identifier":
                props.add(key.name)
        if t == "Identifier":
            name = node.name
            referenced.setdefault(name, node.loc.start.line if node.loc else 0)
        for c in children(node):
            scan(c)

    scan(tree)
    return declared, referenced, props


BUILTINS = {
    "String", "Number", "Boolean", "Array", "Object", "JSON", "Math", "Date",
    "RegExp", "Error", "TypeError", "RangeError", "SyntaxError", "Promise",
    "Map", "Set", "WeakMap", "WeakSet", "Proxy", "Reflect", "Symbol",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI", "setTimeout", "clearTimeout",
    "setInterval", "clearInterval", "requestAnimationFrame", "cancelAnimationFrame",
    "fetch", "alert", "confirm", "prompt", "Audio", "AudioContext",
    "webkitAudioContext", "Blob", "URL", "FormData", "DataView", "ArrayBuffer",
    "Uint8Array", "Int8Array", "TextDecoder", "TextEncoder", "Headers",
    "Request", "Response", "AbortController", "FileReader", "Image", "File",
    "performance", "navigator", "document", "window", "console", "self",
    "Function", "eval", "undefined", "NaN", "Infinity", "globalThis",
    "IntersectionObserver", "MutationObserver", "matchMedia", "getComputedStyle",
    "Event", "EventTarget", "CustomEvent", "queueMicrotask", "CSS",
}


def test_no_undefined_identifier_references(app_src):
    """実行時 ReferenceError になる「未定義の識別子参照」が無いこと

    これが無いと `addEventListener('click', 未定義関数)` のような箇所で
    構文は通るが実行時に例外が投げられ、機能ごと死ぬ。
    """
    tree = esprima.parseScript(app_src, tolerant=False)
    declared, referenced, props = _declared_and_referenced(tree)
    missing = {
        name: line for name, line in referenced.items()
        if name not in declared and name not in BUILTINS and name not in props
    }
    assert not missing, "未定義の識別子参照（実行時 ReferenceError）: %s" % missing


def test_playlist_click_handler_is_defined(app_src):
    """`onPlaylistItemClick` は定義済み（未定義だと番組開始時に落ちる）"""
    assert "function onPlaylistItemClick" in app_src
    assert "button.addEventListener('click', onPlaylistItemClick);" in app_src


# ==============================================================================
# 2. 番組が最後まで進むこと
# ==============================================================================
def test_queue_has_radio_structure(app):
    """1 パスが「曲で始まり曲で終わる・トークが連続しない」形になる"""
    st = app.boot()
    kinds = st["kinds"]
    per = len(kinds) // 3
    one = kinds[:per]
    assert one[0] == "SONG", one[:3]
    assert one[-1] == "SONG", one[-3:]
    assert not any(one[i] == one[i + 1] == "TALK" for i in range(len(one) - 1)), one
    assert one.count("TALK") == 5, one


def test_playback_reaches_the_end_of_the_program(app):
    """全 3 周のトラックを最後まで再生できる（途中で止まらない）"""
    st = app.boot()
    total = st["len"]
    last = st["index"]
    for _ in range(total + 10):
        if st["finished"]:
            break
        st = app.one_track()
        assert st["index"] != last, "index が進んだ: %s" % st
        last = st["index"]
    assert st["finished"], "番組が終了しなかった: %s" % st
    assert st["index"] == total, st


# ==============================================================================
# 3. イベントが欠落しても凍らないこと
# ==============================================================================
def test_watchdog_recovers_when_ended_never_fires(app):
    """`ended` が来なくても watchdog が次のトラックへ進める"""
    st = app.boot()
    st = app.one_track()
    before = st["index"]

    # 再生位置を一切進めずに watchdog を複数回回す
    for _ in range(10):
        app.js("window.__stall();")
        app.js("window.__checkWatchdog();")

    st = app.probe()
    assert st["index"] > before, "watchdog が止まったキューを進めなかった: %s" % st
    assert st["xfadeBusy"] is False, st


def test_watchdog_keeps_advancing_until_the_end(app):
    """watchdog だけで最終トラックまで到達できる"""
    st = app.boot()
    total = st["len"]
    for _ in range(total + 20):
        if st["finished"]:
            break
        app.js("window.__stall();")
        app.js("window.__checkWatchdog();")
        st = app.probe()
    assert st["finished"], "watchdog だけでは番組が終わらなかった: %s" % st


def test_broken_crossfade_target_does_not_freeze_the_queue(app):
    """クロスフェード先が壊れていてもキューが凍らない"""
    app.boot()
    st = app.one_track()
    st = app.one_track()
    before = st["index"]

    # 残りが少ない situation を作ってフェードを起動させる
    app.js("window.__advance(9.7);")
    app.js("window.__tick();")
    app.js("window.__drain();")

    # 待機側の要素を読み込み失敗させる
    broke = app.js("""
    (function () {
        var idle = __harness.audioElements.filter(function (e) { return e.paused; })[0];
        if (!idle) { return false; }
        idle.src = 'http://localhost:8501/api/audio/DEAD.mp3';
        idle.__fail();
        return true;
    })()
    """)
    assert broke, "待機側の要素が見つからない"

    app.js("window.__drain();")
    st = app.probe()

    # フェードは打ち切られ、xfadeBusy が残ってはいない
    assert st["xfadeBusy"] is False, "xfadeBusy が残り続けて凍結する: %s" % st

    # その後もキューが進む
    for _ in range(10):
        app.js("window.__stall();")
        app.js("window.__checkWatchdog();")
        st = app.probe()
        if st["index"] > before or st["finished"]:
            break
    assert st["index"] > before or st["finished"], " programme が凍結した: %s" % st


def test_xfade_state_never_stays_busy(app):
    """クロスフェードを実行したあとも xfadeBusy が残り続けない"""
    app.boot()
    app.one_track()
    app.js("window.__advance(9.7);")
    app.js("window.__tick();")
    app.js("window.__drain();")
    st = app.probe()
    assert st["xfadeBusy"] is False, st
    fades = app.js("JSON.stringify(window.__harness.timers.interval"
                   ".filter(function (t) { return t.ms <= 60; }))")
    assert fades == [], "フェードタイマーが残って無限ループする"
