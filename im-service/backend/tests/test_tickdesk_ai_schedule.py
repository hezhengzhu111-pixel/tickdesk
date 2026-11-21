from app.services.tickdesk_ai_schedule import decide


def test_repeated_scan_ids_and_fetch_times_do_not_trigger_another_report():
    evidence = {
        "serverEpoch": "epoch", "business": {"controlVersion": 1},
        "quant": {"scanId": "scan-0", "batch": {"observations": [
            {"symbol": "600000.SH", "group": "daily_swing", "inputHash": "data",
             "bar": {"status": "complete", "endMs": 10}, "score": {"value": "0"}},
        ]}},
    }
    facts = [{"factId": "version1", "categories": ["companyRisk"], "fetchedAt": 10}]
    first = decide(evidence, facts, "model", previous=None, now=100)
    assert first["trigger"] == "initial"
    previous = {**first, "state": "completed", "validUntil": 1000, "errorCode": None}
    for scan in range(12):
        evidence["quant"]["scanId"] = f"scan-{scan}"
        facts[0]["fetchedAt"] = 20 + scan
        assert decide(evidence, facts, "model", previous=previous, now=101 + scan) is None
    facts[0]["factId"] = "version2"
    second = decide(evidence, facts, "model", previous=previous, now=200)
    assert second["trigger"] == "risk"
    assert second["fingerprint"] != first["fingerprint"]


def test_only_relevant_changes_and_verified_market_revisions_trigger_report():
    evidence = {"serverEpoch": "epoch", "business": {
        "positionRevision": 1, "controlVersion": 1,
        "holdingDetails": [{"symbol": "600000.SH", "positionRevision": 9, "qty": 100}],
    }}
    market = [{"source": {"ref": "local:financial:revision1"}, "record": {"periodEnd": "2025-09-30"}}]
    initial = decide(evidence, [], "model", previous=None, now=100, market=market)
    previous = {**initial, "state": "completed", "validUntil": 1_000, "errorCode": None}
    evidence["business"]["positionRevision"] += 1
    assert decide(evidence, [], "model", previous=previous, now=101, market=market) is None
    market[0]["source"]["ref"] = "local:financial:revision2"
    assert decide(evidence, [], "model", previous=previous, now=102, market=market)["trigger"] == "changed"


def test_published_score_changes_trigger_but_scan_and_read_times_do_not():
    candidate = {"taskId": "task1", "symbol": "300502.SZ", "direction": "buy",
                 "score": "88", "strategies": ["a", "b"], "dataAsOf": 89}
    production = {"scanId": "scan1", "finishedAt": 90, "status": "succeeded", "reason": None,
                  "candidates": [candidate]}
    evidence = {"asOf": 100, "serverEpoch": "epoch", "business": {},
                "quant": {"serverEpoch": "epoch", "production": production}}
    first = decide(evidence, [], "model", previous=None, now=100)
    assert first["trigger"] == "initial"
    previous = {**first, "state": "completed", "validUntil": 1000, "errorCode": None}
    production.update(scanId="scan2", finishedAt=98)
    candidate.update(taskId="task2", dataAsOf=97)
    assert decide(evidence, [], "model", previous=previous, now=101) is None
    candidate["score"] = "89"
    assert decide(evidence, [], "model", previous=previous, now=102)["trigger"] == "changed"
