# マイクロインタラクション設計システム

## 概要
ホバー・フォーカス・アクティブ・トランジションの体系的設計を行い、ユーザーインターフェースに命吹き込み、直感的で応答性の高い体験を提供する。

## インタラクション状態の定義

| 状態 | 説明 | トリガー | 目的 |
|------|------|----------|------|
| **デフォルト** | 通常状態 | なし | 基本的な視覚的基盤 |
| **ホバー** | ポインターオーバー時 | マウス・タッチホバー | インタラクティブであることを示す |
| **フォーカス** | キーボードフォーカス時 | Tabキー・プログラム的フォーカス | アクセシビリティのための視覚的インディケータ |
| **アクティブ** | クリック・タップ押下時 | マウスダウン・タッチ開始 | アクションの実行をフィードバック |
| **ディセーブル** | 無効状態 | プログラム的状態変更 | インタラクション不可能であることを示す |
| **訪問済みリンク** | 以前に訪問済みリンク | ページ遷移履歴 | ナビゲーションのコンテキスト提供 |

## 視覚変化の体系

### 色変化（カラートークン連携）
```css
/* ベースカラー --color-interactive-primary: {color-brand-500}; */
/* ホバー --color-interactive-hover: 色を15% brighten; */
/* フォーカス --color-interactive-focus: 色を20% brighten + アウトライン; */
/* アクティブ --color-interactive-active: 色を10% darken; */
/* ディセーブル --color-interactive-disabled: 色を50% darken, opacity: 0.5; */
```

### スケール変化
- **ホバー**: scale(1.02 - 1.05)
- **アクティブ**: scale(0.95 - 0.98)
- **フォーカス**: 通常スケール（フォーカスリングで強調）

### シャドウ変化
- **デフォルト**: 0 2px 4px rgba(0,0,0,0.1)
- **ホバー**: 0 4px 8px rgba(0,0,0,0.15)
- **フォーカス**: 0 0 0 3px rgba(255,215,0,0.5) （フォーカスリング）
- **アクティブ**: 0 1px 2px rgba(0,0,0,0.1)

### ボーダー変化
- **デフォルト**: 2px solid transparent または 1px solid #ddd
- **ホバー**: 2px solid {color-interactive-hover}
- **フォーカス**: 2px solid {color-interactive-focus} または アウトライン
- **アクティブ**: 2px solid {color-interactive-active}

### トランジション設計
| プロパティ | 期間 | イージングカーブ | 用途 |
|------------|------|------------------|------|
| 色変化 | 150ms | cubic-bezier(0.25, 0.46, 0.45, 0.94) | 背景・テキストカラー |
| スケール変化 | 120ms | cubic-bezier(0.25, 0.46, 0.45, 0.94) | transform: scale() |
| シャドウ変化 | 180ms | cubic-bezier(0.25, 0.46, 0.45, 0.94) | box-shadow |
| ボーダー変化 | 150ms | cubic-bezier(0.25, 0.46, 0.45, 0.94) | border-color |
| 不透明度変化 | 100ms | linear | opacity |

## コンポーネント別マイクロインタラクション仕様

### ボタン
| 状態 | 色変化 | スケール | シャドウ | ボーダー | その他 |
|------|--------|----------|----------|----------|--------|
| デフォルト | --color-bg-primary | scale(1) | 0 2px 4px rgba(0,0,0,0.1) | なし | --color-text-primary |
| ホバー | --color-bg-primary-hover | scale(1.03) | 0 4px 8px rgba(0,0,0,0.15) | なし | --color-text-primary-on-hover |
| フォーカス | --color-bg-primary | scale(1) | 0 0 0 3px rgba(255,215,0,0.5) | 3px solid --color-focus | --color-text-primary |
| アクティブ | --color-bg-primary-active | scale(0.97) | 0 1px 2px rgba(0,0,0,0.1) | なし | --color-text-primary |
| ディセーブル | --color-bg-disabled | scale(1) | なし | なし | --color-text-disabled, cursor: not-allowed |

### フォーム要素（入力・テキストエリア・セレクト）
| 状態 | ボーダー変化 | シャドウ変化 | 背景変化 | その他 |
|------|--------------|--------------|----------|--------|
| デフォルト | 2px solid --color-border | なし | --color-bg-input | --color-text-input |
| ホバー | 2px solid --color-border-hover | なし | --color-bg-input-hover | --color-text-input |
| フォーカス | 2px solid --color-focus | 0 0 0 3px rgba(255,215,0,0.5) | --color-bg-input | --color-text-input, ラベルアニメーション |
| アクティブ | 2px solid --color-focus | 0 0 0 3px rgba(255,215,0,0.5) | --color-bg-input | --color-text-input |
| ディセーブル | 2px solid --color-border-disabled | なし | --color-bg-disabled | --color-text-disabled, cursor: not-allowed |
| エラー | 2px solid --color-error | なし | --color-bg-input-error | --color-text-error |
| 成功 | 2px solid --color-success | なし | --color-bg-input-success | --color-text-success |

### チェックボックス・ラジオボタン
| 状態 | ボーダー変化 | 背景変化 | チェックマーク | アニメーション |
|------|--------------|----------|----------------|----------------|
| デフォルト | 2px solid --color-border | --color-bg-input | 透明 | なし |
| ホバー | 2px solid --color-border-hover | --color-bg-input-hover | 透明 | なし |
| フォーカス | 2px solid --color-focus | --color-bg-input | 透明 | フォーカスリング |
| チェック済み | 2px solid --color-interactive-primary | --color-interactive-primary | 表示 | スケール 0→1 (120ms) |
| チェック済みホバー | 2px solid --color-interactive-hover | --color-interactive-hover | 表示 | スケール 1.05→1 (120ms) |
| チェック済みフォーカス | 2px solid --color-interactive-focus | --color-interactive-focus | 表示 | フォーカスリング |
| ディセーブル | 2px solid --color-border-disabled | --color-bg-disabled | 透明（薄い） | なし |

