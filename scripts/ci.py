"""Fail-closed test runner: real SDK, nonzero discovery and explicit skip policy."""

import os
from pathlib import Path
import sys
import unittest

# Absence must fail, not turn integration tests into skips.
import maibot_sdk  # noqa: F401

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover(
    str(root), pattern="test_*.py", top_level_dir=str(root)
)


def collected(group):
    for test in group:
        if isinstance(test, unittest.TestSuite):
            yield from collected(test)
        else:
            yield test.id().rsplit(".", 2)[0]


groups = set(collected(suite))
required = {
    "test_public_http",
    "test_automation",
    "test_automation_api",
    "test_audit_fixes",
    "test_search_routing",
    "test_release",
    "test_images",
    "test_weather",
}
if not required <= groups:
    raise SystemExit(
        "Required HTTP/bridge/package test groups missing: "
        + ", ".join(sorted(required - groups))
    )
result = unittest.TextTestRunner(verbosity=1).run(suite)
allowed = {"test_world_readable_keys_rejected"} if os.name == "nt" else set()
unexpected = [
    (str(test), reason)
    for test, reason in result.skipped
    if test._testMethodName not in allowed or reason != "POSIX permissions"
]
if unexpected:
    print("Unexpected skipped tests:", unexpected)
raise SystemExit(0 if result.wasSuccessful() and not unexpected else 1)
