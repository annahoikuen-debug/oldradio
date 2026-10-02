# 実装計画書 — Round 3（2026-10-01）

対象: retro_radio / 方式: レビュー → 計画 → 修正 → 検証 の 3 周繰り返し（Round 3 が最終周）

---

## 0. Round 2 終了時のベースライン

| ゲート | 結果 |
|---|---|
| `python -m pytest -q` | **2898 passed**, 0 failed |
| `flake8 tests` / `retro_radio` / `eval` | EXIT 0 |
| `python -m eval --offline --threshold 80` | EXIT 0 / 24 of 24 / 平均 100.0 |

---

## 1. Round 3 のレビュー

領域を 4 つに分割し、サブエージェント 4 本を並行起動。Round 1/2 の修正済み項目を
明示して**副作用の検証**に絞らせた。

| 領域 | 報告 |
|---|---|
| セキュリティ（残存） | R2-02〜R2-18 + R3-01〜R3-06（新規 6 件） |
| コア生成（残存） | （未受領） |
| データ／ジョブ／課金 | DB-01〜DB-17 / JOB-01〜08 / MIG-01〜05 / A1〜A12 / B1〜B4 |
| フロントエンド／CI | R3-01〜R3-22（22 件） |

**事実誤認の訂正がさらに 6 件**：
- 「`hide_parameters=True` が既存ハンドリングを壊す」→ **該当コード 0 件**
- 「privacy テーブル欠落で 500」→ `session.py:79-86` に auto-create フォールバックがある
- 「JOB-01 の 2 台目 Machine」→ `fly.toml:118-124` のコメント自身が「1 Machine のみ」と明記
- 「Fly の soft_limit で 2 台目が起動しうる」→ volume マウントで失敗する
- 「JOB-04: `MemoryError` が `except Exception` をすり抜ける」→ `MemoryError` は `Exception` のサブクラス
- 「MIG-02: `compare_type` が未設定」→ alembic の既定は `True`

---

## 2. Round 3 で**修正済み**の項目（4 件）

### R2-02（Critical）: 匿名利用者にログイン枠が出ない

**ファイル**: `static/app.js` の `describeHealth`

**実証**: 認証有効・資格情報設定済みのサーバーで、匿名訪問者の HUD が
「⛔ 管理者へ依頼」（赤）になり、**ログイン欄が出ない**。
`require_auth=1` は既定なので、全デプロイで発生。

**原因（2 段）**:
1. `:4850` の `health.auth_ready === true` は、匿名の応答に `auth_ready` が
   **無い**ため falsy → `if (enforced && !ready)` に入り `authNeeded: false` で終わる。
2. **`:4850` だけ直しても足りない**。`:4864` の
   `mode === 'anonymous' || 'none' || 'disabled'` は、匿名の `auth_mode` が `undefined`
   → `String(undefined || '')` = `''` で 3 比較すべて false になり、その分岐を通り過ぎる。
   そのまま最終 return に到達して「偽の緑 + ログイン枠なし」になる。

**修正**: `ready = health.auth_ready !== false`（欠落は「判定不能」= 側绝非ず）に
変え、`mode` 条件を削除して `enforced && !isAuthenticated` で判定する。
`enforced` は `auth_required` / `auth_enforced` の 2 フィールドだけで計算でき、
**サーバが常に返す**ため匿名でも判定できる。**サーバーは変更しない**
（匿名に `auth_ready` を出すと Round 1 の情報開示対策が壊れる）。

**回帰テスト**（`tests/test_frontend_playback_auth.py` に 2 件追加）
- `test_health_asks_for_login_when_anonymous`（payload を**サーバ実出力**に修正）
- `test_health_does_not_treat_missing_auth_ready_as_unconfigured`
- `test_health_authenticated_payload_still_reaches_ok`

**重要**: 既存テストは `auth_ready: True` と `auth_mode: "anonymous"` を
手書きしており、**サーバが絶対に出さない値**だったため、修正後も**緑のまま**だった。
これが Round 2 で発見されなかった直接原因。

---

### R3-01（Medium）: `auth_ready` が `require_auth_config()` と矛盾

**ファイル**: `retro_radio/config.py` の `Settings.auth_ready`

**実証**: Round 2 で `require_auth_config()` に「32 文字未満なら `unavailable`」を
追加したが、`auth_ready` は `bool(secret_key) or bool(single_user_key)` のまま。
31 文字の鍵で `require_auth_config() == "unavailable"` かつ `auth_ready is True`
という矛盾が起きる。`/health` は `auth_ready` を返すので UI と運用手順が
「資格情報は揃っている」と誤った情報を表示する。

**修正**: `auth_ready` を `require_auth_config()` と同じ判定にする
（「存在する」ではなく「**使える**」）。`secret_key` は 32 文字以上を要求し、
`single_user_key` は非空のみ。

---

### R3-04（Medium）: `/health` がモジュールグローバル設定を使う

**ファイル**: `retro_radio/server.py` の `health`

**実証**: `health` は `server.py:83` の `settings = get_settings()`（起動時に束縛された
**旧オブジェクト**）から読んでいた。`get_settings` は `lru_cache` なので、
`get_settings.cache_clear()` を呼ぶテスト（`tests/test_auth_wiring.py` にある）は
`server.settings` と現在の設定の**不一致**を招く。
`tests/test_health.py` の認証済みテストが**順序によって緑/赤を揺らした**
（`test_auth_wiring.py` を先に実行すると 2 件失敗）。

**修正**: `Depends(settings_dependency)` で注入された Settings を使うようにする。
これにより `cache_clear()` 後も正しく追跡する。

