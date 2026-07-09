from __future__ import annotations

from sprint_crew.orchestrator.acceptance_failure import analyze_acceptance_output

SCRUM3_COLLECTION = """
$ pytest tests/test_notify_routes.py -q
exit_code=2
============================= test session starts ==============================
collected 0 items / 1 error

==================================== ERRORS ====================================
_________________ ERROR collecting tests/test_notify_routes.py _________________
ImportError while importing test module '/testbed/tests/test_notify_routes.py'.
tests/test_notify_routes.py:12: in <module>
    from api.routes import TaskHandler, HTTPServer
src/api/routes.py:11: in <module>
    from platform.config import default_config
E   ModuleNotFoundError: No module named 'platform.config'
=========================== short test summary info ============================
ERROR tests/test_notify_routes.py
"""


def test_collection_error_in_source_disables_tester() -> None:
    analysis = analyze_acceptance_output(SCRUM3_COLLECTION)
    assert analysis.kind == "collection_error"
    assert analysis.tester_can_help is False
    assert "src/api/routes.py" in analysis.source_paths


def test_import_error_only_in_tests_allows_tester() -> None:
    output = """
$ pytest tests/test_x.py -q
exit_code=2
tests/test_x.py:3: in <module>
    from missing_dep import helper
E   ModuleNotFoundError: No module named 'missing_dep'
"""
    analysis = analyze_acceptance_output(output)
    assert analysis.kind == "import_error"
    assert analysis.tester_can_help is True
    assert analysis.test_paths == ("tests/test_x.py",)


def test_assertion_failure_allows_tester() -> None:
    output = """
$ pytest tests/test_greeter.py -q
exit_code=1
FAILED tests/test_greeter.py::test_hello - AssertionError: assert 'hi' == 'hello'
"""
    analysis = analyze_acceptance_output(output)
    assert analysis.kind == "assertion_failure"
    assert analysis.tester_can_help is True


def test_green_output_is_none() -> None:
    output = """
$ pytest -q
exit_code=0
3 passed
"""
    analysis = analyze_acceptance_output(output)
    assert analysis.kind == "none"
    assert analysis.tester_can_help is False


def test_detail_excerpt_is_bounded() -> None:
    analysis = analyze_acceptance_output(SCRUM3_COLLECTION)
    assert "ModuleNotFoundError" in analysis.detail_excerpt
    assert analysis.detail_excerpt.count("\n") <= 30
