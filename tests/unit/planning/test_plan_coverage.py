from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from pydantic_ai import RunContext

from sprint_crew.orchestrator.plan_coverage import (
    check_mutation_allowed,
    check_patch_mutations_allowed,
    collect_changed_paths,
    continuation_makes_sense,
    coverage_improved,
    expected_paths,
    is_noise_path,
    normalize_path,
    should_invoke_tester,
    validate_plan_coverage,
)
from sprint_crew.schemas.ticket import PlanStep, TaskPlan
from sprint_crew.tools.pydantic_ai import WorkspaceDeps, _maybe_trigger_early_exit


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    readme = tmp_path / "README.md"
    readme.write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def test_normalize_path() -> None:
    assert normalize_path("./src/foo.py") == "src/foo.py"
    assert normalize_path("src\\bar.py") == "src/bar.py"


def test_expected_paths_unions_files_to_touch_and_steps() -> None:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="multi",
        steps=[
            PlanStep(description="a", files=["a.py"]),
            PlanStep(description="b", files=["b.py"]),
        ],
        files_to_touch=["c.py"],
        acceptance_tests=["pytest -q"],
    )
    assert expected_paths(plan) == {"a.py", "b.py", "c.py"}


def test_collect_changed_paths_includes_untracked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    new_file = repo / "greeter.py"
    new_file.write_text("def hello():\n    return 'hello'\n", encoding="utf-8")

    paths = collect_changed_paths(repo)
    assert "greeter.py" in paths


def test_is_noise_path() -> None:
    assert is_noise_path("__pycache__/greeter.cpython-312.pyc")
    assert is_noise_path(".pytest_cache/v/cache/nodeids")
    assert not is_noise_path("greeter.py")


def test_collect_changed_paths_excludes_noise(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    cache_dir = repo / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "greeter.cpython-312.pyc").write_bytes(b"fake")

    paths = collect_changed_paths(repo)
    assert not any(is_noise_path(path) for path in paths)


def test_validate_plan_coverage_ignores_existing_test_not_in_steps(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_greeter.py"
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tests/test_greeter.py"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "commit", "-m", "add test"], cwd=repo, check=True, capture_output=True)

    (repo / "greeter.py").write_text("def hello(): return 'hello'\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="add hello",
        steps=[PlanStep(description="edit greeter", files=["greeter.py"])],
        files_to_touch=["greeter.py", "tests/test_greeter.py"],
        acceptance_tests=["pytest tests/test_greeter.py -q"],
    )
    result = validate_plan_coverage(plan, repo)
    assert result.satisfied is True
    assert "tests/test_greeter.py" not in result.missing


def test_continuation_makes_sense_false_when_missing_exists_on_disk(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_greeter.py").write_text("pass\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="add hello",
        steps=[PlanStep(description="edit greeter", files=["greeter.py"])],
        files_to_touch=["greeter.py", "tests/test_greeter.py"],
        acceptance_tests=["pytest -q"],
    )
    coverage = validate_plan_coverage(plan, repo)
    assert coverage.missing == ["greeter.py"]
    assert continuation_makes_sense(coverage, repo, plan) is True

    coverage_only_test = validate_plan_coverage(
        TaskPlan(
            ticket_key="DEMO-1",
            summary="x",
            steps=[PlanStep(description="noop", files=[])],
            files_to_touch=["tests/test_greeter.py"],
            acceptance_tests=["pytest -q"],
        ),
        repo,
    )
    assert continuation_makes_sense(coverage_only_test, repo, plan) is False


def test_validate_plan_coverage_phantom_paths_excluded_from_missing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "real.py").write_text("ok\n", encoding="utf-8")
    baseline = frozenset({"src/real.py", "README.md"})

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="bad plan",
        steps=[PlanStep(description="edit", files=["src/phantom.py"])],
        files_to_touch=["src/phantom.py"],
        acceptance_tests=["pytest -q"],
    )
    result = validate_plan_coverage(plan, repo, baseline_paths=baseline)
    assert result.phantom_paths == ["src/phantom.py"]
    assert "src/phantom.py" not in result.missing
    assert continuation_makes_sense(result, repo, plan) is False


