# UI/UX 改善契約書（Wave0）

本書は UI/UX 改修並列実装の**唯一の仕様ソース**である。
SubA / SubB / SubC / SubD / SubE は本書の外で自由に DOM・CSS・JS 構造を変えてはならない。

対象: `static/index.html` / `static/app.css` / `static/app.js` / `tests/test_ui_ux.py`

---

## 0. 変更してはならない既存契約（非回帰条件）

### 0-1. 既存 DOM ID（削除・改名・move 禁止）

`app.js` の `cacheDom()` が参照する下記 32 個は**必ず同じ ID で残す**こと。

```
appIcon            seniorToggle        tabModeNormal
tabModeCare        tabModeAnniversary   careModeBox
anniversaryModeBox  careRecreationDate   anniversaryDate
anniversaryName    btnPlayRadio         broadcastOutput
streamStatusTitle  streamStatusDesc     btnAudioAction
manuscriptTitle    manuscriptBody       btnToggleWriting
btnPrintRecreation songTitle            songArtist
vinylDisk          recreationQuizBox    quizList
tubeBulb           vuMeter              serviceStatus
yearBadge          eraText              tunerRail
tunerNeedle        brassKnob
```

### 0-2. JS が toggle する既存クラス（保持必須）

| クラス | 対象 | 操作 |
|---|---|---|
| `.active` | `mode-tab` / `#broadcastOutput` | add / remove |
| `.visible` | `.mode-custom-box` | toggle |
| `.loading` | `#btnPlayRadio` | toggle |
| `.is-receiving` | `#tubeBulb` | toggle |
| `.input-error` | `.form-field input` | add / remove |
| `.vertical-mode` | `#manuscriptBody` | toggle |
| `.spinning` | `#vinylDisk` | add / remove |
| `.quiz-card` / `.quiz-question-row` / `.quiz-q-badge` / `.quiz-hint` / `.quiz-answer-content` | JS 生成 | 生成コードに依存 |
| `.sr-only` | JS 生成 live region | app.js が `document.createElement` する |
| `.is-receiving` uto | `#tubeBulb` | setLoading が toggle |

### 0-3. 静的ファイル不変条件（pytest が検査する）

| 条件 | 検査元 |
|---|---|
| `index.html` に `/static/app.js` を含む | `tests/test_pwa_banner.py` |
| `index.html` に `/static/app.css` を含む | `tests/test_pwa_banner.py` |
| `index.html` に `manifest.json` を含む | `tests/test_pwa_banner.py` |
| `app.js` に `navigator.serviceWorker.register` を含む | `tests/test_pwa_banner.py` |
| `app.js` に `toUpperCase()` を含む | `tests/test_pwa_banner.py` |
| `app.js` に `/api/generate` を含む | `tests/test_pwa_banner.py` |
| `app.js` に `/api/audio/` を含む | `tests/test_pwa_banner.py` |
| `app.js` に `streamlit` を含まない（小文字） | `tests/test_pwa_banner.py` |
| `static/index.html` を 200 で配信 | `tests/test_security.py` |
| `styles/generated.css` と `design_tokens/**` を変更しない | `tests/test_retro_theme.py` |

---

## 1. SubA の担当: `static/index.html`

### 1-1. 追加する head 要素

- `<meta name="theme-color">` は既存値 `#221c18` のまま維持
- 新規 CSS ファイルは作らず **app.css のみ** ссылка
- `<noscript>` ブロックは維持

### 1-2. body 直下に追加

```html
<a class="skip-link" href="#mainContent">メインコンテンツへスキップ</a>
```

### 1-3. main 要素

```html
<main id="mainContent" tabindex="-1">
```

### 1-4. チューナー表示部（`.tuner-display`）の内部に追加

既存 `#yearBadge` / `#eraText` / `.scale-wrapper` は**位置关系を維持**し、
`.tuner-display` の最後（`.scale-wrapper` の後）に以下を追加する。

```html
<div class="decade-chips" id="decadeChips" role="group" aria-label="年代から選ぶ">
    <button type="button" class="decade-chip" data-decade="1950">1950s</button>
    <button type="button" class="decade-chip" data-decade="1960">1960s</button>
    <button type="button" class="decade-chip" data-decade="1970">1970s</button>
    <button type="button" class="decade-chip" data-decade="1980">1980s</button>
    <button type="button" class="decade-chip" data-decade="1990">1990s</button>
    <button type="button" class="decade-chip" data-decade="2000">2000s</button>
    <button type="button" class="decade-chip" data-decade="2010">2010s</button>
    <button type="button" class="decade-chip" data-decade="2020">2020s</button>
</div>

<div class="year-stepper" id="yearStepper" role="group" aria-label="年の微調整">
    <button type="button" class="year-step-btn" id="btnYearMinus10" aria-label="10年前へ">−10</button>
    <button type="button" class="year-step-btn" id="btnYearMinus1" aria-label="1年前へ">−1</button>
    <label class="sr-only" for="yearInput">年を直接入力</label>
    <input type="text" id="yearInput" class="year-input" inputmode="numeric"
           autocomplete="off" maxlength="4" value="1975"
           aria-describedby="yearInputHint" />
    <button type="button" class="year-step-btn" id="btnYearPlus1" aria-label="1年後へ">+1</button>
    <button type="button" class="year-step-btn" id="btnYearPlus10" aria-label="10年後へ">+10</button>
</div>
<p class="year-input-hint" id="yearInputHint">1950年〜2025年の数字を入力できます</p>
```

