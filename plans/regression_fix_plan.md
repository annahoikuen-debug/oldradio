# リグレッション防止計画書（36 ステップ）

作成: 2026-10-01 / 対象: retro_radio / 基準コミット: `eca1eb0`（HEAD）

---

## 0. この計画について

### 0.1 目的

前回レビュー（`docs/final_review_report_2026-10-01.md`）は「2898 件緑」を達成した。
しかし**その緑が守っているものが(Project が最重要視している)契約と一致していない**。

本計画は 2 つの/compose れた”而，专门针对:

- **(A) 契約違反**: コードが「守ると全部書いている」保証を実装していない
- **(B) 検査の穴**: 壊れても緑のままになるテスト・設定

各ステップは**低性能 LLM でも 1 ステップだけ見て実装できる粒度**に分割している。
1 ステップ = 1 ファイル（または 1 関数）+ 1 テストファイル。

### 0.2 すべての数値は本リポジトリで実測した値

| ゲート | 実測結果（2026-10-01 / `eca1eb0`） |
|---|---|
| `python -m pytest -q` | 2918 passed / 0 failed |
| `python -m eval --offline --threshold 80` | EXIT 0（24/24、平均 100.0） |
| `flake8 --config .github/flake8-app-baseline.ini retro_radio` | EXIT 0 |
| 同じ baseline を**外した**場合 | **191 件の違反**（ベースラインが隠蔽） |
| `scripts/validate_songs.py` | fail: 0 / warn: 3061（内 unverified: **3030**） |

### 0.3 前回レビューとの重要な訂正（誤って報告していた箇所）

**訂正 1: 「テストが 9〜13 件赤だった」は誤り。**

監査中に `RETRO_RADIO_GEMINI_API_KEY` を `Remove-Item Env:` で消して試験したため
`conftest.py:27` の `os.environ["RETRO_RADIO_GEMINI_API_KEY"] = ""` が上書きされ、
実 LLM 経路	expected どおりに不走っただけだった。
`tests/test_script_song_alignment.py` の簡体字混入も、**別エージェントが監査中に
17:46〜18:10 に修正した**ため 18:36 時点では緑。
**現在憲は 2918 件すべて緑。** 以下の計画は赤の修正ではなく**緑のまま Aft く Contract のための計画**。

**訂正 2: 環境の Gemini キーは無害。** `conftest.py:27` が `= ""` で必ず上書きする。
実行ごと結果が変わる、という主張は誤り。

### 0.4 コードブロックの凡例（重要）

本計画書のコードブロックには 2 種類ある。**区別して使うこと。**

| フェンス | 意味 | 使い方 |
|---|---|---|
| ` ```python ` | **ファイル全体**（そのまま新規作成、または既存ファイルを丸ごと置換） | ファイルとして書き出す |
| ` ```py-fragment ` | **差し込み片**（既存の関数・クラスの**内部**に貼る） | 該当クラスの**中**に、インデントを保って挿入する |

` ```py-fragment ` は単体では `SyntaxError` になる（インデント付きで書いてあるため）。
**新しいファイルを作らない。**

### 0.5 1 ステップの進め方（低性能 LLM 向け手順）

各ステップは必ず次の順で進める。飛ばさない。

1. **読む**: ステップの「対象ファイル」を行番号つきで読む
2. **測る**: 「検収」コマンドを**修正前に** 1 回実行し、状態を記録する
3. **直す**: コードブロックを適用する
4. **測り直す**: 同じ「検収」コマンドを再実行する
5. **報告**: 期待と実際が一致したかだけを書く。**一致しなければ先に報告する**（次へ進まない）

**途中で red になったら、その場で止まって報告する。**
「テストが古いので直した」ことだけ Herschrell かない。**赤くなった原因を説明すること。**

### 0.6 実行順序（依存グラフ）

```
Phase 0（安全弁）      S1〜S6
   ↓
Phase 1（P0 契約違反） S7〜S16
   ↓
Phase 2（P1 検査の穴） S17〜S24
   ↓
Phase 3（P2 セキュリティ）S25〜S31
   ↓
Phase 4（P3 衛生）     S32〜S36
```

**必ず直列で、1 ステップごとに pytest を回す。**
Step 7〜9（選曲の一本化）は他のどのステップより先に完了させること。
ここを通らないと、以降のテストが「どの曲集合が正しいのか」を定義できない。

---

## Phase 0 — 安全弁（ここを飛ばすと Step 7 以降が台無しになる）

### Step 1: 検証スクリプトを 1 か所にまとめる

**やること**
`scripts/verify_all.py` を新規作成する。以下の 6 つを順に実行し、
どれか 1 つでも非ゼロなら非ゼロを返す。

1. `python -m pytest -q`
2. `python -m flake8 tests`
3. `python -m flake8 --config .github/flake8-app-baseline.ini retro_radio`
4. `python -m flake8 eval`
5. `python -m eval --offline --threshold 80`
6. `python scripts/validate_songs.py --quiet` / `python scripts/validate_facts.py`

** Schrift（正確な形式）**

```python
#!/usr/bin/env python3
"""全ゲートの綜合検証。どれか 1 つでも失敗したら非ゼロを返す。

Codex が使う。CI でも同じ順序で実行する。
"""
import subprocess, sys, pathlib, os

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)

GATES = [
    ("pytest", [sys.executable, "-m", "pytest", "-q"]),
    ("flake8 tests", [sys.executable, "-m", "flake8", "tests"]),
    ("flake8 app", [sys.executable, "-m", "flake8",
                    "--config", ".github/flake8-app-baseline.ini", "retro_radio"]),
    ("flake8 eval", [sys.executable, "-m", "flake8", "eval"]),
    ("eval gate", [sys.executable, "-m", "eval", "--offline", "--threshold", "80"]),
    ("validate_songs", [sys.executable, "scripts/validate_songs.py", "--quiet"]),
    ("validate_facts", [sys.executable, "scripts/validate_facts.py"]),
]

failed = []
for name, cmd in GATES:
    print(f"=== {name} ===", flush=True)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        failed.append(name)
        print(f"!!! {name} FAILED (exit {result.returncode})", flush=True)

if failed:
    print(f"\nFAILED GATES: {', '.join(failed)}")
    sys.exit(1)
print("\nALL GATES PASSED")
sys.exit(0)
```

**新建テスト: 不要**（Step 2 でテストにする）

**検収**: `python scripts/verify_all.py` が EXIT 0

---

### Step 2: 検証スクリプト自体のテスト

**やること**
`tests/test_verify_script.py` を新規作成。以下を検証する。

```python
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]


def test_verify_all_defines_the_seven_gates():
    text = (ROOT / "scripts" / "verify_all.py").read_text(encoding="utf-8")
    for token in ("pytest", "flake8", "eval --offline",
                  "validate_songs", "validate_facts"):
        assert token in text, token


def test_verify_all_returns_nonzero_on_failure(monkeypatch):
    """1 ゲートでも失敗したら非ゼロを返す（文字列検査ではなく実挙動）"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "verify_all", ROOT / "scripts" / "verify_all.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.GATES = [("dummy", [__import__("sys").executable, "-c", "raise SystemExit(3)"])]
    assert module.run_all() == 1
```

**注意**: 上記は `run_all()` という関数に分离した版-write。Step 1 の雛形を
`run_all() -> int` を返す形に直してから使うこと（`sys.exit` を直接呼ぶと
テストから呼び出せない）。

**検収**: `python -m pytest tests/test_verify_script.py -q` が緑

---

### Step 3: 現在の 191 件の lint 違反の記録をテストに固定する

**やること**
「ベースライン外の違反が 0 件ではない」ことを隠さないために、
現状の件数を `tests/test_lint_baseline_ratchet.py` に記録する。

```python
"""lint ベースラインは違反を隠す。増えたら落とすラチェット。

