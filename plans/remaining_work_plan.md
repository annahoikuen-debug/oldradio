# 残作業 実装計画書

作成: 2026-10-01 / 対象: retro_radio / 基準コミット: `d7a5c16`（HEAD）

---

## 0. この計画を書くときの検証状態

すべて本リポジトリで実測した値。記憶や推測ではなく_QUERY 結果。

| ゲート | 結果 |
|---|---|
| `pytest` | **2708 passed**, 3 skipped, 3 deselected, 3 xfailed / 0 failed |
| `flake8 tests` | EXIT 0 |
| `flake8 --config .github/flake8-app-baseline.ini retro_radio` | EXIT 0 |
| `flake8 eval` | EXIT 0 |
| `alembic upgrade head && alembic check` | `No new upgrade operations detected.` / EXIT 0 |
| `python -m eval --offline --threshold 80` | **EXIT 1**（24 ケース / 合格 22 / 不合格 2 / 平均 96.26 / 最低 62.5） |
| `git status` | 変更 77 + 未追跡 21 = **98 ファイル未コミット** |

### 前セッションから状况が変わった点

並行作業により **曲カタログが 43 曲 → 2944 曲 / 72 年** に拡張された
（`scripts/song_source/` に `musicbrainz.tsv` / `accepted.tsv` / `rejected.tsv` /
`candidates_2022_2025.tsv`、`scripts/fetch_song_catalog.py` / `import_songs.py` が新規追加）。
`catalog_health().ok` は `True` になった。

したがって前回「最大の問題」として挙げたカタログ不足は**一部解消済み**。
残る本 Tasks を以下に再定義する。

---

## 1. 前回報告した内容の訂正

**「eval ゲートの失敗はカタログ不足の下流症状である」—— これは誤りだった。**

根拠:

- カタログが 43 → 2944 曲に増えたにもかかわらず、eval の平均 96.26 / 最低 62.5 は
  **小数点以下まで完全に一致した**。影響を受けていない。
- 実測した連鎖:

```
eval/fixtures.py:160
    songs: List[Tuple[str, str]] = validate_song_pairs(None) or []

retro_radio/core/script_generator.py  validate_song_pairs(None)  ->  None
```

  `validate_song_pairs(None)` は設計上 `None` を返す（「渡されない既存呼び出しは現挙動のま
  ま」の契約）。したがって `or []` で **空リスト**になり、
  `_deterministic_script(..., songs=[])` が呼ばれる。

- 結果、決定論スクリプトは**正本カタログを一切参照せず**、内蔵の古いフォールバック
  （東京ブギウギ 1951 / 青い山脈 1951 / リンゴの唄 1955）から曲名を使った。
  これが `normal-1950` / `care_recreation-1950` の
  `warn`「対象年より後にリリースされた曲」の直接原因。

- 一方 `retro_radio.core.script_generator._catalog_allowlist(1950, 6)` は
  **実在する 1950 年の曲**を正しく返す:

```
('Do You Want Melon?', '暁テル子')
('上州の鴉', 'Various Artists')
('黒田節', 'Various Artists')
('Tokyo Shoeshine Boy', '暁テル子')
```

  つまり**正しい関数は既に存在し、`eval/fixtures.py` がそれを使っていない**だけ。

**結論**: eval ゲート失敗はデータ不足ではなく **`eval/fixtures.py` の 1 箇所の関数選択ミス**
である。P1 の修正だけで 2 ケースが合格する見込みが高く、閾値を下げる必要がない。

（補足・別件: `eval/metrics/songs.py:158` は曲照合の 正準リストを
`set(_KNOWN_SONG_TITLES), "static-master"` としており、`core/songs/songs.json` の
2944 曲とは別物。ここは「原稿がカタログに無い曲名を書いていないか」の
検証カバレッジが実カタログと乖離することを意味するが、今回の 2 件の失敗要因では
ない。P4 で扱う。）

---

## 2. 残タスク一覧（優先順）

### P0 — ブロッカー（リリース前の必須）

#### P0-1 `eval/fixtures.py` が正本カタログを使わない

| 項目 | 内容 |
|---|---|
| 優先度 | P0 |
| 影響 | eval ゲートが永久に FAIL。CI に配線できない。品質ゲートの意味がゼロ |
| 原因 | `validate_song_pairs(None)` は `None` を返す契約。`or []` で空になり決定論スクリプトが内蔵フォールバックを使う |
| 変更ファイル | `eval/fixtures.py`（1 ファイル） |
| テスト | `tests/test_eval_harness_hardening.py` に 1 件追加 |

