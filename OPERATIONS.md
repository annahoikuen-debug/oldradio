# トラブルシューティングガイド

## 起動に関する問題

### Q: `streamlit run` コマンドが見つからない / 使えない
A: Streamlit はこのプロジェクトから完全に廃止されました。
起動は uvicorn のみです。

```bash
uvicorn retro_radio.server:app --host 0.0.0.0 --port 8501
```

Windows なら `run_retro_radio.bat` / `run_retro_radio_ja.bat`、
開発者は `debug.bat`（`--reload` + `--log-level debug`）を使えます。

### Q: サーバーが起動直後に落ちる
A: ログを確認してください。

- `ModuleNotFoundError: No module named 'fastapi'` → `pip install -r requirements.txt`
- `Address already in use` → 8501番ポートが使用中です（`netstat -ano | findstr :8501`）
- `RuntimeWarning: RETRO_RADIO_SECRET_KEY が未設定です` → 警告のみ。起動は続行しますが
  セッション/認証機能は使えません。本番では必ず設定してください
- SQLite ファイルがロック中 → `PRAGMA busy_timeout=60000` / WAL 設定が `db/session.py` にあります

## 設定・環境変数に関する問題

### Q: 「Gemini APIキーが設定されていません」と表示される
A:
1. https://aistudio.google.com/app/apikey でAPIキーを取得
2. 環境変数として設定:
   - Windows: `set RETRO_RADIO_GEMINI_API_KEY=your-key-here`
   - Linux/Mac: `export RETRO_RADIO_GEMINI_API_KEY=your-key-here`
3. または `.env` ファイルを作成（`.env.example` をコピーして編集）:
   ```
   RETRO_RADIO_GEMINI_API_KEY=your-key-here
   ```
4. アプリを再起動

> 変数名には必ず `RETRO_RADIO_` プレフィックスが付きます。
> `config.py` の `env_prefix="RETRO_RADIO_"` / `case_sensitive=False` のためです。
> そのため素の `GEMINI_API_KEY` は読み込まれません。
>
> なお、Streamlit 時代の `.streamlit/secrets.toml` / `config.toml` 読み込み機構は
> 存在しません。万一 `secrets.toml` を作成しても**読み込まれません**（この
> プロジェクトで参照するコードは 0 件）。API キーは必ず `.env` または
> プロセス環境変数で渡してください。

### Q: 変更した環境変数が反映されない
A: `Settings` は `@lru_cache()` で1プロセスに1回だけ生成されます。
サーバーを再起動してください。`.env` は起動時にのみ読み込まれます。

### Q: `RETRO_RADIO_CORS_ORIGINS` の設定で起動しない / 形式が判らない
A: このフィールドは `NoDecode` 付きで、`split_cors_origins` の `mode="before"`
バリデータ（`config.py` の `parse_cors_origins`）が**先に**値を正規化してから
型検証に入ります。つまり **CSV でも JSON 配列でも `*` でも
`SettingsError` にはなりません**。

```bash
# どれでも起動する
RETRO_RADIO_CORS_ORIGINS=["http://localhost:8501","http://127.0.0.1:8501"]
RETRO_RADIO_CORS_ORIGINS=http://localhost:8501,http://127.0.0.1:8501
RETRO_RADIO_CORS_ORIGINS=*
```

PaaS の環境変数（`render.yaml` / `fly.toml` / Railway の Variables）でも同じです。
`.env` を完全に削除すればコード既定値（localhost のみ）に戻ります。

> 補足: `SettingsError` を実際に観測するのは、`*` と
> `RETRO_RADIO_CORS_ALLOW_CREDENTIALS=1` を組み合わせた場合だけです
> （`reject_cors_wildcard_with_credentials` が起動時に拒否します）。
> ブラウザが攻撃者のオリジンを `Access-Control-Allow-Origin` に反射したまま
> 資格情報つきリクエストも通してしまうためです。

