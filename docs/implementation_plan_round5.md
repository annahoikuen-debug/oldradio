# 実装計画 Round 5 — 運用安定化（TTS 403 対策・SECRET_KEY・カタログ・ランチャー・ヘルスチェック）

> **ステータス: 提案（2026-11-14）**
>
> 2026-11-14 の実機起動ログで観測された問題を、コード調査（実証つき）に基づき
> 修正するための計画。Round 1〜4 の原則（主担当が自分で再現 → 実証列に
> `ファイル:行` を書く → 依拠テスト洗い出し → 回帰テスト追加）を踏襲する。

---

## 0. 背景 — 起動ログで観測された 4 つの事象

2026-11-14 02:48〜02:50 の起動ログより:

| # | 事象 | 深刻度 | 本計画の項目 |
|---|------|--------|--------------|
| 1 | `run_retro_radio_ja.bat` の日本語 REM 行がコマンドとして実行され `'not recognized'` エラー | 済（修正完了） | 付録 A（参照のみ） |
| 2 | edge-tts が `403 Invalid response status` で失敗し、毎回 gTTS へフォールバック | **P1** | § 1 TTS-01 |
| 3 | `RETRO_RADIO_SECRET_KEY が未設定` の RuntimeWarning（セッション/認証機能が無効） | P2 | § 2 SEC-01 |
| 4 | 曲カタログが目標曲数に届いていない警告（3108 曲 / 74 年、50 曲未満の年が 29 個） | P2 | § 3 CAT-01 |

さらに、これらを運用側から見える化する項目を 2 つ追加する:

| # | 項目 | 目的 |
|---|------|------|
| 5 | バッチランチャーの堅牢化 | 起動失敗時に原因が即わかるようにする | § 4 BAT-01 |
| 6 | ヘルスチェックの拡充 | 運用者が TTS エンジン状態を監視できるようにする | § 5 HLT-01 |

---

## 0-1. ゲート基準（完了条件）

| ゲート | 目標 |
|---|---|
| `python -m pytest -q` | 既存全数 passed / 0 failed + 本 Round の回帰テスト追加分 |
| `flake8 retro_radio utils eval scripts tests` | EXIT 0 |
| `python -m eval --offline --threshold 80` | EXIT 0 / 24 of 24（現状維持） |
| `alembic check` | No new upgrade operations detected（本 Round はスキーマ変更なし） |
| `validate_songs.py` | fail 0 件（CAT-01 追加分を含む） |
| 実機確認 | edge-tts 403 が恒常化している環境で、gTTS フォールバックが**ログに 1 度だけ**記録され、2 回目以降はキャッシュまたは成功経路に流れること |

---

## 1. TTS-01（P1）: edge-tts 403 の根本対策

### 1-1. 実証

- 起動ログ（2026-11-14 02:50:24 / 02:50:41 ほか）— すべての TTS 生成が
  `edge-tts synthesis failed: 403, message='Invalid response status',
  url='wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1?TrustedClientToken=...'`
  で失敗し、gTTS へフォールバックしている。
- [`retro_radio/core/tts_engines.py:103-111`](../retro_radio/core/tts_engines.py) —
  `edge_tts.Communicate(...)` を `voice / rate / pitch / volume` のみで構築しており、
  **`proxy`・`connect_timeout`・`receive_timeout` を渡していない**。
  インストール済み edge-tts 7.2.8 の `Communicate.__init__` シグネチャ（実測）:
  `text, voice, rate, volume, pitch, boundary, connector, proxy=None,
  connect_timeout=10, receive_timeout=60`。
- [`retro_radio/server.py:1333-1355`](../retro_radio/server.py) —
  edge 失敗時は gTTS キーへ切替（設計どおり正しい挙動）だが、
  **「edge が恒常的に死んでいる環境」で毎回 wss 接続 → 403 待ち → フォールバック**
  が全セグメントで繰り返され、1 セグメントあたり約 2.5 秒の死に時間が乗る
  （ログ: 02:50:24 edge 試行 → 02:50:27 gtts 切替、02:50:41 → 02:50:44）。
