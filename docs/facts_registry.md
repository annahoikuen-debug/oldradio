# 事実レジストリ（正本）

**提案⑦「事実整合レイヤー」の実装ドキュメント。**

対象: `plans/evidence_based_improvement_proposals.md` 324〜390 行。

---

## 1. なぜこれが必要か

このアプリは**記憶の内容**を扱う。回想の文脈の中で誤った事実が一度流れると、
集団の中で反復再生され、自己伝記記憶に統合される。したがってこれは表示上の
バグではなく**安全要件**である。

- Mitchell, Thompson, & Lewis (2012). 年配者は若年者に比べて誤情報に強く
  影響されやすく、「誤りだと気づく抵抗」が弱い。**本作の主要ターゲットは
  80 歳代。**
- Lewandowsky et al. (2012) / Ecker & Lewandowsky (2022). 訂正には
  **backfire 効果**がある。「後で訂正します」ではなく**「最初から載せない」**
  という設計判断の根拠になる。

このため、本アプリが語れる事実は **1 事実 1 レコード**の正本
（`retro_radio/core/facts/*.json`）に載っているものに限られる。

---

## 2. ファイル構成

| ファイル | 責務 |
|---|---|
| `retro_radio/core/facts/programs.json` | **正本**。テレビ・ラジオ番組の事実 |
| `retro_radio/core/facts/__init__.py` | ローダと resolver。外部依存なし |
| `scripts/validate_facts.py` | 検証スクリプト（手動実行）。`validate_all()` が公開 API |
| `tests/test_facts_registry.py` | 回帰防止（誤認の再発を止める） |
| `retro_radio/core/fallback.py:502-541` | `RADIO_PROGRAMS_BY_DECADE`（後方互換の**非正規化ビュー**） |

**正本は JSON 1 ファイルだけ**です。`core/fallback.py` に番組データを
直接書かないでください（それが今回是正した分散の根本原因です）。

---

## 3. レコードのスキーマ

### 3.1 必須フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `id` | `str` | 一意な識別子。全体で重複してはならない |
| `kind` | `"tv_program"` \| `"radio_program"` | 種別 |
| `title` | `str` | 番組名（正式表記） |
| `network` | `str` | 放送局 |
| `start_time` | `str` | 放送開始時刻（`"HH:MM"`） |
| `valid_from` | `int` | 放送開始年 |
| `valid_to` | `int \| null` | 放送終了年。`null` は**継続中または終了年未確定** |
| `duration_min` | `int` | 放送時間（分） |
| `claim_ja` | `str` | 正本の主張。出典つき。読み上げ原稿には**載せない** |
| `description_ja` | `str` | 読み上げ用の説明文。4 桁西暦を書かない |
| `source` | `str` | 出典。**必須**。欠落は fail |
| `confidence` | `"verified" \| "unverified"` | 一次文献照合の有無 |

### 3.2 任意フィールド

| フィールド | 型 | 既定値 | 説明 |
|---|---|---|---|
| `source_url` | `str \| null` | `null` | 出典 URL。**本タスクは全て `null`**（後述 3.4） |
| `note_ja` | `str` | `""` | 運用メモ。`unverified` なら必ず記入 |

### 3.3 `description_ja` の書き方（重要）

読み上げ原稿に載る文言は `description_ja` です。**4 桁の西暦を書いてはいけません。**

```jsonc
// NG: 対象年を超えると番組全体が番組表から消える
"description_ja": "1975年に始まった歌謡曲番組"

// OK: 年代バケット表記は対象年を超えない
"description_ja": "1970年代にNETテレビで放送された歌謡曲番組"
```

これは `tests/test_facts_registry.py::test_description_ja_never_contains_a_bare_four_digit_year`
と `scripts/validate_facts.py::check_record_periods_against_years` で機械的に
検査されます。

### 3.4 `source` と `source_url` の扱い（本タスクの制約）

**このタスクはネットワークアクセスができませんでした。したがって:**

- `source_url` は **全レコードで `null`**。`tests/test_facts_registry.py::test_no_fabricated_source_urls`
  がこれを固定しています。
- `source` には**文献名のみ**を書きます（例: `ja.wikipedia:ザ・ヒットパレード (テレビ番組)`）。
- 確実に書けないものは文献名を推測で埋めず、`confidence: "unverified"` と
  `note_ja`（`[要確認]`）で**未照合であることを明示**します。

