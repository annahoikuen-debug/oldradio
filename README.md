# 📻 Retro Radio Time Machine（レトロラジオ・タイムマシン）v2.0.0

<div align="center">

![Version](https://img.shields.io/badge/version-v2.0.0-gold.svg?style=flat-square)
![Architecture](https://img.shields.io/badge/arch-FastAPI%20%2B%20Pure%20SPA-blue.svg?style=flat-square)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-brightgreen.svg?style=flat-square)

**1950年〜2025年の昭和・平成・令和を、その年のニュースとヒット曲でたどる AI ラジオ番組生成サービス。**

**【登録不要】** ・ **【1クリックで再生開始】** ・ **【介護デイサービスの回想法レク】** ・ **【誕生日・記念日ギフト】**

</div>

---

## 🎯 このリポジトリは

指定した年月日のラジオ番組を生成し、**原稿（ナレーションの音声）** と **当時のヒット曲のプレビュー** を連続再生できる Web アプリです。

- **フロントエンド**: Vanilla HTML5 / CSS / JavaScript の Single Page Application（ビルド不要・フレームワークなし）
- **バックエンド**: FastAPI + gTTS + Gemini API
- **音声合成**: gTTS（日本語 / `co.jp`）
- **API キー**: 環境変数のみで管理。ブラウザからキーを入力・保存する画面は**存在しません**

> **Streamlit は完全に廃止されました。** 旧 UI 層（`retro_radio/ui/`）、i18n 重複層、`progressive_loading.py` などは削除済みです。起動は必ず `uvicorn` 経由で行ってください。

---

## 🚀 クイックスタート

### 必要環境

- Python 3.11 / 3.12 / 3.13（Dockerfile は `python:3.11-slim` を基準にしています）
- ネットワークアクセス（gTTS・iTunes・Gemini の各 API 呼び出し）

### 方式 A: Windows ワンクリック起動

リポジトリ直下のバッチをダブルクリックします。

```bat
run_retro_radio.bat
```

ブラウザが `http://localhost:8501` を自動で開きます。

### 方式 B: コマンドライン

```bash
pip install -r requirements.txt

# .env.example をコピーして編集する
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux

uvicorn retro_radio.server:app --port 8501
```

開発中は `--reload` を付けてください。

```bash
uvicorn retro_radio.server:app --port 8501 --reload
```

### 方式 C: Docker

```bash
docker build -t retro-radio .
docker run --rm -p 8501:8501 --env-file .env retro-radio
```

イメージには `HEALTHCHECK`（`/health` を 30 秒間隔でポーリング）が設定されています。

---

## ⚙️ 環境変数

すべての設定値は `RETRO_RADIO_` プレフィックスの環境変数、または `.env` ファイルで指定します。定義の正本は `retro_radio/config.py` です。

| 変数 | 既定値 | 説明 |
|---|---|---|
| `RETRO_RADIO_GEMINI_API_KEY` | （空） | **未設定でも起動します**（→ [API キー未設定時](#api-キー未設定時)）。設定すると AI による原稿生成が有効になります |
| `RETRO_RADIO_GEMINI_MODEL` | `gemini-3.5-flash-lite` | 原稿生成に使う Gemini モデル |
| `RETRO_RADIO_TTS_LANGUAGE` / `_TTS_TLD` | `ja` / `co.jp` | gTTS の言語・音声ドメイン |
| `RETRO_RADIO_MIN_YEAR` / `_MAX_YEAR` | `1950` / `2025` | 選択可能な年代の範囲 |
| `RETRO_RADIO_MAX_CONCURRENT_GENERATIONS` | `2` | 同時に実行できる番組生成の上限 |
| `RETRO_RADIO_CORS_ORIGINS` | `http://localhost:8501,http://127.0.0.1:8501` | **既定はワイルドカードではありません。** カンマ区切りで指定します |
| `RETRO_RADIO_CORS_ALLOW_CREDENTIALS` | `false` | クロスオリジンの資格情報送信を許可するか |
| `RETRO_RADIO_DATABASE_URL` | `sqlite:///./retro_radio.db` | SQLite が既定（PostgreSQL は `postgresql+psycopg://`） |
| `RETRO_RADIO_REQUIRE_AUTH` | `1`（安全側） | `/api/generate` と `/api/audio/*` の認証要否。`1` のまま鍵が無い場合は **fail-closed（503）**。個人利用で認証なしで動かす場合のみ `0` |
| `RETRO_RADIO_SECRET_KEY` | （空） | セッション署名と画面ログイン（`POST /api/auth/session`）の鍵。空だと認証は `503` |
| `RETRO_RADIO_SINGLE_USER_KEY` | （空） | 個人モード用の単一ベアラー資格情報。`SECRET_KEY` が無くても `Authorization: Bearer` で通す |
| `RETRO_RADIO_ADMIN_EMAILS` | （空） | 初期管理者として当てるメールアドレス（CSV / JSON 配列 / 単独のいずれも可） |

`Settings`（`retro_radio/config.py`）の外で、直接 `os.environ` を読む変数が 4 つあります。

| 変数 | 既定値 | 説明 |
|---|---|---|
| `RETRO_RADIO_CSP` | （空） | Content-Security-Policy ヘッダの**完全上書き**。空なら `server.py` の既定ポリシーを使用 |
| `RETRO_RADIO_HSTS_ENABLED` | `1` | HSTS ヘッダの有無 |
| `RETRO_RADIO_HSTS_MAX_AGE` | `31536000` | HSTS の `max-age`（秒） |
| `RETRO_RADIO_FULL_SCRIPT_TTS` | `1` | 全体版ナレーションの TTS。コストを許容できない環境では `0` で無効化 |

> `RETRO_RADIO_CORS_ORIGINS` は **CSV / JSON 配列 / 単独の `*` / 単独の origin のすべてを受け付けます**
> （`NoDecode` + `split_cors_origins` による正規化。CSV で `SettingsError` にはなりません）。

### API キー未設定時

`RETRO_RADIO_GEMINI_API_KEY` が空でも**起動は妨げません**。このとき `GET /health` は `status: "degraded"` / `api_key_configured: false` を返し、フロントエンドは HUD（画面上部）に

> ⚠️ APIキー未設定のため「定型原稿モード」で放送します（原稿は自動生成の定型版です）。

と表示します。番組は**モードに応じた定型原稿**（年代ごとのひな形文）で生成され、TTS と曲検索は通常どおり動作します。Gemini の呼び出しは発生しません。

---

## 📻 3つの放送モード

| モード | `mode` 値 | 対象 | 特徴 |
|---|---|---|---|
| タイムマシン | `normal` | 個人鑑賞・作業 BGM | 1950〜2025年の年を選び、当時のニュースとヒット曲を流す |
| デイサービス回想法レク | `care_recreation` | 介護施設・老人ホーム | 実施日の月日を入力すると、当日の時系列番組表と「思い出クイズ＆会話のタネ」が出力されます。A4 印刷用レイアウトに対応 |
| 記念日・誕生日ギフト | `anniversary` | 還暦・誕生日・結婚記念日 | 対象者名と日付を入力すると、その当時の世相を反映した祝いの番組を生成 |

---

## 🔌 API

起動後 `http://localhost:8501/docs` に OpenAPI（Swagger UI）が公開されます。

| メソッド | パス | 説明 |
|---|---|---|
| `POST` | `/api/generate` | 番組生成（同期）。`year` / `month` / `day` / `mode` / `target_name` を指定 |
| `POST` | `/api/jobs` | 番組生成を**非同期ジョブ**として受付（`202` + `job_id` + `events_url`） |
| `GET` | `/api/jobs/{job_id}` | ジョブ状態（ポーリング用。生成結果も `result` に含まれる） |
| `DELETE` | `/api/jobs/{job_id}` | ジョブの協調的キャンセル |
| `GET` | `/api/jobs/{job_id}/events` | 進捗の SSE 配信（`Last-Event-ID` で再開可） |
| `POST` | `/api/auth/session` | 資格情報（メール+パスワード / ベアラートークン）を検証しセッション Cookie を発行 |
| `GET` | `/api/decades` | 選択可能な年代一覧と既定年 |
| `GET` | `/api/audio/{filename}` | 生成済み TTS 音声（mp3）の配信 |
| `GET` | `/health` | ヘルスチェック（`api_key_configured` / `auth_*` を含む） |
| `GET` | `/` | SPA（`static/index.html`） |
| `GET` | `/static/*` | フロントエンドのアセット |
| `GET` | `/docs` | OpenAPI（Swagger UI） |

### 入力バリデーション

- `year` は `1950`〜`2025`、`month` は `1`〜`12`、`day` は `1`〜`31`
- **実日付として検証**されるため、`2020-02-30` のような存在しない日付は **422** で拒否されます
- `mode` は `normal` / `care_recreation` / `anniversary` のいずれか

### 同時実行制限

`POST /api/generate` は **同期処理**です。Gemini・gTTS・iTunes はいずれもブロッキング HTTP 呼び出しのため、FastAPI のスレッドプールで実行し、同時実行数を既定 **2** に制限しています。上限超過時は **503**（"混雑しています…"）を返します。

---

## ⏱️ 既知の制約と未実装機能

正直に書いておきます。

### 応答が遅い

`/api/generate` は原稿生成後、**トークセグメントごとに gTTS を 4〜6 回連続 HTTP 呼び出し**します。キャッシュミス時は **数十秒〜数分かかる**ことがあります。1 回の生成の待ち時間を避けたい場合は **非同期経路** `POST /api/jobs` を使ってください（`202` で `job_id` を返し、`GET /api/jobs/{job_id}` でポーリング、または `/api/jobs/{job_id}/events` の SSE で進捗を受け取れます）。契約は [`docs/jobs_and_progress.md`](docs/jobs_and_progress.md) を参照。

### TTS キャッシュ

TTS 音声は一時ディレクトリに **SHA-256（テキスト＋言語＋`tld`＋`slow`）** をキーとしてキャッシュされます。書き込みは一時ファイル → `os.replace` の atomic rename（Windows のファイルロックに 3 回までリトライ）。TTL（既定 7 日）を超えたファイルの削除は、**起動時と `generate_tts_cached` の呼び出し回数**でトリガーされます（`RETRO_RADIO_TTS_CACHE_SWEEP_INTERVAL`、既定 50 回 = 概ね 8〜9 回の生成）。時間間隔ではなく**呼び出し回数**で駆動されます。

### 部分実装・到達できないもの

コードとして存在するが、**UI から到達できず API にも接続されていないもの**、
および**実装は済んでいるが画面が無いもの**が複数あります。

- **認証・登録**: `Authenticator` と `POST /api/auth/session`（ログイン → セッション Cookie）が実装済みです。`RETRO_RADIO_REQUIRE_AUTH` の既定は **1（安全側）** で、`/api/generate` と `/api/audio/*` は認証を要求します。認証を有効にしたまま `RETRO_RADIO_SECRET_KEY` も `RETRO_RADIO_SINGLE_USER_KEY` も未設定だと **fail-closed で 503** を返します。個人利用で認証なしで動かすなら `RETRO_RADIO_REQUIRE_AUTH=0` を**明示的に**設定してください。詳細は [`docs/privacy_and_tenancy.md`](docs/privacy_and_tenancy.md)
- **Pro プラン API**: `retro_radio/api/v1.py` は `server.py` に `include` されていないため到達不可
- **Stripe 決済**: `BillingManager` / `WebhookHandler` は実装済みですが、決済エンドポイントは未実装です
- **生成回数制限**: 撤廃されています（FREE でも無制限）
- **履歴・お気に入り**: DB 層（SQLAlchemy / Alembic / SQLite・PostgreSQL）は実装済みですが、履歴・お気に入りを表示する UI はありません。`/api/generate` は DB に `audit_logs` への**監査行のみ**を書きます（下記）
- **ElevenLabs TTS**: `retro_radio/core/tts.py` に実 HTTP 実装がありますが、現在の生成経路は **gTTS のみ** を使用します（`RETRO_RADIO_ELEVENLABS_API_KEY` を設定しても生成経路には影響しません）
- **非同期ジョブ**: `POST /api/jobs` / `GET`・`DELETE /api/jobs/{job_id}` / `GET /api/jobs/{job_id}/events`（SSE）は**実装済み・到達可能です**。`POST /api/generate` が残るのは同期版の互換のためです

### データベースへの書き込み

`/api/generate` と `POST /api/jobs` は、生成の**出入りに `audit_logs` へ行を書きます**。1 回の生成につき **必ず 2 行**です。

- 入場時: `phase: "started"`（`meta` には `path` / `job_id` / `year` のみ。原稿本文・氏名・生年のような個人データは入れません）
- 終端時: `phase` は `completed` / `failed` / `cancel_requested` のいずれか（実際に起きた結果を記録します。`finally` で無条件に `success` を書きません）

生成結果の节目内容そのものは `generations` テーブルには保存されません。開示・削除・保持の運用は [`docs/privacy_and_tenancy.md`](docs/privacy_and_tenancy.md) が正本です。

### 曲カタログの充足状況

1 回の番組で流す曲（既定 3 周 × 1 パス 6 曲 = 18 曲）を
**前回放送と重複させずにまんべんなく回す**仕組みは実装済みです。

**ルール**: 1 年のプールが `retro_radio/core/songs/__init__.py::TARGET_SONGS_PER_YEAR`
（既定 50 曲）あれば、連続する放送どうしで重複ゼロになります。
**実際の曲数と充足状況を語るときに必ず正本を参照してください**（数値をここでハードコードしません）。
正本は `retro_radio/core/songs/songs.json`、進捗は
`python scripts/validate_songs.py --quiet` で確認できます
（`tests/test_song_catalog.py` が同じ検査を pytest 経由で行います）。
曲数が足りない年は 10 年帯へ広げます。
運用手順・設計判断は [`docs/song_catalog.md`](docs/song_catalog.md) を参照。

---

## 🧪 テスト

```bash
pip install -r requirements-dev.txt

python -m pytest                    # 収集対象は pytest.ini の testpaths に従う
python -m pytest -v
python -m pytest --cov=retro_radio --cov-report=term-missing
```

テストの実体は [`tests/`](tests) および `retro_radio/tests/` にあります。**ルート直下にテストスクリプトは置きません。**

---

## 📖 ドキュメント

| ファイル | 内容 |
|---|---|
| [`docs/song_catalog.md`](docs/song_catalog.md) | 曲カタログの正本・選曲ローテーション・音源照合と運用手順 |
| [`docs/facts_registry.md`](docs/facts_registry.md) | 番組レジストリの追加・編集ルール |
| [`docs/privacy_and_tenancy.md`](docs/privacy_and_tenancy.md) | **認証・テナント分離・同意・開示削除・監査ログの正本** |
| [`docs/jobs_and_progress.md`](docs/jobs_and_progress.md) | 非同期ジョブ（`/api/jobs`）と SSE 進捗の契約 |
| [`docs/migrations.md`](docs/migrations.md) | データベースマイグレーション手順 |
| [`docs/migration_rules.md`](docs/migration_rules.md) | マイグレーション作成規約 |
| [`docs/db_backup.md`](docs/db_backup.md) | DB バックアップ手順 |
| [`docs/state_design_system.md`](docs/state_design_system.md) / [`docs/micro_interactions.md`](docs/micro_interactions.md) | フロントエンドのデザインシステム |
| [`docs/premium_features_demo.md`](docs/premium_features_demo.md) | 有料機能の設計メモ |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) / [`OPERATIONS.md`](OPERATIONS.md) | 起動・デプロイ・運用の手順書（現行） |
| [`docs/archive/`](docs/archive) | 過去の計画書と完了報告（現状の仕様書ではありません。旧 `docs/implementation_checklist.md` もここへ移動済み） |
| `http://localhost:8501/docs` | API リファレンス（サーバー起動後） |

---

## ⚖️ ライセンス

**本リポジトリにはライセンスファイル（`LICENSE`）がありません。** 現在のデフォルトは「無断複製・配布・改変・二次利用はすべて権利者の許可が必要」を意味します。利用範囲は権利者にご確認ください。
