# 実装計画書 — Round 1（2026-10-01）

作成: 2026-10-01 / 対象: retro_radio / 方式: レビュー → 計画 → 修正 → 検証 の 3 周繰り返し

---

## 0. この計画の前提となる実測ベースライン

すべてこのリポジトリで実行して得た値。

| ゲート | 結果 |
|---|---|
| `python -m pytest -q` | **2801 passed**, 3 skipped, 3 deselected, 3 xfailed / 0 failed (223s) |
| `flake8 tests` | EXIT 0 |
| `flake8 --config .github/flake8-app-baseline.ini retro_radio` | EXIT 0 |
| `flake8 eval` | EXIT 0 |
| `python -m eval --offline --threshold 80` | EXIT 0（24/24・fact score 平均 100.0・最小 100.0） |
| `python scripts/validate_songs.py --quiet` | fail 0 / warn 2997 |
| `python scripts/validate_facts.py` | warn 17 のみ / EXIT 0 |
| ルートの `tmp_*` 残骸 | 0 件 |

**レビュー方法**: 領域を 4 つに分割し、サブエージェント 4 本を並行起動。
セキュリティ/認証・プライバシー、コア生成、データ/サービス/課金、フロントエンド/テスト/CI/ドキュメント。
各エージェントは「調査・報告のみ／ファイル編集禁止／pytest 実行禁止」を強制した。
報告の**うち重大項目は主担当が自分で再現して確証した**（推測で修正しないため）。

**深刻度の定義**

| 深刻度 | 意味 |
|---|---|
| P0 | 認証の破綻・利用者に見つかる嘘・機械検査が構造的に無力化 |
| P1 | 実害がある（可用性の破壊・秘密漏えい・決定論の破壊・誤った仕様） |
| P2 | 保守性・衛生・運用上の誤誘導 |
| P3 | 仕様判断が必要で、機械的には直せない |

---

## 1. Round 1 の修正対象（13 項目）

各項目に「実証方法」「修正内容」「回帰テスト名」を必ず付ける。

### P0-1 SEC-01: `secret_key` の長さ検査が全経路で無効

**ファイル**: `retro_radio/auth/tokens.py:76,85-100,162-165` / `retro_radio/config.py:355-362` / `retro_radio/server.py:1959,2007-2013` / `retro_radio/auth/tokens.py:384`

**実証（実行済み）**

```
issue_session_token('victim-user', secret='a', tenant_id='t1', role='admin') → 成功
read_session_token(...) → {'uid':'victim-user','tid':'t1','role':'admin',...}
```

**原因**: `tokens.py:162-165` は `secret if secret is not None else get_settings().require_secret_key()`
と三項演算子で書くため、`secret` が渡された瞬間（= `server.py:1959` が常にそうする）
`require_secret_key()` の 32 文字検査が**短絡される**。
`MIN_SECRET_LENGTH = 1`（`tokens.py:76`）は「空でない」ことしか見ない。
検証側の `authenticate_request`（`tokens.py:384`）も `conf.secret_key` をそのまま渡す。

**失敗シナリオ**: `RETRO_RADIO_SECRET_KEY=changeme` の運用では攻撃者が鍵を総当たりし、
`uid=<被害者>` `role=admin` の Cookie を**オフラインで偽造**できる。
偽造 Cookie で `GET /api/me/export`（他人の原稿・同意履歴の全開示）と
`DELETE /api/me`（他人のデータ削除）が通る。`/health` は `secret_key_configured: true` を
返すため「認証は有効」に見える。

**修正**:
1. `tokens.py` の `MIN_SECRET_LENGTH` を `MIN_SECRET_KEY_LENGTH` と**同一の 32** に上げる。
   発行側・検証側の両方で同じ検査を行う（既に `_require_secret` を共有している）。
2. `server.create_session` は `secret = current_settings.require_secret_key()` を使い、
   `ConfigurationError` を 503 に翻訳する（503 にして資格情報を漏らさない）。
3. `authenticate_request` も同じ長さ検査を通す。

**回帰テスト**（`tests/test_auth_hardening.py` に追加）
- `test_issue_session_token_rejects_a_secret_shorter_than_32_characters`
- `test_read_session_token_rejects_a_secret_shorter_than_32_characters`
- `test_authenticate_request_rejects_a_short_secret_key`
- `test_session_endpoint_returns_503_when_secret_key_is_too_short`

