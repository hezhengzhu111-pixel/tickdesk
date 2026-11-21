"""Drive one isolated TickDesk pairing over the real TLS relay.

Used for acceptance evidence: the script only talks to the isolated relay in
the supplied pairing file and never reads production data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import time
import uuid
from pathlib import Path

from websockets.sync.client import connect

PROTOCOL = "tickdesk.v2"


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class Peer:
    def __init__(self, pairing: dict, ca: str):
        self.pairing = pairing
        self.ca = ca
        self.incarnation = "probe_" + uuid.uuid4().hex
        self.ws = None
        self.frames = []
        self.peer = None
        self.epoch = None
        self.last_ack = None

    def __enter__(self):
        context = ssl.create_default_context(cafile=self.ca)
        self.ws = connect(
            self.pairing["relayUrl"],
            ssl=context,
            proxy=None,
            compression=None,
            subprotocols=[PROTOCOL],
            additional_headers={"Authorization": "Bearer " + self.pairing["appToken"]},
            open_timeout=10,
            close_timeout=1,
        )
        self.send({"v": 2, "type": "hello", "incarnation": self.incarnation, "serverEpoch": None})
        welcome = self.wait("welcome")
        if welcome["pairId"] != self.pairing["pairId"] or welcome["role"] != "app":
            raise AssertionError("pairing mismatch")
        presence = self.wait_presence()
        self.peer = presence["peer"]["incarnation"]
        self.epoch = presence["peer"]["serverEpoch"]
        if not presence["peer"]["online"] or not self.epoch:
            raise AssertionError("local service is not online")
        return self

    def __exit__(self, *exc):
        if self.ws:
            self.ws.close()
            self.ws = None

    def send(self, frame):
        self.ws.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))

    def receive(self, timeout=20):
        raw = self.ws.recv(timeout=timeout)
        frame = json.loads(raw)
        if frame["type"] == "deliver":
            if frame["payloadHash"] != digest(frame["payload"]):
                raise AssertionError("payload hash")
            # Persist-before-receipt is the production rule; here the probe is
            # stateless, so acknowledge immediately.
            self.send({
                key: frame[key]
                for key in ("v", "deliveryId", "payloadHash", "fromIncarnation", "toIncarnation")
            } | {"type": "receipt"})
        self.frames.append(frame)
        return frame

    def wait(self, kind=None, predicate=None, start=0, seconds=20):
        deadline = time.monotonic() + seconds
        for frame in self.frames[start:]:
            if (kind is None or frame["type"] == kind) and (predicate is None or predicate(frame)):
                return frame
        while time.monotonic() < deadline:
            frame = self.receive(max(0.05, deadline - time.monotonic()))
            if (kind is None or frame["type"] == kind) and (predicate is None or predicate(frame)):
                return frame
        raise AssertionError(f"timeout waiting for {kind}")

    def wait_presence(self):
        return self.wait("presence", lambda f: f["peer"]["online"] and f["peer"].get("serverEpoch"))

    def command(self, name, fields=None, client_id=None):
        client_id = client_id or uuid.uuid4().hex
        epoch = self.epoch
        return {
            "v": 2,
            "kind": "command",
            "id": uuid.uuid4().hex,
            "dedupeKey": "cmd:" + client_id,
            "ts": int(time.time() * 1000),
            "serverEpoch": epoch,
            "data": {"cmd": name, "clientMsgId": client_id, "serverEpoch": epoch, **(fields or {})},
        }

    def call(self, name, fields=None, client_id=None):
        ok, result, error = self.try_call(name, fields, client_id)
        if not ok:
            raise AssertionError(f"{name} rejected: {error}")
        return result

    def try_call(self, name, fields=None, client_id=None):
        payload = self.command(name, fields, client_id)
        start = len(self.frames)
        delivery = {
            "v": 2,
            "type": "deliver",
            "deliveryId": uuid.uuid4().hex,
            "fromIncarnation": self.incarnation,
            "toIncarnation": self.peer,
            "category": "business",
            "payloadHash": digest(payload),
            "payload": payload,
        }
        self.send(delivery)
        self.wait("receipt", lambda f: f["deliveryId"] == delivery["deliveryId"], start)
        ack = self.wait(
            "deliver",
            lambda f: f["payload"]["kind"] == "ack"
            and f["payload"]["data"]["clientMsgId"] == payload["data"]["clientMsgId"]
            and f["payload"]["data"]["requestHash"] == digest(payload["data"]),
            start,
        )
        data = ack["payload"]["data"]
        self.last_ack = ack
        return data["ok"], data["result"], data["error"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairing", required=True)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args()
    pairing = json.loads(Path(args.pairing).read_text(encoding="utf-8"))
    with Peer(pairing, args.ca) as peer:
        print(json.dumps({"scenario": args.scenario, "epoch": peer.epoch}, ensure_ascii=False))


if __name__ == "__main__":
    main()