### スライダー
| 状態 | つまみ変化 | バー変化 | アニメーション |
|------|------------|----------|----------------|
| デフォルト | --color-thumb, scale(1) | --color-track | なし |
| ホバー | --color-thumb-hover, scale(1.1) | --color-track-hover | つまみ: 120ms |
| フォーカス | --color-thumb, フォーカスリング | --color-track | つまみ: フォーカスリング |
| アクティブ（ドラッグ中） | --color-thumb-active, scale(1.2) | --color-track-active | なし |
| ディセーブル | --color-thumb-disabled | --color-track-disabled | なし |

### トースト・スナックバー
| 状態 | 位置変化 | 不透明度変化 | アニメーション |
|------|----------|--------------|----------------|
| 表示開始 | 下から上がる（translateY: 100% → 0） | 0 → 1 | 300ms ease-out |
| 表示中 | 固定 | 1 | なし |
| 非表示開始 | 下に下がる（translateY: 0 → 100%） | 1 → 0 | 300ms ease-in |
| ホバー中 | 一時停止 | 1 | アニメーション一時停止 |

## トークンシステムへの追加設計

### デザイントークン構造拡張
```json
{
  "interactions": {
    "hover": {
      "scale": "1.03",
      "shadow-intensity": "0.15",
      "color-brightness": "15%"
    },
    "focus": {
      "shadow": "0 0 0 3px rgba(255,215,0,0.5)",
      "outline-width": "3px",
      "outline-offset": "2px"
    },
    "active": {
      "scale": "0.97",
      "shadow-intensity": "0.1",
      "color-brightness": "-10%"
    },
    "transition": {
      "duration-fast": "100ms",
      "duration-moderate": "150ms",
      "duration-slow": "200ms",
      "easing-standard": "cubic-bezier(0.25, 0.46, 0.45, 0.94)",
      "easing-decelerate": "cubic-bezier(0.0, 0.0, 0.2, 1)",
      "easing-accelerate": "cubic-bezier(0.4, 0.0, 0.6, 1)"
    }
  }
}
```

### セマンティックトークン例
```json
{
  "color-interactive-primary": "{color-brand-500}",
  "color-interactive-hover": "{color-brand-400}",
  "color-interactive-focus": "{color-brand-300}",
  "color-interactive-active": "{color-brand-600}",
  "color-interactive-disabled": "{color-gray-400}",
  "color-focus-ring": "rgba(255,215,0,0.5)",
  "shadow-hover": "0 4px 8px rgba(0,0,0,0.15)",
  "shadow-focus": "0 0 0 3px rgba(255,215,0,0.5)",
  "shadow-active": "0 1px 2px rgba(0,0,0,0.1)"
}
```

## アクセシビリティ考慮

### フォーカス可視性
- WCAG 2.1 AA 準拠のフォーカスインディケータ
- 最小 2px コントラスト比
- フォーカスリングの太さ: 3px以上
- フォーカスインディケータは要素の境界から離れて配置可能

### 色コントラスト
- すべての状態変化で WCAG 2.1 AA コントラスト比を維持
- ディセーブル状態でも 4.5:1 のコントラスト比（テキスト対背景）
- 色のみに依存しない状態表現（形状・アニメーション・テキストの組み合わせ）

### モーション配慮
- prefers-reduced-motion メディアクエリ対応
- アニメーション期間を 50% 削減またはキャンセル
- 必要なフィードバックは非アニメーション形式で提供
- スケルトンやプレイスホルダーはアニメーションを控えめに

### キーボード操作性
- タブ順序の論理的な流れ
- エンターキー・スペースキーでのアクティベーション
- ESC キーでのキャンセル・閉じる
- 矢印キーでのスライダー・スピンボタン操作

## 実装優先順位
1. **ボタンマイクロインタラクション**（基本かつ頻繁に使用）
2. **フォーム要素マイクロインタラクション**（入力体験の中核）
3. **ナビゲーション・トランジション**（ページ遷移の体感品質）
4. **フィードバックコンポーネント**（トースト・スナックバー・ローディング）
5. **高度なインタラクション**（ドラッグ・ドロップ・ジェスチャー）

## 使用ガイドライン

### 期間選択の原則
- **即時フィードバック** (50-100ms): ボタンの色変化、インプットのボーダー変化
- **操作フィードバック** (100-200ms): スケール変化、シャドウ変化
- **コンテキスト変化** (200-300ms): 位置変化、サイズ変化、複数プロパティ変化

### イージングカーブ選択
- **標準**: cubic-bezier(0.25, 0.46, 0.45, 0.94) （自然な動き）
- **減速**: cubic-bezier(0.0, 0.0, 0.2, 1) （フェードアウト等）
- **加速**: cubic-bezier(0.4, 0.0, 0.6, 1) （フェードイン等）
- **弾性**: cubic-bezier(0.68, -0.55, 0.265, 1.55) （バウンス効果・注意して使用）

### テストチェックリスト
- [ ] すべてのインタラクティブ要素にホバー状態がある
- [ ] すべてのインタラクティブ要素にフォーカス状態がある（WCAG 準拠）
- [ ] すべてのインタラクティブ要素にアクティブ状態がある
- [ ] ディセーブル状態が適切に視覚的に表現されている
- [ ] トランジション期間が 100-200ms の範囲内にある
- [ ] prefers-reduced-motion が正しく機能する
- [ ] 色コントラスト比が WCAG 2.1 AA を満たしている
- [ ] キーボードのみで完全に操作可能である