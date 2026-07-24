from __future__ import annotations

# Real SCRUM-3 run: the failing import is in src/, not in the test module, so the
# Tester cannot fix it — drives both the acceptance-failure analysis and the graph node.
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
