"""Shared readers for frozen exchange risk-board receipts."""

import json
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

MANIFEST = "capture-evidence.json"
MANIFEST_LIMIT = 128 * 1024
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def read_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if not 0 < len(data) <= limit:
        raise ValueError("snapshot file outside expected size")
    return data


def read_json(data: bytes):
    def unique_members(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON members")
        return result

    def invalid_constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(data, object_pairs_hook=unique_members, parse_constant=invalid_constant)


def receipt(value: str) -> datetime:
    received = datetime.fromisoformat(value)
    if received.utcoffset() is None:
        raise ValueError("snapshot receipt has no time zone")
    return received.astimezone(_SHANGHAI)


def archived_manifests(archive: Path) -> dict[str, str]:
    """Freeze the dated digest index without reading exchange payloads."""
    try:
        directories = list(archive.iterdir())
    except OSError:
        return {}
    result = {}
    for snapshot_dir in directories:
        day = snapshot_dir.name
        try:
            if date.fromisoformat(day).isoformat() != day:
                continue
            body = read_bytes(snapshot_dir / MANIFEST, MANIFEST_LIMIT)
        except (OSError, ValueError):
            continue
        result[day] = sha256(body).hexdigest()
    return result
