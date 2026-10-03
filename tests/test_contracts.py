"""Repository validation for S-002 canonical JSON contracts (Task #84).

Fixtures are synthetic metadata only.  The validator is deliberately shared by
Device/PC consumers: schemas under contracts/ are the sole contract source.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


REPO = Path(__file__).resolve().parents[1]
CONTRACTS = REPO / "contracts"
FIXTURES = REPO / "fixtures"

SCHEMAS = {
    "device": CONTRACTS / "device.schema.json",
    "manifest": CONTRACTS / "manifest.schema.json",
    "processed-ack": CONTRACTS / "processed-ack.schema.json",
}

VALID_FIXTURES = {
    "device": {
        "device/valid/task47-device.json",
    },
    "manifest": {
        "manifest/valid/finalized.json",
        "manifest/valid/recovered.json",
        "manifest/valid/empty-recordings.json",
    },
    "processed-ack": {
        "processed-ack/valid/representative.json",
        "processed-ack/valid/empty-recording-ids.json",
    },
}

# Each invalid fixture names the contract condition it is intended to violate.
# Assert the validator keyword as well as rejection so accidental rejection for
# an unrelated reason cannot make the suite green.
INVALID_FIXTURES = {
    "device": {
        "device/invalid/missing-required.json": "required",
        "device/invalid/unsupported-schema-version.json": "const",
        "device/invalid/malformed-uuid.json": "format",
    },
    "manifest": {
        "manifest/invalid/missing-required.json": "required",
        "manifest/invalid/unsupported-schema-version.json": "const",
        "manifest/invalid/malformed-device-uuid.json": "format",
        "manifest/invalid/malformed-recording-uuid.json": "format",
        "manifest/invalid/timestamp-without-offset.json": "format",
        "manifest/invalid/uppercase-sha256.json": "pattern",
        "manifest/invalid/malformed-sha256.json": "pattern",
        "manifest/invalid/non64-sha256.json": "pattern",
        "manifest/invalid/invalid-state.json": "enum",
        "manifest/invalid/non-wav-filename.json": "pattern",
    },
    "processed-ack": {
        "processed-ack/invalid/missing-required.json": "required",
        "processed-ack/invalid/unsupported-schema-version.json": "const",
        "processed-ack/invalid/malformed-device-uuid.json": "format",
        "processed-ack/invalid/timestamp-without-offset.json": "format",
        "processed-ack/invalid/malformed-recording-id.json": "format",
    },
}


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class ContractSchemaTests(unittest.TestCase):
    @classmethod
    def validator(cls, kind: str) -> Draft202012Validator:
        schema_path = SCHEMAS[kind]
        if not schema_path.is_file():
            raise AssertionError(f"missing canonical schema: {schema_path.relative_to(REPO)}")
        schema = load_json(schema_path)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def test_canonical_schemas_exist_parse_and_are_valid_draft_2020_12(self):
        for kind, path in SCHEMAS.items():
            with self.subTest(kind=kind):
                self.assertTrue(path.is_file(), f"missing {path.relative_to(REPO)}")
                schema = load_json(path)
                self.assertEqual(
                    schema.get("$schema"),
                    "https://json-schema.org/draft/2020-12/schema",
                )
                Draft202012Validator.check_schema(schema)

    def test_required_valid_fixture_matrix_passes(self):
        for kind, relative_paths in VALID_FIXTURES.items():
            validator = self.validator(kind)
            for relative in sorted(relative_paths):
                path = FIXTURES / relative
                with self.subTest(kind=kind, fixture=relative):
                    self.assertTrue(path.is_file(), f"missing fixture: {relative}")
                    errors = sorted(validator.iter_errors(load_json(path)), key=str)
                    self.assertEqual([], errors, "\n".join(str(error) for error in errors))

    def test_required_invalid_fixture_matrix_rejects_for_intended_reason(self):
        for kind, fixtures in INVALID_FIXTURES.items():
            validator = self.validator(kind)
            for relative, expected_keyword in sorted(fixtures.items()):
                path = FIXTURES / relative
                with self.subTest(kind=kind, fixture=relative):
                    self.assertTrue(path.is_file(), f"missing fixture: {relative}")
                    errors = list(validator.iter_errors(load_json(path)))
                    self.assertTrue(errors, f"invalid fixture unexpectedly passed: {relative}")
                    keywords = {error.validator for error in errors}
                    self.assertIn(
                        expected_keyword,
                        keywords,
                        f"{relative}: expected {expected_keyword}, got {sorted(keywords)}",
                    )

    def test_task47_representative_device_and_manifest_shapes_remain_compatible(self):
        # This is a compatibility guard, not a duplicate firmware implementation.
        # Constants and serializer markers below are the current Task #47 output
        # surface; the representative canonical fixtures must validate unchanged.
        config = (REPO / "firmware/components/recorder/include/recorder_config.h").read_text(
            encoding="utf-8"
        )
        identity = (REPO / "firmware/components/recorder/device_identity.c").read_text(
            encoding="utf-8"
        )
        manifest_source = (REPO / "firmware/components/recorder/device_manifest.c").read_text(
            encoding="utf-8"
        )
        for marker in (
            "#define RECORDER_METADATA_SCHEMA_VERSION 1u",
            "#define RECORDER_SAMPLE_RATE_HZ 16000u",
            "#define RECORDER_BITS_PER_SAMPLE 16u",
            "#define RECORDER_CHANNELS 1u",
        ):
            self.assertIn(marker, config)
        for marker in ("audioCapabilities", "schemaVersion", "deviceId", "firmwareVersion"):
            self.assertIn(marker, identity)
        for marker in (
            "recordingId",
            "filename",
            "startedAt",
            "durationMs",
            "sizeBytes",
            "sha256",
            "sampleRate",
            "bitDepth",
            "channels",
            "state",
            "firmwareVersion",
        ):
            self.assertIn(marker, manifest_source)

        device = load_json(FIXTURES / "device/valid/task47-device.json")
        finalized = load_json(FIXTURES / "manifest/valid/finalized.json")
        self.validator("device").validate(device)
        self.validator("manifest").validate(finalized)
        self.assertEqual(1, device["schemaVersion"])
        self.assertEqual(
            {"sampleRate": 16000, "bitDepth": 16, "channels": 1, "format": "pcm"},
            device["audioCapabilities"],
        )
        recording = finalized["recordings"][0]
        self.assertEqual((16000, 16, 1), (
            recording["sampleRate"], recording["bitDepth"], recording["channels"]
        ))
        self.assertTrue(recording["filename"].startswith("recordings/"))
        self.assertTrue(recording["filename"].endswith(".wav"))


if __name__ == "__main__":
    unittest.main()
