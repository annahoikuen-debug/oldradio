# 状態設計システム

## 概要
エラー・空・ローディング状態の統一デザインシステムを定義し、一貫したユーザー体験を提供する。

## 状態タイプ分類

### 1. フィードバック状態（ユーザーアクションの結果）
| 状態 | 用途 | アイコン | トークン | 値 | アクション導線 |
|------|------|----------|----------|----|----------------|
| **成功** | 処理完了、保存完了 | ✓ チェックマーク | `--color-state-success` | `#4ade80` | 次のステップへ、完了 |
| **警告** | 注意喚起、確認必要 | ⚠ 三角形+! | `--color-state-warning` | `#fbbf24` | 確認、詳細を見る |
| **エラー** | 処理失敗、入力不正 | ✕ バツ印 | `--color-state-error` | `#f87171` | 再試行、サポート、代替手段 |
| **情報** | ヒント、補足説明 | ℹ 円形+i | `--color-state-info` | `#60a5fa` | 詳しく見る、設定 |

### 2. コンテンツ状態（データの有無・読み込み状況）
| 状態 | 用途 | アイコン | 使うトークン | アクション導線 |
|------|------|----------|------------|----------------|
| **空状態** | データなし、初回訪問 | 📭 箱/📻 ラジオ | `--color-state-neutral` / `--color-bg-state-neutral`（専用トークンなし） | 作成する、サンプルを見る、ガイド |
| **ローディング** | データ取得中、処理中 | ⏳ スピナー | `--color-state-info`（専用トークンなし） | キャンセル、推定時間 |
| **アイドル** | 待機中、入力待ち | — | `--color-text-muted`（専用トークンなし） | — |

## 視覚言語の統一

### アイコンシステム
- **サイズ**: 24px (インライン), 48px (バナー), 64px (フルスクリーン), 96px (モーダル)
- **スタイル**: アウトラインスタイル、2px ストローク
- **カラー**: 状態色に準拠（`currentColor` 継承）

### カラーパレット（デザイントークン連携）

**正本は [`../static/app.css`](../static/app.css) の `:root` です。**
下に示すのは `app.css` から抜き出した実値です。**ここが `app.css` と
乖離してはいけません**（CSS 側が正で、乖離した場合は CSS を直します）。

```css
/* 状態Foreground（app.css :root の実値） */
--color-state-success: #4ade80;
--color-state-warning: #fbbf24;
--color-state-error:   #f87171;
--color-state-info:    #60a5fa;
--color-state-neutral: #ad9c8f;

/* 状態地の背景（0.1〜0.12 のアルファ） */
--color-bg-state-success: rgba(74, 222, 128, 0.12);
--color-bg-state-warning: rgba(251, 191, 36, 0.12);
--color-bg-state-error:   rgba(248, 113, 113, 0.12);
--color-bg-state-info:    rgba(96, 165, 250, 0.12);
--color-bg-state-neutral: rgba(173, 156, 143, 0.1);

/* フォーカス・オーバーレイ */
--color-focus-ring: #ffcf7a;
--color-overlay: rgba(10, 8, 6, 0.88);
--color-track-bg: #191613;
--color-track-bg-alt: #241e19;
```

> **注意（実際の CSS と本書の差分）**
> `app.css` が実際に定義しているのは `success` / `warning` / `error` / `info` /
> **`neutral`** の5色と `--color-focus-ring` だけです。
> **`--color-state-empty` / `--color-state-loading` / `--color-state-idle` と
> 対応する `--color-bg-state-empty` / `-loading` は CSS に存在しません。**
> 空・ローディング・アイダルの見た目は、上表の色と既存の
> `--color-text-muted` / `--color-cabinet-inner` を組み合わせて表現します。
> 新しいトークンが必要なら、まず **`app.css` を直してから**この表を更新してください。

### タイポグラフィ
| 要素 | サイズ | 太さ | 行高 |
|------|--------|------|------|
| タイトル | 1.125rem (18px) | 600 | 1.4 |
| 本文 | 1rem (16px) | 400 | 1.6 |
| アクションボタン | 0.875rem (14px) | 500 | 1.4 |
| 補足テキスト | 0.75rem (12px) | 400 | 1.5 |

### レイアウト・スペーシング
- **コンテナパディング**: 24px (モバイル), 32px (デスクトップ)
- **要素間ギャップ**: 16px (標準), 24px (大)
- **アイコン・テキスト間**: 12px
- **アクションボタン間**: 12px

## アクション導線設計

### エラー状態
1. **プライマリ**: 再試行ボタン（自動リトライ or 手動）
2. **セカンダリ**: 代替手段（例: オフラインモード、キャッシュ使用）
3. **ターシャリ**: サポートへ連絡、ヘルプセンター、ログ送信

