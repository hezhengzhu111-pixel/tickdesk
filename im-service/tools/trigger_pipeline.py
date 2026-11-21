import json
import time
import urllib.request

BASE = "http://127.0.0.1:3011"


def req(path, method="GET", timeout=30):
    r = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except Exception as e:  # noqa: BLE001
        body = getattr(e, "read", lambda: b"")()
        return None, {"error": str(e), "body": body.decode("utf-8", "replace")[:400]}


spec_status, spec = req("/api/openapi.json", timeout=30)
paths = sorted(p for p in spec.get("paths", {}) if "pipeline" in p or "job" in p)
print("pipeline/job paths:", paths)

status, body = req("/api/pipeline/run", method="POST", timeout=60)
print("POST /api/pipeline/run ->", status, body)
job_id = body.get("job_id") if isinstance(body, dict) else None

if job_id:
    for i in range(40):
        time.sleep(3)
        s, j = req(f"/api/pipeline/jobs/{job_id}")
        if s is None:
            print("status query failed:", j)
            break
        print(
            f"[{i*3:>3}s]",
            j.get("status"),
            f"{j.get('progress', '-')}%",
            j.get("stage") or "",
            (j.get("message") or "")[:120],
        )
        if j.get("status") in {"done", "failed", "cancelled"}:
            print("final:", json.dumps(j, ensure_ascii=False)[:1500])
            break