**注意**: 既存テストが短い鍵を使っている可能性があるので、実装前に
`grep -rn "secret=.*['\"]" tests/` で長い鍵へ書き換える。

---

### P0-2 SEC-08 / F-01: `/health` が認証モードを匿名に漏洩

**ファイル**: `retro_radio/server.py:2492-2496`（docstring は `:2477-2481`）

**実証（実行済み）**: `server.py:2493-2495` は `principal.authenticated` の分岐が無く
`secret_key_configured` / `auth_ready` / `auth_mode` を**無条件**に返す。
docstring は「認証済みの呼び出しにだけ返す」「匿名には出さない」と明記している。
`docs/code_review_2026-10-01.md:325-327` は「2 周目で修正済み」と宣言しているが**未修正**。

**失敗シナリオ**: 匿名プローバが `GET /health` 1 回で「認証は session モードか bearer モードか」
「鍵が設定済みか」を確定し、攻撃の分岐を選ぶ手がかりになる。

**修正**: 3 フィールドを `if principal.authenticated:` の中に入れる。
`auth_required` / `auth_enforced` / `status` / `service` / `version` / `api_key_configured`
は監視に必要な最小集合として残す。docstring の文言と実装を一致させる。

**回帰テスト**（`tests/test_server_regression.py` に追加）
- `test_health_hides_auth_fields_from_anonymous_callers`
- `test_health_returns_auth_fields_to_authenticated_callers`

**確認**: `tests/test_health.py` / `test_server_regression.py` の既存テストが
`auth_mode` の存在を固定している可能性があるので、既存側を壊さない形で更新する。

---

### P0-3 C-01: `songs=[]` が `None` に潰れ「鳴らない曲を紹介」する

**ファイル**: `retro_radio/core/script_generator.py:219,754,764,770`

**実証（実行済み）**:
```
validate_song_pairs([])  → None
_resolve_allowlist([], 1975) → None
```

**原因**: `validate_song_pairs` は末尾で `return pairs or None` とするため、
空リスト（=「1 曲も鳴らせない」と確定した状態）が `None`（＝「未指定」）に潰れる。
`_resolve_allowlist`（`:403-405`）も `if songs is not None: return validate_song_pairs(songs)`
なので判定にも到达しない。

**失敗シナリオ**: `server.py:1439` が音源ゼロのとき `ctx.selected_pairs = []` を設定し、
`:1456-1460` は「`or None` で潰さない。空リストと None は別物」明記なのに、
`script_generator` が**カタログから曲名を導出し直して司会に書かせる**。
利用者は「次は『○○』です」と聞きながら間奏が流れる（嘘になる）。
`tests/test_song_alignment.py::test_empty_songs_behaves_like_none:142` が**このバグを固定化**している。

**修正**: `validate_song_pairs` の戻り値の契約を変える。
`None`（＝未指定）だけ.catalog フォールバックに落とし、空リストは**空リストのまま**通過させる。
`generate_radio_script` の `if allowed:`（`:755`、ログのみ）と
`_resolve_allowlist`（`:403`）を `is None` 判定に書き換える。

**回帰テスト**
- `tests/test_song_alignment.py` の `test_empty_songs_behaves_like_none` を
  **`test_empty_songs_never_mentions_a_song_title`** に書き換える
  （`「…」` パターンが 0 件であることを assert）
- `tests/test_song_alignment.py::test_songs_none_still_derives_from_the_catalog` を追加
- `tests/test_no_audio_gaps.py::test_script_is_told_only_about_playable_songs` が
  現行の意図を固定していることを確認して維持

---

### P0-4 C-02: `text_cleaner` が番号付き本文行を丸ごと削除

**ファイル**: `retro_radio/utils/text_cleaner.py:94`

**実証（実行済み）**:
```
clean_script_for_tts('### T1\n1. 1975年、FOMOという流行語が誕生\n2. 石油危機\nbody.\n')
→ 'body.'
```
本文 2 行が丸ごと消える。`^\s*\d+[\.、]\s*.+$` は見出しの限定ではなく
「数字＋.／、で始まる任意の行」を消す。