> **虚偽の出典を作ってはならない。**
> 推測した URL を書くと「出典があるように見える」が実際は捏造になり、
> 本提案の主題がそれ自体で崩れます。**`source` 欠落は fail**、
> **`unverified` は warn** にして、欠落と未照合を区別します。

### 3.5 `records` の並び順

`records` は **`valid_from` の昇順**に並べます（同時開始なら安定順）。

これは**決定性の前提条件**です。resolver は並び順をそのまま使うので、
並べ替えると `resolve_program(year)` の結果が変わり、既存テスト
（`test_program_guide_is_deterministic`）が壊れます。
`tests/test_facts_registry.py::test_registry_is_sorted_by_valid_from` が検査します。

---

## 4. レジストリの使い方（Python API）

```python
from retro_radio.core.facts import (
    load_facts,           # 正本の全レコード
    facts_valid_for,      # その年に放送中の番組を含む全事実
    programs_for_year,    # その年に有効なテレビ/ラジオ番組のみ
    resolve_program,      # その年に有効な番組のうち index 番目（決定的な回転）
    fact_by_id,           # id から引く
    future_year_mentions, # 4 桁西暦 + 「○年代」の両方で未来年を検出
)
```

### 4.1 resolver の契約

`resolve_program(year, index=None)` は `index` 省略時に `year` を使います。
これは旧 `_historical_pick` の `eligible[year % len(eligible)]` と同じ契約で、
**同じ入力なら常に同じ出力**を保ちます。

```python
resolve_program(1975)   # 有効な番組の [1975 % len] 番目
resolve_program(1975, 0)  # 有効な番組の先頭
```

### 4.2 `core/fallback.py` 側の用法

```python
# 現状（意図的な残置）：RADIO_PROGRAMS_BY_DECADE は後方互換のために残す
#   既存テストが参照するので**削除禁止**。ただし読み取り専用の互換層で、
#   唯一の解決ルールではない。
RADIO_PROGRAMS_BY_DECADE = {
    key: [_schedule_from_fact(r) for r in programs_for_year(key)]
    for key in (1950, 1960, ..., 2020, 2025)
}

# 実際の解決はここ（バケット丸めを使わない）
_decade_programs(year, limit)      # -> programs_for_year(year)[:limit]
_historical_pick(year)             # -> _get_programs_for_year(year) を year で回転
```

**2025 キーについて**: 以前は `RADIO_PROGRAMS_BY_DECADE` に 2025 キーがなく、
2020 バケットが 1 件だけなので `year % 1 == 0` となり **2020〜2025 年すべてに
同じ番組**が出ていました。レジストリ生成に変えたことで解消しています。

---

## 5. 検証スクリプト（fail / warn）

> **CI ゲートではありません。** `.github/workflows/ci.yml` に
> `validate_facts.py` を呼ぶステップは**存在しません**。
> 同じ検査は `tests/test_facts_registry.py` が
> `scripts.validate_facts.validate_all()` を直接 import して pytest 経由で実行します。
> したがって回帰は**間接的に**検出されますが、このスクリプトの終了コードを
> CI が直接見ているわけではありません。

### 5.1 コマンド（手動実行）

```bash
python scripts/validate_facts.py            # 終了コード: fail があれば 1
python scripts/validate_facts.py --quiet    # warn の内訳だけ表示
python scripts/validate_facts.py --json     # 機械向け JSON
```

### 5.2 検査項目

| 検査 | 内容 |
|---|---|
| `missing-field` | 必須フィールドの欠落 |
| `bad-kind` / `bad-confidence` | 許可値以外の値 |
| `inverted-period` | `valid_to < valid_from` |
| `missing-source` | **`source` 欠落（fail）** |
| `unverified` | `confidence: "unverified"`（**warn**） |
| `duplicate-id` | レコード id の重複 |
| `duplicate-in-bucket` | バケット内のタイトル重複 |
| `network-conflict` | 同名番組が複数の放送局を主張 |
| `stale-fact-in-guide` | 対象年により前の歴史番組が番組表に出た（**fail**） |
| `future-year-in-guide` | 番組表に対象年より後の年（**fail**） |
| `stale-fact-in-script` | 対象年により前の番組が台本に出た（**warn**、後述） |
| `future-year-in-script` | 台本に対象年より後の年（**warn**、後述） |

### 5.3 fail と warn の線引き

- **番組表**（`get_program_guide`）はレジストリのレコードだけが生成源なので、
  違反は **fail**。