`.github/flake8-app-baseline.ini` の `extend-ignore` は
**コードベース単位**で除外するため、ここに書いたコードは
新たに発生しても CI が緑のままになる（実測）。
したがって「今の何件が残っているか」を明示し、
減Bootstrap 的时候だけ更新する。
"""
import subprocess, sys, pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Phase 4 の各ステップで必ず減る値。**増やしてはいけない**。
ALLOWED = 191


def _violations() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "flake8", "--max-line-length=100",
         "--extend-ignore=E203,W503", "retro_radio"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return len([ln for ln in result.stdout.splitlines() if ln.strip()])


def test_lint_violations_never_increase():
    current = _violations()
    assert current <= ALLOWED, (
        f"ベースライン外の lint 違反が {ALLOWED} → {current} に増加した。"
        "新しい違反を持ち込まないこと。"
    )


def test_baseline_config_is_still_required():
    """ベースライン設定ファイルが消えていない（消えると実違反が CI で出る）"""
    assert (ROOT / ".github" / "flake8-app-baseline.ini").is_file()
```

**検収**: `python -m pytest tests/test_lint_baseline_ratchet.py -q` が緑、
かつ ALLOWED を创作的でない値にすること（実測値 191）

---

### Step 4: 中盤 import（E402）の記録

**やること**
`retro_radio/core/tts.py:99-102` はファイル末尾付近で

```python
import hashlib
import shutil
import time
from pathlib import Path

TTS_CACHE_DIR = Path(tempfile.gettempdir()) / "retro_radio_tts_cache"
TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
```

と **import 時副作用**_balbal している（E402 でベースラインにより隠されている）。

この副作用は `tests/conftest.py:136-139` に「`TTS_CACHE_DIR.mkdir()` を先に
作っておかないと、パスを後から patch した-held 全部のキャッシュ書き込みが壊れる」
と記録されている。

**やること**: この事実をテストで固定する（修正は Step 33 で行う）。

`tests/test_tts_import_side_effects.py` を新規作成：

```python
"""`core/tts.py` は import 時にディレクトリを作る。

この副作用を理由に `tests/conftest.py:136-139` が
`TTS_CACHE_DIR` を先に mkdir している。壊すと全テストが落ちるため、
現状を明示的に固定する。
"""
import importlib
import pathlib


def test_tts_cache_dir_exists_after_import():
    import retro_radio.core.tts as tts
    assert tts.TTS_CACHE_DIR.is_dir(), tts.TTS_CACHE_DIR


def test_tts_module_can_be_reimported_without_error():
    """再 import が例外を出さない（mkdir の exist_ok=True が効いている）"""
    import retro_radio.core.tts as tts
    importlib.reload(tts)
    assert tts.TTS_CACHE_DIR.is_dir()
```

**検収**: 2 件緑。`pytest.ini` の conftest と競合しないこと

---

### Step 5: テスト決定性を保証する環境固定の検査を追加する

**やること**
`tests/conftest.py:27` は `os.environ["RETRO_RADIO_GEMINI_API_KEY"] = ""` で
キーを消しているが、**`monkeypatch.delenv` を追加しても二重安全**になる。
更重要的是、`os.environ.setdefault` を使うと開発機の `.env` に依存する。

**やること**: `tests/test_hygiene_regression.py` に 1 件追加する。

```python
def test_conftest_forces_gemini_key_empty_not_setdefault():
    """Gemini キーは `= ""` で**上書き**されなければならない。

    `setdefault` だと開発機の環境変数が残るため、
    実 LLM 経路が走るテストが非決定的に化す（実測）。
    """
    import ast
    text = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Subscript)
                    and ast.literal_eval(target.slice) == "RETRO_RADIO_GEMINI_API_KEY"):
                assert isinstance(node.value, ast.Constant), (
                    "RETRO_RADIO_GEMINI_API_KEY は os.environ[...] = \"\" の形で"
                    "上書きされなければならない（setdefault は不可）"
                )
```

**検収**: 緑（現状のコードで緑になることを確認する）

---

### Step 6: Phase 0 の検収

```powershell
python scripts/verify_all.py
python -m pytest tests/test_verify_script.py tests/test_lint_baseline_ratchet.py tests/test_tts_import_side_effects.py -q
```

**完了条件**: EXIT 0、追加 6 テスト緑

---

## Phase 1 — P0: 契約違反（ここが本計画の中核）

### 【背景】why Step 7〜9 が最優先か

このプロジェクト自身が 3 箇所で同一の契約を宣言している。

- `retro_radio/server.py:1319`: 「1 番組ぶんの曲を選ぶ（**原稿とプレイリストの唯一の情報源**）」
- `retro_radio/core/fallback.py:637`: 「原稿に埋め込む曲を決める。**1 本の事実源**」
- `retro_radio/core/songs/__init__.py:1-8`: 「このアプリが流せる曲の**正本は songs.json 1 ファイル**。ここに無い曲はこのアプリでは選曲しない」

**実測: 3 つの別経路が 3 つの別の曲集合を返す。**

```
_catalog_allowlist(1975, 18)      → 18 曲（正本カタログ経由）
select_program_songs(1975, 5)     →  5 曲（36 曲静态マスター経由）
両者の交集                      → 0 曲
```

**実害（実測）**: `songs=None` のとき

```
原稿が名指しした曲: いい日旅立ち / また明日遊ぼうね / 上を向いて歩こう / いつでも夢を
プレイリストの曲  : 水色のときめき / 聞きたいことがあるの / 旅 / プロポーズ作戦 / …
交集             : 0 曲
```

`retro_radio/core/preview_resolver.py:6-16` は「介護施設で『卒業写真（荒井由実）』と
読み上げながら別の曲が流れるのは、記憶が訂正される効果そのものなので許されない」
と書いており、これは**このプロジェクト自身が最重大欠陥として定義した問題**である。
現在その状態が再現している。

---

### Step 7: 静的マスターを正本カタログに寄せる（`select_program_songs`）

**対象ファイル**: `retro_radio/core/fallback.py:293-331`

**現状の問題**
`select_program_songs` は `_bucket_pool`（`:183-194`）経由で
`FALLBACK_SONGS`（`:28-128`、**36 曲だけ**）を参照する。
正本カタログ（`core/songs/songs.json`、3030 曲）を見ていない。

**やること**
`select_program_songs` を書き換え、`core.songs` の正本カタログを参照するようにする。

```py-fragment
def select_program_songs(
    year: int, count: int = 3, *, exclude: Optional[set] = None
) -> List[Tuple[str, str]]:
    """**決定的な**選曲。原稿とプレイリストが同じ 1 本の事実源を見る。

    _selection_songs と二重定義にならないよう、本関数が唯一の選曲口。
    """
    pinned = [s for s in current_pinned_songs() if s not in (exclude or set())]
    if pinned:
        return pinned[: max(1, int(count))]
    if not validate_year_range(year):
        year = settings.default_year
    count = max(1, int(count))

    # 正本カタログから導出する。静的マスター（FALLBACK_SONGS、36 曲）は
    # 参考データであり、選曲の根拠にはしない。
    from .songs import pool_for_year, song_key

    try:
        records = pool_for_year(int(year), count + len(exclude or ()))
    except Exception:  # noqa: BLE001 - カタログが読めない場合の縮退
        logger.exception("正本カタログから選曲できませんでした: year=%s", year)
        records = []

    picked: List[Tuple[str, str]] = []
    seen = set()
    for record in records:
        title = str(record.get("title", "")).strip()
        artist = str(record.get("artist", "")).strip()
        if not title:
            continue
        key = song_key(title, artist)
        if key in seen or key in (exclude or set()):
            continue
        seen.add(key)
        picked.append((title, artist))
        if len(picked) >= count:
            break

    if not picked:
        # カタログが空のときのみ静的マスターへ退避する（尽量使わない）
        ok, future = partition_by_release_year(year, exclude=exclude)
        picked = list(ok)[:count] or list(future)[:count]
    if not picked:
        picked = [get_fallback_song(year, exclude=exclude)]
    return picked[:count]
```

**注意**
- `song_key` は `core.songs` の関数。import は遅延（循環参照回避）
- `exclude` の判定は `song_key` 正規化で行う（表記ゆれ吸収）
- **決定性を保つ**こと。`random.shuffle` を入れない

**検収**
```powershell
python -m pytest tests/test_song_alignment.py tests/test_content_regression.py tests/test_music_profile.py tests/test_radio_program_structure.py -q
python -m eval --offline --threshold 80
```

---

### Step 8: `_catalog_allowlist` を `select_program_songs` に委譲させる

**対象ファイル**: `retro_radio/core/script_generator.py:363-394`

**現状の問題**
`_catalog_allowlist` が `SongSelector(history=None, rng=Random(year)).peek(...)` と
**独立に実装**されており、`select_program_songs` と別の曲集合を返す。

**やること**
`_catalog_allowlist` の実装を消し、`select_program_songs` に委譲させる。

```python
def _catalog_allowlist(year: int, count: int = 6) -> List[Tuple[str, str]]:
    """選曲結果が渡されなかったときの許可リストを**正本カタログ**から作る。

    選曲そのものは :func:`retro_radio.core.fallback.select_program_songs`
    （唯一の選曲口）に委譲する。ここで別の実装を持つと、
    「原稿が名指しする曲集合」と「プレイリストの曲集合」が食い違い、
    介護施設で『A を読み上げながら B が流れる』状態になる（実測）。
    """
    from .fallback import select_program_songs

    try:
        return select_program_songs(int(year), int(count))
    except Exception:  # noqa: BLE001 - 縮退は静かにせずログに残す
        logger.exception("許可リストの導出に失敗しました: year=%s", year)
        return []
```

**検収**
```powershell
python -m pytest tests/test_song_alignment.py tests/test_script_song_alignment.py tests/test_eval_harness.py -q
python -c "from retro_radio.core.script_generator import _catalog_allowlist; from retro_radio.core.fallback import select_program_songs; a=set(t for t,_ in _catalog_allowlist(1975,6)); b=set(t for t,_ in select_program_songs(1975,6)); print('overlap', len(a&b), 'of', len(b))"
```

---

### Step 9: 交差チェックをリグレッションテストとして固定する【最重要テスト】

**やること**
`tests/test_song_selection_single_source.py` を新規作成する。
**これが本計画で最も価値が高いテスト。**

```python
"""選曲経路が 1 本であることを固定する。