**修正**: 番号は行頭のみに限定し（`\s*` が `\n` を跨がないよう `[ \t]*`）、
**見出し語リスト（`_LABEL_ONLY_PATTERNS` が既に持つ語）かコロン付きラベルに限る**。
`_LABEL_ONLY_PATTERNS` の見出し語を共通のfrozensetに切り出し、
番号付き版も `_LABEL_ONLY_PATTERNS` から生成して二重定義をやめる。

**回帰テスト**（`tests/test_text_cleaner.py`）
- `test_clean_script_keeps_numbered_body_lines`（上の入力を assert）
- `test_clean_script_keeps_numbered_lines_that_carry_sentences`
- 既存の `test_clean_script_removes_numbered_sections`（見出しのみ）は緑を維持

---

### P0-5 C-03: 単独 `###` 行が次の行の本文を削除

**ファイル**: `retro_radio/utils/text_cleaner.py:91`

**実証（実行済み）**:
```
clean_script_for_tts('### OPEN\nline1 body.\n###\nline2 IMPORTANT.\n### END\nbye')
→ 'line1 body.\n\nbye'
```
`^\s*#{1,6}\s+.*$` の `\s+` が改行 `\n` を吸収し、`.*$` が次行を飲み込む。

**修正**: `^\s*` → `^[ \t]*`、`\s+` → `[ \t]+`。
さらに見出し行は `#` の後ろに語を要求し（`#{1,6}[ \t]+\S`）、
単独 `###` は本文行として扱う。

**回帰テスト**
- `test_clean_script_does_not_drop_the_line_after_a_bare_hash_heading`（上の入力を assert）
- `test_clean_script_keeps_a_hash_only_line_as_body`（単独 `###` が残る）

---

### P0-6 C-04: フォールバック原稿の文法破綻

**ファイル**: `retro_radio/core/fallback.py:679,685,690`

**実証（実行済み）**: `generate_fallback_script(1975,9,24)` に `。を` が **3 箇所**。
```
それでは、この年のヒット曲をお届けします。を「神田川」（南こうせつとかぐや姫）。
```
テンプレート置換の残骸。TTS がそのまま読み上げる。

**修正**: 3 箇所とも `。を{_song_phrase(x)}。` → `。{_song_phrase(x)}をお听得ください。`
など、文として成立する形にする。`eval/metrics/preannounce.py:6-8` が
「`core/fallback.py` は編集しない」と書いているが、それは生成規則の話であり
可変文字列の修正を禁じる理由にはならない（`eval` 側は変更しない）。

**回帰テスト**（`tests/test_content_regression.py`）
- `test_fallback_script_contains_no_broken_sentence_fragment`
  （`re.search(r"。を", script)` が 0 件）
- `test_fallback_script_song_phrases_end_a_sentence`
  （`_song_phrase` の直前に句点が来ない）

---

### P0-7 H-03: `* ` 箇条書きが丸ごと削除

**ファイル**: `retro_radio/utils/text_cleaner.py:57-60`

**実証（実行済み）**:
```
clean_script_for_tts('### T1\nbody line.\n* 1975 item one\n* 1975 item two\n')
→ 'body line.'
```
`_STAGE_DIRECTION_LINE` の `[※*]+` が Markdown の箇条書き行を丸ごと消す。

**修正**: 記号だけの行（`^[※*]{1,3}[ \t]*$`）と
演出語付きの行（`※音楽…`）を**別パターン**に分ける。
`retro_radio/core/script_generator.py:666-669` の `_SONG_MARKER_BODY` にある
30 字以内の演出語リストと共有する。

**回帰テスト**
- `test_clean_script_keeps_bullet_points_that_are_not_stage_directions`
- `test_clean_script_still_removes_symbol_only_stage_direction_lines`

---

### P0-8 F-03: lint ベースラインが新規違反を構造的に検出できない

**ファイル**: `.github/flake8-app-baseline.ini:31-52`

**実証（実行済み）**: `--select=E302,E501,F401,W293,W292` で **187 件**が実在する。
しかし `extend-ignore` は**個数ではなくコードベース**で除外するため、
`retro_radio/core/fallback.py` に `def foo():` を 1 個足しても CI は緑のまま。

