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

    // gTTS を 5〜6 回連続実行するため応答生成は長めに取る。
    // バックエンドの SSE 上限（jobs.SSE_MAX_SECONDS = 900 秒）と揃える。
    // 短いと正常な生成をクライアント側だけが打ち切ってしまう。
    var GENERATE_TIMEOUT_MS = 900000;
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
       受信進捗
       ---------------------------------------------------------------------
       `POST /api/jobs` が返る SSE イベント（`retro_radio/jobs.py` の
       `UI_LABEL_BY_EVENT` と同じ写像）で行计划和ステップを駆動する。
       SSE が使えない環境（EventSource なし / 旧バックエンド）では
       PROGRESS_TIMELINE の「経過時間ベースの目安」にフォールバックする。
    */
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

    /* --- 受信待ちの充填音（待機音 bed） --------------------------------------
       ボタンを押してから番組が鳴るまで 30〜60 秒、その間はずっと無音だった。
       ここはその待ち時間を「受信ノイズ」として埋めるための定数。

       方針:
         - 音声ファイルを持たない（オフラインでも必ず鳴る）。
         - MediaElementSource は使わない。キューが別オリジン
           （iTunes プレビュー）を含むと、CORS で無音になる既知の罠があるため
           （ファイル冒頭の設計注記）解析系には一切触らせない。
         - AudioContext が取れないブラウザでは静かに何もしない（VU と同じ方針）。
    ------------------------------------------------------------------- */
    var STANDBY_MASTER_GAIN = 0.05;   // 待機音の主音量（控えめにする）
    var STANDBY_HISS_GAIN = 0.35;     // テープヒス（ノイズ）成分
    var STANDBY_HUM_FREQ = 60;        // 電源ハム
    var STANDBY_HUM_GAIN = 0.10;
    var STANDBY_FADE_IN_SECONDS = 0.6;
    var STANDBY_FADE_OUT_SECONDS = 0.35;
    var STANDBY_NOISE_SECONDS = 2;    // ノイズバッファの長さ（ループ）
    // 各ステップに進んだときの効果音（Hz / 長さ秒 / 波形 / ピーク音量）
    var STANDBY_STEP_CUES = {
        connect: { freq: 620, dur: 0.09, type: 'square', gain: 0.5 },
        script: { freq: 880, dur: 0.07, type: 'triangle', gain: 0.45 },
        tts: { freq: 1180, dur: 0.09, type: 'triangle', gain: 0.45 },
        song: { freq: 1560, dur: 0.14, type: 'sine', gain: 0.5 }
    };

    /* --- ジョブ API（SSE）--- */
    /** SSE イベント名 → PROGRESS_STEPS のキー（`jobs.UI_LABEL_BY_EVENT` と同じ写像） */
    var JOB_STEP_BY_EVENT = {
        'job.started': 'connect',
        'script.started': 'script',
        'script.done': 'script',
        'tts.segment': 'tts',
        'tts.done': 'tts',
        'music.started': 'song',
        'music.search.done': 'song',
        'playlist.done': 'song'
    };
    /** ここでストリームを有限に閉じる終端イベント（`jobs.TERMINAL_EVENTS`） */
    var JOB_TERMINAL_EVENTS = ['done', 'failed', 'cancelled'];
    /** ラベルに対応しないイベント（進捗・終端）。UI は無視する。 */
    var JOB_IGNORED_EVENTS = ['estimate'];
    /** ポーリングへ落ちたときの既定間隔（ms）。 */
    var JOB_POLL_MIN_MS = 1000;

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

    // 間奏スロットの表示名。**借り物の曲名を使わない。**
    // サーバーが選曲枠を埋めるためにカタログから借りた曲名は
    // 原稿では一度も紹介されないため、そのまま表示すると
    // 「司会は紹介していないのに番組表に載る」状態になる。
    var INTERMISSION_TITLE = '間奏';

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
        // 3ステップ導線の進行度（0〜3 / 後退しない）
        flowStep: 0,
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
        silenceUrls: null,
        preloader: null,
        queue: [],
        index: -1,
        endedCount: 0,
        errorStreak: 0,
        lastErrorIndex: -1,
        playbackRate: 1,
        playedIndex: -1,
        repeatCountUserSet: false,
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
        segmentCount: 0,
        /* --- 受信待ちの充填音（Web Audio 合成。音声ファイルは使わない） --- */
        standbyActive: false,
        standbyNodes: null,
        standbyCuedStep: '',
        standbyUnsupported: false,
        standbyNoise: null,
        standbyNoiseRate: 0,
        /* --- ジョブ API / SSE --- */
        jobId: '',
        jobStream: null,
        jobStreamJob: '',
        jobStreamToken: 0,
        jobPollTimer: 0,
        jobLastSeq: 0,
        jobEventsSeen: false,
        progressEstimateMs: 0,
        progressDriftAnnounced: false,
        /* --- 認証 --- */
        health: null,
        authEnforced: false,
        /* --- 同意（提案⑧ / P1-2）: 起動時に GET /api/me/consent で確認する --- */
        consentRequired: false,
        consentAccepted: false,
        consentDeclined: false,
        /* 番組を生成した年（ダイヤルの年 state.year と区別する） */
        programYear: 0
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

    /**
     * Apple への送客導線リンクの表示を出し入れする。
     *
     * URL が空（サーバーが導線を無効化している／音源が無い間奏／
     * クライアント側のホスト検証で落ちた）のときは**隠す**。`hidden`
     * を外したまま `href="#"` を残すと、クリックしても何もしない
     * ダミーリンクが画面に残るため。
     */
    function showStoreLink(url) {
        var safe = safeStoreUrl(url);
        if (!safe) {
            if (dom.songStoreLink) { dom.songStoreLink.hidden = true; }
            if (dom.songStoreAnchor) { dom.songStoreAnchor.removeAttribute('href'); }
            return;
        }
        if (dom.songStoreAnchor) {
            dom.songStoreAnchor.setAttribute('href', safe);
        }
        if (dom.songStoreLink) {
            dom.songStoreLink.hidden = false;
        }
    }

    /**
     * Apple ストアへの導線 URL を取り込む。
     *
     * サーバー側は既に `music.apple.com` / `itunes.apple.com` 以外を
     * 弾いているが、これは多层防御である（クライアント側の制約）。
     * `javascript:` や `data:` をそのまま href に載せないよう、
     * クライアント側でも同じホスト制約を掛け直す。
     */
    function safeStoreUrl(url) {
        var raw = String(url === null || url === undefined ? '' : url).trim();
        if (!raw) { return ''; }
        var absolute;
        try {
            absolute = new URL(raw, window.location.href);
        } catch (e) {
            return '';
        }
        if (absolute.protocol !== 'https:') { return ''; }
        var host = absolute.hostname.toLowerCase();
        if (host !== 'music.apple.com' && host !== 'itunes.apple.com') { return ''; }
        return absolute.href;
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
        // バックエンドの segment_index は `###` 見出しごとの採番なので、
        // 見出しの前にある前置文ブロックを数に入れると全部ずれる。
        // 見出し付きブロックだけを採番対象にする。
        var segmentSeq = -1;
        for (var i = 0; i < blocks.length; i += 1) {
            var block = blocks[i];
            if (block.title) { segmentSeq += 1; }
            html.push('<section class="script-block"' +
                (block.title ? ' data-segment-index="' + segmentSeq + '"' : '') + '>');
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
        markFlowYearChosen();

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
            // aria-valuenow はスライダー（role="slider"）用の属性。
            // #yearInput は type="text" なので、付けえると
            // スクリーンリーダーが不正な値を読み上げてしまう。
        }
        if (dom.brassKnob) {
            dom.brassKnob.setAttribute('aria-valuenow', String(value));
            dom.brassKnob.setAttribute('aria-valuetext', value + '年 ' + toEraYear(value));
            // MIN_YEAR / MAX_YEAR は /api/decades の結果で変わるため、
            // ここで毎回 aria-valuemin / aria-valuemax を追従させる。
            dom.brassKnob.setAttribute('aria-valuemin', String(MIN_YEAR));
            dom.brassKnob.setAttribute('aria-valuemax', String(MAX_YEAR));
        }
        if (dom.tunerRail) {
            dom.tunerRail.setAttribute('aria-valuemin', String(MIN_YEAR));
            dom.tunerRail.setAttribute('aria-valuemax', String(MAX_YEAR));
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
            // 高齢者向け操作基準: すでに選択済みの年代の再押下は
            // 「操作が効かなかった」ように見えないよう、 acknowledge する。
            if (target === last && decadeOf(state.year) === last) {
                announce(toEraYear(last) + 'の年代です。ダイヤルで年を微調整できます。');
                return;
            }
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
            // 指がスライダー外へ出たときにドラッグ状態を確実に落とす（高齢者向け操作基準）
            rail.addEventListener('touchcancel', function () { state.draggingRail = false; }, { passive: true });

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

            knob.addEventListener('touchcancel', function () {
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
            // タブパネル（#modePanel）を現在のタブに紐づける
            if (active && dom.modePanel) {
                dom.modePanel.setAttribute('aria-labelledby', tab.el.id || '');
            }
        });

        // モード別ボックスはタブパネル（ARIA APG）として hidden 属性で開閉する。
        // CSS の .visible は装飾だけに使い、意味論は hidden に持たせる。
        if (dom.careModeBox) {
            dom.careModeBox.classList.toggle('visible', mode === 'care_recreation');
            dom.careModeBox.hidden = (mode !== 'care_recreation');
            dom.careModeBox.setAttribute('aria-controls', 'careModeBox');
        }
        if (dom.anniversaryModeBox) {
            dom.anniversaryModeBox.classList.toggle('visible', mode === 'anniversary');
            dom.anniversaryModeBox.hidden = (mode !== 'anniversary');
            dom.anniversaryModeBox.setAttribute('aria-controls', 'anniversaryModeBox');
        }
        if (mode !== 'care_recreation' && dom.recreationQuizBox) {
            dom.recreationQuizBox.style.display = 'none';
        }

        // モードが変わると進行中のリクエストと再生内容は無効になる
        cancelGeneration(true);
        stopPlayback();
        markFlowModeChosen();
        // 前のモードの結果は表示しない（空状態に戻して案内を出す）
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
            // 今日の日付とダイヤルの年を組み合わせても実在する日か確認する。
            // 2月29日に 1975 年を選ぶと 422 で拒否され、利用者は原因を診断できない。
            if (!isValidDate(year, month, day)) {
                return {
                    error: year + '年の' + month + '月' + day + '日は存在しない日付です（' + daysInMonth(year, month) + '日までです）。ダイヤルの年を合わせてください。',
                    field: null
                };
            }
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
            /* 同意ゲートも同じボタンを管理するため、**どちらの状態が勝つかで
               分岐しない**よう、ここで同意状態を参照する（applyConsentGate が
               本当の状態を持ち、ここは導出に徹する）。 */
            button.disabled = !!loading || consentBlocksInput();
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
        // 実行中ならサーバー側のジョブも止めさせる（abort はスレッドを殺せないため）
        requestJobCancel();
        // ストリームを閉じた後はジョブを忘れてよい（再 DELETE も再購読もしない）
        state.jobId = '';
        closeJobStream();
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

    /**
     * 認証・資格情報・混雑を、原因ごとに**別々の文言**へ落とす。
     * 503 は原因が複数あるので detail を見て切り分ける
     * （以前はすべてを「APIキー未設定」で済ませていた）。
     */
    function describeError(status, statusText, payload, rawText) {
        var detail = extractDetail(payload);
        var suffix = detail ? '\n\nサーバーの応答: ' + detail : '';

        if (status === 401) {
            return '🔒 認証が必要です（HTTP 401）。\n' +
                'このサーバーはログインが必要な設定です。上部のログインフォームから\n' +
                'サインインしてから、もう一度お試しください。' + suffix;
        }

        if (status === 403) {
            return '🚫 このアカウントでは放送できません（HTTP 403）。\n' +
                '既に削除済みのアカウントか、利用許可が無い状態の可能性があります。\n' +
                'サーバー管理者へお問い合わせください。' + suffix;
        }

        if (status === 503) {
            // 混雑（同時実行上限）
            if (/混雑|混み|busy|too many|待機/i.test(detail)) {
                return '⏳ 混雑しています（HTTP 503）。\n' +
                    '他の番組が生成中のため、少し待ってから再度お試しください。\n' +
                    '（実行中の番組を「⏹ 受信を中止する」で止めると早く入れます）' + suffix;
            }
            // 認証の資格情報が未設定（サーバー側の設定不備）
            if (/認証|資格情報|secret|SECRET|session/i.test(detail)) {
                return '🔧 サーバーの認証設定が未設定です（HTTP 503）。\n' +
                    '環境変数 RETRO_RADIO_SECRET_KEY が設定されていないか、\n' +
                    'ログイン用のユーザー登録ができていない可能性があります。\n' +
                    'サーバー管理者へ設定の確認を依頼してください。' + suffix;
            }
            // Gemini APIキー未設定
            if (/API|キー|key/i.test(detail)) {
                return '⚠️ Gemini APIキーが未設定のため原稿を生成できません。\n' +
                    'サーバー管理者へ環境変数 RETRO_RADIO_GEMINI_API_KEY の設定を依頼してください。\n' +
                    '（このアプリはブラウザに APIキーを保存しません）' + suffix;
            }
            return '⚠️ サーバー側で原稿生成の準備ができていません（HTTP 503）。' + suffix;
        }

        if (status === 422) {
            // 実際の原因は detail。renderError() が可視メッセージへ出すので、
            // ここでは汎用の案内に留めて重複させない。
            return '⚠️ 入力内容が不正です（HTTP 422）。\n' +
                '送信内容（年月日・モード・記念日の入力）を確認してください。';
        }

        if (status === 404) {
            return '⚠️ 対象のAPIが見つかりません（HTTP 404）。バックエンドが最新かどうか確認してください。' +
                suffix;
        }

        if (status >= 500) {
            return '⚠️ サーバー側でエラーが発生しました（HTTP ' + status + '）。しばらく待ってから再試行してください。' +
                suffix;
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
        // 3ステップすべて終わった状態にする
        markFlowPlaying();
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
        // 新しい受信が始まったら前のジョブの残骸を捨てる
        closeJobStream();
        state.jobId = '';
        state.jobLastSeq = 0;
        state.jobEventsSeen = false;
        setLoading(true);
        // 送信直前に進捗タイマーを開始する（100% は renderSuccess でのみ）
        startProgress(form.year, form.mode);

        setStreamTitle('📡 ' + form.year + '年の電波を受信中…');
        setStreamDesc('【' + modeLabel(form.mode) + '】原稿を生成しています。ナレーション音声の合成も行うため、30〜60秒ほどお待ちください…（待機中はラジオのノイズを鳴らしています）');

        var payload = {
            year: form.year,
            month: form.month,
            day: form.day,
            mode: form.mode
        };
        if (form.mode === 'anniversary' && form.targetName) {
            payload.target_name = form.targetName;
        }

        // ジョブ API が無い旧バックエンドへ安全に落とせるよう、
        // どちらの経路でも同じ AbortController / タイムアウトを使う。
        var controller = createController();
        state.abortController = controller;
        state.timeoutTimer = window.setTimeout(function () {
            state.timeoutTimer = 0;
            state.timedOut = true;
            // abort してもサーバーのスレッドは止まらない。ジョブが走っていれば
            // 協調的キャンセルを明示的に要求する。
            requestJobCancel();
            safeAbort(controller);
        }, GENERATE_TIMEOUT_MS);

        startJobRequest(form, payload, controller, token);
    }

    /* =====================================================================
       ジョブ API（POST /api/jobs + SSE）
       ---------------------------------------------------------------------
       * まず `POST /api/jobs` を試す。202 が返ったら `events_url` を SSE で購読する。
       * 404 / 405 / 501（未実装の旧バックエンド）なら従来の同期版にフォールバックする。
       * EventSource が無い / ストリームが切れた場合は `GET /api/jobs/{id}` の
         ポーリングへ落とす（`last_event_id` で続きから取る）。
       * ストリームは必ず 1 本だけ。終端イベント・新しい受信・中止で必ず閉じる。
       ===================================================================== */

    // JSON を返す fetch ヘルパ。text を先に読んでから JSON にする
    // （FastAPI のエラーは detail 配列で返りうるため）
    function fetchJson(url, options) {
        return window.fetch(url, options).then(function (response) {
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
        });
    }

    function startJobRequest(form, payload, controller, token) {
        var options = {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            // セッション cookie を送る。`<audio src>` も cookie 前提のため
            // 同じオリジンの cookie を明示的に含める。
            credentials: 'same-origin',
            body: JSON.stringify(payload),
            signal: controller.signal,
            cache: 'no-store'
        };

        fetchJson('/api/jobs', options)
            .then(function (result) {
                if (state.requestToken !== token) { return null; }
                if (state.timedOut) { return null; }

                // 旧バックエンド（/api/jobs 無し）は同期版へ
                if (result.status === 404 || result.status === 405 || result.status === 501) {
                    return startSyncGeneration(form, payload, controller, token);
                }
                if (!result.ok) {
                    renderError(result);
                    return null;
                }
                var jobId = String((result.data && result.data.job_id) || '');
                if (!jobId) {
                    return startSyncGeneration(form, payload, controller, token);
                }
                state.jobId = jobId;
                if (isFiniteNumber(result.data.estimated_ms) && result.data.estimated_ms > 0) {
                    // 実測 p50/p95 から計算された推定。進捗の「目安」を出す。
                    state.progressEstimateMs = result.data.estimated_ms;
                    setStreamDesc('【' + modeLabel(form.mode) + '】ジョブを受け付けました（' +
                        '推定 ' + formatElapsed(result.data.estimated_ms) + '程度）。' +
                        '進捗はサーバーからの実測イベントで更新されます…');
                }
                openJobStream(jobId, token);
                return null;
            })
            .catch(function (error) {
                if (state.requestToken !== token) { return; }
                if (error && error.name === 'AbortError') { return; }
                // ジョブが開いている = POST は成功している。この経路で同期版へ
                // 落とすと二重生成になるので、落ちない。
                if (state.jobId) {
                    renderError({
                        status: 0, statusText: '', data: null, text: '',
                        message: '⚠️ 進捗の取得に失敗しました。\nもう一度お試しください。'
                    });
                    return;
                }
                // ジョブ API 自体が到達不能なら同期版を試す
                startSyncGeneration(form, payload, controller, token);
            });
    }

    // 従来の同期版 `POST /api/generate`（ジョブ API が無い環境のフォールバック）
    function startSyncGeneration(form, payload, controller, token) {
        return fetchJson('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(payload),
            signal: controller.signal,
            cache: 'no-store'
        })
            .then(function (result) {
                if (state.requestToken !== token) { return; }
                if (state.timedOut) {
                    renderError({ status: 408, statusText: 'Request Timeout', data: null, text: '' });
                    return;
                }
                if (!result.ok) {
                    renderError(result);
                    return;
                }
                renderSuccessSafely(form, result.data);
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
            .then(afterRequestFinished, afterRequestFinished);
    }

    // renderSuccess 内の例外を「ネットワークエラー(status 0)」にすり替えない。
    // 例外を投げた時点で握り潰さず、原因の分かるエラーとして見せる。
    function renderSuccessSafely(form, data) {
        try {
            renderSuccess(form, data);
        } catch (error) {
            if (error && typeof console !== 'undefined' && console.error) {
                console.error('番組の表示に失敗しました', error);
            }
            renderError({
                status: 0,
                statusText: '',
                data: null,
                text: '',
                message: '⚠️ 番組の受信には成功しましたが、表示の途中でエラーが発生しました。\n' +
                    'もう一度お試しください。（表示処理の例外でありサーバー障害ではありません）'
            });
        }
    }

    // 受信チェーンの後始末。成否によらず必ず走る（冪等なので二重呼び出しも安全）。
    function afterRequestFinished() {
        if (state.timeoutTimer) {
            window.clearTimeout(state.timeoutTimer);
            state.timeoutTimer = 0;
        }
        state.abortController = null;
        setLoading(false);
    }

    // EventSource は window 経由でしか触らない（未定義ブラウザで ReferenceError に
    // ならないよう、素の識別子参照を避けている）
    function canUseEventSource() {
        return typeof window.EventSource === 'function';
    }

    function makeEventSource(url) {
        return new window.EventSource(url);
    }

    // SSE を 1 本だけ開く。開いているものは必ず先に閉じる（多重 open を防ぐ）。
    function openJobStream(jobId, token) {
        closeJobStream();
        if (!jobId) { return; }
        if (!canUseEventSource()) {
            startJobPolling(jobId, token);
            return;
        }

        var url = '/api/jobs/' + encodeURIComponent(jobId) + '/events';
        var stream;
        try {
            // same-origin の cookie（セッション）は EventSource が自動で送る
            stream = makeEventSource(url);
        } catch (e) {
            startJobPolling(jobId, token);
            return;
        }
        state.jobStream = stream;
        state.jobStreamJob = jobId;
        state.jobStreamToken = token;

        var names = [];
        for (var name in JOB_STEP_BY_EVENT) {
            if (Object.prototype.hasOwnProperty.call(JOB_STEP_BY_EVENT, name)) { names.push(name); }
        }
        for (var i = 0; i < names.length; i += 1) {
            bindStreamEvent(stream, names[i], jobId, token);
        }
        for (var j = 0; j < JOB_TERMINAL_EVENTS.length; j += 1) {
            bindStreamEvent(stream, JOB_TERMINAL_EVENTS[j], jobId, token);
        }
        for (var k = 0; k < JOB_IGNORED_EVENTS.length; k += 1) {
            bindStreamEvent(stream, JOB_IGNORED_EVENTS[k], jobId, token);
        }

        // 接続できない / 切断したらポーリングへ落とす。EventSource 自身の
        // 再接続は Last-Event-ID を自動で送るが、ポーリングへ移る方が確実で、
        // 「開いたまま放置される EventSource」を残さない。
        stream.onerror = function () {
            if (state.requestToken !== token) { return; }
            if (state.jobId !== jobId) { return; }
            closeJobStream();
            startJobPolling(jobId, token);
        };
    }

    function bindStreamEvent(stream, name, jobId, token) {
        stream.addEventListener(name, function (event) {
            if (state.requestToken !== token) { return; }
            if (state.jobId !== jobId) { return; }
            handleJobEvent(name, event, jobId, token);
        });
    }

    // SSE ストリームを必ず閉じる（多重 open とリークを防ぐ単一出口）
    function closeJobStream() {
        if (state.jobPollTimer) {
            window.clearTimeout(state.jobPollTimer);
            state.jobPollTimer = 0;
        }
        var stream = state.jobStream;
        state.jobStream = null;
        state.jobStreamJob = '';
        state.jobStreamToken = 0;
        if (!stream) { return; }
        try { stream.onerror = null; } catch (e) { /* noop */ }
        try { stream.close(); } catch (e) { /* noop */ }
    }

    // 実行中ジョブに協調的キャンセルを要求する（失敗しても UI は止めない）
    function requestJobCancel() {
        var jobId = state.jobId;
        if (!jobId) { return; }
        try {
            window.fetch('/api/jobs/' + encodeURIComponent(jobId), {
                method: 'DELETE',
                headers: { 'Accept': 'application/json' },
                credentials: 'same-origin',
                cache: 'no-store'
            }).then(function (response) {
                return response.text().then(function () { return response.status; });
            }).then(function () {
                // 応答は読み捨て（破棄しても例外は投げない）
            }).catch(noop);
        } catch (e) {
            // 送信できなくても UI 側の停止は続行する
        }
    }

    // EventSource が使えない / 切れたときのポーリング代替
    function startJobPolling(jobId, token) {
        if (state.jobPollTimer) { window.clearTimeout(state.jobPollTimer); }
        state.jobPollTimer = window.setTimeout(function () {
            state.jobPollTimer = 0;
            pollJobOnce(jobId, token);
        }, JOB_POLL_MIN_MS);
    }

    function pollJobOnce(jobId, token) {
        if (state.requestToken !== token || state.jobId !== jobId) { return; }
        var url = '/api/jobs/' + encodeURIComponent(jobId);
        if (state.jobLastSeq > 0) {
            url += '?last_event_id=' + encodeURIComponent(String(state.jobLastSeq));
        }
        fetchJson(url, {
            headers: { 'Accept': 'application/json' },
            credentials: 'same-origin',
            cache: 'no-store'
        })
            .then(function (result) {
                if (state.requestToken !== token || state.jobId !== jobId) { return; }
                if (!result.ok) {
                    // 404 = ジョブが消えている（TTL 超過など）。SSE を諦めて同期扱いにする。
                    if (result.status === 404 || result.status === 401 || result.status === 403) {
                        closeJobStream();
                        state.jobId = '';
                        renderError(result);
                        return;
                    }
                    startJobPolling(jobId, token);
                    return;
                }
                var snapshot = result.data || {};
                var events = Array.isArray(snapshot.events) ? snapshot.events : [];
                for (var i = 0; i < events.length; i += 1) {
                    applyJobEvent(events[i].event, events[i].data, events[i].seq);
                }
                if (snapshot.state === 'succeeded') {
                    finishJobWithResult(snapshot.result, token);
                    return;
                }
                if (snapshot.state === 'failed') {
                    finishJobWithFailure(snapshot.error, token);
                    return;
                }
                if (snapshot.state === 'cancelled') {
                    finishJobAsCancelled(token);
                    return;
                }
                startJobPolling(jobId, token);
            })
            .catch(function () {
                if (state.requestToken !== token || state.jobId !== jobId) { return; }
                // 通信断はリトライ（サーバーはまだ走っている可能性がある）
                startJobPolling(jobId, token);
            });
    }

    /**
     * SSE / ポーリングのイベントを 1 件処理する。
     * `label` ではなく **イベント名** でステップを決める（バックエンドの
     * `UI_LABEL_BY_EVENT` と同じ写像を前端に持つ）。
     */
    function applyJobEvent(name, data, seq) {
        if (isFiniteNumber(seq) && seq > state.jobLastSeq) {
            state.jobLastSeq = seq;
        }
        var step = JOB_STEP_BY_EVENT[name];
        if (!step) { return false; }   // 終端 / 進捗系は別で扱う
        state.jobEventsSeen = true;
        var index = PROGRESS_STEPS.indexOf(step);
        if (index < 0) { return false; }
        for (var i = 0; i <= index; i += 1) {
            setProgressStep(PROGRESS_STEPS[i], i < index ? 'completed' : 'active');
        }
        // 実測されたイベント。90% で頭打ち（100% は renderSuccess のみ）
        var percent = Math.min(PROGRESS_MAX_PERCENT, 10 + (index * 22));
        state.progressPercent = percent;
        if (dom.progressFill) { dom.progressFill.style.width = String(percent) + '%'; }
        if (dom.progressTrack) { dom.progressTrack.setAttribute('aria-valuenow', String(percent)); }
        return true;
    }

    function handleJobEvent(name, event, jobId, token) {
        var data = {};
        if (event && typeof event.data === 'string') {
            try { data = JSON.parse(event.data); } catch (e) { data = {}; }
        }
        if (event && isFiniteNumber(event.lastEventId) && event.lastEventId > state.jobLastSeq) {
            state.jobLastSeq = event.lastEventId;
        }
        if (isFiniteNumber(data.seq) && data.seq > state.jobLastSeq) {
            state.jobLastSeq = data.seq;
        }
        if (JOB_IGNORED_EVENTS.indexOf(name) >= 0) { return; }

        var step = JOB_STEP_BY_EVENT[name];
        if (step) { applyJobEvent(name, data, data.seq); return; }
        if (JOB_TERMINAL_EVENTS.indexOf(name) < 0) { return; }

        // 終端イベント。必ずストリームを閉じてから後処理する。
        closeJobStream();
        if (name === 'done') {
            fetchJobResult(jobId, token);
            return;
        }
        if (name === 'failed') {
            finishJobWithFailure(
                { reason: data.reason, detail: '', status: data.status },
                token
            );
            return;
        }
        finishJobAsCancelled(token);
    }

    // `done` イベントには結果が入らないので、スナップショットを取りに行く
    function fetchJobResult(jobId, token) {
        fetchJson('/api/jobs/' + encodeURIComponent(jobId), {
            headers: { 'Accept': 'application/json' },
            credentials: 'same-origin',
            cache: 'no-store'
        })
            .then(function (result) {
                if (state.requestToken !== token) { return; }
                if (!result.ok || !result.data) {
                    renderError(result.ok
                        ? { status: 500, statusText: '', data: null, text: '' }
                        : result);
                    return;
                }
                finishJobWithResult(result.data.result, token);
            })
            .catch(function () {
                if (state.requestToken !== token) { return; }
                renderError({ status: 0, statusText: '', data: null, text: '' });
            });
    }

    function finishJobWithResult(result, token) {
        if (state.requestToken !== token) { return; }
        closeJobStream();
        state.jobId = '';
        if (!result || typeof result !== 'object') {
            renderError({ status: 500, statusText: '', data: null, text: '' });
            return;
        }
        // リクエスト内容は localStorage に残っているので、そこから復元する
        var saved = state.lastRequest || loadLastRequest();
        var form = {
            year: (saved && isFiniteNumber(saved.year)) ? saved.year : state.year,
            month: (saved && isFiniteNumber(saved.month)) ? saved.month : 1,
            day: (saved && isFiniteNumber(saved.day)) ? saved.day : 1,
            mode: (saved && Object.prototype.hasOwnProperty.call(MODE_LABELS, saved.mode))
                ? saved.mode : state.mode,
            targetName: (saved && saved.targetName) ? saved.targetName : ''
        };
        renderSuccessSafely(form, result);
    }

    function finishJobWithFailure(error, token) {
        if (state.requestToken !== token) { return; }
        var info = error && typeof error === 'object' ? error : {};
        var status = isFiniteNumber(info.status) ? info.status : 500;
        closeJobStream();
        state.jobId = '';
        renderError({
            status: status,
            statusText: '',
            data: { detail: info.detail || info.reason || '' },
            text: ''
        });
    }

    function finishJobAsCancelled(token) {
        if (state.requestToken !== token) { return; }
        closeJobStream();
        state.jobId = '';
        afterRequestFinished();
        setStreamTitle('⏹ 受信を中止しました');
        setStreamDesc('別のモードや年を選んで、もう一度お試しください。');
        showEmptyState(true);
        showStateBanner('info', '⏹ 受信を中止しました', 'サーバー側の生成も停止しました。');
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
        // ジョブ API（SSE / ポーリング）経路の終端では afterRequestFinished が
        // 呼ばれないことがあるため、ここで受信状態を必ず解除する。
        // 呼ばないと再生ボタンが「受信中…」のまま残り、タイムアウト
        // タイマーも回収されない。同期経路は .then(afterRequestFinished, ...)
        // で二重に呼ぶが冪等なので無害。
        afterRequestFinished();
        state.lastResult = data;

        var year = isFiniteNumber(data.year) ? data.year : form.year;
        var month = isFiniteNumber(data.month) ? data.month : form.month;
        var day = isFiniteNumber(data.day) ? data.day : form.day;
        var mode = Object.prototype.hasOwnProperty.call(MODE_LABELS, data.mode) ? data.mode : form.mode;

        // 受信完了: ここで初めて進捗を止め、全ステップ完了にして 100% にする。
        // #generationPanel は受信中だけ出す（放送中は「⏹ 受信を中止する」が
        // 生きているボタンのまま残るため、確実に隠す）。
        stopProgress();
        finishProgress();
        hideGenerationPanel();
        hideErrorState();
        showEmptyState(false);

        // 番組を生成した年。ダイヤルの年（anniversary では入力した生年）とは別。
        state.programYear = year;

        // 実測できた成功結果を再試行用に残す
        saveLastRequest(year, month, day, mode, (form && form.targetName) ? form.targetName : '');

        showOutput();

        // 成功時のフォーカス管理（ARIA APG）: プレイヤー領域の見出しへ
        // フォーカスを移す。「再生する」ボタンの次の Tab 位置が
        // プレイヤー操作になるようにする。
        if (dom.playerCard) {
            var heading = dom.playerCard.querySelector('h2, h3') || dom.playerCard;
            try {
                heading.setAttribute('tabindex', '-1');
                heading.focus({ preventScroll: false });
            } catch (e) {
                heading.focus();
            }
        }

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
        var detail = extractDetail(payload);

        var message;
        if (info && typeof info.message === 'string' && info.message) {
            // 描画失敗など、HTTP 起因ではない内部エラー
            message = info.message;
        } else if (status === 408) {
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
        // ストリームはここで必ず閉じる（EventSource が余ると次の受信で開く）
        closeJobStream();
        state.jobId = '';
        // ジョブ API（SSE / ポーリング）経路の終端はここが唯一の後始末になる
        // （同期経路は .then(afterRequestFinished, ...) で二重に呼ぶが冪等）。
        // 呼ばないと再生ボタンが「受信中…」のまま残る。
        afterRequestFinished();

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
        showStoreLink('');
        if (dom.vinylDisk) { dom.vinylDisk.classList.remove('spinning'); }
        setVuLevel(0);
        setTubeLit(false);

        // 画面には 1 行の要約を出し、技術的な詳細は折り畳み領域へ出す。
        // 422 は「何が起きたか」= detail なので、要約にも必ず載せる
        var summary = firstLine(message);
        if (status === 422 && detail) {
            summary = summary + ' 詳細: ' + detail.replace(/\s+/g, ' ');
        }
        showErrorState(
            status === 408 ? '⏱ 応答がタイムアウトしました' : '放送できません',
            summary,
            rawText || ('HTTP ' + status + (statusText ? ' ' + statusText : '') +
                (detail ? '\n' + detail : ''))
        );

        setStreamTitle(status === 408 ? '⏱ タイムアウトしました' : '⚠️ 放送できません（HTTP ' + status + '）');
        setStreamDesc(summary);
        announce(summary);
        // 401 は「ログインすれば解決する」ので、ログイン枠を出す
        if (status === 401) { showAuthPanel(true); }
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

    /* =====================================================================
       受信待ちの充填音（待機 bed）とステップ効果音
       ---------------------------------------------------------------------
       ボタンを押してから番組が鳴るまで 30〜60 秒、その間は何も鳴らない。
       ここでは Web Audio のノードだけ（ノイズ + ハム + LFO）を組み立てて
       「まだ受信中のラジオ」をじわりと鳴らし、ステップ遷移で効果音を
       1 回だけ鳴らす。

       解除の副作用に注意:
         - MediaElementSource / AnalyserNode は **使わない**。キューが別オリジン
           （iTunes プレビュー）を含むと恒久無音化する既知の罠があるため、
           待機音は <audio> の再生経路と完全に切り離す。
         - AudioContext が作れなかったら unsupported として以後試さない。
         - 番組が始まったら必ず stopStandbyTone() で消す（鳴りっぱなしにしない）。
    ===================================================================== */

    // 待機音の音量。ミュート / 音量スライダーに追従させる。
    function standbyLevel() {
        if (state.muted) { return 0; }
        return clampVolume(state.volume) * STANDBY_MASTER_GAIN;
    }

    // 2 秒分のホワイトノイズを 1 度だけ作って使い回す
    function standbyNoiseBuffer(ctx) {
        if (state.standbyNoise && state.standbyNoiseRate === ctx.sampleRate) {
            return state.standbyNoise;
        }
        var frames = Math.floor(ctx.sampleRate * STANDBY_NOISE_SECONDS);
        var buffer = ctx.createBuffer(1, frames, ctx.sampleRate);
        var data = buffer.getChannelData(0);
        for (var i = 0; i < frames; i += 1) {
            // 単純な乱数（-1〜1）。揺らぎは LFO 側で足す。
            data[i] = Math.random() * 2 - 1;
        }
        state.standbyNoise = buffer;
        state.standbyNoiseRate = ctx.sampleRate;
        return buffer;
    }

    // AudioContext を「ユーザージェスチャ内」で用意する。
    // ensureAnalyser() は fetch 完了後にしか到達しないため、
    // 生成と resume は再生ボタンの click ハンドラから行う。
    function ensureAudioContext() {
        if (state.audioCtx) { return state.audioCtx; }
        var Ctor = window.AudioContext || window.webkitAudioContext;
        if (typeof Ctor !== 'function') { return null; }
        try {
            state.audioCtx = new Ctor();
        } catch (e) {
            state.audioCtx = null;
            return null;
        }
        return state.audioCtx;
    }

    function startStandbyTone() {
        if (state.standbyUnsupported) { return; }
        if (state.standbyActive) { return; }
        var ctx = ensureAudioContext();
        if (!ctx) {
            // 一度取れなかったら再試行しない（毎回例外を投げるのを避ける）
            state.standbyUnsupported = true;
            return;
        }
        resumeAudioContext();
        try {
            var now = ctx.currentTime;
            var master = ctx.createGain();
            master.gain.setValueAtTime(0.0001, now);
            master.gain.linearRampToValueAtTime(standbyLevel(), now + STANDBY_FADE_IN_SECONDS);
            master.connect(ctx.destination);

            // テープヒス（ノイズ）＋ ゲインを揺らす LFO（受信感の揺れ）
            var hiss = ctx.createGain();
            hiss.gain.value = STANDBY_HISS_GAIN;
            var noise = ctx.createBufferSource();
            noise.buffer = standbyNoiseBuffer(ctx);
            noise.loop = true;
            noise.connect(hiss);
            hiss.connect(master);

            var lfo = ctx.createOscillator();
            lfo.type = 'sine';
            lfo.frequency.value = 0.7;
            var lfoGain = ctx.createGain();
            lfoGain.gain.value = STANDBY_HISS_GAIN * 0.4;
            lfo.connect(lfoGain);
            lfoGain.connect(hiss.gain);

            // 電源ハム（60Hz）
            var hum = ctx.createOscillator();
            hum.type = 'sine';
            hum.frequency.value = STANDBY_HUM_FREQ;
            var humGain = ctx.createGain();
            humGain.gain.value = STANDBY_HUM_GAIN;
            hum.connect(humGain);
            humGain.connect(master);

            noise.start(now);
            lfo.start(now);
            hum.start(now);

            state.standbyNodes = { master: master, sources: [noise, lfo, hum] };
            state.standbyActive = true;
        } catch (e) {
            state.standbyActive = false;
            state.standbyNodes = null;
            state.standbyUnsupported = true;
        }
    }

    // 待機音（受信 bed）の音量を現在の音量設定に追従させる
    function applyStandbyVolume() {
        var nodes = state.standbyNodes;
        var ctx = state.audioCtx;
        if (!nodes || !ctx) { return; }
        try {
            nodes.master.gain.setValueAtTime(standbyLevel(), ctx.currentTime);
        } catch (e) { /* noop */ }
    }

    function stopStandbyTone() {
        var nodes = state.standbyNodes;
        state.standbyActive = false;
        state.standbyCuedStep = '';
        if (!nodes) { return; }
        state.standbyNodes = null;
        var ctx = state.audioCtx;
        if (!ctx) { return; }
        var now = 0;
        try {
            now = ctx.currentTime || 0;
            nodes.master.gain.cancelScheduledValues(now);
            nodes.master.gain.setValueAtTime(nodes.master.gain.value, now);
            nodes.master.gain.linearRampToValueAtTime(0.0001, now + STANDBY_FADE_OUT_SECONDS);
        } catch (e) { /* noop */ }
        var stopAt = now + STANDBY_FADE_OUT_SECONDS + 0.05;
        nodes.sources.forEach(function (source) {
            try { source.stop(stopAt); } catch (e) { /* noop */ }
            try { source.disconnect(); } catch (e) { /* noop */ }
        });
        // master はフェードが終わるまで繋いだままにする（ここで切るとフェードが無駄）
        window.setTimeout(function () {
            try { nodes.master.disconnect(); } catch (e) { /* noop */ }
        }, (STANDBY_FADE_OUT_SECONDS + 0.1) * 1000);
    }

    // 進捗ステップが「進行中」になった瞬間だけ効果音を 1 回鳴らす。
    // 同じステップを何度 setProgressStep されても 1 回しか鳴らない。
    function playStandbyStepCue(stepKey) {
        if (!state.standbyActive || !state.standbyNodes) { return; }
        if (state.standbyCuedStep === stepKey) { return; }
        var cue = STANDBY_STEP_CUES[stepKey];
        if (!cue) { return; }
        state.standbyCuedStep = stepKey;
        var ctx = state.audioCtx;
        if (!ctx) { return; }
        try {
            var now = ctx.currentTime;
            var osc = ctx.createOscillator();
            osc.type = cue.type;
            osc.frequency.value = cue.freq;
            var env = ctx.createGain();
            env.gain.setValueAtTime(0.0001, now);
            env.gain.linearRampToValueAtTime(cue.gain, now + 0.012);
            env.gain.linearRampToValueAtTime(0.0001, now + cue.dur);
            osc.connect(env);
            env.connect(state.standbyNodes.master);
            osc.start(now);
            osc.stop(now + cue.dur + 0.02);
            osc.onended = function () {
                try { osc.disconnect(); env.disconnect(); } catch (e) { /* noop */ }
            };
        } catch (e) { /* noop */ }
    }

    // 進捗ステップ 1 件の表示を切り替える（'pending'|'active'|'completed'|'failed'）
    function setProgressStep(stepKey, status) {
        var next = status ? status : 'pending';
        // 進行中になった瞬間だけ効果音を 1 回鳴らす（進捗タイマーが 250ms ごとに
        // 呼んでも、同一ステップでは鳴り直さない）。
        if (next === 'active') { playStandbyStepCue(stepKey); }
        if (!dom.progressSteps) { return; }
        var steps = dom.progressSteps.querySelectorAll('.progress-step');
        for (var i = 0; i < steps.length; i += 1) {
            var step = steps[i];
            if (!step) { continue; }
            if (step.getAttribute('data-step') === stepKey) {
                step.setAttribute('data-status', next);
                // 進行中ステップに aria-current を付け、完了したら外す
                if (next === 'active') {
                    step.setAttribute('aria-current', 'step');
                } else {
                    step.removeAttribute('aria-current');
                }
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
        state.progressDriftAnnounced = false;

        if (dom.generationPanel) { dom.generationPanel.hidden = false; }
        if (dom.generationPanelTitle) {
            dom.generationPanelTitle.textContent =
                '📡 ' + (isFiniteNumber(year) ? year : state.year) + '年の電波を受信中…（' + modeLabel(mode) + '）';
        }
        if (dom.generationElapsed) { dom.generationElapsed.textContent = '0秒'; }
        if (dom.generationRemaining) { dom.generationRemaining.textContent = remainingLabel(); }
        if (dom.generationDriftNote) { dom.generationDriftNote.hidden = true; }
        if (dom.progressFill) { dom.progressFill.style.width = '0%'; }
        if (dom.progressTrack) { dom.progressTrack.setAttribute('aria-valuenow', '0'); }
        // 待ち時間を無音にしないため、受信_noise を鳴らし始める
        startStandbyTone();
        syncProgressSteps(PROGRESS_STEPS[0]);

        // 受信し始めるので、直前の空状態とエラー状態は片付ける
        hideErrorState();
        showEmptyState(false);

        tickProgress();
        state.progressTimer = window.setInterval(tickProgress, PROGRESS_TICK_MS);
    }

    // estimated_ms から残り時間の表示を作る（推定が無ければプレースホルダ）。
    // 提案④: 「完了まで約 1 分」は estimated_ms のまま残り時間として表示する。
    function remainingLabel() {
        var estimate = Number(state.progressEstimateMs);
        if (!isFiniteNumber(estimate) || estimate <= 0) { return 'のこり約 —'; }
        var remaining = estimate - (nowMs() - state.progressStartedAt);
        return 'のこり約 ' + formatElapsed(remaining);
    }

    // 実測の経過が推定 + 5 秒を超えたら 1 行だけ出す（繰り返さない）。
    function checkEstimateDrift(elapsed) {
        if (state.progressDriftAnnounced) { return; }
        var estimate = Number(state.progressEstimateMs);
        if (!isFiniteNumber(estimate) || estimate <= 0) { return; }
        if (elapsed > estimate + 5000) {
            state.progressDriftAnnounced = true;
            if (dom.generationDriftNote) { dom.generationDriftNote.hidden = false; }
        }
    }

    // 経過時間と残り時間を更新する（実測値ではない）。
    // ただしジョブ（SSE / ポーリング）から実イベントが来ている間は、
    // 推測タイムラインで実測を上書きしない（経過時間だけ更新する）。
    function tickProgress() {
        var elapsed = nowMs() - state.progressStartedAt;

        if (dom.generationElapsed) { dom.generationElapsed.textContent = formatElapsed(elapsed); }
        if (dom.generationRemaining) { dom.generationRemaining.textContent = remainingLabel(); }
        checkEstimateDrift(elapsed);

        if (state.jobEventsSeen) { return; }

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
        syncProgressSteps(stepKey);
    }

    // 待機音をここで止める。完了 / 失敗 / 中止のどの終端でも必ず通る。
    function stopProgress() {
        stopStandbyTone();
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

    /* =====================================================================
       3ステップ導線（ヘッダーの .flow-steps）
       ---------------------------------------------------------------------
       「年を選ぶ → モードを選ぶ → 再生する」を画面上で常に見せる。
       setYear / setMode / startGeneration からしか呼ばないので、
       操作していない段階だけなら current が残る。
       ===================================================================== */
    var FLOW_TOTAL_STEPS = 3;

    // doneCount は「ここまで進んだステップ数」。後退させないので max で保持する。
    function setFlowStep(doneCount) {
        var done = Math.max(0, Math.min(FLOW_TOTAL_STEPS, Number(doneCount) || 0));
        if (done <= (state.flowStep || 0)) { return; }
        state.flowStep = done;
        if (!dom.flowSteps) { return; }
        var steps = dom.flowSteps.querySelectorAll('.flow-step');
        for (var i = 0; i < steps.length; i++) {
            var index = i + 1;
            steps[i].classList.toggle('is-done', index <= done);
            steps[i].classList.toggle('is-current', index === done + 1);
        }
    }

    // 年を選んだ（= ステップ1完了）
    function markFlowYearChosen() { setFlowStep(1); }

    // モードを選んだ（= ステップ2完了）
    function markFlowModeChosen() { setFlowStep(2); }

    // 再生を始めた（= ステップ3まで完了）
    function markFlowPlaying() { setFlowStep(FLOW_TOTAL_STEPS); }

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
        // フォーカス管理（WCAG 2.2 / ARIA APG）: role="alert" 領域の見出しへ
        // フォーカスを移す。alert の読み上げに加えて、次の Tab 位置が
        // 「もう一度試す」ボタンになるようにする。
        if (dom.errorStateTitle) {
            try {
                dom.errorStateTitle.setAttribute('tabindex', '-1');
                dom.errorStateTitle.focus({ preventScroll: false });
            } catch (e) {
                dom.errorStateTitle.focus();
            }
        }
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
       同意と開示・削除（提案⑧-3 / ⑧-4）
       ---------------------------------------------------------------------
       - 同意: GET /api/terms で規約を取り、POST /api/me/consent で記録する。
         個人データを取り込む前に、対象を明示する。
       - 開示: GET /api/me/export（format=json|csv）。CSV はダウンロード、
         JSON は同じ画面に要約を表示する。
       - 削除: DELETE /api/me。確認なしでは実行しない（window.confirm を 1 度挟む）。
       ===================================================================== */
    function setConsentStatus(text) {
        if (dom.consentStatus) { dom.consentStatus.textContent = text; }
    }

    function setPrivacyStatus(text) {
        if (dom.privacyStatus) { dom.privacyStatus.textContent = text; }
    }

    function openConsent() {
        var dialog = dom.consentDialog;
        if (!dialog) { return; }
        try {
            if (typeof dialog.showModal === 'function') {
                dialog.showModal();
            } else {
                dialog.setAttribute('open', 'open');
            }
        } catch (e) {
            // すでに開いている等は黙って続行する
        }
        // 規約の本文は GET /api/terms から取る（失敗しても閉じられる）
        fetchJson('/api/terms', { method: 'GET' }).then(function (res) {
            var data = res.data || {};
            if (dom.consentTermsBody) {
                dom.consentTermsBody.textContent =
                    (data.title ? data.title + '\n\n' : '') + (data.body || '');
            }
        }).catch(function () { /* noop */ });
    }

    /* 同意ゲート（P1-2）: declined=true のあいだ生成ボタンを無効化する。
       生成自体は同意不要の操作だが、拒否の意思表示を尊重して
       新規受信は止める。同意したら解除する。

       **有効化と無効化を対称にする。** 以前の
       `button.disabled = blocked || button.disabled` は、blocked=false のとき
       `button.disabled` を自分自身へ代入するだけで解除できなかった。
       拒否 → 同意の順に操作するとボタンが disabled=true のまま残り、
       ページをリロードするまで操作できない「死んだコントロール」になっていた。
       しかも aria-disabled だけが外れていたため、支援技術は
       無効なボタンを「有効」と読み上げていた。

       **状態の正本は一箇所に置く。** :func:`setLoading` が
       :func:`consentBlocksInput` を参照するため、同意を切り替えるには
       :func:`applyConsentGate` を呼ぶだけで済み、「どちらのフラグが
       勝つか」で分岐する箇所がなくなる。
    */
    var CONSENT_BLOCK_TITLE =
        '利用規約に同意しないことが記録されているため、新しい受信はできません。';

    function consentBlocksInput() {
        return state.consentDeclined === true;
    }

    function consentButtons() {
        return [dom.btnPlayRadio, dom.btnEmptyPlay, dom.btnRetry];
    }

    function applyConsentGate(declined) {
        state.consentDeclined = !!declined;
        consentButtons().forEach(function (button) {
            if (!button) { return; }
            if (declined) {
                button.disabled = true;
                button.setAttribute('aria-disabled', 'true');
                button.title = CONSENT_BLOCK_TITLE;
            } else {
                button.disabled = false;
                button.removeAttribute('aria-disabled');
                button.removeAttribute('title');
            }
        });
        /* btnPlayRadio の disabled は setLoading が管理しているため、
           現在の受信状態を渡して導出し直す。 */
        setLoading(state.isLoading);
        syncAudioButton();
    }

    /* 起動時の同意確認（P1-2）。required && !consented なら同意モーダルを開く。
       個人モード（required=false）では何もしない。

       **HTTP ステータスを必ず見る。** `fetchJson` は非 2xx でも reject せず
       `{ok, status, data}` を返すため、そのまま `.then()` に落ちると
       401（資格情報なし）や 503（fail-closed）で `data.required` が undefined に
       なり、モーダルも状態表示も出ないまま**黙って成功したように見える**。
       `require_consent` が意味を持つのは認証有効な運用だけなので、
       認証エラーでは同意ゲートに到達できない状態が通常だった。
    */
    function checkConsentOnStartup() {
        fetchJson('/api/me/consent', { method: 'GET', credentials: 'same-origin' })
            .then(function (res) {
                if (!res.ok) {
                    /* 認証系は「同意が要らない」のではなく「確認できなかった」。
                       黙って通過させず、理由が見えるようにする。 */
                    if (res.status === 401 || res.status === 403) {
                        setConsentStatus('同意状態を確認できません（認証が必要です）。');
                    } else if (res.status === 503) {
                        setConsentStatus('同意状態を確認できません（サーバーの認証設定が未完了です）。');
                    } else {
                        setConsentStatus('同意状態を確認できませんでした（' + res.status + '）。');
                    }
                    return;
                }
                var data = res.data || {};
                state.consentRequired = !!data.required;
                state.consentAccepted = !!data.consented;
                if (data.required && !data.consented) {
                    openConsent();
                    setConsentStatus('現行の利用規約への同意が未記録です。ご確認ください。');
                } else if (data.required && data.consented) {
                    setConsentStatus('同意済み（' + (data.terms_version || '') + '）');
                }
            })
            .catch(function () {
                /* ネットワーク断など。API が無い状態は個人構成なので黙って続行。 */
            });
    }

    function closeConsent() {
        var dialog = dom.consentDialog;
        if (!dialog) { return; }
        try {
            if (typeof dialog.close === 'function' && dialog.open) {
                dialog.close();
                return;
            }
        } catch (e) { /* noop */ }
        dialog.removeAttribute('open');
    }

    function recordConsent(accepted) {
        fetchJson('/api/me/consent', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ accepted: accepted })
        }).then(function (res) {
            /* **非 2xx を成功扱いにしない。** 以前は 401 でも
               「同意を記録しました」と表示していた（`fetchJson` は reject しないため）。
               記録できていないのに成功を伝えてはいけない。 */
            if (!res.ok) {
                state.consentRecorded = false;
                if (res.status === 401 || res.status === 403) {
                    setConsentStatus('同意を記録できませんでした（認証が必要です）。');
                } else if (res.status === 503) {
                    setConsentStatus('同意を記録できませんでした（サーバーの認証設定が未完了です）。');
                } else {
                    setConsentStatus('同意の記録に失敗しました（' + res.status + '）。');
                }
                announce('同意を記録できませんでした。');
                return;
            }
            var data = res.data || {};
            state.consentRecorded = accepted;
            state.consentAccepted = accepted;
            setConsentStatus(accepted
                ? '同意を記録しました（' + (data.terms_version || '') + '）'
                : '同意しないことを記録しました。生成の記録は残りません。');
            announce(accepted ? '利用規約に同意しました。' : '同意を記録しませんでした。');
            // P1-2: 同意したら生成ゲートを解除、拒否したら有効化する
            applyConsentGate(!accepted);
            if (accepted) { closeConsent(); }
        }).catch(function () {
            setConsentStatus('同意の記録に失敗しました。通信状況をご確認ください。');
        });
    }

    function exportData(format) {
        var url = '/api/me/export?format=' + encodeURIComponent(format);
        if (format === 'csv') {
            // CSV はそのままダウンロードさせる（同一オリジンなので a タグで足りる）
            var a = document.createElement('a');
            a.href = url;
            a.download = 'retro_radio_my_data.csv';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setPrivacyStatus('CSV のダウンロードを開始しました。');
            return;
        }
        fetchJson(url, { method: 'GET' }).then(function (res) {
            var data = res.data || {};
            var summary = 'データ取得（JSON）: 生成 ' + (data.generation_count || 0) + ' 件 / ' +
                '選曲 ' + (data.favorite_count || 0) + ' 件';
            if (dom.privacyActions && typeof data.user_id === 'string') {
                // 要約は 1 回だけ書く（連打で積み上がらない）
                setPrivacyStatus(summary);
            }
        }).catch(function () {
            setPrivacyStatus('データの取得に失敗しました。個人モードでは開示できません。');
        });
    }

    function deleteMyData() {
        // 削除は確認なしには実行しない（1 度だけ confirm を挟む）
        if (!window.confirm('自分のデータを削除します。よろしいですか？（この操作は取り消せません）')) {
            return;
        }
        window.fetch('/api/me', { method: 'DELETE', credentials: 'same-origin' })
            .then(function (response) {
                if (!response.ok) { throw new Error('delete failed: ' + response.status); }
                return response.json();
            })
            .then(function (data) {
                setPrivacyStatus('データを削除しました（' +
                    (data.sla_hours !== undefined ? 'SLA ' + data.sla_hours + ' 時間' : '') + '）');
            })
            .catch(function () {
                setPrivacyStatus('削除に失敗しました。個人モードでは削除できません。');
            });
    }

    function bindPrivacy() {
        if (dom.btnConsentAccept) {
            dom.btnConsentAccept.addEventListener('click', function () { recordConsent(true); });
        }
        if (dom.btnConsentDecline) {
            dom.btnConsentDecline.addEventListener('click', function () { recordConsent(false); });
        }
        if (dom.btnConsentClose) {
            dom.btnConsentClose.addEventListener('click', closeConsent);
        }
        if (dom.consentDialog) {
            dom.consentDialog.addEventListener('close', function () {
                announce('同意ダイアログを閉じました。');
            });
        }
        if (dom.btnExportDataJson) {
            dom.btnExportDataJson.addEventListener('click', function () { exportData('json'); });
        }
        if (dom.btnExportDataCsv) {
            dom.btnExportDataCsv.addEventListener('click', function () { exportData('csv'); });
        }
        if (dom.btnDeleteData) {
            dom.btnDeleteData.addEventListener('click', deleteMyData);
        }
        if (dom.btnAuditLaunch) {
            dom.btnAuditLaunch.addEventListener('click', openAudit);
        }
        if (dom.btnAuditRefresh) {
            dom.btnAuditRefresh.addEventListener('click', loadAudit);
        }
        if (dom.btnAuditClose) {
            dom.btnAuditClose.addEventListener('click', closeAudit);
        }
        if (dom.auditDialog) {
            dom.auditDialog.addEventListener('close', function () {
                announce('監査ログを閉じました。');
            });
        }
    }

    /* =====================================================================
       管理者: 監査ログ（提案⑧）
       ---------------------------------------------------------------------
       「誰がいつ、誰の記念日を生成したか」を追えることが福祉導入の前提条件。
       `/api/admin/audit` は **admin ロール限定**なので、導線も
       `GET /api/me` の `role === "admin"` のときだけ出す。利用者が
       存在しない個人モード（require_auth=0）では `GET /api/me` が 401 に
       なるので、導線は出ないままになる（= 管理画面が無い運用）。

       **テナントをまたいだ検索はしない。** `scope=global` は API 側に無い。
       ===================================================================== */
    var AUDIT_LIST_LIMIT = 50;

    function setAuditStatus(text) {
        if (dom.auditStatus) { dom.auditStatus.textContent = text; }
    }

    function showAdminActions(isAdmin) {
        if (dom.adminActions) {
            dom.adminActions.hidden = !isAdmin;
        }
    }

    function renderAuditStats(stats) {
        if (!dom.auditStats) { return; }
        dom.auditStats.textContent = '';
        var rows = [
            ['テナント', stats.tenant_id || '-'],
            ['総件数', String(stats.total !== undefined ? stats.total : 0)],
            ['生成 / 再生', String(stats.generation_events || 0) + ' / ' + String(stats.playback_events || 0)],
            ['同意 / エクスポート', String(stats.consent_events || 0) + ' / ' + String(stats.by_action && stats.by_action.export || 0)],
            ['削除請求（未完了）', String(stats.pending_deletions || 0)],
            ['監査完全率', Math.round((stats.coverage_ratio || 0) * 1000) / 10 + '%']
        ];
        rows.forEach(function (row) {
            var dt = document.createElement('dt');
            dt.textContent = row[0];
            var dd = document.createElement('dd');
            dd.textContent = row[1];
            dom.auditStats.appendChild(dt);
            dom.auditStats.appendChild(dd);
        });
    }

    function renderAuditEntries(entries) {
        if (!dom.auditEntries) { return; }
        dom.auditEntries.textContent = '';
        (entries || []).forEach(function (entry) {
            var tr = document.createElement('tr');
            [entry.created_at || '-', entry.action || '-', entry.user_id || '-', entry.outcome || '-']
                .forEach(function (value) {
                    var td = document.createElement('td');
                    // textContent のみ。meta を innerHTML に入れない
                    td.textContent = String(value);
                    tr.appendChild(td);
                });
            dom.auditEntries.appendChild(tr);
        });
    }

    function openAudit() {
        var dialog = dom.auditDialog;
        if (dialog) {
            try {
                if (typeof dialog.showModal === 'function') {
                    dialog.showModal();
                } else {
                    dialog.setAttribute('open', 'open');
                }
            } catch (e) { /* すでに開いている */ }
        }
        loadAudit();
    }

    function closeAudit() {
        var dialog = dom.auditDialog;
        if (!dialog) { return; }
        try {
            if (typeof dialog.close === 'function' && dialog.open) {
                dialog.close();
                return;
            }
        } catch (e) { /* noop */ }
        dialog.removeAttribute('open');
    }

    function loadAudit() {
        setAuditStatus('読み込んでいます…');
        fetchJson('/api/admin/audit/stats?limit=' + encodeURIComponent(AUDIT_LIST_LIMIT), {
            method: 'GET',
            credentials: 'same-origin'
        }).then(function (statsRes) {
            if (!statsRes.ok) {
                if (statsRes.status === 401 || statsRes.status === 403) {
                    setAuditStatus('監査ログを表示できる権限がありません。');
                } else {
                    setAuditStatus('監査サマリを読み込めませんでした（' + statsRes.status + '）。');
                }
                return null;
            }
            renderAuditStats(statsRes.data || {});
            return fetchJson('/api/admin/audit?limit=' + encodeURIComponent(AUDIT_LIST_LIMIT), {
                method: 'GET',
                credentials: 'same-origin'
            });
        }).then(function (listRes) {
            if (!listRes) { return; }
            if (!listRes.ok) {
                setAuditStatus('監査ログを読み込めませんでした（' + listRes.status + '）。');
                return;
            }
            renderAuditEntries((listRes.data || {}).entries || []);
            setAuditStatus('この施設の監査ログ ' + (listRes.data || {}).count + ' 件を表示しています。');
        }).catch(function () {
            setAuditStatus('監査ログの読み込みに失敗しました。通信状況をご確認ください。');
        });
    }

    /* 起動時に自分のロールを確認する（admin のときだけ導線を出す）。 */
    function checkAdminRole() {
        fetchJson('/api/me', { method: 'GET', credentials: 'same-origin' })
            .then(function (res) {
                if (!res.ok) {
                    /* 個人モード（require_auth=0）では 401 になる。
                       管理画面が無い運用なので、導線は出さない。 */
                    showAdminActions(false);
                    return;
                }
                var role = (res.data || {}).role;
                showAdminActions(role === 'admin');
            })
            .catch(function () { showAdminActions(false); });
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
        // 長さごとに別々の無音 URL を返す。1 個だけキャッシュすると
        // 1.6 秒と 2.4 秒のどちらが先に要求されたかで出力が変わってしまう。
        var key = String(Math.max(1, Math.round(Number(seconds) * 1000) / 1000));
        if (state.silenceUrls && state.silenceUrls[key]) { return state.silenceUrls[key]; }
        if (!state.silenceUrls) { state.silenceUrls = {}; }
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

            state.silenceUrls[key] = URL.createObjectURL(new Blob([buffer], { type: 'audio/wav' }));
            state.silenceUrl = state.silenceUrls[key];
        } catch (e) {
            // Blob / DataView が使えない環境では間奏 الصوتを作る（0.01 秒）
            state.silenceUrls[key] =
                'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA=';
        }
        return state.silenceUrls[key];
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
                    // ここに落ちるのは「このスロットに音源が無い」場合だけ。
                    //
                    // **曲名を表示してはいけない。** raw.title は
                    // サーバーが選曲枠を埋めるためにカタログから借りた
                    // 曲名であり、この番組の原稿では一度も紹介されない。
                    // そのまま曲として表示すると、司会が紹介していない
                    // 曲が番組表に載る（= 利用者から見て嘘になる）。
                    // 借りた曲名はサーバ側の metadata.borrowed_song に
                    // 内側だけ残してある。
                    pass.push({
                        kind: INTERMISSION,
                        url: getSilenceUrl(SILENCE_SLOT_SECONDS),
                        title: INTERMISSION_TITLE,
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
                    artist: String(song.artist || ''),
                    // Apple への送客導線。サーバーが `null` を渡したら非表示。
                    storeUrl: safeStoreUrl(song.store_url)
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
        // 操作用 <audio> はスクリーンリーダーの読み上げ対象から外す
        // （UI 側のボタン / スライダーが正の操作手段）。
        audio.setAttribute('aria-hidden', 'true');
        audio.setAttribute('tabindex', '-1');
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
        applyPitchPreservation(audio);
        applyPlaybackRate(audio);
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
        applyStandbyVolume();
        var slots = state.slots;
        if (!slots) { return; }
        [slots.a, slots.b].forEach(function (audio) {
            if (!audio) { return; }
            try {
                audio.volume = state.muted ? 0 : clampVolume(state.volume);
                audio.muted = state.muted;
                applyPlaybackRate(audio);
            } catch (e) { /* noop */ }
        });
    }

    /* =====================================================================
       聞き取り支援（提案③-2）: 話速の 3 段階切替
       ===================================================================== */
    var PLAYBACK_RATES = [0.9, 1, 1.15];

    function clampRate(value) {
        var num = Number(value);
        if (!isFiniteNumber(num)) { return 1; }
        for (var i = 0; i < PLAYBACK_RATES.length; i += 1) {
            if (Math.abs(PLAYBACK_RATES[i] - num) < 0.001) { return PLAYBACK_RATES[i]; }
        }
        return 1;
    }

    /* 話速を変更しても、声の高さは元のまま保つ。
       これを設定しないと 0.9 倍速のときに "~1.2 半音 下がった" ように
       聞こえ、「機械的だ」と感じる一番の要因になる（特に低音域）。
       webkitPreservesPitch は Safari / 旧 Chromium 用の別名。 */
    function applyPitchPreservation(audio) {
        if (!audio) { return; }
        try {
            audio.preservesPitch = true;
            audio.mozPreservesPitch = true;
            audio.webkitPreservesPitch = true;
        } catch (e) { /* noop */ }
    }

    // 1 本の <audio> へ現在の話速を適用する（ピッチは保持する）
    function applyPlaybackRate(audio) {
        if (!audio) { return; }
        try {
            applyPitchPreservation(audio);
            audio.playbackRate = clampRate(state.playbackRate);
            // 話速を変えた直後にMoz / WebKit 系だけ補完が効かないことがあるため、
            // 設定後に読み直して確実に再適用させる。
            applyPitchPreservation(audio);
        } catch (e) { /* noop */ }
    }

    function setPlaybackRate(value, options) {
        var opts = options || {};
        var rate = clampRate(value);
        state.playbackRate = rate;
        writeStore('retro_radio_playback_rate', String(rate));
        var slots = state.slots;
        if (slots) {
            [slots.a, slots.b].forEach(applyPlaybackRate);
        }
        syncPlaybackSpeedButtons();
        if (!opts.silent) {
            announce('読み上げの速さを ' + (rate === 1 ? 'ふつう' : (rate < 1 ? 'ゆっくり' : 'はやい')) + 'にしました。');
        }
    }

    function syncPlaybackSpeedButtons() {
        if (!dom.playbackSpeedRow) { return; }
        var buttons = dom.playbackSpeedRow.querySelectorAll('.playback-speed-btn');
        for (var i = 0; i < buttons.length; i += 1) {
            var btn = buttons[i];
            if (!btn) { continue; }
            var rate = clampRate(btn.getAttribute('data-rate'));
            var active = Math.abs(rate - clampRate(state.playbackRate)) < 0.001;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
        }
    }

    function bindPlaybackSpeedControls() {
        if (!dom.playbackSpeedRow) { return; }
        var buttons = dom.playbackSpeedRow.querySelectorAll('.playback-speed-btn');
        for (var i = 0; i < buttons.length; i += 1) {
            if (!buttons[i]) { continue; }
            buttons[i].addEventListener('click', function (event) {
                var btn = event && event.currentTarget ? event.currentTarget : null;
                if (!btn) { return; }
                setPlaybackRate(btn.getAttribute('data-rate'), { silent: false });
            });
        }
        var stored = Number(readStore('retro_radio_playback_rate', '1'));
        state.playbackRate = clampRate(stored);
        syncPlaybackSpeedButtons();
    }

    function startPlayback(data) {
        // 待機音との二重再生を防ぐ（番組が始まったら待機 bed は止める）
        stopStandbyTone();
        stopPlayback();
        cancelXfade();

        // バックエンドが推奨する周回数を初期値にする。
        // 利用者がこのセッションで周回数を変えていたら上書きしない
        // （上書きすると「変えたのに効かない」= 自己効力感破壊になる）。
        if (data && isFiniteNumber(data.loop_count) && !state.repeatCountUserSet) {
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
        // トラックが進んだときだけ streak を戻す（onAudioPlay では戻さない）
        if (state.lastErrorIndex !== index) {
            state.errorStreak = 0;
            state.lastErrorIndex = -1;
        }
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
            // ユーザー操作による一時停止ではないのに paused なら放置しない。
            // ただし**バッファリング中**（play() の promise が未解決）も paused に
            // なるため、readyState を見る。判別しないと回線が遅い環境で
            // 1 秒ごとにトラックを連続スキップし、キューを使い切る（実測）。
            if (!state.userPaused && !audio.ended) {
                var rs = typeof audio.readyState === 'number' ? audio.readyState : 0;
                var idleMs = Date.now() - state.lastProgressAt;
                // HAVE_FUTURE_DATA 未満 = まだ読み込み中。失敗は onerror が
                // 拾うため、stall 相当の時間（WATCHDOG_STALL_MS）までは
                // 猶予する。読み込み中でも stall 時間を超えたら次へ進める
                // （無音の溝より進むほうが良い）。
                if (rs >= 3 || idleMs >= WATCHDOG_STALL_MS) {
                    forceAdvance('再生が停止しました');
                }
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

        // トラック切替をスクリーンリーダーに 1 回だけ知らせる（WCAG 4.1.3）。
        // 曲名が確定したこのタイミングでのみ announce する。
        if (state.playedIndex !== index) {
            state.playedIndex = index;
            announce(text);
        }

        if (track.kind === SONG) {
            if (dom.songTitle) { dom.songTitle.textContent = track.title || '—'; }
            if (dom.songArtist) { dom.songArtist.textContent = track.artist || '—'; }
            if (dom.vinylDisk) { dom.vinylDisk.classList.add('now-playing'); }
            showStoreLink(track.storeUrl);
        } else if (dom.vinylDisk) {
            dom.vinylDisk.classList.remove('now-playing');
            showStoreLink('');
        }

        // 読み上げ中なら原稿の該当セグメントを起こす
        highlightManuscript(track);

        // プレイヤー領域のトラック表示・ボタン状態を同期する
        // （再生中のトラック情報が画面に一切出ない問題の修正点）
        renderPlayerUI();
    }

    // 再生中トラックの上部ステータス表示（#streamStatusTitle / #streamStatusDesc）を更新する
    function applyStreamMeta(track, index) {
        // ヘッダーは**生成した年**を出す。ダイヤルの年（state.year）は
        // anniversary モードでは入力した生年とずれるため使わない。
        var year = isFiniteNumber(state.programYear) && state.programYear > 0
            ? state.programYear
            : state.year;
        setStreamTitle('📻 ラジオ放送中（' + year + '年）— ' + kindLabel(track) +
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
            // 要素をトラック遷移ごとに new すると参照を保持も破棄もしないため
            // （周回 5 × 20 トラックで 100 個残る）、1 本を取り回す。
            if (!state.preloader) {
                state.preloader = new window.Audio();
                state.preloader.preload = 'auto';
            }
            var preloader = state.preloader;
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
        state.lastErrorIndex = state.index;
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
        // 壊れる予定の URL に対しても play() は呼ばれるため、
        // error の後に同じトラックの play が来ても streak を戻さない
        // （戻すと無限スキップの可能性がある）。トラックが進んだときだけ戻す。
        if (state.index !== state.lastErrorIndex) {
            state.errorStreak = 0;
        }
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
        // 利用者の選択。以降の startPlayback がサーバ既定で上書きしないようにする
        state.repeatCountUserSet = true;
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
                if (!wasPlaying) {
                    // 止めたい状態。toggleAudio() を使うと userPaused が
                    // 立ってしまい、停止中のキューにスタルウォッチドッグが
                    // 効かなくなる（= 無音のまま固まる）。直接 pause する。
                    pauseForRebuild();
                }
            }
        }
    }

    // 作り直し後に「ユーザー停止」状態で再開するための停止
    // userPaused は「ユーザーが一時停止した」意味だけなので false に戻さない
    function pauseForRebuild() {
        var audio = state.audio;
        if (audio) {
            try { audio.pause(); } catch (e) { /* noop */ }
        }
        state.userPaused = false;
        state.needGesture = false;
        syncAudioButton();
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
        // localStorage の保存値も利用者の選択なので、startPlayback が上書きしない
        state.repeatCountUserSet = true;
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
        // 印刷が起きなかった場合に備えて、状態が残っているときは何もしない
        if (!state.printState) { return; }
        restoreDetails(state.printState);
        state.printState = null;
    }

    function handlePrint() {
        state.printState = openAllDetails();
        // afterprint の購読は bindEvents() で 1 度だけ行う。
        // ここで addEventListener すると押した回数だけリスナーが増える。
        window.setTimeout(function () {
            try { window.print(); } catch (e) { /* noop */ }
        }, 60);
    }

    /* =====================================================================
       ヘルスチェック（真の状態表示）
       ---------------------------------------------------------------------
       「サーバーは生きている」だけでは足りない。`/health` が返す
       `auth_enforced` / `auth_ready` / `auth_mode` / `secret_key_configured` を
        見て、**生成が失敗する理由**まで含めた状態を出す。
       ===================================================================== */

    /**
     * `/health` の JSON から「何が起きているか」を決める（DOM を触らない純粋関数）。
     * `kind` は service-status の色（ok / warn / error）。
     */
    function describeHealth(info) {
        var health = (info && typeof info === 'object') ? info : {};
        var version = health.version ? ' v' + health.version : '';
        var enforced = health.auth_enforced === true || health.auth_required === true;
        // Round 3: 匿名の応答には `auth_ready` がない。
        // 情報開示対策で出さないため、`=== true` だと
        // 「資格情報は未設定」と区別できず、
        // 正常に動くのに「管理者に依頼」を出して
        // **ログイン欄が出ない**。`false` のときだけ「未設定」とし、
        // 欠落は「判定できない（自分の資格情報は関係しない）」と見る。
        var ready = health.auth_ready !== false;
        // Round 3: 認証済みなら `auth_mode` が必ず返る（`session` / `bearer`）。
        // 匿名では**返らない**（情報開示対策）ので空文字になる。
        // この判別で「認証済みの閲覧者」を匿名と同列に扱わない。
        var authMode = String(health.auth_mode || '');
        var isAuthenticated = authMode !== '' && authMode !== 'anonymous';
        var hasKey = health.api_key_configured !== false && health.status !== 'degraded';

        if (enforced && !ready) {
            // 資格情報そのものが無いので、ログイン枠を出しても解決できない。
            // 状態表示に「サーバー管理者へ」を出して終わりにする。
            return {
                kind: 'error',
                authNeeded: false,
                text: '⛔ 認証が有効ですが資格情報が未設定です（RETRO_RADIO_SECRET_KEY）。' +
                    'このままだと番組生成がすべて 503 になります。サーバー管理者に設定を依頼してください。'
            };
        }
        // Round 3: `auth_mode` の条件は判定に使わない。
        // 匿名の `/health` は `auth_mode` を返さない（情報開示対策）ので、
        // 以前の `mode` 比較は永久に偽だった。
        // `enforced` は `auth_required` / `auth_enforced` の 2 フィールドだけで
        // 計算でき、**サーバが常に返す**ので匿名でも判定できる。
        // 認証済みの閲覧者（`auth_mode` が返る）は、ログイン欄を出さない。
        if (enforced && !isAuthenticated) {
            return {
                kind: 'warn',
                authNeeded: true,
                text: '🔒 このサーバーは認証が必要です。上のログインフォームからサインインしてください。'
            };
        }
        if (!hasKey) {
            return {
                kind: 'warn',
                authNeeded: false,
                text: 'APIキー未設定のため「定型原稿モード」で放送します（原稿は自動生成の定型版です）。' +
                    'AI生成の原稿が必要なら、サーバー管理者へ RETRO_RADIO_GEMINI_API_KEY の設定を依頼してください。'
            };
        }
        return {
            kind: 'ok',
            authNeeded: false,
            text: '✅ サーバー接続済み' + version + '（APIキー設定済み / AI原稿生成が利用できます）'
        };
    }

    function applyHealth(info) {
        state.health = (info && typeof info === 'object') ? info : null;
        state.authEnforced = !!(info && (info.auth_enforced === true || info.auth_required === true));
        var view = describeHealth(info);
        setServiceStatus(view.kind, view.text);
        showAuthPanel(view.authNeeded);
        if (view.kind !== 'ok') {
            setStreamDesc(view.text);
        }
        return view;
    }

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
                applyHealth(info);
            })
            .catch(function () {
                window.clearTimeout(timer);
                setServiceStatus('error', '⚠️ サーバーに接続できません。バックエンド（retro_radio サーバー）が起動しているか確認してください。');
            });
    }

    /* ---------------------------------------------------------------------
       ログイン（POST /api/auth/session）
       ---------------------------------------------------------------------
       セッション cookie 前提なので `credentials: 'same-origin'` を付ける
       （`<audio src>` も cookie で認証されるため、fetch だけ cookie を
       送らないと音源だけ 401 になる）。
       旧バックエンド（エンドポイントが無い）では 404 なので、
       ログイン枠を隠して通常利用へ戻す。
    */
    function showAuthPanel(visible) {
        if (!dom.authPanel) { return; }
        dom.authPanel.hidden = !visible;
    }

    function setAuthStatus(text) {
        if (dom.authStatus) { dom.authStatus.textContent = text ? String(text) : ''; }
    }

    function bindAuth() {
        if (!dom.authPanel) { return; }
        dom.authPanel.addEventListener('submit', function (event) {
            // ネイティブ送信はしない（= SPA のページ遷移・再読み込みを起こさない）
            if (event && typeof event.preventDefault === 'function') { event.preventDefault(); }
            submitLogin();
        });
    }

    function submitLogin() {
        var email = String(dom.authEmail && dom.authEmail.value ? dom.authEmail.value : '').trim();
        var password = String(dom.authPassword && dom.authPassword.value ? dom.authPassword.value : '');
        if (!email || !password) {
            setAuthStatus('メールアドレスとパスワードを入力してください。');
            return;
        }
        setAuthStatus('⏳ ログインしています…');
        if (dom.btnAuthLogin) { dom.btnAuthLogin.disabled = true; }

        window.fetch('/api/auth/session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            credentials: 'same-origin',
            cache: 'no-store',
            body: JSON.stringify({ email: email, password: password })
        })
            .then(function (response) {
                return response.text().then(function (text) {
                    return { ok: response.ok, status: response.status, text: text };
                });
            })
            .then(function (result) {
                if (dom.btnAuthLogin) { dom.btnAuthLogin.disabled = false; }
                if (result.status === 404) {
                    // 旧バックエンドにログインが無い。UI からも消して通常操作へ戻す。
                    showAuthPanel(false);
                    setAuthStatus('');
                    setServiceStatus('', 'このサーバーにはログイン機能がありません（認証なしモードで動作します）。');
                    return;
                }
                if (!result.ok) {
                    setAuthStatus(result.status === 401 || result.status === 403
                        ? 'メールアドレスまたはパスワードが正しくありません。'
                        : 'ログインできませんでした（HTTP ' + result.status + '）。');
                    return;
                }
                if (dom.authPassword) { dom.authPassword.value = ''; }
                setAuthStatus('ログインしました。番組を生成できます。');
                showAuthPanel(false);
                announce('ログインしました。');
                checkHealth();
            })
            .catch(function () {
                if (dom.btnAuthLogin) { dom.btnAuthLogin.disabled = false; }
                setAuthStatus('サーバーに接続できませんでした。通信環境を確認してください。');
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

    // 初回表示に適用した既定年。loadDecades() が遅れて返ってきたときに
    // 「まだ誰もダイヤルを触っていない」ことを判定する。
    var appliedInitialYear = 0;

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
                var hasDefault = isFiniteNumber(info.default_year);
                if (hasDefault) {
                    DEFAULT_YEAR = info.default_year;
                }
                // `default_year` はサーバーの既定年。以前は state.year（ハードコード値）
                // に当てていて、サーバーが指定した既定が 無視 されていた。
                // ユーザーが既にダイヤルを動かしていなければ既定年を適用する。
                if (hasDefault && state.year === appliedInitialYear) {
                    appliedInitialYear = clampYear(DEFAULT_YEAR);
                    setYear(appliedInitialYear, { silent: true });
                    return;
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
        dom.songStoreLink = byId('songStoreLink');
        dom.songStoreAnchor = byId('songStoreAnchor');
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
        dom.generationRemaining = byId('generationRemaining');
        dom.generationDriftNote = byId('generationDriftNote');
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

        /* --- 3ステップ導線（ヘッダー） --- */
        dom.flowSteps = byId('flowSteps');
        dom.btnGuideLaunch = byId('btnGuideLaunch');

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

        /* --- 提案⑧: 同意と開示・削除 --- */
        dom.consentDialog = byId('consentDialog');
        dom.consentDialogTitle = byId('consentDialogTitle');
        dom.consentTermsBody = byId('consentTermsBody');
        dom.btnConsentAccept = byId('btnConsentAccept');
        dom.btnConsentDecline = byId('btnConsentDecline');
        dom.btnConsentClose = byId('btnConsentClose');
        dom.consentStatus = byId('consentStatus');
        dom.privacyActions = byId('privacyActions');
        dom.btnExportDataJson = byId('btnExportDataJson');
        dom.btnExportDataCsv = byId('btnExportDataCsv');
        dom.btnDeleteData = byId('btnDeleteData');
        dom.privacyStatus = byId('privacyStatus');

        /* --- 管理者: 監査ログ（admin ロールのときだけ導線を出す） --- */
        dom.adminActions = byId('adminActions');
        dom.btnAuditLaunch = byId('btnAuditLaunch');
        dom.auditDialog = byId('auditDialog');
        dom.auditStats = byId('auditStats');
        dom.auditEntries = byId('auditEntries');
        dom.btnAuditRefresh = byId('btnAuditRefresh');
        dom.btnAuditClose = byId('btnAuditClose');
        dom.auditStatus = byId('auditStatus');

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

        /* --- モードタブの実体（role="tabpanel"） --- */
        dom.modePanel = byId('modePanel');

        /* --- ログイン（認証が有効なときだけ出す） --- */
        dom.authPanel = byId('authPanel');
        dom.authEmail = byId('authEmail');
        dom.authPassword = byId('authPassword');
        dom.btnAuthLogin = byId('btnAuthLogin');
        dom.authStatus = byId('authStatus');
    }

    function bindEvents() {
        bindTuner();
        bindModeTabs();
        bindDecadeChips();
        bindYearStepper();
        bindGuide();

        if (dom.btnPlayRadio) {
            dom.btnPlayRadio.addEventListener('click', function () {
                // ユーザージェスチャ内で AudioContext を作る（suspended 起動の防止）。
                // 生成を ensureAnalyser（fetch 後の非ジェスチャ経路）だけに
                // 委ねると WebKit/Firefox で恒久無音になりうる。
                ensureAudioContext();
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
        // afterprint は 1 度だけ購読する（クリック毎に足すとリスナーが積み上がる）
        window.addEventListener('afterprint', handleAfterPrint);

        bindAuth();
        // 提案⑧: 同意と開示・削除の導線を束ねる
        bindPrivacy();

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
        // ヘッダーの常設導線も同じダイアログを開く
        if (dom.btnGuideLaunch) {
            dom.btnGuideLaunch.addEventListener('click', function () { openGuide(); });
        }
        if (dom.btnGuideClose) {
            dom.btnGuideClose.addEventListener('click', function () { closeGuide(); });
        }

        // 生成した無音 Blob URL を破棄する（ページを去る前に後始末する）
        function releaseSilenceUrls() {
            if (!state.silenceUrls) { return; }
            if (typeof URL !== 'undefined' && typeof URL.revokeObjectURL === 'function') {
                Object.keys(state.silenceUrls).forEach(function (key) {
                    var url = state.silenceUrls[key];
                    if (url && url.indexOf('blob:') === 0) {
                        try { URL.revokeObjectURL(url); } catch (e) { /* noop */ }
                    }
                });
            }
            state.silenceUrls = null;
            state.silenceUrl = '';
        }

        window.addEventListener('pagehide', function () {
            stopVu();
            stopProgress();
            cancelXfade();
            // EventSource を残したままページを去ると接続がリークする
            closeJobStream();
            if (state.skipTimer) { window.clearTimeout(state.skipTimer); state.skipTimer = 0; }
            if (state.announceTimer) { window.clearTimeout(state.announceTimer); state.announceTimer = 0; }
            // 先読み用の <audio> と無音 Blob URL も後始末する
            if (state.preloader) {
                try { state.preloader.pause(); state.preloader.removeAttribute('src'); } catch (e) { /* noop */ }
                state.preloader = null;
            }
            releaseSilenceUrls();
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
        // 聞き取り支援（提案③-2）: 話速 3 段階のボタンを束ねる
        bindPlaybackSpeedControls();
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
        appliedInitialYear = clampYear(DEFAULT_YEAR);
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
        // P1-2: 起動時の同意確認（required && !consented なら同意モーダル）
        checkConsentOnStartup();
        // P1-2: admin ロールのときだけ監査ログの導線を出す
        checkAdminRole();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
