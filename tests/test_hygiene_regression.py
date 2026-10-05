"""コード衛生の回帰防止テスト。

このファイルは「一度直したら二度と壊さない」ための機械検証であり、
アプリケーションのロジック（原稿生成・DB・API）は一切検証しない。
検証するのはリポジトリの**形**（ファイル・設定・依存宣言）だけなので
高速かつネットワーク不要（`network` マーカーは付けない）。

各テストが守る不変条件:

  1. BOM           ... Python ソースに UTF-8 BOM が無い（ast.parse と行ずれの防止）
  2. 旧GeminiSDK   ... `google.generativeai` を import しない・非 FutureWarning
  3. .dockerignore ... 秘密/DB/テストをイメージに焼き込まない、かつ必須ファイルを残す
  4. 脆弱な推移依存 ... PYSEC-2026-2132（click）が解決可能な範囲で固定されている
  5. F401          ... 未使用 import が無い（CI の baseline を外して検査する）
  6. W292          ... Python ファイルに末尾改行がある
  7. .env.example  ... 全キーが `Settings` に食わせられ、例外を投げない
  8. 既定値整合    ... .env.example の値が `config.py` の既定値と矛盾しない
  9. 依存の必要性  ... requirements.txt の各パッケージに「使う理由」がある
"""

import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from retro_radio.config import Settings, parse_cors_origins

ROOT = Path(__file__).resolve().parent.parent

# 走査対象から機械的に除外するディレクトリ（キャッシュ・VCS・仮想環境・エディタ状態）
# `.kilo` は Agent Manager / Kilo のローカル状態（worktree・セッション保持）で、
# このリポジトリのソースではない。`.gitignore` 済みだが、ディスク上には
# 存在しうるため**走査対象からも外す**（別ワークツリーの test_* が
# このテストの偽陽性になるのを防ぐ）。
SKIP_DIRS = {
    ".git", ".hypothesis", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "__pycache__", "node_modules", ".venv", "venv", "env", "htmlcov",
    ".kilo",
}

# ---------------------------------------------------------------------------
# D5（衛生担当セッション）が所有するファイル。
# ここは「0 件」が絶対条件で、他ワークストリームの残骸を許容リストでSpeakさない。
# ---------------------------------------------------------------------------
OWNED_PACKAGE_FILES = (
    "retro_radio/core/script_generator.py",
    "retro_radio/utils/validators.py",
    "retro_radio/utils/__init__.py",
    "retro_radio/auth/__init__.py",
    "retro_radio/db/session.py",
    "retro_radio/services/cache_service.py",
    "retro_radio/services/history_service.py",
)

# ---------------------------------------------------------------------------
# 他ワークストリームが所有する**既存の**違反。
# ここに無いファイルで新規に発生した場合、テストは落ちる（＝新規違反は CI で必ず検出される）。
# 該当ファイルが修正されたら、この集合からパスを削除すること。
# ---------------------------------------------------------------------------
KNOWN_F401_ALLOWLIST = frozenset()
# `utils/design_tokens.py` / `retro_radio/core/{fallback,script_generator,song_selector}.py`
# は未使用 import を解消済み。ここに残すと
# `test_allowlist_contains_no_files_without_violations` が腐敗を検出する。
# `retro_radio/core/music_search.py` / `core/pipeline.py` / `core/tts.py` は
# core 内容品質 管轄で未使用 import が解消済み。ここに残すと
# `test_allowlist_contains_no_files_without_violations` が腐敗を検出する。

KNOWN_W292_ALLOWLIST = frozenset({
    "retro_radio/__init__.py",
    "retro_radio/app/plan_control.py",
    "retro_radio/core/__init__.py",
    "retro_radio/core/batch_processor.py",
    "retro_radio/core/fallback.py",
    "retro_radio/core/pipeline.py",
    "retro_radio/core/tts.py",
    "retro_radio/locales/__init__.py",
    "retro_radio/services/__init__.py",
    "retro_radio/utils/async_runner.py",
    "retro_radio/utils/i18n.py",
    "retro_radio/utils/logging_config.py",
    "retro_radio/utils/text_cleaner.py",
    "utils/design_tokens.py",
})

KNOWN_BOM_ALLOWLIST = frozenset({
    "tests/test_auth_db_integration.py",       # 認証/DB テスト 管轄
    "tests/test_db_favorite_repo.py",          # DB テスト 管轄
    "tests/test_db_generation_repo.py",        # DB テスト 管轄
    "tests/test_db_models.py",                 # DB テスト 管轄
    "tests/test_db_session.py",                # DB テスト 管轄
    "tests/test_db_user_repo.py",              # DB テスト 管轄
    "db/migrations/versions/125758440996_initial_migration.py",  # マイグレーション 管轄
})

# pip-audit が返した「配布名 -> (脆弱版, 最小修復版)」
KNOWN_VULNERABILITIES = {
    "click": ("8.1.8", "8.3.3"),
}

# requirements.txt の各パッケージに「なぜ要るか」を宣言する。
# import されないもの（CLI エントリ / プラグイン / 推移依存）は理由を明記して許容する。
IMPORT_NAME_BY_DISTRIBUTION = {
    "alembic": None,            # CLI エントリ + `env.py` は動的 import（`script_location` 経由）
    "psycopg": None,            # SQLAlchemy の方言プラグイン。`postgresql+psycopg://` で動的ロード
    "click": None,              # uvicorn/gTTS の推移依存（PYSEC-2026-2132 の固定対象）
    "edge-tts": "edge_tts",    # 無償のニューラル TTS（`tts_engine=auto` の既定）
    "fastapi": "fastapi",
    "google-genai": "genai",    # `from google import genai`
    "gTTS": "gtts",
    "mutagen": "mutagen",      # 生成済み mp3 の実測 duration（提案③-4、server.py）
    "pydantic": "pydantic",
    "pydantic-settings": "pydantic_settings",
    "requests": "requests",
    "sqlalchemy": "sqlalchemy",
    "stripe": "stripe",
    "tenacity": "tenacity",
    "uvicorn": None,            # CLI エントリ（Dockerfile CMD / 各デプロイ設定の起動コマンド）
}


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------
def test_no_stray_temp_files_in_repo_root():
    """P2-1: ルートに tmp_*.py / tmp_*.txt / pytest_*.txt などの作業用
    一時ファイルが残っていないこと。.gitignore で追跡対象外にしていても、
    ディスク上に残るとレビュー時の走査対象混入・誤コミットの原因になる。

    意図的に残すべきでないパターン（.gitignore の Temporary files と同期）:
    ``tmp_*``、``pytest_*.txt``、``eval_*.txt``、``eval_detail.json``。
    """
    offenders = [
        p.name
        for p in sorted(ROOT.iterdir())
        if p.is_file()
        and (
            p.name.startswith("tmp_")
            or (p.name.startswith("pytest_") and p.suffix == ".txt")
            or (p.name.startswith("eval_") and p.suffix == ".txt")
            or p.name == "eval_detail.json"
        )
    ]
    assert not offenders, f"ルートに一時ファイルが残っています: {offenders}"


def _iter_python_files(*roots: str):
    """指定ディレクトリ配下の .py を（BOM の有無に関わらず）列挙する。"""
    for rel in roots:
        base = ROOT / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIP_DIRS & set(path.relative_to(ROOT).parts):
                continue
            yield path


