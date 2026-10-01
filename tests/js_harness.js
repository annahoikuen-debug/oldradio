// app.js をブラウザなしで走らせるためのダミー環境。
// dukpy に読み込ませて再生ステートマシンを検証する。
var __harness = (function () {
    var audioElements = [];
    var timers = { timeout: [], interval: [] };
    var nextTimerId = 1;
    var listeners = {};   // DOMContentLoaded など

    function makeClassList(el) {
        var set = {};
        return {
            add: function () {
                for (var i = 0; i < arguments.length; i += 1) { set[arguments[i]] = true; }
                sync(el);
            },
            remove: function () {
                for (var i = 0; i < arguments.length; i += 1) { delete set[arguments[i]]; }
                sync(el);
            },
            contains: function (c) { return !!set[c]; },
            toggle: function (c, on) {
                if (on === undefined) { on = !set[c]; }
                if (on) { set[c] = true; } else { delete set[c]; }
                sync(el);
                return on;
            },
            _set: set,
        };
    }

    function sync(el) { /* 視覚状態は検証しない */ }

    function makeElement(tag) {
        var el = {
            tagName: String(tag).toUpperCase(),
            children: [],
            parentNode: null,
            style: { cssText: '' },
            dataset: {},
            classList: null,
            _attrs: {},
            _text: '',
            _listeners: {},
        };
        el.classList = makeClassList(el);
        el.setAttribute = function (k, v) { el._attrs[k] = String(v); el.dataset[k.replace(/^data-/, '').replace(/-(\w)/g, function (m, c) { return c.toUpperCase(); })] = String(v); };
        el.getAttribute = function (k) { return el._attrs[k] === undefined ? null : el._attrs[k]; };
        el.removeAttribute = function (k) { delete el._attrs[k]; };
        el.hasAttribute = function (k) { return el._attrs[k] !== undefined; };
        el.appendChild = function (c) { el.children.push(c); c.parentNode = el; return c; };
        el.removeChild = function (c) {
            var i = el.children.indexOf(c);
            if (i >= 0) { el.children.splice(i, 1); }
            c.parentNode = null;
            return c;
        };
        el.addEventListener = function (t, fn) {
            (el._listeners[t] = el._listeners[t] || []).push(fn);
        };
        el.removeEventListener = function (t, fn) {
            var a = el._listeners[t] || [];
            var i = a.indexOf(fn);
            if (i >= 0) { a.splice(i, 1); }
        };
        el.dispatch = function (t, ev) {
            var a = (el._listeners[t] || []).slice();
            for (var i = 0; i < a.length; i += 1) { a[i](ev || { type: t, target: el }); }
        };
        el.querySelectorAll = function () { return []; };
        el.querySelector = function () { return null; };
        el.getElementsByClassName = function () { return []; };
        /* 実 DOM では `el.title = 'x'` は title 属性に反映される。
           app.js の同意ゲートは `button.title = ...` と**プロパティ代入**で
           理由を伝えるため、属性へ反射させて観測できるようにする。
           （反映しないと title を設定したのに空に見える） */
        Object.defineProperty(el, 'title', {
            get: function () { return el._attrs.title === undefined ? '' : el._attrs.title; },
            set: function (v) { el._attrs.title = String(v); },
        });
        el.getBoundingClientRect = function () { return { top: 0, left: 0, bottom: 0, right: 0, height: 0, width: 0 }; };
        el.scrollIntoView = function () {};
        el.focus = function () {};
        el.click = function () { el.dispatch('click'); };
        el.load = function () {};
        Object.defineProperty(el, 'firstChild', { get: function () { return el.children[0] || null; } });
        Object.defineProperty(el, 'textContent', {
            get: function () { return el._text; },
            set: function (v) { el._text = String(v); },
        });
        Object.defineProperty(el, 'innerHTML', {
            get: function () { return el._html || ''; },
            set: function (v) { el._html = String(v); },
        });
        Object.defineProperty(el, 'children.length', { get: function () { return el.children.length; } });

        if (el.tagName === 'AUDIO') {
            audioElements.push(el);
            el.paused = true;
            el.ended = false;
            el.currentTime = 0;
            el.duration = NaN;
            el.volume = 1;
            el.muted = false;
            el.error = null;
            el.readyState = 4;
            el.__playCalls = 0;
            el.play = function () {
                el.__playCalls += 1;
                if (el.__blockPlay) {
                    var e = new Error('blocked');
                    e.name = 'NotAllowedError';
                    return { then: function (f) { return { catch: function () {} }; }, catch: function () {} };
                }
                el.paused = false;
                el.dispatch('play');
                return { then: function (f) { return { catch: function () {} }; } };
            };
            el.pause = function () {
                if (!el.paused) { el.paused = true; el.dispatch('pause'); }
            };
            el.__finish = function () {
                el.ended = true;
                el.paused = true;
                el.dispatch('ended');
            };
            el.__fail = function () {
                el.error = { code: 4 };
                el.dispatch('error');
            };
            el.__advance = function (to) {
                el.currentTime = to;
                el.dispatch('timeupdate');
            };
        }
        return el;
    }

    var byId = {};
    function stub(id, tag) {
        var el = makeElement(tag || 'div');
        el.id = id;
        byId[id] = el;
        return el;
    }

    var REQUIRED = [
        'generateForm', 'yearSelect', 'monthSelect', 'daySelect', 'modeGroup',
        'targetNameGroup', 'targetNameInput', 'tuningForm', 'tuneFreq', 'startBtn',
        'onAirBadge', 'streamStatus', 'streamStatusTitle', 'streamStatusDesc',
        'stateBanner', 'errorBanner', 'emptyState', 'outputSection', 'tuningSection',
        'manuscriptCard', 'manuscriptTitle', 'manuscriptBody', 'songCard',
        'songTitle', 'songArtist', 'vinylDisk', 'vuMeter', 'tubeGlow', 'playerCard',
        'audioBtn', 'playPauseBtn', 'restartBtn', 'seekBar', 'seekCurrent',
        'seekDuration', 'trackIndex', 'trackTitle', 'trackArtist', 'trackPass',
        'btnPrev', 'btnNext', 'btnMute', 'volumeControl', 'btnLoop', 'btnRepeat',
        'playlistList', 'programGuide', 'repeatLabel', 'quizCard', 'quizList',
        'remembranceCard', 'remembranceBody', 'remembranceImage', 'emptyGuide',
        'loadingIndicator', 'reloadBtn', 'successBtn', 'toggleDetails', 'details',
    ];
    REQUIRED.forEach(function (id) { stub(id); });

    var document = {
        readyState: 'complete',
        body: makeElement('body'),
        documentElement: makeElement('html'),
        getElementById: function (id) { return byId[id] || null; },
        createElement: makeElement,
        createElementNS: function (ns, tag) { return makeElement(tag); },
        // app.js は `button.textContent = ''` してから span とテキストノードを
        // 差し込む（setPlayButtonLabel）。createTextNode が無いと
        // 「not a function」で落ちるため、最小実装を入れておく。
        createTextNode: function (text) {
            var node = makeElement('#text');
            node.nodeValue = String(text);
            node.textContent = String(text);
            return node;
        },
        querySelector: function (sel) {
            if (sel.charAt(0) === '#') { return byId[sel.slice(1)] || null; }
            return null;
        },
        querySelectorAll: function () { return []; },
        addEventListener: function (t, fn) { (listeners[t] = listeners[t] || []).push(fn); },
        removeEventListener: function () {},
    };
    document.body.appendChild = function (c) { return c; };
    document.body.removeChild = function () {};

    function fire(t) {
        (listeners[t] || []).forEach(function (f) { f({ type: t }); });
    }

    var window = {
        document: document,
        navigator: { serviceWorker: { register: function () { return { catch: function () {} }; } }, vibrate: function () {}, onLine: true },
        location: { protocol: 'http:', origin: 'http://localhost:8501', href: 'http://localhost:8501/' },
        addEventListener: function (t, fn) { (listeners[t] = listeners[t] || []).push(fn); },
        removeEventListener: function () {},
        matchMedia: function () { return { matches: false, addEventListener: function () {}, addListener: function () {} }; },
        AudioContext: undefined,
        webkitAudioContext: undefined,
        Audio: function () { return makeElement('audio'); },
        setTimeout: function (fn, ms) { var id = nextTimerId++; timers.timeout.push({ id: id, fn: fn, ms: ms }); return id; },
        clearTimeout: function (id) { timers.timeout = timers.timeout.filter(function (t) { return t.id !== id; }); },
        setInterval: function (fn, ms) { var id = nextTimerId++; timers.interval.push({ id: id, fn: fn, ms: ms }); return id; },
        clearInterval: function (id) { timers.interval = timers.interval.filter(function (t) { return t.id !== id; }); },
        setTimeout_: null,
    };
    window.window = window;
    window.self = window;
    window.globalThis = window;
    // global に露出（app.js は素のグローバルを参照する）
    var g = {};
    g.window = window;
    g.document = document;
    g.navigator = window.navigator;
    g.location = window.location;
    g.localStorage = {
        _d: {},
        getItem: function (k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
        setItem: function (k, v) { this._d[k] = String(v); },
        removeItem: function (k) { delete this._d[k]; },
    };
    g.sessionStorage = g.localStorage;
    g.setTimeout = window.setTimeout;
    g.clearTimeout = window.clearTimeout;
    g.setInterval = window.setInterval;
    g.clearInterval = window.clearInterval;
    g.Audio = window.Audio;
    g.requestAnimationFrame = function (fn) { return window.setTimeout(fn, 16); };
    g.cancelAnimationFrame = function (id) { window.clearTimeout(id); };
    g.matchMedia = window.matchMedia;
    g.console = console;
    g.addEventListener = window.addEventListener;
    g.removeEventListener = function () {};
    g.fetch = function () { return Promise.reject(new Error('no network in harness')); };
    g.FormData = function () { this.append = function () {}; this.entries = function () { return []; }; };
    // checkHealth が window.fetch / AbortController を使うため
    window.fetch = g.fetch;
    g.AbortController = function () {
        this.signal = { aborted: false, addEventListener: function () {} };
        this.abort = function () { this.signal.aborted = true; };
    };
    window.AbortController = g.AbortController;
    g.Headers = function () { this.get = function () { return null; }; };
    g.performance = { now: function () { return 0; } };
    g.getComputedStyle = function () { return { getPropertyValue: function () { return ''; } }; };
    g.innerWidth = 1200;
    g.innerHeight = 800;
    g.alert = function () {};
    // dukpy（Duktape）には URL クラスが無いので app.js の toAbsoluteUrl /
    // isSameOrigin が動く最小実装を入れる。
    function URLCtor(input, base) {
        var href = String(input);
        if (!/^[a-z][a-z0-9+.-]*:/i.test(href)) {
            var origin = 'http://localhost:8501';
            if (href.charAt(0) === '/') {
                href = origin + href;
            } else {
                href = origin + '/' + href;
            }
        }
        this.href = href;
        var m = /^([a-z][a-z0-9+.-]*:)\/\/([^/?#]+)/i.exec(href);
        this.protocol = m ? m[1] : 'http:';
        this.origin = m ? (m[1] + '//' + m[2]) : 'null';
        this.hostname = m ? m[2] : '';
        this.search = '';
        this.hash = '';
    }
    URLCtor.createObjectURL = function () { return 'blob:harness'; };
    URLCtor.revokeObjectURL = function () {};
    g.URL = URLCtor;
    g.Blob = function () {};
    g.DataView = undefined;
    g.ArrayBuffer = ArrayBuffer;
    g.isSecureContext = true;
    g.history = { replaceState: function () {} };
    g.screen = { orientation: { type: 'landscape-primary' } };

    return {
        g: g,
        window: window,
        document: document,
        audioElements: audioElements,
        timers: timers,
        fire: fire,
        byId: byId,
        resetTimers: function () { timers.timeout = []; timers.interval = []; },
    };
})();

// app.js は素のグローバル（document / window / localStorage ...）を参照するため、
// dukpy のグローバルスコープへ実体を露出する。
(function () {
    var g = __harness.g;
    var names = [
        'window', 'document', 'navigator', 'location', 'localStorage', 'sessionStorage',
        'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Audio',
        'requestAnimationFrame', 'cancelAnimationFrame', 'matchMedia', 'console',
        'addEventListener', 'removeEventListener', 'fetch', 'FormData', 'performance',
        'getComputedStyle', 'innerWidth', 'innerHeight', 'alert', 'URL', 'Blob',
        'ArrayBuffer', 'isSecureContext', 'history', 'screen', 'self', 'globalThis',
    ];
    for (var i = 0; i < names.length; i += 1) {
        var n = names[i];
        this[n] = g[n];
    }
    // dukpy には globalThis が無いので window から拾わせる
    if (typeof this.globalThis === 'undefined') { this.globalThis = g.window; }
    // __harness は window のプロパティとしても見えるようにする
    g.window.__harness = __harness;
})();