`retro_radio/server.py:1319`「原稿とプレイリストの唯一の情報源」、
`retro_radio/core/fallback.py:637`「1 本の事実源」、
`retro_radio/core/songs/__init__.py:1-8`「正本は songs.json 1 ファイル」
という 3 箇所の契約を機械的に検証する。

修正前の実測: `_catalog_allowlist` と `select_program_songs` の
交集は 0 曲。原稿が名指しした曲とプレイリストの曲も 0 曲。
"""
import re

import pytest

from retro_radio.core.fallback import select_program_songs
from retro_radio.core.song_selector import SongSelector
from retro_radio.core.script_generator import (
    _catalog_allowlist,
    generate_radio_script,
)

YEARS = [1950, 1960, 1964, 1975, 1985, 1996, 2005, 2015, 2025]


def _named_in(script: str) -> set:
    """原稿が曲名として名指しした曲を取り出す。"""
    from retro_radio.core.songs import load_songs
    titles = {str(r["title"]) for r in load_songs()}
    found = set(re.findall(r"「([^」]{2,40})」", script))
    return found & titles


@pytest.mark.parametrize("year", YEARS)
def test_allowlist_and_selection_are_the_same_songs(year):
    """許可リストと選曲結果が同じ曲集合であること。"""
    allowlist = {t for t, _a in _catalog_allowlist(year, 6)}
    selected = {t for t, _a in select_program_songs(year, 6)}
    assert allowlist == selected, (
        f"{year}年: 許可リストと選曲結果が一致しません。"
        f"差分={allowlist ^ selected}"
    )


@pytest.mark.parametrize("year", [1975, 1985, 2005])
def test_script_never_names_a_song_outside_the_catalog(year):
    """原稿の曲名は必ず正本カタログに含まれる（捏造防止）。"""
    script = generate_radio_script(year, 9, 24, songs=None)
    named = _named_in(script)
    catalog = {str(r["title"])
               for r in __import__("retro_radio.core.songs", fromlist=["x"]).load_songs()}
    assert named <= catalog, named - catalog


@pytest.mark.parametrize("year", [1975, 1985, 2005])
def test_script_named_songs_are_a_subset_of_the_playlist(year):
    """原稿が名指しした曲とプレイリストの曲に重複があること。

    0 なら『A を読み上げながら B が流れる』状態。
    """
    script = generate_radio_script(year, 9, 24, songs=None)
    named = _named_in(script)
    playlist = {str(r["title"])
                for r in SongSelector(history=None).peek(year, 18)}
    assert named, f"{year}年: 原稿が曲名を一切名指ししていない"
    overlap = named & playlist
    assert overlap, (
        f"{year}年: 原稿が名指しした曲 {sorted(named)} と"
        f"プレイリスト {sorted(playlist)} に共通曲が無い。"
        "介護施設では『別の曲が流れる』ことになる。"
    )


def test_selection_is_deterministic():
    """同じ年・同じ本数なら必ず同じ曲（シャッフルしない）。"""
    a = select_program_songs(1975, 6)
    b = select_program_songs(1975, 6)
    assert a == b


def test_exclude_is_respected():
    """exclude に含まれる曲を選ばない。"""
    first = select_program_songs(1975, 6)
    excluded = {first[0]}
    second = select_program_songs(1975, 6, exclude=excluded)
    assert second[0] not in excluded
```

**検収**
```powershell
python -m pytest tests/test_song_selection_single_source.py -q   # 修正前: 複数赤
python -m pytest -q                                             # 全体緑
```

---

### Step 10: `server._select_program_songs` を単一情報源に寄せる

**対象ファイル**: `retro_radio/server.py:1318-1335`

**やること**
プレイリスト側の選曲も `select_program_songs` と同じ正本を通ることを確認し、
二重実装があれば削除する。`SongSelector`（ローテーション付き）は
**履歴による並び替え**だけを担当し、候補集合的选择は正本カタログに委譲させる。

```python
def _select_program_songs(year: int, count: int) -> List[Dict[str, Any]]:
    """1 番組ぶんの曲を選ぶ（**原稿とプレイリストの唯一の情報源**）。

    候補集合は :func:`retro_radio.core.fallback.select_program_songs`
    （唯一の選曲口）と同一。ここは「再生履歴によるローテーション」と
    「dict 化」だけを担当する。曲集合の決定を二重に持たないこと
    （二重化すると原稿とプレイリストが食い違う）。
    """
    try:
        pairs = select_program_songs(year, count)
    except Exception:
        logger.exception(f"選曲に失敗しました（正本カタログへフォールバック）: year={year}")
        pairs = []
    records = [
        {"title": title, "artist": artist, "preview_url": None,
         "artwork_url": None, "is_fallback": False}
        for title, artist in pairs
    ]

    # 履歴ローテーション：同じ曲ばかり出ないよう、未再生を先に並べる
    try:
        history = SongHistoryStore(settings.song_store_path or None)
        selector = SongSelector(history=history)
        ordered = selector.order_candidates(year, records)
        if ordered:
            records = ordered
    except Exception:
        logger.exception(f"再生履歴の並べ替えに失敗しました: year={year}")
    return records
```

**検収**
```powershell
python -m pytest tests/test_job_api.py tests/test_no_audio_gaps.py tests/test_song_store_concurrency.py -q
```

---

### Step 11: 空リスト `[]` と `None` の意味を固定するテスト

**やること**
`tests/test_song_selection_single_source.py` に 3 件追加。

```python
def test_songs_none_derives_from_the_catalog():
    """songs=None は正本カタログから導出する。"""
    script = generate_radio_script(1975, 9, 24, songs=None)
    assert _named_in(script), "カタログから曲名を導出しなかった"


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_songs_empty_never_names_a_song(mode):
    """songs=[]（音源ゼロ確定）は曲名を一切書かない。"""
    script = generate_radio_script(1975, 9, 24, mode=mode, songs=[])
    assert not _named_in(script), sorted(_named_in(script))


def test_none_and_empty_produce_different_scripts():
    """None と [] は結果が異なるのが正しい設計。"""
    from_none = generate_radio_script(1975, 9, 24, songs=None)
    from_empty = generate_radio_script(1975, 9, 24, songs=[])
    assert _named_in(from_none)
    assert not _named_in(from_empty)
```

**検収**: 緑

---

### Step 12: `catalog_health.ok` の意味を正直にする

**対象ファイル**: `retro_radio/core/songs/__init__.py:399-401`

**現状の問題**
`"ok": bool(sufficient)` は「1 番組を**対象年の曲だけ**で埋められる年があるか」だけを表す。
しかし実測で **3030 曲すべてが `confidence: "unverified"`**、
**`preview_url` は 0 件**。つまり:

- 正本性の主張（「曲名と音源の一致」安全要件）が 1 件も裏付けられていない
- RPA な曲名だけがあり、**音源が 1 曲も無い**
- `ok=True` はこの事实を隠している

**やること**
`catalog_health()` に `verified_count` と `preview_count` を追加し、
`ok` は「対象年の曲数 AND 検証済曲がある」の両方を反映させる。

```py-fragment
    verified = sum(1 for r in records if r.get("confidence") == "verified")
    with_preview = sum(1 for r in records if r.get("preview_url"))
    ...
    "verified_count": verified,
    "preview_count": with_preview,
    "ok": bool(sufficient) and verified > 0,
```

**検収**
```powershell
python -m pytest tests/test_catalog_and_fallback_hardening.py tests/test_performance_baseline.py -q
```

**注意**: `tests/test_catalog_and_fallback_hardening.py:57` は
`assert health["ok"] is True` をしている。**このテストを `is False` に変更する**
（現状が嘘なので）。変更的理由を docstring に書くこと。

---

### Step 13: `validate_songs.py` が unverified を fail にする

**対象ファイル**: `scripts/validate_songs.py:209-220, 347`

**現状の問題**
`check_unverified` は全件に対して finding を**出すが `warn` 扱い**で、
`fail: 0 / warn: 3061` でも終了コード 0。

**やること**
`warn` を維持しつつ、出力に `unverified_ratio` を明示する。
（fail にすると現在 3030 件で必ず赤になり運用が止まるため、
段階導入としては warn + 明示が妥当。）

`scripts/validate_songs.py` の `main()` に追加：

```py-fragment
    unverified = sum(1 for r in records if r.get("confidence") != "verified")
    total = max(1, len(records))
    ratio = 100.0 * unverified / total
    print(
        f"一次文献未照合: {unverified}/{total} 件（{ratio:.1f}%）。"
        "介護施設向けの安全要件（曲名=音源）が保証されるのは"
        "verified の曲のみです。",
        file=sys.stderr,
    )
```

**検収**: `python scripts/validate_songs.py --quiet` が EXIT 0、
出力が `100.0%` を含むこと

---

### Step 14: eval の曲照合マスターを正本に同期する（計画の P3-2）

**対象ファイル**: `eval/metrics/songs.py`

**やること**
`eval/metrics/songs.py` の静的マスターを
`retro_radio.core.songs.load_songs()` から組み立てるように変更する。
読み込み失敗時は空集合へ縮退する（既存方針）。

**検収**
```powershell
python -m pytest tests/test_eval_harness.py tests/test_eval_harness_hardening.py -q
python -m eval --offline --threshold 80
```

**注意**: Step 8 で `_catalog_allowlist` が実カタログを使うようになったため、
P3-2 は Step 8 の**後**にしないと検証が突然厳しくなる。

---

### Step 15: `fallback.py` の `FALLBACK_SONGS` を「縮退用」に位置づける

**対象ファイル**: `retro_radio/core/fallback.py:28-138`

**やること**
`FALLBACK_SONGS`（36 曲）が選曲に使われなくなったため、
その位置づけを docstring に明記する（削除はしない —
正本カタログが読めないときの最後の保険として必要）。

```py-fragment
# `FALLBACK_SONGS` は**正本カタログが読めないときの縮退化**であり、
# 選話の根拠にはしない（実測: 静的マスター 36 曲だけだと
# 1 番組 18 曲を埋められず、原稿が名指しした曲と
# プレイリストの曲が全く一致しなくなる）。
# 選曲の唯一の口は `select_program_songs`。
```

**検収**: `python -m pytest tests/test_content_regression.py -q` が緑
（`FALLBACK_SONGS` の構造を変えないこと）

---

### Step 16: Phase 1 の検収

```powershell
python scripts/verify_all.py
python -m pytest tests/test_song_selection_single_source.py -v
```

**完了条件**: EXIT 0、新規 12 テスト緑、
`_catalog_allowlist` と `select_program_songs` の交集が全年で**一致**

---

## Phase 2 — P1: 検査の穴

### Step 17: マイグレーションを実際に実行するテスト【最大の穴】

**現状（サブエージェント実測）**
`grep -c "^(from alembic|import alembic)"` が全 90 テストファイルで **0**。
`tests/conftest.py:290-317` の `_ensure_test_schema` は
`Base.metadata.create_all(bind=engine)` でスキーマを作るので、
**マイグレーションを一度も実行していない**。
`alembic upgrade head` の破綻は 2918 件のテストすべてが見逃す。

**やること**
`tests/test_migrations.py` を新規作成する。

```python
"""`alembic upgrade head` を実 DB で通す。