### 1-5. `#brassKnob` に追加する属性

既存要素に **属性追加のみ**（構造は変えない）。

```html
role="slider" aria-valuemin="1950" aria-valuemax="2025" aria-valuenow="1975"
aria-label="年をogging buttonsで微調整"
```

### 1-6. `.broadcast-output` の先頭（`.live-stream-bar` の前）に追加

```html
<div class="generation-panel" id="generationPanel" hidden aria-live="polite">
    <div class="generation-panel-head">
        <span class="generation-panel-title" id="generationPanelTitle">📡 電波を受信中…</span>
        <span class="generation-elapsed" id="generationElapsed">0秒</span>
    </div>
    <div class="progress-track" id="progressTrack" role="progressbar"
         aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"
         aria-labelledby="generationPanelTitle">
        <div class="progress-fill" id="progressFill"></div>
    </div>
    <ol class="progress-steps" id="progressSteps">
        <li class="progress-step" data-step="connect" data-status="pending">
            <span class="progress-step-dot" aria-hidden="true"></span>
            <span class="progress-step-name">電波を到医院内</span>
        </li>
        <li class="progress-step" data-step="script" data-status="pending">
            <span class="progress-step-dot" aria-hidden="true"></span>
            <span class="progress-step-name">原稿を書く</span>
        </li>
        <li class="progress-step" data-step="tts" data-status="pending">
            <span class="progress-step-dot" aria-hidden="true"></span>
            <span class="progress-step-name">読み上げる</span>
        </li>
        <li class="progress-step" data-step="song" data-status="pending">
            <span class="progress-step-dot" aria-hidden="true"></span>
            <span class="progress-step-name">ヒット曲を送る</span>
        </li>
    </ol>
    <p class="generation-note">※ 進捗は目安です。ナレーション音声の合成に 30〜60秒かかることがあります。</p>
    <button type="button" class="btn-cancel-generation" id="btnCancelGeneration">⏹ 受信を中止する</button>
</div>
```

### 1-7. `.broadcast-output` 内、`.manuscript-card` の前に追加

```html
<div class="state-banner" id="stateBanner" hidden>
    <span class="state-banner-icon" id="stateBannerIcon" aria-hidden="true">ℹ️</span>
    <div class="state-banner-body">
        <p class="state-banner-title" id="stateBannerTitle"></p>
        <p class="state-banner-message" id="stateBannerMessage"></p>
    </div>
    <button type="button" class="state-banner-close" id="stateBannerClose" aria-label="メッセージを閉じる">✕</button>
</div>

<div class="empty-state" id="emptyState">
    <div class="empty-state-icon" aria-hidden="true">📻</div>
    <h2 class="empty-state-title" id="emptyStateTitle">番組を受信していません</h2>
    <p class="empty-state-desc" id="emptyStateDesc">ダイヤルを合わせて「ラジオを再生する」を押すと、あの年の番組が始まります。</p>
    <div class="empty-state-actions">
        <button type="button" class="btn-primary" id="btnEmptyPlay">📻 1975年の番組を再生する</button>
        <button type="button" class="btn-secondary" id="btnShowGuide">📖 使い方を見る</button>
    </div>
</div>

<div class="error-state" id="errorState" hidden role="alert">
    <div class="error-state-icon" id="errorStateIcon" aria-hidden="true">⚠️</div>
    <h2 class="error-state-title" id="errorStateTitle">放送できません</h2>
    <p class="error-state-message" id="errorStateMessage"></p>
    <div class="error-state-actions">
        <button type="button" class="btn-primary" id="btnRetry">🔄 もう一度試す</button>
        <button type="button" class="btn-secondary" id="btnToggleErrorDetail">技術的な詳細を見る</button>
    </div>
    <pre class="error-state-detail" id="errorStateDetail" hidden></pre>
</div>
```

### 1-8. `.live-stream-bar` を `#playerCard` に置換

