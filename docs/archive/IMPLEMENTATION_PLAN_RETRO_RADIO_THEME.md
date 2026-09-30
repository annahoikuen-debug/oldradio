# 実装計画書: レトロラジオ・テーマ一新

## 概要
現在の青基調(#1f6feb)テーマを、温かみのあるレトロラジオ風（アンバー/オレンジ/ブラウン系）に一新する。真空管ラジオ、木製キャビネット、カセットテープ、レコード盤、原稿用紙などのメタファーをUI全体に統合。

---

## 1. デザイントークン更新

### 1.1 プリミティブカラー追加 (`design_tokens/primitives/color.json`)
```json
{
  "color-brand-500": "#d47300",    /* アンバー：メインブランド */
  "color-brand-600": "#b86200",    /* ホバー */
  "color-brand-100": "#fff3e0",    /* ライト背景 */
  "color-vintage-paper": "#fdf6e3", /* 古紙風 */
  "color-tube-glow": "#ffb000",    /* 真空管の光 */
  "color-midnight-navy": "#0d1b2a", /* 深夜放送ネイビー */
  "color-midnight-orange": "#ff8c00", /* 深夜放送オレンジ */
  "color-midnight-text": "#f5f0e1", /* 深夜放送テキスト */
  "color-vinyl-black": "#0d0d0d",  /* レコード盤黒 */
  "color-vinyl-groove": "#1a1a1a", /* レコード溝 */
  "color-cassette-shell": "#2d2d2d", /* カセット外装 */
  "color-cassette-tape": "#1a1a1a", /* カセットテープ */
  "color-cassette-label": "#f5f0e1" /* カセットラベル */
}
```

### 1.2 セマンティックカラー更新 (`design_tokens/semantics/color.json`)
- ライトモード: 温かみのあるアンバー系を基調
- ダークモード: 「深夜放送」専用テーマ（単なる反転ではない）

---

## 2. CSS/スタイル実装

### 2.1 レトロテクスチャ背景 (`body::before`)
- SVGノイズパターン（微細なグランジ）
- 紙目パターン（繊細な横線）
- ラジオ波模様（サイン波）
- `opacity: 0.03`、`pointer-events: none`、`z-index: -1`

### 2.2 メインタイトル「真空管グロー」
```css
.main-title {
  text-shadow: 
    0 0 10px var(--dt-color-tube-glow),
    0 0 20px var(--dt-color-tube-glow),
    0 0 40px rgba(255,176,0,0.4);
  animation: tubeFlicker 3s ease-in-out infinite alternate;
}
@keyframes tubeFlicker {
  0%, 100% { opacity: 1; filter: brightness(1); }
  50% { opacity: 0.85; filter: brightness(1.15); }
}
```

### 2.3 ラジオチューナー風スライダー
- 年号目盛り（1950, 1960...2020）をCSSで描画
- ノブ: 丸いダイヤル風（グラデーション+影）
- ドラッグ時: Web Audio API で微かな「カチッ」音
- 周波数メーター風の視覚フィードバック

### 2.4 ローディング「ラジオノイズ→受信」
```html
<canvas id="noise-canvas"></canvas>
<div class="loading-steps">
  <span data-step="1">📡 チューニング中...</span>
  <span data-step="2">📻 電波を捕捉...</span>
  <span data-step="3">✅ 受信完了</span>
</div>
```
- Canvas/WebGL でホワイトノイズアニメーション
- ステップごとにテキスト切替
- 完了時: 「ピロリーン」受信音（Web Audio API: オシレーター + エンベロープ）

### 2.5 原稿用紙風スクリプト表示
```css
.script-text {
  background: 
    repeating-linear-gradient(
      transparent, transparent 32px,
      rgba(0,0,0,0.05) 32px, rgba(0,0,0,0.05) 33px
    ) var(--dt-color-vintage-paper);
  font-family: "Noto Serif JP", serif;
  writing-mode: vertical-rl; /* オプションで縦書き切替 */
  padding: 2rem;
  border-radius: 8px;
  border: 2px solid var(--dt-color-brand-200);
}
```

### 2.6 レコード盤風「今日の一曲」
```css
.song-info {
  aspect-ratio: 1;
  border-radius: 50%;
  background: radial-gradient(circle at center, #1a1a1a 30%, #0d0d0d 100%);
  border: 4px solid #333;
  box-shadow: 
    0 0 0 2px #444,
    0 10px 30px rgba(0,0,0,0.5),
    inset 0 0 60px rgba(0,0,0,0.5);
  animation: spinRecord 20s linear infinite paused;
}
.song-info.playing { animation-play-state: running; }
@keyframes spinRecord { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
/* センターラベル・溝ディテール追加 */
```

### 2.7 放送日誌/カセットテープ風履歴
```css
.history-item {
  background: linear-gradient(135deg, #2d2d2d 0%, #1a1a1a 100%);
  border-radius: 8px;
  border: 2px solid #444;
  position: relative;
}
.history-item::before {
  /* カセット穴・リール描画 */
}
.history-item:hover .playhead {
  animation: playheadMove 1.5s ease-in-out infinite;
}
```

### 2.8 深夜放送ダークモード
```css
@media (prefers-color-scheme: dark) {
  :root {
    --dt-color-background-base: #0d1b2a;
    --dt-color-background-elevated: #13293d;
    --dt-color-foreground-base: #f5f0e1;
    --dt-color-foreground-muted: #c9b896;
    --dt-color-interactive-primary: #ff8c00;
    --dt-color-interactive-primary-hover: #ffa500;
    --dt-color-border-base: #1f3a4d;
    --dt-color-tube-glow: #ffb000;
    /* グロー効果強化 */
  }
  .main-title { text-shadow: 0 0 15px #ff8c00, 0 0 30px #ff8c00, 0 0 60px rgba(255,140,0,0.5); }
}
```

---

## 3. JavaScript/Web Audio API 実装

### 3.1 音響フィードバック
```javascript
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();

// チューナークリック音
function playTunerClick() {
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = 'square';
  osc.frequency.value = 800;
  gain.gain.setValueAtTime(0.1, audioCtx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.05);
  osc.connect(gain).connect(audioCtx.destination);
  osc.start();
  osc.stop(audioCtx.currentTime + 0.05);
}

// 受信完了音「ピロリーン」
function playReceptionSound() {
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = 'sine';
  [660, 880, 1320].forEach((freq, i) => {
    osc.frequency.setValueAtTime(freq, audioCtx.currentTime + i * 0.15);
  });
  gain.gain.setValueAtTime(0.2, audioCtx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.6);
  osc.connect(gain).connect(audioCtx.destination);
  osc.start();
  osc.stop(audioCtx.currentTime + 0.6);
}
```

### 3.2 ノイズキャンバス
```javascript
function drawNoise(canvas) {
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  const imgData = ctx.createImageData(w, h);
  const data = imgData.data;
  for (let i = 0; i < data.length; i += 4) {
    const val = Math.random() * 255;
    data[i] = data[i+1] = data[i+2] = val;
    data[i+3] = 128;
  }
  ctx.putImageData(imgData, 0, 0);
}
requestAnimationFrame(() => drawNoise(canvas));
```

---

## 4. Streamlit アプリ統合 (`app.py`)

### 4.1 CSS注入更新
- `utils/design_tokens.py` でトークン再生成
- 生成されたCSSを `st.markdown(..., unsafe_allow_html=True)` で注入
- 既存インラインスタイルを削除/置換

### 4.2 コンポーネント置換
- `st.slider` → カスタムHTML/JSスライダーコンポーネント
- `st.progress` + `st.spinner` → カスタムローディングコンポーネント
- `st.text_area` → 原稿用紙風カスタムコンテナ
- サイドバー履歴 → カセットテープ風カード

### 4.3 状態管理
- `st.session_state` でローディング段階・音声再生状態管理
- `st.rerun()` でアニメーション状態同期

---

## 5. テスト計画（リグレッション防止）

### 5.1 単体テスト
| テストファイル | 対象 | 内容 |
|---|---|---|
| `tests/test_design_tokens.py` | トークン変換 | JSON→CSS変換、参照解決、ダークモード上書き |
| `tests/test_color_tokens.py` | カラートークン | 新規レトロ色の存在確認、コントラスト比検証 |
| `tests/test_theme_css.py` | 生成CSS | 必須CSS変数の存在、構文正当性 |

### 5.2 統合テスト
| テストファイル | 対象 | 内容 |
|---|---|---|
| `tests/test_retro_theme_integration.py` | アプリ統合 | 全コンポーネントがレトロテーマ適用されているか |
| `tests/test_dark_mode_midnight.py` | ダークモード | 深夜放送テーマが正しく適用されるか |
| `tests/test_audio_feedback.py` | 音響 | Web Audio API初期化・再生がエラーにならないか |

### 5.3 ビジュアルリグレッション
| テストファイル | 対象 | 内容 |
|---|---|---|
| `tests/visual/test_retro_theme_snapshot.py` | スクリーンショット | 主要画面のスナップショット比較（Playwright/Percy） |
| `tests/visual/test_animations.py` | アニメーション | 主要アニメーションが実行されるか |

### 5.4 アクセシビリティテスト
- コントラスト比 (WCAG AA: 4.5:1)
- `prefers-reduced-motion` 対応確認
- キーボード操作確認
- スクリーンリーダー対応（ARIA属性）

---

## 6. 実装順序（依存関係順）

```
Phase 1: 基盤
  1.1 primitives/color.json 更新
  1.2 semantics/color.json 更新
  1.3 design_tokens.py 実行 → generated.css 更新

Phase 2: コアスタイル
  2.1 レトロテクスチャ背景
  2.2 メインタイトル真空管グロー
  2.3 深夜放送ダークモード

Phase 3: インタラクティブコンポーネント
  3.1 ラジオチューナー風スライダー + Web Audio
  3.2 ローディング「ノイズ→受信」+ Web Audio
  3.3 原稿用紙風スクリプト表示
  3.4 レコード盤風楽曲カード
  3.5 カセットテープ風履歴

Phase 4: アプリ統合
  4.1 app.py スタイル全面置換
  4.2 カスタムコンポーネント埋め込み
  4.3 状態管理・イベントハンドリング

Phase 5: テスト・検証
  5.1 単体テスト作成・実行
  5.2 統合テスト作成・実行
  5.3 ビジュアルリグレッション実行
  5.4 アクセシビリティ検証
  5.5 手動QA・クロスブラウザ確認
```

---

## 7. リスク・対策

| リスク | 影響度 | 対策 |
|---|---|---|
| Streamlit標準コンポーネントのスタイル上書き困難 | 高 | `!important` 使用最小化、カスタムHTMLコンポーネント併用 |
| Web Audio API ブラウザ互換性 | 中 | `try-catch` でフォールバック、ユーザー操作後に初期化 |
| アニメーション性能（モバイル） | 中 | `prefers-reduced-motion` 対応、GPU加速（`transform: translateZ(0)`） |
| ダークモード色覚バリアフリー | 高 | コントラスト比検証ツールで自動チェック、手動確認 |
| 既存機能への副作用 | 高 | 包括的リグレッションテストスイート実装、段階的デプロイ |

---

## 8. 完了基準

- [ ] 全プリミティブ/セマンティックトークンがレトロ色に更新済み
- [ ] 生成CSSに全新規変数が含まれる
- [ ] ライト/ダーク両モードで意図した見た目
- [ ] スライダー操作でクリック音鳴動
- [ ] ローディングでノイズ→受信演出・完了音鳴動
- [ ] スクリプト表示が原稿用紙風（縦書き切替可能）
- [ ] 楽曲カードがレコード盤風・回転アニメーション
- [ ] 履歴がカセットテープ風・ホバーアニメーション
- [ ] 全自動テストパス
- [ ] ビジュアルリグレッション差分なし（許容範囲内）
- [ ] アクセシビリティ基準クリア