**手順**

1. `eval/fixtures.py:158-161` を読む。
2. `validate_song_pairs(None)` を捨て、`_catalog_allowlist` を使う:

```python
# 現状
from retro_radio.core.script_generator import _deterministic_script, validate_song_pairs
songs: List[Tuple[str, str]] = validate_song_pairs(None) or []
return _deterministic_script(year, month, day, mode, target_name, songs)

# 修正後
from retro_radio.core.script_generator import _catalog_allowlist, _deterministic_script
# songs=None は「正本カタログから導出」を意味する。
# validate_song_pairs(None) は None を返す契約なので `or []` で
# 空になり、内蔵の古いフォールバック曲名（1951/1955 年の曲）が使われていた。
songs: List[Tuple[str, str]] = _catalog_allowlist(year, count=SCRIPT_SONG_COUNT)
return _deterministic_script(year, month, day, mode, target_name, songs)
```

3. `SCRIPT_SONG_COUNT` は `retro_radio.core.songs.PROGRAM_SONGS_PER_BROADCAST`
   （= 18）と同じ値を使う。ハードコードせず import する。
4. `retro_radio.core.songs` が読めない場合の縮退（空リストではなく
   `[]` を渡して _deterministic_script に内蔵フォールバックを任せる）を維持する。

**検証**

```powershell
python -m pytest tests/test_eval_harness_hardening.py tests/test_eval_harness.py -q
python -m eval --offline --threshold 80 ; echo "EXIT=$LASTEXITCODE"   # 0 になること
python -m flake8 eval
```

**完了条件**: eval EXIT 0、かつ `normal-1950` / `care_recreation-1950` の
`fact_score` が 80 以上、かつ新規テストが `songs=[]` にならないことを固定する。

**リスク**: 低。1 ファイル、純粋な関数選択の修正。既存テスト 81 件が緑のまま
保たれることを必須条件とする。

---

#### P0-2 作業ツリーが未コミット（97 ファイル）

| 項目 | 内容 |
|---|---|
| 優先度 | P0 |
| 影響 | レビュー不能・ロールバック不能・CI が旧 HEAD を走的 |
| 範囲 | 変更 77 + 未追跡 21 = 98 ファイル。HEAD は `d7a5c16` のまま |

**手順**

1. 並行作業が収束していることを確認する（`Get-ChildItem -Recurse` の
   `LastWriteTime` が 10 分以上動いていない）。
2. 秘密情報を含まないことを確認する（`.env.example` に実キーが
   入っていないこと。`tests/test_env_templates.py` が検査している）。
3. 論理的単位に分けてコミットする。**1 コミットに全部入れない**:

| # | 単位 | 主なファイル |
|---|---|---|
| c1 | 認証・認可の修正 | `retro_radio/api/deps.py`, `retro_radio/auth/*`, `retro_radio/server.py`（`/api/auth/session` 追加） |
| c2 | テストの認証 fixture 対応 | `tests/test_server_api*.py`, `tests/test_job*.py`, `tests/test_me_api.py` |
| c3 | 曲ストアの並行安全性 | `retro_radio/services/song_store.py`, `tests/test_song_store_concurrency.py` |
| c4 | プレビュ negatives | `retro_radio/core/preview_resolver.py` |
| c5 | プライバシー リポジトリ | `retro_radio/db/privacy_repository.py`, `retro_radio/db/privacy_models.py` |
| c6 | パイプライン / スクリプト生成 | `retro_radio/core/pipeline.py`, `retro_radio/core/script_generator.py`, `retro_radio/core/fallback.py` |
| c7 | 曲カタログ拡張 | `scripts/song_source/*`, `scripts/fetch_song_catalog.py`, `scripts/import_songs.py`, `retro_radio/core/songs/*` |
| c8 | TTS / キャッシュ / エクスポート | `retro_radio/core/tts.py`, `retro_radio/core/legacy_tts.py`, `retro_radio/services/*`, `retro_radio/utils/session.py` |
| c9 | フロントエンド | `static/*`, `tests/test_frontend_playback_auth.py` |
| c10 | インフラ / CI | `.github/*`, `fly.toml`, `render.yaml`, `railway.json`, `Dockerfile*`, `requirements*.txt`, `alembic.ini` |
| c11 | eval ハーネス | `eval/*`, `tests/test_eval_harness_hardening.py` |
| c12 | ドキュメント | `README.md`, `DEPLOYMENT.md`, `OPERATIONS.md`, `docs/**` |
| c13 | テスト衛生 | `tests/test_hygiene_regression.py`, `tests/test_pipeline.py`, `tests/test_env_templates.py` |