ベースラインのコメント（`:6`）は「新たに増える違反は必ず CI で落ちる」と
**事実と反転**して書いている。

**修正**: 一度で全部を直すと 154 件（E302 95 + W293 59）が赤になる。
段階的恒星を 2 段で行う。
1. **機械的整形で解消できるコードだけ**をベースラインから外す:
   `W292`（末尾改行 7 件）、`W391`（末尾空行 1 件）、`F401`（未使用 import 2 件）。
   併せて `.pre-commit-config.yaml` の `exclude` から `retro_radio/` を外し、
   `end-of-file-fixer` / `trailing-whitespace` が効くようにする。
2. **`retro_radio/` に機械検査テストを追加**して、ベースラインに
   新しвноいコードが載っていないことを固定する
   （`extend-ignore` への追記を許さない）。

**回帰テスト**（`tests/test_infra_hardening.py`）
- `test_app_flake8_baseline_does_not_wholesale_ignore_real_defect_classes`
  （`F401` / `W292` / `W391` が `extend-ignore` に**載っていない**ことを assert）
- `test_app_baseline_violation_count_never_grows`
  （`flake8 --select=<baseline の除外コード>` の件数に上限を設ける。
  現在値を基準にして、増加したら赤）

---

### P0-9 F-10: 性能テスト3件が `assert True` のプレースホルダー

**ファイル**: `tests/test_performance_baseline.py:12,17,22`

**実証（実行済み）**: 3 関数すべて `assert True  # プレースホルダー`。
冒頭のコメントは「実際の **Streamlit** サーバーが必要なため」という**廃止済み技術**を参照。
`pytest` の 2801 passed にこの 3 件が含まれる。

**修正**: 実際の測定を実装する（以下の 3 つを機械的に測れるものにする）。
1. `load_songs()` を N 回呼んだときの時間
2. `catalog_health()` の実行時間
3. `clean_script_for_tts` のThroughput（文字数/秒）
Streamlit への言及を削除。基準値を超えたら赤になるしきい値を入れる。

**回帰テスト**（`tests/test_hygiene_regression.py` に追加）
- `test_no_placeholder_assert_true_in_tests`
  （`tests/**/*.py` を走査し、`assert True` が 0 件であることを assert。
  **現状このガードが無く両立が不可能なので、まず 3 個を実装してから入れる**）

---

### P0-10 SEC-13: SQLAlchemy 例外文字列にパスワードハッシュが混入

**ファイル**: `retro_radio/db/session.py:33-48` / `retro_radio/auth/authenticator.py:367,461,496`

**実証**: `create_engine(..., echo=False)` のみで `hide_parameters` を指定していない。
SQLAlchemy の既定は `False` で、`str(IntegrityError)` に
`[parameters: ('1', 'b@x', 'PBKDF2...')]` が含まれる。

**失敗シナリオ**: 同一 email で 2 並列 `signup` が race すると UNIQUE 制約違反になり、
`hashed_password`（PBKDF2 600,000 回）と email（PII）がアプリログに平文で出る。
ログ閲覧者はオフライン総当たり、または pass-the-hash が可能になる。

**修正**:
1. `create_engine(..., hide_parameters=True)`（両方）。
2. 認証関連のログで `f"...: {e}"` をやめ、`logger.exception(...)` にして
   例外文字列を直接出さない。メッセージは型名だけ。

**回帰テスト**（`tests/test_db_session.py`）
- `test_engine_hides_bound_parameters_in_sqlalchemy_errors`
  （engine パラメータを assert）
- `test_authentication_failures_do_not_log_the_password_hash`
  （`str(exc)` にハッシュが含まれないことの execute 経路テスト）

---

### P0-11 SEC-11: 認証設定の矛盾が逆向きで fail-open

**ファイル**: `retro_radio/server.py:300-313,344-365`

**実証（実行済み）**: `:344` の `if not _auth_enforced(): return <匿名 principal>` が
`:353` の矛盾検出より**先**に走る。
`RETRO_RADIO_REQUIRE_AUTH=0` なのに `Settings.require_auth=True` のとき、
`resolve_mode` を確認せず匿名の `default` principal を**通してしまう**。
3 周目の fail-closed は「env=1 かつ Settings=disabled」の一方向のみ。