既存の `.live-stream-bar`（`#streamStatusTitle` / `#streamStatusDesc` / `#btnAudioAction` を含む）を、
**ID を保ったまま** 次の構造に置き換える。`#btnAudioAction` の **ID と位置（再生/停止主ボタン）** を保つこと。

```html
<section class="player-card" id="playerCard" aria-label="ラジオプレイヤー">
    <div class="player-status">
        <span class="on-air-badge" id="onAirBadge">ON AIR</span>
        <div>
            <div class="stream-meta-title" id="streamStatusTitle">🎙️ ラジオ放送中（1975年）</div>
            <div class="stream-meta-desc" id="streamStatusDesc">ダイヤルを回して好きな年を選び、【ラジオを再生する】を押すと番組が始まります。</div>
        </div>
    </div>

    <div class="player-track">
        <span class="player-track-index" id="trackIndex">- / -</span>
        <div>
            <div class="player-track-title" id="trackTitle">番組を待機中です</div>
            <div class="player-track-artist" id="trackArtist">—</div>
        </div>
    </div>

    <div class="seek-row">
        <span class="seek-time" id="seekCurrent">0:00</span>
        <input type="range" class="seek-bar" id="seekBar" min="0" max="100" value="0" step="0.1"
               aria-label="再生位置" />
        <span class="seek-time" id="seekDuration">0:00</span>
    </div>

    <div class="player-controls">
        <button type="button" class="player-btn" id="btnPrevTrack" aria-label="前のトラックへ">⏮</button>
        <button type="button" class="player-btn" id="btnReplayTrack" aria-label="このトラックをもう一度">🔁</button>
        <button type="button" class="player-btn player-btn-primary" id="btnAudioAction" type="button" aria-label="一時停止・再生">⏸️</button>
        <button type="button" class="player-btn" id="btnNextTrack" aria-label="次のトラックへ">⏭</button>
        <button type="button" class="player-btn" id="btnMute" aria-label="ミュート切り替え" aria-pressed="false">🔊</button>
    </div>

    <div class="player-volume-row">
        <label class="sr-only" for="volumeControl">音量</label>
        <input type="range" class="volume-control" id="volumeControl" min="0" max="1" step="0.05" value="0.8" />
        <button type="button" class="player-btn-sm" id="btnLoop" aria-pressed="false">🔁 連続</button>
    </div>

    <ol class="playlist-list" id="playlistList" aria-label="番組のトラック一覧"></ol>
</section>
```

### 1-9. `</main>` の直後・`</div>(.container)` の直前に追加

```html
<dialog class="guide-dialog" id="guideDialog" aria-labelledby="guideDialogTitle">
    <h2 class="guide-dialog-title" id="guideDialogTitle">📖 レトロラジオ・タイムマシンの使い方</h2>
    <ol class="guide-steps">
        <li>
            <strong>① 年を選ぶ</strong>
            <p>年代チップ・ダイヤル・年ステッパのどれからでも好きな年を選べます。</p>
        </li>
        <li>
            <strong>② モードを選ぶ</strong>
            <p>「タイムマシン」「デイサービス回想法」「記念日ギフト」から用途に合わせます。</p>
        </li>
        <li>
            <strong>③ 再生する</strong>
            <p>30〜60秒ほど待つと、あの年のナレーションが流れます。原稿は自動で全文表示されます。</p>
        </li>
    </ol>
    <button type="button" class="btn-primary" id="btnGuideClose">閉じる</button>
</dialog>
```

---

## 2. SubB の担当: `static/app.css`

### 2-1. 既存色の再定義は禁止

`:root` の既存変数（`--color-bg` / `--color-cabinet` / `--color-brass-primary` ほか 22 個）の**値を変更しない**。
新規は追記のみ。

### 2-2. 追加する CSS カスタムプロパティ（`:root` に追記）

```css
/* 状態色（docs/state_design_system.md に準拠） */
--color-state-success: #4ade80;
--color-state-warning: #fbbf24;
--color-state-error: #f87171;
--color-state-info: #60a5fa;
--color-state-neutral: #ad9c8f;

/* 状態地の背景 */
--color-bg-state-success: rgba(74, 222, 128, 0.12);
--color-bg-state-warning: rgba(251, 191, 36, 0.12);
--color-bg-state-error: rgba(248, 113, 113, 0.12);
--color-bg-state-info: rgba(96, 165, 250, 0.12);
--color-bg-state-neutral: rgba(173, 156, 143, 0.1);

/* フォーカス・オーバーレイ */
--color-focus-ring: #ffcf7a;
--color-overlay: rgba(10, 8, 6, 0.88);
--color-track-bg: #191613;
--color-track-bg-alt: #241e19;

/* 形状 */
--radius-pill: 999px;
--radius-sm: 8px;

/* スペーシング */
--space-xs: 4px;
--space-sm: 8px;
--space-md: 12px;
--space-lg: 16px;
--space-xl: 24px;
--space-2xl: 32px;

/* タイポグラフィ（追加分） */
--font-size-xs: 0.75rem;
--font-size-sm: 0.85rem;
--font-size-md: 1rem;
--font-size-lg: 1.15rem;
--font-size-xl: 1.35rem;

/* モーション */
--ease-standard: cubic-bezier(0.25, 0.46, 0.45, 0.94);
--transition-fast: 150ms var(--ease-standard);
--transition-base: 200ms var(--ease-standard);
--transition-slow: 320ms var(--ease-standard);

/* 最小タップ領域（シニア配慮） */
--touch-target-min: 44px;
```