- [`retro_radio/core/tts_engines.py:30-40`](../retro_radio/core/tts_engines.py) —
  `EdgeTTSOptions` に `proxy` / timeout が無いため、
  追加してもキャッシュキーが変わらない（音声内容に影響しない項目なので正しい）。

### 1-2. 修正

#### (a) `retro_radio/core/tts_engines.py`

1. `EdgeTTSOptions` に**音質に影響しない 3 フィールド**を追加する
   （`cache_token()` には含めない — 音声内容が変わらないため）:

   ```python
   @dataclass(frozen=True)
   class EdgeTTSOptions:
       voice: str = "ja-JP-NanamiNeural"
       rate: str = "+0%"
       pitch: str = "+0Hz"
       volume: str = "+0%"
       # --- 以下は接続パラメータ。キャッシュキー（cache_token）には含めない。 ---
       proxy: Optional[str] = None
       connect_timeout: float = 10.0
       receive_timeout: float = 60.0
   ```

2. `synthesize_to_file()` で `Communicate` へ渡す:

   ```python
   communicate = edge_tts.Communicate(
       text,
       options.voice,
       rate=options.rate,
       pitch=options.pitch,
       volume=options.volume,
       proxy=options.proxy,
       connect_timeout=options.connect_timeout,
       receive_timeout=options.receive_timeout,
   )
   ```

   - edge-tts 7.x は `proxy` を `websockets.connect` へ透過する。
     `proxy` は「環境変数 `RETRO_RADIO_TTS_EDGE_PROXY` が設定されたときだけ」
     効くようにする（既定 `None` = 現行挙動）。

#### (b) `retro_radio/config.py`（`Settings` の TTS 節、142 行付近）

```python
# edge-tts の接続パラメータ（音声内容に影響しないためキャッシュキーに含まれない）。
# 403 / ネットワーク障害が恒常的な環境（法人プロキシ等）でだけ設定する。
tts_edge_proxy: str = ""
# wss 接続のタイムアウト秒。403 待ちがセグメントあたり 2.5 秒の死に時間になるのを防ぐ。
tts_edge_connect_timeout: float = Field(default=5.0, ge=1.0, le=30.0)
tts_edge_receive_timeout: float = Field(default=60.0, ge=5.0, le=300.0)
# edge が連続失敗したとき、この秒数は edge を試さず gTTS に直接行く（サーキットブレーカー）。
# gTTS の tts_circuit_breaker_seconds と同じ設計。0 で無効化。
tts_edge_circuit_breaker_seconds: float = Field(default=300.0, ge=0.0, le=3600.0)
```

#### (c) `retro_radio/server.py`

1. `_tts_edge_options()`（`_generate_tts_via_edge` で使用）に
   新設定 3 項目を流し込む。
2. **edge サーキットブレーカー**を追加 — gTTS 側（`_tts_is_blocked_by_gtts` /
   `_tts_circuit_open`、`server.py` 内実装済み）と同じパターン:
   - `_edge_failures`（時刻リスト）をモジュール状態として保持し、
     連続失敗が 2 回を超えたら `tts_edge_circuit_breaker_seconds` の間
     `generate_tts_cached()` は edge を試さず gTTS へ直接行く。
   - 回復判定: ブレーカー時間経過後の 1 回だけ edge を再試行（half-open）。
   - **既存の「edge 成功時は edge キー / フォールバック時は gTTS キー」**
     のキャッシュキー切替設計（`server.py:1342-1355`）は**変更しない**。
     ブレーカーが閉じたら edge は再試行され、ニューラル音声に戻る。
3. `generate_tts_cached()` の edge 分岐冒頭（1333 行付近）に
   ブレーカー判定を 1 行追加:

   ```python
   if _active_tts_engine() == tts_engines.ENGINE_EDGE and not _edge_circuit_open():
       try:
           ...
       except Exception as e:
           _note_edge_failure()  # 追加: 連続失敗を記録しブレーカーを開ける
   ```

### 1-3. 効果（受け入れ基準）

