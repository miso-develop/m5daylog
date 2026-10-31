"""#148 offline-only dev-install admission tests: synthetic bytes; no Device I/O."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unittest

from m5daylog.dev_install_preview_148 import preview_install

DEVICE = "11111111-1111-4111-8111-111111111111"
NEW_DEVICE = "33333333-3333-4333-8333-333333333333"
RECORDING = "22222222-2222-4222-8222-222222222222"
GENERATION = "44444444-4444-4444-8444-444444444444"
ATTEMPT = "55555555-5555-4555-8555-555555555555"
PATH = "recordings/2026-10-31/000000_" + RECORDING + ".wav"
BYTES = b"synthetic-PCM-fixture-does-not-contain-real-audio"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True).encode()


def retained_files() -> dict[str, bytes]:
    return {
        "device.json": encoded({
            "schemaVersion": 1, "deviceId": DEVICE, "model": "fixture",
            "firmwareVersion": "0.0.0", "audioCapabilities": {},
        }),
        "manifest.json": encoded({
            "schemaVersion": 1, "deviceId": DEVICE,
            "updatedAt": "2026-10-31T00:00:00+09:00",
            "recordings": [{
                "recordingId": RECORDING, "filename": PATH,
                "startedAt": "2026-10-31T00:00:00+09:00",
                "durationMs": 500, "sizeBytes": len(BYTES),
                "sha256": sha(BYTES), "sampleRate": 16000,
                "bitDepth": 16, "channels": 1,
                "state": "finalized", "firmwareVersion": "0.0.0",
            }],
        }),
        PATH: BYTES,
    }


def fixture(branch: str = "R") -> dict:
    data = {
        "branch": branch,
        "target": {
            "targetBinding": "fixture-capsule", "hostBinding": "fixture-host",
            "generation": GENERATION, "attemptId": ATTEMPT,
        },
        "host": {
            "hostBinding": "fixture-host", "lineage": "NEVER_ENROLLED",
            "independentLineageEvidence": "fixture-independent-h0",
            "releaseSQLite": "ABSENT", "releaseHKCU": "ABSENT",
            "sharedHostCoordinatorBaseline": "INDEPENDENTLY_OBSERVED",
            "historyComplete": True, "earlierReleaseAttempt": False,
        },
        "approval": {
            "purpose": "DEV_FRESH_INSTALL_PREVIEW", "branch": branch,
            "targetBinding": "fixture-capsule", "hostBinding": "fixture-host",
            "generation": GENERATION, "attemptId": ATTEMPT,
            "externalEvidenceRef": "fixture-external-review",
            "intentStatus": "UNISSUED_FOR_REVIEW",
            "priorConsumedOrReplayed": False,
        },
        "custody": {
            "writerExcluded": True, "inventoryComplete": True,
            "externalCustodyEvidence": "fixture-independent-custody",
        },
        "image": {
            "sourceSha256": "a" * 64, "elfSha256": "b" * 64,
            "appSha256": "c" * 64, "bootloaderSha256": "d" * 64,
            "partitionTableSha256": "e" * 64,
            "nvsOffset": 0x9000, "nvsSize": 0x6000,
            "flashSizeBytes": 0x400000,
            "appOffset": 0x10000, "appSize": 0x200000,
            "writeRegions": [{"name": "nvs", "offset": 0x9000,
                              "size": 0x6000}],
        },
        "media": {"files": retained_files(),
                  "listedPaths": sorted(retained_files())},
        "retainedDeviceId": DEVICE,
    }
    if branch == "C":
        data["media"] = {"files": {}, "listedPaths": []}
        data["newDeviceId"] = NEW_DEVICE
        data["independentBlankEvidence"] = "fixture-clean-card"
    if branch == "A":
        original = deepcopy(data["media"])
        data["archive"] = {
            "retirementApproved": True,
            "retirementEvidence": "fixture-retirement",
            "noClobberWitness": True,
            "newBlankMediaEvidence": "fixture-new-card",
            "snapshot": original,
            "destinationEvidenceRef": "fixture-independent-archive-location",
            "verifiedInventory": {p: {"sizeBytes": len(v), "sha256": sha(v)}
                                  for p, v in original["files"].items()},
            "newMediaFiles": {},
        }
        data["newDeviceId"] = NEW_DEVICE
    return data


def set_manifest(data: dict, fn) -> None:
    files = data["media"]["files"]
    obj = json.loads(files["manifest.json"])
    fn(obj)
    files["manifest.json"] = encoded(obj)


class DevInstallPreview148Tests(unittest.TestCase):
    def ok(self, x: dict):
        result = preview_install(x)
        self.assertEqual(("CANDIDATE_PREPARED", "OFFLINE_ONLY"),
                         (result.status, result.reason))
        self.assertEqual("NONE_NOT_A_PERMIT", result.candidate["authority"])
        self.assertFalse(result.candidate["firstReleaseConsumptionImplemented"])
        self.assertEqual("NOT_RUN_NOT_AUTHORIZED",
                         result.candidate["physicalGateStatus"])
        return result.candidate

    def blocked(self, x: dict, why: str):
        result = preview_install(x)
        self.assertEqual(("BLOCKED", why), (result.status, result.reason))
        self.assertIsNone(result.candidate)

    def test_retained_settled_card_preview_deterministic_and_pure(self):
        x = fixture()
        before = deepcopy(x)
        c = self.ok(x)
        self.assertEqual(DEVICE, c["nvsDesiredStateModelOnly"]["device_id"])
        self.assertEqual(1, c["media"]["retainedRecordings"])
        self.assertEqual("FENCE_UNQUALIFIED",
                         c["nvsDesiredStateModelOnly"]["release_boot"])
        self.assertEqual("NORMAL",
                         c["nvsDesiredStateModelOnly"]["shutdown_armed"])
        self.assertEqual("UNQUALIFIED",
                         c["nvsDesiredStateModelOnly"]["rtc"])
        self.assertEqual(c, self.ok(x))
        self.assertEqual(before, x)

    def test_clean_new_card_and_archive_branches(self):
        self.assertEqual(NEW_DEVICE,
                         self.ok(fixture("C"))["nvsDesiredStateModelOnly"]["device_id"])
        a = self.ok(fixture("A"))
        self.assertEqual(3, a["media"]["archivedFiles"])
        self.assertEqual(NEW_DEVICE, a["nvsDesiredStateModelOnly"]["device_id"])

    def test_denies_invalid_branch_or_input(self):
        x = fixture()
        x["branch"] = "OTA"
        self.blocked(x, "BLOCKED_BRANCH")
        self.blocked(None, "BLOCKED_INPUT")

    def test_denies_unqualified_host_history_and_absence_only(self):
        for path, value in (
            ("lineage", "UNKNOWN"), ("lineage", "PREVIOUSLY_ENROLLED"),
            ("independentLineageEvidence", ""),
            ("releaseSQLite", "PRESENT"), ("releaseHKCU", "PRESENT"),
            ("releaseSQLite", "MISSING_AFTER_ENROLLED"),
            ("releaseHKCU", "CORRUPTED"),
            ("hostBinding", "other-host"),
            ("sharedHostCoordinatorBaseline", "SELF_REPORTED"),
            ("historyComplete", False), ("earlierReleaseAttempt", True),
        ):
            with self.subTest(field=path, value=value):
                x = fixture()
                x["host"][path] = value
                self.blocked(x, "BLOCKED_HOST_H0")

    def test_denies_bad_custody_and_permission(self):
        for field, val in (
            ("writerExcluded", False), ("inventoryComplete", False),
            ("externalCustodyEvidence", ""),
        ):
            x = fixture()
            x["custody"][field] = val
            self.blocked(x, "BLOCKED_CUSTODY")
        for field, val in (
            ("purpose", "RELEASE_STORAGE"), ("branch", "C"),
            ("attemptId", "other-attempt"),
            ("externalEvidenceRef", ""),
            ("targetBinding", "wrong-device"),
            ("generation", "77777777-7777-4777-8777-777777777777"),
            ("intentStatus", "CONSUMED"), ("priorConsumedOrReplayed", True),
        ):
            x = fixture()
            x["approval"][field] = val
            self.blocked(x, "BLOCKED_PERMISSION")

    def test_denies_unpinned_or_wrong_nvs_write_set(self):
        for key, val, code in (
            ("appSha256", "wrong", "BLOCKED_IMAGE"),
            ("elfSha256", "", "BLOCKED_IMAGE"),
            ("nvsOffset", 0x8100, "BLOCKED_PARTITION"),
            ("nvsSize", 0, "BLOCKED_PARTITION"),
            ("nvsSize", -4096, "BLOCKED_PARTITION"),
            ("appOffset", 0x9000, "BLOCKED_PARTITION"),
            ("appSize", 0x400000, "BLOCKED_PARTITION"),
            ("flashSizeBytes", 0, "BLOCKED_PARTITION"),
            ("writeRegions", [{"name": "flash", "offset": 0,
                                "size": 0x100000}], "BLOCKED_WRITE_SET"),
            ("writeRegions", [{"name": "nvs", "offset": 0x9000,
                                "size": 0x5000}], "BLOCKED_WRITE_SET"),
        ):
            with self.subTest(key=key):
                x = fixture()
                x["image"][key] = val
                self.blocked(x, code)

    def test_retain_rejects_identity_and_manifest_contradictions(self):
        x = fixture()
        x["retainedDeviceId"] = NEW_DEVICE
        self.blocked(x, "BLOCKED_MEDIA_IDENTITY")
        x = fixture()
        x["newDeviceId"] = NEW_DEVICE
        self.blocked(x, "BLOCKED_MEDIA_IDENTITY")
        x = fixture()
        set_manifest(x, lambda m: m.update(deviceId=NEW_DEVICE))
        self.blocked(x, "BLOCKED_MEDIA_IDENTITY")

    def test_retain_accepts_complete_auxiliary_inventory_without_mutation(self):
        x = fixture()
        x["media"]["files"]["events/first-session.jsonl"] = b"synthetic-event"
        x["media"]["listedPaths"].append("events/first-session.jsonl")
        before = deepcopy(x)
        digests = self.ok(x)["media"]["retainedDigests"]
        self.assertEqual(sha(b"synthetic-event"), digests["events/first-session.jsonl"])
        self.assertEqual(before, x)

    def test_retain_rejects_contradictory_host_or_nvs_id(self):
        for field in ("observedNvsDeviceId", "observedHostDeviceId"):
            x = fixture()
            x[field] = NEW_DEVICE
            self.blocked(x, "BLOCKED_MEDIA_IDENTITY")

    def test_retain_rejects_bytes_size_and_hash(self):
        x = fixture()
        x["media"]["files"][PATH] = BYTES + b"damage"
        self.blocked(x, "BLOCKED_MEDIA_HASH")
        x = fixture()
        set_manifest(x, lambda m: m["recordings"][0].update(sha256="0" * 64))
        self.blocked(x, "BLOCKED_MEDIA_HASH")

    def test_retain_rejects_orphan_and_unsettled_artifacts(self):
        for extra, code in (
            ("recordings/orphan.wav", "BLOCKED_MEDIA_EXTRA_FILE"),
            ("manifest.json.tmp", "BLOCKED_MEDIA_UNSETTLED"),
            ("manifest.json.bak", "BLOCKED_MEDIA_UNSETTLED"),
            ("recordings/orphan.wav.part", "BLOCKED_MEDIA_UNSETTLED"),
            ("recordings/temp.partial", "BLOCKED_MEDIA_UNSETTLED"),
        ):
            with self.subTest(extra=extra):
                x = fixture()
                x["media"]["files"][extra] = b"old"
                x["media"]["listedPaths"].append(extra)
                self.blocked(x, code)

    def test_retain_rejects_duplicate_ids_and_missing_wav(self):
        x = fixture()
        set_manifest(x, lambda m: m["recordings"].append(
            deepcopy(m["recordings"][0])))
        self.blocked(x, "BLOCKED_MEDIA_CONFLICT")
        x = fixture()
        del x["media"]["files"][PATH]
        x["media"]["listedPaths"].remove(PATH)
        self.blocked(x, "BLOCKED_MEDIA_MISSING_WAV")

    def test_retain_rejects_bad_path_and_unlisted_inventory(self):
        for filename in (
            "../external.wav", "recordings/../outside.wav",
            r"C:\outside.wav", "recordings//double.wav",
        ):
            with self.subTest(filename=filename):
                x = fixture()
                set_manifest(x, lambda m: m["recordings"][0].update(filename=filename))
                self.blocked(x, "BLOCKED_MEDIA_CONFLICT")
        x = fixture()
        x["media"]["listedPaths"].remove(PATH)
        self.blocked(x, "BLOCKED_MEDIA_INVENTORY")

    def test_retain_rejects_duplicate_json_and_zero_pcm_stale_metadata(self):
        x = fixture()
        x["media"]["files"]["manifest.json"] = (
            b'{"schemaVersion":1,"schemaVersion":1}')
        self.blocked(x, "BLOCKED_MEDIA_DUPLICATE_JSON")
        x = fixture()
        set_manifest(x, lambda m: m["recordings"][0].update(state="pending"))
        self.blocked(x, "BLOCKED_MEDIA_METADATA")

    def test_clean_card_cannot_be_assumed_from_missing_manifest(self):
        x = fixture("C")
        x["independentBlankEvidence"] = ""
        self.blocked(x, "BLOCKED_MEDIA_NOT_BLANK")
        x = fixture("C")
        x["media"]["files"]["some.bin"] = b"leftover"
        x["media"]["listedPaths"].append("some.bin")
        self.blocked(x, "BLOCKED_MEDIA_NOT_BLANK")

    def test_archive_requires_full_no_clobber_and_independent_retirement(self):
        for field, value, reason in (
            ("retirementApproved", False, "BLOCKED_ARCHIVE_APPROVAL"),
            ("retirementEvidence", "", "BLOCKED_ARCHIVE_APPROVAL"),
            ("noClobberWitness", False, "BLOCKED_ARCHIVE_APPROVAL"),
            ("newBlankMediaEvidence", "", "BLOCKED_ARCHIVE_APPROVAL"),
            ("newMediaFiles", {"old": b"data"}, "BLOCKED_MEDIA_NOT_BLANK"),
            ("destinationEvidenceRef", "", "BLOCKED_ARCHIVE_INCOMPLETE"),
            ("verifiedInventory", {}, "BLOCKED_ARCHIVE_INCOMPLETE"),
        ):
            x = fixture("A")
            x["archive"][field] = value
            self.blocked(x, reason)
        x = fixture("A")
        del x["archive"]["snapshot"]["files"][PATH]
        x["archive"]["snapshot"]["listedPaths"].remove(PATH)
        self.blocked(x, "BLOCKED_ARCHIVE_INCOMPLETE")
        x = fixture("A")
        x["archive"]["snapshot"]["files"][PATH] = BYTES + b"mutation"
        self.blocked(x, "BLOCKED_ARCHIVE_HASH")
        x = fixture("A")
        x["archive"]["verifiedInventory"][PATH]["sha256"] = "0" * 64
        self.blocked(x, "BLOCKED_ARCHIVE_HASH")

    def test_pinned_geometry_and_disposition(self):
        for branch in ("C", "R", "A"):
            cand = self.ok(fixture(branch))
            self.assertEqual(ATTEMPT, cand["firstSupervisedReleaseAttemptId"])
            self.assertEqual(0x10000, cand["image"]["appOffset"])
            self.assertIn("originalDataDisposition", cand)
            self.assertEqual("NONE_NOT_A_PERMIT", cand["authority"])

    def test_receipt_candidate_binding_replay_and_indeterminate_fields(self):
        x = fixture()
        original = self.ok(x)["candidateDigestSha256"]
        x["target"]["attemptId"] = (
            "66666666-6666-4666-8666-666666666666"
        )
        self.blocked(x, "BLOCKED_PERMISSION")
        x["approval"]["attemptId"] = x["target"]["attemptId"]
        changed = self.ok(x)["candidateDigestSha256"]
        self.assertNotEqual(original, changed)
        x["target"]["generation"] = None
        self.blocked(x, "BLOCKED_TARGET")

    def test_invalid_data_fails_closed_without_exception_or_mutation(self):
        x = fixture()
        x["media"]["files"]["device.json"] = b"\xff"
        self.blocked(x, "BLOCKED_INVALID_EVIDENCE")
        x = fixture()
        x["media"]["files"]["manifest.json"] = b"not-json"
        self.blocked(x, "BLOCKED_INVALID_EVIDENCE")
        x = fixture()
        x["media"]["files"][PATH] = "not-bytes"
        self.blocked(x, "BLOCKED_MEDIA_PATH")
        x = fixture()
        x["media"]["listedPaths"].append("manifest.json")
        self.blocked(x, "BLOCKED_MEDIA_INVENTORY")


if __name__ == "__main__":
    unittest.main()