### 2-3. 必須スタイル定義

以下のセレクタに必ずスタイルを定義する。クラス名は**正確に一致**させること。

**フォーカスリング（グローバル）**

```css
:where(a, button, input, select, textarea, [tabindex]):focus-visible {
    outline: 3px solid var(--color-focus-ring);
    outline-offset: 2px;
    border-radius: var(--radius-sm);
}
```

**スキップリンク** — `.skip-link`（非表示 → フォーカスで可視）
**年代チップ** — `.decade-chips`, `.decade-chip`, `.decade-chip.active`
**年ステッパ** — `.year-stepper`, `.year-step-btn`, `.year-input`, `.year-input-hint`, `.year-input.input-error`
**進捗パネル** — `.generation-panel`, `.generation-panel-head`, `.generation-panel-title`, `.generation-elapsed`, `.progress-track`, `.progress-fill`, `.progress-steps`, `.progress-step`, `.progress-step-dot`, `.progress-step-name`, `.generation-note`, `.btn-cancel-generation`
ステップ状態: `.progress-step[data-status="active"]`, `[data-status="completed"]`, `[data-status="failed"]`

**バナー** — `.state-banner`, `.state-banner[data-kind="success"|"warning"|"error"|"info"]`, `.state-banner-icon`, `.state-banner-body`, `.state-banner-title`, `.state-banner-message`, `.state-banner-close`

**空状態** — `.empty-state`, `.empty-state-icon`, `.empty-state-title`, `.empty-state-desc`, `.empty-state-actions`
**エラー状態** — `.error-state`, `.error-state-icon`, `.error-state-title`, `.error-state-message`, `.error-state-actions`, `.error-state-detail`

**共通ボタン** — `.btn-primary`, `.btn-secondary`（最小高さ `var(--touch-target-min)`）

**プレイヤー** — `.player-card`, `.player-status`, `.player-track`, `.player-track-index`, `.player-track-title`, `.player-track-artist`, `.seek-row`, `.seek-bar`, `.seek-time`, `.player-controls`, `.player-btn`, `.player-btn-primary`, `.player-btn-sm`, `.player-volume-row`, `.volume-control`, `.playlist-list`, `.playlist-item`, `.playlist-item.active`

**ガイド** — `.guide-dialog`, `.guide-dialog-title`, `.guide-steps`
**ユーティリティ** — `.sr-only`（**未定義なら追加必須**。app.js が live region 生成に使う）

### 2-4. レスポンシブ追加

既存 `@media (max-width: 600px)` ブロック**の中に**以下を追記する（ブロックは増やさない）。

- `.decade-chips` → 横スクロール（`overflow-x: auto` / `scrollbar-width: none`）かつ `flex-wrap: nowrap`
- `.year-step-btn` → 最小 `var(--touch-target-min)`
- `.generation-panel` → `padding` 縮小
- `.player-controls` → `gap` 縮小、`flex-wrap: wrap`
- `.seek-row` → `gap: var(--space-sm)`
- `.error-state-actions` / `.empty-state-actions` → `flex-direction: column` / `align-items: stretch`
- `.guide-dialog` → `width: calc(100vw - 32px)`

`@media (max-width: 380px)` にも `.year-step-btn` の縮小を追記する。

### 2-5. シニアモード追���

`body.senior-mode` ブロックに以下を追記する。

- `.year-stepper .year-step-btn` → `min-height: 64px; font-size: 1.3rem`
- `.year-input` → `font-size: 1.4rem; min-height: 64px`
- `.decade-chip` → `font-size: 1.05rem; min-height: 56px`
- `.player-btn` → `width: 72px; height: 72px`
- `.player-btn-primary` → `width: 88px; height: 88px`
- `.progress-step-name` → `font-size: 1.15rem`
- `.error-state-message` / `.empty-state-desc` → `font-size: 1.2rem`
- `.state-banner-message` → `font-size: 1.1rem`

### 2-6. アクセシビリティ / モーション

- 既存 `@media (prefers-reduced-motion: reduce)` ブロックに、新要素向けの上書きを追記する
  （`.progress-fill { transition: none }` / `.vinyl-disk` 回転停止は既存でも可）