def test_coverage_improved() -> None:
    from sprint_crew.orchestrator.plan_coverage import PlanCoverageResult

    prev = PlanCoverageResult(
        missing=["a.py", "b.py"], unexpected=[], out_of_scope_hits=[], satisfied=False
    )
    better = PlanCoverageResult(
        missing=["b.py"], unexpected=[], out_of_scope_hits=[], satisfied=False
    )
    same = PlanCoverageResult(
        missing=["a.py", "b.py"], unexpected=[], out_of_scope_hits=[], satisfied=False
    )
    assert coverage_improved(prev, better) is True
    assert coverage_improved(prev, same) is False


def test_validate_plan_coverage_missing_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True, capture_output=True)

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="multi",
        steps=[PlanStep(description="both", files=["a.py", "b.py"])],
        files_to_touch=["a.py", "b.py"],
        acceptance_tests=["pytest -q"],
    )
    result = validate_plan_coverage(plan, repo)
    assert result.satisfied is False
    assert result.missing == ["b.py"]


def test_validate_plan_coverage_out_of_scope_hit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "secret.py").write_text("x\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="scope",
        steps=[PlanStep(description="edit readme", files=["README.md"])],
        files_to_touch=["README.md"],
        acceptance_tests=["pytest -q"],
        out_of_scope=["secret.py"],
    )
    result = validate_plan_coverage(plan, repo)
    assert "secret.py" in result.out_of_scope_hits
    assert result.satisfied is False


def test_check_mutation_allowed_queue_worker_ok() -> None:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[PlanStep(description="wire queue", files=["src/messaging/queue_worker.py"])],
        files_to_touch=["src/messaging/queue_worker.py"],
        acceptance_tests=["pytest -q"],
        out_of_scope=["src/messaging/retry_policy.py"],
    )
    assert check_mutation_allowed("src/messaging/queue_worker.py", plan) is None


def test_check_mutation_allowed_rejects_out_of_scope() -> None:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[PlanStep(description="wire queue", files=["src/messaging/queue_worker.py"])],
        files_to_touch=["src/messaging/queue_worker.py"],
        acceptance_tests=["pytest -q"],
        out_of_scope=["src/messaging/retry_policy.py"],
    )
    err = check_mutation_allowed("src/messaging/retry_policy.py", plan)
    assert err is not None
    assert "out_of_scope" in err


def test_check_mutation_allowed_rejects_unplanned_src() -> None:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[PlanStep(description="wire queue", files=["src/messaging/queue_worker.py"])],
        files_to_touch=["src/messaging/queue_worker.py"],
        acceptance_tests=["pytest -q"],
    )
    err = check_mutation_allowed("src/messaging/ferry.py", plan)
    assert err is not None
    assert "files_to_touch" in err


def test_check_patch_mutations_allowed_rejects_unplanned_path() -> None:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[PlanStep(description="wire queue", files=["src/messaging/queue_worker.py"])],
        files_to_touch=["src/messaging/queue_worker.py"],
        acceptance_tests=["pytest -q"],
    )
    patch = """\
--- a/src/messaging/ferry.py
+++ b/src/messaging/ferry.py
@@ -1 +1 @@
-x
+y
"""
    err = check_patch_mutations_allowed(patch, plan)
    assert err is not None


def test_validate_plan_coverage_legacy_empty_expected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "extra.py").write_text("x\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="legacy",
        steps=[PlanStep(description="do work", files=[])],
        acceptance_tests=["pytest -q"],
    )
    result = validate_plan_coverage(plan, repo)
    assert result.satisfied is True


def test_should_invoke_tester_false_when_acceptance_green(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "validators.py").write_text(
        "def validate_email(a): return '@' in a\n", encoding="utf-8"
    )

    plan = TaskPlan(
        ticket_key="DEMO-2",
        summary="add validate_email",
        steps=[PlanStep(description="edit validators", files=["validators.py"])],
        files_to_touch=["validators.py"],
        acceptance_tests=["pytest tests/test_validators.py -q"],
    )
    assert should_invoke_tester(plan, repo, acceptance_green=True) is False


