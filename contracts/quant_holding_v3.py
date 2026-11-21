"""Canonical definitions and fixtures for version 3 holding details."""
from copy import deepcopy


def ref(name):
    return {"$ref": f"#/$defs/{name}"}


def obj(fields, optional=()):
    return {
        "type": "object",
        "properties": fields,
        "required": [name for name in fields if name not in optional],
        "additionalProperties": False,
    }


def enum(*values):
    return {"enum": list(values)}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def array(schema, max_items):
    return {"type": "array", "items": schema, "maxItems": max_items}


def holding_contract(definitions, timestamp):
    qty = {"type": "integer", "minimum": 0, "maximum": 9_007_199_254_740_991}
    positive = dict(qty, minimum=1)
    hash_ = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    as_of = {"type": "string", "format": "date", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
    adjustment_arguments = deepcopy(definitions["cmd_adjust_position"]["properties"])
    for key in ("cmd", "clientMsgId", "serverEpoch", "availableQty", "pendingLots"):
        del adjustment_arguments[key]
    snapshot_id = definitions["RuleSnapshot"]["properties"]["ruleSnapshotId"]
    definitions.update(
        HoldingOriginV3=obj({
            "group": enum("daily_swing", "short_term", "long_term"),
            "strategyId": {"type": "string", "minLength": 1, "maxLength": 80},
            "codeHash": hash_, "parametersHash": hash_, "dependenciesHash": hash_,
        }),
        HoldingRestrictionV3={"oneOf": [
            obj({"kind": enum("unknown")}),
            obj({"kind": enum("freeze"), "evidenceRef": {
                "type": "string", "minLength": 1, "maxLength": 128, "pattern": r"\S",
            }}),
            obj({"kind": enum("settlement"), "acquiredOn": as_of,
                 "earliestSession": as_of, "ruleSnapshotId": snapshot_id}),
        ]},
        HoldingPartitionV3=obj({
            "qty": positive,
            "restrictions": dict(array(ref("HoldingRestrictionV3"), 4), uniqueItems=True),
            "origin": nullable(ref("HoldingOriginV3")),
        }, ("origin",)),
        HoldingDetailsV3=obj({
            "symbol": ref("Symbol"), "qty": qty, "availableQty": nullable(qty),
            "positionRevision": qty, "positionAsOf": qty, "snapshotAsOf": nullable(qty),
            "reconciliationRequired": {"type": "boolean"},
            "sellableBasis": enum("confirmed_snapshot", "local_inferred", "unknown"),
            "originStatus": enum("assigned", "unallocated", "partial", "unknown"),
            "partitions": nullable(array(ref("HoldingPartitionV3"), 32)),
        }),
    )
    partitions = array(ref("HoldingPartitionV3"), 32)
    adjustment_arguments["partitions"] = partitions
    entries = {
        "reconcile_holding": (adjustment_arguments,
            deepcopy(definitions["result_adjust_position"]["properties"]), "write", ("confirmZero",)),
        "get_holding_details": ({"symbol": ref("Symbol")}, {
            "version": enum(3), "serverEpoch": snapshot_id, "snapshotSeq": qty,
            "holding": nullable(ref("HoldingDetailsV3")),
        }, "read", ()),
    }
    partition = {"qty": 100, "restrictions": [], "origin": None}
    symbol = "300502.SZ"
    position = {
        "symbol": symbol, "name": symbol, "qty": 100, "availableQty": 100,
        "cost": "10", "pendingLots": [], "asOf": timestamp, "positionRevision": 1,
        "reconciliationRequired": False, "lastPrice": None, "priceAsOf": None,
        "priceStatus": "missing", "marketValue": None, "unrealizedPnl": None,
        "unrealizedPnlRatio": None,
    }
    samples = {
        "reconcile_holding": ({
            "symbol": symbol, "basePositionRevision": 0, "qty": 100, "cost": "10",
            "asOf": timestamp, "reason": "确认券商持仓", "resolvedExecutionIds": [],
            "partitions": [partition],
        }, {
            "adjustment": {"adjustmentId": "holding_adjustment_demo", "symbol": symbol,
                "asOf": timestamp, "before": None, "after": position, "reason": "确认券商持仓",
                "resolvedExecutionIds": [], "createdAt": timestamp},
            "positionRevision": 1, "snapshotSeq": 42,
        }),
        "get_holding_details": ({"symbol": symbol}, {
            "version": 3, "serverEpoch": "epoch_demo_a", "snapshotSeq": 42,
            "holding": {"symbol": symbol, "qty": 100, "availableQty": 100,
                "positionRevision": 1, "positionAsOf": timestamp, "snapshotAsOf": timestamp,
                "reconciliationRequired": False, "sellableBasis": "confirmed_snapshot",
                "originStatus": "unallocated", "partitions": [partition]},
        }),
    }
    negatives = [
        ("holding_partition_rejects_unknown_fields", "reconcile_holding",
         {"partitions": [dict(partition, availableQty=100)]}),
        ("holding_partition_rejects_duplicate_restrictions", "reconcile_holding",
         {"partitions": [dict(partition, restrictions=[{"kind": "unknown"}] * 2)]}),
        ("holding_freeze_requires_evidence", "reconcile_holding",
         {"partitions": [dict(partition, restrictions=[{"kind": "freeze", "evidenceRef": " "}])]}),
    ]
    return entries, samples, negatives