def _rel(path: Path) -> str:
    """リポジトリルートからの相対パス（常に `/` 区切り・CI/Linux と一致させる）。"""
    return path.relative_to(ROOT).as_posix()


def _requirement_lines(path: Path) -> dict:
    """requirements*.txt を {配布名: 宣言文字列} にパースする。"""
    declared = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip()
        if name:
            declared[name] = line
    return declared


def _run_flake8(select: str, targets: list) -> list:
    """baseline を完全に外して flake8 を 1 回だけ叩き、`(ファイル, コード, メッセージ)` を返す。

    CI は `--config .github/flake8-app-baseline.ini` で F401 / W292 を
    意図的に無効化しているため、ここでは `--isolated` で素の検査を行う。
    出力を 1 行ずつ parse して返すことで、Windows/Unix のパス区切り差を吸収する。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "flake8", "--isolated", f"--select={select}", *targets],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode not in (0, 1):
        raise AssertionError(
            f"flake8 の実行に失敗しました（returncode={proc.returncode}）:\n{proc.stderr}"
        )
    violations = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        # flake8 の出力は `path:lineno:col: CODE message`（コロンは 3 個）
        parts = line.split(":", 3)
        if len(parts) < 4:
            violations.append((line.strip().replace("\\", "/"), "", ""))
            continue
        path, _lineno, _col, rest = parts
        rest = rest.strip()
        code = rest.split(" ")[0] if rest else ""
        violations.append((path.strip().replace("\\", "/"), code, rest))
    return violations


@pytest.fixture(scope="session")
def f401_report():
    """F401（未使用 import）の結果。session スコープで 1 回だけ実行する（数秒）。"""
    return _run_flake8("F401", ["retro_radio", "utils"])


@pytest.fixture(scope="session")
def w292_report():
    """W292（末尾改行なし）の結果。session スコープで 1 回だけ実行する（数秒）。"""
    return _run_flake8("W292", ["retro_radio", "utils", "tests", "db", "scripts"])


# ===========================================================================
# 1. UTF-8 BOM が Python ソースに混入していないこと
# ===========================================================================
class TestNoBom:
    """BOM（U+FEFF）混入の防止。

    BOM が入ると `ast.parse` が `SyntaxError: invalid non-printable
    character U+FEFF` を投げ、ruff / black / grep 系の行が 1 桁ずれる。
    """

    def _files_with_bom(self, roots) -> list:
        return [
            _rel(p) for p in _iter_python_files(*roots)
            if p.read_bytes().startswith(b"\xef\xbb\xbf")
        ]

    def test_package_sources_have_no_bom(self):
        """アプリ本体（retro_radio/）の Python ファイルに BOM が無いこと。"""
        offenders = self._files_with_bom(["retro_radio"])
        assert not offenders, f"UTF-8 BOM が混入しています（BOM を除去してください）: {offenders}"

    def test_owned_files_have_no_bom(self):
        """D5 管轄のファイルに BOM が無いこと（上の部分集合に対する厳密版）。"""
        offenders = [rel for rel in OWNED_PACKAGE_FILES
                     if (ROOT / rel).read_bytes().startswith(b"\xef\xbb\xbf")]
        assert not offenders, f"BOM が残っています: {offenders}"

    def test_python_sources_parse_without_bom_error(self):
        """BOM が無くても全ソースが `ast.parse` を通ること（構文も壊れていない）。"""
        broken = {}
        for path in _iter_python_files("retro_radio", "utils"):
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                broken[_rel(path)] = f"line {exc.lineno}: {exc.msg}"
        assert not broken, f"ast.parse に失敗したファイルがあります: {broken}"

    def test_repo_wide_bom_is_limited_to_documented_allowlist(self):
        """repo 全体で BOM が無いこと。

        `KNOWN_BOM_ALLOWLIST` は他ワークストリームの管轄で未修正のファイルを
        明示的に列挙したもの。许可リストに無い新規 BOM は必ず落ちる。
        """
        offenders = set()
        for path in ROOT.rglob("*.py"):
            if SKIP_DIRS & set(path.relative_to(ROOT).parts):
                continue
            if path.read_bytes().startswith(b"\xef\xbb\xbf"):
                offenders.add(_rel(path))
        unexpected = sorted(offenders - KNOWN_BOM_ALLOWLIST)
        assert not unexpected, f"許可リストに無いファイルに BOM が混入しています: {unexpected}"

    def test_permit_list_has_no_stale_entries(self):
        """BOM 許可リストに、解消済みのエントリが残っていないこと（腐化の検出）。"""
        offenders = {
            _rel(p)
            for p in ROOT.rglob("*.py")
            if not (SKIP_DIRS & set(p.relative_to(ROOT).parts))
            and p.read_bytes().startswith(b"\xef\xbb\xbf")
        }
        stale = sorted(KNOWN_BOM_ALLOWLIST - offenders)
        assert not stale, f"BOM 許可リストに解消済みのエントリが残っています: {stale}"


# ===========================================================================
# 1a. 追跡ファイルに資格情報リテラルが無いこと
# ===========================================================================
#: 資格情報らしきリテラルの変数名・キー名。
_SECRET_NAME_PATTERN = re.compile(
    r"(?i)\b(?:default_?|hardcoded_|fallback_)?"
    r"(?:password|passwd|pwd|secret|api_?key|token|private_?key|shared_?secret)"
    r"\b"
)
#: 変数代入の右辺が文字列リテラルで、かつ短いものだけを見る（長い文章は誤検出する）。
_ASSIGN_LITERAL = re.compile(
    r"""(?m)^\s*(?P<name>[A-Z_][A-Z0-9_]*)\s*(?::[^=\n]+)?=\s*(?P<value>["'][^"'\n]{3,120}["'])"""
)
#: テストが使う固定値。これらは**秘密ではない**ので明示的に除外する。
_SECRET_LITERAL_ALLOWLIST = frozenset({
    # テスト用の固定秘密鍵（`tests/conftest.py` とその利用側）。
    "pytest-secret-key-not-for-production",
    "s4-test-secret-key",
    "server-api-auth-test-secret-key",
    "server-api-auth-test-single-user-key",
    "s3cret",
    "right",
    "k",
    "individual-key",
    # テスト用の資格情報キー。
    "individual-key", "correct-key", "correct-key-2", "unit-test-key",
    # プレースホルダ / 例示。
    "changeme", "your-secret-key-here", "xxx", "placeholder",
})
#: `tests/` 配下は資格情報リテラルの検査対象外（テストは固定値で動く）。
_SECRET_SCAN_SKIP_DIRS = SKIP_DIRS | {"tests", ".github"}


class TestNoCommittedCredentials:
    """追跡ファイルに「_embed_された資格情報」が無いこと。

    なぜこれが問題か: `scripts/create_admin.py` に実メールアドレスと
    8 文字のパスワードが `DEFAULT_EMAIL` / `DEFAULT_PASSWORD` として
    コミットされていた（実測）。引数なし実行でそのアカウントを PRO に
    昇格でき、**リポジトリを読めた全員が資格情報を知っていた**。

    機械検査が無かったため検出されずに残っていた。`tests/test_security.py`
    は応答本文を、`tests/test_auth_wiring.py` は `.env.example` の
    キー**存在**を調べるだけで、ソース中のリテラルは見ていなかった。
    """

    def test_no_credential_literals_in_python_sources(self):
        offenders = []
        for path in _iter_python_files("retro_radio", "scripts", "utils", "db", "eval"):
            rel = _rel(path)
            if set(rel.split("/")) & _SECRET_SCAN_SKIP_DIRS:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                match = _ASSIGN_LITERAL.match(line)
                if not match:
                    continue
                name, value = match.group("name"), match.group("value").strip("\"'")
                if not _SECRET_NAME_PATTERN.search(name):
                    continue
                if value.lower() in _SECRET_LITERAL_ALLOWLIST:
                    continue
                if value.startswith(("os.environ", "getenv", "secrets.", "env:")):
                    continue
                offenders.append(f"{rel}:{lineno} {name} = {value!r}")
        assert not offenders, (
            "ソースに資格情報リテラルが埋め込まれています。"
            "既定値を持たない（引数を必須にする）か、環境変数から読む形に変更して"
            "ください:\n" + "\n".join(offenders)
        )

    def test_create_admin_refuses_to_run_without_arguments(self):
        """`create_admin.py` は引数なし実行を拒否すること。

        引数なし実行が既定アカウントを更新する形だと、
        「スクリプトを 1 回実行した」だけで実アカウントの資格が書き換わる。
        スクリプトの入口として安全であることを固定する。
        """
        import subprocess
        import sys as _sys

        script = ROOT / "scripts/create_admin.py"
        source = script.read_text(encoding="utf-8")
        assert "DEFAULT_PASSWORD" not in source, (
            "create_admin.py に DEFAULT_PASSWORD が復活しています"
        )
        assert "DEFAULT_EMAIL" not in source, (
            "create_admin.py に DEFAULT_EMAIL が復活しています"
        )

        proc = subprocess.run(
            [_sys.executable, str(script)],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        assert proc.returncode != 0, (
            "引数なし実行が 0 で終わりました（資格情報の既定値が残っています）"
        )


# ===========================================================================
# 1b. 日本語ソースに簡体字（簡体字中国語）が混ざっていないこと
# ===========================================================================
#: 簡体字のうち、**日本語の常用漢字・当用漢字に存在しない字形だけ**を列挙したもの。
#: 1 文字でも含まれれば「簡体字の混入」とみなす。
#:
#: 誤検出を避けた選別根拠:
#: - U+4E0E と U+4E49 は「付与・寄与」と「定義・意義」で**正当な日本語**のため**除外**。
#: - 残りはすべて「日本語では書かない字形」だけ（例: U+8FD9→U+9059, U+65F6→U+6642）。
#:
#: このファイル自身は字面を**エスケープ列**で組み立てているため、
#: 検出対象にならない（自己検出の誤検出を避ける）。
_SIMPLIFIED_CODEPOINTS = (
    0x8FD9, 0x4E2A, 0x4EEC, 0x4ECE, 0x65F6, 0x95F4, 0x636E, 0x6237, 0x95EE, 0x9898,
    0x8FD8, 0x8BF4, 0x8C01, 0x8BA9, 0x8BE5, 0x4E48, 0x6837, 0x536B, 0x4E66, 0x98CE,
    0x8D1D, 0x9875, 0x5355, 0x4E25, 0x4E3D, 0x56FE, 0x56E2, 0x573A, 0x62A5, 0x53D8,
    0x8FB9, 0x5904, 0x8FBE, 0x5E26, 0x5BFC, 0x5C9B, 0x52A8, 0x6076, 0x53D1, 0x89C2,
    0x5E7F, 0x89C4, 0x5F52, 0x8FC7, 0x534E, 0x574F, 0x6B22, 0x83B7, 0x51FB, 0x7EE7,
    0x7C7B, 0x8FDE, 0x8BBA, 0x9A6C, 0x95E8, 0x96BE, 0x9E1F, 0x8BA4, 0x626B, 0x4F24,
    0x7ECD, 0x8BBE, 0x5E08, 0x8BC6, 0x8BD5, 0x89C6, 0x82CF, 0x5C81, 0x5B59, 0x8C08,
    0x6C64, 0x5934, 0x7F51, 0x4E3A, 0x95FB, 0x65E0, 0x52A1, 0x620F, 0x7EC6, 0x53BF,
    0x54CD, 0x5174, 0x987B, 0x9009, 0x4E9A, 0x9633, 0x4E1A, 0x8BAE, 0x94F6, 0x8FDC,
    0x613F, 0x8FD0, 0x6742, 0x810F, 0x8D23, 0x6218, 0x5F20, 0x8BC1, 0x949F, 0x79CD,
    0x4F17, 0x4E13, 0x8F6C, 0x8D44, 0x7EC4, 0x8BC9, 0x8BA8,
)
SIMPLIFIED_ONLY_CHARS = frozenset(chr(cp) for cp in _SIMPLIFIED_CODEPOINTS)


def _simplified_hits(text: str) -> list:
    """`text` に含まれる簡体字を、重複を除いて返す。"""
    return sorted({ch for ch in text if ch in SIMPLIFIED_ONLY_CHARS})


class TestNoSimplifiedChineseInJapaneseCode:
    """日本語の**コード**に簡体字が混ざっていないこと。

    なぜこれが問題か: 過去の生成セッションで簡体字がコメント・docstring に
    混入していた（実測 41 ファイル・61 箇所。うち `retro_radio/server.py` の
    **API 応答文字列**にも入っていた）。コードの意味は変わらないが、
    レビュー時に「誤植か意図か」が読めなくなり credibility そのものが落ちる。

    検査対象は `retro_radio/` `eval/` `scripts/` `tests/` `static/` `db/` の
    コードファイルに限定する。次は**対象外**:

    - データファイル（`songs.json` 等）— 中国語の曲名があり得る正本文書。
    - `docs/` `plans/` — 引用文献の原題など、外語を正当に含む散文。
    - `SKIP_DIRS`（VCS・キャッシュ・エディタ状態）。
    """

    #: 検査対象のトップレベルディレクトリ。
    TARGET_DIRS = ("retro_radio", "eval", "scripts", "tests", "static", "db")
    #: 検査対象の拡張子。
    TARGET_SUFFIXES = (".py", ".js", ".html", ".css")

    def _offenders(self) -> list:
        offenders = []
        for top in self.TARGET_DIRS:
            base = ROOT / top
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in self.TARGET_SUFFIXES:
                    continue
                if SKIP_DIRS & set(path.relative_to(ROOT).parts):
                    continue
                if _rel(path) == _rel(Path(__file__)):
                    continue  # この定義ファイル自身は字面を持ち得ない
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    hits = _simplified_hits(line)
                    if hits:
                        offenders.append(
                            f"{_rel(path)}:{lineno} [{''.join(hits)}] {line.strip()[:90]}"
                        )
        return offenders

    def test_no_simplified_chinese_chars_in_code(self):
        offenders = self._offenders()
        assert not offenders, (
            "コードに簡体字が混入しています。日本語表記に直してください"
            "（利用者に見える文字列であれば表示の不具合そのもの）:\n"
            + "\n".join(offenders)
        )


# ===========================================================================
# 1c. コードにハングル（韓国語）が混ざっていないこと
# ===========================================================================
#: ハングルの音節・字母の範囲。日本語・簡体字中国語とは**重ならない**ため、
#: 1 文字でも含まれれば混入とみなせる（CJK 互換字母の一部は除く）。
_HANGUL_RANGES = (
    (0xAC00, 0xD7A3),   # ハングル音節
    (0x1100, 0x11FF),   # ハングル字母
    (0xA960, 0xA97F),   # ハングル字母拡張 A
    (0xD7B0, 0xD7FF),   # ハングル字母拡張 B
    (0x3131, 0x318E),   # ハングル互換字母（日本語と重ならない範囲のみ）
)


def _is_hangul(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _HANGUL_RANGES)


class TestNoHangulInJapaneseCode:
    """日本語のコードに**ハングル**が混ざっていないこと。

    なぜこれが問題か: 簡体字中文の検査（``TestNoSimplifiedChineseInJapaneseCode``）を
    先に作ったら、**ハングルが，对其検査を通り抜けた**（実測 20 ファイル 20 箇所）。
    検査済みのように見えるコードが、実際には
    「1 件も 無い」のように読めなくなる。レビュー可能性が落ちる。

    ハングルは日本語と字形が重ならないため**誤検出ゼロ**。
    コード・テスト・ドキュメントすべてを対象にする。
    """

    TARGET_DIRS = ("retro_radio", "eval", "scripts", "tests", "static", "db", "docs", "plans")
    TARGET_SUFFIXES = (".py", ".js", ".html", ".css", ".md", ".yml", ".yaml")

    def _offenders(self) -> list:
        offenders = []
        for top in self.TARGET_DIRS:
            base = ROOT / top
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in self.TARGET_SUFFIXES:
                    continue
                if SKIP_DIRS & set(path.relative_to(ROOT).parts):
                    continue
                if _rel(path) == _rel(Path(__file__)):
                    continue  # この定義ファイル自身は字面を持ち得ない
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    hits = sorted({ch for ch in line if _is_hangul(ch)})
                    if hits:
                        offenders.append(
                            f"{_rel(path)}:{lineno} [{''.join(hits)}] {line.strip()[:90]}"
                        )
        return offenders

    def test_no_hangul_chars(self):
        offenders = self._offenders()
        assert not offenders, (
            "ハングル（韓国語）が混入しています。日本語表記に直してください:\n"
            + "\n".join(offenders)
        )


# ===========================================================================
# 1d. コード/ドキュメントに Unicode 置換文字（U+FFFD）が混ざっていないこと
# ===========================================================================
class TestNoReplacementCharacters:
    """**壊れた UTF-8 の残骸**（U+FFFD）が残っていないこと。

    U+FFFD は「その位置のバイトを UTF-8 として解釈できなかった」印で、
    日本語の文字が**読み取れなくなった**状態そのもの。
    目に見える形で本文が欠けるため、レビュー時に気づかないまま確定する。
    ただし**検査の意図で U+FFFD を書くテスト**は許可する。
    """

    TARGET_DIRS = ("retro_radio", "eval", "scripts", "tests", "static", "db", "docs", "plans")
    TARGET_SUFFIXES = (".py", ".js", ".html", ".css", ".md", ".yml", ".yaml", ".toml", ".ini")
    #: U+FFFD を**検査のために**書いているテスト（残骸ではない）。
    ALLOWED = frozenset({
        "tests/test_deployment.py",
        "tests/test_env_templates.py",
        "tests/test_operations.py",
    })

    def _offenders(self) -> list:
        offenders = []
        for top in self.TARGET_DIRS:
            base = ROOT / top
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in self.TARGET_SUFFIXES:
                    continue
                rel = _rel(path)
                if rel in self.ALLOWED or rel == _rel(Path(__file__)):
                    continue
                if SKIP_DIRS & set(path.relative_to(ROOT).parts):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if "\ufffd" in line:
                        offenders.append(f"{rel}:{lineno} {line.strip()[:90]}")
        return offenders

    def test_no_replacement_characters(self):
        offenders = self._offenders()
        assert not offenders, (
            "Unicode 置換文字（U+FFFD）が出ています。文字化けした原稿です:\n"
            + "\n".join(offenders)
        )


# ===========================================================================
# 2. サポート終了した旧 Gemini SDK の残存防止
# ===========================================================================
class TestLegacyGeminiSdkRemoved:
    """`google-generativeai`（2026 年にサポート終了）の撤去と再混入の防止。

    旧 SDK は import 時に FutureWarning を出し、さらに tenacity の再試行ごとに
    「新 SDK 1 回 + 旧 SDK 1 回」= 最大 2 倍の API 呼び取りになるため残さない。
    """

    LEGACY_IMPORT_PATTERNS = (
        r"\bimport\s+google\.generativeai\b",
        r"\bfrom\s+google\.generativeai\b",
        r"\bfrom\s+google\s+import\s+generativeai\b",
    )

    def test_no_legacy_sdk_import_in_package(self):
        """retro_radio/** に旧 SDK の import が無いこと（docstring の言及は許可）。"""
        hits = []
        for path in _iter_python_files("retro_radio"):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                for pattern in self.LEGACY_IMPORT_PATTERNS:
                    if re.search(pattern, line):
                        hits.append(f"{_rel(path)}:{lineno}: {line.strip()}")
        assert not hits, "旧 SDK の import が残っています:\n" + "\n".join(hits)

    def test_legacy_sdk_not_in_requirements(self):
        """requirements.txt / requirements-dev.txt に旧 SDK が無いこと。"""
        for name in ("requirements.txt", "requirements-dev.txt"):
            assert "google-generativeai" not in _requirement_lines(ROOT / name), (
                f"{name} にサポート終了済みの google-generativeai が残っています"
            )

    def test_import_does_not_raise_future_warning(self):
        """モジュール読み込み時に FutureWarning が出ないこと（警告を例外化して実測）。"""
        proc = subprocess.run(
            [sys.executable, "-W", "error::FutureWarning", "-c",
             "import retro_radio.core.script_generator"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert proc.returncode == 0, (
            "script_generator の import で警告/例外が発生しました:\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
        assert "FutureWarning" not in proc.stderr, f"FutureWarning が出ています:\n{proc.stderr}"

    def test_new_sdk_is_the_only_declared_sdk(self):
        """新 SDK（google-genai）は宣言済みであること（旧 SDK 撤去と対で担保）。"""
        assert "google-genai" in _requirement_lines(ROOT / "requirements.txt")

    def test_call_gemini_has_no_legacy_fallback_branch(self):
        """`_call_gemini` に旧 SDK へのフォールバック分岐が残っていないこと。"""
        text = (ROOT / "retro_radio/core/script_generator.py").read_text(encoding="utf-8")
        body = text.split("def _call_gemini", 1)[1].split("\ndef ", 1)[0]
        assert "genai.GenerativeModel" not in body, "旧 SDK の GenerativeModel 分岐が残っています"
        assert "genai.configure" not in body, "旧 SDK の configure 分岐が残っています"
        assert "client.models.generate_content" in body, "新 SDK の呼び出しがありません"

    def test_fallback_import_direction_is_preserved(self):
        """`core/fallback.py` からの import 方向と公開関数が維持されていること。

        他の作業ストリームが `fallback.py` を編集するため、本ファイル側は
        import 契約を変えない（公開 API の維持が D4 側の前提条件）。
        """
        text = (ROOT / "retro_radio/core/script_generator.py").read_text(encoding="utf-8")
        assert "from ..core.fallback import" in text
        for func in ("generate_fallback_script", "generate_care_script",
                     "generate_anniversary_script"):
            assert re.search(rf"\b{func}\b", text), f"{func} への参照が消えています"
        fallback = (ROOT / "retro_radio/core/fallback.py").read_text(encoding="utf-8")
        for func in ("generate_fallback_script", "generate_care_script",
                     "generate_anniversary_script"):
            assert re.search(rf"^def {func}\(", fallback, re.M), (
                f"core/fallback.py に公開関数 {func} がありません（import 先と不一致）"
            )

    def test_api_key_error_message_contract_is_preserved(self):
        """`app_error_handler` が 503 判定に使う「APIキー」文字列を保持していること。

        `retro_radio/server.py` の `app_error_handler` は
        `if "APIキー" in exc.user_message` で 503 を返す契約。
        この文字列が変わると 503 が 400 に落ちるため固定する。
        """
        text = (ROOT / "retro_radio/core/script_generator.py").read_text(encoding="utf-8")
        match = re.search(
            r'ScriptGenerationError\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)', text
        )
        assert match, "ScriptGenerationError の送出箇所が見つかりません"
        assert "APIキー" in match.group(1), (
            f"user_message に 'APIキー' がありません: {match.group(1)!r}"
        )
        assert match.group(2) == "GEMINI_API_KEY not configured"

    def test_generate_radio_script_still_returns_fallback(self):
        """新 SDK が失敗しても `generate_radio_script` は必ず空でない文字列を返すこと。"""
        import retro_radio.core.script_generator as sg

        original_key = sg.settings.gemini_api_key
        original_call = sg._call_gemini
        try:
            sg.settings.gemini_api_key = "dummy-key-for-hygiene-regression"
            sg._call_gemini = lambda prompt: (_ for _ in ()).throw(RuntimeError("boom"))
            script = sg.generate_radio_script(1975, 9, 24)
        finally:
            sg.settings.gemini_api_key = original_key
            sg._call_gemini = original_call
        assert isinstance(script, str) and script.strip(), "フォールバック原稿が空です"


# ===========================================================================
# 3. .dockerignore による秘密・DB・テストの焼き込み防止
# ===========================================================================
def _dockerignore_patterns() -> list:
    """`.dockerignore` をコメント・空行を除いたリストにする。"""
    path = ROOT / ".dockerignore"
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _dockerignore_excludes(rel_path: str) -> bool:
    """Docker の .dockerignore 意味論で、1 つのパスが除外されるかを判定する。

    Go の patternmatcher 完全実装ではなく、本リポジトリが使う構文
    （`dir/`・`*.ext`・`name`・`!` による再包含）だけを扱う。
    親ディレクトリが除外されたらその配下も除外とし、後から評価した
    パターン（`!` による再包含を含む）が優先される。
    """
    parts = Path(rel_path).as_posix().split("/")
    candidates = ["/".join(parts[:i + 1]) for i in range(len(parts))]
    excluded = False
    for raw in _dockerignore_patterns():
        negate = raw.startswith("!")
        pattern = (raw[1:] if negate else raw).rstrip("/")
        for candidate in candidates:
            if candidate == pattern or fnmatch.fnmatch(candidate, pattern):
                excluded = not negate
                break
    return excluded


class TestDockerignore:
    """`COPY . .` が .env / *.db / tests/ をイメージに焼き込まないこと。

    Dockerfile は `COPY . .` の **後** に `useradd` するため、
    ビルド時点のファイルはイメージ層に平文で残る（後段で消しても消えない）。
    """

    def test_dockerignore_exists(self):
        assert (ROOT / ".dockerignore").is_file(), (
            ".dockerignore がありません。Docker は .gitignore を読まないため、"
            "`COPY . .` が .env / retro_radio.db をイメージに含めてしまう"
        )

    def test_dockerignore_mentions_required_patterns(self):
        """最低限の除外パターンが直接書かれていること（可読性・レビュー容易性）。"""
        patterns = set(_dockerignore_patterns())
        for required in (".env", "*.db", "__pycache__/", ".git/",
                         ".hypothesis/", ".pytest_cache/", "tests/"):
            assert required in patterns, f".dockerignore に '{required}' がありません"

    @pytest.mark.parametrize("rel_path", [
        ".env",
        ".env.local",
        "retro_radio.db",
        "retro_radio.db-wal",
        "retro_radio.db-shm",
        "tests/test_ui_ux.py",
        ".hypothesis/example/abc123",
        ".pytest_cache/v/cache/lastfailed",
        "retro_radio/__pycache__/config.cpython-313.pyc",
        "demo.gif",
        "README.md",
        "docs/operations/x.md",
    ])
    def test_secrets_databases_and_tests_are_excluded(self, rel_path):
        assert _dockerignore_excludes(rel_path), (
            f"'{rel_path}' がイメージに焼き込まれます（.dockerignore に追加してください）"
        )

    @pytest.mark.parametrize("rel_path", [
        "requirements.txt",
        "retro_radio/server.py",
        "retro_radio/core/script_generator.py",
        "retro_radio/locales/ja.json",
        "static/index.html",
        "static/app.js",
        "static/app.css",
        "alembic.ini",
        "db/migrations/env.py",
        "scripts/init_db.py",
        ".env.example",
    ])
    def test_runtime_files_are_kept(self, rel_path):
        """Dockerfile とデプロイ設定が必要とするファイルは除外してはいけない。"""
        assert not _dockerignore_excludes(rel_path), (
            f"'{rel_path}' が除外されています。イメージでの起動やマイグレーションが"
            f"壊れます（Dockerfile / render.yaml / fly.toml を確認してください）"
        )

    def test_every_static_asset_survives(self):
        """static/ 配下は 1 ファイルも除外されないこと（アプリが配信する）。"""
        static_dir = ROOT / "static"
        if not static_dir.is_dir():
            pytest.skip("static/ が存在しません")
        excluded = [
            _rel(p) for p in sorted(static_dir.rglob("*"))
            if p.is_file() and _dockerignore_excludes(_rel(p))
        ]
        assert not excluded, f"static/ の資産が除外されています: {excluded}"


# ===========================================================================
# 4. 脆弱な推移依存の固定
# ===========================================================================
class TestVulnerableTransitiveDependencies:
    """既知の脆弱性 ID に対して、解決可能な範囲で固定が宣言されていること。

    `pip-audit` を pytest 内で実行するのは重すぎる（ネットワーク依存・数十秒）ため、
    ここでは宣言の**形**だけを担保する。実際の監査は CI 側で
    `pip-audit -r requirements.txt` を回すこと。
    """

    def test_vulnerable_version_is_not_pinned_exactly(self):
        """脆弱版を完全固定（==）していないこと。"""
        declared = _requirement_lines(ROOT / "requirements.txt")
        for name, (vulnerable, _fixed) in KNOWN_VULNERABILITIES.items():
            spec = declared.get(name)
            if spec is None:
                continue
            assert f"=={vulnerable}" not in spec, (
                f"{name}=={vulnerable} は脆弱版として固定されています（{spec}）"
            )

    def test_vulnerable_transitive_dependency_is_declared_explicitly(self):
        """click が推移依存に埋もれたままだと、pip は毎回脆弱版へ解決し得る。"""
        declared = _requirement_lines(ROOT / "requirements.txt")
        assert "click" in declared, (
            "click が requirements.txt に無いと、pip の解決のたびに脆弱版へ戻る可能性があります"
        )

    def test_uncapped_click_floor_is_documented(self):
        """click の上限が修復版未満なら、理由と脆弱性 ID をコメントに明記すること。

        gTTS が `click<8.2` を宣言しているため 2026-09-29 時点で
        `click>=8.3.3` は解決不能（実測: ResolutionImpossible）。
        約束を残したまま「直す手段が無い」状態を次の扱当者に引き継い。
        """
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        spec = _requirement_lines(ROOT / "requirements.txt").get("click", "")
        upper = re.search(r"<\s*([0-9.]+)", spec)
        capped_below_fix = bool(upper) and tuple(
            int(x) for x in upper.group(1).split(".")
        ) < (8, 3)
        if not capped_below_fix:
            return
        for _name, (vulnerable, fixed) in KNOWN_VULNERABILITIES.items():
            assert "PYSEC" in text, "脆弱性 ID（例: PYSEC-2026-2132）がコメントにありません"
            assert vulnerable in text, f"脆弱版 {vulnerable} の記載がありません"
            assert fixed in text, f"最小修復版 {fixed} の記載がありません"
        assert "gTTS" in text, "click の上限が gTTS 制約由来なのに、その仕組みの名前がコメントにありません"

    def test_no_fully_pinned_requirements(self):
        """完全固定（==）が混入していないこと（更新の手動運用を避ける）。"""
        for name, spec in _requirement_lines(ROOT / "requirements.txt").items():
            assert "==" not in spec, (
                f"{name} は完全固定（==）です。下限指定（>=）に統一してください: {spec}"
            )


# ===========================================================================
# 5. 未使用 import（F401）
# ===========================================================================
class TestNoUnusedImports:
    """CI は baseline 設定で F401 を無効化しているため、ここでは素の検査を行う。"""

    def test_owned_files_have_no_unused_imports(self, f401_report):
        """D5 管轄のファイルに未使用 import が無いこと（許容リストでSpeakさない厳密版）。"""
        owned = set(OWNED_PACKAGE_FILES)
        offenders = [f"{p}:{msg}" for p, code, msg in f401_report
                     if code == "F401" and p in owned]
        assert not offenders, "未使用 import があります:\n" + "\n".join(offenders)

    def test_no_new_unused_imports_outside_allowlist(self, f401_report):
        """リポジトリ全体で F401 が無いこと（他管轄の残骸は明示列挙のみ許容）。"""
        offenders = sorted({p for p, code, _msg in f401_report if code == "F401"})
        unexpected = sorted(set(offenders) - KNOWN_F401_ALLOWLIST)
        assert not unexpected, (
            "未使用 import が新たに発生しました（許可リストを更新してください）:\n"
            + "\n".join(unexpected)
        )

    def test_allowlist_contains_no_files_without_violations(self, f401_report):
        """許可リストのエントリが実際に違反を持つファイルだけであること（腐化の検出）。"""
        offenders = {p for p, code, _msg in f401_report if code == "F401"}
        stale = sorted(KNOWN_F401_ALLOWLIST - offenders)
        assert not stale, (
            f"許可リストに違反が解消済みのファイルが残っています（削除してください）: {stale}"
        )


# ===========================================================================
# 6. 末尾改行（W292）
# ===========================================================================
class TestTrailingNewline:
    """Python ファイルに末尾改行があること（差分ツールのノイズ防止）。"""

    def test_owned_files_end_with_newline(self):
        offenders = [
            rel for rel in OWNED_PACKAGE_FILES
            if not (ROOT / rel).read_bytes().endswith(b"\n")
        ]
        assert not offenders, f"末尾改行がありません: {offenders}"

    def test_no_new_missing_trailing_newline(self, w292_report):
        """アプリ・テスト・マイグレーション・運用スクリプトに新規 W292 が無いこと。"""
        offenders = sorted({p for p, code, _msg in w292_report if code == "W292"})
        unexpected = sorted(set(offenders) - KNOWN_W292_ALLOWLIST)
        assert not unexpected, (
            "末尾改行の無いファイルが増えました（許可リストから削除してください）:\n"
            + "\n".join(unexpected)
        )


# ===========================================================================
# 10. テストが import 時に os.environ を書き換えていないこと（実行順序依存の防止）
# ===========================================================================
def _module_level_env_writes(path: Path) -> list:
    """モジュールの**トップレベル**で `os.environ` を書き換える行を返す。

    `ast` で body を直接歩くので、関数・クラス・フィクスチャの中で
    `monkeypatch.setenv` を正常使用しているコードは検出されない
    （それは正しい做法）。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):  # pragma: no cover - 壊れたファイルは他テストが拾う
        return []

    found = []
    for node in tree.body:
        # `os.environ[...] = v` / `os.environ.setdefault(...)` / `os.environ.update(...)`
        targets: list = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if _is_environ_subscript(target):
                found.append(node.lineno)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in ("setdefault", "update", "pop", "popitem", "clear")
                and _is_environ(func.value)
            ):
                found.append(node.lineno)
    return sorted(set(found))


def _is_environ(node: ast.AST) -> bool:
    """`os.environ` / `environ` 参照か。"""
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    if isinstance(node, ast.Name) and node.id == "environ":
        return True
    return False


def _is_environ_subscript(node: ast.AST) -> bool:
    """`os.environ["X"] = ...` のような代入対象か。"""
    return (
        isinstance(node, ast.Subscript)
        and _is_environ(node.value)
    )


class TestNoImportTimeEnvMutation:
    """テストモジュールが import 時に `os.environ` を書き換えていないこと。

    なぜこれが禁止か:

    `retro_radio.server._auth_enforced()` は `settings`（import 時に
    `get_settings()` で冻结したオブジェクト）ではなく**環境変数を優先**して読む。
    そのため `os.environ.setdefault("RETRO_RADIO_REQUIRE_AUTH", ...)` を
    import 時に書くと、値は**プロセス全体で共有**され、実行順で結果が変わる。

    実際にこの不具合が发生时:
        pytest tests/test_server_api_auth.py tests/test_me_api.py tests/test_job_api.py
    が `24 failed, 76 passed` になり、既定のアルファベット順では
    全て通っていた（= 緑は信頼できない）。Fixture 化
    （`monkeypatch.setenv`）ならテスト終了時に必ず復元されるので順序に依存しない。
    """

    #: import 時に環境変数を決めてよい唯一のファイル（conftest が集める側）。
    ALLOWED = frozenset({"tests/conftest.py"})

    def test_no_module_level_environ_writes(self):
        offenders = []
        for path in sorted((ROOT / "tests").rglob("test_*.py")):
            if _rel(path) in self.ALLOWED:
                continue
            for lineno in _module_level_env_writes(path):
                offenders.append(f"{_rel(path)}:{lineno}")
        assert not offenders, (
            "import 時に os.environ を書き換えるテストがあります。"
            "実行順序依存の原因になるため、`monkeypatch.setenv` を使う "
            "autouse fixture に移してください:\n" + "\n".join(offenders)
        )


# ===========================================================================
# 7. .env.example の全キーが Settings に食わせられること
# ===========================================================================
def _env_example_entries() -> dict:
    """`.env.example` を {環境変数名: 生の値} にする（コメント・空行は除く）。"""
    entries = {}
    for raw in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("RETRO_RADIO_"):
            entries[key] = value.strip()
    return entries


class TestEnvExampleMatchesSettings:
    """`.env.example` は「コピーしてそのまま起動」できること。"""

    def test_every_settings_field_has_a_key(self):
        entries = _env_example_entries()
        missing = sorted(
            f"RETRO_RADIO_{name.upper()}" for name in Settings.model_fields
            if f"RETRO_RADIO_{name.upper()}" not in entries
        )
        assert not missing, f".env.example に設定項目が欠けています: {missing}"

    def test_no_unknown_keys(self):
        valid = {f"RETRO_RADIO_{name.upper()}" for name in Settings.model_fields}
        unknown = sorted(k for k in _env_example_entries() if k not in valid)
        assert not unknown, f"config.py に存在しない設定が混ざっています: {unknown}"

    def test_env_example_loads_into_settings(self, monkeypatch, tmp_path):
        """`.env.example` の値を実際に `Settings` に食わせて例外が出ないこと。

        `RETRO_RADIO_CORS_ORIGINS` は `List[str]` なので JSON 配列でなければ
        `SettingsError` になる。ここでは `.env` ファイルを一切作らず環境変数のみで
        ロードすることで、ファイル配置に隠れたバグも検出する。
        """
        entries = _env_example_entries()
        assert "RETRO_RADIO_CORS_ORIGINS" in entries, "CORS_ORIGINS の記載がありません"
        json.loads(entries["RETRO_RADIO_CORS_ORIGINS"])  # JSON 配列であることを確認

        for key in list(os.environ):
            if key.startswith("RETRO_RADIO_"):
                monkeypatch.delenv(key, raising=False)
        for key, value in entries.items():
            monkeypatch.setenv(key, value)

        # リポジトリ直下の .env を読まないように隔離ディレクトリへ移動する
        monkeypatch.chdir(tmp_path)
        settings = Settings(_env_file=None)
        assert settings.app_name
        assert isinstance(settings.cors_origins, list) and settings.cors_origins

    def test_cors_comment_does_not_contradict_parser(self):
        """`.env.example` の CORS コメントが実装の挙動と矛盾しないこと。

        コメントが「* やカンマ区切りで SettingsError」と書いているのに
        実装が受理すると、どちらかが嘘になる。どちらを信じるべきかは
        機械では決められないので、実測の受理結果と文言の整合だけを検証する。
        """
        assert parse_cors_origins("*") == ["*"]
        assert parse_cors_origins("http://a,http://b") == ["http://a", "http://b"]
        assert parse_cors_origins('["http://a","http://b"]') == ["http://a", "http://b"]

        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        assert "SettingsError になる" not in text, (
            "CORS コメントが「* やカンマ区切りで SettingsError」と断定していますが、"
            "parse_cors_origins はいずれも受理します（コメントだけ実装と逆）"
        )


# ===========================================================================
# 8. .env.example の値が config.py の既定値と矛盾しないこと
# ===========================================================================
#: `.env.example` の値が `config.py` の既定値と**意図的に**異なる項目。
#: ここは「相違を許す場所」ではなく「相違の理由を残す場所」。
#: 新しい項目を追加するときは「なぜコード既定値と違うのか」をコメントで必ず残すこと。
INTENTIONAL_ENV_EXAMPLE_DIVERGENCE = {
    # 乱数シードはコード既定値 None（OS のエントロピー）。
    # テンプレートでは eval・監査の再現性を確保するため固定シードを例示する。
    "RETRO_RADIO_RNG_SEED": "テンプレートは再現性のため固定シードを例示する（本番では任意）",
    # コード既定値は security-first（施設導入を想定して認証を必須にする）。
    # 一方 `.env.example` は **個人利用のクイックスタート**として配布する。
    # そのまま既定だと README のクイックスタートが 503 になり、
    # そのためだけ昇格した利用者が `require_auth=0` を自己責任で外す運用を
    # 前提にしたくない。コード既定値は変えず、テンプレート側で個人用に落とす。
    "RETRO_RADIO_REQUIRE_AUTH": "テンプレートは個人モードを配布する（施設は手順書に従い 1 にする）",
}


class TestEnvExampleValuesMatchDefaults:
    """`.env.example` に書く値は `config.py` の既定値と一致させること。

    **このテストの限界**: 既定値の**実在性**（モデル名・APIキー形式など）は
    検証しない。ネットワークもモックも使わないため、確認できるのは
    「テンプレート == コード既定値」だけである。つまり
    `RETRO_RADIO_GEMINI_MODEL=gemini-1.5-flash` のような退役モデルが
    `config.py` の既定値として入れ替わっていた場合、このテストは
    「両者が一致している」ことしか言わない。テンプレートの値鵜呑みにせず、
    `config.py` 側も併せてレビューすること。

    ただし `INTENTIONAL_ENV_EXAMPLE_DIVERGENCE` に登記した項目は**意図的な相違**として
    許容する。相違が生じるときは 1 箇所ずつ理由とともに登記すること。
    """

    @staticmethod
    def _coerce(raw: str, default):
        """環境変数の生文字列を、既定値と同じ型に寄せて比較する。"""
        if isinstance(default, bool):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        if isinstance(default, list):
            return parse_cors_origins(raw)
        return raw

    def test_values_match_config_defaults(self):
        entries = _env_example_entries()
        mismatches = []
        for name, field in Settings.model_fields.items():
            key = f"RETRO_RADIO_{name.upper()}"
            if key not in entries or entries[key] == "":
                continue
            if key in INTENTIONAL_ENV_EXAMPLE_DIVERGENCE:
                continue
            default = field.default
            try:
                actual = self._coerce(entries[key], default)
            except (TypeError, ValueError):
                continue  # 値の解釈は pydantic に任せる（例外は上記テストが検出する）
            if actual != default:
                mismatches.append(
                    f"{key}: .env.example={entries[key]!r} / config.py={default!r}"
                )
        assert not mismatches, "既定値と矛盾しています:\n" + "\n".join(mismatches)

    def test_gemini_model_template_matches_config_default(self):
        """`RETRO_RADIO_GEMINI_MODEL` が `config.py` の既定値と一致すること。

        モデルの**実在性は保証しない**（docstring 参照）。
        """
        entries = _env_example_entries()
        default = Settings.model_fields["gemini_model"].default
        assert entries.get("RETRO_RADIO_GEMINI_MODEL") == default

    def test_default_year_is_within_configured_range(self):
        """既定年が min/max の範囲に収まっていること（`validate_year_range` の前提）。"""
        min_year = Settings.model_fields["min_year"].default
        max_year = Settings.model_fields["max_year"].default
        default_year = Settings.model_fields["default_year"].default
        assert min_year <= default_year <= max_year


# ===========================================================================
# 9. requirements.txt のパッケージが実際に使われていること
# ===========================================================================
class TestRequirementPackagesAreUsed:
    """未使用パッケージの検出。

    `uvicorn`（CLI エントリ）のようにコードから import されないものは
    「理由つき許容」で受け入れる。許容の根拠は `IMPORT_NAME_BY_DISTRIBUTION`
    に「None = import されない（理由コメントつき）」として宣言すること。
    """

    @pytest.fixture(scope="session")
    def source_text(self) -> str:
        chunks = [
            path.read_text(encoding="utf-8", errors="replace")
            for path in _iter_python_files("retro_radio", "utils", "scripts", "db")
        ]
        return "\n".join(chunks)

    def test_no_undeclared_core_imports(self, source_text):
        """コア依存を import しているなら requirements.txt にも宣言されていること。

        宣言名は配布名（`gTTS`）で、import 名（`gtts`）とは綴りが違うことがあるため
        `IMPORT_NAME_BY_DISTRIBUTION` を逆引きしてから突き合わせる。
        """
        declared = _requirement_lines(ROOT / "requirements.txt")
        missing = []
        for distribution, import_name in IMPORT_NAME_BY_DISTRIBUTION.items():
            if not import_name:
                continue
            used = re.search(
                rf"^\s*(?:import|from)\s+{re.escape(import_name)}\b", source_text, re.M
            )
            if used and distribution not in declared:
                missing.append(f"{distribution} (import名: {import_name})")
        assert not missing, (
            f"import されているのに requirements.txt で未宣言です: {sorted(missing)}"
        )

    def test_every_requirement_has_a_justification(self):
        """requirements.txt の全配布名に「import 名」か「許容理由」が登記されていること。"""
        declared = _requirement_lines(ROOT / "requirements.txt")
        unknown = sorted(set(declared) - set(IMPORT_NAME_BY_DISTRIBUTION))
        assert not unknown, (
            "理由が登記されていない配布名があります（IMPORT_NAME_BY_DISTRIBUTION に"
            f"追加してください）: {unknown}"
        )

    def test_requirements_are_sorted(self):
        """宣言順がアルファベットで安定していること（差分の可読性 / 衝突の減少）。"""
        names = [
            re.split(r"[<>=!~\[; ]", line.strip(), maxsplit=1)[0].lower()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert names == sorted(names), f"パッケージがソートされていません: {names}"

    def test_no_duplicate_requirements(self):
        names = [
            re.split(r"[<>=!~\[; ]", line.strip(), maxsplit=1)[0].lower()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        assert not duplicates, f"重複した宣言があります: {duplicates}"


class TestNoVacuousAssertions:
    """空虚な assert（`assert True` / `assert x is not None` だけ）を塞ぐ。

    `tests/test_performance_baseline.py` は 3 関数すべてが
    `assert True  # プレースホルダー` で、**何も計測していないのに**
    `pytest` の passed 件数に含まれていた。「何が壊れても緑」に見える数字を
    信任させないためのガード。
    """

    #: スキャン対象の Python ソース（テストのみ。アプリ側はこちらで担保しない）。
    SCAN_GLOBS = ("tests/**/*.py",)

    def _python_files(self):
        for pattern in self.SCAN_GLOBS:
            for path in sorted(ROOT.glob(pattern)):
                if any(part in SKIP_DIRS for part in path.parts):
                    continue
                yield path

    def test_no_bare_assert_true_in_tests(self):
        """`assert True`（プレースホルダー）が残っていないこと。

    **docstring / コメント内の言及は除外**する（説明として書くのは正当なので）。
    """
        offenders = []
        for path in self._python_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assert):
                    continue
                test = node.test
                # `assert True` / `assert 1` / `assert "non-empty"` のみを対象にする。
                if isinstance(test, ast.Constant) and test.value in (True, 1, "x"):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        assert not offenders, (
            "空虚な assert があります（プレースホルダーのまま残されています）。"
            f"要么実測，要么 pytest.mark.skip にしてください: {offenders}"
        )

    def test_performance_baseline_has_real_assertions(self):
        """性能ベースラインのテストが**必ず何かを検証している**こと。

    ファイル名を「計測しているように見せる」前に、
    中身が空でないことを固定する。
    """
        target = ROOT / "tests" / "test_performance_baseline.py"
        tree = ast.parse(target.read_text(encoding="utf-8"))
        tests = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        assert len(tests) >= 4, f"性能テストが {len(tests)} 件しかありません"

        vacuous = [
            node.name
            for node in tests
            if not any(isinstance(n, ast.Assert) for n in ast.walk(node))
        ]
        assert not vacuous, (
            f"検証の無いテストがあります（assert が 1 度も無い）: {vacuous}"
        )