def test_should_invoke_tester_true_when_acceptance_red_and_source_only(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "greeter.py").write_text("def hello(): return 'hello'\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="add hello",
        steps=[PlanStep(description="edit greeter", files=["greeter.py"])],
        files_to_touch=["greeter.py"],
        acceptance_tests=["pytest tests/test_greeter.py -q"],
    )
    assert should_invoke_tester(plan, repo, acceptance_green=False) is True


def test_should_invoke_tester_false_when_plan_requires_tests(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "greeter.py").write_text("def hello(): return 'hello'\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="add hello with tests",
        steps=[PlanStep(description="impl", files=["greeter.py", "tests/test_greeter.py"])],
        files_to_touch=["greeter.py", "tests/test_greeter.py"],
        acceptance_tests=["pytest tests/test_greeter.py -q"],
    )
    assert should_invoke_tester(plan, repo, acceptance_green=False) is False


def test_should_invoke_tester_false_when_source_build_failure(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "greeter.py").write_text("def hello(): return 'hello'\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="add hello",
        steps=[PlanStep(description="edit greeter", files=["greeter.py"])],
        files_to_touch=["greeter.py"],
        acceptance_tests=["pytest tests/test_greeter.py -q"],
    )
    output = """
$ pytest tests/test_greeter.py -q
exit_code=2
src/greeter.py:1: in <module>
    from missing import x
E   ModuleNotFoundError: No module named 'missing'
"""
    assert (
        should_invoke_tester(
            plan,
            repo,
            acceptance_green=False,
            acceptance_output=output,
        )
        is False
    )


def test_should_invoke_tester_true_when_assertion_failure(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "greeter.py").write_text("def hello(): return 'hi'\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="add hello",
        steps=[PlanStep(description="edit greeter", files=["greeter.py"])],
        files_to_touch=["greeter.py"],
        acceptance_tests=["pytest tests/test_greeter.py -q"],
    )
    output = """
$ pytest tests/test_greeter.py -q
exit_code=1
FAILED tests/test_greeter.py::test_hello - AssertionError: assert 'hi' == 'hello'
"""
    assert (
        should_invoke_tester(
            plan,
            repo,
            acceptance_green=False,
            acceptance_output=output,
        )
        is True
    )


def test_is_noise_path_rejects_patch_artifacts() -> None:
    assert is_noise_path("src/storage/sqlite_repo.py.rej")
    assert is_noise_path("src/storage/sqlite_repo.py.orig")
    assert not is_noise_path("src/storage/sqlite_repo.py")


def test_validate_plan_coverage_ignores_baseline_umbrella_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    src = repo / "src"
    src.mkdir()
    helper = src / "helper.py"
    helper.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/helper.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add helper"], cwd=repo, check=True, capture_output=True)

    (repo / "main.py").write_text("import helper\n", encoding="utf-8")
    baseline = frozenset({"README.md", "src/helper.py"})

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="edit main",
        steps=[PlanStep(description="edit main", files=["main.py"])],
        files_to_touch=["main.py", "src/helper.py"],
        acceptance_tests=["pytest -q"],
    )
    result = validate_plan_coverage(plan, repo, baseline_paths=baseline)
    assert result.satisfied is True
    assert "src/helper.py" not in result.missing


def test_validate_plan_coverage_step_required_file_still_missing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    src = repo / "src"
    src.mkdir()
    worker = src / "worker.py"
    worker.write_text("pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/worker.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add worker"], cwd=repo, check=True, capture_output=True)

    baseline = frozenset({"README.md", "src/worker.py"})
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="wire worker",
        steps=[PlanStep(description="implement worker", files=["src/worker.py"])],
        files_to_touch=["src/worker.py"],
        acceptance_tests=["pytest -q"],
    )
    result = validate_plan_coverage(plan, repo, baseline_paths=baseline)
    assert result.satisfied is False
    assert result.missing == ["src/worker.py"]


