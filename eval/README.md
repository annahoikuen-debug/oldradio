# 評価ハーネス（提案⑨ / サブエージェント S2）

このディレクトリは**テストではなく測定器**である。
目的は `plans/evidence_based_improvement_proposals.md` 438〜494 行が言うとおり、
「1,000 文字以上」という能動的に逆向きの content メトリクスを捨てて、
**台本が壊れている条件を列挙して検査する**ことにある。

---

## 1. ディレクトリ構成

```
eval/
├── README.md              # このファイル
├── __init__.py            # パッケージ（sys.path の保険）
├── __main__.py            # `python -m eval` の入口
├── cases/
│   ├── __init__.py
│   ├── build_cases.py     # ケース定義の生成器（正本から生成する）
│   ├── case_defs.json     # 生成物（24 ケース・コミットする）
│   └── loader.py          # 生成物の読み込み
├── metrics/
│   ├── __init__.py        # run_case / run_all の集約
│   ├── fact_score.py      # FactScore 簡易実装
│   ├── checklist.py       # CheckList 8 項目
│   ├── length.py          # 文字数のレンジ制
│   ├── songs.py           # 曲名一致率（S3 へ渡す中立 API）
│   └── preannounce.py     # 「予告したのに配信しない」検出
├── bww/
│   └── README.md          # Best-Worst 人間評価の手順書（人手が必要・CI には載せない）
└── results/
    └── README.md          # 結果の置き場所（YYYYMM.json）
```

---

## 2. 公開 API

### 2.1 `eval.metrics.fact_score`

```python
from eval.metrics.fact_score import fact_score, extract_atomic_claims, split_sentences

result = fact_score(script: str, year: int) -> FactScoreResult
```

| メンバ | 型 | 意味 |
|---|---|---|
| `score` | `float` | 0〜100。`100 * supported / checkable` |
| `supported` | `int` | 外部ソースで支持された主張の数 |
| `checkable` | `int` | 検証可能な主張の数（情景描写は数えない） |
| `claims` | `tuple[AtomicClaim, ...]` | 抽出した原子的事実 |
| `failures` | `tuple[AtomicClaim, ...]` | **ゲートを落とす**主張（正本に無い番組名の断定） |
| `warnings` | `tuple[AtomicClaim, ...]` | ゲートを落とさない主張（期間外の番組・未来年・対象年以降の曲） |
| `coverage` | `float` | 正解テーブルに載っている事実のうち台本が言及した割合（**別指標**） |
| `is_gate_ok(threshold=80.0)` | `bool` | `failures` が空かつ `score >= threshold` |
| `describe()` | `str` | 1 行サマリ |

補助: `split_sentences(script)` / `extract_atomic_claims(script, year, *, source_titles=None)` /
`is_song_reference(sentence, match)` / `known_song_titles()` / `registry_titles()` /
`is_valid_program(title, year)`。

### 2.2 `eval.metrics.checklist`

```python
from eval.metrics.checklist import run_checklist, CHECKLIST_ITEMS

result = run_checklist(script, year, *, allowed_song_titles=None) -> ChecklistResult
```

8 項目（各項目は独立した公開関数。個別にテストできる）:

| id | 関数 | 違反の意味 |
|---|---|---|
| `heading_order` | `check_heading_order(script)` | 見出し順の反転・欠落・前後 |
| `required_segments` | `check_required_segments(script)` | オープニング/エンディングの欠落 |
| `song_match` | `check_song_match(script, year, *, allowed_titles=None)` | 曲名一致率 90% 未満 |
| `era_words` | `check_era_words(script, year)` | 対象年より後の年・年代表記（warn 相当） |
| `prompt_leftover` | `check_prompt_leftover(script)` | プロンプト残骸・API 応答ラベル |
| `japanese_ratio` | `check_japanese_ratio(script, threshold=0.9)` | 日本語文字比率 |
| `song_duplication` | `check_song_duplication(script, *, allowed_titles=None)` | 同一曲 2 回以上 |
| `unfulfilled_preannounce` | `check_preannounce(script)` | 予告したのに配信しない |