- `@media (prefers-contrast: more)` を新規追加し、以下を 加强する
  - `.decade-chip` / `.btn-primary` / `.btn-secondary` / `.player-btn` → `border: 2px solid currentColor`
  - `.progress-fill` → `background: #ffd166`
  - `.progress-step[data-status]` → 状態を色だけに依存させない（✓ / ! / ✕ を `::after` に出す）

### 2-7. 印刷

既存 `@media print` に以下を追記する。

- `.generation-panel` / `.state-banner` / `.empty-state` / `.error-state` / `.player-card` / `.playlist-list` / `.guide-dialog` / `.skip-link` / `.decade-chips` / `.year-stepper` → `display: none !important`

**注意**: 既存 `@media print` は `header, .top-controls, .radio-cabinet, .live-stream-bar, ...` を hide している。
`.live-stream-bar` は削除されるため、`#playerCard` 側にhide を移動すること（原稿のみA4印刷する既存挙動を維持）。

---

## 3. SubD の担当: `static/app.js` 前半

### 3-1. 編集可能リージョン（**この範囲外を触ってはいけない**）

```
行 1 〜 1075   （定数 / state / 小道具 / チューナー / モード / 入力検証 / 再生フロー / レスポンス表示 / renderQuiz）
行 1686 〜 1822 （cacheDom / bindEvents / init）
```

**`buildQueue` -functions from 行1076  onwards は SubE の所有。触らないこと。**
編集は必ず `apply_diff` の完全一致 SEARCH/REPLACE で行うこと（ファイル全書き禁止）。

#### `/api/generate` の `passes`（周回ごとの別プレイリスト）

`buildQueue(data)` は次の順でキューを組み立てる。

1. `data.passes` が**長さ 1 以上の配列**なら、**それを順に連結**する。
   パスごとに**別の曲**が入っているので、同じ曲を 1 回の放送で
   2 回以上流さない。連続 OFF のとき（`effectiveRepeatCount() === 1`）は
   `passes[0]` だけを使う。
2. `data.passes` が無い（**旧サーバー**）場合は、従来どおり
   `buildPass(data)` で 1 パスを作り `effectiveRepeatCount()` 回回す。

`buildPass(data, items)` は第 2 引数にプレイリストを渡すと
その配列だけで 1 パスを作る。省略時は `data.playlist` を使う
（= 旧クライアント互換）。

| フィールド | 意味 |
|---|---|
| `playlist` | 1 パスのプレイリスト（**`passes[0]` と同一**。旧クライアント向け） |
| `passes` | 周回数ぶんのパス。パスごとに別の曲 |
| `loop_count` | サーバーが用意したパス数（既定 3） |

設計の詳細は [`docs/song_catalog.md`](../docs/song_catalog.md) を参照。

### 3-2. 定数追加（`MODE_LABELS` の直後付近）

```js
var DECADE_STARTS = [1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020];
var PROGRESS_STEPS = ['connect', 'script', 'tts', 'song'];
// 経過時間（ms）→ 進捗率の目安。バックエンドは逐次応答しないため推定値を使う
var PROGRESS_TIMELINE = [
    { at: 0,    percent: 5,  step: 'connect' },
    { at: 800,  percent: 20, step: 'script'  },
    { at: 8000, percent: 45, step: 'script'  },
    { at: 20000, percent: 65, step: 'tts'    },
    { at: 45000, percent: 85, step: 'tts'    },
    { at: 70000, percent: 92, step: 'song'   }
];
var PROGRESS_MAX_PERCENT = 95; // 応答受信まで 95% で頭打ちにする（100% は renderSuccess でのみ）
var PROGRESS_TICK_MS = 250;
var RETRY_STORAGE_KEY = 'lastRequest';
var VOLUME_STORAGE_KEY = 'volume';
var LOOP_STORAGE_KEY = 'loopEnabled';
```

### 3-3. `state` への追加

```js
progressTimer: 0,
progressStartedAt: 0,
progressPercent: 0,
lastRequest: null,      // 再試行用に保持する { year, month, day, mode, targetName }
playerBound: false,
```

### 3-4. 追加する関数（サブD 所有）

```js
/* ---------- 年代選択 3 層 ---------- */
function bindDecadeChips()
function bindYearStepper()
function syncYearInputs(year)          // #yearInput.value と #brassKnob[aria-valuenow] を更新
function setDecadeChipActive(year)     // 該当チップに .active
function decadeOf(year)                // Math.floor((year-1950)/10)*10+1950 を 1950..2020 に丸める
function readYearInput()               // クランプ済み年数 or null

/* ---------- 進捗 ---------- */
function startProgress(year, mode)
function tickProgress()
function setProgressStep(stepKey, status)  // 'pending' | 'active' | 'completed' | 'failed'
function stopProgress()
function formatElapsed(ms)              // "0秒" / "1分05��" / "1時間02分"

/* ---------- 状態 ---------- */
function showStateBanner(kind, title, message)   // kind: success|warning|error|info
function hideStateBanner()
function showEmptyState(visible)
function showErrorState(title, message, detail)  // detail 省略可
function hideErrorState()
function openGuide()
function closeGuide()
```

