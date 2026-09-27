"""verify-trace / trace-tests conformance against a freshly signed record."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib import assemble
from tests.conftest import REAL_HOME

ROOT = Path(__file__).resolve().parent.parent


def _library_available() -> bool:
    try:
        import trace_tests  # noqa: F401
        from trace_tests.runner import run  # noqa: F401

        return True
    except ImportError:
        return False


def _cli_available() -> bool:
    """True when ``trace-tests`` CLI imports cleanly under the real HOME."""
    exe = shutil.which("trace-tests")
    if not exe:
        return False
    env = os.environ.copy()
    env["HOME"] = REAL_HOME
    probe = subprocess.run(
        [exe, "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return probe.returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_trace_tests,
]


@pytest.mark.skipif(not _library_available(), reason="trace_tests library not importable")
def test_signed_record_passes_via_runner(write_trail, enforce_policy: Path):
    pytest.importorskip("agentrust_trace")
    from trace_tests.loader import load_record
    from trace_tests.modules.unverified import finding_counts_as_level_failure
    from trace_tests.runner import run

    trail = write_trail("verify-runner.jsonl")
    dest, _, _ = assemble.assemble_and_sign_from_trail(trail)
    record, fmt = load_record(str(dest))
    # Wide freshness window so CI clocks / slow suites do not flake on iat.
    # Schema conformance uses software-observed TRACE (--level 0).
    results = run(record, fmt, level=0, max_age_seconds=10**9)
    failures = [
        f"{mod}: {f.message}"
        for mod, findings in results.items()
        for f in findings
        if finding_counts_as_level_failure(f, 0)
    ]
    assert not failures, failures


@pytest.mark.skipif(not _cli_available(), reason="trace-tests CLI unavailable or missing deps")
def test_signed_record_passes_via_cli(write_trail, enforce_policy: Path, real_home: str):
    pytest.importorskip("agentrust_trace")
    trail = write_trail("verify-cli.jsonl")
    dest, _, _ = assemble.assemble_and_sign_from_trail(trail)

    verify_py = ROOT / "bin" / "verify_trace.py"
    assert verify_py.is_file()
    # Subprocess needs the real HOME so user-site deps (click) resolve.
    env = os.environ.copy()
    env["HOME"] = real_home
    proc = subprocess.run(
        [sys.executable, str(verify_py), str(dest)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout or "Result: PASS" in proc.stdout