- **台本の自由文**は `REMINISCENCE_DATA`（クイズ）や LLM 自由生成など
  **レジストリ外のデータ**も混ざっています。そこを fail にすると
  所有範囲外の修正を要求してしまうため、**warn** として可視化します。

  現在の warn 例（`REMINISCENCE_DATA` の管轄外、放置している）:
  - `care_recreation/1970`: オールナイトニッポン（1971 年開始）
  - `care_recreation/2000`: 「2001年」
  - `care_recreation/2010`: 「2011年」「2012年」
  - `care_recreation/2011`: 「2012年」

  これらは**検出はできている**。レガシー自由文を正本に昇格させるか、
  自由文の生成そのものを廃止するかを別タスクで決める必要があります。

### 5.4 終了コード

- `fail` が 1 件以上 → `1`
- `warn` のみ → `0`

---

## 6. 事実を追加する方法

1. **一次文献で確かめる。** ネットアクセス不能用の場面では、
   `confidence: "unverified"` + `note_ja: "[要確認] ..."` を書いて**スキップしない**。
2. `retro_radio/core/facts/programs.json` の `records` に追加する。
   **`valid_from` 昇順の位置**に差し込む。
3. `description_ja` は 4 桁西暦を書かない。年代表記を使う。
4. `source` を必ず書く（文献名でも可）。
5. 実行して確認:

   ```bash
   python scripts/validate_facts.py
   python -m pytest tests/test_facts_registry.py -q
   ```

6. **新しい年の追加は誤りの上に重なる**（Lewandowsky & Ecker 2012）。
   訂正 UI を追加するのではなく、`note_ja` に訂正履歴を追記する運用にする。

---

## 7. `[要確認]` 一覧（一次文献照合が必要なもの）

**現在の正本は 17 レコードすべて `confidence: "unverified"` です。**
これは意図的で、**ネットアクセスができない以上、確認していないものを
確認済みと書くことが最も危険な誤り**だからです（虚偽の出典を作らない）。

| id | 特に確認が必要な点 |
|---|---|
| `japan_countdown_1984` | **最も要確認**。開始年・終了年・放送時間が一次文献で確認できていない。`valid_from` / `valid_to` / `start_time` すべて参考値 |
| `sportacent_2004` | 開始年は「2004年頃」とされ正確な年が未確定。終了年も未確認（`valid_to: null`） |
| `packin_music_1970` | 終了年 1994 年。終了年が違っても `valid_from` と `description_ja` は変わらない |
| `asayan_1988` | 途中の放送休止（1992-1993 年の空白）を含んだ期間設定 |
| `hachiji_dayo_1968` | 開始年・終了年。旧データは時刻と「深夜」の記述が矛盾していた |
| `uta_parade_1977` | 放送終了年と局の変遷（NET テレビ → 日本テレビ） |
| `nhk_radio_first_1925` | 現行表記（NHK ラジオ第一 / NHK|第1放送）と終了年 |
| 他 10 件 | 放送期間と放送時間の確認 |

### 付録 C の書誌について（本タスクのスコープ外）

`plans/evidence_based_improvement_proposals.md` の**付録 C（参照文献）**に
ある Groarke et al. (2018) などの書誌は、**事実レジストリの管轄外**です。

- 書誌の `[要確認]` は**論文の書誌情報**（巻・号・頁）の問題であり、
  番組の事実の検証とは別物です。
- 事実レジストリが使うのは**各レコードの `source`** であり、これは
  番組そのものの一次資料（放送局の公式ページ、辞典項目など）です。
- したがって**付録 C の書誌照合は本タスクでは扱っていません**。
  先行して着手する場合は必ず一次文献で照合すること
  （付録 C 冒頭の「書誌情報の確度が低いものは `[要確認]` を付す。
  着手前に必ず一次文献で照合すること。」に従う）。

---

## 8. 効果の検証指標

| 指標 | 現在 |
|---|---|
| fact validator の `fail` 数 | **0**（pytest 経由で検証） |
| fact validator の `warn` 数 | 22（`unverified` 17 + 台本自由文 5） |
| `source` を欠くレコード | **0** |
| 誤認 4 件の再発 | 0（`tests/test_facts_registry.py` が固定） |

**未計測のもの**: サンプリング外部評価（施設職員・ご家族による 20 本の台本の
事実誤認率）。これは提案⑨（評価ハーネス）と共通インフラ。
