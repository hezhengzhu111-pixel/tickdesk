"""R06 acceptance: paged review, cursor expiry, and manual review notes over WSS."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance_business_probe import Peer  # noqa: E402

DAY = 86_400_000
SYMBOL = "300509.SZ"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairing", required=True)
    parser.add_argument("--ca", required=True)
    args = parser.parse_args()
    pairing = json.loads(Path(args.pairing).read_text(encoding="utf-8"))
    now = int(time.time() * 1000)
    failures: list[str] = []

    def check(label, actual, expected):
        ok = actual == expected
        print(f"{'PASS' if ok else 'FAIL'} {label}: actual={actual!r} expected={expected!r}")
        if not ok:
            failures.append(label)

    with Peer(pairing, args.ca) as peer:
        executions = []
        for index in range(6):
            peer.call("record_execution", {
                "symbol": SYMBOL, "side": "buy", "price": "10", "qty": 10,
                "fees": "0", "executedAt": now - (12 - index) * DAY,
            })
            sold = peer.call("record_execution", {
                "symbol": SYMBOL, "side": "sell", "price": "11", "qty": 10,
                "fees": "0", "executedAt": now - (6 - index) * DAY,
            })
            executions.append(sold["execution"]["executionId"])

        first = peer.call("get_trade_review", {"symbol": SYMBOL, "limit": 2})
        check("page1.items", len(first["items"]), 2)
        check("page1.summary.closedTrades", first["summary"]["closedTrades"], 6)
        check("page1.summary.grossPnl", first["summary"]["grossPnl"], "60")
        check("page1.hasMore", first["hasMore"], True)
        cursor = first["nextCursor"]
        second = peer.call("get_trade_review", {"symbol": SYMBOL, "limit": 2, "cursor": cursor})
        check("page2.items", len(second["items"]), 2)
        check("page2.summary.closedTrades", second["summary"]["closedTrades"], 6)
        check("page2.samples", second["samples"], [])
        third = peer.call("get_trade_review", {"symbol": SYMBOL, "limit": 2, "cursor": second["nextCursor"]})
        check("page3.items", len(third["items"]), 2)
        check("page3.hasMore", third["hasMore"], False)

        peer.call("record_execution", {
            "symbol": SYMBOL, "side": "buy", "price": "10", "qty": 10,
            "fees": "0", "executedAt": now - DAY,
        })
        peer.call("record_execution", {
            "symbol": SYMBOL, "side": "sell", "price": "11", "qty": 1,
            "fees": "0", "executedAt": now,
        })
        ok, _, error = peer.try_call(
            "get_trade_review", {"symbol": SYMBOL, "limit": 2, "cursor": cursor}
        )
        check("expired.ok", ok, False)
        check("expired.code", error["code"], "TRADE_REVIEW_CURSOR_EXPIRED")

        note_id = "note_r06_a"
        recorded = peer.call("record_trade_review_note", {
            "noteId": note_id, "subjectType": "execution", "subjectId": executions[0],
            "verdict": "false_positive", "note": "人工复核：仅记录，不改成交。",
            "reviewer": "local_user", "evidenceExecutionIds": [executions[0]],
            "at": now,
        })
        check("note.version", recorded["note"]["version"], 1)
        listed = peer.call("get_trade_review_notes", {"subjectId": executions[0]})
        check("note.count", listed["manualCount"], 1)
        check("note.id", listed["items"][0]["noteId"], note_id)
        ok, _, error = peer.try_call("record_trade_review_note", {
            "noteId": "note_r06_b", "subjectType": "execution", "subjectId": executions[0],
            "verdict": "false_positive", "note": "人工复核：仅记录，不改成交。",
            "reviewer": "local_user", "evidenceExecutionIds": [executions[0]],
            "at": now,
        })
        check("duplicate.ok", ok, False)
        check("duplicate.code", error["code"], "TRADE_REVIEW_NOTE_DUPLICATE")
        withdrawn = peer.call("withdraw_trade_review_note", {
            "noteId": note_id, "baseVersion": 1, "reason": "閲嶅璁板綍", "at": now,
        })
        check("withdraw.status", withdrawn["note"]["status"], "withdrawn")
        check("withdraw.version", withdrawn["note"]["version"], 2)

    if failures:
        raise SystemExit("R06 failures: " + ", ".join(failures))
    print("R06 PASS")


if __name__ == "__main__":
    main()

