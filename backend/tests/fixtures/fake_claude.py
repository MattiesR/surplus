"""FakeClaude for the get_usage probe: answers control requests from a recorded fixture.

Mode via FAKE_CLAUDE_MODE: ok | exit (dies without answering) | hang | error | prompt_guard.
In every mode, receiving a user message is a hard failure: the probe must never send one.
"""

import json
import os
import sys
import time
from pathlib import Path

FIXTURE = Path(__file__).parent / "probe" / "get_usage_response.json"
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")

if mode == "exit":
    sys.stderr.write("Invalid API key · Please run /login\n")
    sys.exit(1)

for raw in sys.stdin:
    msg = json.loads(raw)
    if msg.get("type") == "user":
        sys.stderr.write("PROBE SENT A USER MESSAGE\n")
        sys.exit(99)
    if msg.get("type") != "control_request":
        continue
    rid = msg["request_id"]
    sub = msg["request"]["subtype"]
    if sub == "initialize":
        out = {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": rid, "response": {}},
        }
    elif sub == "get_usage":
        if mode == "hang":
            time.sleep(30)
        if mode == "error":
            out = {
                "type": "control_response",
                "response": {"subtype": "error", "request_id": rid, "error": "nope"},
            }
        else:
            recorded = json.loads(FIXTURE.read_text())
            recorded["response"]["request_id"] = rid
            out = recorded
    else:
        continue
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