**これが無いと**、テストは `create_all` で書いたスキーマを検証するだけで、
本番が実際に得るスキーマは一切検証されない。
FastAPI + SQLAlchemy + Alembic 構成の最も頻出する本番デプロイ失敗を
この 1 テストで検出する。
"""
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _run(args, db_url):
    env = dict(os.environ)
    env["RETRO_RADIO_DATABASE_URL"] = db_url
    env["RETRO_RADIO_REQUIRE_AUTH"] = "0"
    env["RETRO_RADIO_GEMINI_API_KEY"] = ""
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=180,
    )


def test_upgrade_head_creates_the_expected_tables(tmp_path):
    db = tmp_path / "migrate.db"
    url = f"sqlite:///{db.as_posix()}"
    result = _run(["-m", "alembic", "upgrade", "head"], url)
    assert result.returncode == 0, result.stdout + result.stderr

    import sqlite3
    with sqlite3.connect(db) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("users", "generations", "favorites", "tenants",
                  "user_security", "consents", "alembic_version"):
        assert table in names, f"{table} が migration 後のスキーマに無い"


def test_alembic_check_reports_no_new_operations(tmp_path):
    """モデルとマイグレーションが乖離していないこと。"""
    db = tmp_path / "check.db"
    url = f"sqlite:///{db.as_posix()}"
    up = _run(["-m", "alembic", "upgrade", "head"], url)
    assert up.returncode == 0, up.stdout + up.stderr
    result = _run(["-m", "alembic", "check"], url)
    assert result.returncode == 0, result.stdout + result.stderr


def test_downgrade_then_upgrade_roundtrip(tmp_path):
    """downgrade → upgrade でスキーマが壊れないこと。"""
    db = tmp_path / "roundtrip.db"
    url = f"sqlite:///{db.as_posix()}"
    assert _run(["-m", "alembic", "upgrade", "head"], url).returncode == 0
    down = _run(["-m", "alembic", "downgrade", "base"], url)
    assert down.returncode == 0, down.stdout + down.stderr
    up = _run(["-m", "alembic", "upgrade", "head"], url)
    assert up.returncode == 0, up.stdout + up.stderr
```

**検収**: 3 件緑（Downgrade が失敗する場合は migration の修正が必要）

---

### Step 18: `conftest` の `create_all` と migration の二重定義を止める

**やること**
`tests/conftest.py:290-317` の `_ensure_test_schema` を、
migration 済みの DB を使う形に書き換える。ただし
`tests/test_db_session.py` が `Base.metadata.drop_all` を実行するため、
セッションスコープ化は**できない**（既存 docstring の記録どおり）。

**やること**: 既存フェ大侠を保ちつつ、**セッションスコープの一度だけ
migration を通す**フィクスチャを追加する。

```python
@pytest.fixture(scope="session", autouse=True)
def _migrated_schema():
    """セッション開始時に一度だけ `alembic upgrade head` を通す。

    既存の `_ensure_test_schema`（関数スコープ）は
    `tests/test_db_session.py` の `drop_all` に対応するため残す。
    本フィクスチャは「本番と同じ経路でスキーマが作れる」ことの担保。
    """
    import tempfile
    db = pathlib.Path(tempfile.gettempdir()) / "retro_radio_migrate_probe.db"
    if db.exists():
        db.unlink()
    url = f"sqlite:///{db.as_posix()}"
    import os, subprocess, sys
    env = dict(os.environ)
    env["RETRO_RADIO_DATABASE_URL"] = url
    env["RETRO_RADIO_REQUIRE_AUTH"] = "0"
    env["RETRO_RADIO_GEMINI_API_KEY"] = ""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=pathlib.Path(__file__).resolve().parents[1],
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head が失敗しました:\n{result.stdout}\n{result.stderr}"
    )
    yield
    db.unlink(missing_ok=True)
```

**検収**: `python -m pytest -q` が緑

---

### Step 19: `history_service` の 4 公開関数を実 DB でテストする

**現状（サブエージェント実測）**
`retro_radio/services/history_service.py` のカバレッジは **35%**。
`record_generation` / `record_playback` / `list_for_user` / `delete_for_user` の
**4 公開関数が全滅未テスト**。`tests/test_history_service.py` は
`get_db_sync` と `GenerationRepository` の**両方**を mock しており、
`repo.create.assert_called_once_with(...)` を検査するだけ（モックの検査）。

**やること**
`tests/test_history_service_real_db.py` を新規作成する。
**mock を一切使わず、実 DB に対して呼ぶ。**

```python
"""`history_service` の公開関数を**実 DB** で検証する。

