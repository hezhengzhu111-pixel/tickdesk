import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Event

import httpx
import pytest

from app.services.tickdesk_regime_archive import import_calendar, import_probe, lookup, replay
from app.services.tickdesk_regime_collect import collect_once
from app.services.tickdesk_regime_evidence import INDUSTRY_FIELD, MARKET_FIELD
from tests.test_tickdesk_regime_archive import (
    DAY,
    INDUSTRY,
    MARKET,
    MORNING,
    SESSIONS,
    SYMBOL,
    artifact,
    members,
    prices,
)


def seed_calendar(db, covered=True):
    days = [*SESSIONS, DAY] if covered else SESSIONS
    import_calendar(
        db,
        *artifact(
            {
                "source": "fuyao",
                "through": days[-1].isoformat(),
                "observedAt": int(MORNING.timestamp() * 1000),
                "sessions": [day.isoformat() for day in days],
            }
        ),
    )


def body(packet):
    # Exact spelling and whitespace must survive the capture layer.
    return (json.dumps(packet["response"], indent=2) + "\n").encode()


def client(handler):
    return lambda: httpx.Client(
        base_url="https://fuyao.example", transport=httpx.MockTransport(handler)
    )


def test_disabled_and_outside_scope_never_make_requests_or_claim(tmp_path):
    db = tmp_path / "archive.sqlite3"

    def forbidden():
        raise AssertionError("unexpected provider initialization")

    assert (
        collect_once(db, SYMBOL, clock=lambda: MORNING, open_http=forbidden)["state"] == "disabled"
    )
    assert not db.exists()
    seed_calendar(db, covered=False)
    assert (
        collect_once(db, SYMBOL, enabled=True, clock=lambda: MORNING, open_http=forbidden)["reason"]
        == "calendar_unknown"
    )
    assert (
        collect_once(
            db, f"{SYMBOL},{MARKET}", enabled=True, clock=lambda: MORNING, open_http=forbidden
        )["reason"]
        == "unsupported_symbol"
    )
    assert (
        collect_once(
            db, SYMBOL, enabled=True, clock=lambda: MORNING.replace(hour=15), open_http=forbidden
        )["reason"]
        == "decision_cutoff_elapsed"
    )
    assert not list(tmp_path.glob("*.collection"))


def test_exact_prior_window_two_requests_preserve_http_entity_and_deduplicate(tmp_path):
    db = tmp_path / "archive.sqlite3"
    seed_calendar(db)
    requests = []
    membership = body(members(MORNING))
    index = prices(MARKET, MORNING)
    index["response"]["data"]["item"].append(
        prices(INDUSTRY, MORNING, 99.75)["response"]["data"]["item"][0]
    )
    indexed = body(index)

    def handle(req):
        requests.append(req)
        return httpx.Response(200, content=membership if len(requests) == 1 else indexed)

    result = collect_once(db, SYMBOL, enabled=True, clock=lambda: MORNING, open_http=client(handle))
    assert result["state"] == "ready"
    assert result["requests"] == result["archivedResponses"] == 2
    assert requests[0].url.params["thscode"] == SYMBOL
    assert requests[0].url.params["type"] == "industry"
    assert requests[1].url.params["thscodes"] == f"{MARKET},{INDUSTRY}"
    assert requests[1].url.params["start"] == str(index["parameters"]["start"])
    assert requests[1].url.params["end"] == str(index["parameters"]["end"] + 86_399_999)
    assert requests[1].headers["Accept-Encoding"] == "identity"
    record = lookup(db, "indices", MARKET, MORNING).record
    assert record.provider_sha256 == hashlib.sha256(indexed).hexdigest()
    entry = next(iter(json.loads(record.body).values()))
    assert base64.b64decode(entry["responseBodyBase64"]) == indexed
    assert entry["captureLayer"] == "http-entity-body"
    assert lookup(db, "indices", MARKET, MORNING - timedelta(milliseconds=1)).state == "missing"
    replayed = replay(db, SYMBOL, MORNING)
    assert replayed[MARKET_FIELD].value == 1
    assert replayed[INDUSTRY_FIELD].value == 0
    again = collect_once(db, SYMBOL, enabled=True, clock=lambda: MORNING, open_http=client(handle))
    assert again["state"] == "deduplicated" and again["priorState"] == "ready"
    assert len(requests) == 2
    entry["responseBodyBase64"] = base64.b64encode(indexed + b" ").decode()
    with pytest.raises(ValueError, match="hash"):
        import_probe(db, *artifact({"tampered": entry}))


def test_industry_failure_still_collects_market_without_retrying(tmp_path):
    db = tmp_path / "archive.sqlite3"
    seed_calendar(db)
    requests = []

    def handle(req):
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(429)
        return httpx.Response(200, content=body(prices(MARKET, MORNING)))

    result = collect_once(db, SYMBOL, enabled=True, clock=lambda: MORNING, open_http=client(handle))
    assert result["state"] == "partial"
    assert result["sourceIssues"]["membership"] == "http_429"
    assert requests[1].url.params["thscodes"] == MARKET
    assert result["fields"][MARKET_FIELD]["value"] == 1
    assert result["fields"][INDUSTRY_FIELD]["value"] is None
    assert lookup(db, "membership", SYMBOL, MORNING).state == "missing"


