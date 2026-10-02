# 実装計画書 — Round 2（2026-10-01）

対象: retro_radio / 方式: レビュー → 計画 → 修正 → 検証 の 3 周繰り返し（Round 2 が 2 周目）

---

## 0. Round 1 終了時のベースライン

| ゲート | 結果 |
|---|---|
| `python -m pytest -q` | **2885 passed**, 0 failed（Round 1 で +84 テスト） |
| `flake8 tests` / `--config .github/flake8-app-baseline.ini retro_radio` / `flake8 eval` | EXIT 0 |
| `python -m eval --offline --threshold 80` | EXIT 0 / 24 of 24 / 平均 100.0 |
| `alembic upgrade head` → `alembic check` | `No new upgrade operations detected.` |
| `validate_songs.py` / `validate_facts.py` | fail 0 / warn のみ |

---

## 1. Round 2 のレビュー方法

領域を 4 つに分割し、サブエージェント 4 本を並行起動。各エージェントに
「調査・報告のみ／ファイル編集・pytest 実行禁止」を強制し、
Round 1 の修正済み項目を明示して**重複報告と副作用の検証**に絞らせた。

| 領域 | 報告 |
|---|---|
| セキュリティ・認証・プライバシー | R2-01〜R2-18（18 項目） |
| コア生成ドメイン | R2-01〜R2-14 + 優先度表（Critical 3、High 5） |
| データ／サービス／ジョブ／課金 | DB-01〜DB-17 / JOB-01〜JOB-08 / MIG-01〜MIG-05 / CFG-02〜03 / CACHE-01 |
| フロントエンド／テスト品質／CI／ドキュメント | R2-01〜R2-20 |

**報告の訂正が 2 回あった**（Round 1 の誤りを指摘された）。
- JOB-04: 「`MemoryError` が `except Exception` をすり抜ける」は**誤り**
  （`MemoryError` は `Exception` のサブクラス。実測で `issubclass` を確認）
- MIG-02: 「`compare_type` が未設定」は**誤り**（alembic の既定は `True`）
- DB-08: 「採番が消費されない」は**潜在**（`_alloc_seqs` の呼び出し元が 0 件）
- R2-13: 「`F-04` / `F-13` / `F-18` / `F-27` は現ツリーで再現しない」
  （Round 1 の報告が古い行番号と存在しない不具合を報告していた）

---

## 2. Round 2 で**修正済み**の項目（3 件）

### R2-01（コア Critical）: 音源ゼロでも `care_recreation` / `anniversary` が曲名を挙げる

**ファイル**: `retro_radio/core/fallback.py`

**実証（修正前に主担当が再現）**

```
normal          -> titles: []          (OK)
care_recreation -> titles: ['京急liste', ' UBC的歌', '虹の橋']   (NG)
anniversary     -> titles: ['花嫁の父', '北国の春', '旅路']    (NG)
```

**原因**: Round 1 は `pinned_songs` と `current_pinned_songs` を直したが、
`select_program_songs` の `if pinned:` 真偽判定が**残っていた**。
空リスト（= 音源ゼロ確定）が「未差し込み（`None`）」と誤解され、
カタログから曲を選び直し直していた。

**修正**:
1. `has_pinned_songs()` を追加（`None` と `[]` の区別を集約）
2. `select_program_songs` を `if has_pinned_songs():` に変更
3. `generate_care_script` / `generate_anniversary_script` の
   `first_song = songs[0]` を `songs[0] if songs else ("", "")` に
   （`IndexError` の防止）

**教訓**: Round 1 は「3 状態」を 3 関数全部で直したつもりだったが、
1 関数だけ残っていた。**同種の判定を 1 箇所に集約**する方針に改めた。

---

### R2-02（コア High）: 音源ゼロ原稿が eval の length 下限を下回る

**ファイル**: `retro_radio/core/fallback.py` の `_generate_no_music_script`

**実証**: `generate_fallback_script(1975, 9, 24, songs=[])` → **737 字**。
eval ゲートの下限（800 字）を **63 字**下回る。
`songs=[]` の eval ケースを追加した瞬間にゲートが赤になる潜在バグ。

**修正**: 専用の原稿に「西湖の待ち合わせ…」の 2 行を追加（737 → **910 字**）。

---

### R2-03（セキュリティ Critical）: 短すぎる `secret_key` で `session` モードが成立する

**ファイル**: `retro_radio/config.py` の `Settings.require_auth_config()`