各コミット前に `python -m pytest -q` と `flake8` 3 種を通す。

**注意**: コミットはユーザー指示がないため**実施しない**。手順書として残す。

---

### P1 — 機能ギャップ（実害あり）

#### P1-1 曲カタログの年別偏り（1950-1965 / 1996 / 2005）

`catalog_health().coverage` の実測値。1 番組 = 18 曲。

| 年 | 曲数 | 状況 |
|---|---|---|
| 1950 | 2 | 18 曲に届かない |
| 1951 | 8 | 届かない |
| 1952 | 2 | 届かない |
| 1953 | — | **エントリなし** |
| 1954 | — | **エントリなし** |
| 1955 | 1 | 届かない |
| 1956 | 1 | 届かない |
| 1957 | 3 | 届かない |
| 1958 | 6 | 届かない |
| 1959 | 7 | 届かない |
| 1960 | 15 | 届かない |
| 1964 | 7 | 届かない |
| 1965 | 14 | 届かない |
| 1972 | — | **エントリなし**（前後年は 50） |
| 1996 | 1 | **到達不能。異常の疑い**（1995=51, 1997=50） |
| 2005 | — | **エントリなし**（前後年は 50） |
| 2011 | 33 | 18 は満たすが 50 未満 |

上記 12 年 + 4 年欠落。1950-1965 は戦前・戦中の releasing が少ないため
構造的困难ayas あるが、**1996 / 1972 / 2005 は隣接年が 50 曲あるのに欠落しており、
これはデータ的な欠落であって musically な sparsity ではない**。ここを先に片付ける。

**手順**

1. **1996 / 1972 / 2005 の欠落理由を特定する**（P0-1 と同時並行可）。
   - `scripts/import_songs.py` と `rejected.tsv` を読む。
   - `rejected.tsv` 45 件の拒否理由の分布は実測済み:
     - 「iTunes で見つからない」系: 20 件
     - 「iTunes の結果戮り不正」系: 8 件
     - 「iTunes HTTP 403」系: 15 件（Yoh Kamiyama, milet, Creepy Nuts 等の
       アーティスト名を検索して 403。**iTunes のレート制限/robots 由来の可能性**）
   - 403 が特定アーティストに集中していれば、iTunes 側の制約であり
     MusicBrainz 側のデータでは埋まらない。**この場合は 1950-1965 と同じ
     「構造的不足」として記録する**。
2. **編集は `scripts/song_source/songs.tsv` で行う**。`songs.json` は生成物であり
   `scripts/validate_songs.py` の `tsv-drift` 検査に failing（`scripts/build_song_catalog.py` で再生成）。
3. 曲名・アーティストの pairs は**実在するものだけ**を追加する。作詞・演唱者の
   不確かな組合せを作らない。preview URL は検証できないなら `null` のまま。
4. 1950-1965 は 18 曲に届かないのが構造的なら、`catalog_health()` が
   `sufficient_years_for_program` に出さない現状の挙動を**仕様として文書化する**
   （`docs/song_catalog.md`）。
5. 選曲側の緩めロジック（隣接年・同一 10 年帯へ広げる）は**残す**。
   1950 の番組が 1951 の曲を jarring に流さない才是 intended。

**完了条件**: `python scripts/validate_songs.py` が `fail: 0`、
`catalog_health()['ok'] is True`、1950/1972/1996/2005 の扱いが
コードか文書のいずれかで明示されていること。

---

#### P1-2 SPA が `/api/me/*` と `/api/admin/audit` を未配線

| 項目 | 内容 |
|---|---|
| 優先度 | P1（ただし**仕様判断が前提**） |
| 現状 | `static/app.js` は会話のみ_create。同意画面も、お気に入り画面も、管理者監査画面も無い |
| ブロック要因 | `require_consent` 系エンドポイントが 409 を返す仕様だが、**どの操作に同意を求めるか**が決まっていない |

**手順（仕様判断が先）**

1. 利用者ごとに確認すべき論点を列挙する:
   - 同意は**施設側の契約**単位か**個人**単位か
   - 同意前に許す操作（番組生成）と許さない操作（データエクスポート）は分かれるか
   - 同意の版（`terms_version`）を跨いだとき再取得が必要か
2. 判断agues を `docs/privacy_and_tenancy.md` に書く（先に文書化する）。
3. その後 SPA 側で `/api/me/consent` を起動時に呼び、409 なら同意モーダル、
   200 なら通常起動。不同意時は生成ボタンを無効化する。