**修正**: 判定順を入れ替える。先に `_auth_enforced()` を評価し、
次に `resolve_mode(current_settings)` を確認して、
**両者が「認証が無効」を双双指示するときだけ**匿名を通す。それ以外は 503。

**回帰テスト**（`tests/test_server_api_auth.py` に追加）
- `test_env_zero_with_settings_require_auth_true_fails_closed`
- `test_audio_route_and_generate_route_agree_on_auth_enforcement`

---

### P1-12 H-01 / H-02: 本番経路に seed なし `random`

**ファイル**: `retro_radio/core/script_generator.py:111` / `retro_radio/core/song_selector.py:59` / `retro_radio/core/fallback.py:263,265,289`

**実証（報告値・コア担当実測）**: `_build_prompt(1975,9,24,'normal')` を 20 回で **11 通り**。
`SongSelector(history=..., rng 未注入)` は**実際に流れる曲と順序が非決定**。
`script_generator.py:374` の `Random(int(year))` だけが正しく実装されている。

**失敗シナリオ**: `eval --offline` は決定論を要求する。
Gemini 経路では**プロンプトが変わる**ため同じ入力でも出力が変わり、CI が再現不能になる。

**修正**: 既定の rng を決定的なものにする。
1. `song_selector.SongSelector.__init__` の `rng or random.Random()` を、
   `history` や呼び出し引数から導出した決定的な seed にする（`random.Random()` を素で使わない）。
2. `script_generator.select_news_topics` に `rng` 引数を足し、`_build_segmented_prompt` から年を渡す。
3. `fallback.py` の `random.choice` / `random.shuffle` を `rng` 引数経由にする。

**回帰テスト**
- `tests/test_song_catalog.py::test_song_selector_default_rng_is_deterministic`
- `tests/test_core_models.py::test_select_news_topics_is_deterministic_for_a_fixed_year`
- `tests/test_content_regression.py::test_fallback_script_is_identical_across_repeated_calls`

**注意**: 決定論lify すると eval の台本が変わる可能性があるため、
修正後に `python -m eval --offline --threshold 80` が緑であることを**必須**とする。

---

### P1-13 SEC-06 / SEC-07: プライバシー最小化と履歴が実経路に未配線

**ファイル**: `retro_radio/api/me.py:207-303`（`normalize_target_name` / `validate_anniversary_input`）
/ `retro_radio/services/history_service.py:94`（`record_generation`）
/ `retro_radio/server.py:609-634`（`GenerateRequest`）

**実証（実行済み）**: `grep` で `retro_radio/` 内の `validate_anniversary_input` /
`normalize_target_name` / `record_generation` の**production 参照は 0 件**。
`GenerateRequest.target_name` は `max_length=64` で、`anniversary` モードでも
`month` / `day` を保持する。

**失敗シナリオ**:
1. 施設 UI が「本名ではなくニックネーム（16 文字以内）」と表示しても、
   サーバは 64 文字の本名と完全な生年月日を受理し `generations` に保存する。
   同意取得時に開示した内容と実データが食い違う。
2. `DELETE /api/me` は `favorite_tracks` / `music_profiles` / `consents` を消すだけで
   `generations`（原稿本文）を消さない。`purge_user_personal_data`
   （`privacy_repository.py:948-964`）は `music_profiles` と `mark_deleted` のみ。
3. `GET /api/me/export` の `generations` が常に `[]` になる（構造的に空）。

**修正**:
1. `GenerateRequest` の `model_validator` から `normalize_target_name` を呼ぶ。
   `anniversary` モードでは `month`/`day` を `None` に落とす。
2. `purge_user_personal_data` に `GenerationRepository.delete_for_user` を足す。
3. `record_generation` を生成完了時に呼ぶ（`user_id` が無い個人モードでは記録しない、
   既存方針を維持）。

**回帰テスト**（`tests/test_me_api.py` / `tests/test_history_service.py`）
- `test_generate_request_caps_target_name_at_16_characters`
- `test_anniversary_mode_drops_month_and_day`
- `test_delete_me_purges_generation_rows`
- `test_export_after_deletion_returns_no_personal_rows`

---