| 事象 | 現状 | 修正後 |
|---|---|---|
| edge が恒常 403 の環境 | 全 6 セグメント × 毎回 wss 接続 + 403 待ち（約 15 秒の死に時間） | 1〜2 回失敗したら以降は即 gTTS（死に時間ほぼ 0） |
| 法人プロキシ環境 | edge が使えない | `RETRO_RADIO_TTS_EDGE_PROXY` で edge が使える可能性 |
| edge 回復時 | 自動復帰（既存設計） | 変更なし（ブレーカー half-open で 1 回試して復帰） |

### 1-4. 依拠テスト（洗い出し）

- `tests/` 配下の edge-tts 関連（`grep -l "edge" tests/` で特定）—
  `EdgeTTSOptions` を直接構築しているテストは**デフォルト値のみの変更**なので
  影響しない見込み。ただし `synthesize_to_file` の呼び出しを mock している
  テストは、`Communicate` への kwargs 追加でシグネチャ検証がある場合のみ更新。
- `tests/test_env_templates.py` — **未知の `RETRO_RADIO_` キーを弾く**ため、
  `.env.example` と `config.py` の両方に新キーを足す必要がある
  （Round 4 の実装記録に同様の失敗例あり）。

### 1-5. 回帰テスト

新規 `tests/test_tts_edge_circuit.py`:

1. `test_edge_failure_opens_circuit` — edge 合成を 2 回失敗させると、
   3 回目は edge を試行せず gTTS 経路に直接行くこと（`_generate_tts_via_edge`
   が呼ばれないことを mock で検証）。
2. `test_edge_circuit_half_open_retry` — ブレーカー時間経過後は
   edge を 1 回だけ再試行すること。
3. `test_edge_circuit_does_not_affect_cache_key` — ブレーカー開閉が
   キャッシュファイル名（edge キー / gTTS キーの切替）に影響しないこと。
4. `test_edge_proxy_passed_to_communicate` — `RETRO_RADIO_TTS_EDGE_PROXY`
   設定時、`Communicate` に `proxy=` が渡ること（edge-tts を mock）。
5. `test_edge_options_excluded_from_cache_token` — `cache_token()` に
   proxy / timeout が含まれないこと（同一 voice/rate/pitch/volume なら
   同一トークン）。

---

## 2. SEC-01（P2）: SECRET_KEY のセットアップ支援

### 2-1. 実証

- 起動時 `RuntimeWarning: RETRO_RADIO_SECRET_KEY が未設定です...`
  （pydantic が `.env` を読み込んだ時点で出る）。
- [`.env`](../.env) — 現在 `RETRO_RADIO_REQUIRE_AUTH=0` の 1 行のみ。
  `RETRO_RADIO_SECRET_KEY` は未設定。個人利用モードでは**動作に必須ではない**が、
  警告が毎回出て、将来 `REQUIRE_AUTH=1` に切り替えたときに
  [`retro_radio/config.py:11`](../retro_radio/config.py) の
  `MIN_SECRET_KEY_LENGTH = 32` 未満・未設定で fail-closed（503）になる。
- [`.env.example:252-264`](../.env.example) — 設定方法のドキュメントは
  あるが、生成コマンドを実際に打つ手順は README に無い。

### 2-2. 修正

#### (a) `run_retro_radio_ja.bat` / `run_retro_radio.bat`

Gemini キー確認ブロック（32-44 行付近）の直後に、RETRO_RADIO_SECRET_KEY の
**有無チェックのみ**を追加（値を読まない・表示しない）:

```bat
REM RETRO_RADIO_SECRET_KEY の有無確認（値は読まない。認証モードでのみ必要）。
set "HAS_SECRET_KEY=0"
if defined RETRO_RADIO_SECRET_KEY set "HAS_SECRET_KEY=1"
if exist ".env" (
    findstr /B /C:"RETRO_RADIO_SECRET_KEY=" .env | findstr /V /C:"RETRO_RADIO_SECRET_KEY=$" > nul 2>&1
    if not errorlevel 1 set "HAS_SECRET_KEY=1"
)
if !HAS_SECRET_KEY!==0 (
    echo [情報] RETRO_RADIO_SECRET_KEY が未設定です（個人利用モードでは起動できます）。
    echo [情報] 認証を有効にする場合は .env に追記してください:
    echo [情報]   生成: !PYTHON_CMD! -c "import secrets; print(secrets.token_urlsafe(48))"
)
```

