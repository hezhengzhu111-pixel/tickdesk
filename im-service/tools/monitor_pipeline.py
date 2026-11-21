"""Trigger the first-run data pipeline and follow its progress."""
import json
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:3011"
LOG = Path(__file__).resolve().parent.parent / "data" / "logs" / "backend.log"


def req(path, method="GET", timeout=30):
    r = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def tail_new(lines_seen, n=400):
    try:
        text = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return lines_seen, []
    new = text[lines_seen:]
    return len(text), new[-n:]


job = req("/api/pipeline/run", method="POST", timeout=60)
print("pipeline job:", job, flush=True)
job_id = job.get("job_id")

seen = 0
seen, _ = tail_new(seen)
last = None
start = time.time()
for i in range(120):  # up to 12 minutes
    time.sleep(6)
    try:
        j = req(f"/api/pipeline/jobs/{job_id}")
    except Exception as e:  # noqa: BLE001
        print(f"[{int(time.time()-start):>4}s] status error: {e}", flush=True)
        continue
    key = (j.get("status"), j.get("stage"), j.get("progress"))
    if key != last:
        print(
            f"[{int(time.time()-start):>4}s] {j.get('status')} {j.get('progress')}% "
            f"{j.get('stage') or ''} {(j.get('message') or '')[:150]}",
            flush=True,
        )
        last = key
    seen, new_lines = tail_new(seen)
    for line in new_lines:
        if any(k in line for k in ("pipeline", "daily", "batch", "sync", "enriched", "阶段", "pipeline_jobs")):
            print("   log:", line[:220], flush=True)
    if j.get("status") in {"done", "failed", "cancelled"}:
        print("FINAL:", json.dumps(j, ensure_ascii=False)[:2000], flush=True)
        break
else:
    print("monitor window elapsed; pipeline continues in backend", flush=True)