class TestNoUnprefixedEnvVarInTests:
    """接頭辞なしの環境変数设定を塞ぐ（**本番データを壊す事故**の防止）。

    `Settings` の `env_prefix` は `RETRO_RADIO_` なので、`DATABASE_URL` と
    書いても**まったく読まれない**。にもかかわらず
    `tests/test_db_session.py` / `tests/test_db_user_repo.py` は
    `os.environ['DATABASE_URL'] = 'sqlite:///:memory:'` を書いていた。

    結果として単体実行で **ルートの `retro_radio.db`**（本番用）が
    `drop_all` / `create_all` の対象になり、`test_hygiene_regression.py` の
    「ルートに一時ファイルが無いこと」が連動して赤になっていた（実測）。
    """
    #: 接頭辞なしで設定されると危険なもの（本番データや外部通信に触れる）。
    RISKY_UNPREFIXED = ("DATABASE_URL", "SECRET_KEY", "STRIPE_SECRET_KEY",
                        "GEMINI_API_KEY", "ELEVENLABS_API_KEY")

    #: 正当な用途（接頭辞なしが**無視される**ことを検証するテスト）。
    ALLOWED_CONTEXTS = (
        # 「接頭辞なしの DATABASE_URL では効かない」ことの検証そのもの。
        "tests/test_config.py",
        # このガード自身の説明文。
        "tests/test_hygiene_regression.py",
    )

    def test_no_unprefixed_env_assignment_in_tests(self):
        offenders = []
        for path in sorted((ROOT / "tests").glob("**/*.py")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in self.ALLOWED_CONTEXTS:
                continue
            source = path.read_text(encoding="utf-8")
            for name in self.RISKY_UNPREFIXED:
                for m in re.finditer(
                    rf"""environ\s*\[\s*['"]{name}['"]\s*\]|setenv\(\s*['"]{name}['"]""",
                    source,
                ):
                    line_no = source.count("\n", 0, m.start()) + 1
                    offenders.append(f"{rel}:{line_no} {name}")
        assert not offenders, (
            "接頭辞なしの環境変数を設定しています（Settings は "
            "RETRO_RADIO_ 接頭辞しか読みません）: " + ", ".join(offenders)
        )

    def test_conftest_pins_the_test_database(self):
        """`conftest.py` が**正しい名前で**テスト用 DB を指していることを確認する。"""
        source = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
        assert 'os.environ["RETRO_RADIO_DATABASE_URL"]' in source or (
            "os.environ['RETRO_RADIO_DATABASE_URL']" in source
        ), "conftest.py が RETRO_RADIO_DATABASE_URL を設定していません"