**実証（修正前）**: `RETRO_RADIO_SECRET_KEY` を 16 文字にすると
`require_auth_config()` は `if self.secret_key:` で判定するため `"session"` を返す。
結果:
- `POST /api/auth/session` は `issue_session_token` の長さ検査で `TokenError` になる。
  `TokenError` は `AppError` の**サブクラスではない**ため
  `server.app_error_handler` を素通りし **500** になる
  （運用者に何が起きているか分からない）。
- 既存の Cookie も `authenticate_request` が `invalid_session` を返し全 API が 401。
  = **ログイン画面すら開けない**。

Round 1 で `tokens.MIN_SECRET_LENGTH` を 1 → 32 に上げた際、
`config.py` の側だけ検査が無く、今回の可用性障害を生んでいた。

**修正**: 判定を 1 か所（`require_auth_config`）に寄せ、
`len(secret_key) < MIN_SECRET_KEY_LENGTH` なら `"unavailable"`（fail-closed）を返す。

**回帰テスト**（`tests/test_auth_wiring.py` に 2 件追加）
- `test_auth_mode_is_unavailable_when_secret_key_is_too_short`
- `test_auth_mode_is_session_when_secret_key_is_long_enough`

**検証**: 修正前 16 文字で `"session"` → 修正後 `"unavailable"`。114 passed。

---

## 3. Round 2 の回帰テスト

`tests/test_song_alignment.py` に 2 関数（parametrize で計 6 ケース）追加。

| テスト | 内容 |
|---|---|
| `test_empty_songs_never_mentions_a_song_title_in_any_mode[3 モード]` | 3 モード全てで正本カタログの曲名を挙げないこと。番組名（NHKラジオ第一）は曲名ではないので含まれない |
| `test_empty_songs_keeps_the_script_structure[3 モード]` | 曲名だけを取り除いており、見出し・年・長さ（≥800 字）は保たれること |

**検証**: 修正前 `normal` 以外 2 モードで失敗 → 修正後 **40 passed**。

**`_catalog_titles()` を遅延評価にした理由**: `load_songs()` を import 時に呼ぶと
曲カタログの読み込みと警告出力が先に発生し、このファイルがテストする
「選曲結果の反映」自体に影響した（実測で 8 件の既存テストが壊れた）。

---

## 4. Round 2 で**意図的に直さない**もの（理由付き）

| ID | 項目 | 理由 |
|---|---|---|
| **R2-02/SEC-02** | `/health` の匿名分岐で `auth_ready` が無い → `app.js:4850` の `=== true` 判定が falsy に落ち、**ログイン枠が出ない** | **Round 3 の最優先 P0**。匿名利用者が「管理者が認証を壊した」と表示され、Cookie 取得経路が存在しない |
| **R2-04/SEC-03** | リクエストボディ無制限（`content-length` 検査が middleware 含むどこにも無い） | DoS。ASGI middleware 追加が必要 |
| **R2-05/SEC-04** | `/api/generate` に per-principal 上限が無い | 実測: 8 並列で `/api/auth/session` が 27.6 秒 |
| **R2-06/SEC-05** | SSE 同時接続数に上限が無い | 実測: 16 接続で executor 枯渇、`/api/generate` が 60 秒タイムアウト |
| **R2-08/SEC-07** | `record_generation` 未配線 + `generations` が削除請求後も残る | **仕様判断が要る**（`docs/privacy_and_tenancy.md:126` は「行を消さない」設計と明記、SEC rone は「消す」方針） |
| **DB-01** | `generations` への書き込み配線 | 上記 R2-08 の仕様判断が先。**単独で入れると削除請求後に原稿本文が開示できる** |
| **DB-02** | Stripe 解約時に `metadata.user_id` が無く降格できない（DB 上で永久プレミアム） | 課金の正しさ。`get_by_stripe_subscription_id` の追加が必要 |
| **MIG-01** | `users.email` に UNIQUE 制約と索引の両方が存在する（2 スキーマ） | `MIG-04`（env.py の pragma）→ `MIG-01` の順で必要 |
| **R2-05/F-08** | `eval/metrics/fact_score.py` の `except Exception: return years, counts` | 報告が**到達性は限定的**と訂正（壊れた `songs.json` は既に CI を赤くする） |
| **Round 1 の 201 上限** | `test_app_baseline_violation_count_never_grows` の fragility | 0 件コードの baseline 追加は合計数が増えないため検出しない |

---

## 5. Round 2 の残作業（優先度順）