### 3-5. 既存関数への変更点

| 関数 | 変更 |
|---|---|
| `setYear()` | 末尾で `syncYearInputs(value)` と `setDecadeChipActive(value)` を呼ぶ |
| `setMode()` | モード変更時に `showEmptyState(true)` / `hideErrorState()` を呼ぶ |
| `setLoading(true)` |  Probess 開始。`setLoading(false)` で停止（進捗100%は出さない） |
| `startGeneration()` | 成功payload を `state.lastRequest` に保存。fetch 送信直前に `startProgress()` |
| `renderSuccess()` | `stopProgress()` → `setProgressStep` 全完了 → `#generationPanel` を hidden → `hideErrorState()` → `showStateBanner('success', ...)` |
| `renderError()` | `stopProgress()` → `hideErrorState()` → `showErrorState(title, message, rawText)` → 既存の `manuscriptBody.textContent` への代入は**維持**（印刷互換） |
| `showInputError()` | `#yearInput` なら `.input-error` を付与 |
| `init()` | `bindDecadeChips()` / `bindYearStepper()` / `#btnCancelGeneration` / `#btnRetry` / `#btnShowGuide` / `#btnGuideClose` / `#stateBannerClose` / `#btnToggleErrorDetail` / `#btnEmptyPlay` のイベント登録、`showEmptyState(true)`、`localStorage` から `lastRequest` 復元 |

### 3-6. `cacheDom()` に追加する ID

```js
dom.decadeChips = byId('decadeChips');
dom.yearInput = byId('yearInput');
dom.yearInputHint = byId('yearInputHint');
dom.btnYearMinus10 = byId('btnYearMinus10');
dom.btnYearMinus1 = byId('btnYearMinus1');
dom.btnYearPlus1 = byId('btnYearPlus1');
dom.btnYearPlus10 = byId('btnYearPlus10');
dom.generationPanel = byId('generationPanel');
dom.progressTrack = byId('progressTrack');
dom.progressFill = byId('progressFill');
dom.progressSteps = byId('progressSteps');
dom.generationElapsed = byId('generationElapsed');
dom.generationPanelTitle = byId('generationPanelTitle');
dom.btnCancelGeneration = byId('btnCancelGeneration');
dom.stateBanner = byId('stateBanner');
dom.stateBannerIcon = byId('stateBannerIcon');
dom.stateBannerTitle = byId('stateBannerTitle');
dom.stateBannerMessage = byId('stateBannerMessage');
dom.stateBannerClose = byId('stateBannerClose');
dom.emptyState = byId('emptyState');
dom.btnEmptyPlay = byId('btnEmptyPlay');
dom.btnShowGuide = byId('btnShowGuide');
dom.errorState = byId('errorState');
dom.errorStateIcon = byId('errorStateIcon');
dom.errorStateTitle = byId('errorStateTitle');
dom.errorStateMessage = byId('errorStateMessage');
dom.errorStateDetail = byId('errorStateDetail');
dom.btnRetry = byId('btnRetry');
dom.btnToggleErrorDetail = byId('btnToggleErrorDetail');
dom.guideDialog = byId('guideDialog');
dom.btnGuideClose = byId('btnGuideClose');
/* SubE 所有（SubD が cache するのみ） */
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
```

### 3-7. `bindEvents()` に追加する呼び出し

`bindPlayerControls()` は **SubE が定義する**。SubD は `bindEvents()` の最後で **1行だけ** 呼び出す。

```js
if (typeof bindPlayerControls === 'function') { bindPlayerControls(); }
```

### 3-8. 正直さ（Honesty）要件

- 進捗バーは実測ではないため、UI テキストに「目安」「推測」のDana を必ず含める（`#generationPanel` 内の `.generation-note`）
- `PROGRESS_MAX_PERCENT = 95` を越えない。100% にするのは `renderSuccess()` のみ
- タイムアウト（`state.timedOut`）時は 100% にしない

---

## 4. SubE の担当: `static/app.js` 後半（プレイヤー）

### 4-1. 編集可能リージョン

