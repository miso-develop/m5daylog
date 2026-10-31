"""#148: offline-only, non-authoritative DEV_FRESH_INSTALL preview.

No filesystem, registry, USB, serial, NVS, process, or release operations are
implemented here. All media and host observations are caller-supplied fixtures.
A CANDIDATE_PREPARED result is NEVER a Human Gate permit or release authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping
from uuid import UUID, uuid5


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REQUIRED_ENTRY = (
    "recordingId", "filename", "startedAt", "durationMs", "sizeBytes",
    "sha256", "sampleRate", "bitDepth", "channels", "state", "firmwareVersion",
)


class _Denied(ValueError):
    def __init__(self, code: str):
        self.code = code


@dataclass(frozen=True)
class PreviewResult:
    status: str
    reason: str
    candidate: dict[str, Any] | None = None


def _require(test: bool, reason: str) -> None:
    if not test:
        raise _Denied(reason)


def _ref(value: Any) -> bool:
    return isinstance(value, str) and bool(_SAFE_REF.fullmatch(value))


def _uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not re.search(r"(Z|[+-]\d{2}:\d{2})$", value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def _path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return False
    parts = PurePosixPath(value).parts
    return value == str(PurePosixPath(value)) and all(
        part not in ("", ".", "..") for part in parts
    ) and not value.startswith("/")


def _json_document(data: bytes) -> dict[str, Any]:
    def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, val in items:
            if key in result:
                raise _Denied("BLOCKED_MEDIA_DUPLICATE_JSON")
            result[key] = val
        return result

    obj = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    _require(type(obj) is dict, "BLOCKED_MEDIA_METADATA")
    return obj


def _media_bytes(media: Any) -> dict[str, bytes]:
    _require(type(media) is dict, "BLOCKED_MEDIA_INVENTORY")
    files = media.get("files")
    listed = media.get("listedPaths")
    _require(type(files) is dict and type(listed) is list,
             "BLOCKED_MEDIA_INVENTORY")
    _require(all(_path(p) and type(v) is bytes for p, v in files.items()),
             "BLOCKED_MEDIA_PATH")
    _require(all(_path(p) for p in listed) and len(listed) == len(set(listed)),
             "BLOCKED_MEDIA_INVENTORY")
    _require(set(listed) == set(files), "BLOCKED_MEDIA_INVENTORY")
    return files


def _settled_retained(files: dict[str, bytes], expected_id: str) -> dict[str, Any]:
    _require("device.json" in files and "manifest.json" in files,
             "BLOCKED_MEDIA_METADATA")
    # Case aliases can refer to the same file on FAT/Windows media. A
    # "complete" offline inventory must not quietly admit two aliases.
    _require(len({p.casefold() for p in files}) == len(files),
             "BLOCKED_MEDIA_CONFLICT")
    # Admission cannot invoke manifest recovery or operate on transaction scraps.
    # Deny pending files regardless of filename case, including auxiliary metadata.
    _require(not any(
        any(part.casefold().endswith(
                (".bak", ".tmp", ".part", ".partial", ".new", ".txn"))
            or part.casefold().startswith(".part") for part in p.split("/"))
        for p in files
    ), "BLOCKED_MEDIA_UNSETTLED")
    device = _json_document(files["device.json"])
    manifest = _json_document(files["manifest.json"])
    # Import the unmodified, Integration-owned canonical schema validator.
    # These are source-contract reads, never Device/host media inspection.
    from .sync import ContractValidationError, validate_contract_document
    # Pin the repository-owned schema. Never follow a host environment override
    # that could point at a mounted SD/MSC target or arbitrary private file.
    contracts = Path(__file__).resolve().parents[3] / "contracts"
    try:
        validate_contract_document(device, contracts / "device.schema.json")
        validate_contract_document(manifest, contracts / "manifest.schema.json")
    except ContractValidationError:
        raise _Denied("BLOCKED_MEDIA_METADATA") from None
    _require(device.get("schemaVersion") == 1 and
             manifest.get("schemaVersion") == 1 and
             type(device.get("audioCapabilities")) is dict and
             isinstance(device.get("model"), str) and bool(device["model"]) and
             isinstance(device.get("firmwareVersion"), str) and
             bool(device["firmwareVersion"]), "BLOCKED_MEDIA_METADATA")
    _require(_uuid(device.get("deviceId")) and
             device["deviceId"] == manifest.get("deviceId") == expected_id,
             "BLOCKED_MEDIA_IDENTITY")
    entries = manifest.get("recordings")
    _require(type(entries) is list and _timestamp(manifest.get("updatedAt")),
             "BLOCKED_MEDIA_METADATA")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for entry in entries:
        _require(type(entry) is dict and
                 all(key in entry for key in _REQUIRED_ENTRY),
                 "BLOCKED_MEDIA_METADATA")
        rid, filename = entry["recordingId"], entry["filename"]
        _require(_uuid(rid) and rid not in seen_ids and
                 _path(filename) and filename.startswith("recordings/") and
                 filename.endswith(".wav") and filename not in seen_paths,
                 "BLOCKED_MEDIA_CONFLICT")
        _require(_timestamp(entry["startedAt"]) and
                 entry["state"] in ("finalized", "recovered") and
                 isinstance(entry["firmwareVersion"], str) and
                 bool(entry["firmwareVersion"]) and
                 all(type(entry[key]) is int and entry[key] >= 0
                     for key in ("durationMs", "sizeBytes")) and
                 all(type(entry[key]) is int and entry[key] > 0
                     for key in ("sampleRate", "bitDepth", "channels")) and
                 _digest(entry["sha256"]), "BLOCKED_MEDIA_METADATA")
        _require(filename in files, "BLOCKED_MEDIA_MISSING_WAV")
        raw = files[filename]
        _require(len(raw) == entry["sizeBytes"] and
                 hashlib.sha256(raw).hexdigest() == entry["sha256"],
                 "BLOCKED_MEDIA_HASH")
        seen_ids.add(rid)
        seen_paths.add(filename)
    # Other fully inventoried historical support files must remain byte-for-byte
    # untouched; a stray WAV or recording-directory artifact is never settled.
    extras = set(files) - seen_paths - {"device.json", "manifest.json"}
    _require(not any(p.casefold().endswith(".wav") or
                     p.casefold().startswith("recordings/") or
                     p.casefold() in ("device.json", "manifest.json")
                     for p in extras), "BLOCKED_MEDIA_EXTRA_FILE")
    return {"retainedDeviceId": expected_id, "retainedRecordings": len(seen_ids),
            "retainedDigests": {p: hashlib.sha256(files[p]).hexdigest()
                                for p in sorted(files)}}


def _image(image: Any) -> dict[str, Any]:
    _require(type(image) is dict, "BLOCKED_IMAGE")
    exact_fields = {
        "sourceSha256", "elfSha256", "appSha256", "bootloaderSha256",
        "partitionTableSha256", "flashSizeBytes", "appOffset", "appSize",
        "nvsOffset", "nvsSize", "writeRegions",
    }
    _require(set(image) == exact_fields, "BLOCKED_IMAGE")
    for field in ("sourceSha256", "elfSha256", "appSha256",
                  "bootloaderSha256", "partitionTableSha256"):
        _require(_digest(image.get(field)), "BLOCKED_IMAGE")
    offset, size = image.get("nvsOffset"), image.get("nvsSize")
    flash, app_offset, app_size = (image.get("flashSizeBytes"),
                                   image.get("appOffset"), image.get("appSize"))
    _require(all(type(v) is int for v in
                 (offset, size, flash, app_offset, app_size)) and
             0x100000 <= flash <= 0x1000000 and
             0x9000 <= offset and size >= 0x1000 and
             size % 0x1000 == 0 and offset % 0x1000 == 0 and
             app_offset >= 0x10000 and app_offset % 0x10000 == 0 and
             app_size >= 0x1000 and app_size % 0x1000 == 0 and
             offset + size <= flash and app_offset + app_size <= flash and
             (offset + size <= app_offset or app_offset + app_size <= offset),
             "BLOCKED_PARTITION")
    regions = image.get("writeRegions")
    _require(type(regions) is list and
             regions == [{"name": "nvs", "offset": offset, "size": size}],
             "BLOCKED_WRITE_SET")
    # Rebuild the one authorized write region from validated scalar values.
    # Do not leak the caller's nested list/dict into the pinned candidate.
    plan = {k: image[k] for k in
            ("sourceSha256", "elfSha256", "appSha256",
             "bootloaderSha256", "partitionTableSha256",
             "flashSizeBytes", "appOffset", "appSize",
             "nvsOffset", "nvsSize")}
    plan["writeRegions"] = [{"name": "nvs", "offset": offset, "size": size}]
    return plan


def _image_plan_digest(image: dict[str, Any]) -> str:
    """Offline comparison pin. No signature, authentication or live evidence."""
    canonical = json.dumps(image, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _fresh_device_id(branch: str, target: Mapping[str, Any]) -> str:
    """Deterministic *candidate* from a separately issued random v4 generation.

    This cannot establish real-world freshness, uniqueness or enrollment state.
    An independently attested generation and full prior-ID inventory are required.
    """
    return str(uuid5(
        UUID(target["generation"]),
        f"m5daylog:dev-fresh-install:v1:{branch}:"
        f"{target['targetBinding']}:{target['hostBinding']}",
    ))


def _candidate(input_data: Mapping[str, Any]) -> dict[str, Any]:
    _require(type(input_data) is dict, "BLOCKED_INPUT")
    branch = input_data.get("branch")
    _require(branch in ("C", "R", "A"), "BLOCKED_BRANCH")
    target, host, approval = (
        input_data.get("target"), input_data.get("host"),
        input_data.get("approval"),
    )
    _require(type(target) is dict and
             all(_ref(target.get(k)) for k in
                 ("targetBinding", "hostBinding", "generation", "attemptId")) and
             _uuid(target["generation"]) and _uuid(target["attemptId"]) and
             target["generation"] != target["attemptId"],
             "BLOCKED_TARGET")
    _require(type(host) is dict and
             host.get("lineage") == "NEVER_ENROLLED" and
             _ref(host.get("independentLineageEvidence")) and
             host.get("releaseSQLite") == "ABSENT" and
             host.get("releaseHKCU") == "ABSENT" and
             host.get("hostBinding") == target["hostBinding"] and
             host.get("sharedHostCoordinatorBaseline") == "INDEPENDENTLY_OBSERVED" and
             host.get("historyComplete") is True and
             host.get("earlierReleaseAttempt") is False,
             "BLOCKED_HOST_H0")
    _require(type(approval) is dict and
             approval.get("purpose") == "DEV_FRESH_INSTALL_PREVIEW" and
             approval.get("branch") == branch and
             approval.get("targetBinding") == target["targetBinding"] and
             approval.get("hostBinding") == target["hostBinding"] and
             approval.get("generation") == target["generation"] and
             approval.get("attemptId") == target["attemptId"] and
             _ref(approval.get("externalEvidenceRef")) and
             approval.get("intentStatus") == "UNISSUED_FOR_REVIEW" and
             approval.get("priorConsumedOrReplayed") is False,
             "BLOCKED_PERMISSION")
    custody = input_data.get("custody")
    _require(type(custody) is dict and
             custody.get("writerExcluded") is True and
             custody.get("inventoryComplete") is True and
             _ref(custody.get("externalCustodyEvidence")),
             "BLOCKED_CUSTODY")
    image = _image(input_data.get("image"))
    _require(_digest(approval.get("imagePlanSha256")) and
             approval["imagePlanSha256"] == _image_plan_digest(image),
             "BLOCKED_PERMISSION_IMAGE")
    files = _media_bytes(input_data.get("media"))
    media_state: dict[str, Any]
    if branch == "R":
        device_id = input_data.get("retainedDeviceId")
        _require(_uuid(device_id), "BLOCKED_MEDIA_IDENTITY")
        media_state = _settled_retained(files, device_id)
        _require(input_data.get("observedNvsDeviceId") in (None, device_id) and
                 input_data.get("observedHostDeviceId") in (None, device_id),
                 "BLOCKED_MEDIA_IDENTITY")
        _require(input_data.get("newDeviceId") in (None, device_id),
                 "BLOCKED_MEDIA_IDENTITY")
    else:
        # C and A select an independently fresh generation for new media.
        # R never enters this path and never generates a new identity.
        fresh = input_data.get("freshIdentity")
        _require(type(fresh) is dict and
                 _ref(fresh.get("independentGenerationEvidence")) and
                 fresh.get("priorIdentityInventoryComplete") is True and
                 type(fresh.get("knownPriorDeviceIds")) is list and
                 all(_uuid(value) for value in fresh["knownPriorDeviceIds"]) and
                 len(fresh["knownPriorDeviceIds"]) ==
                 len(set(fresh["knownPriorDeviceIds"])) and
                 UUID(target["generation"]).version == 4,
                 "BLOCKED_FRESH_IDENTITY")
        device_id = _fresh_device_id(branch, target)
        _require(device_id not in fresh["knownPriorDeviceIds"] and
                 input_data.get("newDeviceId") in (None, device_id) and
                 input_data.get("retainedDeviceId") != device_id and
                 input_data.get("observedNvsDeviceId") != device_id and
                 input_data.get("observedHostDeviceId") != device_id,
                 "BLOCKED_MEDIA_IDENTITY")
        if branch == "C":
            _require(not files and _ref(input_data.get("independentBlankEvidence")),
                     "BLOCKED_MEDIA_NOT_BLANK")
            media_state = {"independentlyBlankCandidate": True}
        else:
            archive = input_data.get("archive")
            _require(type(archive) is dict and archive.get("retirementApproved") is True
                     and _ref(archive.get("retirementEvidence")) and
                     archive.get("noClobberWitness") is True and
                     _ref(archive.get("newBlankMediaEvidence")),
                     "BLOCKED_ARCHIVE_APPROVAL")
            archived = _media_bytes(archive.get("snapshot"))
            _require(bool(files) and files.keys() == archived.keys(),
                     "BLOCKED_ARCHIVE_INCOMPLETE")
            verified = archive.get("verifiedInventory")
            _require(type(verified) is dict and set(verified) == set(files) and
                     _ref(archive.get("destinationEvidenceRef")),
                     "BLOCKED_ARCHIVE_INCOMPLETE")
            _require(all(
                type(verified[p]) is dict and
                verified[p].get("sizeBytes") == len(files[p]) and
                verified[p].get("sha256") == hashlib.sha256(files[p]).hexdigest()
                and len(archived[p]) == len(files[p])
                and hashlib.sha256(archived[p]).digest() ==
                    hashlib.sha256(files[p]).digest()
                for p in files
            ), "BLOCKED_ARCHIVE_HASH")
            _require(archive.get("newMediaFiles") == {},
                     "BLOCKED_MEDIA_NOT_BLANK")
            media_state = {
                "archivedFiles": len(files),
                "archivedDigests": {p: hashlib.sha256(files[p]).hexdigest()
                                    for p in sorted(files)},
            }
    _require(approval.get("deviceId") == device_id, "BLOCKED_PERMISSION_IDENTITY")
    candidate = {
        "kind": "DEV_FRESH_INSTALL_OFFLINE_CANDIDATE",
        "authority": "NONE_NOT_A_PERMIT",
        "branch": branch,
        "target": {k: target[k] for k in
                   ("targetBinding", "hostBinding", "generation", "attemptId")},
        "image": image,
        "imagePlanSha256": _image_plan_digest(image),
        "identityProvenanceModelOnly": (
            "RETAIN_VERIFIED_SD_UUID" if branch == "R"
            else "UUID5_FROM_EXTERNALLY_ATTESTED_V4_GENERATION_CANDIDATE"
        ),
        "freshGenerationEvidenceRef": (
            None if branch == "R" else fresh["independentGenerationEvidence"]
        ),
        "nvsDesiredStateModelOnly": {
            "device_id": device_id,
            "release_boot": "FENCE_UNQUALIFIED",
            "shutdown_armed": "NORMAL",
            "rtc": "UNQUALIFIED",
        },
        "media": media_state,
        "originalDataDisposition": {
            "C": "INDEPENDENTLY_BLANK_NEW_MEDIA",
            "R": "RETAIN_ALL_PRIOR_BYTES_NO_REPAIR",
            "A": "SEPARATELY_ARCHIVED_RETIREMENT_REQUIRES_HUMAN_GATE",
        }[branch],
        "firstSupervisedReleaseAttemptId": target["attemptId"],
        "approvalEvidenceRef": approval["externalEvidenceRef"],
        "hostLineageEvidenceRef": host["independentLineageEvidence"],
        "custodyEvidenceRef": custody["externalCustodyEvidence"],
        "firstReleaseIntent": "UNSPENT_CANDIDATE_ONLY",
        "firstReleaseConsumptionImplemented": False,
        "firstEnrollmentImplemented": False,
        "physicalGateStatus": "NOT_RUN_NOT_AUTHORIZED",
    }
    # Digest is a comparison fingerprint, not a signature or authorization.
    encoded = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
    candidate["candidateDigestSha256"] = hashlib.sha256(encoded).hexdigest()
    return candidate


def preview_install(snapshot: Mapping[str, Any]) -> PreviewResult:
    """Pure in-memory, negative-first candidate preview. Never mutates inputs."""
    try:
        return PreviewResult("CANDIDATE_PREPARED", "OFFLINE_ONLY",
                             _candidate(snapshot))
    except _Denied as exc:
        return PreviewResult("BLOCKED", exc.code)
    except (TypeError, ValueError, KeyError, UnicodeError, OverflowError,
            RecursionError):
        return PreviewResult("BLOCKED", "BLOCKED_INVALID_EVIDENCE")