def test_early_exit_blocked_when_coverage_incomplete(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "a.py").write_text("a\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="multi",
        steps=[PlanStep(description="both", files=["a.py", "b.py"])],
        files_to_touch=["a.py", "b.py"],
        acceptance_tests=["pytest -q"],
    )
    deps = WorkspaceDeps(
        root=repo,
        registry=MagicMock(),
        acceptance_tests=("pytest -q",),
        task_plan=plan,
        require_full_coverage=True,
    )
    ctx = MagicMock(spec=RunContext)
    ctx.deps = deps

    _maybe_trigger_early_exit(ctx, "pytest -q", ok=True)
    assert deps.early_exit_handoff is None


def test_validate_plan_coverage_ignores_baseline_test_in_steps(tmp_path: Path) -> None:
    """SCRUM-2 regression: existing test in steps does not require git diff."""
    repo = _init_repo(tmp_path)
    src = repo / "src" / "messaging"
    src.mkdir(parents=True)
    (src / "retry_policy.py").write_text("def with_retry(fn): return fn\n", encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ferry_retry.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True)

    (src / "retry_policy.py").write_text(
        "def with_retry(fn, retries=3):\n    for _ in range(retries):\n        try:\n            return fn()\n        except Exception:\n            pass\n    raise RuntimeError('failed')\n",
        encoding="utf-8",
    )

    plan = TaskPlan(
        ticket_key="SCRUM-2",
        summary="retry policy",
        steps=[
            PlanStep(
                description="implement with_retry",
                files=["src/messaging/retry_policy.py", "tests/test_ferry_retry.py"],
            )
        ],
        files_to_touch=["src/messaging/retry_policy.py", "tests/test_ferry_retry.py"],
        acceptance_tests=["pytest tests/test_ferry_retry.py -q"],
    )
    result = validate_plan_coverage(plan, repo)
    assert result.satisfied is True
    assert "tests/test_ferry_retry.py" not in result.missing


def test_validate_plan_coverage_requires_new_test_file_in_diff(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    src = repo / "src"
    src.mkdir()
    (src / "worker.py").write_text("pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True)

    (src / "worker.py").write_text("def run(): return 1\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="add tests",
        steps=[
            PlanStep(
                description="create new test file",
                files=["src/worker.py", "tests/test_worker.py"],
            )
        ],
        files_to_touch=["src/worker.py", "tests/test_worker.py"],
        acceptance_tests=["pytest tests/test_worker.py -q"],
    )
    result = validate_plan_coverage(plan, repo)
    assert result.satisfied is False
    assert "tests/test_worker.py" in result.missing


def test_validate_plan_coverage_blocking_unexpected_src(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    src = repo / "src"
    src.mkdir()
    (src / "planned.py").write_text("a\n", encoding="utf-8")
    (src / "extra.py").write_text("b\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True)

    (src / "planned.py").write_text("a2\n", encoding="utf-8")
    (src / "extra.py").write_text("b2\n", encoding="utf-8")

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="edit planned",
        steps=[PlanStep(description="edit", files=["src/planned.py"])],
        files_to_touch=["src/planned.py"],
        acceptance_tests=["pytest -q"],
    )
    result = validate_plan_coverage(plan, repo)
    # blocking_unexpected is advisory only: an unplanned src edit does not block
    # merge when tests + review are green (see PR-A). It is still surfaced.
    assert result.satisfied is True
    assert "src/extra.py" in result.unexpected
    assert "src/extra.py" in result.blocking_unexpected


def test_continuation_makes_sense_false_for_ignorable_baseline_test(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_greeter.py").write_text("pass\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tests/test_greeter.py"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "commit", "-m", "add test"], cwd=repo, check=True, capture_output=True)

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="noop",
        steps=[PlanStep(description="verify", files=["tests/test_greeter.py"])],
        files_to_touch=["tests/test_greeter.py"],
        acceptance_tests=["pytest tests/test_greeter.py -q"],
    )
    coverage = validate_plan_coverage(plan, repo)
    assert coverage.satisfied is True
    assert continuation_makes_sense(coverage, repo, plan) is False