- **注意**: findstr は正規表現 `$` を使えないため、実装時は
  `findstr /B /C:"RETRO_RADIO_SECRET_KEY=" .env` で「行の存在」だけを見て、
  「空値」判定は Python 側（pydantic）に任せる（警告が出るのは既知の挙動）。
  上記は疑似コードであり、実装時は**動作確認を必須**とする。

#### (b) `README.md`

「初回セットアップ」節に RETRO_RADIO_SECRET_KEY 生成手順を 3 行で追記:

```
認証を有効にする場合（施設利用）は .env に RETRO_RADIO_SECRET_KEY を設定:
  python -c "import secrets; print(secrets.token_urlsafe(48))" の出力を
  RETRO_RADIO_SECRET_KEY= に貼り付け（32 文字以上必須）
```

### 2-3. 非目標

- `.env` への**自動書き込みはしない**（Gemini キーと違い、書き込みミスで
  全セッションが無効化されるリスクがあるため、手動手順の案内に留める）。
- 警告自体の抑制（pydantic の `warnings.filterwarnings`）はしない。
  「未設定であること」は運用者が知るべき事実。

### 2-4. 依拠テスト / 回帰テスト

- バッチファイルは CI 対象外のため回帰テストなし。実機ダブルクリックで
  3 パターン確認: (1) `.env` 無し (2) `RETRO_RADIO_SECRET_KEY=` 空 (3) 値あり。
- README は lint 対象外。

---

## 3. CAT-01（P2）: 楽曲カタログ拡張ワークフロー

### 3-1. 実証

- 起動ログ — `曲カタログが目標曲数に届いていません: 総 3108 曲 / 74 年、
  1 年 50 曲未満の年が 29 個（目標 50 曲）`。
- [`retro_radio/core/songs/__init__.py:14-28`](../retro_radio/core/songs/__init__.py) —
  モジュール自身が「契約は満たせていない」と明記しており、
  `pool_for_year` が隣接年・10 年帯へ広げることで動作は継続している
  （＝**壊れているわけではなく、品質問題**）。
- [`docs/song_catalog.md`](../song_catalog.md) — 編集手順は
  `scripts/song_source/songs.tsv` → 生成 → `validate_songs.py` の
  既存ワークフローが存在する。

### 3-2. 修正（コード変更ではなく運用 + 小さなスクリプト追加）

1. **不足年の特定スクリプト** `scripts/catalog_gaps.py` を新規作成:

   ```python
   """1 年あたりの曲数が目標（50）に届かない年を一覧出力する。

   使い方: python scripts/catalog_gaps.py [--min 50]
   出力: 年 / 曲数 / 不足数 を TSV で stdout に書く（スプレッドシートに貼れる）。
   """
   from retro_radio.core.songs import catalog_health
   ```

   - `catalog_health()` は既存の実測値 API（`songs/__init__.py` が公開）。
     新規ロジックは書かず、既存関数の **プレゼンテーション層** に留める。

2. **拡張対象の優先順位**（`catalog_gaps.py` の出力で決定）:
   - 対象年選択頻度が高い年（`default_year=1975` 付近、1950-1980）を優先。
   - 1 年あたり +10 曲ずつ追記 → `validate_songs.py` → 実機放送 1 回 → 次の年。
     （一括数百曲より、検証可能な単位で進める）
3. **source / confidence 規約の遵守** — 追記レコードは
   `songs/__init__.py:69-80` の `REQUIRED_FIELDS` / `ALLOWED_CONFIDENCE` に従い、
   `source` に推測 URL を書かない（CI ゲートで fail）。

### 3-3. 受け入れ基準

- `catalog_health()` の「50 曲未満の年」が 29 → **20 以下**（段階的に）。
- `validate_songs.py` fail 0 を維持。
- 1983 年（ログで実放送した年）で隣接年補完なしに 18 曲確保できること。

### 3-4. 回帰テスト

- `scripts/catalog_gaps.py` 自体は CI 対象外だが、
  `flake8 scripts` は通す。