### 空状態
1. **プライマリ**: 作成・追加・開始アクション（CTA）
2. **セカンダリ**: サンプル・テンプレート・デモ表示
3. **ターシャリ**: オンボーディングガイド、ヘルプ

### ローディング状態
1. **プライマリ**: キャンセルボタン（長時間処理の場合）
2. **情報表示**: 推定残り時間、進捗パーセンテージ、処理中のステップ名

## アクセシビリティ考慮

### スクリーンリーダー対応
- **role="alert"**: エラー・警告（即座にアナウンス）
- **role="status"**: 成功・情報・ローディング（ポリートにアナウンス）
- **aria-live="polite"**: 空状態、アイドル
- **aria-busy="true"**: ローディング中のコンテナ

### フォーカス管理
- エラー表示時: 最初のアクションボタンにフォーカス
- モーダル表示時: モーダル内の最初のフォーカス可能要素にトラップ
- 空状態のCTA: 自然なTab順序に組み込み

### 色のみに依存しない情報伝達
- アイコン + テキスト + 色の組み合わせ
- パターン・テクスチャによる補完（ハイコントラストモード）

### prefers-reduced-motion
- スピナーアニメーションの無効化
- トランジションの無効化
- 静的な進捗バー表示への切り替え

## コンポーネントインターフェース定義

### 共通プロパティ
```python
class StateComponentProps:
    # 共通
    variant: Literal["banner", "inline", "modal", "fullscreen"]  # 表示バリエーション
    size: Literal["compact", "regular", "large"]  # サイズ
    title: str  # 必須：状態のタイトル
    message: str  # 必須：詳細メッセージ
    icon: Optional[str] = None  # カスタムアイコン（SVGパスまたは絵文字）
    
    # アクション
    primary_action: Optional[Action] = None
    secondary_actions: List[Action] = []
    
    # アクセシビリティ
    aria_live: Literal["assertive", "polite", "off"] = "polite"
    role: Literal["alert", "status", "region"] = "status"
    
    # 動作
    dismissible: bool = False
    auto_dismiss: Optional[int] = None  # ミリ秒

class Action:
    label: str
    on_click: Callable
    variant: Literal["primary", "secondary", "tertiary", "link"] = "secondary"
    disabled: bool = False
    loading: bool = False
```

### エラーコンポーネント固有
```python
class ErrorStateProps(StateComponentProps):
    error_code: Optional[str] = None  # エラーコード（サポート用）
    retry_action: Optional[Action] = None  # 専用リトライアクション
    support_url: Optional[str] = None
    show_technical_details: bool = False
    technical_details: Optional[str] = None
```

### 空状態コンポーネント固有
```python
class EmptyStateProps(StateComponentProps):
    illustration: Optional[str] = None  # イラストSVGパス
    illustration_alt: str = ""  # イラストのaltテキスト
    cta_action: Optional[Action] = None  # メインCTA
    secondary_cta: Optional[Action] = None  # セカンダリCTA
```

### ローディングコンポーネント固有
```python
class LoadingStateProps(StateComponentProps):
    progress: Optional[float] = None  # 0-100 (Noneでスピナー)
    estimated_time: Optional[int] = None  # 推定秒数
    steps: Optional[List[LoadingStep]] = None  # ステップ表示
    cancellable: bool = False
    cancel_action: Optional[Action] = None

class LoadingStep:
    name: str
    status: Literal["pending", "active", "completed", "failed"]
    detail: Optional[str] = None
```

## 実装優先順位
1. **基本実装**: インライン・バナー・モーダルの3バリエーション
2. **サイズバリエーション**: compact/regular/large
3. **フルスクリーン**: メンテナンス・致命的エラー用
4. **高度な機能**: プログレスステップ、推定時間、キャンセル

## 使用ガイドライン

### いつ使うか
| シナリオ | 推奨状態 | バリエーション |
|----------|----------|----------------|
| API呼び出し失敗 | エラー | インライン/バナー |
| フォームバリデーション失敗 | エラー | インライン |
| 検索結果0件 | 空状態 | インライン/バナー |
| 初回ログイン・データなし | 空状態 | モーダル/フルスクリーン |
| ページ読み込み中 | ローディング | フルスクリーン/バナー |
| ボタンクリック後の処理中 | ローディング | インライン/ボタン内 |
| 長時間バッチ処理 | ローディング | モーダル（プログレス付き） |

### アンチパターン
- ❌ 同一画面に複数のバナーエラーを表示
- ❌ 空状態にCTAがない
- ❌ ローディングにキャンセル手段がない（30秒以上）
- ❌ エラーメッセージが技術的すぎる（ユーザー向けに翻訳）
- ❌ 色のみで状態を区別