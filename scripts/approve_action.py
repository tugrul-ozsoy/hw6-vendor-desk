"""Human approval for side-effect tools (Problem 6, design layer).

A vendor document can never authorize send_email or approve_payment: in the defended agent those calls are
refused and logged to output/blocked_actions.json. This script is the only way to run one, and it needs a named
staff member who types the confirmation.

    python scripts/approve_action.py                                   # list blocked requests
    python scripts/approve_action.py --approve 3 --approver "Jordan Lee"
    python scripts/approve_action.py --reject 3 --approver "Jordan Lee"

The tools are still the Problem 3 decoys, so an approved call only adds one entry to output/trap_calls.json
marked approved_by. Nothing is sent and no money moves.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from defenses import BLOCKED_ACTIONS, human_approve  # noqa: E402
from tools import _LOG_LOCK, _write_json, read_json_array  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Review blocked side-effect requests")
    p.add_argument("--approve", type=int, help="index of the blocked request to approve and run")
    p.add_argument("--reject", type=int, help="index of the blocked request to reject")
    p.add_argument("--approver", default="", help="your name (required to approve or reject)")
    args = p.parse_args()
    entries = read_json_array(BLOCKED_ACTIONS)
    if args.approve is None and args.reject is None:
        for i, e in enumerate(entries):
            print(f"#{i} {e['status']:<30} {e['tool']:<16} from {e['document']} (run {e['run_id']}): "
                  f"{json.dumps(e['arguments'], ensure_ascii=False)[:120]}")
        print(f"{len(entries)} blocked request(s)." if entries else "No blocked requests.")
        return 0
    if not args.approver.strip():
        p.error("--approver with your name is required")
    index = args.approve if args.approve is not None else args.reject
    if not 0 <= index < len(entries):
        p.error(f"no blocked request #{index}")
    e = entries[index]
    print(f"Request #{index}: {e['tool']} {json.dumps(e['arguments'], ensure_ascii=False)}")
    print(f"It came from vendor document {e['document']}, which is untrusted content.")
    word = "APPROVE" if args.approve is not None else "REJECT"
    if input(f"Type {word} to confirm: ").strip() != word:
        print("Not confirmed; nothing changed.")
        return 1
    if args.approve is not None:
        human_approve(index, args.approver)
        print(f"Approved by {args.approver}; the (decoy) tool ran and was logged to output/trap_calls.json.")
    else:
        with _LOG_LOCK:
            entries = read_json_array(BLOCKED_ACTIONS)
            if entries[index]["status"] != "blocked_pending_human_review":
                p.error(f"request #{index} is already {entries[index]['status']}")
            entries[index].update(status="rejected", approved_by=args.approver.strip())
            _write_json(BLOCKED_ACTIONS, entries)
        print(f"Rejected by {args.approver}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
