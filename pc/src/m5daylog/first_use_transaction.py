"""#143 R5-FU initial host-enrollment transaction, source-only / NOT wired.

The *sole* permitted positive case models a separately supervised, #119-bound
first-use receipt already provisioned by an independent trusted workflow.
Neither this file nor an opt-in monitor can issue that receipt or establish its
Device/physical/Human provenance. No CLI, watcher, or CDC sender invokes this.

The transaction holds the existing R3 cooperating OS lock while consuming a
host-local one-use intent, committing the first (device, cycle, attempt) tuple
in the initial SQLite transaction, flushing the independent anchor, and
checking the exact consumed tuple. Every ambiguous intermediate state blocks.
Actual Windows persistence and live Device safety are NOT qualified here.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from .first_use_admission import (
    HostHistory, InitialBootObservation, InitialIntent, MaintenanceReceipt,
    _opaque, _uuid, inspect_first_use_admission,
)
from .release import PreconditionBlocked
from .release_cycle_fence import CycleAttemptFence


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FileFirstUseIntent:
    """Unwired host intent *consumer*. Issuance is intentionally absent.

    The pre-existing receipt is not authentication. A future supervised
    issuer must durably provide the positive #119/actual-handler/Human proof.
    File fsync + rename is a source-only transaction prototype, not verified
    Windows power-loss durability or anti-rollback protection.
    """

    def __init__(self, path: Path, *, device_root: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        root = Path(device_root).expanduser().resolve()
        if self.path == root or root in self.path.parents:
            raise PreconditionBlocked("first-use intent cannot be on Device media")
        self.pending = self.path.with_name(self.path.name + ".pending")

    @staticmethod
    def expected(intent: InitialIntent, *, state: str) -> dict[str, object]:
        """No identifiers or raw receipts in this user-local serialized model."""
        if type(intent) is not InitialIntent or state not in ("UNCONSUMED", "CONSUMED"):
            raise PreconditionBlocked("first-use intent not qualified")
        return {
            "v": 1,
            "state": state,
            "device": _digest(_uuid(intent.device_id)),
            "physical_parent": _digest(_opaque(intent.physical_parent)),
            "session": _digest(_opaque(intent.maintenance_session)),
            "maintenance_attempt": _digest(_uuid(intent.maintenance_attempt_id)),
        }

    def read(self) -> Mapping[str, object]:
        """Require a complete receipt and no evidence of a partial replacement."""
        try:
            if self.pending.exists():
                raise PreconditionBlocked("first-use intent mutation uncertain")
            raw = self.path.read_bytes()
            if len(raw) > 2048:
                raise PreconditionBlocked("first-use intent oversized")
            state = json.loads(raw.decode("utf-8"))
            if (type(state) is not dict
                    or set(state) != {"v", "state", "device", "physical_parent",
                                      "session", "maintenance_attempt"}
                    or type(state["v"]) is not int or state["v"] != 1
                    or state["state"] not in ("UNCONSUMED", "CONSUMED")
                    or any(type(state[key]) is not str
                           or len(state[key]) != 64
                           or any(ch not in "0123456789abcdef" for ch in state[key])
                           for key in ("device", "physical_parent", "session",
                                       "maintenance_attempt"))):
                raise PreconditionBlocked("first-use intent invalid")
            return state
        except (OSError, UnicodeError, ValueError, TypeError):
            raise PreconditionBlocked("first-use intent unreadable") from None

    def consume(self, intent: InitialIntent) -> None:
        """Burn exactly one previously issued receipt; no recovery or reissue.

        The parent lock and independent host enrollment check are held by the
        caller. A surviving *.pending file is an irrevocable STOP until a
        separately authorized forensic/recovery decision (never auto-cleanup).
        """
        if self.read() != self.expected(intent, state="UNCONSUMED"):
            raise PreconditionBlocked("first-use intent already used or mismatched")
        updated = self.expected(intent, state="CONSUMED")
        try:
            # Exclusive creation makes a prior interrupted consumption visible.
            with self.pending.open("xb") as output:
                output.write(json.dumps(updated, sort_keys=True,
                                        separators=(",", ":")).encode("ascii"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(self.pending, self.path)
            # Flush parent directory where supported. Real Windows semantics
            # still require independent host power-loss qualification.
            if os.name != "nt":
                fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            if self.read() != updated:
                raise PreconditionBlocked("first-use intent reread mismatch")
        except (OSError, ValueError, TypeError):
            raise PreconditionBlocked("first-use intent consumption uncertain") from None


def consume_first_use_source_only(
    *, fence: CycleAttemptFence, intent_file: FileFirstUseIntent,
    status: Mapping[str, object], history: HostHistory,
    intent: InitialIntent, maintenance: MaintenanceReceipt,
    boot: InitialBootObservation, release_attempt_id: str,
) -> None:
    """Source-only fail-closed model. NEVER call from a release/USB code path.

    This function does not verify any external fact (Human permit, Device
    build/boot, terminal PCM, original protected media). All positive evidence
    must be independently proven before a future production integrator can
    use an equivalent protocol; this is not such a proof or authorization.
    """
    attempt = _uuid(release_attempt_id)
    with fence.serialized():
        # Observe the original host entry-state under the same lock that
        # serializes the later initial DB + anchor commit. A newly created
        # enrollment must not be mistaken for a pre-existing enrollment.
        fence.check_unenrolled_locked()
        candidate = inspect_first_use_admission(
            status=status, history=history, intent=intent,
            maintenance=maintenance, boot=boot,
        )
        if attempt == candidate.maintenance_attempt_id:
            raise PreconditionBlocked("initial cycle cannot reuse maintenance attempt")
        if intent_file.read() != intent_file.expected(intent, state="UNCONSUMED"):
            raise PreconditionBlocked("missing or consumed supervised intent")

        # Durably burn the supervised one-shot BEFORE creating either host
        # continuity witness. If interrupted, no sender is invoked and the
        # missing/consumed/partial intent makes a later retry fail closed.
        intent_file.consume(intent)
        if intent_file.read() != intent_file.expected(intent, state="CONSUMED"):
            raise PreconditionBlocked("first-use intent not durably consumed")

        # Atomic initial ledger+first cycle; independent anchor second.
        # A failure or crash at either boundary permanently blocks normal
        # admission, rather than restoring an allegedly never-enrolled host.
        fence.initialize_first_cycle_locked(
            candidate.device_id, candidate.release_cycle_id, attempt,
        )
        fence.verify_reserved_locked(
            candidate.device_id, candidate.release_cycle_id, attempt,
        )
        # No return token, release callback or enrollment approval is produced.
