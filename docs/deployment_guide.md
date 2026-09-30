# デプロイメントガイド

## 環境変数

### 必須環境変数
| 変数名 | 説明 | デフォルト |
|--------|------|------------|
| DATABASE_URL | データベース接続URL | sqlite:///./retro_radio.db |
| GEMINI_API_KEY | Google Gemini API キー | (必須) |
| STRIPE_SECRET_KEY | Stripe シークレットキー | (本番必須) |
| STRIPE_PUBLISHABLE_KEY | Stripe 公開キー | (本番必須) |
| STRIPE_WEBHOOK_SECRET | Stripe Webhook シークレット | (本番必須) |
| SECRET_KEY | セッション暗号化キー | (必須) |

### オプション環境変数
| 変数名 | 説明 | デフォルト |
|--------|------|------------|
| DEBUG | デバッグモード | false |
| LOG_LEVEL | ログレベル | INFO |
| PORT | ポート番号 | 8501 |

## デプロイ手順

### 1. 環境変数設定
`ash
# .env ファイル作成
cat > .env << EOF
DATABASE_URL=postgresql://user:pass@localhost/retro_radio
GEMINI_API_KEY=your-api-key
STRIPE_SECRET_KEY=sk_...
STRIPE_PUBLISHABLE_KEY=pk_...
STRIPE_WEBHOOK_SECRET=whsec_...
SECRET_KEY=your-secret-key
DEBUG=false
EOF
`",
",

`ash
# マイグレーション実行
alembic upgrade head

# 初期データ投入 (必要な場合)
python scripts/init_db.py
`",
",

`ash
# 開発環境
streamlit run app.py

# 本番環境 (Gunicorn + Uvicorn 等)
uvicorn retro_radio.main:app --host 0.0.0.0 --port 8501 --workers 4
`",
",

- [ ] 環境変数すべて設定済み
- [ ] データベースマイグレーション完了
- [ ] SSL 証明書設定済み
- [ ] Stripe Webhook URL 設定済み
- [ ] 静的ファイル配信設定 (Nginx等)
- [ ] ログローテーション設定
- [ ] ヘルスチェックエンドポイント確認
- [ ] バックアップスケジュール設定

## ロールバック手順
`ash
# 前のバージョンに戻す
git checkout <previous-tag>
alembic downgrade -1
systemctl restart retro-radio
`",
",

- ヘルスチェック: GET /health
- メトリクス: Prometheus + Grafana 推奨
- アラート: エラー率 > 1% でアラート
レスポンス時間 > 2s でアラート