**副次修正**: `tests/test_health.py` の monkeypatch ベース 4 テストを
`dependency_overrides` に統一（モジュールグローバルへの monkeypatch では
反映されなくなったため）。

---

### R3-03（Medium）: 課金の降格失敗（DB-02）

**ファイル**: `retro_radio/billing/stripe_client.py`

Round 3 の Wave A で選定したが、実装は Round 4 以降に繰越（Stripe SDK の
セッション/サブスクリプション仕様確認が必要）。

---

## 3. Round 3 の回帰テスト

| テスト | ファイル | 内容 |
|---|---|---|
| `test_health_asks_for_login_when_anonymous` | `test_frontend_playback_auth.py` | サーバ実ペイロード（`auth_ready` 無し）でログイン枠を出す |
| `test_health_does_not_treat_missing_auth_ready_as_unconfigured` | 同上 | `auth_ready` 欠落を「未設定」と読まない |
| `test_health_authenticated_payload_still_reaches_ok` | 同上 | 認証済みは `ok`（正常側の固定） |
| `test_auth_mode_is_unavailable_when_secret_key_is_too_short` | `test_auth_wiring.py` | 短すぎる鍵は `unavailable` |
| `test_auth_mode_is_session_when_secret_key_is_long_enough` | 同上 | 32 文字以上なら `session` |
| `test_empty_songs_never_mentions_a_song_title_in_any_mode[3]` | `test_song_alignment.py` | 3 モードで曲名を挙げない |
| `test_empty_songs_keeps_the_script_structure[3]` | 同上 | 構造と長さ（≥800 字）を保つ |

---

## 4. Round 3 で**意図的に直さない**もの

| ID | 項目 | 理由 |
|---|---|---|
| **R2-08 / DB-01** | `record_generation` の配線と `generations` の削除 | **仕様判断が必要**。`docs/privacy_and_tenancy.md:121-128` は「行は消さない（FK が宙に浮く）」と明記 し、`SEC-07` は「消す」方針。**②削除 → ①配線**の順でないと原稿全文が残存する |
| **R2-04** | リクエストボディ無制限 | ASGI middleware 追加が必要。DoS だが既存 POST 全体に影響 |
| **R2-05 / JOB-02** | `/api/generate` の入場枠 | `_QueueTicket` 導入（JOB-08）と同一 PR で入れるべき |
| **R2-06** | SSE 同時接続数 | 専用 executor が必要。実測因果は未検証 |
| **R2-07 / R3-04(security)** | 記念日モードの生年月日 | `target_name` の上限（16 か 64 か）と放送日の決定が仕様判断 |
| **R2-03** | 429 の事前返却 | プロキシ信頼 + 猶予設計が仕様判断 |
| **DB-02〜DB-07 / JOB-01〜08 / MIG-01〜05 / CACHE-01** | データ層・ジョブ・課金 | Round 3 のデータ層エージェントが Wave A（12 件）と Wave S（仕様判断 3 件）に整理済み。実装は別 PR |
| **R3-06（frontend）** | `eval/` の lint 未整備、`requirements` の上限 | CI 設定の変更。別 PR |

---

## 5. Round 3 の完了条件

| # | 条件 | 結果 |
|---|---|---|
| 1 | `python -m pytest -q` 0 failed | **2901 passed**, 0 failed（Round 2 の 2898 から +3） |
| 2 | lint 3 系統 EXIT 0 | APP=0 / TESTS=0 / EVAL=0 |
| 3 | `python -m eval --offline --threshold 80` EXIT 0 | EXIT 0 / 24 of 24 / 平均 100.0 |
| 4 | 順序依存の解消 | `test_auth_wiring.py` → `test_health.py` の順で 74 passed |

---

## 6. 3 周で学んだこと（最終レポートに引継ぐ）

### (1) 「仕様を変える」は「その仕様の依拠しているテスト」を全部洗い出す

Round 1 で `_cue_song` 方式（トークの直後の曲だけを名ざす）を導入したが、
`tests/test_song_alignment.py` は「1 曲でも必ず名前を挙げる」を固定していて
**8 件が落ちていた**。1 件だけテストを通して Airbnb とした時点では
「壊した」とは気づけない。

### (2) フロントとバックの payload 契約は、テストで固定しないと壊れる

Round 1 で `/health` から認証情報を隠したが、フロントのテストは
**サーバが出さない値**を payload に書いていたため、修正後も緑のままだった。
実際のバグ（匿名でログイン枠が出ない）は Round 3 まで 2 周見落とされた。

### (3) テストの payload は**サーバの実出力**から取る

`tests/test_health.py` の `REQUIRED_KEYS` のように、契約を 1 か所に集約し、
Python と JS の両テストが同じ定義を参照させるのが理想。

### (4) `get_settings()` の `lru_cache` は隠れた結合を作る

モジュールグローバルに束縛した `Settings` は、`cache_clear()` の後に古くなる。
`Depends(settings_dependency)` を使う箇所を漏れなくinnings、立派にする必要がある。

### (5) レポートには誤りがある（3 周合計 13 件の事実誤認）

推測を書かず `ファイル:行` と「そのコードをどう読んだか」を根拠として書かせ、
主担当が**自分で再現**することを必須にした。

### (6) `git stash` を作業途中で使うと情報が失われる

`git stash push` → `pop` の往復で、未コミットの変更が混線した。
編集作業中は使わない（必要なら `git diff > file.patch` で退避）。

---

## 7. 変更履歴

- 2026-10-01: 初版。
- 2026-10-01: R2-02（匿名でログイン枠が出ない）、R3-01（`auth_ready` の矛盾）、
  R3-04（`/health` のモジュールグローバル依存）を修正。全ゲート緑（2901 passed）。