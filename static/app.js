/* ==========================================================================
   Retro Radio Time Machine - フロントエンド制御（Pure SPA）
   --------------------------------------------------------------------------
   画面操作（ダイヤル / モード切替 / 再生）と FastAPI バックエンド
   (POST /api/generate, GET /api/audio/{filename}, GET /health) の通信を司る。

   設計上の要点:
     - バックエンド由来のテキストは原則 textContent で描画する。
        構造化レンダリング（<h3> / <p>）が必要な原稿のみ escapeHtml() を通した
       文字列を innerHTML に代入し、生の文字列を直接 innerHTML しない。
     - 連続放送は単一の <audio> 要素の src を差し替えて実現する。
     - Web Audio 解析（VU メーター）は「キュー全体が同一オリジン」の
       ときだけ有効化する。別オリジン（iTunes プレビュー）を
       MediaElementSource 経由anjutkanと CORS 制約で無音になるため。
   ========================================================================== */

(function () {
    'use strict';

    /* ---------------------------------------------------------------------
       定数
       --------------------------------------------------------------------- */
    var MIN_YEAR = 1950;
    var MAX_YEAR = 2025;
    var DEFAULT_YEAR = 1975;

    // gTTS を 5〜6 回連続実行するため応答生成は長めに取る
    var GENERATE_TIMEOUT_MS = 180000;
    var HEALTH_TIMEOUT_MS = 8000;
    var SCRIPT_MAX_CHARS = 40000;
    var STORAGE_PREFIX = 'retroRadio.';

    var MODE_LABELS = {
        normal: 'タイムマシン本放送',
        care_recreation: 'デイサービス回想法',
        anniversary: '記念日ギフト'
    };

    /* ---------------------------------------------------------------------
       年選択（ダイヤル / 年代チップ / 年ステッパの 3 層で共有）
       --------------------------------------------------------------------- */
    var DECADE_STARTS = [1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020];

    /* ---------------------------------------------------------------------
       受信進捗（バックエンドは逐次応答しないため「経過時間ベースの目安」を使う）
       --------------------------------------------------------------------- */
    var PROGRESS_STEPS = ['connect', 'script', 'tts', 'song'];
    var PROGRESS_TIMELINE = [
        { at: 0, percent: 5, step: 'connect' },
        { at: 800, percent: 20, step: 'script' },
        { at: 8000, percent: 45, step: 'script' },
        { at: 20000, percent: 65, step: 'tts' },
        { at: 45000, percent: 85, step: 'tts' },
        { at: 70000, percent: 92, step: 'song' }
    ];
    // 応答を受信するまでは 95% で頭打ちにする（100% は renderSuccess でのみ）
    var PROGRESS_MAX_PERCENT = 95;
    var PROGRESS_TICK_MS = 250;

    /* ---------------------------------------------------------------------
       localStorage（既存の readStore / writeStore が prefix を付ける）
       --------------------------------------------------------------------- */
    var RETRY_STORAGE_KEY = 'lastRequest';
    var VOLUME_STORAGE_KEY = 'volume';
    var LOOP_STORAGE_KEY = 'loopEnabled';

    /* 状態バナーのアイコン（docs/state_design_system.md の 4 種に対応） */
    var STATE_BANNER_ICONS = {
        success: '✅',
        warning: '⚠️',
        error: '⛔',
        info: 'ℹ️'
    };

    var DEFAULT_STATUS_TITLE = '📡 ラジオを再生してください';
    var DEFAULT_STATUS_DESC = 'ダイヤルを回して好きな年を選び、【ラジオを再生する】を押すと番組が始まります。';

    var TALK = 'TALK';
    var SONG = 'SONG';

    /* ---------------------------------------------------------------------
       内部状態
       --------------------------------------------------------------------- */
    var dom = {};
    var liveRegion = null;

    var state = {
        year: DEFAULT_YEAR,
        mode: 'normal',
        isLoading: false,
        requestToken: 0,
        abortController: null,
        timeoutTimer: 0,
        timedOut: false,
        skipTimer: 0,
        announceTimer: 0,
        audio: null,
        queue: [],
        index: -1,
        endedCount: 0,
        errorStreak: 0,
        userPaused: false,
        needGesture: false,
        finished: false,
        prefetchedUrl: '',
        vuTimer: 0,
        audioCtx: null,
        sourceNode: null,
        analyser: null,
        allSameOrigin: false,
        analyserAttached: false,
        draggingRail: false,
        draggingKnob: false,
        knobAccum: 0,
        knobLastY: 0,
        knobAngle: 0,
        printState: null,
        lastResult: null,
        /* --- Wave2 (SubD): 受信進捗 --- */
        progressTimer: 0,
        progressStartedAt: 0,
        progressPercent: 0,
        errorDetail: '',
        /* 再試行用に保持する { year, month, day, mode, targetName } */
        lastRequest: null,
        /* --- Wave2: SubE（プレイヤー UI）が使うフラグ --- */
        playerBound: false,
        muted: false,
        loopEnabled: false,
        volume: 0.8
    };

    /* =====================================================================
       小道具
       ===================================================================== */

    function byId(id) {
        return document.getElementById(id);
    }

    function noop() { }

    function safeAbort(controller) {
        if (!controller) { return; }
        try { controller.abort(); } catch (e) { /* noop */ }
    }

    function createController() {
        if (typeof window.AbortController === 'function') {
            return new window.AbortController();
        }
        // 旧ブラウザ用のフォールバック（中断はできないが動作はする）
        return { signal: undefined, abort: noop };
    }

    function readStore(key, fallback) {
        try {
            var value = window.localStorage.getItem(STORAGE_PREFIX + key);
            return value === null ? fallback : value;
        } catch (e) {
            return fallback;
        }
    }

    function writeStore(key, value) {
        try {
            window.localStorage.setItem(STORAGE_PREFIX + key, value);
        } catch (e) {
            // プライベートモード等で例外になる場合は無視する
        }
    }

    function clampYear(value) {
        var year = Math.round(Number(value));
        if (!isFinite(year)) { return DEFAULT_YEAR; }
        if (year < MIN_YEAR) { return MIN_YEAR; }
        if (year > MAX_YEAR) { return MAX_YEAR; }
        return year;
    }

    function isFiniteNumber(value) {
        return typeof value === 'number' && isFinite(value);
    }

    function modeLabel(mode) {
        if (Object.prototype.hasOwnProperty.call(MODE_LABELS, mode)) {
            return MODE_LABELS[mode];
        }
        return MODE_LABELS.normal;
    }

    function toAbsoluteUrl(url) {
        var raw = String(url === null || url === undefined ? '' : url).trim();
        if (!raw) { return ''; }
        if (/^data:/i.test(raw) || /^blob:/i.test(raw)) { return raw; }
        try {
            return new URL(raw, window.location.href).href;
        } catch (e) {
            return '';
        }
    }

    function isSameOrigin(url) {
        try {
            return new URL(url, window.location.href).origin === window.location.origin;
        } catch (e) {
            return false;
        }
    }

    function firstLine(text) {
        var lines = String(text).split('\n');
        return lines[0] || '';
    }

    /* =====================================================================
       和暦変換
       ---------------------------------------------------------------------
       明治: 1868-1912 / 大正: 1912-1926 / 昭和: 1926-1989
       平成: 1989-2019 / 令和: 2019-
       ===================================================================== */
    function toEraYear(year) {
        var y = Number(year);
        if (!isFinite(y)) { return '—'; }
        if (y >= 2019) { return '令和' + (y - 2018) + '年'; }
        if (y >= 1989) { return '平成' + (y - 1988) + '年'; }
        if (y >= 1926) { return '昭和' + (y - 1925) + '年'; }
        if (y >= 1912) { return '大正' + (y - 1911) + '年'; }
        if (y >= 1868) { return '明治' + (y - 1867) + '年'; }
        return '西暦' + y + '年';
    }

    /* =====================================================================
       日付の検証
       ===================================================================== */
    function daysInMonth(year, month) {
        var table = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
        if (month === 2) {
            var leap = (year % 4 === 0 && year % 100 !== 0) || (year % 400 === 0);
            return leap ? 29 : 28;
        }
        return table[month - 1];
    }

    function isValidDate(year, month, day) {
        if (!(month >= 1 && month <= 12)) { return false; }
        if (!(day >= 1 && day <= 31)) { return false; }
        return day <= daysInMonth(year, month);
    }

    // "9-24" / "9/24" / "9月24日" を分解する
    function parseMonthDay(raw) {
        var value = String(raw === null || raw === undefined ? '' : raw).trim();
        if (!value) {
            return { error: '実施日（月日）を入力してください。例: 9-24' };
        }
        var matched = /^(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?$/.exec(value);
        if (!matched) {
            return { error: '実施日の形式が不正です。「月-日」（例: 9-24 や 9月24日）で入力してください。' };
        }
        var month = parseInt(matched[1], 10);
        var day = parseInt(matched[2], 10);
        if (!(month >= 1 && month <= 12)) {
            return { error: '月は 1〜12 の範囲で入力してください（入力値: ' + month + '）。' };
        }
        if (!(day >= 1 && day <= 31)) {
            return { error: '日は 1〜31 の範囲で入力してください（入力値: ' + day + '）。' };
        }
        return { month: month, day: day };
    }

    // "1975-05-12" を分解する（年つき）
    function parseFullDate(raw) {
        var value = String(raw === null || raw === undefined ? '' : raw).trim();
        if (!value) {
            return { error: 'お祝いの日付を入力してください。例: 1975-05-12' };
        }
        var matched = /^(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*日?$/.exec(value);
        if (!matched) {
            return { error: '日付の形式が不正です。「西暦-月-日」（例: 1975-05-12）で入力してください。' };
        }
        var year = parseInt(matched[1], 10);
        var month = parseInt(matched[2], 10);
        var day = parseInt(matched[3], 10);
        if (year < MIN_YEAR || year > MAX_YEAR) {
            return { error: '年は ' + MIN_YEAR + '年〜' + MAX_YEAR + '年の範囲で入力してください（入力値: ' + year + '）。' };
        }
        if (!(month >= 1 && month <= 12)) {
            return { error: '月は 1〜12 の範囲で入力してください（入力値: ' + month + '）。' };
        }
        if (!isValidDate(year, month, day)) {
            return { error: year + '年' + month + '月' + day + '日は存在しない日付です（' + daysInMonth(year, month) + '日までです）。' };
        }
        return { year: year, month: month, day: day };
    }

    /* =====================================================================
       XSS 対策 / 原稿レンダラ
       ===================================================================== */
    function escapeHtml(value) {
        if (value === null || value === undefined) { return ''; }
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * 「### 見出し」付きの原稿テキストを安全な HTML へ変換する。
     * 必ず escapeHtml() を通した文字列のみを innerHTML に渡すこと。
     */
    function renderScriptHtml(raw) {
        var text = typeof raw === 'string' ? raw : '';
        if (!text.trim()) { return ''; }

        var lines = text.split(/\r\n|\r|\n/);
        var html = [];
        var buffer = [];
        var emitted = 0;

        function flushParagraph() {
            if (!buffer.length) { return; }
            var body = buffer.map(escapeHtml).join('<br>');
            html.push('<p class="script-paragraph">' + body + '</p>');
            emitted += buffer.join('').length;
            buffer = [];
        }

        for (var i = 0; i < lines.length; i += 1) {
            if (emitted >= SCRIPT_MAX_CHARS) {
                flushParagraph();
                html.push('<p class="script-paragraph script-truncated">（以下、省略されました）</p>');
                break;
            }
            var line = lines[i];
            var heading = /^\s*#{1,6}\s+(\S.*?)\s*$/.exec(line);
            if (heading) {
                flushParagraph();
                html.push('<h3 class="script-heading">' + escapeHtml(heading[1]) + '</h3>');
                continue;
            }
            if (!line.trim()) {
                flushParagraph();
                continue;
            }
            buffer.push(line);
        }
        flushParagraph();

        return html.join('');
    }

    /* =====================================================================
       ステータス表示
       ===================================================================== */
    function setStreamTitle(text) {
        if (dom.streamStatusTitle) { dom.streamStatusTitle.textContent = text; }
    }

    function setStreamDesc(text) {
        if (dom.streamStatusDesc) { dom.streamStatusDesc.textContent = text; }
    }

    function setServiceStatus(kind, text) {
        if (!dom.serviceStatus) { return; }
        dom.serviceStatus.textContent = text;
        dom.serviceStatus.className = 'service-status' + (kind ? ' ' + kind : '');
    }

    function announce(text) {
        if (!liveRegion) { return; }
        liveRegion.textContent = '';
        window.setTimeout(function () { liveRegion.textContent = text; }, 30);
    }

    function announceYear() {
        if (state.announceTimer) { window.clearTimeout(state.announceTimer); }
        state.announceTimer = window.setTimeout(function () {
            state.announceTimer = 0;
            announce(state.year + '年（' + toEraYear(state.year) + '）を選択中。' + modeLabel(state.mode) + 'モード。');
        }, 400);
    }

    function updatePendingTitle() {
        if (!dom.manuscriptTitle) { return; }
        dom.manuscriptTitle.textContent =
            '📻 ' + state.year + '年 【' + modeLabel(state.mode) + '】（未受信）';
    }

    /* =====================================================================
       ダイヤル（年選択）
       ===================================================================== */
    function setYear(value, options) {
        var opts = options || {};
        var year = clampYear(value);
        state.year = year;

        if (dom.yearBadge) { dom.yearBadge.textContent = year + '年'; }
        if (dom.eraText) { dom.eraText.textContent = toEraYear(year); }
        if (dom.tunerNeedle) {
            var span = MAX_YEAR - MIN_YEAR;
            var ratio = span === 0 ? 0 : (year - MIN_YEAR) / span;
            dom.tunerNeedle.style.left = (ratio * 100).toFixed(2) + '%';
        }
        if (dom.tunerRail) {
            dom.tunerRail.setAttribute('aria-valuenow', String(year));
            dom.tunerRail.setAttribute('aria-valuetext', year + '年 ' + toEraYear(year));
        }

        // 年選択 UI（年ステッパ / ノブ / 年代チップ）は setYear を単一入口にする
        syncYearInputs(year);
        setDecadeChipActive(year);
        if (dom.btnEmptyPlay) {
            dom.btnEmptyPlay.textContent = '📻 ' + year + '年の番組を再生する';
        }

        if (!opts.silent) {
            announceYear();
        }
    }

    function stepYear(delta) {
        setYear(state.year + delta, { silent: false });
    }

    /* =====================================================================
       年代選択 3 層（年代チップ / 年ステッパ / 年の直接入力）
       ---------------------------------------------------------------------
       状態を二重管理しないため、ここでは state を触らず setYear() だけを呼ぶ。
       ===================================================================== */

    // 年が属する年代（1950〜2020 の 10 年刻み）を返す
    function decadeOf(year) {
        var value = Number(year);
        if (!isFinite(value)) { return DECADE_STARTS[0]; }
        var decade = MIN_YEAR + Math.floor((value - MIN_YEAR) / 10) * 10;
        var last = DECADE_STARTS[DECADE_STARTS.length - 1];
        if (decade < MIN_YEAR) { decade = MIN_YEAR; }
        if (decade > last) { decade = last; }
        return decade;
    }

    // #yearInput / #brassKnob / ヒントを state.year に同期する
    function syncYearInputs(year, options) {
        var opts = options || {};
        var value = clampYear(year);

        if (dom.yearInput) {
            // 入力中の打ち人影を消さないよう、フォーカス中は書き換えない
            if (opts.force || document.activeElement !== dom.yearInput) {
                dom.yearInput.value = String(value);
            }
            dom.yearInput.setAttribute('aria-valuenow', String(value));
        }
        if (dom.brassKnob) {
            dom.brassKnob.setAttribute('aria-valuenow', String(value));
            dom.brassKnob.setAttribute('aria-valuetext', value + '年 ' + toEraYear(value));
        }
        if (dom.yearInputHint) {
            dom.yearInputHint.textContent =
                MIN_YEAR + '年〜' + MAX_YEAR + '年の数字を入力できます（現在 ' + value + '年）';
        }
    }

    // 該当年代チップだけを .active にする
    function setDecadeChipActive(year) {
        var container = dom.decadeChips;
        if (!container) { return; }
        var current = decadeOf(year);
        var chips = container.querySelectorAll('.decade-chip');
        for (var i = 0; i < chips.length; i += 1) {
            var chip = chips[i];
            if (!chip) { continue; }
            var value = parseInt(chip.getAttribute('data-decade'), 10);
            var active = (value === current);
            chip.classList.toggle('active', active);
            chip.setAttribute('aria-pressed', active ? 'true' : 'false');
        }
    }

    // #yearInput の値をクランプ済み年数として読む（数字でなければ null）
    function readYearInput() {
        var input = dom.yearInput;
        if (!input) { return null; }
        var raw = String(input.value === null || input.value === undefined ? '' : input.value).trim();
        if (!raw) { return null; }
        // inputmode="numeric" でも記号は打てるため、数字以外を弾く
        for (var i = 0; i < raw.length; i += 1) {
            var code = raw.charCodeAt(i);
            if (code < 48 || code > 57) { return null; }
        }
        var value = parseInt(raw, 10);
        if (!isFinite(value)) { return null; }
        // 範囲外は 1950〜2025 に丸めて返す
        return clampYear(value);
    }

    // 年代チップを押した時: その年代へスナップ（同じ年代なら次の年代へ）
    function onDecadeChipClick(event) {
        var chip = event && event.currentTarget ? event.currentTarget : null;
        if (!chip) { return; }
        var target = parseInt(chip.getAttribute('data-decade'), 10);
        if (!isFinite(target)) { return; }

        var last = DECADE_STARTS[DECADE_STARTS.length - 1];
        if (decadeOf(state.year) === target) {
            // 同じ年代をもう一度押したら 10 年先へ（末尾の年代はそのまま）
            target = (target >= last) ? last : target + 10;
        }
        setYear(target, { silent: false });
    }

    function bindDecadeChips() {
        var container = dom.decadeChips;
        if (!container) { return; }
        var chips = container.querySelectorAll('.decade-chip');
        for (var i = 0; i < chips.length; i += 1) {
            if (!chips[i]) { continue; }
            chips[i].addEventListener('click', onDecadeChipClick);
        }
        setDecadeChipActive(state.year);
    }

    // 年ステッパ / 年の直接入力を確定する
    function commitYearInput() {
        var input = dom.yearInput;
        if (!input) { return; }

        var value = readYearInput();
        if (value === null) {
            var raw = String(input.value === null || input.value === undefined ? '' : input.value).trim();
            if (raw) {
                showInputError('年は ' + MIN_YEAR + '〜' + MAX_YEAR + ' を数字で入力してください。', dom.yearInput);
            }
            syncYearInputs(state.year, { force: true });
            return;
        }

        // readYearInput がクランプ済みなのでそのまま反映する
        setYear(value, { silent: false });
        syncYearInputs(state.year, { force: true });
    }

    function bindYearStepper() {
        if (dom.btnYearMinus10) {
            dom.btnYearMinus10.addEventListener('click', function () { stepYear(-10); });
        }
        if (dom.btnYearMinus1) {
            dom.btnYearMinus1.addEventListener('click', function () { stepYear(-1); });
        }
        if (dom.btnYearPlus1) {
            dom.btnYearPlus1.addEventListener('click', function () { stepYear(1); });
        }
        if (dom.btnYearPlus10) {
            dom.btnYearPlus10.addEventListener('click', function () { stepYear(10); });
        }
        if (dom.yearInput) {
            dom.yearInput.addEventListener('blur', function () { commitYearInput(); });
            dom.yearInput.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    commitYearInput();
                }
            });
            dom.yearInput.addEventListener('input', function () {
                this.classList.remove('input-error');
                this.removeAttribute('aria-invalid');
            });
        }
    }

    function yearFromClientX(clientX) {
        if (!dom.tunerRail) { return state.year; }
        var rect = dom.tunerRail.getBoundingClientRect();
        if (!rect || !rect.width) { return state.year; }
        var ratio = (clientX - rect.left) / rect.width;
        if (ratio < 0) { ratio = 0; }
        if (ratio > 1) { ratio = 1; }
        return MIN_YEAR + Math.round(ratio * (MAX_YEAR - MIN_YEAR));
    }

    function bindTuner() {
        var rail = dom.tunerRail;
        var knob = dom.brassKnob;

        /* --- Rail: クリック / ドラッグ --- */
        if (rail) {
            rail.addEventListener('click', function (event) {
                setYear(yearFromClientX(event.clientX), { silent: false });
            });
            rail.addEventListener('mousedown', function (event) {
                if (event.button !== undefined && event.button !== 0) { return; }
                state.draggingRail = true;
                setYear(yearFromClientX(event.clientX), { silent: false });
            });
            rail.addEventListener('touchstart', function (event) {
                var touch = event.touches && event.touches[0];
                if (!touch) { return; }
                state.draggingRail = true;
                setYear(yearFromClientX(touch.clientX), { silent: false });
            }, { passive: true });
            rail.addEventListener('touchend', function () { state.draggingRail = false; }, { passive: true });

            /* --- Rail: キーボード（role="slider" 準拠） --- */
            rail.addEventListener('keydown', function (event) {
                var handled = true;
                switch (event.key) {
                    case 'ArrowRight':
                    case 'ArrowUp':
                        stepYear(event.shiftKey ? 10 : 1);
                        break;
                    case 'ArrowLeft':
                    case 'ArrowDown':
                        stepYear(event.shiftKey ? -10 : -1);
                        break;
                    case 'PageUp':
                        stepYear(10);
                        break;
                    case 'PageDown':
                        stepYear(-10);
                        break;
                    case 'Home':
                        setYear(MIN_YEAR, { silent: false });
                        break;
                    case 'End':
                        setYear(MAX_YEAR, { silent: false });
                        break;
                    default:
                        handled = false;
                }
                if (handled) {
                    event.preventDefault();
                }
            });
        }

        /* --- document 共通のドラッグ追従 --- */
        document.addEventListener('mousemove', function (event) {
            if (state.draggingRail) {
                setYear(yearFromClientX(event.clientX), { silent: false });
            }
            if (state.draggingKnob) {
                var deltaY = event.clientY - state.knobLastY;
                state.knobLastY = event.clientY;
                applyKnobDelta(deltaY);
            }
        });

        document.addEventListener('mouseup', function () {
            state.draggingRail = false;
            state.draggingKnob = false;
        });

        /* --- Knob: ホイール / ドラッグ / キー --- */
        if (knob) {
            knob.addEventListener('wheel', function (event) {
                event.preventDefault();
                if (event.deltaY < 0) {
                    stepYear(1);
                } else if (event.deltaY > 0) {
                    stepYear(-1);
                }
            }, { passive: false });

            knob.addEventListener('mousedown', function (event) {
                if (event.button !== undefined && event.button !== 0) { return; }
                state.draggingKnob = true;
                state.knobAccum = 0;
                state.knobLastY = event.clientY;
                event.preventDefault();
            });

            knob.addEventListener('touchstart', function (event) {
                var touch = event.touches && event.touches[0];
                if (!touch) { return; }
                state.draggingKnob = true;
                state.knobAccum = 0;
                state.knobLastY = touch.clientY;
            }, { passive: true });

            knob.addEventListener('touchmove', function (event) {
                if (!state.draggingKnob) { return; }
                var touch = event.touches && event.touches[0];
                if (!touch) { return; }
                var deltaY = touch.clientY - state.knobLastY;
                state.knobLastY = touch.clientY;
                applyKnobDelta(deltaY);
                event.preventDefault();
            }, { passive: false });

            knob.addEventListener('touchend', function () {
                state.draggingKnob = false;
            }, { passive: true });

            knob.addEventListener('keydown', function (event) {
                var handled = true;
                switch (event.key) {
                    case 'ArrowRight':
                    case 'ArrowUp':
                        stepYear(event.shiftKey ? 10 : 1);
                        break;
                    case 'ArrowLeft':
                    case 'ArrowDown':
                        stepYear(event.shiftKey ? -10 : -1);
                        break;
                    case 'PageUp':
                        stepYear(10);
                        break;
                    case 'PageDown':
                        stepYear(-10);
                        break;
                    case 'Home':
                        setYear(MIN_YEAR, { silent: false });
                        break;
                    case 'End':
                        setYear(MAX_YEAR, { silent: false });
                        break;
                    default:
                        handled = false;
                }
                if (handled) { event.preventDefault(); }
            });
        }
    }

    // ノブの上下ドラッグ: 6px ごとに 1 年
    function applyKnobDelta(deltaY) {
        if (!deltaY) { return; }
        state.knobAccum += deltaY;
        var changed = 0;
        while (state.knobAccum >= 6) {
            state.knobAccum -= 6;
            changed -= 1;
        }
        while (state.knobAccum <= -6) {
            state.knobAccum += 6;
            changed += 1;
        }
        if (changed !== 0) {
            setYear(state.year + changed, { silent: false });
        }
        state.knobAngle = (state.knobAngle - deltaY * 1.2) % 360;
        if (dom.brassKnob) {
            dom.brassKnob.style.transform = 'rotate(' + state.knobAngle.toFixed(1) + 'deg)';
        }
    }

    /* =====================================================================
       モード切替
       ===================================================================== */
    function tabList() {
        return [
            { el: dom.tabModeNormal, mode: 'normal' },
            { el: dom.tabModeCare, mode: 'care_recreation' },
            { el: dom.tabModeAnniversary, mode: 'anniversary' }
        ];
    }

    function setMode(mode, options) {
        var opts = options || {};
        if (!Object.prototype.hasOwnProperty.call(MODE_LABELS, mode)) {
            mode = 'normal';
        }
        state.mode = mode;

        tabList().forEach(function (tab) {
            if (!tab.el) { return; }
            var active = (tab.mode === mode);
            tab.el.classList.toggle('active', active);
            tab.el.setAttribute('aria-selected', active ? 'true' : 'false');
            tab.el.tabIndex = active ? 0 : -1;
        });

        if (dom.careModeBox) {
            dom.careModeBox.classList.toggle('visible', mode === 'care_recreation');
        }
        if (dom.anniversaryModeBox) {
            dom.anniversaryModeBox.classList.toggle('visible', mode === 'anniversary');
        }
        if (mode !== 'care_recreation' && dom.recreationQuizBox) {
            dom.recreationQuizBox.style.display = 'none';
        }

        // モードが変わると進行中のリクエストと再生内容は無効になる
        cancelGeneration(true);
        stopPlayback();
        // 前のモードの結果は表示し Transit しない（空状態に戻して案内を出す）
        hideErrorState();
        hideStateBanner();
        showEmptyState(true);
        updatePendingTitle();
        if (!opts.silent) {
            announce(modeLabel(mode) + 'モードに切り替えました。');
        }
    }

    function bindModeTabs() {
        var tabs = tabList();
        tabs.forEach(function (tab) {
            if (!tab.el) { return; }
            tab.el.addEventListener('click', function () {
                setMode(tab.mode, { silent: false });
            });
            tab.el.addEventListener('keydown', function (event) {
                var current = -1;
                for (var i = 0; i < tabs.length; i += 1) {
                    if (tabs[i].el && tabs[i].el === document.activeElement) { current = i; }
                }
                if (current < 0) { return; }

                var next = -1;
                if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
                    next = (current + 1) % tabs.length;
                } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
                    next = (current - 1 + tabs.length) % tabs.length;
                } else if (event.key === 'Home') {
                    next = 0;
                } else if (event.key === 'End') {
                    next = tabs.length - 1;
                }
                if (next < 0) { return; }

                event.preventDefault();
                if (tabs[next].el) {
                    tabs[next].el.focus();
                    setMode(tabs[next].mode, { silent: false });
                }
            });
        });
    }

    /* =====================================================================
       入力値の読み取りと検証
       ===================================================================== */
    function markInvalidField(field) {
        var fields = [dom.careRecreationDate, dom.anniversaryDate, dom.anniversaryName, dom.yearInput];
        fields.forEach(function (element) {
            if (!element) { return; }
            if (element === field) {
                element.classList.add('input-error');
                element.setAttribute('aria-invalid', 'true');
            } else {
                element.classList.remove('input-error');
                element.removeAttribute('aria-invalid');
            }
        });
    }

    function clearInvalidFields() {
        markInvalidField(null);
    }

    function readForm() {
        var mode = state.mode;
        var year;
        var month;
        var day;
        var targetName = '';

        if (mode === 'anniversary') {
            var parsed = parseFullDate(dom.anniversaryDate ? dom.anniversaryDate.value : '');
            if (parsed.error) {
                return { error: parsed.error, field: dom.anniversaryDate };
            }
            year = parsed.year;
            month = parsed.month;
            day = parsed.day;
            targetName = String(dom.anniversaryName && dom.anniversaryName.value ? dom.anniversaryName.value : '').trim();
            if (!targetName) {
                return { error: '記念日ギフトモードでは「主役のお名前」を入力してください。', field: dom.anniversaryName };
            }
        } else if (mode === 'care_recreation') {
            year = state.year;
            var monthDay = parseMonthDay(dom.careRecreationDate ? dom.careRecreationDate.value : '');
            if (monthDay.error) {
                return { error: monthDay.error, field: dom.careRecreationDate };
            }
            month = monthDay.month;
            day = monthDay.day;
            if (!isValidDate(year, month, day)) {
                return {
                    error: year + '年の' + month + '月' + day + '日は存在しない日付です（' + daysInMonth(year, month) + '日までです）。ダイヤルの年を合わせてください。',
                    field: dom.careRecreationDate
                };
            }
        } else {
            var today = new Date();
            year = state.year;
            month = today.getMonth() + 1;
            day = today.getDate();
        }

        if (!(year >= MIN_YEAR && year <= MAX_YEAR)) {
            return { error: '年は ' + MIN_YEAR + '年〜' + MAX_YEAR + '年の範囲で指定してください。', field: null };
        }

        return { year: year, month: month, day: day, mode: mode, targetName: targetName };
    }

    function showInputError(message, field) {
        clearInvalidFields();
        markInvalidField(field);
        setStreamTitle('⚠️ 入力内容を確認してください');
        setStreamDesc(message);
        if (field && typeof field.focus === 'function') {
            try { field.focus(); } catch (e) { /* noop */ }
        }
        announce('入力エラー: ' + message);
    }

    /* =====================================================================
       再生フロー（POST /api/generate）
       ===================================================================== */
    function setPlayButtonLabel(loading) {
        var button = dom.btnPlayRadio;
        if (!button) { return; }
        button.textContent = '';
        var icon = document.createElement('span');
        icon.textContent = '📻';
        button.appendChild(icon);
        button.appendChild(document.createTextNode(loading ? ' 受信中…' : ' ラジオを再生する'));
    }

    function setLoading(loading) {
        state.isLoading = !!loading;
        var button = dom.btnPlayRadio;
        if (button) {
            button.disabled = !!loading;
            button.classList.toggle('loading', !!loading);
            button.setAttribute('aria-busy', loading ? 'true' : 'false');
        }
        setPlayButtonLabel(!!loading);
        if (dom.tubeBulb) {
            dom.tubeBulb.classList.toggle('is-receiving', !!loading);
        }

        // 進捗は受信中だけ動かす。100% にはしない（renderSuccess でのみ 100%）
        if (state.isLoading) {
            startProgress(state.year, state.mode);
        } else {
            stopProgress();
        }
    }

    function cancelGeneration(silent) {
        var wasLoading = state.isLoading;
        if (state.timeoutTimer) {
            window.clearTimeout(state.timeoutTimer);
            state.timeoutTimer = 0;
        }
        if (state.abortController) {
            safeAbort(state.abortController);
            state.abortController = null;
        }
        state.requestToken += 1; // 古いチェーンの結果を無効化する
        if (wasLoading) {
            setLoading(false);
            stopProgress();
            hideGenerationPanel();
            if (dom.tubeBulb) { dom.tubeBulb.classList.remove('is-receiving'); }
            if (!silent) {
                setStreamTitle('⏹ 受信を中止しました');
                setStreamDesc('別のモードや年を選んで、もう一度お試しください。');
                showEmptyState(true);
                showStateBanner('info', '⏹ 受信を中止しました', 'しばらく時間をおいてから、もう一度お試しください。');
            }
        }
    }

    function extractDetail(payload) {
        if (!payload || typeof payload !== 'object') { return ''; }
        var detail = payload.detail;
        if (typeof detail === 'string' && detail) { return detail; }
        if (Array.isArray(detail)) {
            // FastAPI のバリデーションエラー (422) は detail が配列になる
            return detail.map(function (item) {
                if (!item || typeof item !== 'object') { return String(item); }
                var location = Array.isArray(item.loc) ? item.loc.join(' > ') : '';
                var message = item.msg || JSON.stringify(item);
                return location ? location + ': ' + message : message;
            }).join('\n');
        }
        if (detail) { return JSON.stringify(detail); }
        if (typeof payload.message === 'string' && payload.message) { return payload.message; }
        return '';
    }

    function describeError(status, statusText, payload, rawText) {
        var detail = extractDetail(payload);

        if (status === 503) {
            if (detail && /API|キー|key/i.test(detail)) {
                return '⚠️ Gemini APIキーが未設定のため原稿を生成できません。\n' +
                    'サーバー管理者へ環境変数 RETRO_RADIO_GEMINI_API_KEY の設定を依頼してください。\n' +
                    '（このアプリはブラウザに APIキーを保存しません）\n\nサーバーの応答: ' + detail;
            }
            return '⚠️ サーバー側で原稿生成の準備ができていません（HTTP 503）。APIキーの設定状況を確認してください。' +
                (detail ? '\n\nサーバーの応答: ' + detail : '');
        }

        if (status === 422) {
            return '⚠️ リクエストが不正です（HTTP 422）。\n' +
                '送信する年月日が 1950〜2025 年の範囲に収まっているか確認してください。' +
                (detail ? '\n\nサーバーの応答: ' + detail : '');
        }

        if (status === 404) {
            return '⚠️ 対象のAPIが見つかりません（HTTP 404）。バックエンドが最新かどうか確認してください。' +
                (detail ? '\n\nサーバーの応答: ' + detail : '');
        }

        if (status >= 500) {
            return '⚠️ サーバー側でエラーが発生しました（HTTP ' + status + '）。しばらく待ってから再試行してください。' +
                (detail ? '\n\nサーバーの応答: ' + detail : '');
        }

        if (detail) {
            return '⚠️ ' + detail;
        }
        if (rawText) {
            return '⚠️ 放送に失敗しました（HTTP ' + status + '）。\n\nサーバーの応答:\n' + firstLine(rawText);
        }
        return '⚠️ 放送に失敗しました（HTTP ' + status + (statusText ? ' ' + statusText : '') + '）。';
    }

    // 再試行用に 直前の送信内容を state / localStorage へ残す
    function saveLastRequest(year, month, day, mode, targetName) {
        state.lastRequest = {
            year: year,
            month: month,
            day: day,
            mode: mode,
            targetName: targetName || ''
        };
        try {
            writeStore(RETRY_STORAGE_KEY, JSON.stringify(state.lastRequest));
        } catch (e) {
            // 保存できなくても生成処理は続行する
        }
    }

    function loadLastRequest() {
        var raw = readStore(RETRY_STORAGE_KEY, '');
        if (!raw) { return null; }
        try {
            var parsed = JSON.parse(raw);
            if (parsed && typeof parsed === 'object' && isFiniteNumber(parsed.year)) {
                return parsed;
            }
        } catch (e) {
            // 壊れている値は捨てる
        }
        return null;
    }

    function startGeneration() {
        var form = readForm();
        if (form.error) {
            showInputError(form.error, form.field);
            return;
        }
        clearInvalidFields();

        cancelGeneration(true);

        saveLastRequest(form.year, form.month, form.day, form.mode, form.targetName);

        state.requestToken += 1;
        var token = state.requestToken;
        state.timedOut = false;
        setLoading(true);
        // 送信直前に進捗タイマーを開始する（100% は renderSuccess でのみ）
        startProgress(form.year, form.mode);

        setStreamTitle('📡 ' + form.year + '年の電波を受信中…');
        setStreamDesc('【' + modeLabel(form.mode) + '】原稿を生成しています。ナレーション音声の合成も行うため、30〜60秒ほどお待ちください…');

        var payload = {
            year: form.year,
            month: form.month,
            day: form.day,
            mode: form.mode
        };
        if (form.mode === 'anniversary' && form.targetName) {
            payload.target_name = form.targetName;
        }

        var controller = createController();
        state.abortController = controller;
        state.timeoutTimer = window.setTimeout(function () {
            state.timeoutTimer = 0;
            state.timedOut = true;
            safeAbort(controller);
        }, GENERATE_TIMEOUT_MS);

        window.fetch('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify(payload),
            signal: controller.signal,
            cache: 'no-store'
        })
            .then(function (response) {
                return response.text().then(function (text) {
                    var data = null;
                    if (text) {
                        try { data = JSON.parse(text); } catch (e) { data = null; }
                    }
                    return {
                        ok: response.ok,
                        status: response.status,
                        statusText: response.statusText,
                        data: data,
                        text: text
                    };
                });
            })
            .then(function (result) {
                if (state.requestToken !== token) { return; }
                if (state.timedOut) {
                    renderError({
                        status: 408,
                        statusText: 'Request Timeout',
                        data: null,
                        text: ''
                    });
                    return;
                }
                if (!result.ok) {
                    renderError(result);
                    return;
                }
                renderSuccess(form, result.data);
            })
            .catch(function (error) {
                if (state.requestToken !== token) { return; }
                if (error && error.name === 'AbortError') {
                    if (state.timedOut) {
                        renderError({ status: 408, statusText: 'Request Timeout', data: null, text: '' });
                    } else {
                        setStreamTitle('⏹ 受信を中止しました');
                        setStreamDesc('別のモードや年を選んで、もう一度お試しください。');
                    }
                    return;
                }
                renderError({ status: 0, statusText: '', data: null, text: '' });
            })
            .then(function () {
                // 成否にかかわらず必ず後始末する
                if (state.requestToken !== token) { return; }
                if (state.timeoutTimer) {
                    window.clearTimeout(state.timeoutTimer);
                    state.timeoutTimer = 0;
                }
                state.abortController = null;
                setLoading(false);
            });
    }

    /* =====================================================================
       レスポンスの表示
       ===================================================================== */
    function showOutput() {
        if (dom.broadcastOutput) {
            dom.broadcastOutput.classList.add('active');
        }
    }

    function renderSuccess(form, data) {
        if (!data || typeof data !== 'object') {
            renderError({ status: 200, statusText: '', data: null, text: '' });
            return;
        }
        state.lastResult = data;

        var year = isFiniteNumber(data.year) ? data.year : form.year;
        var month = isFiniteNumber(data.month) ? data.month : form.month;
        var day = isFiniteNumber(data.day) ? data.day : form.day;
        var mode = Object.prototype.hasOwnProperty.call(MODE_LABELS, data.mode) ? data.mode : form.mode;

        // 受信完了: ここで初めて進捗を止め、全ステップ完了にして 100% にする
        stopProgress();
        finishProgress();
        hideErrorState();
        showEmptyState(false);

        // 実測できた成功結果を再試行用に残す
        saveLastRequest(year, month, day, mode, (form && form.targetName) ? form.targetName : '');

        showOutput();

        if (dom.manuscriptTitle) {
            dom.manuscriptTitle.textContent =
                '📻 ' + year + '年' + month + '月' + day + '日 【' + modeLabel(mode) + '】';
        }
        if (dom.manuscriptBody) {
            // 構造化のため innerHTML を使うが、必ず escapeHtml 済みの文字列のみ渡す
            dom.manuscriptBody.innerHTML = renderScriptHtml(data.script);
        }

        var song = (data.song && typeof data.song === 'object') ? data.song : null;
        if (dom.songTitle) {
            dom.songTitle.textContent = (song && song.title) ? String(song.title) : '—';
        }
        if (dom.songArtist) {
            dom.songArtist.textContent = (song && song.artist) ? String(song.artist) : '—';
        }

        renderQuiz(mode === 'care_recreation' ? data.reminiscence_quiz : null);

        if (dom.vinylDisk) {
            dom.vinylDisk.classList.remove('spinning');
            // アニメーションを確実に再開始させるためのリフロー
            void dom.vinylDisk.offsetWidth;
            dom.vinylDisk.classList.add('spinning');
        }

        var scriptLength = typeof data.script === 'string' ? data.script.length : 0;
        var songCount = Array.isArray(data.songs) ? data.songs.length : 0;
        setStreamTitle('🎙️ ラジオ放送中（' + year + '年）');
        setStreamDesc('【' + modeLabel(mode) + '】原稿 ' + scriptLength + '文字・ヒット曲 ' + songCount + '曲を受信しました。トークの終了後に曲プレビューが自動で再生されます。');

        showStateBanner(
            'success',
            '🎉 ' + year + '年の番組を受信しました',
            '【' + modeLabel(mode) + '】原稿 ' + scriptLength + '文字・ヒット曲 ' + songCount + '曲。'
        );

        announce(year + '年' + month + '月' + day + '日の' + modeLabel(mode) + 'を受信しました。');

        startPlayback(data);
    }

    function renderError(info) {
        var status = info && isFiniteNumber(info.status) ? info.status : 0;
        var statusText = info && info.statusText ? info.statusText : '';
        var payload = info && info.data ? info.data : null;
        var rawText = info && info.text ? info.text : '';

        var message;
        if (status === 408) {
            message = '⏱ 応答がタイムアウトしました。\n' +
                '原稿生成には時間がかかるため、後から再試行してください。\n' +
                '（バックエンドが対応している場合は、定型原稿での配信となります）';
        } else if (status === 0) {
            message = '⚠️ サーバーに接続できませんでした。\n' +
                'バックエンドが起動しているか、ネットワーク接続を確認してください。';
        } else {
            message = describeError(status, statusText, payload, rawText);
        }

        stopPlayback();
        renderQuiz(null);

        // 進捗は 100% にしない（応答が具体的に成功した時だけ 100%）
        stopProgress();
        setProgressStep(PROGRESS_STEPS[PROGRESS_STEPS.length - 1], 'failed');
        hideGenerationPanel();
        showEmptyState(false);

        showOutput();
        if (dom.manuscriptTitle) {
            dom.manuscriptTitle.textContent = '⚠️ 受信に失敗しました';
        }
        if (dom.manuscriptBody) {
            dom.manuscriptBody.textContent = message;
        }
        if (dom.songTitle) { dom.songTitle.textContent = '—'; }
        if (dom.songArtist) { dom.songArtist.textContent = '—'; }
        if (dom.vinylDisk) { dom.vinylDisk.classList.remove('spinning'); }
        setVuLevel(0);
        setTubeLit(false);

        // 画面には 1 行の要約を出し、技術的な詳細は折り畳み領域へ出す
        showErrorState(
            status === 408 ? '⏱ 応答がタイムアウトしました' : '放送できません',
            firstLine(message),
            rawText || ('HTTP ' + status + (statusText ? ' ' + statusText : ''))
        );

        setStreamTitle(status === 408 ? '⏱ タイムアウトしました' : '⚠️ 放送できません（HTTP ' + status + '）');
        setStreamDesc(firstLine(message));
        announce(firstLine(message));
    }

    /* =====================================================================
       回想法クイズ
       ===================================================================== */
    function renderQuiz(quiz) {
        var list = Array.isArray(quiz) ? quiz : [];
        var valid = list.filter(function (item) {
            return item && typeof item === 'object' &&
                (item.question || item.answer || item.hint);
        });

        if (!dom.quizList) {
            if (dom.recreationQuizBox) {
                dom.recreationQuizBox.style.display = valid.length ? 'block' : 'none';
            }
            return;
        }
        dom.quizList.textContent = '';

        valid.forEach(function (item, index) {
            var card = document.createElement('details');
            card.className = 'quiz-card';

            var summary = document.createElement('summary');
            summary.className = 'quiz-question-row';

            var badge = document.createElement('span');
            badge.className = 'quiz-q-badge';
            badge.textContent = 'Q' + (index + 1);

            var question = document.createElement('span');
            question.className = 'quiz-question-text';
            question.textContent = String(item.question || '');

            summary.appendChild(badge);
            summary.appendChild(question);

            var body = document.createElement('div');
            body.className = 'quiz-answer-content';

            if (item.hint) {
                var hint = document.createElement('p');
                hint.className = 'quiz-hint';
                hint.textContent = 'ヒント: ' + String(item.hint);
                body.appendChild(hint);
            }
            var answer = document.createElement('p');
            answer.className = 'quiz-answer-text';
            answer.textContent = String(item.answer || '（回答が登録されていません）');
            body.appendChild(answer);

            card.appendChild(summary);
            card.appendChild(body);
            dom.quizList.appendChild(card);
        });

        if (dom.recreationQuizBox) {
            dom.recreationQuizBox.style.display = valid.length ? 'block' : 'none';
        }
    }

    /* =====================================================================
       受信進捗（#generationPanel）
       ---------------------------------------------------------------------
       バックエンドは逐次応答しないため、経過時間から進捗を推測して見せる。
       UI には必ず「目安」の文言を出すこと（#generationPanel の .generation-note）。
       PROGRESS_MAX_PERCENT（95%）を越えず、100% にできるのは renderSuccess だけ。
       ===================================================================== */
    function nowMs() {
        if (window.performance && typeof window.performance.now === 'function') {
            return window.performance.now();
        }
        return (new Date()).getTime();
    }

    function pad2(value) {
        var text = String(value);
        return (text.length < 2 ? '0' + text : text);
    }

    // 経過時間を "0秒" / "1分05秒" / "1時間02分" にする
    function formatElapsed(ms) {
        var total = Math.floor(Math.max(0, Number(ms) || 0) / 1000);
        var hours = Math.floor(total / 3600);
        var minutes = Math.floor((total % 3600) / 60);
        var seconds = total % 60;
        if (hours > 0) {
            return hours + '時間' + pad2(minutes) + '分';
        }
        if (minutes > 0) {
            return minutes + '分' + pad2(seconds) + '秒';
        }
        return seconds + '秒';
    }

    // 進捗ステップ 1 件の表示を切り替える（'pending'|'active'|'completed'|'failed'）
    function setProgressStep(stepKey, status) {
        if (!dom.progressSteps) { return; }
        var steps = dom.progressSteps.querySelectorAll('.progress-step');
        var next = status ? status : 'pending';
        for (var i = 0; i < steps.length; i += 1) {
            var step = steps[i];
            if (!step) { continue; }
            if (step.getAttribute('data-step') === stepKey) {
                step.setAttribute('data-status', next);
            }
        }
    }

    // 4 つのステップを「完了 / 進行中 / 待機」に揃える
    function syncProgressSteps(activeStep) {
        var activeIndex = PROGRESS_STEPS.indexOf(activeStep);
        if (activeIndex < 0) { activeIndex = 0; }
        for (var i = 0; i < PROGRESS_STEPS.length; i += 1) {
            var status = 'pending';
            if (i < activeIndex) {
                status = 'completed';
            } else if (i === activeIndex) {
                status = 'active';
            }
            setProgressStep(PROGRESS_STEPS[i], status);
        }
    }

    // 受信完了: 全ステップを完了にして 100% にする（renderSuccess からのみ呼ぶ）
    function finishProgress() {
        for (var i = 0; i < PROGRESS_STEPS.length; i += 1) {
            setProgressStep(PROGRESS_STEPS[i], 'completed');
        }
        state.progressPercent = 100;
        if (dom.progressFill) { dom.progressFill.style.width = '100%'; }
        if (dom.progressTrack) { dom.progressTrack.setAttribute('aria-valuenow', '100'); }
    }

    function hideGenerationPanel() {
        if (dom.generationPanel) { dom.generationPanel.hidden = true; }
    }

    function startProgress(year, mode) {
        stopProgress();
        state.progressStartedAt = nowMs();
        state.progressPercent = 0;

        if (dom.generationPanel) { dom.generationPanel.hidden = false; }
        if (dom.generationPanelTitle) {
            dom.generationPanelTitle.textContent =
                '📡 ' + (isFiniteNumber(year) ? year : state.year) + '年の電波を受信中…（' + modeLabel(mode) + '）';
        }
        if (dom.generationElapsed) { dom.generationElapsed.textContent = '0秒'; }
        if (dom.progressFill) { dom.progressFill.style.width = '0%'; }
        if (dom.progressTrack) { dom.progressTrack.setAttribute('aria-valuenow', '0'); }
        syncProgressSteps(PROGRESS_STEPS[0]);

        // 受信し始めるので、直前の空状態とエラー状態は片付ける
        hideErrorState();
        showEmptyState(false);

        tickProgress();
        state.progressTimer = window.setInterval(tickProgress, PROGRESS_TICK_MS);
    }

    // 経過時間から進捗率を推測して更新する（実測値ではない）
    function tickProgress() {
        var elapsed = nowMs() - state.progressStartedAt;
        var percent = 0;
        var stepKey = PROGRESS_STEPS[0];

        for (var i = 0; i < PROGRESS_TIMELINE.length; i += 1) {
            var item = PROGRESS_TIMELINE[i];
            if (elapsed >= item.at) {
                percent = item.percent;
                stepKey = item.step;
            }
        }
        if (percent > PROGRESS_MAX_PERCENT) { percent = PROGRESS_MAX_PERCENT; }
        state.progressPercent = percent;

        if (dom.progressFill) { dom.progressFill.style.width = String(percent) + '%'; }
        if (dom.progressTrack) {
            dom.progressTrack.setAttribute('aria-valuenow', String(Math.round(percent)));
        }
        if (dom.generationElapsed) { dom.generationElapsed.textContent = formatElapsed(elapsed); }
        syncProgressSteps(stepKey);
    }

    function stopProgress() {
        if (state.progressTimer) {
            window.clearInterval(state.progressTimer);
            state.progressTimer = 0;
        }
    }

    /* =====================================================================
       状態表示（バナー / 空状態 / エラー状態 / 使い方ガイド）
       ---------------------------------------------------------------------
       docs/state_design_system.md の 4 種（success / warning / error / info）。
       サーバー由来の文字列は textContent で描画する（innerHTML は使わない）。
       ===================================================================== */
    function showStateBanner(kind, title, message) {
        if (!dom.stateBanner) { return; }
        var key = Object.prototype.hasOwnProperty.call(STATE_BANNER_ICONS, kind) ? kind : 'info';

        dom.stateBanner.setAttribute('data-kind', key);
        if (dom.stateBannerIcon) { dom.stateBannerIcon.textContent = STATE_BANNER_ICONS[key]; }
        if (dom.stateBannerTitle) {
            dom.stateBannerTitle.textContent = title ? String(title) : '';
        }
        if (dom.stateBannerMessage) {
            dom.stateBannerMessage.textContent = message ? String(message) : '';
        }
        dom.stateBanner.hidden = false;
        announce((title ? String(title) + '。' : '') + (message ? String(message) : ''));
    }

    function hideStateBanner() {
        if (!dom.stateBanner) { return; }
        dom.stateBanner.hidden = true;
        if (dom.stateBannerTitle) { dom.stateBannerTitle.textContent = ''; }
        if (dom.stateBannerMessage) { dom.stateBannerMessage.textContent = ''; }
    }

    function showEmptyState(visible) {
        if (!dom.emptyState) { return; }
        dom.emptyState.hidden = !visible;
    }

    function showErrorState(title, message, detail) {
        state.errorDetail = detail ? String(detail) : '';
        if (!dom.errorState) { return; }

        if (dom.errorStateIcon) { dom.errorStateIcon.textContent = '⚠️'; }
        if (dom.errorStateTitle) {
            dom.errorStateTitle.textContent = title ? String(title) : '放送できません';
        }
        if (dom.errorStateMessage) {
            dom.errorStateMessage.textContent = message ? String(message) : '';
        }
        if (dom.errorStateDetail) {
            dom.errorStateDetail.textContent = state.errorDetail;
            // 詳細は「技術的な詳細を見る」で開くまで隠す
            dom.errorStateDetail.hidden = true;
        }
        if (dom.btnToggleErrorDetail) {
            dom.btnToggleErrorDetail.textContent = '技術的な詳細を見る';
            dom.btnToggleErrorDetail.setAttribute('aria-expanded', 'false');
        }
        dom.errorState.hidden = false;
        showEmptyState(false);
    }

    function hideErrorState() {
        state.errorDetail = '';
        if (dom.errorState) { dom.errorState.hidden = true; }
        if (dom.errorStateDetail) {
            dom.errorStateDetail.textContent = '';
            dom.errorStateDetail.hidden = true;
        }
        if (dom.btnToggleErrorDetail) {
            dom.btnToggleErrorDetail.textContent = '技術的な詳細を見る';
            dom.btnToggleErrorDetail.setAttribute('aria-expanded', 'false');
        }
    }

    function toggleErrorDetail() {
        if (!dom.errorStateDetail) { return; }
        var open = dom.errorStateDetail.hidden;
        dom.errorStateDetail.hidden = !open;
        if (dom.btnToggleErrorDetail) {
            dom.btnToggleErrorDetail.textContent = open ? '技術的な詳細を隠す' : '技術的な詳細を見る';
            dom.btnToggleErrorDetail.setAttribute('aria-expanded', open ? 'true' : 'false');
        }
    }

    function openGuide() {
        var dialog = dom.guideDialog;
        if (!dialog) { return; }
        try {
            if (typeof dialog.showModal === 'function') {
                dialog.showModal();
                return;
            }
            if (typeof dialog.show === 'function') {
                dialog.show();
                return;
            }
        } catch (e) {
            // 既に表示中などの場合は下の open 属性で代用する
        }
        dialog.setAttribute('open', 'open');
    }

    function closeGuide() {
        var dialog = dom.guideDialog;
        if (!dialog) { return; }
        try {
            if (typeof dialog.close === 'function' && dialog.open) {
                dialog.close();
                return;
            }
        } catch (e) {
            // 旧ブラウザは removeAttribute へフォールバックする
        }
        dialog.removeAttribute('open');
    }

    function bindGuide() {
        if (!dom.guideDialog) { return; }
        // Esc などで閉じた場合も close イベントで状態を合わせる
        dom.guideDialog.addEventListener('close', function () {
            announce('使い方ガイドを閉じました。');
        });
        // 背景クリックで閉じる
        dom.guideDialog.addEventListener('click', function (event) {
            if (event.target === dom.guideDialog) {
                closeGuide();
            }
        });
    }

    /* =====================================================================
       連続オーディオ再生
       ---------------------------------------------------------------------
       バックエンドの playlist を 1 つの <audio> 要素で順番に再生する。
       別オリジン（iTunes プレビュー）は CORS が通らない可能性があるため、
       MediaElementSource は「キュー全体が同一オリジン」のときだけ作る。
       ===================================================================== */
    function buildQueue(data) {
        var items = [];

        function pushTalk(url, title) {
            var absolute = toAbsoluteUrl(url);
            if (!absolute) { return; }
            items.push({ kind: TALK, url: absolute, title: title || 'ナレーション', artist: '' });
        }

        function pushSong(song) {
            if (!song || typeof song !== 'object') { return; }
            // フォールバック曲（preview_url が無い）は再生できないのでスキップ
            if (!song.preview_url) { return; }
            var absolute = toAbsoluteUrl(song.preview_url);
            if (!absolute) { return; }
            items.push({
                kind: SONG,
                url: absolute,
                title: String(song.title || 'ヒット曲'),
                artist: String(song.artist || '')
            });
        }

        var playlist = Array.isArray(data && data.playlist) ? data.playlist : [];
        playlist.forEach(function (raw) {
            if (!raw || typeof raw !== 'object') { return; }
            // 契約では "TALK" / "SONG" だが、実装により "talk" / "song" の
            // 可能性があるため必ず大文字小文字を正規化して判定する
            var type = String(raw.type === null || raw.type === undefined ? '' : raw.type)
                .trim().toUpperCase();

            if (type === TALK) {
                var audioUrl = raw.audio_url;
                if (!audioUrl && raw.metadata && typeof raw.metadata === 'object') {
                    audioUrl = raw.metadata.audio_url;
                }
                pushTalk(audioUrl, raw.title);
            } else if (type === SONG) {
                pushSong({
                    title: raw.title,
                    artist: raw.artist,
                    preview_url: raw.preview_url,
                    is_fallback: raw.is_fallback
                });
            } else {
                // type が未知の場合は可能是 URL から推測する
                if (raw.audio_url) {
                    pushTalk(raw.audio_url, raw.title);
                } else {
                    pushSong(raw);
                }
            }
        });

        if (!items.length) {
            // playlist が無い/空の場合のフォールバック: 全文 TTS → 曲
            if (data && data.audio_url) {
                pushTalk(data.audio_url, '番組全文');
            }
            var songs = Array.isArray(data && data.songs) ? data.songs : [];
            songs.forEach(pushSong);
        }

        return items;
    }

    function createAudioElement() {
        var audio = document.createElement('audio');
        audio.preload = 'auto';
        audio.setAttribute('playsinline', '');
        // display:none は Safari でメディア再生が停止しうるため極小オフスクリーン配置にする
        audio.style.cssText = 'position:absolute;left:-9999px;top:0;width:1px;height:1px;opacity:0;pointer-events:none;';
        audio.addEventListener('ended', onTrackEnded);
        audio.addEventListener('play', onAudioPlay);
        audio.addEventListener('pause', onAudioPause);
        audio.addEventListener('error', onAudioError);
        // 再生位置の反映（#seekBar / #seekCurrent / #seekDuration）
        audio.addEventListener('timeupdate', updateSeekBar);
        audio.addEventListener('loadedmetadata', updateSeekBar);
        audio.addEventListener('seeked', updateSeekBar);
        // 音量・ミュートが変わったときはスライダーとボタンへ反映する
        audio.addEventListener('volumechange', onAudioVolumeChange);
        document.body.appendChild(audio);
        return audio;
    }

    // 音量・ミュートは <audio> が正なので、こっちからスライダーとボタンへ同期する
    function onAudioVolumeChange() {
        if (!state.audio) { return; }
        state.volume = clampVolume(state.audio.volume);
        state.muted = !!state.audio.muted;
        if (dom.volumeControl) {
            dom.volumeControl.value = String(state.volume);
        }
        if (dom.btnMute) {
            dom.btnMute.textContent = state.muted ? '🔇' : '🔊';
            dom.btnMute.setAttribute('aria-pressed', state.muted ? 'true' : 'false');
        }
    }

    /**
     * MediaElementSource を作り直せるよう、解析ノードだけを破棄して
     * <audio> 要素を新しく差し替える。同一オリジン専用に解析していた
     * 要素は、別オリジン（iTunes プレビュー）を鳴ら وقالت無音になるため。
     */
    function replaceAudioElement() {
        if (state.audio && state.audio.parentNode) {
            try { state.audio.pause(); } catch (e) { /* noop */ }
            state.audio.removeEventListener('ended', onTrackEnded);
            state.audio.removeEventListener('play', onAudioPlay);
            state.audio.removeEventListener('pause', onAudioPause);
            state.audio.removeEventListener('error', onAudioError);
            state.audio.removeEventListener('timeupdate', updateSeekBar);
            state.audio.removeEventListener('loadedmetadata', updateSeekBar);
            state.audio.removeEventListener('seeked', updateSeekBar);
            state.audio.removeEventListener('volumechange', onAudioVolumeChange);
            state.audio.parentNode.removeChild(state.audio);
        }
        state.sourceNode = null;
        state.analyser = null;
        state.analyserAttached = false;
        state.audio = createAudioElement();
    }

    function startPlayback(data) {
        stopPlayback();
        state.queue = buildQueue(data);
        state.index = -1;
        state.endedCount = 0;
        state.errorStreak = 0;
        state.userPaused = false;
        state.needGesture = false;
        state.finished = false;
        state.prefetchedUrl = '';
        // キューが確定した時点でプレイリストを描画する
        renderPlaylist(state.queue);

        if (!state.queue.length) {
            setVuLevel(0);
            setTubeLit(false);
            syncAudioButton();
            setStreamDesc('再生できる音源が見つかりませんでした。原稿のみ表示しています。');
            return;
        }

        var allSameOrigin = state.queue.every(function (track) {
            return isSameOrigin(track.url);
        });
        if (state.analyserAttached && !allSameOrigin) {
            replaceAudioElement();
        }
        state.allSameOrigin = allSameOrigin;
        resumeAudioContext();
        ensureAnalyser();
        playIndex(0, false);
    }

    function stopPlayback() {
        if (state.skipTimer) {
            window.clearTimeout(state.skipTimer);
            state.skipTimer = 0;
        }
        // removeAttribute('src') + load() が error を発しうるため、
        // 先にキューを空にして onAudioError のスキップ処理を発火させない
        state.queue = [];
        state.index = -1;
        stopVu();
        if (state.audio) {
            try { state.audio.pause(); } catch (e) { /* noop */ }
            try {
                state.audio.removeAttribute('src');
                state.audio.load();
            } catch (e) { /* noop */ }
        }
        state.endedCount = 0;
        state.prefetchedUrl = '';
        state.userPaused = false;
        state.needGesture = false;
        state.finished = false;
        setTubeLit(false);
        syncAudioButton();
        // 停止したらトラック表示とプレイリストを初期状態へ戻す
        updateSeekBar();
        renderPlaylist([]);
        renderPlayerUI();
    }

    function playIndex(index, isSkip) {
        if (state.skipTimer) {
            window.clearTimeout(state.skipTimer);
            state.skipTimer = 0;
        }
        if (!isSkip) {
            state.endedCount = 0;
        }
        if (index < 0 || index >= state.queue.length) {
            onPlaylistEnd();
            return;
        }

        var track = state.queue[index];
        state.index = index;
        state.finished = false;
        updateTrackMeta(track, index);
        // トラック情報が画面（#trackTitle 等）にも出るようにする
        applyStreamMeta(track, index);
        updateSeekBar();

        if (state.audio) {
            try {
                state.audio.src = track.url;
                state.audio.load();
            } catch (e) {
                onAudioError();
                return;
            }
        }
        prefetchTrack(index + 1);
        tryPlay();
    }

    function updateTrackMeta(track, index) {
        var kindLabel = (track.kind === SONG) ? '🎵 ヒット曲' : '🎙️ ナレーション';
        var text = kindLabel + ' ' + (index + 1) + '/' + state.queue.length + '：' + track.title;
        if (track.artist) { text += ' / ' + track.artist; }
        text += '（終了後に自動で次へ）';
        setStreamDesc(text);

        if (track.kind === SONG) {
            if (dom.songTitle) { dom.songTitle.textContent = track.title || '—'; }
            if (dom.songArtist) { dom.songArtist.textContent = track.artist || '—'; }
            if (dom.vinylDisk) { dom.vinylDisk.classList.add('now-playing'); }
        } else if (dom.vinylDisk) {
            dom.vinylDisk.classList.remove('now-playing');
        }

        // プレイヤー領域のトラック表示・ボタン状態を同期する
        // （再生中のトラック情報が画面に一切出ない問題の修正点）
        renderPlayerUI();
    }

    // 再生中トラックの上部ステータス表示（#streamStatusTitle / #streamStatusDesc）を更新する
    function applyStreamMeta(track, index) {
        var kindLabel = (track.kind === SONG) ? '🎵 ヒット曲' : '🎙️ ナレーション';
        setStreamTitle('📻 ラジオ放送中（' + state.year + '年）— ' + kindLabel +
            ' ' + (index + 1) + '/' + state.queue.length);
        var text = '▶ ' + (track.title || '—');
        if (track.artist) { text += ' / ' + track.artist; }
        text += '（終了後に自動で次へ）';
        setStreamDesc(text);
    }

    // 次のトラックの URL を先読みして切替の空白を減らす
    function prefetchTrack(index) {
        if (index < 0 || index >= state.queue.length) { return; }
        var url = state.queue[index].url;
        if (!url || url === state.prefetchedUrl) { return; }
        state.prefetchedUrl = url;
        try {
            var preloader = new window.Audio();
            preloader.preload = 'auto';
            preloader.src = url;
            if (typeof preloader.load === 'function') { preloader.load(); }
        } catch (e) {
            // 先読みできなくても再生自体には影響しない
        }
    }

    function tryPlay() {
        if (!state.audio) { return; }
        if (state.userPaused) {
            syncAudioButton();
            return;
        }
        var promise;
        try {
            promise = state.audio.play();
        } catch (e) {
            markNeedsGesture();
            return;
        }
        if (promise && typeof promise.then === 'function') {
            promise.then(noop).catch(function (error) {
                // src 差し替えによる AbortError は連打時の想定内
                if (error && error.name === 'AbortError') { return; }
                markNeedsGesture();
            });
        }
    }

    function markNeedsGesture() {
        state.needGesture = true;
        syncAudioButton();
        setStreamDesc('▶ ボタンを押して番組の再生を開始してください（ブラウザの自動再生制限）。');
    }

    function onPlaylistEnd() {
        // 連続再生が有効なら先頭からもう一度流し、なければ「放送終了」を見せる
        if (state.loopEnabled && state.queue.length) {
            state.finished = false;
            setStreamTitle('📻 連続再生中（' + state.year + '年）');
            setStreamDesc('番組を先頭から続けて再生しています。');
            restartPlayback();
            return;
        }

        state.finished = true;
        state.index = state.queue.length;
        stopVu();
        setTubeLit(false);
        syncAudioButton();
        setStreamTitle('📻 番組を終了しました（' + state.queue.length + 'トラック）');
        setStreamDesc('もう一度聴く場合は ▶ を押してください。');
        // 放送終了の表示也跟着追跡情報は「待機中」へ戻す
        updateSeekBar();
        renderPlayerUI();
    }

    function restartPlayback() {
        if (!state.queue.length) { return; }
        state.finished = false;
        state.userPaused = false;
        state.needGesture = false;
        state.errorStreak = 0;
        playIndex(0, false);
    }

    function onTrackEnded() {
        state.endedCount += 1;
        playIndex(state.index + 1, false);
    }

    function onAudioError() {
        if (!state.queue.length || state.index < 0) { return; }
        var track = state.queue[state.index];
        state.errorStreak += 1;
        if (state.errorStreak >= Math.max(2, state.queue.length)) {
            stopPlayback();
            setTubeLit(false);
            setStreamTitle('⚠️ 音源を読み込めませんでした');
            setStreamDesc('サーバー上の音声ファイルを再生成してから、もう一度お試しください。');
            updateSeekBar();
            return;
        }
        setStreamDesc('「' + (track ? track.title : '音源') + '」を読み込めませんでした。次のトラックへスキップします。');
        // 読み込めなかったトラックの残量を画面へ戻しておく
        updateSeekBar();
        state.skipTimer = window.setTimeout(function () {
            state.skipTimer = 0;
            playIndex(state.index + 1, true);
        }, 500);
    }

    function onAudioPlay() {
        state.errorStreak = 0;
        state.needGesture = false;
        resumeAudioContext();
        syncAudioButton();
        setTubeLit(true);
        // 一時停止で VU のタイマーを止めているので、再生再開時に必ず復帰させる
        if (state.vuTimer === 0) {
            if (state.analyser) { startVuAnalyser(); } else { startVuSimulation(); }
        }
    }

    function onAudioPause() {
        syncAudioButton();
        setTubeLit(false);
        stopVu();
    }

    function isPlaying() {
        return !!(state.audio && !state.audio.paused && !state.audio.ended &&
            state.index >= 0 && state.index < state.queue.length);
    }

    function syncAudioButton() {
        var button = dom.btnAudioAction;
        if (!button) { return; }
        var playing = isPlaying();
        button.textContent = playing ? '⏸️' : '▶️';
        button.setAttribute('aria-label', playing ? '一時停止' : '再生');
        button.setAttribute('aria-pressed', playing ? 'true' : 'false');
        // 前後トラックのボタンはキューの状況に応じて押せるようにする
        syncTrackNavButtons();
    }

    // 先頭 / 末尾 では前後に進めないようにする
    // （CSS に :disabled の定義が無いため .disabled クラスも併せて付ける）
    function syncTrackNavButtons() {
        var total = state.queue.length;
        var hasPrev = total > 0 && state.index > 0;
        var hasNext = total > 0 && state.index >= 0 && state.index < total - 1;

        setControlDisabled(dom.btnPrevTrack, !hasPrev);
        setControlDisabled(dom.btnNextTrack, !hasNext);
        setControlDisabled(dom.btnReplayTrack, total === 0 || state.index < 0);
    }

    function setControlDisabled(button, disabled) {
        if (!button) { return; }
        if (disabled) {
            button.disabled = true;
            button.classList.add('disabled');
        } else {
            button.disabled = false;
            button.classList.remove('disabled');
        }
    }

    function toggleAudio() {
        if (!state.audio) { return; }
        if (state.finished || state.index >= state.queue.length) {
            restartPlayback();
            return;
        }
        if (state.audio.paused) {
            state.userPaused = false;
            tryPlay();
        } else {
            state.userPaused = true;
            state.audio.pause();
        }
        syncAudioButton();
    }

    /* =====================================================================
       プレイヤー UI（#playerCard）
       ---------------------------------------------------------------------
       再生位置・前後トラック・ミュート・音量・連続再生・プレイリストを司る。
       バックエンド由来の文字列は必ず textContent で描画し、innerHTML は使わない。
       ===================================================================== */

    // シークバーをドラッグ中は timeupdate に value を上書きさせない
    var seekDragging = false;

    // 秒数を "0:00" / "1:23" / "1:02:03" にする
    // （SubD の formatElapsed / pad2 とは別系統の整形なので自前で持つ）
    function formatTime(seconds) {
        var total = Math.floor(Math.max(0, Number(seconds) || 0));
        var hours = Math.floor(total / 3600);
        var minutes = Math.floor((total % 3600) / 60);
        var rest = total % 60;
        var minText = (minutes < 10 ? '0' : '') + minutes;
        var secText = (rest < 10 ? '0' : '') + rest;
        if (hours > 0) {
            return hours + ':' + minText + ':' + secText;
        }
        return minText + ':' + secText;
    }

    // 音量は 0〜1 に丸める（小数2桁）
    function clampVolume(value) {
        var volume = Number(value);
        if (!isFinite(volume)) { return 0; }
        if (volume < 0) { return 0; }
        if (volume > 1) { return 1; }
        return Math.round(volume * 100) / 100;
    }

    // 再生位置と残り時間を #seekBar / #seekCurrent / #seekDuration へ反映する
    function updateSeekBar() {
        var duration = 0;
        var current = 0;
        if (state.audio) {
            if (isFiniteNumber(state.audio.duration) && state.audio.duration > 0) {
                duration = state.audio.duration;
            }
            if (isFiniteNumber(state.audio.currentTime) && state.audio.currentTime >= 0) {
                current = state.audio.currentTime;
            }
        }
        if (dom.seekDuration) { dom.seekDuration.textContent = formatTime(duration); }
        if (dom.seekCurrent) { dom.seekCurrent.textContent = formatTime(current); }
        if (dom.seekBar) {
            // レンジの最大値を実際の尺に合わせてから位置を乗せる
            dom.seekBar.max = String(Math.max(1, Math.ceil(duration)));
            if (!seekDragging) {
                dom.seekBar.value = String(Math.min(current, Math.max(1, Math.ceil(duration))));
            }
            dom.seekBar.setAttribute('aria-valuetext',
                formatTime(current) + ' / ' + formatTime(duration));
        }
    }

    // シークバーの値をそのまま再生位置として反映する
    function seekFromRange() {
        if (!dom.seekBar || !state.audio) { return; }
        var value = Number(dom.seekBar.value);
        if (!isFinite(value)) { return; }
        try { state.audio.currentTime = value; } catch (e) { /* noop */ }
    }

    // 音量を 0〜1 で適用し、localStorage へ保存する
    function setVolume(value) {
        var volume = clampVolume(value);
        state.volume = volume;
        if (state.audio) {
            try { state.audio.volume = volume; } catch (e) { /* noop */ }
        }
        if (dom.volumeControl) { dom.volumeControl.value = String(volume); }
        writeStore(VOLUME_STORAGE_KEY, String(volume));
        if (dom.btnMute) {
            dom.btnMute.textContent = state.muted ? '🔇' : '🔊';
            dom.btnMute.setAttribute('aria-pressed', state.muted ? 'true' : 'false');
        }
    }

    // ミュートの ON / OFF を切り替える
    function toggleMute() {
        state.muted = !state.muted;
        if (state.audio) {
            try { state.audio.muted = state.muted; } catch (e) { /* noop */ }
        }
        if (dom.btnMute) {
            dom.btnMute.textContent = state.muted ? '🔇' : '🔊';
            dom.btnMute.setAttribute('aria-pressed', state.muted ? 'true' : 'false');
        }
        announce(state.muted ? 'ミュートしました。' : 'ミュートを解除しました。');
    }

    // 連続再生の ON / OFF を切り替えて保存する
    function setLoopEnabled(enabled) {
        state.loopEnabled = !!enabled;
        if (dom.btnLoop) {
            dom.btnLoop.setAttribute('aria-pressed', state.loopEnabled ? 'true' : 'false');
        }
        writeStore(LOOP_STORAGE_KEY, state.loopEnabled ? '1' : '0');
        announce(state.loopEnabled
            ? '連続再生を有効にしました。'
            : '連続再生を無効にしました。');
    }

    // 保存済みの音量・連続再生を復元する
    function applyStoredAudioPrefs() {
        var storedVolume = readStore(VOLUME_STORAGE_KEY, '');
        if (storedVolume !== '' && storedVolume !== null && storedVolume !== undefined) {
            state.volume = clampVolume(storedVolume);
        }
        state.loopEnabled = (readStore(LOOP_STORAGE_KEY, '0') === '1');
        // ミュートは保存しない（毎回オフから始める）
        state.muted = false;

        if (state.audio) {
            try {
                state.audio.volume = state.volume;
                state.audio.muted = false;
            } catch (e) { /* noop */ }
        }
        if (dom.volumeControl) { dom.volumeControl.value = String(state.volume); }
        if (dom.btnLoop) {
            dom.btnLoop.setAttribute('aria-pressed', state.loopEnabled ? 'true' : 'false');
        }
        if (dom.btnMute) {
            dom.btnMute.textContent = '🔊';
            dom.btnMute.setAttribute('aria-pressed', 'false');
        }
    }

    // 再生中のトラック情報とボタン状態を #playerCard へ同期する
    function renderPlayerUI() {
        var total = state.queue.length;
        var hasTrack = total > 0 && state.index >= 0 && state.index < total;
        var track = hasTrack ? state.queue[state.index] : null;

        if (dom.trackIndex) {
            dom.trackIndex.textContent = hasTrack ? (state.index + 1) + ' / ' + total : '- / -';
        }
        if (dom.trackTitle) {
            if (track) {
                dom.trackTitle.textContent = track.title || '—';
            } else if (state.finished) {
                dom.trackTitle.textContent = '放送を終了しました';
            } else {
                dom.trackTitle.textContent = '番組を待機中です';
            }
        }
        if (dom.trackArtist) {
            if (!track) {
                dom.trackArtist.textContent = '—';
            } else if (track.artist) {
                dom.trackArtist.textContent = track.artist;
            } else {
                dom.trackArtist.textContent = (track.kind === SONG) ? 'ヒット曲' : 'ナレーション';
            }
        }

        syncPlaylistActive();
        syncTrackNavButtons();
        if (dom.btnLoop) {
            dom.btnLoop.setAttribute('aria-pressed', state.loopEnabled ? 'true' : 'false');
        }
        if (dom.btnMute) {
            dom.btnMute.textContent = state.muted ? '🔇' : '🔊';
            dom.btnMute.setAttribute('aria-pressed', state.muted ? 'true' : 'false');
        }
    }

    // #playlistList をキューから作り直す
    function renderPlaylist(trackQueue) {
        var list = dom.playlistList;
        if (!list) { return; }
        var tracks = Array.isArray(trackQueue) ? trackQueue : [];
        while (list.firstChild) {
            list.removeChild(list.firstChild);
        }
        for (var i = 0; i < tracks.length; i += 1) {
            list.appendChild(buildPlaylistItem(tracks[i], i));
        }
        syncPlaylistActive();
    }

    // 1 件のプレイリスト行を作る（テキストは textContent で入れる）
    function buildPlaylistItem(track, index) {
        var item = document.createElement('li');
        // <button> にして UA スタイルが漏れないようにする
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'playlist-item';
        button.setAttribute('data-track-index', String(index));

        var num = document.createElement('span');
        num.className = 'playlist-item-index';
        num.textContent = String(index + 1);

        var label = document.createElement('span');
        label.className = 'playlist-item-label';
        label.textContent = (track.kind === SONG ? '🎵 ' : '🎙️ ') + (track.title || '—');

        button.appendChild(num);
        button.appendChild(label);

        if (track.artist) {
            var artist = document.createElement('span');
            artist.className = 'playlist-item-artist';
            artist.textContent = track.artist;
            button.appendChild(artist);
        }

        button.setAttribute('aria-label',
            'トラック' + (index + 1) + '：' + (track.title || '無題'));
        button.addEventListener('click', onPlaylistItemClick);
        item.appendChild(button);
        return item;
    }

    // プレイリストの行クリックでそのまま再生する
    function onPlaylistItemClick() {
        var raw = this.getAttribute('data-track-index');
        var index = Number(raw);
        if (!isFinite(index)) { return; }
        state.userPaused = false;
        state.needGesture = false;
        playIndex(index, true);
    }

    // 再生中トラックに .active を付ける
    function syncPlaylistActive() {
        var list = dom.playlistList;
        if (!list) { return; }
        var items = list.getElementsByClassName('playlist-item');
        for (var i = 0; i < items.length; i += 1) {
            if (i === state.index) {
                items[i].classList.add('active');
                items[i].setAttribute('aria-current', 'true');
            } else {
                items[i].classList.remove('active');
                items[i].removeAttribute('aria-current');
            }
        }
    }

    // 前の / 次のトラックへ移動する
    function gotoRelativeTrack(step) {
        var next = state.index + step;
        if (!state.queue.length) { return; }
        if (next < 0 || next >= state.queue.length) { return; }
        state.userPaused = false;
        state.needGesture = false;
        playIndex(next, true);
    }

    // 現在のトラックを先頭からもう一度再生する
    function replayCurrentTrack() {
        if (!state.audio || !state.queue.length) { return; }
        if (state.index < 0 || state.index >= state.queue.length) { return; }
        try { state.audio.currentTime = 0; } catch (e) { /* noop */ }
        state.userPaused = false;
        state.needGesture = false;
        tryPlay();
        updateSeekBar();
        announce('このトラックをもう一度再生します。');
    }

    // プレイヤーのコントロールを一度だけ登録する
    // （bindEvents() から引数なしで呼ばれるため、引数は定義しない）
    function bindPlayerControls() {
        if (state.playerBound) { return; }
        state.playerBound = true;

        // 保存済みの音量・連続再生を復元してから画面を初期描画する
        applyStoredAudioPrefs();

        if (dom.btnPrevTrack) {
            dom.btnPrevTrack.addEventListener('click', function () { gotoRelativeTrack(-1); });
        }
        if (dom.btnNextTrack) {
            dom.btnNextTrack.addEventListener('click', function () { gotoRelativeTrack(1); });
        }
        if (dom.btnReplayTrack) {
            dom.btnReplayTrack.addEventListener('click', replayCurrentTrack);
        }
        if (dom.btnMute) {
            dom.btnMute.addEventListener('click', toggleMute);
        }
        if (dom.btnLoop) {
            dom.btnLoop.addEventListener('click', function () {
                setLoopEnabled(!state.loopEnabled);
            });
        }
        if (dom.volumeControl) {
            dom.volumeControl.addEventListener('input', function () {
                setVolume(Number(this.value));
            });
        }
        if (dom.seekBar) {
            // ドラッグ中(input)はその場で反映し、離した時(change)に確定させる
            dom.seekBar.addEventListener('input', function () {
                seekDragging = true;
                seekFromRange();
                updateSeekBar();
            });
            dom.seekBar.addEventListener('change', function () {
                seekFromRange();
                seekDragging = false;
                updateSeekBar();
            });
            dom.seekBar.addEventListener('pointerup', function () { seekDragging = false; });
        }

        renderPlayerUI();
        updateSeekBar();
    }

    /* =====================================================================
       VU メーター / 真空管
       ===================================================================== */
    function setVuLevel(level) {
        if (!dom.vuMeter) { return; }
        var segments = dom.vuMeter.querySelectorAll('.vu-segment');
        if (!segments.length) { return; }
        var clamped = level;
        if (!(clamped >= 0)) { clamped = 0; }
        if (clamped > 1) { clamped = 1; }
        var lit = Math.round(clamped * segments.length);
        for (var i = 0; i < segments.length; i += 1) {
            var className = 'vu-segment';
            if (i < lit) {
                className += (i < 5) ? ' active-green' : (i < 7 ? ' active-amber' : ' active-red');
            }
            segments[i].className = className;
        }
    }

    function setTubeLit(lit) {
        if (!dom.tubeBulb) { return; }
        dom.tubeBulb.classList.toggle('is-off', !lit);
    }

    function stopVu() {
        if (state.vuTimer) {
            window.clearInterval(state.vuTimer);
            state.vuTimer = 0;
        }
        setVuLevel(0);
        setTubeLit(false);
    }

    function resumeAudioContext() {
        if (!state.audioCtx) { return; }
        try {
            if (state.audioCtx.state === 'suspended') {
                var resumed = state.audioCtx.resume();
                if (resumed && typeof resumed.catch === 'function') { resumed.catch(noop); }
            }
        } catch (e) { /* noop */ }
    }

    function ensureAnalyser() {
        // 別オリジンのトラックが混在する場合は MediaElementSource を作らない。
        // 一度作るとその要素の出力は恒久的に AudioContext 経由になり、
        // 後から別オリジン（iTunes プレビュー）を再生すると無音になるため。
        if (!state.allSameOrigin || !state.audio) {
            startVuSimulation();
            return;
        }
        var Ctor = window.AudioContext || window.webkitAudioContext;
        if (typeof Ctor !== 'function') {
            startVuSimulation();
            return;
        }
        try {
            if (!state.audioCtx) { state.audioCtx = new Ctor(); }
            resumeAudioContext();
            if (!state.sourceNode) {
                state.sourceNode = state.audioCtx.createMediaElementSource(state.audio);
                state.analyser = state.audioCtx.createAnalyser();
                state.analyser.fftSize = 256;
                state.analyser.smoothingTimeConstant = 0.72;
                state.sourceNode.connect(state.analyser);
                state.analyser.connect(state.audioCtx.destination);
                state.analyserAttached = true;
            }
            startVuAnalyser();
        } catch (e) {
            state.audioCtx = null;
            state.sourceNode = null;
            state.analyser = null;
            state.analyserAttached = false;
            startVuSimulation();
        }
    }

    function startVuAnalyser() {
        if (state.vuTimer) { window.clearInterval(state.vuTimer); }
        if (!state.analyser) { startVuSimulation(); return; }
        var data = new Uint8Array(state.analyser.frequencyBinCount);
        state.vuTimer = window.setInterval(function () {
            if (!state.analyser) {
                stopVu();
                return;
            }
            state.analyser.getByteFrequencyData(data);
            var sum = 0;
            for (var i = 0; i < data.length; i += 1) { sum += data[i]; }
            var level = (sum / data.length) / 255;
            setVuLevel(level * 2.2);
            setTubeLit(isPlaying());
        }, 100);
    }

    // Web Audio が使えない場合のタイムベース演出
    function startVuSimulation() {
        if (state.vuTimer) { window.clearInterval(state.vuTimer); }
        var tick = 0;
        state.vuTimer = window.setInterval(function () {
            if (!isPlaying()) {
                setVuLevel(0);
                setTubeLit(false);
                return;
            }
            tick += 1;
            var wave = (Math.sin(tick / 6) * 0.5) + 0.5;
            var level = 0.22 + (wave * 0.4) + (Math.random() * 0.4);
            setVuLevel(level);
            setTubeLit(true);
        }, 120);
    }

    /* =====================================================================
       シニア表示 / 縦書き / 印刷
       ===================================================================== */
    function applySeniorMode(enabled) {
        var on = !!enabled;
        document.body.classList.toggle('senior-mode', on);
        document.documentElement.classList.toggle('senior-mode', on);
        if (dom.seniorToggle) { dom.seniorToggle.checked = on; }
        if (dom.appIcon) { dom.appIcon.textContent = on ? '📻✨' : '📻'; }
    }

    function applyVerticalWriting(enabled) {
        var on = !!enabled;
        if (dom.manuscriptBody) {
            dom.manuscriptBody.classList.toggle('vertical-mode', on);
        }
        if (dom.btnToggleWriting) {
            dom.btnToggleWriting.textContent = on ? '横書きに戻す' : '縦書き原稿にする';
            dom.btnToggleWriting.setAttribute('aria-pressed', on ? 'true' : 'false');
        }
    }

    function openAllDetails() {
        if (!dom.quizList) { return []; }
        var details = Array.prototype.slice.call(dom.quizList.querySelectorAll('details'));
        var previous = details.map(function (item) { return item.open; });
        details.forEach(function (item) { item.open = true; });
        return previous;
    }

    function restoreDetails(previous) {
        if (!previous || !dom.quizList) { return; }
        var details = dom.quizList.querySelectorAll('details');
        for (var i = 0; i < details.length && i < previous.length; i += 1) {
            details[i].open = previous[i];
        }
    }

    function handleAfterPrint() {
        restoreDetails(state.printState);
        state.printState = null;
    }

    function handlePrint() {
        state.printState = openAllDetails();
        // Ctrl+P 経由（beforeprint 側）で開いた details も元へ戻すため常駐させる
        window.addEventListener('afterprint', handleAfterPrint);
        window.setTimeout(function () {
            try { window.print(); } catch (e) { /* noop */ }
        }, 60);
    }

    /* =====================================================================
       ヘルスチェック（真の状態表示）
       ===================================================================== */
    function checkHealth() {
        var controller = createController();
        var timer = window.setTimeout(function () {
            safeAbort(controller);
        }, HEALTH_TIMEOUT_MS);

        window.fetch('/health', {
            headers: { 'Accept': 'application/json' },
            cache: 'no-store',
            signal: controller.signal
        })
            .then(function (response) {
                window.clearTimeout(timer);
                if (!response.ok) { throw new Error('health ' + response.status); }
                return response.json();
            })
            .then(function (info) {
                if (!info || typeof info !== 'object') { return; }
                var version = info.version ? ' v' + info.version : '';
                if (info.api_key_configured === false || info.status === 'degraded') {
                    var message = 'APIキー未設定のため「定型原稿モード」で放送します（原稿は自動生成の定型版です）。' +
                        'AI生成の原稿が必要なら、サーバー管理者へ RETRO_RADIO_GEMINI_API_KEY の設定を依頼してください。';
                    setServiceStatus('warn', '⚠️ ' + message);
                    setStreamDesc(message);
                } else {
                    setServiceStatus('ok', '✅ サーバー接続済み' + version + '（APIキー設定済み / AI原稿生成が利用できます）');
                }
            })
            .catch(function () {
                window.clearTimeout(timer);
                setServiceStatus('error', '⚠️ サーバーに接続できません。バックエンド（retro_radio サーバー）が起動しているか確認してください。');
            });
    }

    /* =====================================================================
       PWA（Service Worker）
       ===================================================================== */
    function isSecureContextForSw() {
        if (location.protocol === 'https:') { return true; }
        if (location.protocol !== 'http:') { return false; }
        var host = location.hostname;
        return host === 'localhost' || host === '127.0.0.1' || host === '::1' || host === '[::1]';
    }

    function registerServiceWorker() {
        if (!('serviceWorker' in navigator)) { return; }
        if (!isSecureContextForSw()) { return; }

        var load = function () {
            var url = '/static/service-worker.js';
            // 既定スコープは /static/ のみ。ルート獲得を試み、拒否されたら既定に戻す
            var promise;
            try {
                promise = navigator.serviceWorker.register(url, { scope: '/' });
            } catch (e) {
                promise = Promise.reject(e);
            }
            Promise.resolve(promise)
                .catch(function () { return navigator.serviceWorker.register(url); })
                .catch(function () { /* 登録失敗は黙って無視する */ });
        };

        if (document.readyState === 'complete') {
            load();
        } else {
            window.addEventListener('load', load, { once: true });
        }
    }

    /* =====================================================================
       年代レンジの取得（サーバー値を反映）
       ===================================================================== */
    function loadDecades() {
        window.fetch('/api/decades', { headers: { 'Accept': 'application/json' }, cache: 'no-store' })
            .then(function (response) {
                if (!response.ok) { throw new Error('decades ' + response.status); }
                return response.json();
            })
            .then(function (info) {
                if (!info || typeof info !== 'object' || !Array.isArray(info.decades)) { return; }
                var valid = info.decades.filter(function (value) {
                    return isFiniteNumber(value);
                });
                if (!valid.length) { return; }
                var low = Math.min.apply(null, valid);
                var high = Math.max.apply(null, valid);
                if (!isFiniteNumber(low) || !isFiniteNumber(high) || low === high) { return; }
                MIN_YEAR = low;
                MAX_YEAR = high;
                if (isFiniteNumber(info.default_year)) {
                    DEFAULT_YEAR = info.default_year;
                }
                setYear(state.year, { silent: true });
            })
            .catch(function () {
                // 取得できなくても既定レンジで動作する
            });
    }

    /* =====================================================================
       イベント登録
       ===================================================================== */
    function cacheDom() {
        dom.yearBadge = byId('yearBadge');
        dom.eraText = byId('eraText');
        dom.tunerRail = byId('tunerRail');
        dom.tunerNeedle = byId('tunerNeedle');
        dom.brassKnob = byId('brassKnob');
        dom.appIcon = byId('appIcon');
        dom.seniorToggle = byId('seniorToggle');
        dom.tabModeNormal = byId('tabModeNormal');
        dom.tabModeCare = byId('tabModeCare');
        dom.tabModeAnniversary = byId('tabModeAnniversary');
        dom.careModeBox = byId('careModeBox');
        dom.anniversaryModeBox = byId('anniversaryModeBox');
        dom.careRecreationDate = byId('careRecreationDate');
        dom.anniversaryDate = byId('anniversaryDate');
        dom.anniversaryName = byId('anniversaryName');
        dom.btnPlayRadio = byId('btnPlayRadio');
        dom.broadcastOutput = byId('broadcastOutput');
        dom.streamStatusTitle = byId('streamStatusTitle');
        dom.streamStatusDesc = byId('streamStatusDesc');
        dom.btnAudioAction = byId('btnAudioAction');
        dom.manuscriptTitle = byId('manuscriptTitle');
        dom.manuscriptBody = byId('manuscriptBody');
        dom.btnToggleWriting = byId('btnToggleWriting');
        dom.btnPrintRecreation = byId('btnPrintRecreation');
        dom.songTitle = byId('songTitle');
        dom.songArtist = byId('songArtist');
        dom.vinylDisk = byId('vinylDisk');
        dom.recreationQuizBox = byId('recreationQuizBox');
        dom.quizList = byId('quizList');
        dom.tubeBulb = byId('tubeBulb');
        dom.vuMeter = byId('vuMeter');
        dom.serviceStatus = byId('serviceStatus');

        /* --- 年選択 3 層（年代チップ / 年ステッパ / 年の直接入力） --- */
        dom.decadeChips = byId('decadeChips');
        dom.yearStepper = byId('yearStepper');
        dom.yearInput = byId('yearInput');
        dom.yearInputHint = byId('yearInputHint');
        dom.btnYearMinus10 = byId('btnYearMinus10');
        dom.btnYearMinus1 = byId('btnYearMinus1');
        dom.btnYearPlus1 = byId('btnYearPlus1');
        dom.btnYearPlus10 = byId('btnYearPlus10');

        /* --- 受信進捗パネル --- */
        dom.generationPanel = byId('generationPanel');
        dom.generationPanelTitle = byId('generationPanelTitle');
        dom.generationElapsed = byId('generationElapsed');
        dom.progressTrack = byId('progressTrack');
        dom.progressFill = byId('progressFill');
        dom.progressSteps = byId('progressSteps');
        dom.btnCancelGeneration = byId('btnCancelGeneration');

        /* --- 状態バナー --- */
        dom.stateBanner = byId('stateBanner');
        dom.stateBannerIcon = byId('stateBannerIcon');
        dom.stateBannerTitle = byId('stateBannerTitle');
        dom.stateBannerMessage = byId('stateBannerMessage');
        dom.stateBannerClose = byId('stateBannerClose');

        /* --- 空状態 --- */
        dom.emptyState = byId('emptyState');
        dom.btnEmptyPlay = byId('btnEmptyPlay');
        dom.btnShowGuide = byId('btnShowGuide');

        /* --- エラー状態 --- */
        dom.errorState = byId('errorState');
        dom.errorStateIcon = byId('errorStateIcon');
        dom.errorStateTitle = byId('errorStateTitle');
        dom.errorStateMessage = byId('errorStateMessage');
        dom.errorStateDetail = byId('errorStateDetail');
        dom.btnRetry = byId('btnRetry');
        dom.btnToggleErrorDetail = byId('btnToggleErrorDetail');

        /* --- 使い方ガイド --- */
        dom.guideDialog = byId('guideDialog');
        dom.guideDialogTitle = byId('guideDialogTitle');
        dom.btnGuideClose = byId('btnGuideClose');

        /* --- SubE 所有のプレイヤー UI（cache のみ。描画は SubE が行う） --- */
        dom.playerCard = byId('playerCard');
        dom.trackIndex = byId('trackIndex');
        dom.trackTitle = byId('trackTitle');
        dom.trackArtist = byId('trackArtist');
        dom.seekBar = byId('seekBar');
        dom.seekCurrent = byId('seekCurrent');
        dom.seekDuration = byId('seekDuration');
        dom.btnPrevTrack = byId('btnPrevTrack');
        dom.btnReplayTrack = byId('btnReplayTrack');
        dom.btnNextTrack = byId('btnNextTrack');
        dom.btnMute = byId('btnMute');
        dom.volumeControl = byId('volumeControl');
        dom.btnLoop = byId('btnLoop');
        dom.playlistList = byId('playlistList');
    }

    function bindEvents() {
        bindTuner();
        bindModeTabs();
        bindDecadeChips();
        bindYearStepper();
        bindGuide();

        if (dom.btnPlayRadio) {
            dom.btnPlayRadio.addEventListener('click', function () {
                resumeAudioContext();
                startGeneration();
            });
        }

        if (dom.btnAudioAction) {
            dom.btnAudioAction.addEventListener('click', toggleAudio);
        }

        if (dom.seniorToggle) {
            dom.seniorToggle.addEventListener('change', function () {
                applySeniorMode(this.checked);
                writeStore('seniorMode', this.checked ? '1' : '0');
                announce(this.checked ? 'シニア見守り特大表示を有効にしました。' : 'シニア表示を通常に戻しました。');
            });
        }

        if (dom.btnToggleWriting) {
            dom.btnToggleWriting.addEventListener('click', function () {
                var next = !(dom.manuscriptBody && dom.manuscriptBody.classList.contains('vertical-mode'));
                applyVerticalWriting(next);
                writeStore('verticalWriting', next ? '1' : '0');
            });
        }

        if (dom.btnPrintRecreation) {
            dom.btnPrintRecreation.addEventListener('click', handlePrint);
        }
        window.addEventListener('beforeprint', function () {
            state.printState = openAllDetails();
        });

        var inputs = [dom.careRecreationDate, dom.anniversaryDate, dom.anniversaryName];
        inputs.forEach(function (input) {
            if (!input) { return; }
            input.addEventListener('input', function () {
                this.classList.remove('input-error');
                this.removeAttribute('aria-invalid');
            });
        });

        /* --- 受信の停止と再試行 --- */
        if (dom.btnCancelGeneration) {
            dom.btnCancelGeneration.addEventListener('click', function () {
                cancelGeneration(false);
                stopProgress();
                hideGenerationPanel();
                showEmptyState(true);
            });
        }
        if (dom.btnRetry) {
            dom.btnRetry.addEventListener('click', function () {
                resumeAudioContext();
                startGeneration();
            });
        }
        if (dom.btnEmptyPlay) {
            dom.btnEmptyPlay.addEventListener('click', function () {
                resumeAudioContext();
                startGeneration();
            });
        }

        /* --- 状態表示 / 使い方ガイド --- */
        if (dom.stateBannerClose) {
            dom.stateBannerClose.addEventListener('click', function () {
                hideStateBanner();
            });
        }
        if (dom.btnToggleErrorDetail) {
            dom.btnToggleErrorDetail.addEventListener('click', function () {
                toggleErrorDetail();
            });
        }
        if (dom.btnShowGuide) {
            dom.btnShowGuide.addEventListener('click', function () { openGuide(); });
        }
        if (dom.btnGuideClose) {
            dom.btnGuideClose.addEventListener('click', function () { closeGuide(); });
        }

        window.addEventListener('pagehide', function () {
            stopVu();
            stopProgress();
            if (state.skipTimer) { window.clearTimeout(state.skipTimer); state.skipTimer = 0; }
            if (state.announceTimer) { window.clearTimeout(state.announceTimer); state.announceTimer = 0; }
        });

        document.addEventListener('visibilitychange', function () {
            if (document.hidden) {
                stopVu();
            } else if (isPlaying()) {
                if (state.analyser) { startVuAnalyser(); } else { startVuSimulation(); }
            }
        });

        // プレイヤーのコントロールは SubE が定義する（多重登録は state.playerBound で防ぐ）
        if (typeof bindPlayerControls === 'function') { bindPlayerControls(); }
    }

    /* =====================================================================
       初期化
       ===================================================================== */
    function init() {
        cacheDom();
        liveRegion = document.createElement('div');
        liveRegion.className = 'sr-only';
        liveRegion.setAttribute('role', 'status');
        liveRegion.setAttribute('aria-live', 'polite');
        document.body.appendChild(liveRegion);

        state.audio = createAudioElement();
        // 前回送信した内容を再試行用に復元する
        state.lastRequest = loadLastRequest();

        setServiceStatus('', '⏳ サーバー状態を確認しています…');
        setStreamTitle(DEFAULT_STATUS_TITLE);
        setStreamDesc(DEFAULT_STATUS_DESC);

        applySeniorMode(readStore('seniorMode', '0') === '1');
        applyVerticalWriting(readStore('verticalWriting', '0') === '1');

        setYear(DEFAULT_YEAR, { silent: true });
        setMode('normal', { silent: true });
        setLoading(false);
        syncAudioButton();
        setVuLevel(0);
        setTubeLit(false);

        // 出力欄を開いて「まだ何も受信していない」状態を見せる
        showOutput();
        hideGenerationPanel();
        hideErrorState();
        hideStateBanner();
        showEmptyState(true);

        bindEvents();
        checkHealth();
        loadDecades();
        registerServiceWorker();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
