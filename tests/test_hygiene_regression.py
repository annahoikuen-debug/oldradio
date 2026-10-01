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
KNOWN_F401_ALLOWLIST = frozenset({
    "retro_radio/app/plan_control.py",        # アプリ側バグ修正 管轄
    "retro_radio/services/export_service.py",  # 認証/サービス 管轄
    "utils/design_tokens.py",                 # フロントエンド 管轄
})
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
            "未使用 import が新たに発生しました（許可リスト要从Streamsしてください）:\n"
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