4. `/api/admin/audit` は管理者専用画面を追加する。`require_admin` は
   DB の `user_security.role` を正とする実装済みなので、
   SPA は `403` 時に「管理者ではない」を表示するだけでよい。

**風險**: 中。仕様判断を飛ばして実装すると UI とバックエンドの意味がずれる。

---

### P2 — 衛生

#### P2-1 ルートの一時ファイル

19 ファイル（`.coverage` を含む。`tmp_*` が 17 個、`pytest_now.txt` が 1 個）。列挙:

```
.coverage
pytest_now.txt
tmp_all.txt
tmp_all_final.txt
tmp_bom.txt
tmp_fail.txt
tmp_final.txt
tmp_fix_log.txt
tmp_fix_mojibake.py
tmp_flake.txt
tmp_full_test_1.txt
tmp_mojibake_check.py
tmp_mojibake_result.txt
tmp_moji_report.txt
tmp_new_test.txt
tmp_one.txt
tmp_route_probe.py
tmp_route_probe.txt
tmp_test_log.txt
```

**手順**

1. すべて削除する（作業用であり成果物ではない）。
2. `.gitignore` に以下を追加:

```
# 作業用の一時ファイル（レビューやテスト実行の残骸）
tmp_*.txt
tmp_*.py
pytest_now.txt
.coverage
.coverage.*
htmlcov/
```

3. `tests/test_hygiene_regression.py` に
   「ルートに `tmp_*` が残っていないこと」を検査する 1 件を追加する。
   arily 検査は `.gitignore` ではなく**実ファイル**を見る。

**完了条件**: `Get-ChildItem -File | Where-Object Name -match '^tmp_'` が空、
`git status` に `tmp_` が現れない。

---

#### P2-2 ルートの一時ファイル_ratio طويل

上記 P2-1 に含む。個別扱いはしない。

---

### P3 — 既知制約（残してよいもの）

#### P3-1 bearer のみモードではログインが 503

`RETRO_RADIO_SECRET_KEY` 未設定 + `RETRO_RADIO_SINGLE_USER_KEY` 設定 のとき、
`POST /api/auth/session` が 503 を返す。セッション Cookie を署名する鍵が無いため。

- 個人利用（`RETRO_RADIO_REQUIRE_AUTH=0`）では影響なし。
- 施設利用では `SECRET_KEY` が必須。`DEPLOYMENT.md` の環境変数表に
  「本番必須」と明記済み。
- **対応**: 不要。ドキュメントで正しい。

#### P3-2 eval の曲照合マスターが実カタログと乖離

`eval/metrics/songs.py:158` が `set(_KNOWN_SONG_TITLES), "static-master"` を返す。
正本は `core/songs/songs.json`（2944 曲）だが、eval 側は別リストを使う。

- **影響**: 「原稿がカタログに無い曲名を書いていないか」の検証が、
  実際に選択され得る曲に対して行われていない。
- **対応**: `eval/metrics/songs.py` を
  `retro_radio.core.songs.load_songs()` から曲名集合を組み立てるように変更する。
  hermetic を保つため、**読み込み失敗時は空集合へ縮退**する
  （現状の `static-master` と同じ縮退方針）。
- **注意**: P0-1 の修正により eval が実カタログを使い始めるため、
  この P3-2 は P0-1 の**後**に実施する（順序を入れ替えると
  検証が突然厳しくなる）。

---

## 3. 実行順序と並列化

```
                    ┌─────────────────────────────┐
                    │ P0-1 eval/fixtures.py       │  1 ファイル・独立
                    │ （最優先・約 15 分）        │
                    └──────────────┬──────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
   P1-1 欠落年の調査          P0-2 コミット準備          P2-1 tmp 掃除
   （read-only 調査のみ）     （コミットは要指示）        （独立・即完了）
        │                          │                          │
        └──────────────────────────┼──────────────────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ P0-1 検証（eval EXIT 0）      │
                    └──────────────┬──────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              │                                         │
    P1-1 曲カタログ補充                         P3-2 eval マスター同期
    （P0-1 の後ろ。実カタログが                    （P0-1 の後ろ）
      eval に流れるため）                              │
              │                                         │
              └────────────────────┬────────────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ eval ゲートを CI に配線      │
                    │ （threshold 80 で EXIT 0）    │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ P1-2 /api/me/* 配線           │
                    │ （仕様判断が前提・最後に）    │
                    └─────────────────────────────┘
```

