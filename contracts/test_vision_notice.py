"""Regression for the accepted/revoked consent result shared by all endpoints."""
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator


class VisionNoticeContractTest(unittest.TestCase):
    def test_revocation_success_preserves_null_consent_time(self):
        schema = json.loads((Path(__file__).parent / "v2/business.schema.json").read_text(encoding="utf-8"))
        result = {"noticeVersion": "vision-v1", "consentedAt": None, "snapshotSeq": 1}
        selected = {"$defs": schema["$defs"], "$ref": "#/$defs/result_accept_vision_notice"}
        Draft202012Validator(selected).validate(result)

    def test_acceptance_and_revocation_have_valid_complete_ack_fixtures(self):
        directory = Path(__file__).parent / "v2"
        schema = json.loads((directory / "business.schema.json").read_text(encoding="utf-8"))
        fixtures = json.loads((directory / "fixtures.json").read_text(encoding="utf-8"))
        cases = {case["name"]: case["payload"] for case in fixtures["cases"]}
        for name in ("ack_accept_vision_notice", "ack_revoke_vision_notice"):
            Draft202012Validator(schema).validate(cases[name])
        self.assertIsNone(cases["ack_revoke_vision_notice"]["data"]["result"]["consentedAt"])
        self.assertIsInstance(cases["ack_accept_vision_notice"]["data"]["result"]["consentedAt"], int)


if __name__ == "__main__":
    unittest.main()
