"""R01 acceptance over the real TLS/WSS relay against an isolated local DB."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance_business_probe import Peer  # noqa: E402

DAY = 86_400_000


def executions(db_path: Path, symbol: str) -> int:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        return db.execute(
            "SELECT count(*) FROM objects WHERE collection='executions' "
            "AND json_extract(body,'$.symbol')=? AND json_extract(body,'$.positionEffect')<>'superseded'",
            (symbol,),
        ).fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairing", required=True)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    pairing = json.loads(Path(args.pairing).read_text(encoding="utf-8"))
    database = Path(args.database)
    now = int(time.time() * 1000)
    failures: list[str] = []

    def check(label, actual, expected):
        ok = actual == expected
        print(f"{'PASS' if ok else 'FAIL'} {label}: actual={actual!r} expected={expected!r}")
        if not ok:
            failures.append(label)

    with Peer(pairing, args.ca) as peer:
        # A: negative gross and negative net survive the summary.
        sym = "300501.SZ"
        peer.call("record_execution", {"symbol": sym, "side": "buy", "price": "10", "qty": 100,
                                       "fees": "2", "executedAt": now - 2 * DAY})
        peer.call("record_execution", {"symbol": sym, "side": "sell", "price": "9", "qty": 100,
                                       "fees": "3", "executedAt": now - DAY})
        review = peer.call("get_trade_review", {"symbol": sym})
        check("A.item.grossPnl", review["items"][0]["grossPnl"], "-100")
        check("A.item.netPnl", review["items"][0]["netPnl"], "-105")
        check("A.summary.grossPnl", review["summary"]["grossPnl"], "-100")
        check("A.summary.netPnl", review["summary"]["netPnl"], "-105")
        check("A.summary.losses", review["summary"]["losses"], 1)
        check("A.summary.flat", review["summary"]["flat"], 0)
        check("A.db.executions", executions(database, sym), 2)

        # B: the original buy fee is allocated once across two sells.
        sym = "300502.SZ"
        peer.call("record_execution", {"symbol": sym, "side": "buy", "price": "10", "qty": 100,
                                       "fees": "2", "executedAt": now - 3 * DAY})
        peer.call("record_execution", {"symbol": sym, "side": "sell", "price": "11", "qty": 40,
                                       "fees": "3", "executedAt": now - 2 * DAY})
        peer.call("record_execution", {"symbol": sym, "side": "sell", "price": "11", "qty": 60,
                                       "fees": "3", "executedAt": now - DAY})
        review = peer.call("get_trade_review", {"symbol": sym})
        fees = [item["buyFee"] for item in review["items"]]
        check("B.buyFees", fees, ["0.8", "1.2"])
        check("B.summary.netPnl", review["summary"]["netPnl"], "92")
        check("B.summary.closedSamples", review["summary"]["closedSamples"], 1)
        samples = peer.call("get_trade_review", {"symbol": sym, "view": "samples"})
        check("B.sample.netPnl", samples["samples"][0]["netPnl"], "92")
        check("B.db.executions", executions(database, sym), 3)

        # C: history before `from` still feeds FIFO; the in-period fragment is 60.
        sym = "300503.SZ"
        peer.call("record_execution", {"symbol": sym, "side": "buy", "price": "10", "qty": 100,
                                       "fees": "2", "executedAt": now - 5 * DAY})
        peer.call("record_execution", {"symbol": sym, "side": "sell", "price": "11", "qty": 40,
                                       "fees": "3", "executedAt": now - 3 * DAY})
        peer.call("record_execution", {"symbol": sym, "side": "sell", "price": "11", "qty": 60,
                                       "fees": "3", "executedAt": now - DAY})
        review = peer.call("get_trade_review", {"symbol": sym, "from": now - 2 * DAY, "to": now + 1})
        check("C.items.len", len(review["items"]), 1)
        check("C.item.qty", review["items"][0]["qty"], 60)
        check("C.item.grossPnl", review["items"][0]["grossPnl"], "60")
        check("C.item.buyFee", review["items"][0]["buyFee"], "1.2")
        check("C.summary.openLots", review["summary"]["openLots"], 0)
        check("C.db.executions", executions(database, sym), 3)

        # D: positive price difference can still be a net loss.
        sym = "300504.SZ"
        peer.call("record_execution", {"symbol": sym, "side": "buy", "price": "10", "qty": 100,
                                       "fees": "50", "executedAt": now - 2 * DAY})
        peer.call("record_execution", {"symbol": sym, "side": "sell", "price": "10.2", "qty": 100,
                                       "fees": "60", "executedAt": now - DAY})
        review = peer.call("get_trade_review", {"symbol": sym})
        check("D.grossPnl", review["summary"]["grossPnl"], "20")
        check("D.netPnl", review["summary"]["netPnl"], "-90")
        check("D.losses", review["summary"]["losses"], 1)
        check("D.priceWins", review["summary"]["priceWins"], 1)

        # E: known zero fee and unknown fee are different states.
        sym = "300505.SZ"
        peer.call("record_execution", {"symbol": sym, "side": "buy", "price": "10", "qty": 100,
                                       "fees": "0", "executedAt": now - 2 * DAY})
        peer.call("record_execution", {"symbol": sym, "side": "sell", "price": "11", "qty": 100,
                                       "fees": "0", "executedAt": now - DAY})
        review = peer.call("get_trade_review", {"symbol": sym})
        check("E.zeroFee.netPnl", review["summary"]["netPnl"], "100")
        check("E.zeroFee.missing", review["summary"]["missingFeeCount"], 0)
        sym = "300506.SZ"
        peer.call("record_execution", {"symbol": sym, "side": "buy", "price": "10", "qty": 100,
                                       "executedAt": now - 2 * DAY})
        peer.call("record_execution", {"symbol": sym, "side": "sell", "price": "11", "qty": 100,
                                       "executedAt": now - DAY})
        review = peer.call("get_trade_review", {"symbol": sym})
        check("F.unknownFee.netPnl", review["summary"]["netPnl"], None)
        check("F.unknownFee.missing", review["summary"]["missingFeeCount"], 1)
        check("F.unknownFee.complete", review["summary"]["netPnlComplete"], False)

    if failures:
        raise SystemExit("R01 failures: " + ", ".join(failures))
    print("R01 PASS")


if __name__ == "__main__":
    main()