- カタログ追記は既存 `tests/test_songs*.py`（存在するもの）の
  「総曲数」前提テストがあれば更新が必要 — **実装時に grep で洗い出す**
  （例: 曲数カウントをアサートするテスト）。

---

## 4. BAT-01（P2）: バッチランチャーの堅牢化

### 4-1. 実証

- 付録 A のとおり、`run_retro_radio_ja.bat` は UTF-8/CRLF 修復済み。
- [`run_retro_radio_ja.bat:116-120`](../run_retro_radio_ja.bat) —
  uvicorn 起動の成否を**何も見ていない**。起動失敗（例: ポート占有）でも
  バッチは無言で終了し、原因が残らない。
- ポート 8501 が既に使用中の場合、uvicorn は `error while attempting to bind
  on address ... only one usage of each socket address` で即終了するが、
  利用者には「窓が一瞬で消える」ように見える。

### 4-2. 修正（`run_retro_radio_ja.bat` / `run_retro_radio.bat` 両方）

1. **ポート占有の事前チェック**（起動前に分かりやすく失敗させる）:

   ```bat
   netstat -ano | findstr /C:":!PORT! " | findstr /C:"LISTENING" > nul 2>&1
   if not errorlevel 1 (
       echo [エラー] ポート !PORT! は既に使用されています。
       echo [情報] 前回のサーバーが残っていないか確認するか、PORT を変えてください。
       pause
       exit /b 1
   )
   ```

2. **uvicorn 起動失敗時の待機**: 120 行の `goto :eof` 直前に、
   `if errorlevel 1 (echo [エラー] サーバーが異常終了しました & pause)`
   を追加し、窓が即座に閉じないようにする。

3. **encoding コメントの是正**: 6-8 行の REM に
   「本ファイルは UTF-8 (BOM なし)・CRLF で保存すること。BOM があると
   1 行目が壊れる」を明記（付録 A の再発防止）。
   さらに `.gitattributes` に `*.bat text eol=crlf` を追加し、
   git が LF に正規化しないようにする。

### 4-3. 受け入れ基準

- ポート占有時に**即座に**日本語エラーメッセージ + pause で停止する。
- uvicorn 異常終了時に窓が残り、エラーが読める。
- `git status` で `.bat` が常に CRLF 保たれる（`.gitattributes` 反映後）。

---

## 5. HLT-01（P3）: ヘルスチェック拡充

### 5-1. 実証

- [`retro_radio/server.py:3355-3398`](../retro_radio/server.py) —
  `/health` は匿名へ `status / service / version / api_key_configured /
  auth_required / auth_enforced` を返す。docstring が**情報開示の契約**を
  明示している（匿名には最小集合、`secret_key_configured` 等は認証済みのみ）。
- TTS の健全性（どのエンジンが実効的に使えるか）は `/health` に出ておらず、
  付録 A のような 403 恒常化は**ログを読まないと分からない**。

### 5-2. 修正（`retro_radio/server.py` の `health()`）

1. **匿名にも出してよい集合**（docstring 契約を遵守しつつ追加）:

   ```python
   payload["tts_engine_active"] = _active_tts_engine()
   # "edge" / "gtts" のいずれか。値そのものは機微情報ではない。
   ```

2. **認証済みにのみ出す集合**（`if principal.authenticated:` 内）:

   ```python
   payload["tts_edge_circuit_open"] = _edge_circuit_open()   # TTS-01 で追加
   payload["song_catalog_years_total"] = <catalog_health の年数>
   payload["song_catalog_thin_years"] = <50 曲未満の年数>
   ```

   - 曲数などのカタログ統計は攻撃面にならないが、**契約（匿名=最小集合）を
     崩さないため**認証済み側に置く。
   - `catalog_health()` の呼び出しは **lru_cache 済みの正本読み込み**を
     再利用するため、監視プローブの頻度でも負荷にならない。

3. docstring の契約記述を更新（追加フィールドと開示レベルを明記）。

### 5-3. 依拠テスト（洗い出し）

- `/health` のレスポンス形式を検証する既存テスト
  （`tests/test_server_api.py` 等、`grep -n "health" tests/` で特定）—
  **辞書の完全一致**をしているテストがあれば新キーで落ちるため更新。
  匿名/認証済みの 2 パターンがあるはずで、両方を確認する。

