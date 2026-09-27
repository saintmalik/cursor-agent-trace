"""Optional manual end-to-end walkthrough (prefer pytest for CI)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import install_hooks, verify_trace

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bin" / "cursor-agent-trace-hook.py"
JSONL_TO_TRACE = ROOT / "bin" / "jsonl-to-trace.py"
COLLECTOR_PORT = 18787


def _clear_cursor_env() -> None:
    for key in list(os.environ):
        if key.startswith("CURSOR_"):
            del os.environ[key]
    os.environ.pop("TRACE_PRIVATE_KEY_PEM", None)


def _clean_env(extra_drop: set[str] | None = None) -> dict[str, str]:
    drop = extra_drop or set()
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CURSOR_") and k not in drop
    }


def _run_hook(
    project: Path,
    e2e_home: Path,
    payload: dict,
    *,
    extra_env: dict[str, str] | None = None,
) -> dict:
    env = _clean_env({"PAYLOAD", "HOOK", "PROJECT", "E2E_HOME"})
    env["HOME"] = str(e2e_home)
    env["CURSOR_PROJECT_DIR"] = str(project)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _base_payload(conv: str, name: str, extra: dict) -> dict:
    payload = {
        "conversation_id": conv,
        "generation_id": "gen-1",
        "model": "default",
        "model_id": "composer-2",
        "hook_event_name": name,
        "cursor_version": "e2e",
        "workspace_roots": [str(ROOT)],
        "user_email": "redacted@example.com",
        "transcript_path": None,
    }
    payload.update(extra)
    return payload


def _http_json(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    expect_status: int | None = 200,
) -> tuple[int, dict | str]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        status = exc.code
        if expect_status is not None and status != expect_status:
            raise
        try:
            return status, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return status, raw
    if expect_status is not None and status != expect_status:
        raise RuntimeError(f"unexpected status {status} for {url}: {raw}")
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, raw


def _wait_health(url: str, attempts: int = 50) -> None:
    for _ in range(attempts):
        try:
            status, body = _http_json("GET", url, expect_status=None)
            if status == 200 and isinstance(body, dict) and body.get("ok") is True:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"collector health check failed: {url}")


def _start_collector(data_dir: Path, log_path: Path, token: str) -> subprocess.Popen:
    data_dir.mkdir(parents=True, exist_ok=True)
    log_f = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["COLLECTOR_TOKEN"] = token
    env["LOCAL_DIR"] = str(data_dir)
    env["HOST"] = "127.0.0.1"
    env["PORT"] = str(COLLECTOR_PORT)
    return subprocess.Popen(
        [sys.executable, str(ROOT / "collector" / "server.py")],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
    )


def _stop_collector(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run_manual_e2e() -> int:
    _clear_cursor_env()

    tmp = ROOT / ".tmp-e2e"
    if tmp.exists():
        shutil.rmtree(tmp)
    project = tmp / "project"
    e2e_home = tmp / "home"
    (project / ".cursor").mkdir(parents=True)
    e2e_home.mkdir(parents=True)

    (project / ".cursor" / "agent-trace.env").write_text(
        "# Written by install-hooks — process env overrides; no shell exports needed\n"
        "CURSOR_AGENT_TRACE_REDACT=safe\n"
        "CURSOR_AGENT_TRACE_SIGN=1\n"
        "CURSOR_AGENT_TRACE_S3=0\n",
        encoding="utf-8",
    )
    shutil.copy(
        ROOT / "templates" / "agent-trace.policy.json",
        project / ".cursor" / "agent-trace.policy.json",
    )

    os.environ["CURSOR_PROJECT_DIR"] = str(project)
    trail_dir = project / ".cursor" / "agent-trace"
    conv = f"e2e-conv-{int(time.time())}"

    def run_event(name: str, extra: dict) -> dict:
        return _run_hook(project, e2e_home, _base_payload(conv, name, extra))

    print("== beforeSubmitPrompt (no CURSOR_* feature exports) ==")
    out = run_event(
        "beforeSubmitPrompt",
        {"prompt": "hello secret TOKEN=abc", "attachments": []},
    )
    print(f"stdout: {json.dumps(out)}")
    assert out.get("continue") is True

    print("== preToolUse (allow normal shell) ==")
    out = run_event(
        "preToolUse",
        {
            "tool_name": "Shell",
            "tool_input": {"command": "echo hi"},
            "tool_use_id": "t1",
            "cwd": str(ROOT),
        },
    )
    assert out.get("permission") == "allow"

    print("== postToolUse ==")
    run_event("postToolUse", {"tool_name": "Shell", "tool_use_id": "t1", "tool_output": "hi"})

    print("== afterAgentResponse ==")
    run_event("afterAgentResponse", {"text": "assistant reply"})

    print("== stop (sign TRACE via config defaults) ==")
    run_event("stop", {"status": "completed", "loop_count": 0})

    trail = trail_dir / f"{conv}.jsonl"
    trace = trail_dir / f"{conv}.trace.json"
    honesty_path = trail_dir / f"{conv}.honesty.json"
    assert trail.is_file() and trace.is_file() and honesty_path.is_file()

    honesty = json.loads(honesty_path.read_text(encoding="utf-8"))
    rows = [json.loads(l) for l in trail.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 5, len(rows)
    blob = trail.read_text(encoding="utf-8")
    assert "TOKEN=abc" not in blob
    assert all(r.get("redact") == "safe" for r in rows), rows[0]
    rec = json.loads(trace.read_text(encoding="utf-8"))
    assert rec["eat_profile"] == "tag:agentrust-io.com,2026:trace-v0.2"
    assert rec["runtime"]["platform"] == "software-only"
    assert rec["policy"]["enforcement_mode"] == "enforce", rec["policy"]
    assert rec["appraisal"]["status"] == "none"
    assert rec["origin"]["kind"] == "third-party-control-plane"
    assert rec["model"]["provider"] == "cursor-asserted"
    assert rec["model"]["model_id"] == "composer-2", rec["model"]
    assert "signature" in rec and rec["signature"]
    assert "cnf" in rec
    assert honesty["weights_attested"] is False
    assert honesty["tee"] is False
    assert honesty["attestation"] == "none"
    assert honesty["runtime_platform"] == "software-only"
    print("assemble OK (defaults, no CURSOR_* feature exports)")
    print(f"trail={trail}")
    print(f"trace={trace}")
    print(f"model_id={rec['model']['model_id']}")

    print("== enforce: deny .env read ==")
    out = run_event(
        "beforeReadFile",
        {"file_path": str(project / ".env"), "tool_name": "Read"},
    )
    print(f"stdout: {json.dumps(out)}")
    assert out.get("permission") == "deny", out
    rows = [json.loads(l) for l in trail.read_text(encoding="utf-8").splitlines() if l.strip()]
    dec = rows[-1].get("policy_decision") or {}
    assert dec.get("decision") == "deny", dec
    assert dec.get("rule_kind") == "file_read", dec
    print("deny .env OK", dec.get("matched_rule"))

    print("== enforce: allow normal file read ==")
    out = run_event(
        "beforeReadFile",
        {"file_path": str(project / "README.md"), "tool_name": "Read"},
    )
    assert out.get("permission") == "allow", out
    rows = [json.loads(l) for l in trail.read_text(encoding="utf-8").splitlines() if l.strip()]
    dec = rows[-1].get("policy_decision") or {}
    assert dec.get("decision") == "allow", dec
    print("allow README OK")

    print("== enforce: deny dangerous shell ==")
    out = run_event(
        "beforeShellExecution",
        {"command": "sudo rm -rf /", "cwd": str(project)},
    )
    print(f"stdout: {json.dumps(out)}")
    assert out.get("permission") == "deny", out
    rows = [json.loads(l) for l in trail.read_text(encoding="utf-8").splitlines() if l.strip()]
    dec = rows[-1].get("policy_decision") or {}
    assert dec.get("decision") == "deny", dec
    assert dec.get("rule_kind") == "shell", dec
    print("deny dangerous shell OK", dec.get("matched_rule"))

    print("== observe mode: would_deny but allow ==")
    obs_proj = tmp / "observe-project"
    (obs_proj / ".cursor").mkdir(parents=True)
    shutil.copy(project / ".cursor" / "agent-trace.env", obs_proj / ".cursor" / "agent-trace.env")
    pol = json.loads(
        (ROOT / "templates" / "agent-trace.policy.json").read_text(encoding="utf-8")
    )
    pol["mode"] = "observe"
    (obs_proj / ".cursor" / "agent-trace.policy.json").write_text(
        json.dumps(pol, indent=2) + "\n", encoding="utf-8"
    )
    obs_conv = f"e2e-obs-{int(time.time())}"
    out = _run_hook(
        obs_proj,
        e2e_home,
        {
            "conversation_id": obs_conv,
            "generation_id": "gen-obs",
            "model": "default",
            "hook_event_name": "beforeReadFile",
            "file_path": str(obs_proj / ".env"),
            "tool_name": "Read",
        },
    )
    assert out.get("permission") == "allow", out
    obs_trail = obs_proj / ".cursor" / "agent-trace" / f"{obs_conv}.jsonl"
    dec = json.loads(obs_trail.read_text(encoding="utf-8").splitlines()[0]).get(
        "policy_decision"
    ) or {}
    assert dec.get("decision") == "would_deny", dec
    assert dec.get("mode") == "observe", dec
    print("observe would_deny OK")

    print("== installer writes policy + env ==")
    inst = tmp / "install-target"
    inst.mkdir(parents=True)
    assert install_hooks.install(mode="project", target=inst) == 0
    assert (inst / ".cursor" / "agent-trace.env").is_file()
    assert (inst / ".cursor" / "agent-trace.policy.json").is_file()
    pol = json.loads(
        (inst / ".cursor" / "agent-trace.policy.json").read_text(encoding="utf-8")
    )
    assert pol["mode"] == "enforce"
    assert ".env" in pol["deny_file_read"]
    pol["mode"] = "observe"
    (inst / ".cursor" / "agent-trace.policy.json").write_text(
        json.dumps(pol, indent=2) + "\n", encoding="utf-8"
    )
    assert install_hooks.install(mode="project", target=inst) == 0
    pol = json.loads(
        (inst / ".cursor" / "agent-trace.policy.json").read_text(encoding="utf-8")
    )
    assert pol["mode"] == "observe", "installer must not overwrite existing policy"
    print("installer policy OK")

    print("== default-only model honesty (baked SIGN=1, no model override) ==")
    conv2 = f"e2e-default-{int(time.time())}"
    _run_hook(
        project,
        e2e_home,
        {
            "conversation_id": conv2,
            "generation_id": "g2",
            "model": "default",
            "hook_event_name": "beforeSubmitPrompt",
            "prompt": "x",
        },
    )
    _run_hook(
        project,
        e2e_home,
        {
            "conversation_id": conv2,
            "generation_id": "g2",
            "model": "default",
            "hook_event_name": "stop",
            "status": "completed",
        },
    )
    rec = json.loads((trail_dir / f"{conv2}.trace.json").read_text(encoding="utf-8"))
    assert rec["model"]["model_id"] == "default", rec["model"]
    print("default-honest OK")

    print("== CURSOR_AGENT_TRACE_MODEL_ID lab declared override ==")
    conv3 = f"e2e-override-{int(time.time())}"
    extra = {"CURSOR_AGENT_TRACE_MODEL_ID": "sdk-composer-override"}
    _run_hook(
        project,
        e2e_home,
        {
            "conversation_id": conv3,
            "generation_id": "g3",
            "model": "default",
            "hook_event_name": "beforeSubmitPrompt",
            "prompt": "y",
        },
        extra_env=extra,
    )
    _run_hook(
        project,
        e2e_home,
        {
            "conversation_id": conv3,
            "generation_id": "g3",
            "model": "default",
            "hook_event_name": "stop",
            "status": "completed",
        },
        extra_env=extra,
    )
    rec = json.loads((trail_dir / f"{conv3}.trace.json").read_text(encoding="utf-8"))
    honesty = json.loads(
        (trail_dir / f"{conv3}.honesty.json").read_text(encoding="utf-8")
    )
    assert rec["model"]["model_id"] == "sdk-composer-override", rec["model"]
    assert rec["model"]["provider"] == "operator-declared", rec["model"]
    assert honesty["from_operator_override"] is True
    assert honesty["weights_attested"] is False
    assert "declared" in honesty["model_binding"]
    print("override OK")

    print("== config-file override (installer --redact full) ==")
    cfg_dir = tmp / "cfg-project"
    (cfg_dir / ".cursor").mkdir(parents=True)
    (cfg_dir / ".cursor" / "agent-trace.env").write_text(
        "CURSOR_AGENT_TRACE_REDACT=full\n"
        "CURSOR_AGENT_TRACE_SIGN=0\n"
        "CURSOR_AGENT_TRACE_S3=0\n",
        encoding="utf-8",
    )
    cfg_conv = f"e2e-cfg-{int(time.time())}"
    _run_hook(
        cfg_dir,
        e2e_home,
        {
            "conversation_id": cfg_conv,
            "generation_id": "gen-cfg",
            "model": "default",
            "hook_event_name": "beforeSubmitPrompt",
            "prompt": "config-file FULL body visible",
            "user_email": "cfg@example.com",
        },
    )
    cfg_trail = cfg_dir / ".cursor" / "agent-trace" / f"{cfg_conv}.jsonl"
    row = json.loads(cfg_trail.read_text(encoding="utf-8").splitlines()[0])
    assert row["redact"] == "off", row
    assert row["payload"]["prompt"] == "config-file FULL body visible"
    print("config-file OK")

    print("== baked defaults with no env file at all ==")
    empty_proj = tmp / "empty-project"
    empty_proj.mkdir(parents=True)
    baked_conv = f"e2e-baked-{int(time.time())}"
    for event, extra in [
        ("beforeSubmitPrompt", {"prompt": "baked secret TOKEN=xyz"}),
        ("stop", {"status": "completed"}),
    ]:
        _run_hook(
            empty_proj,
            e2e_home,
            {
                "conversation_id": baked_conv,
                "generation_id": "gen-baked",
                "model": "default",
                "model_id": "composer-2",
                "hook_event_name": event,
                **extra,
            },
        )
    baked_trail = empty_proj / ".cursor" / "agent-trace" / f"{baked_conv}.jsonl"
    baked_trace = baked_trail.with_name(f"{baked_conv}.trace.json")
    assert baked_trail.is_file(), baked_trail
    assert baked_trace.is_file(), "SIGN must default ON without any config"
    blob = baked_trail.read_text(encoding="utf-8")
    assert "TOKEN=xyz" not in blob
    row = json.loads(blob.splitlines()[0])
    assert row["redact"] == "safe", row
    rec = json.loads(baked_trace.read_text(encoding="utf-8"))
    assert rec["model"]["model_id"] == "composer-2"
    assert rec["policy"]["enforcement_mode"] == "enforce", rec["policy"]
    print("baked-defaults OK (no env file)")

    print("== offline jsonl-to-trace ==")
    offline = tmp / "offline.trace.json"
    proc = subprocess.run(
        [sys.executable, str(JSONL_TO_TRACE), str(trail), "--out", str(offline)],
        check=False,
    )
    assert proc.returncode == 0
    assert offline.is_file()

    print("== outbox unit (pending + durable queue, no network) ==")
    sys.path.insert(0, str(ROOT))
    from lib.outbox import Outbox, PostResult  # noqa: E402

    outbox_tmp = tmp / "outbox-unit"
    outbox_tmp.mkdir(parents=True)
    box = Outbox(outbox_tmp)
    calls: list[dict] = []

    def ok_post(payload):
        calls.append(payload)
        return PostResult(ok=True, status=200, response={"ok": True, "kind": payload.get("kind")})

    def fail_post(payload):
        calls.append(payload)
        return PostResult(ok=False, error="simulated down", url="http://test/v1/ingest")

    box.enqueue_event(conversation_id="c1", body={"n": 1}, filename="c1.jsonl")
    box.enqueue_event(conversation_id="c1", body={"n": 2}, filename="c1.jsonl")
    assert box.pending_count() == 2
    info = box.flush(ok_post, force=True)
    assert info["flushed"] is True
    assert info["batch_size"] == 2
    assert info["ok"] is True
    assert box.pending_count() == 0
    assert calls[-1]["kind"] == "batch"
    assert len(calls[-1]["events"]) == 2

    box.enqueue_event(conversation_id="c2", body={"n": 3})
    info = box.flush(fail_post, force=True)
    assert info["ok"] is False
    assert box.outbox_count() == 1
    assert "queued" in info
    entry = json.loads(next(box.outbox_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert entry["payload"]["kind"] == "batch"
    assert entry["attempts"] == 0

    calls.clear()
    for p in box.list_outbox():
        e = json.loads(p.read_text(encoding="utf-8"))
        e["next_attempt_at"] = 0
        p.write_text(json.dumps(e), encoding="utf-8")
    replay = box.replay_outbox(fail_post)
    assert replay["attempted"] == 1
    assert replay["failed"] == 1
    assert box.outbox_count() == 1
    entry = json.loads(next(box.outbox_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert entry["attempts"] == 1
    assert entry["next_attempt_at"] > time.time()

    for p in box.list_outbox():
        e = json.loads(p.read_text(encoding="utf-8"))
        e["next_attempt_at"] = 0
        p.write_text(json.dumps(e), encoding="utf-8")
    replay = box.replay_outbox(ok_post)
    assert replay["succeeded"] == 1
    assert box.outbox_count() == 0
    print("outbox unit OK")

    print("== org collector: local disk ingest + hook POST ==")
    collector_data = tmp / "collector-data"
    collector_log = tmp / "collector.log"
    collector_token = "e2e-collector-token"
    if collector_data.exists():
        shutil.rmtree(collector_data)
    collector_proc = _start_collector(collector_data, collector_log, collector_token)
    try:
        base = f"http://127.0.0.1:{COLLECTOR_PORT}"
        _wait_health(f"{base}/healthz")
        status, body = _http_json("GET", f"{base}/healthz")
        assert isinstance(body, dict) and body.get("ok") is True

        status, body = _http_json(
            "POST",
            f"{base}/v1/ingest",
            token=collector_token,
            body={
                "kind": "event",
                "conversation_id": "direct-demo",
                "body": {"hook_event_name": "stop", "ts": "e2e"},
            },
        )
        assert isinstance(body, dict) and body.get("ok") is True and body.get("backend") == "local"
        direct = collector_data / "cursor-agent-trace" / "direct-demo.jsonl"
        assert direct.is_file()
        assert '"hook_event_name": "stop"' in direct.read_text(
            encoding="utf-8"
        ) or '"hook_event_name":"stop"' in direct.read_text(encoding="utf-8")

        status, body = _http_json(
            "POST",
            f"{base}/v1/ingest",
            token=collector_token,
            body={
                "kind": "batch",
                "events": [
                    {
                        "kind": "event",
                        "conversation_id": "batch-demo",
                        "body": {"hook_event_name": "beforeSubmitPrompt", "n": 1},
                    },
                    {
                        "kind": "event",
                        "conversation_id": "batch-demo",
                        "body": {"hook_event_name": "stop", "n": 2},
                    },
                ],
            },
        )
        assert (
            isinstance(body, dict)
            and body.get("ok") is True
            and body.get("kind") == "batch"
            and body.get("accepted") == 2
        )
        batch_lines = [
            json.loads(l)
            for l in (collector_data / "cursor-agent-trace" / "batch-demo.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if l.strip()
        ]
        assert len(batch_lines) == 2, batch_lines
        print("batch POST OK")

        status, _ = _http_json(
            "POST",
            f"{base}/v1/ingest",
            token="wrong",
            body={"kind": "event", "conversation_id": "x", "body": {}},
            expect_status=401,
        )
        assert status == 401

        coll_inst = tmp / "collector-install"
        coll_inst.mkdir(parents=True)
        assert (
            install_hooks.install(
                mode="project",
                target=coll_inst,
                collector_url=base,
                collector_token=collector_token,
            )
            == 0
        )
        env_text = (coll_inst / ".cursor" / "agent-trace.env").read_text(encoding="utf-8")
        assert f"COLLECTOR_URL={base}" in env_text
        assert f"COLLECTOR_TOKEN={collector_token}" in env_text
        assert "COLLECTOR_BATCH_SIZE=10" in env_text
        assert "COLLECTOR_FLUSH_INTERVAL_SEC=2" in env_text
        assert "COLLECTOR_RETRY_BASE_SEC=1" in env_text

        coll_proj = tmp / "collector-project"
        (coll_proj / ".cursor").mkdir(parents=True)
        shutil.copy(
            ROOT / "templates" / "agent-trace.policy.json",
            coll_proj / ".cursor" / "agent-trace.policy.json",
        )
        (coll_proj / ".cursor" / "agent-trace.env").write_text(
            "CURSOR_AGENT_TRACE_REDACT=safe\n"
            "CURSOR_AGENT_TRACE_SIGN=1\n"
            "CURSOR_AGENT_TRACE_S3=0\n"
            f"COLLECTOR_URL={base}\n"
            f"COLLECTOR_TOKEN={collector_token}\n"
            "COLLECTOR_BATCH_SIZE=1\n"
            "COLLECTOR_FLUSH_INTERVAL_SEC=0\n"
            "COLLECTOR_RETRY_BASE_SEC=0.1\n"
            "COLLECTOR_RETRY_MAX_SEC=1\n"
            "COLLECTOR_TIMEOUT=2\n",
            encoding="utf-8",
        )
        conv_coll = f"e2e-coll-{int(time.time())}"
        for event, extra in [
            ("beforeSubmitPrompt", {"prompt": "collector path TOKEN=secret"}),
            ("stop", {"status": "completed"}),
        ]:
            _run_hook(
                coll_proj,
                e2e_home,
                {
                    "conversation_id": conv_coll,
                    "generation_id": "gen-coll",
                    "model": "default",
                    "model_id": "composer-2",
                    "hook_event_name": event,
                    **extra,
                },
            )
        coll_trail = coll_proj / ".cursor" / "agent-trace" / f"{conv_coll}.jsonl"
        meta = coll_trail.with_suffix(".collector.jsonl")
        assert coll_trail.is_file(), coll_trail
        assert meta.is_file(), "collector meta must be logged"
        meta_rows = [
            json.loads(l) for l in meta.read_text(encoding="utf-8").splitlines() if l.strip()
        ]
        assert any(
            r.get("ok") and r.get("kind") in {"batch", "event", "buffer"} and r.get("flushed")
            for r in meta_rows
        ), meta_rows
        assert any(r.get("ok") and r.get("kind") == "trace" for r in meta_rows), meta_rows
        assert "TOKEN=secret" not in coll_trail.read_text(encoding="utf-8")
        print("hook→collector batch OK", len(meta_rows), "meta lines")

        assert (collector_data / "cursor-agent-trace" / f"{conv_coll}.jsonl").is_file()
        assert (collector_data / "cursor-agent-trace" / f"{conv_coll}.trace.json").is_file()
        data = collector_data / "cursor-agent-trace"
        lines = [
            json.loads(l)
            for l in (data / f"{conv_coll}.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        assert len(lines) >= 2, lines
        ctrace = json.loads((data / f"{conv_coll}.trace.json").read_text(encoding="utf-8"))
        assert ctrace["eat_profile"].startswith("tag:agentrust-io.com")
        assert "signature" in ctrace
        print("collector disk artifacts OK")

        # Fail-open: collector down → outbox filled
        _stop_collector(collector_proc)
        collector_proc = None

        conv_down = f"e2e-down-{int(time.time())}"
        out = _run_hook(
            coll_proj,
            e2e_home,
            {
                "conversation_id": conv_down,
                "generation_id": "gen-down",
                "model": "default",
                "hook_event_name": "beforeSubmitPrompt",
                "prompt": "still works offline",
            },
        )
        assert out.get("continue") is True, out
        down_trail = coll_proj / ".cursor" / "agent-trace" / f"{conv_down}.jsonl"
        assert down_trail.is_file(), "local trail always written"
        down_meta = down_trail.with_suffix(".collector.jsonl")
        assert down_meta.is_file()
        row = json.loads(down_meta.read_text(encoding="utf-8").splitlines()[-1])
        assert row.get("ok") is False, row
        outbox = coll_proj / ".cursor" / "agent-trace" / "outbox"
        assert outbox.is_dir(), outbox
        queued = list(outbox.glob("*.json"))
        assert queued, "outbox must hold failed batch"
        entry = json.loads(queued[0].read_text(encoding="utf-8"))
        assert entry["payload"]["kind"] == "batch"
        assert len(entry["payload"]["events"]) >= 1
        print("fail-open + outbox OK", queued[0].name)

        # Restart collector → opportunistic replay
        collector_proc = _start_collector(collector_data, collector_log, collector_token)
        _wait_health(f"{base}/healthz")

        for p in outbox.glob("*.json"):
            e = json.loads(p.read_text(encoding="utf-8"))
            e["next_attempt_at"] = 0
            p.write_text(json.dumps(e, indent=2) + "\n", encoding="utf-8")
        print("outbox made due:", len(list(outbox.glob("*.json"))))

        conv_replay = f"e2e-replay-{int(time.time())}"
        assert list(outbox.glob("*.json")), "precondition: outbox non-empty"
        _run_hook(
            coll_proj,
            e2e_home,
            {
                "conversation_id": conv_replay,
                "generation_id": "gen-replay",
                "model": "default",
                "hook_event_name": "beforeSubmitPrompt",
                "prompt": "replay trigger",
            },
        )

        for _ in range(10):
            remaining = list(outbox.glob("*.json"))
            if not remaining:
                break
            for p in remaining:
                e = json.loads(p.read_text(encoding="utf-8"))
                e["next_attempt_at"] = 0
                p.write_text(json.dumps(e, indent=2) + "\n", encoding="utf-8")
            _run_hook(
                coll_proj,
                e2e_home,
                {
                    "conversation_id": conv_replay,
                    "generation_id": "gen-replay",
                    "model": "default",
                    "hook_event_name": "afterAgentResponse",
                    "text": "nudge",
                },
            )
            time.sleep(0.05)

        remaining = list(outbox.glob("*.json"))
        assert not remaining, f"outbox not drained: {remaining}"
        down_path = data / f"{conv_down}.jsonl"
        assert down_path.is_file(), down_path
        lines = [
            json.loads(l) for l in down_path.read_text(encoding="utf-8").splitlines() if l.strip()
        ]
        assert lines, lines
        print("outbox replay drain OK", down_path.name, "lines=", len(lines))
    finally:
        _stop_collector(collector_proc)

    print("== verify signed TRACE ==")
    assert verify_trace.verify(trace) == 0
    assert verify_trace.verify(offline) == 0
    assert verify_trace.verify(trail_dir / f"{conv3}.trace.json") == 0

    examples = ROOT / "examples"
    examples.mkdir(parents=True, exist_ok=True)
    shutil.copy(trace, examples / "sample.trace.json")
    shutil.copy(honesty_path, examples / "sample.honesty.json")
    print(f"copied sample → {examples / 'sample.trace.json'}")

    print("all e2e checks passed")
    return 0


def add_parser_clean(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "manual-e2e",
        help="Optional manual E2E walkthrough (prefer pytest)",
        description=__doc__,
    )
    p.set_defaults(_handler=lambda _a: run_manual_e2e())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manual_e2e.py", description=__doc__)
    parser.parse_args(argv)
    return run_manual_e2e()


if __name__ == "__main__":
    raise SystemExit(main())