## 2. 実行順序

P0 は互いに独立しているため、ファイル単位で排他を確保して並列実行できる。
ただし **`text_cleaner`（P0-4/5/7）は同一ファイル**なので 1 本にまとめる。

```
  P0-1 (tokens/server/config)     P0-4,5,7 (text_cleaner)
        │                                  │
  P0-2 (server /health)            P0-3 (script_generator)
        │                                  │
  P0-8 (baseline/pre-commit)       P0-6 (fallback)
        │                                  │
  P0-9 (performance tests)         P0-10 (db/session/authenticator)
        │                                  │
        └──────────────┬───────────────────┘
                       │
              P0-11 (server tenant_principal)
                       │
              P1-12 (決定論) ──→ eval ゲート緑を必須確認
                       │
              P1-13 (privacy 配線)
```

**注意**: P0-1 / P0-2 / P0-11 はいずれも `retro_radio/server.py` を触る。
1 本にまとめて直列で進める（競合を避ける）。

---

## 3. 各修正の完了条件（機械的に検証できることのみ）

| # | 条件 | 検証コマンド |
|---|---|---|
| 1 | 全テスト緑 | `python -m pytest -q` → 0 failed |
| 2 | lint 3 系統緑 | `flake8 tests` / `flake8 --config .github/flake8-app-baseline.ini retro_radio` / `flake8 eval` → EXIT 0 |
| 3 | 追加した lint が緑 | `flake8 scripts utils db` → EXIT 0（現状は未配線） |
| 4 | eval ゲート緑 | `python -m eval --offline --threshold 80` → EXIT 0 |
| 5 | 新回帰テストが**修正前に赤になる**こと | 各テストを個別に走らせ、修正前は fail を確認してから修正する |
| 6 | ベースラインの違反数が増えていない | 新規テスト `test_app_baseline_violation_count_never_grows` が緑 |
| 7 | 曲カタログ整合 | `python scripts/validate_songs.py` → fail 0 |
| 8 | 事実レジストリ整合 | `python scripts/validate_facts.py` → fail 0 |
| 9 | マイグレーション整合 | `alembic check` → `No new upgrade operations detected.` |
| 10 | 既存テストの**意図変更**がない | `tests/test_song_alignment.py::test_empty_songs_behaves_like_none` の書き換え理由を docstring に残す |

**手順の規律**: すべての回帰テストは**先に書いて、修正前に赤くなることを確認してから**修正に入る。
「 green だから正しい 」を排除するため。

---

## 4. Round 1 で意図的に直さないもの（仕様判断・破壊的変更）

| ID | 項目 | 理由 |
|---|---|---|
| SEC-02 | 認証前の恒久ロックアウト（429 がパスワード検証より前） | 総当たり対策と可用性対策の分離は設計変更。Round 2 以降で扱う |
| SEC-03 | リクエストボディ無制限（DoS） | ミドルウェア追加はhops 影响が大きい。Round 2 |
| SEC-04 | `_generation_slots` に per-principal 上限が無い | 429 の導入は API 契約の変更。Round 2 |
| SEC-05 | SSE 同時接続数の上限が無い | 専用 executor への切替は設計変更。Round 2 |
| SEC-09 | `POST /api/admin/audit` の `meta` にサイズ上限が無い | 保持年限の設計とセット。Round 2 |
| SEC-10 | `X-Forwarded-Proto` を無条件信頼 | 信頼プロキシの IP リストという設定が要る。Round 2 |
| S-02 | `confidence` が 2966/2966 で `unverified` かつどのコードも参照しない | データ検証（一次文献照合）が要る。機械では決められない |
| S-03 | facts の `valid_to: null` が 1925 年開始の番組を 1975 年の番組表に出す | データ検証が要る |
| M-24 | facts の `valid_to: null` の意味論 | 同上 |

これらは Round 2 / Round 3 の対象外でも、**残存リスクとして最終レポートに明示**する。

---

## 5. 変更履歴

- 2026-10-01: 初版。サブエージェント 4 本のレビュー（セキュリティ・コア生成・
  データ/サービス・フロントエンド/CI/テスト）を受けて P0 11 項目 + P1 2 項目を計画化。
  すべての P0 は主担当が実行して再現を確認済み。