```
行 1076 〜 1685
（buildQueue / createAudioElement / replaceAudioElement / startPlayback / stopPlayback /
  playIndex / updateTrackMeta / prefetchTrack / tryPlay / markNeedsGesture /
  onPlaylistEnd / restartPlayback / onTrackEnded / onAudioError / onAudioPlay / onAudioPause /
  isPlaying / syncAudioButton / toggleAudio / setVuLevel / setTubeLit / stopVu /
  resumeAudioContext / ensureAnalyser / startVuAnalyser / startVuSimulation /
  applySeniorMode / applyVerticalWriting / openAllDetails / restoreDetails /
  handleAfterPrint / handlePrint / checkHealth / isSecureContextForSw /
  registerServiceWorker / loadDecades）
```

**注意**: 上記リージョンに `cacheDom()` / `bindEvents()` / `init()` は**含まれない**
（それらは SubD が所有）。`init()` も SubD 所有。
編集は `apply_diff` の完全一致 SEARCH/REPLACE のみ。ファイル全書き禁止。

### 4-2. 追加する関数（SubE 所有）

```js
/* ---------- プレイヤー UI ---------- */
function bindPlayerControls()          // 1回だけ。state.playerBound で多重登録を防ぐ
function renderPlayerUI()              // #trackIndex / #trackTitle / #trackArtist / ボタン状態を同期
function renderPlaylist(trackQueue)    // #playlistList を再構築。クリックで playIndex(i, true)
function updateSeekBar()               // timeupdate から呼ぶ。#seekBar / #seekCurrent / #seekDuration
function formatTime(seconds)           // "0:00" / "1:23"
function setVolume(value)              // audio.volume に適用し localStorage 保存
function toggleMute()
function setLoopEnabled(enabled)
function applyStoredAudioPrefs()       // localStorage から音量・連続再生を復元
```

### 4-3. 既存関数への変更点

| 関数 | 変更 |
|---|---|
| `updateTrackMeta(track, index)` | 末尾で `renderPlayerUI()` を呼ぶ。**これが懸案3（メタ未反映）の修正点** |
| `playIndex()` | `updateTrackMeta()` 呼び出し後、`state.audio.currentTime = 0` 時に `updateSeekBar()`。`setStreamTitle` / `setStreamDesc` をトラック情報で上書きして画面にも反映する |
| `createAudioElement()` | `timeupdate` リスナ → `updateSeekBar()` を追加。`loadedmetadata` → `updateSeekBar()` を追加 |
| `stopPlayback()` | 末尾で `renderPlaylist([])` と `renderPlayerUI()` を呼ぶ |
| `startPlayback()` | `state.queue` 確定後に `renderPlaylist(state.queue)` を呼ぶ |
| `onPlaylistEnd()` | 連続再生が有効なら先頭へ、なければ「放送終了」表示に `renderPlayerUI()` |
| `onAudioError()` | スキップ後も末尾で `updateSeekBar()` |

### 4-4. 再生ボタンの既存契約

`syncAudioButton()` は既存の `⏸️` / `▶️` を `dom.btnAudioAction` に設定する。
**この関数の挙動は変更しない**（既存 UI 契約）。
追加するのは `#btnAudioAction` 周辺の新規コントロールのみ。

### 4-5. プレイヤー状態

`state` に以下を追加すること（SubD 領域の `state` 定義 nevertheless 確実でないため、
**SubD が `state` に `playerBound: false` を追加済み**であることを前提に、
SubE は `bindPlayerControls()` 内で `state.playerBound` を使う）:

```js
// SubE が使用する想定フィールド（SubD の追加が未反映でも動くよう、X || 0 で初期化すること）
state.muted        // undefined なら false 扱い
state.loopEnabled  // undefined なら false 扱い
```

---

## 5. SubC の担当: `tests/test_ui_ux.py`

pytest ファイル。既存の `tests/test_pwa_banner.py` と同じスタイル（`Path("static/xxx")` を直接読む）。

### 5-1. 必須テストケース

```python
REQUIRED_IDS = [
    "skipLink 相当の .skip-link（クラス存在）",
    "mainContent", "decadeChips", "yearInput", "yearInputHint",
    "btnYearMinus10", "btnYearMinus1", "btnYearPlus1", "btnYearPlus10",
    "generationPanel", "progressTrack", "progressFill", "progressSteps",
    "generationElapsed", "btnCancelGeneration",
    "stateBanner", "stateBannerTitle", "stateBannerMessage", "stateBannerClose",
    "emptyState", "btnEmptyPlay", "btnShowGuide",
    "errorState", "errorStateTitle", "errorStateMessage",
    "errorStateDetail", "btnRetry", "btnToggleErrorDetail",
    "guideDialog", "btnGuideClose",
    "playerCard", "trackIndex", "trackTitle", "trackArtist",
    "seekBar", "seekCurrent", "seekDuration",
    "btnPrevTrack", "btnReplayTrack", "btnNextTrack", "btnMute",
    "volumeControl", "btnLoop", "playlistList",
]
EXISTING_IDS = [ /* 本書 0-1 の 32 個 */ ]
```