## データベースに関する問題

### Q: `sqlite3.OperationalError: FOREIGN KEY constraint failed` が出る
A: `db/session.py` が `PRAGMA foreign_keys=ON` を有効化しているため、古い
テーブル定義（`ON DELETE CASCADE` なし）のまま `delete_old()` などを実行すると
外部キー違反になります。マイグレーションを適用してください。

```bash
alembic upgrade head
```

### Q: テーブル定義を変更したのに反映されない
A: `Base.metadata.create_all()` は**新規テーブルの作成のみ**を行います。
既存テーブルは変更されません。必ず `alembic revision --autogenerate` で
migration を作成して適用してください。

### Q: 管理者ユーザー（PROプラン）を作り直したい
A: repository は `flush()` までが責務で、commit は `get_db()` contextmanager のみが
行います。**必ず contextmanager の中で操作すること**が原則です。

```bash
python scripts/init_db.py            # テーブル作成 / マイグレーション適用
python scripts/create_admin.py       # 既定の管理者を作成（PRO）
python scripts/set_admin.py a@b.c   # 既存ユーザーを PRO に変更
```

`get_db_sync()` は後方互換用のラッパーで **commit しません**。commit 漏れの原因になるので
新規コードでは使わないでください。

## ヘルスチェック・運用

### Q: ヘルスチェックの URL は?
A: **`GET /health`** です（JSON を返します）。

**匿名で叩いた場合**（監視・コンテナ/K8s のプローブ이는通常こちら）:

```
$ curl http://localhost:8501/health
{"status":"degraded","service":"Retro Radio Time Machine","version":"2.0.0",
 "api_key_configured":false,"auth_required":true,"auth_enforced":true}
```

**認証済みで叩いた場合**（追加で認証フィールドが返ります）:

```
$ curl -b "retro_radio_session=<Cookie>" http://localhost:8501/health
{"status":"degraded","service":"Retro Radio Time Machine","version":"2.0.0",
 "api_key_configured":false,"auth_required":true,"auth_enforced":true,
 "secret_key_configured":true,"auth_ready":true,"auth_mode":"session"}
```

- `status: "degraded"` は `RETRO_RADIO_GEMINI_API_KEY` 未設定の状態で、**HTTP 200 を返します**
  （コンテナ/K8s のヘルスチェックは「到達できること」が目的なので正常です）
- `status: "healthy"` にするには API キーを設定してください

**情報開示の方針（重要）**: `auth_mode` / `auth_ready` / `secret_key_configured` は
**認証済みの呼び出しにだけ**返ります。これらは攻撃者に「窃取した Cookie / Bearer を使うか、
未認証の経路を探すか」を選ばせる手がかりになるため、匿名には出しません
（`retro_radio/server.py` の `health` の docstring に明記）。
**監視スクリプトが `auth_ready` を見たい場合は認証情報を付けて叩いてください。**

| フィールド | 匿名にも出る | 意味 | 異常時の対処 |
|---|---|---|---|
| `status` | 是 | `healthy` / `degraded` | `degraded` なら API キー未設定 |
| `api_key_configured` | 是 | `RETRO_RADIO_GEMINI_API_KEY` の有無 | `false` なら定型原稿モード |
| `auth_required` | 是 | `RETRO_RADIO_REQUIRE_AUTH` の値（既定 `true`） | `false` なら意図せず保護が切れています |
| `auth_enforced` | 是 | このプロセスが実際に保護を適用しているか | `false` かつ `auth_required=true` なら環境変数と `Settings` が矛盾しています（この場合 `/api/generate` は 503） |
| `auth_ready` | **否** | 資格情報が 1 つでも存在するか | `false` かつ `auth_required=true` なら **`/api/generate` は 503** |
| `auth_mode` | **否** | 実際に成立した認証方式（`disabled` / `session` / `bearer` / `unavailable`） | — |
| `secret_key_configured` | **否** | `RETRO_RADIO_SECRET_KEY` の有無 | `false` なら画面ログインが使えません |

