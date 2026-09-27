# Cursor Agent TRACE

**Capture:** Cursor hooks write conversation activity to JSONL (prompts, tool calls, outcomes).

**Attest:** That activity becomes a signed [AgentRust TRACE](https://trace.agentrust-io.com/) (Level 0) record you can verify with their conformance tools.

**Govern:** Optional policy allow/deny on tool use, plus an org collector that batches trails to S3 with offline retry.

## Prerequisites

- Python 3.10+
- [Cursor](https://cursor.com/) IDE with hooks enabled
- Optional: org collector / S3 (see [Org collector / S3](#org-collector--s3-optional))

## Quick Start

```bash
git clone https://github.com/saintmalik/cursor-agent-trace.git
cd cursor-agent-trace

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m lib install-hooks --user
python -m lib doctor
```

What setup does:

- Merges this project's hook command into `~/.cursor/hooks.json`
- Writes `~/.cursor/agent-trace.env` (redact / sign / collector defaults) if missing
- Writes `~/.cursor/agent-trace.policy.json` if missing (never overwrites an existing policy)

Then confirm the hook under **Customize → Hooks**, send an Agent message, and look for artifacts under `<project>/.cursor/agent-trace/`.

Project-only install:

```bash
python -m lib install-hooks --project /path/to/repo
```

## Configuration

Config loads from `$CURSOR_PROJECT_DIR/.cursor/agent-trace.env` or `~/.cursor/agent-trace.env` (process env wins). Full knobs live in [`env.example`](env.example).

| Setting | Purpose |
| --- | --- |
| `CURSOR_AGENT_TRACE_REDACT` | `safe` (default) or `full` body retention |
| `CURSOR_AGENT_TRACE_SIGN` | Sign TRACE records (`1` by default) |
| `COLLECTOR_URL` / `COLLECTOR_TOKEN` | Org ingest (off unless URL is set) |
| `CURSOR_AGENT_TRACE_S3_*` | Optional direct S3 (prefer collector for teams) |
| `agent-trace.policy.json` | Allow/deny rules (not an env var) |

Policy resolution: project `.cursor/agent-trace.policy.json` → `~/.cursor/` → shipping template.

## Usage

After install, capture is automatic. Artifacts per conversation: `<id>.jsonl`, `<id>.trace.json`, `<id>.honesty.json`.

```bash
# Health check
python -m lib doctor

# Browse local trails (library mode)
python -m lib view

# Assemble a signed TRACE from JSONL
python bin/jsonl-to-trace.py path/to/conversation.jsonl

# Verify a signed TRACE (Level 0)
python -m lib verify path/to/conversation.trace.json
```

## Policy (optional)

Edit `~/.cursor/agent-trace.policy.json` (or the project copy):

```json
{
  "mode": "enforce",
  "deny_file_read": [".env", ".env.*", "**/.env", "**/credentials.json", "**/*secret*"],
  "deny_shell_patterns": ["rm -rf /", "rm -rf /*", "mkfs", "dd if="],
  "deny_tools": []
}
```

- **`enforce`:** matching reads / shell / tools return `permission: deny` (still logged)
- **`observe`:** same rules logged as `would_deny`, always allow

## Org collector / S3 (optional)

Point laptops at your org collector so AWS credentials stay on the server:

```bash
python -m lib install-hooks --user \
  --collector-url https://trace-collector.example.com \
  --collector-token "$ORG_COLLECTOR_TOKEN"
```

Hooks buffer on disk, POST batches to `/v1/ingest`, and keep a durable outbox under `<project>/.cursor/agent-trace/outbox/` on failure. Always fail-open.

See [`collector/README.md`](collector/README.md) for the server.

## Fleet

Silent per-user install (suitable for login scripts or MDM "run as user"):

```bash
python -m lib install-hooks --user --quiet
```

Or:

```bash
TRACE_HOME=/opt/cursor-agent-trace ./scripts/fleet-install.sh
```

`--quiet` / `--noninteractive` print a single `ok …` line and use the same deterministic paths as a normal `--user` install.

## Viewer

```bash
python -m lib view
```

Opens `http://127.0.0.1:8765/` in library mode: lists conversations from configured trail dirs (and optional S3 when configured). Click a trail for timeline + Level-0 verify.

```bash
python -m lib view path/to/conversation.jsonl
python -m lib view --library --dir /path/to/extra/agent-trace
python -m lib view --port 8765 --no-open
```

Deep links: `/?id=<conversation_id>` or `/t/<conversation_id>`.

## Verify / Level-0 honesty

This produces a **software-observed** TRACE ([AgentRust conformance levels](https://tests.agentrust-io.com/docs/levels/)):

- **Model:** Whatever Cursor puts on `model` / `model_id` in hook payloads, recorded as-is (often `default`)
- **Platform:** `software-only`, appraisal `none`, `third-party-control-plane`
- **Capture:** Agent and Tab events Cursor exposes via hooks; bodies redacted by default (`safe`)
- **Deny:** Narrow when `mode: enforce`; hook crashes fail-open

```bash
python -m lib verify path/to/conversation.trace.json
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Hooks not firing | Cursor **Customize → Hooks**; restart Cursor after install; `python -m lib doctor` |
| No TRACE files | Send an Agent message in a project; look under `<project>/.cursor/agent-trace/` |
| `doctor` fails | Re-run `pip install -r requirements.txt` and `python -m lib install-hooks --user` |

## Uninstall

Remove entries whose `command` contains `cursor-agent-trace-hook.py` from `~/.cursor/hooks.json` (and any project `.cursor/hooks.json`). Optionally delete `~/.cursor/agent-trace.env` and `~/.cursor/agent-trace.policy.json`. Trails under `.cursor/agent-trace/` are left in place.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

## License

Licensed under the [Apache License 2.0](LICENSE).

## References

- [Cursor hooks](https://cursor.com/docs/hooks)
- [AgentRust get started](https://agentrust-io.com/quickstart/)
- [AgentRust TRACE](https://trace.agentrust-io.com/)
- [AgentRust conformance levels](https://tests.agentrust-io.com/docs/levels/)