テストクラス:

1. `TestIndexContract`
   - `test_existing_ids_are_preserved` — 32 個すべてが `id="..."` として存在
   - `test_new_ids_exist` — `REQUIRED_IDS` すべてが存在
   - `test_skip_link_points_to_main` — `.skip-link` の `href="#mainContent"` と `id="mainContent"` が両方存在
   - `test_no_duplicate_ids` — `id="x"` が2回以上出ないこと
   - `test_index_references_app_css_and_js` — 非回帰
   - `test_manifest_reference` — 非回帰

2. `TestAriaContract`
   - `test_progressbar_has_role_and_bounds` — `role="progressbar"` と `aria-valuemin` / `aria-valuemax` / `aria-valuenow`
   - `test_brass_knob_is_slider` — `role="slider"` と `aria-valuemin` / `aria-valuemax` / `aria-valuenow`
   - `test_year_input_is_numeric` — `inputmode="numeric"`
   - `test_error_state_has_alert_role` — `id="errorState"` を含む要素に `role="alert"`
   - `test_toggle_buttons_have_aria_pressed` — `#btnMute` と `#btnLoop` に `aria-pressed`
   - `test_dialog_has_labelledby` — `#guideDialog` に `aria-labelledby="guideDialogTitle"`
   - `test_decade_chips_are_buttons` — `class="decade-chip"` の要素がすべて `<button type="button">` で `data-decade` を持つ
   - `test_decade_chips_cover_all_decades` — 1950/1960/1970/1980/1990/2000/2010/2020 の 8 個

3. `TestCssContract`
   - `test_sr_only_class_defined` — `.sr-only`  定义が app.css に存在
   - `test_focus_visible_rule_defined` — `:focus-visible` が app.css に存在
   - `test_new_component_classes_defined` — `.decade-chip` / `.progress-fill` / `.player-card` / `.error-state` / `.empty-state` / `.state-banner` / `.guide-dialog` が app.css に存在
   - `test_existing_tokens_not_broken` — `--color-brass-primary` / `--color-bg` / `--font-serif` が `app.css` に存在
   - `test_reduced_motion_block_exists`
   - `test_print_hides_new_components` — `@media print` の中に `.generation-panel` と `.player-card` への hide がある

4. `TestJsContract`
   - `test_app_js_parses` — `node --check` が使える環境で実行。不可なら括弧バランス検査に fallback
   - `test_existing_function_names_preserved` — `setYear` / `setMode` / `startGeneration` / `renderSuccess` / `renderError` / `renderQuiz` / `buildQueue` / `playIndex` / `updateTrackMeta` / `cancelGeneration` / `cacheDom` / `bindEvents` / `init` が app.js に存在
   - `test_new_functions_defined` — `bindDecadeChips` / `bindYearStepper` / `startProgress` / `stopProgress` / `showStateBanner` / `showEmptyState` / `showErrorState` / `openGuide` / `bindPlayerControls` / `renderPlayerUI` / `renderPlaylist` / `updateSeekBar` / `formatTime` が app.js に存在
   - `test_pwa_strings_preserved` — `navigator.serviceWorker.register` / `/api/generate` / `/api/audio/` / `toUpperCase()` を含む
   - `test_no_streamlit` — `streamlit` が小文字で含まれない
   - `test_existing_dom_ids_cached` — `cacheDom()` 内で 32 個すべての `byId('...')` が依然存在する
   - `test_progress_capped_before_success` — `PROGRESS_MAX_PERCENT` が定義され、95 以下の値

5. `TestNoBackendRegression`
   - `test_static_dir_untouched` — `static/manifest.json` の `start_url` が `"/"` のまま
   - `test_generated_css_untouched` — `styles/generated.css` が存在し `:root` を含む
   - `test_design_tokens_untouched` — `design_tokens/primitives/color.json` に `color-brand-500` が `"#d47300"` のまま

---

## 6. Wave3 統合チェックリスト

- [ ] `node --check static/app.js` が成功する
- [ ] `app.js` の `cacheDom()` に本書 3-6 の全 ID がある
- [ ] `bindEvents()` 末尾に `bindPlayerControls()` の呼び出しがある
- [ ] `index.html` に **重複 ID が 1 つも無い**
- [ ] `app.css` に本書 2-3 の全セレクタの定義がある
- [ ] `pytest tests/test_ui_ux.py tests/test_pwa_banner.py tests/test_security.py tests/test_server_api.py tests/test_retro_theme.py` が全件 green
- [ ] `styles/generated.css` と `design_tokens/**` に差分がない
- [ ] `retro_radio/` 配下に差分がない（バックエンド非変更）