補助: `extract_headings(script)` / `japanese_char_ratio(script)` /
`CHECKLIST_ITEMS` / `REQUIRED_SEGMENTS` / `SONG_MATCH_RATIO_MIN` /
`JAPANESE_RATIO_MIN`。

### 2.3 `eval.metrics.length`

```python
from eval.metrics.length import length_bounds, check_length, measure_lengths

lower, upper = length_bounds()             # (800, 1500)
result = check_length(script) -> LengthResult   # .ok / .direction / .deviation / .describe()
report = measure_lengths()                 # 実測（p5 / p50 / p95 …）
```

- 下限 = `target_script_chars(1000) - script_char_tolerance(200)` = **800**
- 上限 = `max(target + 2*tolerance = 1400, ceil50(実測 p95 = 1476) = 1500)` = **1500**
- **`config.py` は削除も変更もしない**（S4 の所有ファイル）。import するだけ。
- 実測で分布を取り直すには `python -m eval.metrics.length --measure`。

### 2.4 `eval.metrics.songs`（S3 への API 契約）

```python
from eval.metrics.songs import song_match_rate, extract_song_mentions

# S3 未実装（静的マスターと照合）
result = song_match_rate(script, year)                              # 照合先 = static-master

# S3 実装後（実際の選曲結果を渡す）
result = song_match_rate(script, year, allowed_titles=selection_titles)  # 照合先 = caller
```

| メンバ | 意味 |
|---|---|
| `rate` | 一致率 0.0〜1.0。**言及 0 件なら 1.0** |
| `matched` / `unmatched` | 一致した／していない曲名の主張 |
| `duplicates` | 1 番組内で 2 回以上言及された曲 |
| `source` | 照合先の由来（`static-master` / `caller`） |

**`year` 引数は照合に使わない**（中立性を保つため）。S3 は集合を `allowed_titles` で渡す。

### 2.5 `eval.metrics.preannounce`

```python
from eval.metrics.preannounce import detect_unfulfilled_preannounce

findings = detect_unfulfilled_preannounce(script) -> list[str]
```

違反の説明リスト（空なら違反なし）。**検出のみを行い、原稿は変更しない。**

### 2.6 集約

```python
from eval.metrics import run_case, run_all, build_script

result = run_case(case_dict) -> CaseResult     # .fact / .checklist / .length / .failures / .warnings / .passed
report = run_all()               -> EvalReport  # .results / .mean_fact_score / .to_dict()
```

`run_case(case, script=...)` に `script` を渡せば生成せず評価できる
（LLM 出力の切り分け用）。

---

## 3. コマンド

```bash
# 24 ケースを全部回す（失敗があれば終了コード 1）
python -m eval

# 内訳を表示する
python -m eval --verbose

# 機械向け JSON
python -m eval --out eval/results/202610.json

# 文字数の実測（レンジ決定の根拠）
python -m eval.metrics.length --measure

# ケース定義の生成 / 乖離チェック
python -m eval.cases.build_cases
python -m eval.cases.build_cases --check
```

---

## 4. CI ゲートの整理

### 4.1 PR ゲート（自動指標のみ・1 分以内）

| # | ゲート | 閾値 | 現状 |
|---|---|---|---|
| G1 | **pytest 全 suite** | 全部 pass | `pytest -q` 相当 |
| G2 | **文字数の帯**（76 年 × 3 モード = 228 サンプル） | 帯外 0 件 | **0 件**（帯 800〜1500） |
| G3 | **CheckList 違反**（`heading_order` / `required_segments` / `prompt_leftover` / `japanese_ratio` / `song_duplication`） | 0 件 | **0 件** |
| G4 | **fact score の `fail`**（正本に無い番組名の断定） | 0 件 | **0 件** |

> **PR ゲートに載せないもの**（意図的な除外。理由を明記する）:

| 除外項目 | 理由 |
|---|---|
| `unfulfilled_preannounce` | `core/fallback.py:341` は **S1/S3 所有**。S2 は編集せず**検出のみ**を行う。現状 `normal` モード 8 ケースすべてで検出されるが、PR を落とすと所有範囲外の修正を要求することになる。**nightly の報告項目**とする。 |
| fact score の `warn` | S1 の `docs/facts_registry.md` 5.3 が定める線引き（番組表 = fail / 台本自由文 = warn）に従う。既存の warn 5 件は**ゲートを落とさない**。 |
| fact score の**閾値** | **ベースラインが未確立**。現状 24 ケースで平均 97.88 / 最小 76.92。`care_recreation/1950` は 1951 年リリースの曲を含むため 76.92 で閾値を下回る。S1/S3 が `FALLBACK_SONGS` を直した後、閾値を 90 以上に引き上げる。 |
| 24 ケースの全走査 | 24 ケースの生成は約 2 秒だが、**人間評価の募集は人手**なので CI には載せない。 |

### 4.2 Nightly

| # | 項目 | 実行者 |
|---|---|---|
| N1 | `python -m eval --verbose --out eval/results/<date>.json` | 自動 |
| N2 | 文字数の実測 `python -m eval.metrics.length --measure`（分布の妥当性を確認） | 自動 |
| N3 | `python -m eval.cases.build_cases --check`（正本と生成物の乖離） | 自動 |
| N4 | `python scripts/validate_facts.py`（S1 の事实ゲート。S1 所有） | 自動 |
| N5 | **Best-Worst 人間評価の募集と結果の可視化** | **人手**（手順書: `eval/bww/README.md`） |

### 4.3 既存 pytest スイートへの追加方法

`pytest.ini` は **S10 所有**なので S2 は編集しない。
`eval/` には `test_*.py` を置かない（`testpaths = tests` の外側になるため、
`pytest -q` では.collect されない）。**`tests/` 側のファイルから `eval` を import する**
ことで既存スイートに載る。以下 2 ファイルがそれを行っている。

```bash
# 評価ハーネス自体のテストだけを回す
python -m pytest tests/test_eval_harness.py -q

# 既存の 2 ファイル（レンジ判定と未履行予告）
python -m pytest tests/test_script_length.py tests/test_content_regression.py -q

# ハーネスを既存スイートに載せる確認（何か壊れていないか）
python -m pytest tests/test_eval_harness.py tests/test_script_length.py tests/test_content_regression.py -q

# 24 ケースを pytest の中で回したい場合（人手・約 2 秒）
python -m pytest -q -k "eval_harness or script_length or content_regression"
```

CI への追加を `.github/workflows/` に入れる場合の設定例（S10 と調整すること）:

```yaml
# 24 ケースの Nightly ジョブ
- name: eval nightly
  run: python -m eval --verbose --out eval/results/$(date +%Y%m%d).json
  continue-on-error: true   # 未履行予告の既知欠陥で落ちても nightly は止めない
```

---

## 5. 現状のベースライン（2026-09-30 実測）

| 指標 | 値 |
|---|---|
| fact score 平均 | **97.88**（24 ケース） |
| fact score 最小 | **76.92**（`care_recreation/1950`） |
| fact score `fail` | **0 件** |
| CheckList 違反 | **8 件**（すべて `normal` の `unfulfilled_preannounce`） |
| 文字数の帯外 | **0 件 / 228** |
| 文字数 p5 / p50 / p95 | **1058 / 1339 / 1476** |

**このベースラインが最初の成果物である。** プロンプトを変えたら ±2 点は
意味がないことを統計的に言いたい（提案 489 行）。

---

## 6. 所有境界

**触ってはいけないもの（他サブエージェントの所有）**:

- `retro_radio/core/*`（S1/S3） — **`eval/` は読み取りのみ**
- `retro_radio/server.py`（S5）
- `retro_radio/config.py`（S4） — import するだけ
- `static/*`（S6/S7/S9）
- `scripts/validate_facts.py` / `retro_radio/core/facts/*`（S1） — 読み取りのみ
- `pytest.ini`（S10）

**S2 が所有するもの**: `eval/` 全体・`tests/test_content_regression.py`・
`tests/test_script_length.py`・`tests/test_eval_harness.py`・`docs/model_card.md`。
