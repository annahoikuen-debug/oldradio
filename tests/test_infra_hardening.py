"""Infrastructure hardening regression tests.

Each test pins one defect that made a deploy or a CI job fail outright, or that
made a configuration lie about itself. They parse the real files (YAML / TOML /
INI / JSON / Dockerfile) instead of grepping prose, so a comment cannot satisfy
them and a renamed key cannot silently pass.

Covered defects
---------------
1. fly.toml set ``RETRO_RADIO_DATABASE_URL = ""``. pydantic-settings treats the
   empty string as an override and does NOT fall back to the config.py default,
   so ``db/session.py`` called ``create_engine("")``; startup, the health check
   and the Fly release command all failed, i.e. every deploy was broken.
2. render.yaml never declared ``RETRO_RADIO_REQUIRE_AUTH``, so the app fell back
   to the code default (true) with no working login path and answered 401 on
   every protected API.
3. No PostgreSQL driver was installed although four documents describe
   PostgreSQL as the production path and ``db/session.py`` has a live
   ``postgresql`` branch.
4. fly / render / railway all ran SQLite on an ephemeral filesystem.
5. release.yml tried to build and publish a distribution package in a repo
   that has no pyproject.toml, no setup.py and no license.
6. security.yml uploaded pip-audit JSON as SARIF, used the retired
   ``codeql-action@v2``, granted no explicit permissions and referenced a
   dependabot config that did not exist.
7. ci.yml had no pip cache, used the deprecated ``codecov-action@v3`` and
   pinned every action to a mutable tag.
8. The Dockerfile installed alembic unversioned and outside requirements.txt,
   shipped a C toolchain in the runtime image and declared no VOLUME.
9. alembic.ini carried a ``sqlalchemy.url`` that ``db/migrations/env.py``
   always overwrites.
10. Lint tooling was split three ways (pre-commit ruff, CI flake8, dev
    requirements ruff+black+isort).
"""

import configparser
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _read(name: str) -> str:
    path = ROOT / name
    assert path.is_file(), f"{name} does not exist"
    return path.read_text(encoding="utf-8")


def _load_yaml(name: str):
    return yaml.safe_load(_read(name))


def _load_toml(name: str) -> dict:
    tomllib = pytest.importorskip("tomllib")
    return tomllib.loads(_read(name))


def _workflow_files():
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert files, "no workflow files found"
    return files


def _action_pins(text: str):
    """Yield the ref part of every ``uses: owner/repo@ref`` entry."""
    return re.findall(r"uses:\s*[\w.-]+/[\w.-]+@([^\s#]+)", text)


def _requirement_lines(name: str) -> dict:
    declared = {}
    for raw in _read(name).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        dist = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip()
        if dist:
            declared[dist] = line
    return declared


