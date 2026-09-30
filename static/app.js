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
       MediaElementSource 経由にすると CORS 制約で無音になるため。
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
    var REPEAT_STORAGE_KEY = 'repeatCount';

    /* ---------------------------------------------------------------------
       番組のループ / 継ぎ目
       ---------------------------------------------------------------------
       ラジオ番組は「テーマ曲 → 司会 → 曲 → 司会 → …」という単位を
       2〜3 周して成立する。REPEAT_MIN/MAX がその周回数の範囲。
    */
    var MIN_REPEAT = 1;
    var MAX_REPEAT = 5;
    var DEFAULT_REPEAT = 3;

    // 曲（プレビュー音源が無い）のスロットに割く無音の長さ。
    // 音源が無いのにトラックを「落とす」のではなく短い間奏として残すことで、
    // 番組のリズム（曲→司会→曲）が壊れないようにする。
    var SILENCE_SLOT_SECONDS = 1.6;

    // gTTS の MP3 は先頭/末尾に無音が入る。読み上げの冒頭を少し進めて
    // ファイル間に生まれる「聞こえる無音」を削る。
    var TALK_LEAD_TRIM_SECONDS = 0.18;

    // トラック切替時のクロスフェード。
    var XFADE_MS = 420;
    // 次のトラックの再生を開始し始める残り時間（秒）。
    var XFADE_PREROLL_SECONDS = 0.7;

    // 再生監視（watchdog）の granularity。
    // 1 秒ごとに再生位置を確認し、WATCHDOG_STALL_MS 動かなければ
    // `ended` を取り逃がした／要素が壊れた場合に強制的に次のトラックへ進める。
    // これが無いと 1 つのイベント欠落で番組が永久に止まる。
    var WATCHDOG_INTERVAL_MS = 1000;
    var WATCHDOG_STALL_MS = 6000;

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
    // プレビュー音源が無い曲にも「間奏」スロットを残す。
    // そのスロットを落とすと 曲→司会→曲 のリズムが崩れ、
    // 気づけば司会の朗読だけが 5 本続く放送になってしまう。
    var INTERMISSION = 'INTERMISSION';

    // ON AIR バッジの表示状態
    var ON_AIR_LIVE = '📡 ON AIR';
    var ON_AIR_READY = '📻 STANDBY';
    var ON_AIR_ENDED = '📴 放送終了';

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
        // クロスフェード用に 2 本の <audio> を交互に使う。
        // slots.a / slots.b が実体、state.audio は常に「現在再生中のほう」を指す。
        slots: { a: null, b: null },
        activeSlot: 'a',
        analyserNodes: { a: null, b: null },
        xfadeTimer: 0,
        xfadeBusy: false,
        xfadePending: -1,
        xfadeFrom: null,
        xfadeTo: null,
        // 再生が進んでいるかを監視する watchdog。-ended が来ないまま
        // 固まっても、次のトラックへ強制的に進める。
        watchdogTimer: 0,
        lastProgressAt: 0,
        lastProgressTime: -1,
        silenceUrl: '',
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
        volume: 0.8,
        /* 番組の周回数（1〜5）。loopEnabled が false のときは 1 周として扱う。 */
        repeatCount: DEFAULT_REPEAT,
        /* 1 パスのトラック数（レンダリングと周回表示に使う） */
        passLength: 0,
        /* 原稿のセグメント数（キューシートのハイライトに使う） */
        segmentCount: 0
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
     * 「### 見出し」付きの原稿テキストを、セグメント単位の構造に分解する。
     * `{ title, lines }` の配列を返す（見出しが無ければ 1 つの素のブロック）。
     *
     * 再生中の原稿をハイライトするため、DOM 側では 1 セグメント = 1 <section> として
     * 描画できる形へ分解しておく。
     */
    function parseScriptBlocks(raw) {
        var text = typeof raw === 'string' ? raw : '';
        if (!text.trim()) { return []; }

        var lines = text.split(/\r\n|\r|\n/);
        var blocks = [];
        var current = null;
        var buffer = [];
        var emitted = 0;

        function flush() {
            if (!buffer.length) { return; }
            if (!current) {
                current = { title: '', lines: [] };
                blocks.push(current);
            }
            var chunk = buffer.join('\n');
            if (emitted + chunk.length > SCRIPT_MAX_CHARS) {
                chunk = chunk.slice(0, Math.max(0, SCRIPT_MAX_CHARS - emitted));
                if (chunk) { current.lines.push(chunk); }
                emitted = SCRIPT_MAX_CHARS;
                current = { title: '', lines: ['（以下、省略されました）'], truncated: true };
                blocks.push(current);
                current = null;
                buffer = [];
                return;
            }
            current.lines.push(chunk);
            emitted += chunk.length;
            buffer = [];
        }

        for (var i = 0; i < lines.length; i += 1) {
            if (emitted >= SCRIPT_MAX_CHARS) { break; }
            var line = lines[i];
            var heading = /^\s*#{1,6}\s+(\S.*?)\s*$/.exec(line);
            if (heading) {
                flush();
                current = { title: heading[1], lines: [] };
                blocks.push(current);
                continue;
            }
            if (!line.trim()) {
                flush();
                continue;
            }
            buffer.push(line);
        }
        flush();

        return blocks.filter(function (block) {
            return block.title || block.lines.length;
        });
    }

    /**
     * バックエンドが返した `segments` を原稿用紙の HTML にする。
     *
     * `data.script` を再度パースし直すと境界の解釈がバックエンドとズレ、
     * `metadata.segment_index` と原稿の行がずれて「読み上げ中」が
     * 別の段落に付く。segments はパース済みでインデックスが確定しているので、
     * こちらを正とする。
     */
    function renderSegmentsHtml(segments) {
        var list = Array.isArray(segments) ? segments : [];
        if (!list.length) { return ''; }

        var html = [];
        var emitted = 0;
        for (var i = 0; i < list.length; i += 1) {
            var seg = list[i];
            if (!seg || typeof seg !== 'object') { continue; }
            var index = isFiniteNumber(seg.order) ? seg.order : i;
            var title = String(seg.title || ('セグメント' + (i + 1)));
            var body = String(seg.content === null || seg.content === undefined ? '' : seg.content);

            html.push('<section class="script-block" data-segment-index="' + index + '">');
            html.push('<h3 class="script-heading">' + escapeHtml(title) + '</h3>');
            var paragraphs = body.split(/\r\n|\r|\n/).filter(function (line) {
                return line.trim().length > 0;
            });
            for (var j = 0; j < paragraphs.length; j += 1) {
                if (emitted + paragraphs[j].length > SCRIPT_MAX_CHARS) {
                    html.push('<p class="script-paragraph script-truncated">（以下、省略されました）</p>');
                    html.push('</section>');
                    return html.join('');
                }
                emitted += paragraphs[j].length;
                html.push('<p class="script-paragraph">' + escapeHtml(paragraphs[j]) + '</p>');
            }
            html.push('</section>');
        }
        return html.join('');
    }

    /**
     * セグメント単位の原稿を安全な HTML へ変換する（後方互換用）。
     * 必ず escapeHtml() を通した文字列のみを innerHTML に渡すこと。
     * 各 <section> は data-segment-index を持ち、再生位置のハイライトに使う。
     */
    function renderManuscriptHtml(raw) {
        var blocks = parseScriptBlocks(raw);
        if (!blocks.length) { return ''; }

        var html = [];
        for (var i = 0; i < blocks.length; i += 1) {
            var block = blocks[i];
            html.push('<section class="script-block" data-segment-index="' + i + '">');
            if (block.title) {
                html.push('<h3 class="script-heading">' + escapeHtml(block.title) + '</h3>');
            }
            for (var j = 0; j < block.lines.length; j += 1) {
                var body = escapeHtml(block.lines[j]).split('\n').join('<br>');
                var cls = block.truncated ? 'script-paragraph script-truncated' : 'script-paragraph';
                html.push('<p class="' + cls + '">' + body + '</p>');
            }
            html.push('</section>');
        }
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
            // バックエンドの `segments`（パース済み・インデックス確定）を正とする。
            // script を再パースすると曲マーカー等が混ざり、ハイライトがずれる。
            var segments = Array.isArray(data.segments) ? data.segments : null;
            var html = (segments && segments.length)
                ? renderSegmentsHtml(segments)
                : renderManuscriptHtml(data.script);
            dom.manuscriptBody.innerHTML = html;
        }
        state.segmentCount = (Array.isArray(data.segments) && data.segments.length)
            ? data.segments.length
            : parseScriptBlocks(data.script).length;

        var song = (data.song && typeof data.song === 'object') ? data.song : null;
        if (dom.songTitle) {
            dom.songTitle.textContent = (song && song.title) ? String(song.title) : '—';
        }
        if (dom.songArtist) {
            dom.songArtist.textContent = (song && song.artist) ? String(song.artist) : '—';
        }

        renderQuiz(mode === 'care_recreation' ? data.reminiscence_quiz : null);
        // バックエンドが組み立てている番組表をそのまま見せる
        renderProgramGuide(data.program_guide);

        if (dom.vinylDisk) {
            dom.vinylDisk.classList.remove('spinning');
            // アニメーションを確実に再開始させるためのリフロー
            void dom.vinylDisk.offsetWidth;
            dom.vinylDisk.classList.add('spinning');
        }

        var scriptLength = typeof data.script === 'string' ? data.script.length : 0;
        var songCount = Array.isArray(data.songs) ? data.songs.length : 0;
        // 読み上げ音声が生成できなかったセグメントを数える。
        // gTTS が 429 で弾かれると原稿ごと無声になるが、
        // 黙って流すより「声が出ていない」と伝えて owing したい。
        var silentTalks = countSilentTalks(data);
        setStreamTitle('🎙️ ラジオ放送中（' + year + '年）');
        if (silentTalks) {
            setStreamDesc('【' + modeLabel(mode) + '】原稿 ' + scriptLength + '文字・ヒット曲 ' +
                songCount + '曲を受信しましたが、読み上げ音声が ' + silentTalks +
                'か所生成できませんでした。時間をおいてもう一度お試しください。');
        } else {
            setStreamDesc('【' + modeLabel(mode) + '】原稿 ' + scriptLength + '文字・ヒット曲 ' +
                songCount + '曲を受信しました。オープニング曲から始まり、テーマ曲と原稿が交互に放送されます。');
        }

        if (silentTalks) {
            showStateBanner(
                'warning',
                '⚠️ 読み上げ音声が生成できませんでした',
                '原稿 ' + scriptLength + '文字・ヒット曲 ' + songCount + '曲を受信しましたが、' +
                '読み上げ音声が ' + silentTalks + 'か所で生成できませんでした。' +
                '一時的なレート制限の可能性があるため、時間をおいて再度お試しください。'
            );
        } else {
            showStateBanner(
                'success',
                '🎉 ' + year + '年の番組を受信しました',
                '【' + modeLabel(mode) + '】原稿 ' + scriptLength + '文字・ヒット曲 ' + songCount + '曲。'
            );
        }

        announce(year + '年' + month + '月' + day + '日の' + modeLabel(mode) + 'を受信しました。');

        startPlayback(data);
    }

    /**
     * 読み上げ音声が無いトーク（原稿）の数を数える。
     * gTTS は 429 で弾かれることがあり、そのとき原稿が丸ごと無声になる。
     * 黙って流すのではなくユーザーへ伝えるため、生成直後に必ず数える。
     */
    function countSilentTalks(data) {
        var playlist = Array.isArray(data && data.playlist) ? data.playlist : [];
        var silent = 0;
        for (var i = 0; i < playlist.length; i += 1) {
            var raw = playlist[i];
            if (!raw || typeof raw !== 'object') { continue; }
            var type = String(raw.type === null || raw.type === undefined ? '' : raw.type)
                .trim().toUpperCase();
            if (type !== TALK) { continue; }
            var url = raw.audio_url;
            if (!url && raw.metadata && typeof raw.metadata === 'object') {
                url = raw.metadata.audio_url;
            }
            if (!url) { silent += 1; }
        }
        return silent;
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
       連続オーディオ再生（ラジオ番組のキュー）
       ---------------------------------------------------------------------
       バックエンドの playlist（曲で始まり曲で終わる）を、
       「1 パス」= 1 周分のトラック列へ変換し、続けて周回数だけ繰り返す。

       MediaElementSource は「キュー全体が同一オリジン」のときだけ作る。
       別オリジン（iTunes プレビュー）は CORS が通らないため、
       作ると以降の別オリジン音源が無音になる。
       ===================================================================== */

    // 置き換えるのではなく途切れに短い無音（間奏）を挟むための音源。
    // <audio> に読ませるので、この場も crossfade / ended の通常経路に乗る。
    function getSilenceUrl(seconds) {
        if (state.silenceUrl) { return state.silenceUrl; }
        try {
            var rate = 8000;
            var frames = Math.max(1, Math.round(seconds * rate));
            var dataBytes = frames;
            var buffer = new ArrayBuffer(44 + dataBytes);
            var view = new DataView(buffer);

            function writeStr(offset, text) {
                for (var i = 0; i < text.length; i += 1) {
                    view.setUint8(offset + i, text.charCodeAt(i));
                }
            }

            writeStr(0, 'RIFF');
            view.setUint32(4, 36 + dataBytes, true);
            writeStr(8, 'WAVE');
            writeStr(12, 'fmt ');
            view.setUint32(16, 16, true);   // PCM ヘッダ長
            view.setUint16(20, 1, true);    // 形式 = PCM
            view.setUint16(22, 1, true);    // モノラル
            view.setUint32(24, rate, true);
            view.setUint32(28, rate, true); // バイトレート
            view.setUint16(32, 1, true);    // ブロックアライン
            view.setUint16(34, 8, true);    // ビット深度 = 8
            writeStr(36, 'data');
            view.setUint32(40, dataBytes, true);
            // 8bit PCM の無音は 128
            for (var f = 0; f < dataBytes; f += 1) {
                view.setUint8(44 + f, 128);
            }

            state.silenceUrl = URL.createObjectURL(new Blob([buffer], { type: 'audio/wav' }));
        } catch (e) {
            // Blob / DataView が使えない環境では間奏 الصوتを作る（0.01 秒）
            state.silenceUrl =
                'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA=';
        }
        return state.silenceUrl;
    }

    // 1 パス分のトラック列を組み立てる。
    // バックエンドの playlist がすでに「曲 → トーク → 曲 …」の順に並んでいるので、
    // **順序はそのまま維持**し、曲の音源だけ差し替える。
    //
    // `items` を渡すと、その配列だけで 1 パスを作る。`data` だけの場合は
    // `data.playlist` を使う（= 旧クライアント互換）。
    function buildPass(data, items) {
        var pass = [];
        var playlist = Array.isArray(items) ? items
            : (Array.isArray(data && data.playlist) ? data.playlist : []);
        var fullScriptUrl = toAbsoluteUrl(data && data.audio_url);
        var talkCount = 0;
        var noAudioTalks = 0;

        function readAudioUrl(raw) {
            if (raw.audio_url) { return toAbsoluteUrl(raw.audio_url); }
            if (raw.metadata && typeof raw.metadata === 'object' && raw.metadata.audio_url) {
                return toAbsoluteUrl(raw.metadata.audio_url);
            }
            return '';
        }

        function readSegmentIndex(raw, fallback) {
            if (raw.metadata && typeof raw.metadata === 'object' &&
                isFiniteNumber(raw.metadata.segment_index)) {
                return raw.metadata.segment_index;
            }
            return fallback;
        }

        function readType(raw) {
            // 契約では "TALK" / "SONG" だが、実装により "talk" / "song" の
            // 可能性があるため必ず大文字小文字を正規化して判定する
            return String(raw.type === null || raw.type === undefined ? '' : raw.type)
                .trim().toUpperCase();
        }

        // --- 1 巡目: 実際に鳴らせる曲を集める（曲名は重複させない） -------
        var playable = [];
        var seen = {};
        playlist.forEach(function (raw) {
            if (!raw || typeof raw !== 'object') { return; }
            if (readType(raw) !== SONG) { return; }
            var url = toAbsoluteUrl(raw.preview_url);
            if (!url) { return; }
            var title = String(raw.title || 'ヒット曲');
            var artist = String(raw.artist || '');
            var key = title + '|' + artist;
            if (seen[key]) { return; }
            seen[key] = true;
            playable.push({ title: title, artist: artist, url: url });
        });

        // 1 パス内で既に使った曲。同一曲は何回まで再生してよいか。
        // 古い年代は iTunes から 1 パスぶんの別々の曲が取れず、
        // 全部使うと無音になる。無音の方が施設では困るので、
        // 「使い切ったら最後に使った曲から 1 回だけ再利用する」方針にする。
        var MAX_PLAYS_PER_SONG = 2;
        var playCount = {};
        var lastUsedAt = {};
        var seq = 0;
        var usedUp = false;

        function keyOf(song) { return song.title + '|' + song.artist; }

        function markUsed(song) {
            if (!song) { return; }
            var key = keyOf(song);
            playCount[key] = (playCount[key] || 0) + 1;
            lastUsedAt[key] = seq;
            seq += 1;
        }

        function takeUnused(pool) {
            if (!pool.length) { return null; }
            var chosen = null;
            for (var i = 0; i < pool.length; i += 1) {
                if (!playCount[keyOf(pool[i])]) { chosen = pool[i]; break; }
            }
            if (!chosen) {
                // 全部既に使った: 上限に達してない曲のうち、最後に使ったのが古いものを取る
                for (var j = 0; j < pool.length; j += 1) {
                    var key = keyOf(pool[j]);
                    if ((playCount[key] || 0) >= MAX_PLAYS_PER_SONG) { continue; }
                    if (chosen === null || lastUsedAt[key] < lastUsedAt[keyOf(chosen)]) {
                        chosen = pool[j];
                    }
                }
            }
            if (!chosen) {
                // 上限まで全部使い切った（= 1 曲しか無い場合）
                usedUp = true;
                chosen = pool[0];
            }
            // 返した曲は必ず記録する（記録しないと毎回同じ曲を選んでしまう）
            markUsed(chosen);
            return chosen;
        }

        // --- 2 巡目: playlist の順序どおりに並べる -----------------------
        playlist.forEach(function (raw) {
            if (!raw || typeof raw !== 'object') { return; }
            var type = readType(raw);

            if (type === SONG) {
                var ownUrl = toAbsoluteUrl(raw.preview_url);
                var pick = null;
                if (ownUrl) {
                    pick = { title: String(raw.title || 'ヒット曲'), artist: String(raw.artist || ''), url: ownUrl };
                    markUsed(pick);
                } else {
                    // 音源が無いスロットには、まだ使っていない曲を渡す。
                    // 全て使い切った場合は最後に使った古い曲を 1 回だけ再利用する
                    //（1 パスの途中で無音になるのを避けるため）。
                    pick = takeUnused(playable);
                }
                if (pick) {
                    pass.push({ kind: SONG, url: pick.url, title: pick.title, artist: pick.artist });
                } else {
                    pass.push({
                        kind: INTERMISSION,
                        url: getSilenceUrl(SILENCE_SLOT_SECONDS),
                        title: String(raw.title || '間奏'),
                        artist: usedUp
                            ? '曲を使い切りました（1 パスでは重複させないため）'
                            : '音源が見つかりません'
                    });
                }
                return;
            }

            // type が未知でも audio_url が読めればトークとして扱う
            var audioUrl = readAudioUrl(raw);
            var isTalk = (type === TALK) || (!type && !!audioUrl);
            if (!isTalk) { return; }

            // 個別音声が無い場合、空 URL のトラックを作ると <audio> が
            // error を出して 500ms 後にスキップされ、結果として
            // 「曲 → 無音 → 曲」で原稿が丸ごと消える。
            // 短い間奏トラックにして、番組の骨組みは必ず残す。
            pass.push({
                kind: audioUrl ? TALK : INTERMISSION,
                url: audioUrl || getSilenceUrl(SILENCE_SLOT_SECONDS * 1.5),
                title: String(raw.title || 'ナレーション'),
                artist: audioUrl ? '' : '音声が生成できませんでした',
                segmentIndex: readSegmentIndex(raw, talkCount),
                beatOnly: !audioUrl
            });
            talkCount += 1;
            if (!audioUrl) { noAudioTalks += 1; }
        });

        // playlist が空のときのフォールバック: 全文 TTS → 曲
        if (!playlist.length) {
            if (fullScriptUrl) {
                pass.push({
                    kind: TALK,
                    url: fullScriptUrl,
                    title: '番組全文',
                    artist: '',
                    segmentIndex: 0,
                    beatOnly: false
                });
                talkCount += 1;
            }
            var songs = Array.isArray(data && data.songs) ? data.songs : [];
            songs.forEach(function (song) {
                if (!song || typeof song !== 'object') { return; }
                var url = toAbsoluteUrl(song.preview_url);
                if (!url) { return; }
                pass.push({
                    kind: SONG,
                    url: url,
                    title: String(song.title || 'ヒット曲'),
                    artist: String(song.artist || '')
                });
            });
        }

        // 個別音声が 1 つでも欠けている場合、本文全体の音声があれば
        // 「最初の無音トーク」slot へ差し込む。
        // 何も読まれないと原稿が丸ごと聞こえなくなるため、
        // 本文を 1 度は読み上げることを最優先する（残りは間奏のまま）。
        if (fullScriptUrl) {
            for (var i = 0; i < pass.length; i += 1) {
                if (pass[i].beatOnly) {
                    pass[i].kind = TALK;
                    pass[i].url = fullScriptUrl;
                    pass[i].beatOnly = false;
                    pass[i].artist = '';
                    pass[i].title = pass[i].title + '（番組全文）';
                    break;
                }
            }
        }

        return pass;
    }

    // 番組全体（周回数ぶん）のキューを組み立てる。
    //
    // バックエンドが `passes`（パスごとに別の曲入り）を返している場合は、
    // それを順に連結する。**同じ 1 パスを回さない**。
    // 旧サーバーは `passes` を返さないので `playlist` を周回する（互換）。
    function buildQueue(data) {
        var serverPasses = (data && Array.isArray(data.passes)) ? data.passes : null;
        var usablePasses = [];
        if (serverPasses) {
            for (var i = 0; i < serverPasses.length; i += 1) {
                if (Array.isArray(serverPasses[i]) && serverPasses[i].length) {
                    usablePasses.push(serverPasses[i]);
                }
            }
        }

        if (!usablePasses.length) {
            // 旧形式: 1 パスを作って周回する
            var single = buildPass(data);
            state.passLength = single.length;
            if (!single.length) { return []; }
            var repeats = effectiveRepeatCount();
            var legacy = [];
            for (var p = 0; p < repeats; p += 1) {
                for (var j = 0; j < single.length; j += 1) {
                    var track = single[j];
                    track.pass = p + 1;
                    track.passTotal = repeats;
                    track.isFirstInPass = (j === 0);
                    legacy.push(track);
                }
            }
            return legacy;
        }

        // 新形式: サーバーが決めたパスを使う。各パスは独立に組むので、
        // パス内の重複回避（takeUnused）や間奏の補完もパスごとに効く。
        // UI の周回数で**使うパス数**を絞る（連続 OFF なら 1 パスだけ）。
        var wanted = effectiveRepeatCount();
        if (wanted < usablePasses.length) {
            usablePasses = usablePasses.slice(0, wanted);
        }

        var builtPasses = [];
        for (var k = 0; k < usablePasses.length; k += 1) {
            var built = buildPass(data, usablePasses[k]);
            if (built.length) { builtPasses.push(built); }
        }
        if (!builtPasses.length) { return []; }
        state.passLength = builtPasses[0].length;

        var queue = [];
        for (var q = 0; q < builtPasses.length; q += 1) {
            for (var m = 0; m < builtPasses[q].length; m += 1) {
                var t = builtPasses[q][m];
                t.pass = q + 1;
                t.passTotal = builtPasses.length;
                t.isFirstInPass = (m === 0);
                queue.push(t);
            }
        }
        return queue;
    }

    // 連続再生の ON/OFF と周回数をまとめて「実際に何回流すか」に変換する
    function effectiveRepeatCount() {
        if (!state.loopEnabled) { return 1; }
        var count = Math.round(Number(state.repeatCount));
        if (!isFinite(count)) { return DEFAULT_REPEAT; }
        if (count < MIN_REPEAT) { return MIN_REPEAT; }
        if (count > MAX_REPEAT) { return MAX_REPEAT; }
        return count;
    }

    function createAudioElement(slot) {
        var audio = document.createElement('audio');
        audio.preload = 'auto';
        audio.setAttribute('playsinline', '');
        audio.setAttribute('data-slot', slot);
        // display:none は Safari でメディア再生が停止しうるため極小オフスクリーン配置にする
        audio.style.cssText = 'position:absolute;left:-9999px;top:0;width:1px;height:1px;opacity:0;pointer-events:none;';
        audio.addEventListener('ended', onTrackEnded);
        audio.addEventListener('play', onAudioPlay);
        audio.addEventListener('pause', onAudioPause);
        audio.addEventListener('error', onAudioError);
        // 再生位置の反映（#seekBar / #seekCurrent / #seekDuration）
        audio.addEventListener('timeupdate', updateSeekBar);
        audio.addEventListener('loadedmetadata', onAudioLoadedMetadata);
        audio.addEventListener('seeked', updateSeekBar);
        // 音量・ミュートが変わったときはスライダーとボタンへ反映する
        audio.addEventListener('volumechange', onAudioVolumeChange);
        document.body.appendChild(audio);
        return audio;
    }

    function destroyAudioElement(audio) {
        if (!audio) { return; }
        try { audio.pause(); } catch (e) { /* noop */ }
        audio.removeEventListener('ended', onTrackEnded);
        audio.removeEventListener('play', onAudioPlay);
        audio.removeEventListener('pause', onAudioPause);
        audio.removeEventListener('error', onAudioError);
        audio.removeEventListener('timeupdate', updateSeekBar);
        audio.removeEventListener('loadedmetadata', onAudioLoadedMetadata);
        audio.removeEventListener('seeked', updateSeekBar);
        audio.removeEventListener('volumechange', onAudioVolumeChange);
        if (audio.parentNode) { audio.parentNode.removeChild(audio); }
    }

    // クロスフェード用に 2 本を用意し、先頭（a）を active にして差し替える
    function createAudioPair() {
        state.slots.a = createAudioElement('a');
        state.slots.b = createAudioElement('b');
        state.activeSlot = 'a';
        state.audio = state.slots.a;
    }

    // 待機中スロット側の <audio>（crossfade の行き先）
    function idleSlot() {
        return (state.activeSlot === 'a') ? 'b' : 'a';
    }

    function activeAnalyserSlot() {
        return state.activeSlot;
    }

    // 音量・ミュートは <audio> が正なので、こっちからスライダーとボタンへ同期する
    function onAudioVolumeChange(event) {
        var audio = (event && event.target) ? event.target : state.audio;
        if (!audio) { return; }
        // フェード中（クロスフェード）は音量が三角波になるため UI を上書きしない
        if (state.xfadeBusy) { return; }
        if (audio !== state.audio) { return; }
        state.volume = clampVolume(audio.volume);
        state.muted = !!audio.muted;
        if (dom.volumeControl) {
            dom.volumeControl.value = String(state.volume);
        }
        if (dom.btnMute) {
            dom.btnMute.textContent = state.muted ? '🔇' : '🔊';
            dom.btnMute.setAttribute('aria-pressed', state.muted ? 'true' : 'false');
        }
    }

    /**
     * 読み上げ音声の冒頭の無音を少し進めて、ファイル間の沈黙を削る。
     * gTTS の MP3 は先頭に必ず無音が入るため、2 つのファイルを直結するたびに効く。
     */
    function onAudioLoadedMetadata(event) {
        var audio = (event && event.target) ? event.target : state.audio;
        if (!audio || audio !== state.audio) { return; }
        var track = state.queue[state.index];
        if (!track || track.kind !== TALK) { return; }
        var duration = audio.duration;
        if (!isFiniteNumber(duration) || duration <= 0) { return; }
        if (duration <= TALK_LEAD_TRIM_SECONDS * 2) { return; }
        try {
            audio.currentTime = Math.min(TALK_LEAD_TRIM_SECONDS, duration / 2);
        } catch (e) { /* noop */ }
    }

    /**
     * 解析ノードを作り直せるよう、<audio> 要素を 2 本とも新しく差し替える。
     * `createMediaElementSource` は 1 要素につき 1 回しか呼べないため、
     * 差し替え時はノードごと破棄する。
     */
    function replaceAudioPair() {
        destroyAudioElement(state.slots.a);
        destroyAudioElement(state.slots.b);
        state.analyser = null;
        state.analyserNodes.a = null;
        state.analyserNodes.b = null;
        state.analyserAttached = false;
        createAudioPair();
        applyVolumeToAll();
    }

    // 2 本の <audio> へ音量とミュートを適用する
    function applyVolumeToAll() {
        var slots = state.slots;
        if (!slots) { return; }
        [slots.a, slots.b].forEach(function (audio) {
            if (!audio) { return; }
            try {
                audio.volume = state.muted ? 0 : clampVolume(state.volume);
                audio.muted = state.muted;
            } catch (e) { /* noop */ }
        });
    }

    function startPlayback(data) {
        stopPlayback();
        cancelXfade();

        // バックエンドが推奨する周回数を初期値にする（UI で後から変えられる）
        if (data && isFiniteNumber(data.loop_count)) {
            state.repeatCount = clampRepeat(data.loop_count);
        }
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
            setOnAir(ON_AIR_READY);
            setStreamDesc('再生できる音源が見つかりませんでした。原稿のみ表示しています。');
            return;
        }

        var allSameOrigin = state.queue.every(function (track) {
            return isSameOrigin(track.url);
        });
        if (state.analyserAttached && !allSameOrigin) {
            replaceAudioPair();
        }
        state.allSameOrigin = allSameOrigin;
        resumeAudioContext();
        ensureAnalyser();
        setOnAir(ON_AIR_LIVE);
        startWatchdog();
        playIndex(0, false);
    }

    function stopPlayback() {
        if (state.skipTimer) {
            window.clearTimeout(state.skipTimer);
            state.skipTimer = 0;
        }
        stopWatchdog();
        cancelXfade();
        // removeAttribute('src') + load() が error を発しうるため、
        // 先にキューを空にして onAudioError のスキップ処理を発火させない
        state.queue = [];
        state.index = -1;
        state.passLength = 0;
        stopVu();
        [state.slots.a, state.slots.b].forEach(function (audio) {
            if (!audio) { return; }
            try { audio.pause(); } catch (e) { /* noop */ }
            try {
                audio.removeAttribute('src');
                audio.load();
            } catch (e) { /* noop */ }
        });
        state.endedCount = 0;
        state.prefetchedUrl = '';
        state.userPaused = false;
        state.needGesture = false;
        state.finished = false;
        setTubeLit(false);
        setOnAir(ON_AIR_READY);
        syncAudioButton();
        clearManuscriptHighlight();
        // 停止したらトラック表示とプレイリストを初期状態へ戻す
        updateSeekBar();
        renderPlaylist([]);
        renderPlayerUI();
    }

    // トラックの種類ラベル（間奏は曲扱いだが「音源なし」を明示する）
    function kindLabel(track) {
        if (!track) { return '🎙️ ナレーション'; }
        if (track.kind === SONG) { return '🎵 ヒット曲'; }
        if (track.kind === INTERMISSION) { return '🎼 間奏'; }
        return '🎙️ ナレーション';
    }

    // 1 パスの何周目かを "2/3周" 形式で返す（1 周のときは空文字）
    function passLabel(track) {
        if (!track || !track.passTotal || track.passTotal <= 1) { return ''; }
        return ' ' + track.pass + '/' + track.passTotal + '周';
    }

    function playIndex(index, isSkip) {
        if (state.skipTimer) {
            window.clearTimeout(state.skipTimer);
            state.skipTimer = 0;
        }
        cancelXfade();
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
                state.audio.pause();
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

    /* ---------------------------------------------------------------------
       クロスフェード
       ---------------------------------------------------------------------
       別々の MP3 を <audio> の src 差し替えだけで繋ぐと、
       「前のトラックの末尾 + 次のトラックの先頭」で必ず無音が聞こえる。
       そこで 2 本の <audio> を使い、残り 0.7 秒の時点で
       次のトラックを音量 0 で鳴らし始め、XFADE_MS かけて重ねる。
       ユーザー操作による切替では行わない。
       --------------------------------------------------------------------- */
    /* ---------------------------------------------------------------------
       監視（watchdog）
       ---------------------------------------------------------------------
       どれか 1 つの <audio> イベントが欠けた／発火しないと、
       `ended` が来ないままキューが永久に進まなくなる。
       実測した停止要因:
         - フェード先が読み込み失敗し、active に差し替えると ended が来ない
         - クロスフェードのタイマーが例外で中断され xfadeBusy が残る
       どちらでも「再生位置が動かない」状態になるので、
       位置が進まないまま一定時間経過したら次のトラックへ強制的に進める。
       --------------------------------------------------------------------- */
    function startWatchdog() {
        stopWatchdog();
        state.lastProgressAt = Date.now();
        state.lastProgressTime = -1;
        state.watchdogTimer = window.setInterval(function () {
            try {
                checkWatchdog();
            } catch (e) {
                console.error('watchdog でエラーが発生しました', e);
            }
        }, WATCHDOG_INTERVAL_MS);
    }

    function stopWatchdog() {
        if (state.watchdogTimer) {
            window.clearInterval(state.watchdogTimer);
            state.watchdogTimer = 0;
        }
    }

    function checkWatchdog() {
        if (state.userPaused || state.finished) { return; }
        if (!state.queue.length || state.index < 0) { return; }
        if (state.index >= state.queue.length) { return; }
        if (state.needGesture) { return; }

        // フェード中は待ち伏せ帯。フェード自体に時間を与える。
        if (state.xfadeBusy) { return; }

        var audio = state.audio;
        if (!audio) {
        // 要素が無い = 再生できる状態じゃないので復帰を試みる
            forceAdvance('再生要素が見つかりません');
            return;
        }
        if (audio.paused) {
            // ユーザー操作による一時停止ではないのに paused なら放置しない
            if (!state.userPaused && !audio.ended) {
                forceAdvance('再生が停止しました');
            }
            return;
        }
        if (audio.ended) {
            // ended イベントを取り逃した
            forceAdvance('再生位置の更新がありません');
            return;
        }

        var t = audio.currentTime;
        if (!isFiniteNumber(t)) { t = 0; }
        if (t !== state.lastProgressTime) {
            state.lastProgressTime = t;
            state.lastProgressAt = Date.now();
            return;
        }

        var idleMs = Date.now() - state.lastProgressAt;
        if (idleMs >= WATCHDOG_STALL_MS) {
            forceAdvance('再生が ' + Math.round(idleMs / 1000) + ' 秒間止まっています');
        }
    }

    // 監視が鳴ったら次のトラックへ強制的に進める
    function forceAdvance(reason) {
        if (!state.queue.length) { return; }
        var next = state.index + 1;
        if (reason) {
            logger_line('再生を再開します: ' + reason);
        }
        if (next >= state.queue.length) {
            onPlaylistEnd();
            return;
        }
        // フェードが張ったままなら先に解除する（xfadeBusy が残ると
        // onTrackEnded が無視し続けてキューが止まるため）
        cancelXfade();
        // lastProgressTime はリセットしない。リセットすると次のポーリングで
        // 「位置が変わった」と誤判定され、1 トラック進むのに 2 回の監視が
        // 必要になってしまう。時刻だけ更新して判定を続ける。
        state.lastProgressAt = Date.now();
        playIndex(next, true);
    }

    // ログ窓（#streamStatusDesc）へ 1 行だけ出す
    function logger_line(text) {
        setStreamDesc(text);
    }

    function canCrossfade() {
        if (state.userPaused || state.finished) { return false; }
        if (state.xfadeBusy) { return false; }
        if (!state.audio || !state.slots.a || !state.slots.b) { return false; }
        if (state.index < 0 || state.index >= state.queue.length - 1) { return false; }
        if (state.audio.paused || state.audio.ended) { return false; }
        var duration = state.audio.duration;
        return isFiniteNumber(duration) && duration > 0;
    }

    // 残り時間が XFADE_PREROLL_SECONDS を切ったら次のトラックを仕込む
    function maybeStartXfade() {
        if (!canCrossfade()) { return; }
        var duration = state.audio.duration;
        var current = state.audio.currentTime;
        if (!isFiniteNumber(duration) || !isFiniteNumber(current)) { return; }
        if (duration - current > XFADE_PREROLL_SECONDS) { return; }
        startXfade(state.index + 1);
    }

    function startXfade(nextIndex) {
        if (nextIndex < 0 || nextIndex >= state.queue.length) { return; }
        if (state.xfadeBusy) { return; }

        var from = state.audio;
        var target = state.slots[idleSlot()];
        if (!from || !target) { return; }

        var track = state.queue[nextIndex];
        if (!track) { return; }

        state.xfadeBusy = true;
        state.xfadePending = nextIndex;
        state.endedCount = 0;
        state.xfadeFrom = from;
        state.xfadeTo = target;
        // フェード先が読み込み失敗して「音が出ない要素」に
        // 差し替えると、その要素は ended を発火せずキューが永久に止まる。
        // 差し替え前に「実際に鳴り始めたか」を必ず確認する。
        target.__xfadeOk = false;

        try {
            target.pause();
            target.src = track.url;
            target.load();
        } catch (e) {
            finishXfade(true);
            onAudioError();
            return;
        }

        // 音量は element.volume でランプする（state.volume / muted は変えない）
        var peak = state.muted ? 0 : clampVolume(state.volume);
        try {
            target.volume = 0;
            target.muted = state.muted;
        } catch (e) { /* noop */ }

        var played = false;
        try {
            var promise = target.play();
            played = true;
            target.__xfadeOk = true;
            if (promise && typeof promise.then === 'function') {
                promise.then(noop).catch(function (error) {
                    // 再生が弾かれた場合はフェードをやめて通常の切替に委ねる
                    if (error && error.name === 'AbortError') { return; }
                    abortXfade(nextIndex);
                });
            }
        } catch (e) {
            played = false;
        }

        if (!played) {
            finishXfade(true);
            playIndex(nextIndex, false);
            return;
        }

        // フェードを stepped に進行させる
        var steps = 14;
        var step = 0;
        if (state.xfadeTimer) { window.clearInterval(state.xfadeTimer); }
        state.xfadeTimer = window.setInterval(function () {
            // どこかで例外が出ても xfadeBusy が残るとキューが止まるので、
            // 進行部分は必ず try/catch で囲み、最終処理は finally で行う。
            try {
                if (target.error || target.__xfadeBroken || !target.__xfadeOk) {
                    // フェード先が壊れていた／鳴り始めていなかった
                    // → 元の要素に戻して通常切替
                    abortXfade(nextIndex);
                    return;
                }
                step += 1;
                var ratio = Math.min(1, step / steps);
                try {
                    from.volume = peak * (1 - ratio);
                    target.volume = peak * ratio;
                } catch (e) { /* noop */ }

                if (ratio < 1) { return; }

                // フェード完了：active を差し替える
                // 先に active を差し替える。
                // from.pause() は onAudioPause を同期発火するため、
                // まだ from が state.audio のままだと VU と真空管が止まってしまう。
                state.activeSlot = idleSlot();
                state.audio = target;
                state.analyser = state.analyserNodes[activeAnalyserSlot()] || state.analyser;
                try { state.audio.volume = peak; } catch (e) { /* noop */ }

                try {
                    from.pause();
                    from.volume = peak;
                } catch (e) { /* noop */ }

                state.index = nextIndex;
                state.finished = false;
                updateTrackMeta(track, nextIndex);
                applyStreamMeta(track, nextIndex);
                setOnAir(ON_AIR_LIVE);
                updateSeekBar();
                prefetchTrack(nextIndex + 1);
                // 新しい要素の play は差し替え前（state.audio !== target）に発火して
                // 無視されているため、VU と真空管はここで明示的に復帰させる
                if (state.vuTimer === 0) {
                    if (state.analyser) { startVuAnalyser(); } else { startVuSimulation(); }
                }
                setTubeLit(true);
            } catch (e) {
                console.error('クロスフェード中にエラーが発生しました', e);
                abortXfade(nextIndex);
            } finally {
                // 完了 or 例外 のどちらでも、必ずフェード状態を解除する
                if (step >= steps) {
                    if (state.xfadeTimer) {
                        window.clearInterval(state.xfadeTimer);
                        state.xfadeTimer = 0;
                    }
                    state.xfadeBusy = false;
                    state.xfadePending = -1;
                    state.xfadeFrom = null;
                    state.xfadeTo = null;
                }
            }
        }, Math.max(16, Math.round(XFADE_MS / steps)));
    }

    // フェード状態だけを安全に片付ける（:element の破棄は行わない）
    function finishXfade(hardStopTarget) {
        if (state.xfadeTimer) {
            window.clearInterval(state.xfadeTimer);
            state.xfadeTimer = 0;
        }
        if (hardStopTarget && state.xfadeTo) {
            try {
                state.xfadeTo.pause();
            } catch (e) { /* noop */ }
        }
        var peak = state.muted ? 0 : clampVolume(state.volume);
        if (state.xfadeTo) {
            try { state.xfadeTo.volume = peak; } catch (e) { /* noop */ }
        }
        state.xfadeBusy = false;
        state.xfadePending = -1;
        state.xfadeFrom = null;
        state.xfadeTo = null;
    }

    // フェード中に次のトラックへの切替が必要になった場合
    function abortXfade(nextIndex) {
        finishXfade(true);
        playIndex(nextIndex, false);
    }

    function cancelXfade() {
        finishXfade(true);
        // フェードで音量を動かして止めた <audio> を元へ戻す
        var peak = state.muted ? 0 : clampVolume(state.volume);
        [state.slots.a, state.slots.b].forEach(function (audio) {
            if (!audio) { return; }
            try { audio.volume = peak; } catch (e) { /* noop */ }
        });
    }

    function updateTrackMeta(track, index) {
        var text = kindLabel(track) + passLabel(track) + ' ' +
            (index + 1) + '/' + state.queue.length + '：' + (track.title || '—');
        if (track.artist) { text += ' / ' + track.artist; }
        if (track.kind === INTERMISSION) {
            text += '（この枠には音源が無いため間奏です）';
        } else {
            text += '（終了後に自動で次へ）';
        }
        setStreamDesc(text);

        if (track.kind === SONG) {
            if (dom.songTitle) { dom.songTitle.textContent = track.title || '—'; }
            if (dom.songArtist) { dom.songArtist.textContent = track.artist || '—'; }
            if (dom.vinylDisk) { dom.vinylDisk.classList.add('now-playing'); }
        } else if (dom.vinylDisk) {
            dom.vinylDisk.classList.remove('now-playing');
        }

        // 読み上げ中なら原稿の該当セグメントを起こす
        highlightManuscript(track);

        // プレイヤー領域のトラック表示・ボタン状態を同期する
        // （再生中のトラック情報が画面に一切出ない問題の修正点）
        renderPlayerUI();
    }

    // 再生中トラックの上部ステータス表示（#streamStatusTitle / #streamStatusDesc）を更新する
    function applyStreamMeta(track, index) {
        setStreamTitle('📻 ラジオ放送中（' + state.year + '年）— ' + kindLabel(track) +
            passLabel(track) + ' ' + (index + 1) + '/' + state.queue.length);
        var text = '▶ ' + (track.title || '—');
        if (track.artist) { text += ' / ' + track.artist; }
        if (track.kind === INTERMISSION) {
            text += '（音源が無いため間奏）';
        } else {
            text += '（終了後に自動で次へ）';
        }
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
        // 周回数は buildQueue の時点でキューへ展開済みなので、
        // ここに到達した時点で节目は終わり。
        state.finished = true;
        state.index = state.queue.length;
        cancelXfade();
        stopVu();
        setTubeLit(false);
        syncAudioButton();
        setOnAir(ON_AIR_ENDED);
        var passes = state.passLength ? Math.max(1, Math.ceil(state.queue.length / state.passLength)) : 1;
        setStreamTitle('📻 番組を終了しました（' + state.year + '年・' + passes + '周）');
        setStreamDesc('もう一度聴く場合は ▶ を押してください。');
        clearManuscriptHighlight();
        // 放送終了の表示も追跡情報は「待機中」へ戻す
        updateSeekBar();
        renderPlayerUI();
    }

    function restartPlayback() {
        if (!state.queue.length) { return; }
        state.finished = false;
        state.userPaused = false;
        state.needGesture = false;
        state.errorStreak = 0;
        setOnAir(ON_AIR_LIVE);
        playIndex(0, false);
    }

    // ended は「フェード完了后的切替」では別の要素から来るため、
    // そのときフェード済みなら何もしない（フェード側で index を進めている）。
    function onTrackEnded(event) {
        if (state.xfadeBusy) { return; }
        var audio = (event && event.target) ? event.target : state.audio;
        if (audio && state.audio && audio !== state.audio) { return; }
        state.endedCount += 1;
        playIndex(state.index + 1, false);
    }

    function onAudioError(event) {
        if (!state.queue.length || state.index < 0) { return; }
        var audio = (event && event.target) ? event.target : state.audio;
        // フェード先の要素でエラーが出たら「音が出ない要素」だと分かるよう印を付ける。
        // 印が無いと差し替え後に ended が来ず、番組が永久に止まる。
        if (state.xfadeBusy && audio && audio === state.xfadeTo) {
            audio.__xfadeBroken = true;
            return;
        }
        // フェード先のエラーは通常の error 経路で扱わない（握り潰してスキップ）
        if (state.xfadeBusy && audio && audio !== state.audio) { return; }
        if (audio && state.audio && audio !== state.audio) { return; }
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

    function onAudioPlay(event) {
        var audio = (event && event.target) ? event.target : state.audio;
        if (audio && state.audio && audio !== state.audio) { return; }
        state.errorStreak = 0;
        state.needGesture = false;
        resumeAudioContext();
        syncAudioButton();
        setTubeLit(true);
        setOnAir(ON_AIR_LIVE);
        // 一時停止で VU のタイマーを止めているので、再生再開時に必ず復帰させる
        if (state.vuTimer === 0) {
            if (state.analyser) { startVuAnalyser(); } else { startVuSimulation(); }
        }
    }

    function onAudioPause(event) {
        var audio = (event && event.target) ? event.target : state.audio;
        if (audio && state.audio && audio !== state.audio) { return; }
        // フェードで古い側を pause する瞬間は、まだ放送は続いている
        if (state.xfadeBusy) { return; }
        syncAudioButton();
        setTubeLit(false);
        if (isPlaying()) {
            setOnAir(ON_AIR_LIVE);
        } else {
            setOnAir(state.finished ? ON_AIR_ENDED : ON_AIR_READY);
        }
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
        // 残り時間が切れたら次のトラックを仕込む（クロスフェードの準備）
        maybeStartXfade();
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
        // フェード中は 2 本の <audio> の音量が動いているため触らない
        if (!state.xfadeBusy) { applyVolumeToAll(); }
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
        if (!state.xfadeBusy) { applyVolumeToAll(); }
        if (dom.btnMute) {
            dom.btnMute.textContent = state.muted ? '🔇' : '🔊';
            dom.btnMute.setAttribute('aria-pressed', state.muted ? 'true' : 'false');
        }
        announce(state.muted ? 'ミュートしました。' : 'ミュートを解除しました。');
    }

    // 周回数を MIN_REPEAT〜MAX_REPEAT へ収める
    function clampRepeat(value) {
        var count = Math.round(Number(value));
        if (!isFinite(count)) { return DEFAULT_REPEAT; }
        if (count < MIN_REPEAT) { return MIN_REPEAT; }
        if (count > MAX_REPEAT) { return MAX_REPEAT; }
        return count;
    }

    // 連続再生の ON / OFF を切り替えて保存する
    // （OFF のときは周回数に関わらず 1 周だけ流す）
    function setLoopEnabled(enabled) {
        state.loopEnabled = !!enabled;
        if (dom.btnLoop) {
            dom.btnLoop.setAttribute('aria-pressed', state.loopEnabled ? 'true' : 'false');
        }
        writeStore(LOOP_STORAGE_KEY, state.loopEnabled ? '1' : '0');
        if (dom.btnRepeat) {
            dom.btnRepeat.disabled = !state.loopEnabled;
            dom.btnRepeat.setAttribute('aria-disabled', state.loopEnabled ? 'false' : 'true');
        }
        syncRepeatButton();
        announce(state.loopEnabled
            ? '連続再生を有効にしました。番組を ' + effectiveRepeatCount() + ' 周流します。'
            : '連続再生を無効にしました。番組を 1 周だけ流します。');
    }

    // 番組の周回数を設定する（1〜5）。キューを作り直す必要があるため再生成する
    function setRepeatCount(value) {
        var next = clampRepeat(value);
        var changed = (next !== state.repeatCount);
        state.repeatCount = next;
        writeStore(REPEAT_STORAGE_KEY, String(next));
        syncRepeatButton();
        if (!changed) { return; }
        announce('番組の周回数を ' + next + ' 周にしました。');
        // 既に組んだキューは周回数ぶん短いので、作り直す
        if (state.lastResult && state.queue.length) {
            var resumeAt = state.index;
            var wasPlaying = isPlaying();
            startPlayback(state.lastResult);
            if (state.queue.length) {
                var target = Math.min(resumeAt, state.queue.length - 1);
                if (target >= 0) { playIndex(target, true); }
                if (!wasPlaying) { toggleAudio(); }
            }
        }
    }

    // 周回数の数字を 1 つ進める（MAX で/min に戻る）
    function cycleRepeatCount() {
        if (!state.loopEnabled) { return; }
        var next = state.repeatCount + 1;
        if (next > MAX_REPEAT) { next = MIN_REPEAT; }
        setRepeatCount(next);
    }

    // #btnRepeat の表示を現在の設定に合わせる
    function syncRepeatButton() {
        if (!dom.btnRepeat) { return; }
        dom.btnRepeat.textContent = '🔂 ' + effectiveRepeatCount() + '周';
        dom.btnRepeat.setAttribute('aria-label',
            '番組の周回数。現在 ' + effectiveRepeatCount() + ' 周。押すと切り替わります。');
        dom.btnRepeat.disabled = !state.loopEnabled;
        dom.btnRepeat.setAttribute('aria-disabled', state.loopEnabled ? 'false' : 'true');
        dom.btnRepeat.classList.toggle('is-off', !state.loopEnabled);
    }

    // 保存済みの音量・連続再生・周回数を復元する
    function applyStoredAudioPrefs() {
        var storedVolume = readStore(VOLUME_STORAGE_KEY, '');
        if (storedVolume !== '' && storedVolume !== null && storedVolume !== undefined) {
            state.volume = clampVolume(storedVolume);
        }
        state.loopEnabled = (readStore(LOOP_STORAGE_KEY, '1') === '1');
        state.repeatCount = clampRepeat(readStore(REPEAT_STORAGE_KEY, String(DEFAULT_REPEAT)));
        // ミュートは保存しない（毎回オフから始める）
        state.muted = false;

        applyVolumeToAll();
        if (dom.volumeControl) { dom.volumeControl.value = String(state.volume); }
        if (dom.btnLoop) {
            dom.btnLoop.setAttribute('aria-pressed', state.loopEnabled ? 'true' : 'false');
        }
        syncRepeatButton();
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
                dom.trackArtist.textContent = kindLabel(track);
            }
        }
        if (dom.trackPass) {
            dom.trackPass.textContent = hasTrack ? (passLabel(track) || '') : '';
            dom.trackPass.hidden = !(hasTrack && passLabel(track));
        }

        syncPlaylistActive();
        syncTrackNavButtons();
        if (dom.btnLoop) {
            dom.btnLoop.setAttribute('aria-pressed', state.loopEnabled ? 'true' : 'false');
        }
        syncRepeatButton();
        if (dom.btnMute) {
            dom.btnMute.textContent = state.muted ? '🔇' : '🔊';
            dom.btnMute.setAttribute('aria-pressed', state.muted ? 'true' : 'false');
        }
    }

    // #playlistList をキューから作り直す（周回の切れ目には区切りを入れる）
    // 描画に失敗しても再生は止めない（この関数が例外を投げると
    // startPlayback ごと中断して番組が始まらないため）。
    function renderPlaylist(trackQueue) {
        try {
            renderPlaylistUnsafe(trackQueue);
        } catch (e) {
            console.error('プレイリストの描画に失敗しました（再生は続行します）', e);
        }
    }

    function renderPlaylistUnsafe(trackQueue) {
        var list = dom.playlistList;
        if (!list) { return; }
        var tracks = Array.isArray(trackQueue) ? trackQueue : [];
        while (list.firstChild) {
            list.removeChild(list.firstChild);
        }
        for (var i = 0; i < tracks.length; i += 1) {
            if (tracks[i].isFirstInPass && i > 0) {
                list.appendChild(buildPassDivider(tracks[i]));
            }
            list.appendChild(buildPlaylistItem(tracks[i], i));
        }
        syncPlaylistActive();
    }

    // 「2周目」の境目に入れる区切り行
    function buildPassDivider(track) {
        var item = document.createElement('li');
        item.className = 'playlist-pass-divider';
        item.setAttribute('aria-hidden', 'true');
        item.textContent = '── ' + (track.pass || 2) + ' 周目 ──';
        return item;
    }

    // 1 件のプレイリスト行を作る（テキストは textContent で入れる）
    function buildPlaylistItem(track, index) {
        var item = document.createElement('li');
        // <button> にして UA スタイルが漏れないようにする
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'playlist-item';
        button.setAttribute('data-track-index', String(index));
        if (track.kind === INTERMISSION) {
            button.classList.add('is-intermission');
        }

        var num = document.createElement('span');
        num.className = 'playlist-item-index';
        num.textContent = String(index + 1);

        var label = document.createElement('span');
        label.className = 'playlist-item-label';
        label.textContent = (track.kind === SONG ? '🎵 ' : (track.kind === INTERMISSION ? '🎼 ' : '🎙️ ')) +
            (track.title || '—');

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
        // 参照先が未定義だと addEventListener 時点で ReferenceError になり、
        // 呼び出し元（startPlayback → renderPlaylist）ごと中断して
        // 何も始まらないため、ハンドラは必ず同じファイル内に定義する。
        button.addEventListener('click', onPlaylistItemClick);
        item.appendChild(button);
        return item;
    }

    // プレイリストの行を押したらそのトラックから再生する
    function onPlaylistItemClick(event) {
        var button = (event && event.currentTarget) ? event.currentTarget : null;
        if (!button || typeof button.getAttribute !== 'function') { return; }
        var raw = button.getAttribute('data-track-index');
        var index = Number(raw);
        if (!isFiniteNumber(index) || index < 0 || index >= state.queue.length) { return; }
        state.errorStreak = 0;
        state.endedCount = 0;
        state.userPaused = false;
        state.needGesture = false;
        state.finished = false;
        playIndex(index, true);
        // 自動再生がブロックされている環境では、クリックはユーザー操作なので
        // そのまま再生できる。念のため tryPlay を走らせる。
        tryPlay();
    }

    // 再生中トラックに .active を付ける
    // （区切り行が混ざるので data-track-index で照合する）
    function syncPlaylistActive() {
        var list = dom.playlistList;
        if (!list) { return; }
        var items = list.getElementsByClassName('playlist-item');
        for (var i = 0; i < items.length; i += 1) {
            var raw = items[i].getAttribute('data-track-index');
            var index = Number(raw);
            if (index === state.index) {
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
        if (dom.btnRepeat) {
            dom.btnRepeat.addEventListener('click', cycleRepeatCount);
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
       ON AIR バッジ / 原稿キューシート / 番組表
       ---------------------------------------------------------------------
       ラジオ番組らしく見せるための 3 つ。
       ・ON AIR バッジは実際の状態（待機 / 放送中 / 終了）を表す
       ・原稿は「今どこを朗読中か」が分かるよう該当セグメントを起こす
       ・番組表はバックエンドが返す program_guide をそのまま見せる
       ===================================================================== */

    // ON AIR バッジの状態を切り替える
    function setOnAir(label) {
        if (!dom.onAirBadge) { return; }
        dom.onAirBadge.textContent = label;
        var live = (label === ON_AIR_LIVE);
        dom.onAirBadge.classList.toggle('is-live', live);
        dom.onAirBadge.classList.toggle('is-ended', label === ON_AIR_ENDED);
        dom.onAirBadge.setAttribute('aria-label',
            live ? '放送中' : (label === ON_AIR_ENDED ? '放送終了' : '待機中'));
    }

    // 読み上げ中なら原稿の該当セグメントを起こす（それ以外は解除）
    function highlightManuscript(track) {
        if (!dom.manuscriptBody) { return; }
        if (!track || track.kind !== TALK) {
            clearManuscriptHighlight();
            return;
        }
        var index = track.segmentIndex;
        if (!isFiniteNumber(index)) { return; }

        var blocks = dom.manuscriptBody.querySelectorAll('.script-block');
        for (var i = 0; i < blocks.length; i += 1) {
            var raw = blocks[i].getAttribute('data-segment-index');
            if (Number(raw) === index) {
                blocks[i].classList.add('is-now-reading');
                scrollManuscriptIntoView(blocks[i]);
                return;
            }
        }
    }

    function clearManuscriptHighlight() {
        if (!dom.manuscriptBody) { return; }
        var blocks = dom.manuscriptBody.querySelectorAll('.is-now-reading');
        for (var i = 0; i < blocks.length; i += 1) {
            blocks[i].classList.remove('is-now-reading');
        }
    }

    // 縦に長い原稿でも読者が今いる場所へ飛べるよう、近いときだけスクロールする
    function scrollManuscriptIntoView(block) {
        if (!block || typeof block.getBoundingClientRect !== 'function') { return; }
        try {
            if (typeof block.scrollIntoView === 'function') {
                block.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            }
        } catch (e) {
            // 古いブラウザでは options を解釈しないため引数なしで試す
            try { block.scrollIntoView(); } catch (e2) { /* noop */ }
        }
    }

    // 番組表（program_guide）を #programGuide へ描画する
    function renderProgramGuide(guide) {
        if (!dom.programGuide) { return; }
        while (dom.programGuide.firstChild) {
            dom.programGuide.removeChild(dom.programGuide.firstChild);
        }
        if (!guide || typeof guide !== 'object') {
            dom.programGuide.hidden = true;
            return;
        }
        var schedules = Array.isArray(guide.schedules) ? guide.schedules : [];
        if (!schedules.length) {
            dom.programGuide.hidden = true;
            return;
        }

        var header = document.createElement('p');
        header.className = 'program-guide-header';
        var dateText = String(guide.date || '');
        var weekday = String(guide.weekday || '');
        header.textContent = '📅 今日の番組表（' + dateText +
            (weekday ? '（' + weekday + '）' : '') + '）';
        dom.programGuide.appendChild(header);

        var list = document.createElement('ol');
        list.className = 'program-guide-list';
        schedules.forEach(function (item) {
            if (!item || typeof item !== 'object') { return; }
            var row = document.createElement('li');
            row.className = 'program-guide-row';
            if (item.is_historical) { row.classList.add('is-historical'); }

            var time = document.createElement('span');
            time.className = 'program-guide-time';
            time.textContent = String(item.start_time || '--:--');

            var title = document.createElement('span');
            title.className = 'program-guide-title';
            title.textContent = String(item.title || '（無題）');

            row.appendChild(time);
            row.appendChild(title);

            if (item.description) {
                var desc = document.createElement('span');
                desc.className = 'program-guide-desc';
                desc.textContent = String(item.description);
                row.appendChild(desc);
            }

            list.appendChild(row);
        });
        dom.programGuide.appendChild(list);
        dom.programGuide.hidden = false;
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
            if (!state.analyserNodes.a && !state.analyserNodes.b) {
                // クロスフェードで交互に使う 2 本の両方に解析ノードを張る。
                // createMediaElementSource は 1 要素につき 1 回しか呼べないので、
                // ここで両方を Ensure しないと途中で差し替えられない。
                ['a', 'b'].forEach(function (slot) {
                    var audio = state.slots[slot];
                    if (!audio) { return; }
                    var source = state.audioCtx.createMediaElementSource(audio);
                    var analyser = state.audioCtx.createAnalyser();
                    analyser.fftSize = 256;
                    analyser.smoothingTimeConstant = 0.72;
                    source.connect(analyser);
                    analyser.connect(state.audioCtx.destination);
                    state.analyserNodes[slot] = analyser;
                });
                if (state.analyserNodes.a) {
                    state.analyserAttached = true;
                }
            }
            state.analyser = state.analyserNodes[activeAnalyserSlot()] || state.analyser;
            startVuAnalyser();
        } catch (e) {
            state.audioCtx = null;
            state.analyser = null;
            state.analyserNodes.a = null;
            state.analyserNodes.b = null;
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
        dom.btnRepeat = byId('btnRepeat');
        dom.playlistList = byId('playlistList');
        dom.onAirBadge = byId('onAirBadge');
        dom.trackPass = byId('trackPass');
        dom.programGuide = byId('programGuide');
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
            cancelXfade();
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

        // クロスフェード用に 2 本を用意する
        createAudioPair();
        // 前回送信した内容を再試行用に復元する
        state.lastRequest = loadLastRequest();

        setServiceStatus('', '⏳ サーバー状態を確認しています…');
        setStreamTitle(DEFAULT_STATUS_TITLE);
        setStreamDesc(DEFAULT_STATUS_DESC);
        setOnAir(ON_AIR_READY);

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