```
Wave 0（Round 1 由来の新規バグ・最先）
  SEC-01 後段（短鍵で 500）        … P0
  SEC-02（/health 匿名でログイン枠が出ない）… P0

Wave 1（外部入力で到達可能）
  SEC-03（ボディ無制限）          … DoS
  SEC-04（per-principal 上限）      … 可用性
  SEC-05（SSE 上限）               … 可用性
  SEC-06（プライバシー最小化の未配線）… 要配慮個人情報

Wave 2（内部ネットワーク露出）
  SEC-09（audit meta 無制限 + user_id 偽装）
  SEC-08 / DB-01（generations の配線）… 仕様判断が先
  DB-02 → DB-03 → DB-04（課金）
  JOB-05 / JOB-06 / JOB-08（ジョブ制御）
  CACHE-01（TTS キャッシュ上限）

Wave 3（migration 整合）
  MIG-04 → MIG-01 → MIG-02

Wave 4（決定論）
  H-01（script_generator の random）
  H-02（song_selector の rng）
  H-10（fallback の random）
  R2-05（対象年の曲を先に鳴らす保証の壊れ）
  R2-07（ブレーカーが非連続失敗で開く）

Wave 5（フロントエンド）
  F-05（同意ゲートの拒否がリロードで消える）
  F-06（/api/terms 失敗で空の規約）
  F-07（連打で孤児ジョブ）
  F-12（/health が service worker の cacheFirst）
```

---

## 6. Round 2 の完了条件

| # | 条件 | 結果 |
|---|---|---|
| 1 | `python -m pytest -q` 0 failed | **2898 passed**, 0 failed（Round 1 の 2885 から +13） |
| 2 | lint 3 系統 EXIT 0 | APP=0 / TESTS=0 / EVAL=0 |
| 3 | `python -m eval --offline --threshold 80` EXIT 0 | EVAL=0 / 24 of 24 / 平均 100.0 |
| 4 | 既存 40 件が緑のまま R2-01/R2-02 の修正が入る | **40 passed** |
| 5 | 新回帰テストが修正前に赤くなること | 3 モード中 2 モード・短鍵の 2 ケースで失敗を確認 |
| 6 | `validate_songs.py` / `validate_facts.py` | fail 0 / warn のみ |

---

## 7. Round 2 で**学んだこと**（Round 3 への反映）

### (1) Round 1 の `_cue_song` 方式は既存契約と非互換だった

Round 1 で「トークの**直後に鳴る**曲だけを名ざす」`_cue_song` 方式を導入したが、
`tests/test_song_alignment.py` は「1 曲でも必ず名前を挙げる」「3 曲すべてを挙げる」を
固定していたため **8 件が落ちていた**。この矛盾は Round 1 の時点では
テストを先に通したことに合わせて見落としていた。

Round 2 では **HEAD に戻してから最小差分**で R2-01 を適用し直し、
既存 40 件を全て緑のまま修正を入れた。
（`select_program_songs(1975, 3)` と `select_program_songs(1975, 5)[2:]` の
期待値の違いが露見した。）

**教訓**: 仕様を変えるときは、その仕様の**依拠しているテスト**を全部洗い出す。
1 件だけ通した時点では「壊した」とは気づけない。

### (2) 3 状態の判定は 1 箇所に集約する

`None`（未指定）/ `[]`（音源ゼロ確定）/ 非空 の 3 状態を、3 つの関数が
それぞれ判定していたため、1 箇所だけが直不到位になっていた。
`has_pinned_songs()` を新設して集約した。

### (3) レポートには誤りがある

4 本のレポートで**合計 7 件の事実誤認**が見つかった
（JOB-04 / MIG-02 / DB-08 / DB-10 / JOB-02 / JOB-03 / R2-13 ほか）。
推測を書かず `ファイル:行` と「そのコードをどう読んだか」を根拠として
書かせること、そして**主担当が自分で再現する**ことを必須にした。

### (4) `git stash` を作業途中で使うと情報が失われる

`git stash push` → `git stash pop` の往復で、`_cue_song` / `_cue_line` の定義と
`_script_songs(year, songs, 5)` / `_decade_songs(year, 4)` が混線し、
8 件のテストが壊れた。**編集作業中は `git stash` を使わない**
（必要なら `git diff > file.patch` で退避する）。

---

## 8. 変更履歴

- 2026-10-01: 初版。サブエージェント 4 本のレビューを受けて計画化。
- 2026-10-01: R2-01（3 モードでの音源ゼロ）、R2-02（音源ゼロ原稿の長さ）、
  R2-03（短すぎる secret_key で session モードが成立）を修正。全ゲート緑。