- `auth_enforced` は「`_auth_enforced()` が保護を適用しているか」です。
  **503 が返るのは `resolve_mode() == "unavailable"` のとき**（資格情報が無い）であり、
  `auth_enforced: false` そのものが原因ではありません。
- 旧ドキュメントの `/?health=check` は SPA の HTML を返すだけなので
  ヘルスチェックとして使用できません

### Q: 音声が再生されない
A:
1. ブラウザの音量とタブのミュート状況を確認
2. ネットワーク障害で gTTS / iTunes へ接続できない場合、フォールバック音源に切り替わります
3. ブラウザのコンソールで `/api/audio/{filename}` のレスポンスを確認
4. 別ブラウザ（Chrome推奨）で試す

### Q: ページが読み込み途中で止まる
A:
1. ページを更新（F5またはCtrl+R）
2. ブラウザのキャッシュをクリア（Ctrl+Shift+R）
3. 拡張機能を無効にして試す（特に広告ブロッカー）
4. `GET /health` が 200 を返すか確認する

### Q: 「混雑しています」（HTTP 503）が表示される
A: 同時生成数が `RETRO_RADIO_MAX_CONCURRENT_GENERATIONS`（既定 2）に達しました。
Gemini / gTTS / iTunes はいずれもブロッキングHTTPのため、同時実行数を制限しています。
待って再試行するか、必要なら上限を引き上げてください。

### Q: `/api/generate` が 503 を返す（"混雑しています" ではない）
A: 同時実行数の上限ではありません。**認証が fail-closed になっている**状態です。
`RETRO_RADIO_REQUIRE_AUTH=1`（既定）かつ `RETRO_RADIO_SECRET_KEY` も
`RETRO_RADIO_SINGLE_USER_KEY` も未設定だと、`require_auth_config()` は
`"unavailable"` を返し、保护対象エンドポイントは**すべて 503** になります
（起動時に `RuntimeWarning` が出ます）。

動作する設定は次のどちらか一方だけです。

```bash
# (1) 施設利用（認証あり）
RETRO_RADIO_REQUIRE_AUTH=1
RETRO_RADIO_SECRET_KEY=<32文字以上のランダム値>   # 画面ログイン
# または
RETRO_RADIO_SINGLE_USER_KEY=<ランダム値>          # 単一ベearer

# (2) 個人利用（認証なし）— 明示的に選ぶ
RETRO_RADIO_REQUIRE_AUTH=0
```

`GET /health` の `auth_required` / `auth_ready` / `auth_mode` / `auth_enforced`
で現状が分かります。詳細は [`docs/privacy_and_tenancy.md`](docs/privacy_and_tenancy.md)。

### Q: ログイン（`POST /api/auth/session`）が 503 / 400 を返す
A: 503 は認証資格情報が未設定（fail-closed）、400 は
`RETRO_RADIO_REQUIRE_AUTH=0` で認証が意図的に無効になっている状態です。
連続失敗は 429（スロットリング）になります。`429` は `(email, IP)` の失敗回数だけを
漏らすので、登録の有無は列挙できません。

## 運用チェックリスト
- [ ] `GET /health` が 200 を返し `status` が `healthy` であること
- [ ] `GET /health` の `auth_required` が意図した値であること
- [ ] `GET /health` の `auth_ready` が `true`（認証を有効にする構成なら）
- [ ] `RETRO_RADIO_SECRET_KEY` が設定されていること
- [ ] `alembic current` が `head` と一致していること
- [ ] `.env` が `.gitignore` に含まれていること（コミットしない）
- [ ] ページが正常に読み込まれ、CORS設定が実際の公開オリジンに合っていること
- [ ] 音声が正常に再生されていること
- [ ] サーバーコンソールにエラーがないこと
- [ ] `docker logs` / PaaS のログにエラーがないこと
