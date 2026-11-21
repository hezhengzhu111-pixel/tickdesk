"""R06 large-response acceptance: 190 closed samples + 300 exclusions over WSS."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance_business_probe import Peer  # noqa: E402

DAY = 86_400_000
SYMBOL = "300511.SZ"


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

    def page(command_fields: dict):
        result = peer.call("get_trade_review", command_fields)
        size = len(json.dumps(peer.last_ack["payload"], ensure_ascii=False,
                              separators=(",", ":")).encode())
        check(f"ack.size.{command_fields.get('view','items')}.{command_fields.get('cursor','first')[:12]}",
              size <= 64 * 1024, True)
        return result

    with Peer(pairing, args.ca) as peer:
        for index in range(190):
            peer.call("record_execution", {
                "symbol": SYMBOL, "side": "buy", "price": "10", "qty": 1,
                "fees": "0", "executedAt": now - 20 * DAY + index * 1_000,
            })
            peer.call("record_execution", {
                "symbol": SYMBOL, "side": "sell", "price": "11", "qty": 1,
                "fees": "0", "executedAt": now - 10 * DAY + index * 1_000,
            })
        for index in range(300):
            peer.call("record_execution", {
                "symbol": SYMBOL, "side": "sell", "price": "11", "qty": 1,
                "fees": "0", "executedAt": now - 5 * DAY + index * 1_000,
            })

        items = {}
        cursor = None
        while True:
            fields = {"symbol": SYMBOL, "limit": 50, "view": "items"}
            if cursor:
                fields["cursor"] = cursor
            result = page(fields)
            check("items.summary.closedTrades", result["summary"]["closedTrades"], 190)
            check("items.summary.excludedCount", result["summary"]["excludedCount"], 300)
            for item in result["items"]:
                items[item["sellExecutionId"]] = item
            cursor = result["nextCursor"]
            if not cursor:
                break
        check("items.unique", len(items), 190)

        samples = {}
        cursor = None
        while True:
            fields = {"symbol": SYMBOL, "limit": 50, "view": "samples"}
            if cursor:
                fields["cursor"] = cursor
            result = page(fields)
            check("samples.summary.closedSamples", result["summary"]["closedSamples"], 190)
            for sample in result["samples"]:
                samples[sample["entryKey"]] = sample
            cursor = result["nextCursor"]
            if not cursor:
                break
        check("samples.unique", len(samples), 190)

        exclusions = {}
        cursor = None
        while True:
            fields = {"symbol": SYMBOL, "limit": 100, "view": "exclusions"}
            if cursor:
                fields["cursor"] = cursor
            result = page(fields)
            for row in result["exclusions"]:
                exclusions[row["executionId"]] = row
            cursor = result["nextCursor"]
            if not cursor:
                break
        check("exclusions.unique", len(exclusions), 300)

        first = page({"symbol": SYMBOL, "limit": 2, "view": "items"})
        cursor = first["nextCursor"]
        ok, _, error = peer.try_call("get_trade_review",
                                     {"symbol": SYMBOL, "limit": 2, "view": "samples", "cursor": cursor})
        check("cross-view.ok", ok, False)
        check("cross-view.code", error["code"], "TRADE_REVIEW_CURSOR_EXPIRED")
        ok, _, error = peer.try_call("get_trade_review",
                                     {"symbol": SYMBOL, "from": now - DAY, "limit": 2,
                                      "view": "items", "cursor": cursor})
        check("cross-query.ok", ok, False)
        check("cross-query.code", error["code"], "TRADE_REVIEW_CURSOR_EXPIRED")

    if failures:
        raise SystemExit("R06 large failures: " + ", ".join(failures[:10]))
    print("R06 LARGE PASS")


if __name__ == "__main__":
    main()
