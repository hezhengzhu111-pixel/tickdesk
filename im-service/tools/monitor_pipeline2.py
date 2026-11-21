"""Follow the long first-run pipeline until it finishes (or ~70 min elapse)."""
import json
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:3018"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "logs" / "backend.log"


def req(path, method="GET", timeout=30):
    r = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


job_id = "ad8649e1ec"
start = time.time()
last = None
seen = 0
deadline = 70 * 60
while time.time() - start < deadline:
    time.sleep(20)
    try:
        j = req(f"/api/pipeline/jobs/{job_id}")
    except Exception as e:  # noqa: BLE001
        print(f"[{int(time.time()-start):>4}s] backend unreachable: {e}", flush=True)
        continue
    key = (j.get("status"), j.get("stage"), j.get("progress"))
    if key != last:
        print(
            f"[{int(time.time()-start):>4}s] {j.get('status')} {j.get('progress')}% "
            f"{j.get('stage') or ''} {(j.get('message') or '')[:160]}",
            flush=True,
        )
        last = key
    if j.get("status") in {"done", "failed", "cancelled"}:
        print("FINAL:", json.dumps(j, ensure_ascii=False)[:3000], flush=True)
        break
else:
    print("monitor deadline reached; pipeline still running", flush=True)

# data sanity check
try:
    latest = req("/api/kline/daily/latest?symbol=000001.SZ", timeout=30)
    print("latest kline 000001.SZ:", json.dumps(latest, ensure_ascii=False)[:400], flush=True)
except Exception as e:  # noqa: BLE001
    print("latest kline query failed:", e, flush=True)

try:
    files = [p for p in (ROOT / "data").rglob("*") if p.is_file() and p.suffix in {".parquet", ".duckdb"}]
    total = sum(p.stat().st_size for p in files)
    print(f"data files: {len(files)} parquet/duckdb, {total/1048576:.1f} MB", flush=True)
except Exception as e:  # noqa: BLE001
    print("data dir scan failed:", e, flush=True)
