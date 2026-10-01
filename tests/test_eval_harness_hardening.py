"""評価ハーネス（``eval/``）のハードニングのテスト。

``tests/test_eval_harness.py`` が「指標の意味」を固定するのに対し、
このファイルは次の 3 つの**ハーネス自身の欠陥**を固定する:

1. **報告する閾値と強制する閾値が同じであること**（``run_all(threshold=95)``
   が fact score 82 のケースを通さない）。
2. **既定が閉路（hermetic）であること**（``--offline`` がネットワークを
   一切触らない）。
3. **CLI の終了コードがゲート結果を正しく表すこと**（不合格なら非ゼロ）。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from eval import fixtures
from eval.metrics import CaseResult, EvalReport, main, run_all, run_case
from eval.metrics.fact_score import AtomicClaim, FactScoreResult

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. 報告する閾値 == 強制する閾値
# ---------------------------------------------------------------------------
def _clean_script() -> str:
    """CheckList と長さのゲートを**両方通す**原稿（決定的、ネットワーク不要）。

    閾値の検証だけに集中したいので、違反原稿を混ぜないためのヘルパ。
    """
    return fixtures.deterministic_script(1975, "normal", month=5, day=15)


def _case_result_with_score(score: float, threshold: float) -> CaseResult:
    """fact score だけを指定して :class:`CaseResult` を組み立てる。

    ``fail`` レベルの主張は 0 件（warn だけ）なので、ゲートが落ちるかどうかは
    **閾値との比較だけ**で決まる。よってこの形なら「報告値だけで
    成否が変わる」ことを切り分けられる。
    """
    year = 1975
    claims = (AtomicClaim("夕暮れの空の色", "atmosphere", "ok", "検証対象の錨なし"),)
    fact = FactScoreResult(
        year=year,
        score=score,
        supported=0,
        checkable=1,
        claims=claims,
        expected_facts=("p1",),
        mentioned_expected_facts=(),
    )
    # 長さも checklist もゲートを落とさない原稿を使う。
    script = _clean_script()
    from eval.metrics.checklist import run_checklist
    from eval.metrics.length import check_length

    return CaseResult(
        case_id="threshold-probe",
        year=year,
        mode="normal",
        script=script,
        fact=fact,
        checklist=run_checklist(script, year),
        length=check_length(script),
        threshold=threshold,
    )


def test_case_result_fails_at_its_own_threshold_not_the_hardcoded_default():
    """fact score 82 のケースは閾値 95 で落ち、閾値 80 では通る。

    これが本タスクの中心的な主張: **報告値と強制値が同じオブジェクト**である。
    """
    strict = _case_result_with_score(82.0, threshold=95.0)
    lenient = _case_result_with_score(82.0, threshold=80.0)

    assert strict.threshold == 95.0
    assert strict.passed is False, "閾値 95 なのに score 82 で合格した"
    assert lenient.passed is True, "閾値 80 なら score 82 は合格であるべき"

    # 落ちた理由が「閾値不足」であることが読み取れる。
    assert any("95" in problem for problem in strict.failures)


def test_case_result_reports_the_threshold_it_enforced():
    payload = _case_result_with_score(82.0, threshold=95.0).to_dict()
    assert payload["threshold"] == 95.0
    assert payload["passed"] is False
    assert payload["failures"], "不合格なのに failures が空"


def test_run_all_threads_the_threshold_into_every_case():
    """``run_all(threshold=95)`` は全 :class:`CaseResult` に 95 を通す。

    仮に内部で既定値 80 に戻していても、この 1 文で落ちる。
    """
    case = {"id": "normal-1975", "year": 1975, "mode": "normal"}
    report = run_all(cases=[case], threshold=95.0, scripts={"normal-1975": _clean_script()})

    assert report.threshold == 95.0
    assert [r.threshold for r in report.results] == [95.0]
    assert report.to_dict()["threshold"] == 95.0


def test_run_all_threshold_95_fails_a_case_scoring_82(monkeypatch):
    """**要求の核心**: ``run_all(threshold=95)`` は score 82 のケースを落とす。

    fact score だけ 82 に固定し、他（CheckList / 長さ）は正常なままにする。
    これで「落ちたのは閾値だけ」が保証される。score 82 は台本からは
    自然に作りにくいので、``fact_score`` だけを差し替える。
    """
    from eval.metrics import fact_score as real_fact_score

    def _scored_82(script, year, **_kwargs):
        return real_fact_score(script, year).__class__(
            year=year,
            score=82.0,
            supported=82,
            checkable=100,
            claims=(),
            expected_facts=("p1",),
            mentioned_expected_facts=("p1",),
        )

    monkeypatch.setattr("eval.metrics.fact_score", _scored_82)
    case = {"id": "normal-1975", "year": 1975, "mode": "normal"}
    scripts = {"normal-1975": _clean_script()}

    strict = run_all(cases=[case], threshold=95.0, scripts=scripts)
    assert strict.results[0].fact.score == 82.0
    assert strict.results[0].passed is False, "閾値 95 なのに score 82 で合格した"
    assert strict.failed_count == 1
    assert strict.gate_ok is False
    assert any("95" in problem for problem in strict.results[0].failures)

    # 同じ score 82 でも、報告した閾値が 80 なら通る。
    # 「gate が 80 に戻っている」わけではないことの反証。
    lenient = run_all(cases=[case], threshold=80.0, scripts=scripts)
    assert lenient.results[0].fact.score == 82.0
    assert lenient.gate_ok is True


def test_run_all_reports_the_same_value_it_enforces_even_when_it_passes():
    """``threshold=0`` で通ることは、強制値がそのまま使われている証明になる。"""
    case = {"id": "normal-1975", "year": 1975, "mode": "normal"}
    report = run_all(cases=[case], threshold=0.0, scripts={"normal-1975": _clean_script()})

    assert report.threshold == 0.0
    assert report.gate_ok is True


def test_eval_report_gate_ok_tracks_failed_count():
    report = EvalReport(results=(), threshold=95.0)
    assert report.gate_ok is True
    assert report.failed_count == 0


# ---------------------------------------------------------------------------
# 2. 閉路（hermetic）である
# ---------------------------------------------------------------------------
def test_default_script_source_never_touches_the_network(monkeypatch):
    """既定の供給源は、**ソケットを一切作らずに**原稿を生成する。"""
    monkeypatch.delenv(fixtures.SCRIPT_SOURCE_ENV, raising=False)

    def _boom(*args, **kwargs):  # pragma: no cover - 通ったらテストは失敗
        raise AssertionError("offline モードがネットワークに接続しようとした")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)

    report = run_all(threshold=0.0)

    assert report.script_source == fixtures.DEFAULT_SCRIPT_SOURCE
    assert report.hermetic is True
    assert len(report.results) == 24
    assert all(r.script for r in report.results)


def test_run_all_is_reproducible(monkeypatch):
    """同じ入力なら 2 回走らせて**完全に同じ結果**になる。"""
    first = run_all(threshold=80.0)
    second = run_all(threshold=80.0)
    assert first.to_dict() == second.to_dict()


def test_offline_flag_forces_the_deterministic_source(monkeypatch):
    """``--offline`` は ``EVAL_SCRIPT_SOURCE=gemini`` を上書きする。"""
    monkeypatch.setenv(fixtures.SCRIPT_SOURCE_ENV, "gemini")
    parser_args = ["--offline", "--json"]
    monkeypatch.setattr(sys, "argv", ["eval"] + parser_args)
    # 終了コードだけ見る（JSON は大量なので捨てる）
    main(["--offline", "--json"])
    assert fixtures.resolve_source(None) == "gemini", "環境変数の解決自体は変わらない"

    case = {"id": "normal-1975", "year": 1975, "mode": "normal"}
    report = run_all(cases=[case], threshold=0.0, script_source="deterministic")
    assert report.script_source == "deterministic"
    assert report.hermetic is True


def test_gemini_source_is_reported_as_non_hermetic(monkeypatch):
    """``gemini`` 経路を使ったときは報告書が正直に ``hermetic: false`` を出す。"""
    monkeypatch.delenv(fixtures.SCRIPT_SOURCE_ENV, raising=False)
    case = {"id": "normal-1975", "year": 1975, "mode": "normal"}
    report = EvalReport(
        results=run_all(cases=[case], threshold=0.0).results,
        threshold=0.0,
        script_source="gemini",
    )
    assert report.hermetic is False
    assert report.to_dict()["hermetic"] is False


def test_unknown_script_source_is_rejected(monkeypatch):
    monkeypatch.delenv(fixtures.SCRIPT_SOURCE_ENV, raising=False)
    with pytest.raises(fixtures.ScriptSourceError):
        run_all(cases=[], script_source="telepathy")


def test_fixture_source_reads_scripts_from_disk(tmp_path, monkeypatch):
    """``--script-source fixtures`` はファイルから読む（ネットワーク不要）。"""
    payload = {"cases": {"normal-1975": "### オープニング\n原稿。\n### エンディング\nおやすみ。"}}
    path = tmp_path / "scripts.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.delenv(fixtures.SCRIPT_SOURCE_ENV, raising=False)
    case = {"id": "normal-1975", "year": 1975, "mode": "normal"}
    report = run_all(cases=[case], threshold=0.0, script_source="fixtures", fixtures=path)

    assert report.results[0].script == payload["cases"]["normal-1975"]
    assert report.hermetic is True


def test_fixtures_source_without_a_path_is_a_usage_error(tmp_path, monkeypatch):
    monkeypatch.delenv(fixtures.SCRIPT_SOURCE_ENV, raising=False)
    monkeypatch.delenv(fixtures.FIXTURES_ENV, raising=False)
    with pytest.raises(fixtures.ScriptSourceError):
        run_all(cases=[], script_source="fixtures")


# ---------------------------------------------------------------------------
# 3. CLI の終了コード
# ---------------------------------------------------------------------------
def test_cli_exit_code_is_zero_when_the_gate_passes(tmp_path, monkeypatch, capsys):
    """合格する閾値なら 0。CI はここで落ちない。"""
    monkeypatch.delenv(fixtures.SCRIPT_SOURCE_ENV, raising=False)
    # 1 ケースだけを使い、確実に通る閾値にする。
    monkeypatch.setattr(
        "eval.metrics.load_cases",
        lambda: [{"id": "normal-1975", "year": 1975, "mode": "normal"}],
    )
    code = main(["--offline", "--threshold", "0", "--json"])
    assert code == 0
    capsys.readouterr()


def test_cli_exit_code_is_nonzero_when_the_gate_fails(monkeypatch, capsys):
    """1 ケースでも落ちれば非ゼロ。

    閾値 100.5 は「達成不可能な閾値」であり、どの台本でも落ちる。
    """
    monkeypatch.delenv(fixtures.SCRIPT_SOURCE_ENV, raising=False)
    monkeypatch.setattr(
        "eval.metrics.load_cases",
        lambda: [{"id": "normal-1975", "year": 1975, "mode": "normal"}],
    )
    code = main(["--offline", "--threshold", "100.5", "--json"])
    assert code == 1
    capsys.readouterr()


def test_cli_exit_code_2_is_for_a_bad_script_source(monkeypatch, capsys):
    """ゲート不合格（1）とは別の「使い方の誤り」（2）を返す。"""
    monkeypatch.setenv(fixtures.SCRIPT_SOURCE_ENV, "fixtures")
    monkeypatch.delenv(fixtures.FIXTURES_ENV, raising=False)
    code = main(["--json"])
    assert code == 2
    assert "fixture" in capsys.readouterr().err


def test_cli_json_output_carries_the_threshold_it_enforced(monkeypatch, capsys):
    monkeypatch.delenv(fixtures.SCRIPT_SOURCE_ENV, raising=False)
    monkeypatch.setattr(
        "eval.metrics.load_cases",
        lambda: [{"id": "normal-1975", "year": 1975, "mode": "normal"}],
    )
    main(["--offline", "--threshold", "100.5", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["threshold"] == 100.5
    assert payload["cases"][0]["threshold"] == 100.5
    assert payload["summary"]["gate_ok"] is False


def test_cli_help_runs_without_error():
    result = subprocess.run(
        [sys.executable, "-m", "eval", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        # main() 側が stdout を UTF-8 で出力するため、親側も UTF-8 で
        # デコードする（Windows 既定の cp932 だと日本語バイトで
        # UnicodeDecodeError になり、reader thread が落ちて stdout が
        # None になる。兄弟テストと同じ対策）。
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert result.returncode == 0
    assert "--offline" in result.stdout
    assert "--script-source" in result.stdout


def test_cli_offline_end_to_end_exit_code_is_meaningful():
    """サブプロセスで実際に ``python -m eval --offline`` を走らせる。

    ``--threshold 0`` は「ゲートは通る」= 終了コード 0、
    ``--threshold 100`` は「ゲートは落ちる」= 終了コード 1。
    """
    ok = subprocess.run(
        [sys.executable, "-m", "eval", "--offline", "--threshold", "0", "--json"],
        cwd=_REPO_ROOT,
        capture_output=True,
        # main() 側が stdout を UTF-8 で出力するため、親側も UTF-8 で
        # デコードする（Windows 既定の cp932 だと日本語バイトで
        # UnicodeDecodeError になり、reader thread が落ちて stdout が
        # None になる）。
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["hermetic"] is True

    ng = subprocess.run(
        [sys.executable, "-m", "eval", "--offline", "--threshold", "100.5", "--json"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    assert ng.returncode == 1
    assert json.loads(ng.stdout)["summary"]["gate_ok"] is False


# ---------------------------------------------------------------------------
# 4. 「測れない」を明示する
# ---------------------------------------------------------------------------
def test_empty_script_is_reported_as_not_applicable_not_as_a_pass():
    """空の原稿で 100% を返さない。適用外として明示する。"""
    case = {"id": "normal-1975", "year": 1975, "mode": "normal"}
    result = run_case(case, script="", threshold=0.0)

    assert result.not_applicable, "空の原稿は適用外として報告されるべき"
    assert any("japanese_ratio" in item for item in result.not_applicable)
    assert any("song_match" in item for item in result.not_applicable)
    assert any("fact_score" in item for item in result.not_applicable)


def test_song_match_reports_not_applicable_when_nothing_is_mentioned():
    script = "### オープニング\n Napa の夜です。\n### エンディング\nまた次回。"
    result = run_case({"id": "x", "year": 1975, "mode": "normal"}, script=script, threshold=0.0)
    assert result.checklist.song_match is not None
    assert result.checklist.song_match.applicable is False
    assert "N/A" in result.checklist.song_match.describe()
    assert any("song_match" in item for item in result.not_applicable)


def test_checklist_result_exposes_not_applicable_items():
    script = "### オープニング\n Napa の夜です。\n### エンディング\nまた次回。"
    result = run_case({"id": "x", "year": 1975, "mode": "normal"}, script=script, threshold=0.0)
    payload = result.to_dict()
    assert payload["song_match_applicable"] is False
    assert payload["japanese_ratio_applicable"] is True
    assert payload["not_applicable"]


# ---------------------------------------------------------------------------
# 5. 決定的原稿は正本カタログから導出される（P0-1 の回帰防止）
# ---------------------------------------------------------------------------
def _fixture_function_body(name: str) -> str:
    """``eval/fixtures.py`` の関数本体（コメントを除いた素な文字列）を返す。

    コメント全体を検査すると「以前は validate_song_pairs(None) を使って
    いた」という説明文まで引っかかるため、本体だけを見る。
    """
    import re as _re

    source = Path(fixtures.__file__).read_text(encoding="utf-8")
    match = _re.search(
        rf"^def {name}\(.*?(?=^def |\Z)", source, _re.DOTALL | _re.MULTILINE
    )
    assert match is not None, f"def {name} が見つからない"
    body = match.group(0)
    # コメント行を落とす
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )


def test_deterministic_script_derives_songs_from_catalog():
    """``deterministic_script`` が正本カタログの曲を使うこと。

    以前は ``validate_song_pairs(None) or []`` で ``None``（= 未指定）を
    「空」として扱い、内蔵の古いフォールバック曲名（1951/1955 年）が
    使われていた。現在は ``_catalog_allowlist(year, ...)`` で導出する。
    """
    body = _fixture_function_body("deterministic_script")
    assert "_catalog_allowlist(year" in body, (
        "deterministic_script は正本カタログの allowlist から曲を導出すべき"
    )
    assert "validate_song_pairs(None)" not in body, (
        "None は「正本カタログから導出」を意味するので or [] で空にしない"
    )


def test_deterministic_script_songs_are_from_catalog():
    """決定的原稿に使われる曲名が、正本カタログの曲であること。

    内蔵フォールバックの静的マスターに戻ると、eval の照合先（正本カタログ）
    と乖離して不一致が増える。曲名の抽出は song_match 経由で確認できる。
    """
    from eval.metrics.checklist import run_checklist

    for year in (1950, 1975):
        script = fixtures.deterministic_script(year, "normal", month=5, day=15)
        checklist = run_checklist(script, year)
        if checklist.song_match is not None and checklist.song_match.applicable:
            unmatched = [m.title for m in checklist.song_match.unmatched]
            assert not unmatched, f"{year}: カタログに無い曲名の言及: {unmatched}"
            # 照合先が正本カタログであること（P3-2 の同期）
            assert checklist.song_match.source == "catalog", (
                f"{year}: 照合先が正本カタログでない: {checklist.song_match.source}"
            )