@pytest.mark.parametrize(("cached", "expected"), [
    (("market",), [SYMBOL, INDUSTRY]),
    (("market", "industry"), [SYMBOL]),
    (("market", "member"), [INDUSTRY]),
    (("market", "industry", "member"), []),
])
def test_collection_requests_only_the_missing_point_in_time_inputs(tmp_path, cached, expected):
    archive = tmp_path / "archive.sqlite3"
    seed_calendar(archive)
    earlier = MORNING - timedelta(minutes=5)
    packets = {
        "market": prices(MARKET, earlier), "industry": prices(INDUSTRY, earlier, 99.75),
        "member": members(earlier),
    }
    import_probe(archive, *artifact({key: packets[key] for key in cached}))
    before = lookup(archive, "indices", MARKET, MORNING).record
    requests, opened = [], []

    def request(req):
        if "thscode" in req.url.params:
            requests.append(req.url.params["thscode"])
            return httpx.Response(200, content=body(members(MORNING)))
        codes = req.url.params["thscodes"]
        requests.append(codes)
        sample = prices(MARKET, MORNING)
        groups = {"market": MARKET, "industry": INDUSTRY}
        sample["response"]["data"]["item"] = [
            packets[key]["response"]["data"]["item"][0] for key, code in groups.items() if code in codes.split(",")
        ]
        return httpx.Response(200, content=body(sample))

    def open_http():
        opened.append(True)
        return client(request)()

    result = collect_once(archive, SYMBOL, enabled=True, clock=lambda: MORNING, open_http=open_http)
    assert result["state"] == "ready"
    assert requests == expected
    assert result["requests"] == len(expected) == result["archivedResponses"]
    assert bool(opened) == bool(expected)
    assert lookup(archive, "indices", MARKET, MORNING).record == before
    assert collect_once(archive, SYMBOL, enabled=True, clock=lambda: MORNING, open_http=open_http)["state"] == "deduplicated"


def test_late_success_is_archived_with_actual_receipt_and_no_second_request(tmp_path):
    db = tmp_path / "archive.sqlite3"
    seed_calendar(db)
    cutoff = MORNING.replace(hour=15)
    current = [cutoff - timedelta(milliseconds=10)]
    requests = []

    def handle(req):
        requests.append(req)
        current[0] = cutoff + timedelta(milliseconds=10)
        return httpx.Response(200, content=body(members(MORNING)))

    result = collect_once(
        db, SYMBOL, enabled=True, clock=lambda: current[0], open_http=client(handle)
    )
    assert result["state"] == "failed" and result["reason"] == "decision_cutoff_elapsed"
    assert result["requests"] == 1 and len(requests) == 1
    assert lookup(db, "membership", SYMBOL, cutoff).state == "missing"
    assert lookup(db, "membership", SYMBOL, current[0]).state == "known"


def test_provider_error_is_sanitized_and_scope_stays_claimed(tmp_path):
    db = tmp_path / "archive.sqlite3"
    seed_calendar(db)

    def handle(req):
        raise httpx.ReadTimeout("a private diagnostic", request=req)

    result = collect_once(db, SYMBOL, enabled=True, clock=lambda: MORNING, open_http=client(handle))
    assert result["state"] == "failed"
    assert result["requests"] == 2 and result["archivedResponses"] == 0
    assert result["sourceIssues"] == {
        "membership": "provider_unreachable",
        "indices": "provider_unreachable",
    }
    again = collect_once(db, SYMBOL, enabled=True, clock=lambda: MORNING, open_http=client(handle))
    assert again["state"] == "deduplicated" and again["priorState"] == "failed"


def test_overlapping_invocation_claims_budget_once(tmp_path):
    db = tmp_path / "archive.sqlite3"
    seed_calendar(db)
    opening = Event()
    entered = Event()

    @contextmanager
    def paused_client():
        opening.set()
        assert entered.wait(timeout=3)
        with client(lambda _: httpx.Response(503))() as value:
            yield value

    def foreground():
        return collect_once(
            db, SYMBOL, enabled=True, clock=lambda: MORNING, open_http=paused_client
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(foreground)
        try:
            assert opening.wait(timeout=3)
            parallel = workers.submit(foreground).result(timeout=3)
        finally:
            entered.set()
        finished = first.result(timeout=3)
    assert parallel == {
        "state": "deduplicated",
        "priorState": "in_progress_or_interrupted",
        "requests": 0,
    }
    assert finished["state"] == "failed" and finished["requests"] == 2


def test_window_is_rechecked_between_industry_and_index_requests(tmp_path):
    db = tmp_path / "archive.sqlite3"
    seed_calendar(db)
    closed = MORNING.replace(hour=11, minute=30)
    current = [closed - timedelta(milliseconds=1)]
    calls = []

    def handle(request):
        calls.append(request)
        current[0] = closed
        return httpx.Response(200, content=body(members(MORNING)))

    result = collect_once(
        db,
        SYMBOL,
        enabled=True,
        clock=lambda: current[0],
        open_http=client(handle),
        window_allowed=lambda stamp: stamp < closed,
    )
    assert result["sourceIssues"] == {"indices": "outside_market_window"}
    assert result["requests"] == result["archivedResponses"] == len(calls) == 1
    assert lookup(db, "membership", SYMBOL, closed).state == "known"