def _code(text: str) -> str:
    """Drop whole-line and trailing comments.

    These files explain in comments WHY a defect was removed ("no twine upload",
    "no gcc in the runtime stage"). A plain substring search would then match
    the explanation instead of the code, so the assertions below run against
    the executable content only.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(re.split(r"\s+#", line, maxsplit=1)[0])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. deploy configs must not blank out the database URL
# ---------------------------------------------------------------------------
class TestDatabaseUrlIsNotOverriddenWithEmpty:
    def test_fly_toml_database_url_is_set_and_not_empty(self):
        env = _load_toml("fly.toml")["env"]
        url = env.get("RETRO_RADIO_DATABASE_URL")
        assert url, "fly.toml must declare RETRO_RADIO_DATABASE_URL (or none at all)"
        assert url.strip(), (
            'fly.toml sets RETRO_RADIO_DATABASE_URL="" which overrides the app '
            "default and makes db/session.py call create_engine(\"\")"
        )
        assert "://" in url, f"not a SQLAlchemy URL: {url!r}"

    def test_fly_toml_has_no_empty_valued_settings(self):
        env = _load_toml("fly.toml")["env"]
        empty = sorted(k for k, v in env.items() if isinstance(v, str) and not v.strip())
        assert not empty, (
            "these fly.toml env values are empty strings. An empty string is an "
            f"OVERRIDE, not an unset: {empty}"
        )

    def test_render_database_url_is_not_empty(self):
        service = _load_yaml("render.yaml")["services"][0]
        for entry in service["envVars"]:
            value = entry.get("value")
            if value is None:
                continue
            if entry["key"] in ("RETRO_RADIO_DATABASE_URL", "RETRO_RADIO_SONG_STORE_PATH"):
                assert value.strip(), f"render.yaml {entry['key']} is an empty override"


# ---------------------------------------------------------------------------
# 2. auth mode is explicit on every deploy target
# ---------------------------------------------------------------------------
class TestRequireAuthIsExplicit:
    def test_fly_toml_declares_require_auth(self):
        env = _load_toml("fly.toml")["env"]
        assert "RETRO_RADIO_REQUIRE_AUTH" in env, (
            "fly.toml relies on the code default for the auth mode"
        )
        assert env["RETRO_RADIO_REQUIRE_AUTH"].lower() in ("1", "0", "true", "false")

    def test_render_yaml_declares_require_auth(self):
        service = _load_yaml("render.yaml")["services"][0]
        keys = {e["key"]: e for e in service["envVars"]}
        assert "RETRO_RADIO_REQUIRE_AUTH" in keys, (
            "render.yaml must declare the auth mode explicitly; without it the app "
            "falls back to require_auth=true and answers 401 forever"
        )
        assert "sync" not in keys["RETRO_RADIO_REQUIRE_AUTH"]

    def test_railway_documents_require_auth(self):
        # railway.json's schema has no env section (see the JSON schema linked
        # in the file), so the variables live in the documented companion file.
        railway = json.loads(_read("railway.json"))
        assert railway["deploy"]["startCommand"]
        companion = _read("railway.env.example")
        assert re.search(r"^RETRO_RADIO_REQUIRE_AUTH=(1|0|true|false)$", companion, re.M), (
            "railway.env.example must pin RETRO_RADIO_REQUIRE_AUTH"
        )

    def test_every_deploy_target_has_a_secret_key_source(self):
        fly_env = _load_toml("fly.toml")["env"]
        assert fly_env.get("RETRO_RADIO_SECRET_KEY") != "", (
            'fly.toml pins RETRO_RADIO_SECRET_KEY="" and thereby overrides the '
            "value stored with `fly secrets set`"
        )
        render = _load_yaml("render.yaml")["services"][0]
        secret = {e["key"]: e for e in render["envVars"]}["RETRO_RADIO_SECRET_KEY"]
        assert secret.get("generateValue") is True or "sync" in secret


# ---------------------------------------------------------------------------
# 3. the documented PostgreSQL path is actually installable
# ---------------------------------------------------------------------------
class TestPostgresDriverIsDeclared:
    DRIVERS = ("psycopg", "psycopg2", "asyncpg", "pg8000")

    def test_requirements_declares_a_postgres_driver(self):
        declared = {d.lower() for d in _requirement_lines("requirements.txt")}
        assert declared & set(self.DRIVERS), (
            "docs recommend PostgreSQL for production and db/session.py has a "
            f"postgresql branch, but no driver is in requirements.txt: {sorted(declared)}"
        )

    def test_postgres_driver_is_pinned(self):
        for dist, spec in _requirement_lines("requirements.txt").items():
            if dist.lower() in self.DRIVERS:
                assert re.search(r"[<>]=?\s*\d", spec), f"{dist} is not version-bounded: {spec}"
                assert "==" not in spec, f"{dist} is exactly pinned: {spec}"

    def test_postgres_driver_is_installed_in_the_image(self):
        dockerfile = _read("Dockerfile")
        assert "requirements.txt" in dockerfile
        assert not re.search(r'pip install[^\n]*"?alembic', dockerfile), (
            "the Dockerfile still installs alembic outside requirements.txt"
        )


# ---------------------------------------------------------------------------
# 4. persistent storage is declared
# ---------------------------------------------------------------------------
class TestPersistentStorage:
    def test_fly_declares_a_volume_mount(self):
        config = _load_toml("fly.toml")
        mounts = config.get("mounts") or []
        assert mounts, "fly.toml has no [[mounts]]; SQLite lives on an ephemeral FS"
        destinations = {m["destination"] for m in mounts}
        assert any(d.startswith("/data") for d in destinations), destinations

    def test_fly_database_lives_on_the_mounted_volume(self):
        config = _load_toml("fly.toml")
        destinations = [m["destination"] for m in config.get("mounts") or []]
        url = config["env"]["RETRO_RADIO_DATABASE_URL"]
        assert any(path in url for path in destinations), (
            f"{url} is not on any mounted volume {destinations}"
        )

    def test_render_declares_a_disk(self):
        service = _load_yaml("render.yaml")["services"][0]
        disk = service.get("disk")
        assert disk, "render.yaml has no disk; the database is lost on every deploy"
        assert disk["mountPath"].startswith("/data")
        assert disk.get("sizeGB", 0) > 0

    def test_render_database_lives_on_the_disk(self):
        service = _load_yaml("render.yaml")["services"][0]
        mount = service["disk"]["mountPath"]
        env = {e["key"]: e.get("value") for e in service["envVars"]}
        assert mount in env["RETRO_RADIO_DATABASE_URL"]

    def test_render_disk_requires_a_paid_plan(self):
        # Render refuses to attach a disk to the free plan, which would turn the
        # persistence fix into a deploy-time error.
        service = _load_yaml("render.yaml")["services"][0]
        assert service.get("disk"), "no disk to validate against"
        assert service.get("plan") != "free", (
            "render.yaml declares a disk but still uses the free plan, which does "
            "not support persistent disks"
        )

    def test_railway_requires_a_mounted_volume_path(self):
        railway = json.loads(_read("railway.json"))
        assert railway["deploy"].get("requiredMountPath", "").startswith("/data"), (
            "railway.json cannot declare a volume; requiredMountPath at least "
            "makes a missing volume a loud failure"
        )
        assert "volumes" not in json.dumps(railway), (
            "railway.json has no volumes section in its schema; do not invent one"
        )


# ---------------------------------------------------------------------------
# 5. release.yml matches what this project actually is
# ---------------------------------------------------------------------------
class TestReleaseWorkflowIsReal:
    def test_no_packaging_of_a_non_package(self):
        text = _code(_read(".github/workflows/release.yml"))
        for forbidden in ("python -m build", "twine upload", "PYPI_TOKEN", "git cliff"):
            assert forbidden not in text, (
                f"release.yml still tries to {forbidden!r}; this repository has no "
                "pyproject.toml, no setup.py, no license and no git-cliff"
            )

    def test_no_placeholder_repository_owner(self):
        text = _code(_read(".github/workflows/release.yml"))
        assert "your-org" not in text, "publish step is still gated on a placeholder owner"

    def test_release_builds_the_container_image(self):
        text = _read(".github/workflows/release.yml")
        assert "docker/build-push-action" in text
        assert "softprops/action-gh-release" in text


# ---------------------------------------------------------------------------
# 6. security workflow defects
# ---------------------------------------------------------------------------
class TestSecurityWorkflow:
    def test_pip_audit_json_is_not_uploaded_as_sarif(self):
        text = _code(_read(".github/workflows/security.yml"))
        assert "upload-sarif" not in text, (
            "pip-audit --format=json is not SARIF; uploading it as SARIF makes the "
            "upload fail and hides the findings"
        )
        assert "--format=json" in text, "pip-audit should still emit machine-readable JSON"

    def test_codeql_is_not_v2(self):
        text = _code(_read(".github/workflows/security.yml"))
        assert not re.search(r"codeql-action/[\w-]+@v2\b", text), "codeql-action@v2 is retired"
        assert "codeql-action/init@" in text, "CodeQL needs an init step before analyze"

    def test_workflows_declare_permissions(self):
        for path in _workflow_files():
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert "permissions" in data, f"{path.name} has no top-level permissions block"
            for job_name, job in (data.get("jobs") or {}).items():
                assert isinstance(job, dict)
                if "permissions" not in job and "uses" not in job:
                    assert data.get("permissions") != "write-all", (
                        f"{path.name}:{job_name} inherits write-all permissions"
                    )

    def test_dependabot_config_exists_and_is_referenced(self):
        config = ROOT / ".github" / "dependabot.yml"
        assert config.is_file(), ".github/dependabot.yml is missing"
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data.get("version") == 2
        assert data.get("updates"), "no ecosystems configured"
        referenced = any(
            ".github/dependabot.yml" in path.read_text(encoding="utf-8")
            for path in _workflow_files()
        )
        assert referenced, "no workflow references .github/dependabot.yml"


# ---------------------------------------------------------------------------
# 7. CI workflow defects
# ---------------------------------------------------------------------------
class TestCiWorkflow:
    def test_pip_cache_is_enabled(self):
        text = _read(".github/workflows/ci.yml")
        assert "cache: pip" in text, "pip caching is not configured"
        assert "cache-dependency-path" in text

    def test_codecov_action_is_not_v3(self):
        text = _read(".github/workflows/ci.yml")
        assert not re.search(r"codecov/codecov-action@v3\b", text), "codecov-action@v3 is deprecated"

    def test_all_actions_are_pinned_to_a_commit_sha(self):
        for path in _workflow_files():
            for ref in _action_pins(path.read_text(encoding="utf-8")):
                assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                    f"{path.name} pins an action to {ref!r} instead of a commit SHA"
                )

    def test_eval_gate_step_is_wired_with_the_expected_shape(self):
        """eval ゲートの**構造**を検証する（YAML を parse する）。

        かつては `ci.yml` を文字列 grep するだけで「配線済み」だったため、
        **ゲートが赤でもテストは緑**になっていた（実測: 3 ケース不合格の
        まま `python -m eval --offline --threshold 80` が EXIT 1 で、
        このテストは通過していた）。コメントが変わってもステップが
        `test` ジョブから外れても検出できるよう、YAML を parse して
        形を固定する。**gate が実際に通ること**は
        :meth:`test_eval_gate_command_exits_zero` が担保する。
        """
        data = yaml.safe_load(_read(".github/workflows/ci.yml"))
        steps = data["jobs"]["test"]["steps"]
        matches = [
            s
            for s in steps
            if isinstance(s.get("run"), str) and "python -m eval --offline" in s["run"]
        ]
        assert matches, "the eval gate step is missing from jobs.test.steps"
        step = matches[0]
        assert "--threshold 80" in step["run"], step["run"]
        assert "continue-on-error" not in step, (
            "the eval gate must fail the build on regression "
            "(continue-on-error would make it advisory)"
        )
        assert step.get("if"), "the eval gate must be scoped to one python version"

    def test_eval_gate_command_exits_zero(self):
        """**実際に eval ゲートを実行して** EXIT 0 であることを検証する。

        これが唯一の「ゲートは緑」という主張を観測できる場所。
        文字列 grep では、コマンドが赤的事实を検出できない。
        """
        import subprocess
        import sys as _sys

        proc = subprocess.run(
            [_sys.executable, "-m", "eval", "--offline", "--threshold", "80"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        assert proc.returncode == 0, (
            "the eval gate does not pass at threshold 80; CI would be red:\n"
            + "\n".join(
                line for line in proc.stdout.splitlines() if line.startswith(("[NG", "合計"))
            )
        )


# ---------------------------------------------------------------------------
# 8. Dockerfile
# ---------------------------------------------------------------------------
class TestDockerfile:
    def test_volume_is_declared(self):
        dockerfile = _read("Dockerfile")
        assert re.search(r"^VOLUME\s+\[", dockerfile, re.M), "no VOLUME declaration"
        volumes = re.search(r"^VOLUME\s+\[([^\]]*)\]", dockerfile, re.M).group(1)
        assert "/data" in volumes, "the SQLite volume is not declared"
        assert "retro_radio_audio_cache" in volumes, "the TTS cache volume is not declared"

    def test_no_compiler_in_the_runtime_stage(self):
        dockerfile = _read("Dockerfile")
        stages = re.split(r"^FROM ", dockerfile, flags=re.M)[1:]
        assert len(stages) >= 2, "expected a multi-stage build"
        final = _code(stages[-1])
        for tool in ("gcc", "libpq-dev", "build-essential", "g++"):
            assert tool not in final, f"{tool} is present in the runtime stage"

    def test_alembic_is_not_installed_outside_requirements(self):
        dockerfile = _code(_read("Dockerfile"))
        assert "alembic>=1.13.0" not in dockerfile
        assert re.search(r"pip install[^\n]*alembic", dockerfile) is None, (
            "alembic is still installed by a separate pip command"
        )
        assert "alembic" in _requirement_lines("requirements.txt"), (
            "alembic must come from requirements.txt, and the deploy targets run it"
        )

    def test_start_command_still_serves_the_app(self):
        dockerfile = _read("Dockerfile")
        assert "uvicorn" in dockerfile
        assert "retro_radio.server:app" in dockerfile


# ---------------------------------------------------------------------------
# 9. alembic.ini
# ---------------------------------------------------------------------------
class TestAlembicIni:
    def test_no_dead_sqlalchemy_url(self):
        parser = configparser.ConfigParser()
        parser.read(ROOT / "alembic.ini", encoding="utf-8")
        assert not parser.has_option("alembic", "sqlalchemy.url"), (
            "db/migrations/env.py always overrides sqlalchemy.url from "
            "RETRO_RADIO_DATABASE_URL, so the ini value is dead config"
        )

    def test_the_dead_value_is_documented_where_it_used_to_be(self):
        text = _read("alembic.ini")
        assert "sqlalchemy.url" in text, (
            "the commented-out line should stay visible with an explanation"
        )

    def test_env_py_still_takes_the_url_from_settings(self):
        env_py = (ROOT / "db" / "migrations" / "env.py").read_text(encoding="utf-8")
        assert "get_settings" in env_py, "env.py no longer reads the app settings"


# ---------------------------------------------------------------------------
# 10. one lint tool only
# ---------------------------------------------------------------------------
class TestSingleLintTool:
    REDUNDANT = ("ruff", "black", "isort")

    def test_requirements_dev_installs_flake8_only(self):
        declared = {d.lower() for d in _requirement_lines("requirements-dev.txt")}
        assert "flake8" in declared, "CI runs flake8; it must be a declared dev dependency"
        for tool in self.REDUNDANT:
            assert tool not in declared, f"{tool} is still installed in requirements-dev.txt"

    def test_pre_commit_does_not_use_ruff(self):
        text = _code(_read(".pre-commit-config.yaml"))
        for tool in self.REDUNDANT:
            assert tool not in text, f"pre-commit still uses {tool}"

    def test_pre_commit_flake8_hook_uses_the_project_config(self):
        data = _load_yaml(".pre-commit-config.yaml")
        hooks = [h for repo in data["repos"] for h in repo.get("hooks", [])]
        flake8 = [h for h in hooks if h["id"] == "flake8"]
        assert flake8, "pre-commit has no flake8 hook"
        args = " ".join(flake8[0].get("args", []))
        assert "--max-line-length" not in args, "line length must come from .flake8 only"
        assert "--extend-ignore" not in args, "ignore list must come from .flake8 only"

    def test_ci_runs_the_same_flake8_commands(self):
        text = _read(".github/workflows/ci.yml")
        assert "flake8 tests" in text
        assert ".github/flake8-app-baseline.ini" in text
        for tool in self.REDUNDANT:
            assert tool not in text, f"CI still runs {tool}"


class TestFlake8BaselineCannotGrowSilently:
    """`flake8-app-baseline.ini` の拡大を機械的に防ぐ。

    `extend-ignore` は**個数ではなくコードベース**で除外する。
    したがってここに載っているコードは「新たに発生しても CI が緑のまま」になる。
    Round 1 でベースラインのコメントが
    「新たに増える違反は必ず CI で落ちる」と**事実と反転**して書いていたため、
    違反総数の**上限**と**機械的違反の禁止**の両方を固定する。
    """

    BASELINE = ".github/flake8-app-baseline.ini"

    def _ignored_codes(self):
        import configparser

        parser = configparser.ConfigParser()
        parser.read_string(_read(self.BASELINE))
        raw = parser["flake8"]["extend-ignore"]
        return {c.strip().upper() for c in raw.replace("\n", ",").split(",") if c.strip()}

    @pytest.mark.parametrize("code", ["F401", "W292", "W391"])
    def test_machine_fixable_codes_are_not_wholesale_ignored(self, code):
        """機械的に 100% 直せるコードはベースラインに**載せない**こと。

        Round 1 で F401 / W292 / W391 の実 10 件を全部直し、ベースラインから外した。
        再度載せると、`end-of-file-fixer` 等の formatter が
        pre-commit で直せるはずのものを黙って隠す。
        """
        assert code not in self._ignored_codes(), (
            f"{code} は機械的に修正可能なのでベースラインに載せてはいけません"
            "（外的情况: pre-commit の end-of-file-fixer / 自動 import 削除で直せる）"
        )

    def test_baseline_comment_does_not_claim_false_guarantees(self):
        """ベースラインのコメントが**事実と反転した保証**を述べていないこと。

        Round 1 で「新たに増える違反は必ず CI で落ちる」と書かれていたが、
        `extend-ignore` はコードベースで除外するため**真ではなかった**
        （`retro_radio/core/fallback.py` に `def foo():` を 1 個足しても CI は exit 0。実測）。
        """
        text = _read(self.BASELINE)
        forbidden = [
            "新規に増えた違反",
            "new violations will always fail",
            "新たに増える違反",
        ]
        for phrase in forbidden:
            assert phrase not in text, (
                f"ベースラインのコメントが事実と反転した保証を述べています: {phrase!r}"
            )

    def test_precommit_lints_the_app_directory(self):
        """pre-commit が `retro_radio/` を lint すること。

        従来は `exclude: ^(retro_radio/|...)` により**完全にスキップ**され、
        `end-of-file-fixer` / `trailing-whitespace` も効かなかった
        （結果として W292 7 件・W293 59 件が永久に消えなかった。実測）。
        """
        data = _load_yaml(".pre-commit-config.yaml")
        hooks = [h for repo in data["repos"] for h in repo.get("hooks", [])]

        app_hook = [h for h in hooks if h["id"] == "flake8-app-baseline"]
        assert app_hook, (
            "pre-commit に retro_radio/ を lint する local hook がありません"
            "（`exclude` で除外_pipe ，不代表 CI もチェックしている）"
        )
        assert "retro_radio" in app_hook[0]["entry"]

        main_hook = [h for h in hooks if h["id"] == "flake8"][0]
        exclude = main_hook.get("exclude", "")
        files = main_hook.get("files", "")
        assert "retro_radio" not in exclude, (
            "pre-commit の flake8 が retro_radio/ を exclude しています"
        )
        assert "retro_radio" not in files, (
            "pre-commit の flake8 が retro_radio/ を files で除外しています"
        )

    def test_app_baseline_violation_count_never_grows(self):
        """ベースラインが隠している違反の**総数が増えていない**こと。

        formatter（ruff-format）導入で 1 度に全部直すのは大きいため、
        少なくとも「増えていない」ことを固定して silent な拡大を防ぐ。
        """
        baseline_codes = self._ignored_codes()
        select = ",".join(sorted(baseline_codes))
        result = subprocess.run(
            [
                sys.executable, "-m", "flake8", "--isolated",
                "--max-line-length=100", f"--select={select}", "retro_radio",
            ],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        offenders = [line for line in result.stdout.splitlines() if line.strip()]
        # Round 1 完了時点の想定値（E302 94 + W293 59 + E501 23 + E305 8 +
        # W503 6 + E402 6 + E502 3 + E131 1 + W291 1 = 201）。
        # 1 件でも増えたら赤になる。
        assert len(offenders) <= 201, (
            f"ベースラインが隠す違反が {len(offenders)} 件（Round 1 完了時点で 201 件）に増えました。"
            f"新しいコードを追加してください:\n" + "\n".join(offenders[:20])
        )