### 5-4. 回帰テスト

1. `test_health_anonymous_minimal_fields` — 匿名レスポンスに
   `tts_engine_active` は含まれるが、`tts_edge_circuit_open` /
   カタログ統計 / `secret_key_configured` は含まれないこと。
2. `test_health_authenticated_extended_fields` — 認証済みレスポンスに
   拡張フィールドが含まれること。
3. `test_health_tts_engine_active_reflects_settings` —
   `TTS_ENGINE=gtts` 設定時に `tts_engine_active == "gtts"` になること。

---

## 6. 実装順序と依存関係

```
Day 1 前半   TTS-01 (a)(b)(c) ──→ 回帰テスト 1-5 ──→ 実機 1 回放送で確認
Day 1 後半   BAT-01 (.bat 2 本 + .gitattributes) ──→ 実機 3 パターン確認
Day 2 前半   SEC-01 (.bat チェック + README) ──→ 実機確認
Day 2 後半   HLT-01 ──→ TTS-01 のブレーカー状態を /health で見える化
Day 3 以降   CAT-01（catalog_gaps.py → 段階的追記。1 年ずつ validate）
```

- TTS-01 → HLT-01 は**依存**（HLT-01 が `_edge_circuit_open()` を参照する）。
- 他は独立。CAT-01 はデータ作業のためコードゲートとは分離して進める。
- 全完了後、ゲート基準（§ 0-1）を一括実行して本計画を「完了」に更新する。

## 7. リスクと対策

| リスク | 影響 | 対策 |
|---|---|---|
| edge-tts の `proxy` kwarg がバージョンによっては無い | 起動済み環境で TypeError | `requirements.txt` で edge-tts 下限を `>=7.0` に固定、または kwargs を `inspect.signature` でフィルタ |
| `/health` へのフィールド追加で既存テストが広範に壊れる | ゲート赤 | 実装前に `grep -n "health" tests/` で全数洗い出し（依拠テスト節） |
| `.env.example` と `config.py` のキー不整合 | `test_env_templates.py` が落ちる（Round 4 と同型の失敗） | 新キーは 3 箇所（config / .env.example / INTENTIONAL_ENV_EXAMPLE_DIVERGENCE 判定要否）を同時に編集 |
| edge ブレーカーが「回復している環境」でも gTTS に寄せすぎる | 音質低下 | half-open で 1 回必ず再試行 / `cache_token` は不変なので回復後すぐ edge キーに戻る |
| カタログ追記で `source` 規約違反 | CI fail | 1 年ずつ `validate_songs.py` を回す（§ 3-2 の手順） |
| `.bat` の日本語再破損（エディタで Shift-JIS 保存） | 付録 A の再発 | `.gitattributes` で `*.bat eol=crlf` + README 注意書き |

## 8. 非目標（本 Round でやらないこと）

- ElevenLabs 等の有料 TTS 追加（設定キーは既に存在。運用判断のため）
- `/metrics`（Prometheus）追加 — 監視基盤の選定が先
- カタログへの一括数百曲投入 — 検証単位を崩すため段階的に
- 認証の強制化（`REQUIRE_AUTH=1` への変更）— 運用モードの判断のため

---

## 付録 A: 済み修正 — `run_retro_radio_ja.bat` の文字化け（2026-11-14 対応済み）

- **事象**: 起動時に日本語 REM コメントの断片が
  `'...より下の日本語が正しく表示されます。' is not recognized as an internal
  or external command` として実行された。
- **原因**: ファイルの先頭付近（「先頭の `chcp 65001`...」の行）に
  Shift-JIS バイト（`\x85\x88`）と UTF-8 バイト（`\xe3\x81\xae`）が混在。
  cmd.exe が `REM` を解釈できず、行の一部をコマンドとして実行した。
- **修正**: ファイル全体を **UTF-8 (BOM なし)・CRLF** で書き直した
  （`python` でバイト検証: BOM=False / CRLF=True / valid UTF-8=True を確認済み）。
- **再発防止**: § 4 BAT-01 の `.gitattributes` と注意書き。
