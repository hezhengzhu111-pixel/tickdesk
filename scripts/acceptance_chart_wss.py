import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance_business_probe import Peer

parser = argparse.ArgumentParser()
parser.add_argument("--pairing", required=True)
parser.add_argument("--ca", required=True)
args = parser.parse_args()
pairing = json.loads(Path(args.pairing).read_text(encoding="utf-8"))
with Peer(pairing, args.ca) as peer:
    for timeframe in ("1m", "5m"):
        result = peer.call("get_chart", {"symbol": "300502.SZ", "adjustment": "none",
                                         "timeframe": timeframe, "limit": 6})
        candles = result["candles"]
        print(timeframe, "status", result["dataStatus"], "count", len(candles),
              "hasMore", result["hasMore"], "cursor", bool(result["nextCursor"]))
        print(json.dumps(candles[:2], ensure_ascii=False))
        if timeframe == "1m":
            dup = [c for c in candles if c.get("quality") == "duplicate_minute"]
            print("duplicate", dup)
        if timeframe == "5m":
            print("quality", [c.get("quality") for c in candles])
        if result["nextCursor"]:
            page2 = peer.call("get_chart", {"symbol": "300502.SZ", "adjustment": "none",
                                            "timeframe": timeframe, "limit": 6,
                                            "cursor": result["nextCursor"]})
            print(timeframe, "page2", len(page2["candles"]), page2["hasMore"])