既存の `tests/test_history_service.py` は DB もリポジトリも mock しているため、
配線を検査しているだけ。本ファイルは privacy 経路の回帰她自己。
"""
import pytest

from retro_radio.db.models import Base
from retro_radio.db.privacy_models import create_privacy_tables
from retro_radio.db.privacy_repository import (
    AuditRepository, MusicProfileRepositoryImpl,
)
from retro_radio.db.repository import GenerationRepository, UserRepository
from retro_radio.db.session import get_engine


@pytest.fixture
def real_db():
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    create_privacy_tables(engine)
    from retro_radio.db.session import get_session_factory
    session = get_session_factory()()
    yield session
    session.rollback()
    session.close()


def _make_user(session):
    from retro_radio.models.user import PlanType, User
    domain = User(
        email=f"h{random_int()}@example.com", hashed_password="x",
        plan=PlanType.FREE, generation_count=0,
    )
    return UserRepository(session).create(domain)


def random_int():
    import random
    return random.randint(100000, 999999)


def test_record_generation_persists_and_writes_audit(real_db):
    from retro_radio.services.history_service import record_generation
    user = _make_user(real_db)
    real_db.commit()
    result = record_generation(
        user_id=user.id, tenant_id="default", year=1975, month=9, day=24,
        script="### オープニング\n本文", song_title="A", artist_name="B",
        mode="normal",
    )
    assert result["recorded"] is True
    rows = GenerationRepository(real_db).get_by_user(user.id, limit=10)
    assert len(rows) == 1
    assert AuditRepository(real_db).count() >= 1


def test_audit_meta_never_contains_the_script(real_db):
    """監査ログに原稿本文が入らないこと（プライバシー要件）。"""
    from retro_radio.services.history_service import record_generation
    user = _make_user(real_db)
    real_db.commit()
    record_generation(
        user_id=user.id, tenant_id="default", year=1975, month=9, day=24,
        script="機密本文XYZ", song_title="A", artist_name="B", mode="normal",
    )
    rows = AuditRepository(real_db).list(tenant_id="default", limit=50)
    for row in rows:
        assert "機密本文XYZ" not in str(row.get("meta")), row


def test_delete_for_user_removes_generations(real_db):
    """`delete_for_user` が本当に消すこと（現状 0 テスト）。"""
    from retro_radio.services.history_service import delete_for_user
    user = _make_user(real_db)
    real_db.commit()
    real_db.add(__import__("retro_radio.db.models", fromlist=["x"])
                .GenerationModel(
                    user_id=user.id, year=1975, month=9, day=24,
                    script="x", song_title="A", artist_name="B",
                    created_at=__import__("datetime").datetime.utcnow(),
                ))
    real_db.commit()
    assert GenerationRepository(real_db).get_by_user(user.id, limit=10)
    removed = delete_for_user(user.id)
    assert removed >= 1
    assert GenerationRepository(real_db).get_by_user(user.id, limit=10) == []


def test_record_generation_skips_when_user_unknown(real_db):
    """個人モード（user_id 空）は記録しない（削除請求を追えなくなるため）。"""
    from retro_radio.services.history_service import record_generation
    result = record_generation(
        user_id="", tenant_id="default", year=1975, month=9, day=24,
        script="x", song_title="A", artist_name="B", mode="normal",
    )
    assert result["recorded"] is False
```

**検収**: 緑。失敗するテストがあれば**実装が壊れている**証拠なので、
テストを弱めず実装を直すこと

---

### Step 20: `DELETE /api/me` を HTTP 経由でテストする

**現状**
`tests/test_me_api.py:529-552` は `purge_user_personal_data` を
**直接呼び出す**だけで、`DELETE /api/me` のエンドポイントを通さない。

**やること**
`tests/test_me_api_http_auth.py` に 2 件追加。

```python
def test_delete_me_removes_generations_end_to_end(client, auth_user):
    """実 HTTP で削除請求が全データを消すこと。"""
    from retro_radio.db.repository import GenerationRepository
    # 生成を 1 件作る
    resp = client.post("/api/generate", json={
        "year": 1975, "month": 9, "day": 24, "mode": "normal",
    })
    assert resp.status_code in (200, 202), resp.text

    before = GenerationRepository(...).get_by_user(auth_user.id, limit=10)
    assert before, "テスト前提: 生成履歴ought 1 件以上あること"

    resp = client.delete("/api/me")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deleted"

    after = GenerationRepository(...).get_by_user(auth_user.id, limit=10)
    assert after == [], "削除後も生成履歴が残っている"


def test_delete_me_keeps_audit_rows(client, auth_user):
    """監査ログは『処理の証明』として残る。"""
    from retro_radio.db.privacy_repository import AuditRepository
    client.post("/api/generate", json={
        "year": 1975, "month": 9, "day": 24, "mode": "normal"})
    before = AuditRepository(...).count()
    client.delete("/api/me")
    assert AuditRepository(...).count() >= before
```

**注意**: `...` は既存の `client` / `auth_user` フィクスチャの
DB セッション取得方法に合わせて埋める。**1 ステップで 1 テストだけ**実装し、
緑を確認してから次へ進む。

---

### Step 21: `delete_for_user` を削除請求に組み込む（Step 19/20 の前提）

**対象ファイル**: `retro_radio/db/privacy_repository.py:948-964`

**現状（実測）**
`purge_user_personal_data` は `favorite_tracks` / `music_profiles` / `consents`
を削除し `deleted_at` を立てるだけ。
`generations.script`（**対象の愛称と生年に基づく原稿全文**）は残る。
`history_service.delete_for_user()`（`:213`）が正しく実装されているが
**どこからも呼ばれていない**。

**やること**
`purge_user_personal_data` に `generations` の削除を追加する。

```python
def purge_user_personal_data(db: Session, user_id: str) -> Dict[str, Any]:
    """削除請求の実行本体（`DELETE /api/me` から呼ばれる）。

    方針:
    - **消す**: `generations` / `favorite_tracks` / `music_profiles` / `consents`。
      `generations.script` は対象者の愛称と生年に基づく原稿であり、
      削除請求の主要な対象（従来は漏れていた）。
    - **匿名化して残す証拠**: `users.email` / `users.hashed_password`。
    - **消さない**: `audit_logs`（処理の証明）。
    """
    from .models import GenerationModel

    repo = MusicProfileRepositoryImpl(db)
    removed = repo.delete_owner(user_id)

    generations_removed = (
        db.query(GenerationModel)
        .filter(GenerationModel.user_id == user_id)
        .delete(synchronize_session=False)
    )

    sec = UserSecurityRepository(db).mark_deleted(user_id)
    db.flush()
    return {
        "s4_rows_removed": removed,
        "generations_removed": generations_removed,
        "deleted_at": sec["deleted_at"],
    }
```

**検収**
```powershell
python -m pytest tests/test_me_api.py tests/test_me_api_http_auth.py tests/test_history_service_real_db.py -q
```

---

### Step 22: `api/audit.py` の管理者エンドポイントをテストする

**対象ファイル**: `retro_radio/api/audit.py:145-163`

**現状（実測）**
`GET /api/admin/audit/stats`（管理者専用、削除 SLA の監視に使う）は
**テスト 0 件**。`require_admin` 依存が一度も実行されていないため、
テナントGrpc-blind になる回帰が素通りする。

**やること**
`tests/test_api_access_control.py` に 3 件追加。

```python
def test_audit_stats_requires_admin(client, member_token):
    resp = client.get("/api/admin/audit/stats",
                      headers={"Authorization": f"Bearer {member_token}"})
    assert resp.status_code == 403, resp.text


def test_audit_stats_requires_auth(client):
    resp = client.get("/api/admin/audit/stats")
    assert resp.status_code in (401, 403), resp.text


def test_audit_stats_is_tenant_scoped(client, admin_token, tenant_b_user):
    """他テナントの件数が見えないこと。"""
    resp = client.get("/api/admin/audit/stats",
                      headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.text
    assert "total" in resp.json()
```

**検収**: 3 件緑

---

### Step 23: 低価値テストを 1 件ずつ機械的検査に置き換える

**現状（サブエージェント実測、file:line と引用付き）**

| # | 場所 | 問題 |
|---|---|---|
| 1 | `tests/test_me_api.py:730` | `assert f'op.create_table(\n        "{table}"' in text` — migration の**インデント 8 スペース**を検査 |
| 2 | `tests/test_tts_rate_limit.py:48` | `assert "def _tts_is_network_client" in Path(server_module.__file__).read_text()` — 関数名の存在を grep。本文を空にしても通る |
| 3 | `tests/test_utils_async_runner.py:42` | `inspect.getsource` で `"get_running_loop" in source` を検査 |
| 4 | `tests/test_hygiene_regression.py:660` | import 行を `re.search` で grep。`ast` が同じファイルに import 済み |
| 5 | `tests/test_hygiene_regression.py:679` | regex で例外メッセージをソースから**抜き出して**検査。ハンドラ側の分岐は未検証 |
| 6 | `tests/test_tts_branches.py:350` | `def test_cleanup_swallows_permission_errors` に **assert が 0 件**。`cleanup_audio_file` が `pass` でも通る |
| 7 | `tests/test_song_store_concurrency.py:207` | `inspect.signature(...).parameters[...].default` の既定値を検査。TTL を削除しても通る |
| 8 | `tests/test_song_alignment.py:218` | `assert len(script) >= 800` の魔法値。同ファイル内で `eval.metrics.length.length_bounds` を import できるのに再定義している |

**やること（1 テストずつ、8 サブステップ）**

各テストを「実挙動」を検査する形に書き換える。**一度に全部直さない。**

例（#1 の直し方）:

```python
# 旧: assert f'op.create_table(\n        "{table}"' in text
# 新: migration を実際に適用してテーブルが存在することを確認
def test_migration_creates_the_privacy_tables(tmp_path):
    """文字列ではなく、実 DB にテーブルが作られることを検証する。"""
    # Step 17 の _run ヘルパを再利用する
    ...
```

例（#6 の直し方）:

```python
# 旧: assert が 0 件
# 新: 例外が飲み込まれ、ファイルが残ることを検証
def test_cleanup_swallows_permission_errors(tmp_path, monkeypatch):
    target = tmp_path / "locked.mp3"
    target.write_bytes(b"data")
    real_unlink = pathlib.Path.unlink

    def boom(self, *args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(pathlib.Path, "unlink", boom)
    try:
        cleanup_audio_file(str(target))   # 例外が漏れないこと
    finally:
        monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
    assert target.exists(), "PermissionError でもファイルは残るはず"
```

**検収**: 各サブステップ後 `python -m pytest <file> -q` が緑

---

### Step 24: assert が 0 件のテストを機械的に検出する

**やること**
`tests/test_hygiene_regression.py` に 1 件追加。

```python
class TestNoAssertFreeTests:
    """`assert` の 0 件のテスト関数がないこと。

    サブエージェント実測: 1659 関数中 34 関数が assert 0 件。
    それらは関数を `pass` にしても緑になる。
    """

    def _test_functions(self):
        import ast
        path = Path(__file__).parent / "test_tts_branches.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                has_assert = any(
                    isinstance(n, ast.Assert) for n in ast.walk(node))
                if not has_assert:
                    yield f"{path.name}:{node.lineno} {node.name}"

    def test_no_assert_free_tests_in_tts_branches(self):
        offenders = list(self._test_functions())
        assert not offenders, (
            "assert の無いテスト（関数の中身を pass にしても緑になる）:\n"
            + "\n".join(offenders)
        )
```

**検収**: 緑になるには #23 の #6 を直す必要がある。
両方同時の実装でもよいが、**#23-#6 を先に直す**こと

---

## Phase 3 — P2: セキュリティ

### Step 25: セッション失効フックを起動時に配線する【H1】

**対象ファイル**: `retro_radio/server.py:566-601`（`lifespan`）

**現状（実測）**
`tokens.set_session_verifier`（`tokens.py:283`）は
**定義・docstring・`__all__` 以外にどこからも呼ばれていない**。
`POST /api/auth/logout`（`server.py:2058-2062`）は `response.delete_cookie(...)` のみ、
すなわちクライアントへの指示に過ぎない。
盗まれた Cookie は残り TTL（8 時間）Bearer や Cookie として通用。

**やること**
`lifespan` に以下を追加する。

```py-fragment
    # セッションの失効フックを配線する。
    # これを配線しないと `POST /api/auth/logout` は
    # `delete_cookie`（= クライアントへの指示）に過ぎず、
    # 盗まれたセッションが TTL の残り時間だけ使い続けられる。
    from .auth import tokens as _tokens

    def _verify_session(user_id: str, payload: Dict[str, Any]) -> bool:
        """削除済みユーザのセッションを無効化する。"""
        if not user_id:
            return False
        try:
            from .db.privacy_repository import UserSecurityRepository
            from .db.session import get_db
            with get_db() as db:
                return not UserSecurityRepository(db).is_deleted(user_id)
        except Exception:  # noqa: BLE001 - 判定できない場合は fail-closed
            logger.exception("セッション失効判定に失敗しました: user_id=%s", user_id)
            return False

    _tokens.set_session_verifier(_verify_session)
    yield
    _tokens.set_session_verifier(None)
```

**検収**
```powershell
python -m pytest tests/test_auth_hardening.py tests/test_health.py -q
```

---

### Step 26: ログインスロットルの飽和 DoS を防ぐ【H2】

**対象ファイル**: `retro_radio/auth/authenticator.py:274-277`

**現状（実測）**
```py-fragment
    def delay_for(self, key):
        ...
            if stamps is None:
                if len(self._records) >= self.max_keys:
                    return self.max_delay      # ← 未知キーは全員 60 秒
                return 0.0
```
`MAX_KEYS = 10_000`。未知キーに対して `max_delay` を返すため、
攻撃者が 10,000 個の異なるメールを送ると
**以降 15 分間、正規ユーザーのログインが全て 429**。
資格情報は一切不要。`server.py:1988` が `delay > 0` を 429 に変換。

**やること**
`return self.max_delay` を `return 0.0` に変える。
未記録キーなので「記録が無くても通常 user's 試行を妨げない」——
既存キー（実際に総当たりされているアカウント）は自分の記録で保護される。

```py-fragment
            if stamps is None:
                if len(self._records) >= self.max_keys:
                    # 記録上限に達している状態で**未知のキー**に
                    # 最大遅延を返すと、攻撃者が 1 万個の別メールを
                    # 撒くだけで**正規ユーザー全員がロックアウト**される
                    # （実測）。未記録キーなので、何もしない。
                    return 0.0
                return 0.0
```

**検収**
```powershell
python -m pytest tests/test_auth_hardening.py tests/test_tts_rate_limit.py -q
```

---

### Step 27: `SINGLE_USER_KEY` に長さ検査を適用する【H3】

**対象ファイル**: `retro_radio/server.py:1913-1928`

**現状（実測）**
`_verify_bearer_secret` は生キーを `_constant_time_equals` で直接比較し、
`_require_secret` を**呼ばない**。他経路（`tokens.py:258`）は
`MIN_SECRET_LENGTH = 32` を検査している。
`server.py:2016` のベアラー経路はスロットルも無く、
`changeme` のような弱いキーが 8 時間有効セッションを発行しうる。

**やること**
`config.py` に設定検査を追加する。

```py-fragment
    single_user_key: str = Field(default="")

    # ... 既存の validate_* メソッドの前に追加

    @model_validator(mode="after")
    def _require_secret_length(self) -> "Settings":
        """`single_user_key` はセッション署名鍵として使われるため、
        32 文字未満を拒否する（`tokens.MIN_SECRET_LENGTH` と同じ下限）。

        これを怠ると `/api/generate` では拒否される弱い鍵が
        `POST /api/auth/session` では通ってしまう（実測）。
        """
        from .auth.tokens import MIN_SECRET_LENGTH
        if self.single_user_key and len(self.single_user_key) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"RETRO_RADIO_SINGLE_USER_KEY が短すぎます"
                f"（{len(self.single_user_key)} 文字 < 最小 {MIN_SECRET_LENGTH} 文字）"
            )
        return self
```

**検収**
```powershell
python -m pytest tests/test_config.py tests/test_env_templates.py -q
```

---

### Step 28: `is_deleted` を fail-closed にする【H5】

**対象ファイル**: `retro_radio/db/privacy_repository.py:244-252`

**現状（実測）**
`is_deleted` は `get()` が `None` を返すと `False` を返す。
`deps.py:380` の削除済み 403 がこの呼び出しに依存しているため、
`user_security` 行を消すと**アクセスが復活**する。

**やること**
```py-fragment
    def is_deleted(self, user_id: str) -> bool:
        """削除済みかどうか。

        行が無い場合は **True**（= 削除済み扱い）とする。
        行の欠落は「確認できない」であり、「削除されていない」ではない。
        欠落を False にすると、行を消すことでアクセスを復活できてしまう（実測）。
        """
        found = self.get(user_id)
        if found is None:
            return True
        return bool(found.get("deleted_at"))
```

**検収**
```powershell
python -m pytest tests/test_me_api.py tests/test_auth_wiring.py tests/test_api_access_control.py -q
```

**注意**: 既存テストがこの変更で赤くなる可能性がある。
赤くなった場合、それは「テストが誤っている」のではなく
「現実装の契約（＝fail-open）を固定していた」ものである。該当テストの期待値を更新し、
その旨を docstring に書くこと。

---

### Step 29: `UserRepository.update` の全列書きを部分更新にする【M4】

**対象ファイル**: `retro_radio/db/repository.py:121-133`

**現状（実測）**
`update` は `email` / `hashed_password` / `plan` / `generation_count` /
`generation_reset_at` の**全列**を、記憶の上の `UserDomain` から書く。
Stripe webhook（`billing/webhook.py:128-129,166-167,185-186`）が
`plan` を更新した直後に、ログイン経路が古い `plan=FREE` を書き戻す
= **有料アップグレードの静かな巻き戻し**。

**やること**
部分更新メソッドを追加する。

```py-fragment
    def update_plan(self, user_id: str, plan) -> UserDomain:
        """`plan` だけを書く。**他の列は他経路の値を壊さない。**"""
        model = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        if not model:
            raise ValueError(f"User not found: {user_id}")
        model.plan = _coerce_plan(plan)
        model.updated_at = utcnow()
        self.db.flush()
        return self._to_domain(model)
```

**検収**
```powershell
python -m pytest tests/test_billing.py tests/test_db_user_repo.py tests/test_auth_password.py -q
```

---

### Step 30: `check_can_generate` の無制限を撤廃する【M2】

**対象ファイル**: `retro_radio/auth/authenticator.py:413-417`

**現状（実測）**
```py-fragment
    def check_can_generate(self, user):
        if user is not None:
            self._reset_generation_period(user)
        return True, UNLIMITED_GENERATIONS
```
認証済み 1 セッションで `POST /api/generate` を無限ループでき、
運用者の Gemini / gTTS / iTunes 枠を消費できる。

**やること**
`Settings` に日次上限を追加し、ここで判定する。

`config.py` に追加:
```py-fragment
    max_generations_per_day: int = Field(default=50, ge=1, le=10000)
```

`authenticator.py` の置換:
```py-fragment
    def check_can_generate(self, user: User) -> tuple[bool, int]:
        """生成可能かチェックする。

        認証済み利用者が Gemini / gTTS / iTunes の枠を
        無制限に消費できないよう、**1 日あたりの上限**を設ける。
        """
        if user is None:
            return True, UNLIMITED_GENERATIONS
        self._reset_generation_period(user)
        limit = settings.max_generations_per_day
        used = int(user.generation_count or 0)
        if used >= limit:
            return False, 0
        return True, limit - used
```

**検収**
```powershell
python -m pytest tests/test_billing.py tests/test_me_api.py tests/test_plan_control.py -q
```

---

### Step 31: Phase 3 の検収

```powershell
python scripts/verify_all.py
python -m pytest tests/test_auth_hardening.py tests/test_config.py tests/test_me_api.py -q
```

**完了条件**: EXIT 0

---

## Phase 4 — P3: 衛生

### Step 32: 文字化け（混入）を全件修正する

**現状（実測・`git show HEAD` で確認済み）**

| ファイル:行 | 混入 |
|---|---|
| `retro_radio/server.py:1048` | 安定した分割**才可以**する。 |
| `retro_radio/server.py:1061` | ここに統一**liesbecause**、 |
| `retro_radio/server.py:1063` | ズレ**重生**する |
| `retro_radio/api/me.py:30,119,610` | **counsel** による確認（×3） |
| `retro_radio/db/privacy_repository.py:959` | **counsel** 確認が必要 |
| `db/migrations/env.py:18` | このプロセスを起動した**motivation** の |
| `docs/archive/F1.md:88,133` | **podob**なアクセシビリティテストツール |
| `docs/archive/F2.md:60` | **aptic**フィードバック |
| `plans/remaining_work_plan.md:202` | 構造的困難**ayas** あるが |
| `plans/remaining_work_plan.md:247` | 判断**agues** を `docs/...` に書く |
| `plans/remaining_work_plan.md:310` | ルートの一時ファイル**_ratio طويل** |

**やること（1 ファイルずつ、11 サブステップ）**

| # | ファイル | 直し方 |
|---|---|---|
| 32.1 | `retro_radio/server.py` | `才可以` → `できる`、`liesbecause` → `するため`、`重生` → `が生じる` |
| 32.2 | `retro_radio/api/me.py` | `counsel` → `弁護士`（3 箇所） |
| 32.3 | `retro_radio/db/privacy_repository.py` | `counsel` → `弁護士` |
| 32.4 | `db/migrations/env.py` | `motivation` → `migration` |
| 32.5 | `docs/archive/F1.md` | `podob` → `同種`（2 箇所） |
| 32.6 | `docs/archive/F2.md` | `aptic` → `ハaptic` |
| 32.7 | `plans/remaining_work_plan.md` | 3 箇所を修正 |
| 32.8 | `retro_radio/core/songs/__init__.py` | ディレクトリ docstring の「36 曲しか無く」を現状（3030 曲）に更新 |
| 32.9 | `eval/README.md` | G5 の「2 件不合格」を実測値（0 件）に更新 |
| 32.10 | `plans/remaining_work_plan.md` | P0-1（eval fixtures）を「完了」に更新 |
| 32.11 | `plans/remaining_work_plan.md` | P1-2（SPA 配線済み）を「完了」に更新 |

**検収**: `python scripts/verify_all.py` が緑

---

### Step 33: 文字化け混入の機械検査をソース全域に広げる

**やること**
`tests/test_hygiene_regression.py` に 1 件追加。
現状は `.md` のみ検査している（`test_no_live_doc_has_unicode_replacement_characters`）。

```python
class TestNoMixedScriptCorruption:
    """日本語文中承諾に混入した英単語/中国語断片が無いこと。

    実測で `retro_radio/server.py:1048`（`才可以`）、
    `retro_radio/api/me.py:30`（`counsel`）などが検出された。
    これは生成セッションの文字化けであり、利用者に見える文字列
    （API 応答）に混入すると表示の不具合そのもの。
    """

    # 文脈的に正当な英単語（技術用語・識別子）
    ALLOWED = {
        "API", "HTTP", "HTTPS", "JSON", "YAML", "SQL", "URL", "URI",
        "UTF", "CSV", "HTML", "CSS", "JS", "LLM", "CI", "CD", "IPA",
        "Docker", "Alembic", "SQLAlchemy", "FastAPI", "Pydantic",
        "pytest", "flake8", "mypy", "Redis", "PostgreSQL", "SQLite",
        "Stripe", "ElevenLabs", "Gemini", "gTTS", "iTunes", "OAuth",
        "Web", "Audio", "Content", "Security", "Policy", "Type",
        "Server", "Error", "Warning", "Info", "Debug", "True", "False",
    }

    def test_no_latin_run_embedded_in_japanese(self):
        import re, pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        offenders = []
        for path in list((root / "retro_radio").rglob("*.py")):
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                for match in re.finditer(
                        r"[぀-ヿ一-鿿]([A-Za-z]{3,})[぀-ヿ一-鿿]", line):
                    word = match.group(1)
                    if word in self.ALLOWED or word.lower() in {w.lower() for w in self.ALLOWED}:
                        continue
                    offenders.append(f"{path.relative_to(root)}:{lineno} {word}")
        assert not offenders, "日本語文中に混入した英単語:\n" + "\n".join(offenders)
```

**検収**: 緑になるまで Step 32 を完了すること
（残りがあれば Step 32 に戻って直す）

---

### Step 34: lint ベースラインを段階的に縮小する

**やること（3 サブステップ）**

**34.1**: `.github/flake8-app-baseline.ini` から `E501`（行長 100 超）を削除し、
`max-line-length` を 100 のまま使う。

```powershell
python -m flake8 --max-line-length=100 retro_radio
```

**34.2**: `E402` を削除し、中盤 import を 6 ファイルへ移動する。
対象（実測順）:
- `retro_radio/config.py:11,13`
- `retro_radio/core/tts.py:99-102`

**34.3**: `W293`（行末空白）と `E302`（空行数）を 1 つずつ解消するたびに
Step 3 の `ALLOWED` を実測値に更新する。

**検収**: 各サブステップ後 `python scripts/verify_all.py` が緑

---

### Step 35: 死んだ資産を整理する

**やること（3 サブステップ）**

**35.1**: `styles/generated.css` と `design_tokens/` は
`static/index.html` から読まれない（`app.css` だけを読む）。
`tests/test_retro_theme.py:85` と `tests/test_ui_ux.py:1104` は
その**存在だけ**を検査している。

選択肢（使用者判断）:
- (a) `index.html` に読み込ませ、tokens を実際に使う
- (b) 削除し、`test_generated_css_untouched` を削除する

**推奨 (b)**（現状 1 枚の CSS に統合されている）。

**35.2**: `docs/archive/` の 24 ファイル（380KB）を 1 ファイルに統合するか、
`docs/archive/README.md` に「Streamlit 時代の資料。参照しない」と明記する。

**35.3**: git 追跡済みの実行ログ
`eval_gate.txt` / `eval_warn.txt` / `eval_script50.txt` / `eval_detail.json`
を削除し、`.gitignore` に追加する。

```gitignore
# eval の実行ログ（成果物ではなく残骸）
eval_*.txt
eval_*.json
.pytest_cache/
.hypothesis/
```

**検収**: `python scripts/verify_all.py` が緑

---

### Step 36: 計画書・レポートの同期

**やること**
以下を実測値で更新する（**嘘を残さない**）。

| ファイル | 現状の嘘 |
|---|---|
| `plans/remaining_work_plan.md:13` | 「2708 passed / 0 failed」 |
| `plans/remaining_work_plan.md:18` | 「eval EXIT 1 / 2 件不合格」→ 既に EXIT 0 |
| `plans/remaining_work_plan.md:233` | 「`/api/me/*` 未配線」→ 配線済み |
| `eval/README.md:4.1` | 「G5 2 件不合格」→ 0 件 |
| `docs/song_catalog.md` | 曲数の記述が 36 曲時代のまま |

**検収**: `python -m pytest tests/test_docs_consistency.py -q` が緑

---

## 全体完了の定義（Definition of Done）

| # | 条件 | 検証コマンド |
|---|---|---|
| 1 | 選曲の実質的な不具合が解消 | `_catalog_allowlist` と `select_program_songs` の**全年で交集一致** |
| 2 | 原稿とプレイリストの曲一致 | `test_script_named_songs_are_a_subset_of_the_playlist` が全緑 |
| 3 | 新規リグレッションテスト緑 | `python -m pytest tests/test_song_selection_single_source.py tests/test_migrations.py tests/test_history_service_real_db.py -q` |
| 4 | マイグレーションが実 DB で通る | `tests/test_migrations.py` 3 件緑 |
| 5 | 削除請求が全データを消す | `test_delete_me_removes_generations_end_to_end` 緑 |
| 6 | セキュリティ 5 件が直る | Step 25〜30 の検収すべて緑 |
| 7 | 文字化けゼロ | `test_no_latin_run_embedded_in_japanese` 緑 |
| 8 | 全ゲート緑 | `python scripts/verify_all.py` → EXIT 0 |
| 9 | 未コミットなし | `git status --porcelain` → 空 |
| 10 | 文書が実測値と一致 | `tests/test_docs_consistency.py` 緑 |

---

## 変更履歴

- 2026-10-01: 初版。前回レビュー（`docs/final_review_report_2026-10-01.md`）の
  「2918 件緑」を出発点として、**緑のまま気づけない契約違反**と**検査の穴**を
  36 ステップに分割。Step 7〜9（選曲の一本化）が最優先。