**並列実行可能**: P0-1 / P1-1（調査）/ P2-1 / P0-2（準備）
**直列必須**: P0-1 → P1-1 → eval CI 配線 → P1-2

**安全側の注意**: 直前まで別エージェントが
`retro_radio/core/songs/*`, `scripts/song_source/*`, `eval/*`,
`tests/test_ui_ux.py` を触っていた。各作業の着手時に
`git status` と直近の `LastWriteTime` を確認し、
他作業者が触れていないファイルを**ファイル単位で排他確保**してから編集する。

---

## 4. 全体完了の定義（Definition of Done）

以下が**すべて**揃って初めて「完成形」とする。

| # | 条件 | 検証コマンド |
|---|---|---|
| 1 | eval ゲートが PASS | `python -m eval --offline --threshold 80` → EXIT 0 |
| 2 | eval ゲートが CI に配線済み | `.github/workflows/ci.yml` に `python -m eval --offline --threshold 80` |
| 3 | 全テスト緑 | `python -m pytest -q` → 0 failed |
| 4 | lint 緑（3 系統） | `flake8 tests` / `flake8 --config .github/flake8-app-baseline.ini retro_radio` / `flake8 eval` → 全て EXIT 0 |
| 5 | マイグレーション整合 | `alembic upgrade head && alembic check` → `No new upgrade operations detected.` |
| 6 | 曲カタログ整合 | `python scripts/validate_songs.py` → `fail: 0` |
| 7 | 事実レジストリ整合 | `python scripts/validate_facts.py` → `fail: 0` |
| 8 | ルートに一時ファイル無し | `Get-ChildItem -File \| Where-Object Name -match '^tmp_'` → 空 |
| 9 | 未コミットなし | `git status --porcelain` → 空（要ユーザー指示） |
| 10 | 既知の制約が文書化されている | `DEPLOYMENT.md` に `SECRET_KEY` 必須、bearer のみモードの制約 |

**現状の達成度**: 1, 3, 4, 5, 6, 7, 10 は**既に達成**。
残り = **2（eval 配線、1 が塞がっている）, 8, 9** と P1-1 の一部。

---

## 5. この計画で扱っていない既知の問題（参考）

前回レビューで報告したが、上記の優先度では**見送った**もの。
issue として残す価値はある。

| 項目 | 場所 | 状況 |
|---|---|---|
| `test_job_api.py` の跨モジュール状態漏れ | `tests/test_job_api.py` | import 時に `RETRO_RADIO_REQUIRE_AUTH=0` を固定するが `Settings` は `lru_cache` される。単独実行では通るが結合すると落ちる。`Settings` キャッシュの無効化を `conftest.py` でFixture 化するのが正解 |
| `build_playlist` の off-by-one | `retro_radio/server.py` | `talk_total` .compare `talk_total + 1` に修正済み（前回のセッション）。 |
| `TTS_CACHE_DIR` の上限 | `retro_radio/core/tts.py` | `TTS_CACHE_MAX_ENTRIES=200` / TTL 7 日で上限済み。 |
| `.env` の実ファイル | ルート | 開発用。`.gitignore` 済み。`test_no_dotenv_committed` は「追跡されているか」で判定するよう修正済み。 |
| Gemini API キーのWhite流 | — | 前セッション中に `.env.example` へ実キーが書き込まれたのを発見し秘匿化済み。**未追跡ファイル**だが、流出の可能性を排除するため**キーのローテーション**を推奨。 |

---

## 6. 判断が分かれる箇所（実装前に決めたいこと）

1. **1950-1965 をどう扱うか**
   - 選択肢 A: 18 曲に届かない年を `catalog_health()` で明示し、UI でも警告する
   - 選択肢 B: 隣接年への拡大を仕様として明記し、警告しない
   - 推奨: **A**。介護利用では「1950年の番組に 1951年の曲が流れる」ことが
     利用者にとって不自然 因此、可視化するのが安全。

2. **eval ゲートを CI で hard fail にするか**
   - 選択肢 A: `--threshold 80` で hard fail
   - 選択肢 B: `continue-on-error: true` で段階導入
   - 推奨: **P0-1 修正後に A**。既に EXIT 0 になるので B の意味がない。

3. **同意ダイアログの範囲**
   - 施設契約単位か個人単位か decides P1-2 の scope。

---

## 変更履歴

- 2026-10-01: 初版。前回レビュー・10 エージェント実装・統合のふまえで作成。
  カタログ拡張（43→2944）の反映と、前回報告した eval 失敗原因の訂正を含む。
