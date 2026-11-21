import json
import urllib.request


def get(url, timeout=20):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except Exception as e:  # noqa: BLE001
        return None, str(e).encode()


def show(url, n=300):
    status, body = get(url)
    text = body[:n].decode("utf-8", "replace") if body else ""
    print(f"{url} -> {status}")
    print(f"  {text[:n]}")
    return status, body


show("http://127.0.0.1:3011/")
show("http://127.0.0.1:3011/health")
status, body = get("http://127.0.0.1:3011/api/openapi.json")
if status == 200:
    spec = json.loads(body)
    paths = sorted(spec.get("paths", {}).keys())
    print(f"\nAPI paths via proxy: {len(paths)}")
    for p in paths[:25]:
        print("  ", p)
    for candidate in ["/api/overview", "/api/overview/summary", "/api/indices", "/api/data/status"]:
        if candidate in spec.get("paths", {}):
            show("http://127.0.0.1:3011" + candidate, 400)
