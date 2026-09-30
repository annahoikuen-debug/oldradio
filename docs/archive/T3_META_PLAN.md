# T3 実装計画書作成のためのメタプラン

## T3 構成方針
改善案9（アクセシビリティ・国際化・モバイルUX）と本番運用要件（監視・ログ・バックアップ・スケーリング）を統合

## T3 12ステップ構成案

| Step | 領域 | 内容 | 成果物 |
|------|------|------|--------|
| 1 | アクセシビリティ基盤 | WCAG 2.1 AA準拠・ARIA・キーボード操作・スクリーンリーダー対応 | `utils/accessibility.py`, CSS拡張 |
| 2 | 国際化(i18n) | gettextベース・日本語/英語切替・日付数値ロケール対応 | `utils/i18n.py`, `.po`ファイル |
| 3 | モバイルUX強化 | タッチターゲット・ジェスチャー・PWAインストール促進・オフラインUI | `ui/mobile.py`, Service Worker拡張 |
| 4 | 監視・メトリクス | Prometheusメトリクス・ヘルスチェック拡張・構造化ログ | `services/monitoring.py` |
| 5 | 分散トレーシング | OpenTelemetry・リクエストID伝播・レイテンシ可視化 | `utils/tracing.py` |
| 6 | エラートラッキング | Sentry統合・ユーザー影響度分類・自動アラート | `utils/error_tracking.py` |
| 7 | 設定・機能フラグ | LaunchDarkly風・段階的ロールアウト・A/Bテスト基盤 | `services/feature_flags.py` |
| 8 | データ永続化・バックアップ | SQLite→PostgreSQL移行パス・履歴エクスポート・定期バックアップ | `services/storage.py` |
| 9 | スケーリング対応 | セッション外部化(Redis)・ステートレス化・水平スケール設計 | `services/scaling.py` |
| 10 | セキュリティ強化 | CSP・HSTS・入力検証強化・レート制限・監査ログ | `utils/security.py` |
| 11 | ドキュメント・運用手順 | API仕様・運用Runbook・障害対応フロー・オンボーディング | `docs/`, `RUNBOOK.md` |
| 12 | 受け入れテスト・リリース検証 | 本番同等環境でのE2E・カナリアデプロイ・ロールバック検証 | `tests/test_acceptance.py` |

## 各ステップ共通フォーマット（T1/T2準拠）
1. **対象ファイル**: 新規/更新パス
2. **作業内容**: 実装コード例込み
3. **依存関係**: requirements追加
4. **テスト**: ユニット・統合・E2E・手動確認
5. **完了基準**: 客観的判定条件

## 次アクション
上記構成で `IMPLEMENTATION_PLAN_T3.md` を作成します。よろしければ続行します。