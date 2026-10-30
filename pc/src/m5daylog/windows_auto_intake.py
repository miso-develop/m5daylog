"""#135 opt-in Windows arrival observer for intake-only operation.

There is deliberately NO automatic RELEASE_STORAGE call. This observer uses
read-only CDC GET_STATUS and #89 MSC/CDC binding to admit known USB_SYNC
sessions, then delegates raw copying to #51 through auto_intake.py.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from .auto_intake import Arrival, IntakeCoordinator, PollingMonitor
from .capacity import VerifiedSession
from .db import resolve_db_path
from .sync import _contracts_dir, _load_manifest, _preflight, inspect_device
from .windows_capacity import WindowsCapacityReader
from .windows_release import NativeWin32DeviceApi, WindowsHostReleasePort

_COM = re.compile(r"COM[1-9][0-9]{0,3}\Z", re.IGNORECASE)
_DRIVE_REMOVABLE = 2
_CONFIG_MAX_BYTES = 65536


def local_config_path() -> Path:
    """No persistent Windows service, machine-wide config or elevated install."""
    local = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not local:
        raise OSError("Windows per-user LOCALAPPDATA required")
    return Path(local) / "M5Daylog" / "monitor.json"


def read_opt_in(path: Path) -> frozenset[str]:
    """A missing, disabled, malformed, or empty allowlist stops all observation."""
    try:
        if path.stat().st_size > _CONFIG_MAX_BYTES:
            raise ValueError("config too large")
        document = json.loads(path.read_text(encoding="utf-8"))
        ids = document["trustedDeviceIds"]
        if (not isinstance(document, dict) or document.get("enabled") is not True
                or not isinstance(ids, list) or not 1 <= len(ids) <= 16):
            raise ValueError("monitor not explicitly opted in")
        trusted = frozenset(str(uuid.UUID(s)) for s in ids if isinstance(s, str))
        if len(trusted) != len(ids):
            raise ValueError("invalid or duplicate authorization")
        return trusted
    except (OSError, KeyError, ValueError, TypeError) as exc:
        # Do not expose the contents/paths of user configuration.
        raise ValueError("MONITOR_OPT_IN_NOT_QUALIFIED") from None


def enumerate_removable_roots(kernel32: object) -> list[str]:
    """Only mounted drive roots; never discover or inspect a fixed PC disk."""
    get_strings = kernel32.GetLogicalDriveStringsW
    get_strings.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    get_strings.restype = wintypes.DWORD
    get_type = kernel32.GetDriveTypeW
    get_type.argtypes = [wintypes.LPCWSTR]
    get_type.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(4096)
    chars = get_strings(len(buffer), buffer)
    if not chars or chars >= len(buffer):
        raise OSError("Windows volume enumeration failed")
    return sorted(
        root for root in buffer[:chars].split("\0")
        if re.fullmatch(r"[A-Za-z]:\\", root) and get_type(root) == _DRIVE_REMOVABLE
    )


def enumerate_com_ports() -> list[str]:
    import winreg
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM"
        ) as handle:
            values = []
            for index in range(128):
                try:
                    _, value, _ = winreg.EnumValue(handle, index)
                except OSError:
                    break
                if isinstance(value, str) and _COM.fullmatch(value):
                    values.append(value.upper())
    except OSError:
        return []
    return sorted(set(values))


class WindowsArrivalProvider:
    """Admission-only observer; every ambiguity blocks rather than guesses."""

    def __init__(
        self, *, trusted: frozenset[str], api: NativeWin32DeviceApi,
        port: WindowsHostReleasePort,
        roots=None, ports=None, status=None, preflight=None, port_factory=None,
    ) -> None:
        self.trusted = trusted
        self.api = api
        self.port = port
        self.port_factory = port_factory or (lambda: port)
        self.roots = roots or (lambda: enumerate_removable_roots(self.api.k))
        self.ports = ports or enumerate_com_ports
        self.status = status or self.api.read_status
        self.preflight = preflight or self._canonical_preflight
        self._epochs: dict[tuple[str, str, str], str] = {}

    def _canonical_preflight(self, root: str) -> str:
        device = inspect_device(root, contracts_dir=self.port.contracts_dir)
        manifest = _load_manifest(Path(root), _contracts_dir(self.port.contracts_dir))
        _preflight(Path(root), device, manifest)
        return device["deviceId"]

    def scan(self) -> list[Arrival]:
        candidates: list[Arrival] = []
        com_ports = self.ports()
        for root in self.roots():
            try:
                device_id = self.preflight(root)
                if device_id not in self.trusted:
                    continue
                session_port = self.port_factory()
                target = session_port.resolve_target(root, device_id)
                # Require exactly one current CDC on the same USB physical parent.
                matched = []
                for com in com_ports:
                    try:
                        endpoint = self.api.cdc_interface(com)
                        if endpoint.physical_usb_instance_id == target.usb_physical_instance_id:
                            matched.append(com)
                    except Exception:
                        continue
                if len(matched) != 1:
                    continue
                com = matched[0]
                session_port.revalidate_target(target, com)
                if not session_port.volume_available(target):
                    continue
                result = self.status(com)
                if (not isinstance(result, dict)
                        or result.get("state") != "USB_SYNC"
                        or not isinstance(result.get("rtcCorrectionPending"), bool)):
                    continue
                session_port.revalidate_target(target, com)
                if not session_port.volume_available(target):
                    continue
                key = (target.volume_guid, target.usb_physical_instance_id, com)
                epoch = self._epochs.setdefault(key, str(uuid.uuid4()))
                candidates.append(Arrival(VerifiedSession(target, com, epoch), True))
            except Exception:
                # Untrusted media or OS failures must not cause any write.
                continue
        # More than one MSC mapping to one physical parent is ambiguous.
        by_parent: dict[str, int] = {}
        for a in candidates:
            parent = a.binding.target.usb_physical_instance_id
            by_parent[parent] = by_parent.get(parent, 0) + 1
        safe = [
            a for a in candidates
            if by_parent[a.binding.target.usb_physical_instance_id] == 1
        ]
        active = {
            (a.binding.target.volume_guid,
             a.binding.target.usb_physical_instance_id,
             a.binding.cdc_port) for a in safe
        }
        self._epochs = {key: value for key, value in self._epochs.items() if key in active}
        return safe

    def current_epoch(self) -> str:
        if len(self._epochs) != 1:
            return ""
        return next(iter(self._epochs.values()))


def run_monitor(*, once: bool = False, output=None) -> int:
    """Entry point is inert without a pre-existing opt-in file."""
    if os.name != "nt":
        return 2
    try:
        root = local_config_path()
        trusted = read_opt_in(root)
        api = NativeWin32DeviceApi()
        port = WindowsHostReleasePort(api)
        provider = WindowsArrivalProvider(
            trusted=trusted, api=api, port=port,
            port_factory=lambda: WindowsHostReleasePort(api),
        )
        local_state = root.parent / "state"
        coordinator = IntakeCoordinator(
            port=port, capacity_reader=WindowsCapacityReader(),
            trusted_device_ids=trusted,
            local_lock_file=local_state / "intake.lock",
            library_root=Path.home() / "Documents" / "M5Daylog",
            db_path=resolve_db_path(data_dir=str(root.parent)),
            port_factory=lambda: WindowsHostReleasePort(api),
        )
        def report(notice: dict[str, object]) -> None:
            # One bounded, sanitized local status file. Never log the root,
            # serial, Device ID, audio, username or raw Win32 exception.
            serialized = json.dumps(notice, ensure_ascii=False, sort_keys=True)
            tmp = local_state / "auto_intake_status.tmp"
            dst = local_state / "auto_intake_status.json"
            local_state.mkdir(parents=True, exist_ok=True)
            tmp.write_text(serialized + "\n", encoding="utf-8")
            tmp.replace(dst)
            if output is not None:
                output(serialized)
        monitor = PollingMonitor(
            provider, coordinator, provider.current_epoch, report
        )
        if once:
            monitor.poll_once()
            time.sleep(2)
            monitor.poll_once()
        else:
            monitor.run_forever()
        return 0
    except (OSError, ValueError):
        # No echo of sensitive config or Windows exception data.
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M5Daylog user opt-in intake-only monitor")
    parser.add_argument("--once", action="store_true", help="One bounded synthetic/diagnostic scan")
    args = parser.parse_args(argv)
    return run_monitor(once=args.once, output=print)


if __name__ == "__main__":
    sys.exit(main())
