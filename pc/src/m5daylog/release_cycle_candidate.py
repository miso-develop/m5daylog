"""#143 R3 source-only admission check for an optional Device cycle witness.

This check is deliberately NOT release authorization: #144 current-boot
qualification, #51 complete hash/SQLite sync, #50 RTC mutation, #137 pre-seal
and #89 exact-target release still require independently verified ordering.
No function here sends CDC, creates a release attempt or touches Device media.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .capacity import VerifiedSession
from .first_use_admission import CycleOrigin, parse_cycle_origin
from .release import EjectTarget, PreconditionBlocked
from .sync import _contracts_dir, _load_manifest, _preflight, inspect_device


class ExactTargetReader(Protocol):
    def revalidate_target(self, target: EjectTarget, cdc_port: str) -> None: ...
    def volume_available(self, target: EjectTarget) -> bool: ...


@dataclass(frozen=True)
class QualifiedCycleCandidate:
    """Origin-tagged read-only candidate, never release or enrollment permission."""

    device_id: str = field(repr=False)
    release_cycle_id: str = field(repr=False)
    release_cycle_origin: CycleOrigin = field(repr=False)
    binding: VerifiedSession = field(repr=False)


def _canonical(value: object) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise PreconditionBlocked("cycle evidence invalid") from None
    if normalized != value:
        raise PreconditionBlocked("cycle evidence noncanonical")
    return normalized


def _cycle_from_documents(
    status: object, device: object, manifest: object,
    prepared: object,
) -> tuple[str, str, CycleOrigin]:
    """Read-only R5 paired status and manifest candidate, not authorization."""
    if not isinstance(device, dict) or not isinstance(manifest, dict):
        raise PreconditionBlocked("Device metadata missing")
    device_id = _canonical(device.get("deviceId"))
    if manifest.get("deviceId") != device_id:
        raise PreconditionBlocked("manifest Device identity mismatch")
    if not isinstance(status, dict) or status.get("state") != "USB_SYNC":
        raise PreconditionBlocked("qualified USB_SYNC state missing")
    if not isinstance(status.get("rtcCorrectionPending"), bool):
        raise PreconditionBlocked("RTC state missing")
    # An origin-unaware/legacy cycleId-only response cannot qualify.
    origin = parse_cycle_origin(status)
    cycle = _canonical(status.get("releaseCycleId"))
    if not isinstance(prepared, list) or not prepared:
        raise PreconditionBlocked("manifest not qualified")
    matching = []
    for item in prepared:
        if (not isinstance(item, tuple) or len(item) != 2
                or not isinstance(item[0], dict)):
            raise PreconditionBlocked("manifest entries invalid")
        rec = item[0]
        if rec.get("recordingId") == cycle:
            matching.append(rec)
    if len(matching) != 1:
        raise PreconditionBlocked("cycle not uniquely manifest-bound")
    rec = matching[0]
    # A header-only WAV, partial/recovered entry or no-duration synthetic
    # manifest record must not qualify. Only #51 copy/hash verifies actual
    # source content, and only #144 can prove current-boot real PCM origin.
    if (rec.get("state") != "finalized"
            or type(rec.get("durationMs")) is not int or rec["durationMs"] <= 0
            or type(rec.get("sizeBytes")) is not int or rec["sizeBytes"] <= 44
            or rec.get("sampleRate") != 16000
            or rec.get("bitDepth") != 16
            or rec.get("channels") != 1
            or not isinstance(rec.get("sha256"), str)
            or len(rec["sha256"]) != 64):
        raise PreconditionBlocked("manifest cycle lacks finalized PCM")
    return device_id, cycle, origin


def inspect_release_cycle_candidate(
    *, session: VerifiedSession,
    port: ExactTargetReader,
    status_reader: Callable[[str], dict[str, object]],
    current_session_epoch: Callable[[], str],
    contracts_dir: Path | None = None,
) -> QualifiedCycleCandidate:
    """Read-only exact-session observation; fail closed on any stale callback.

    The caller must hold the common release coordinator OS lock. A host epoch
    is a stale-callback guard, never a replacement Device releaseCycleId.
    """
    if not isinstance(session, VerifiedSession):
        raise PreconditionBlocked("missing verified Windows session")
    t = session.target
    if (not isinstance(t, EjectTarget) or not session.cdc_port
            or not session.session_epoch
            or current_session_epoch() != session.session_epoch):
        raise PreconditionBlocked("release session expired")
    try:
        port.revalidate_target(t, session.cdc_port)
        if not port.volume_available(t):
            raise PreconditionBlocked("exact target absent")
        root = Path(t.mount_root)
        device = inspect_device(root, contracts_dir=contracts_dir)
        manifest = _load_manifest(root, _contracts_dir(contracts_dir))
        prepared = _preflight(root, device, manifest)
        status = status_reader(session.cdc_port)
        dev_id, cycle_id, origin = _cycle_from_documents(status, device, manifest, prepared)
        if dev_id != t.expected_device_id:
            raise PreconditionBlocked("release target identity mismatch")
        port.revalidate_target(t, session.cdc_port)
        if (not port.volume_available(t)
                or current_session_epoch() != session.session_epoch):
            raise PreconditionBlocked("release session changed after status")
        return QualifiedCycleCandidate(dev_id, cycle_id, origin, session)
    except PreconditionBlocked:
        raise
    except Exception:
        # Includes malformed/untrusted Device documents, source path escapes,
        # stale CDC endpoint, OS errors and provider exceptions. No path/IDs
        # are echoed into the public diagnosis.
        raise PreconditionBlocked("release cycle candidate not qualified") from None
