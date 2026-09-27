"""Tests for ``python -m lib doctor`` and hooks template Tab coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib import doctor, install_hooks

ROOT = Path(__file__).resolve().parent.parent


def test_hooks_template_includes_tab_events():
    data = json.loads((ROOT / "templates" / "hooks.template.json").read_text(encoding="utf-8"))
    hooks = data["hooks"]
    assert "afterTabFileEdit" in hooks
    assert "beforeTabFileRead" in hooks
    assert "afterFileEdit" in hooks


def test_doctor_fails_when_hooks_missing(project_dir: Path):
    checks = doctor.run_checks(project=project_dir)
    by_name = {c.name: c for c in checks}
    assert by_name["hooks"].ok is False
    assert by_name["hooks"].hard is True
    assert doctor.doctor(project=project_dir, json_out=True) == 1


def test_doctor_ok_after_user_install(project_dir: Path):
    rc = install_hooks.install(mode="user", quiet=True)
    assert rc == 0
    hooks = Path.home() / ".cursor" / "hooks.json"
    assert hooks.is_file()
    data = json.loads(hooks.read_text(encoding="utf-8"))
    assert "afterTabFileEdit" in data["hooks"]
    assert "beforeTabFileRead" in data["hooks"]

    checks = doctor.run_checks(project=project_dir)
    by_name = {c.name: c for c in checks}
    assert by_name["hooks"].ok is True
    assert by_name["policy"].ok is True
    assert by_name["hook-script"].ok is True
    hard_fails = [c for c in checks if not c.ok and c.hard]
    if by_name.get("agentrust-trace") and not by_name["agentrust-trace"].ok:
        assert any(c.name == "agentrust-trace" for c in hard_fails)
    else:
        assert doctor.doctor(project=project_dir) == 0


def test_install_quiet_single_line(capsys):
    rc = install_hooks.install(mode="user", quiet=True)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("ok hooks=")
    assert "Install complete" not in out
