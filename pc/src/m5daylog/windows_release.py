"""Windows exact-volume lock/dismount and CDC release adapter (D-031).

Only stdlib ctypes is used. Native device access is disabled on non-Windows.
No PnP eject, shell removal or SCSI pass-through is release authority.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import struct
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from typing import ContextManager, Protocol

from .release import (
    AlreadyRemoved,
    EjectTarget,
    HostQuiesceError,
    ReleaseIndeterminate,
    ReleaseRejected,
    UnsupportedTarget,
    WrongTarget,
)
from .sync import inspect_device

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
DIGCF_PRESENT = 2
DIGCF_DEVICEINTERFACE = 0x10
CM_DRP_CAPABILITIES = 0x10
CM_DEVCAP_REMOVABLE = 4
CR_SUCCESS = 0
ERROR_NO_MORE_ITEMS = 259
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_CDC_REPLY = 4096


@dataclass(frozen=True)
class DiskInterface:
    disk_number: int
    instance_id: str
    removable_instance_id: str | None


class Win32DeviceApi(Protocol):
    def volume_guid(self, mount_root: str) -> str: ...
    def volume_disk_numbers(self, volume_guid: str) -> tuple[int, ...]: ...
    def disk_interfaces(self) -> list[DiskInterface]: ...
    def filesystem_accessible(self, volume_guid: str) -> bool: ...
    def lock_and_dismount(self, volume_guid: str) -> ContextManager[None]: ...
    def release_cdc(self, port: str, attempt_id: str) -> None: ...


class WindowsHostReleasePort:
    """Pure orchestration of a mockable Win32 boundary and canonical metadata."""

    def __init__(self, api: Win32DeviceApi, *, contracts_dir: str | None = None):
        self.api = api
        self.contracts_dir = contracts_dir

    def resolve_target(self, mount_root: str, expected_device_id: str) -> EjectTarget:
        # Fail closed on non-root paths; no drive label / first removable heuristic.
        if re.fullmatch(r"[A-Za-z]:\\", mount_root) is None:
            raise UnsupportedTarget("an exact Windows drive root is required")
        guid = self.api.volume_guid(mount_root)
        disks = self.api.volume_disk_numbers(guid)
        if len(disks) != 1 or disks[0] < 0:
            raise UnsupportedTarget("ambiguous volume extents")
        matches = [iface for iface in self.api.disk_interfaces()
                   if iface.disk_number == disks[0]]
        if len(matches) != 1:
            raise UnsupportedTarget("ambiguous disk interface")
        only = matches[0]
        if not only.instance_id or not only.removable_instance_id:
            raise UnsupportedTarget("removable device boundary not proven")
        return EjectTarget(
            expected_device_id, mount_root, guid, disks[0],
            only.instance_id, only.removable_instance_id,
        )

    def revalidate_target(self, target: EjectTarget) -> None:
        # Device metadata must still match even when drive-letter reuse occurs.
        current_id = inspect_device(
            target.mount_root, contracts_dir=self.contracts_dir
        )["deviceId"]
        if current_id != target.expected_device_id:
            raise WrongTarget("device identity changed")
        current = self.resolve_target(target.mount_root, target.expected_device_id)
        if current != target:
            raise WrongTarget("frozen target topology changed")

    def volume_available(self, target: EjectTarget) -> bool:
        return self.api.filesystem_accessible(target.volume_guid)

    def quiesce(self, target: EjectTarget) -> ContextManager[None]:
        return self.api.lock_and_dismount(target.volume_guid)

    def release_storage(self, cdc_port: str, attempt_id: str) -> None:
        self.api.release_cdc(cdc_port, attempt_id)

    def volume_disappeared(self, target: EjectTarget) -> bool:
        # Dismount alone blocks normal filesystem opens; it is NOT release proof.
        # The exact disk interface must also disappear from the live interface set.
        matching = [
            iface for iface in self.api.disk_interfaces()
            if iface.instance_id == target.disk_instance_id
        ]
        if matching:
            return False
        return not self.api.filesystem_accessible(target.volume_guid)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
    ]


DISK_INTERFACE_GUID = GUID(
    0x53F56307, 0xB6BF, 0x11D0,
    (ctypes.c_ubyte * 8)(0x94, 0xF2, 0x00, 0xA0, 0xC9, 0x1E, 0xFB, 0x8B),
)


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p),
    ]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("ClassGuid", GUID),
        ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p),
    ]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [
        ("ReadIntervalTimeout", wintypes.DWORD),
        ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
        ("ReadTotalTimeoutConstant", wintypes.DWORD),
        ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
        ("WriteTotalTimeoutConstant", wintypes.DWORD),
    ]


class NativeWin32DeviceApi:
    """Thin Win32 API implementation; all calls are executed synchronously."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows host required")
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.s = ctypes.WinDLL("setupapi", use_last_error=True)
        self.cm = ctypes.WinDLL("cfgmgr32", use_last_error=True)

        self.k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self.k.CreateFileW.restype = wintypes.HANDLE
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k.CloseHandle.restype = wintypes.BOOL
        self.k.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self.k.DeviceIoControl.restype = wintypes.BOOL
        self.k.GetVolumePathNameW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ]
        self.k.GetVolumePathNameW.restype = wintypes.BOOL
        self.k.GetVolumeNameForVolumeMountPointW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ]
        self.k.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
        self.k.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
        ]
        self.k.GetVolumeInformationW.restype = wintypes.BOOL

        self.s.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
        ]
        self.s.SetupDiGetClassDevsW.restype = wintypes.HANDLE
        self.s.SetupDiEnumDeviceInterfaces.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
            wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ]
        self.s.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        self.s.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
            ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(SP_DEVINFO_DATA),
        ]
        self.s.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
        self.s.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
        self.s.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

        self.cm.CM_Get_Device_IDW.argtypes = [
            wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD, wintypes.ULONG,
        ]
        self.cm.CM_Get_Device_IDW.restype = wintypes.ULONG
        self.cm.CM_Get_Parent.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.ULONG,
        ]
        self.cm.CM_Get_Parent.restype = wintypes.ULONG
        self.cm.CM_Get_DevNode_Registry_PropertyW.argtypes = [
            wintypes.DWORD, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
            ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG), wintypes.ULONG,
        ]
        self.cm.CM_Get_DevNode_Registry_PropertyW.restype = wintypes.ULONG

        self.k.SetCommTimeouts.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(COMMTIMEOUTS),
        ]
        self.k.SetCommTimeouts.restype = wintypes.BOOL
        self.k.ReadFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        self.k.ReadFile.restype = wintypes.BOOL
        self.k.WriteFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        self.k.WriteFile.restype = wintypes.BOOL

    def _open(self, path: str, access: int, sharing: int) -> int:
        handle = self.k.CreateFileW(path, access, sharing, None, OPEN_EXISTING, 0, None)
        if handle is None or handle == INVALID_HANDLE_VALUE:
            raise HostQuiesceError("Win32 CreateFile failed")
        return handle

    def _ioctl(self, handle: int, code: int, output_size: int = 0) -> bytes:
        buf = ctypes.create_string_buffer(output_size) if output_size else None
        returned = wintypes.DWORD()
        ok = self.k.DeviceIoControl(
            handle, code, None, 0, buf, output_size, ctypes.byref(returned), None,
        )
        if not ok:
            raise HostQuiesceError("Win32 DeviceIoControl failed")
        return buf.raw[:returned.value] if buf is not None else b""

    def volume_guid(self, mount_root: str) -> str:
        path = ctypes.create_unicode_buffer(512)
        guid = ctypes.create_unicode_buffer(512)
        if not self.k.GetVolumePathNameW(mount_root, path, len(path)):
            raise WrongTarget("mount path resolution failed")
        if path.value.casefold() != mount_root.casefold():
            raise WrongTarget("mount path is not the volume root")
        if not self.k.GetVolumeNameForVolumeMountPointW(path.value, guid, len(guid)):
            raise WrongTarget("volume GUID resolution failed")
        if re.fullmatch(r"\\\\\?\\Volume\{[0-9a-fA-F-]{36}\}\\", guid.value) is None:
            raise WrongTarget("invalid Volume GUID")
        return guid.value

    def volume_disk_numbers(self, volume_guid: str) -> tuple[int, ...]:
        handle = self._open(volume_guid.rstrip("\\"), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE)
        try:
            data = self._ioctl(handle, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, 4096)
            if len(data) < 12:
                raise UnsupportedTarget("no volume disk extents")
            count = struct.unpack_from("<I", data, 0)[0]
            if count != 1 or len(data) < 32:
                raise UnsupportedTarget("volume must have one disk extent")
            return (struct.unpack_from("<I", data, 8)[0],)
        finally:
            self.k.CloseHandle(handle)

    def _instance_id(self, devinst: int) -> str:
        buf = ctypes.create_unicode_buffer(512)
        if self.cm.CM_Get_Device_IDW(devinst, buf, len(buf), 0) != CR_SUCCESS:
            raise UnsupportedTarget("device instance id unavailable")
        return buf.value

    def _removable_ancestor(self, devinst: int) -> str | None:
        for _ in range(32):
            caps = wintypes.DWORD()
            size = wintypes.ULONG(ctypes.sizeof(caps))
            result = self.cm.CM_Get_DevNode_Registry_PropertyW(
                devinst, CM_DRP_CAPABILITIES, None,
                ctypes.byref(caps), ctypes.byref(size), 0,
            )
            if result == CR_SUCCESS and caps.value & CM_DEVCAP_REMOVABLE:
                return self._instance_id(devinst)
            parent = wintypes.DWORD()
            if self.cm.CM_Get_Parent(ctypes.byref(parent), devinst, 0) != CR_SUCCESS:
                return None
            devinst = parent.value
        return None

    def disk_interfaces(self) -> list[DiskInterface]:
        devs = self.s.SetupDiGetClassDevsW(
            ctypes.byref(DISK_INTERFACE_GUID), None, None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
        )
        if devs is None or devs == INVALID_HANDLE_VALUE:
            raise UnsupportedTarget("disk interface enumeration failed")
        discovered: list[DiskInterface] = []
        try:
            index = 0
            while True:
                interface = SP_DEVICE_INTERFACE_DATA()
                interface.cbSize = ctypes.sizeof(interface)
                if not self.s.SetupDiEnumDeviceInterfaces(
                    devs, None, ctypes.byref(DISK_INTERFACE_GUID), index,
                    ctypes.byref(interface),
                ):
                    if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                        break
                    raise UnsupportedTarget("disk interface iteration failed")
                # 64-bit Windows requires cbSize=8; 32-bit uses 6.
                detail = ctypes.create_string_buffer(4096)
                struct.pack_into("<I", detail, 0, 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
                needed = wintypes.DWORD()
                info = SP_DEVINFO_DATA()
                info.cbSize = ctypes.sizeof(info)
                if not self.s.SetupDiGetDeviceInterfaceDetailW(
                    devs, ctypes.byref(interface), detail, len(detail),
                    ctypes.byref(needed), ctypes.byref(info),
                ):
                    raise UnsupportedTarget("disk interface details unavailable")
                interface_path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
                handle = self._open(interface_path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE)
                try:
                    data = self._ioctl(handle, IOCTL_STORAGE_GET_DEVICE_NUMBER, 12)
                    if len(data) < 12:
                        raise UnsupportedTarget("disk number unavailable")
                    disk_number = struct.unpack_from("<I", data, 4)[0]
                finally:
                    self.k.CloseHandle(handle)
                discovered.append(DiskInterface(
                    disk_number,
                    self._instance_id(info.DevInst),
                    self._removable_ancestor(info.DevInst),
                ))
                index += 1
        finally:
            self.s.SetupDiDestroyDeviceInfoList(devs)
        return discovered

    def filesystem_accessible(self, volume_guid: str) -> bool:
        # A failed FS query alone is NOT teardown proof; disk interface
        # disappearance is independently required by volume_disappeared().
        serial, maximum, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        return bool(self.k.GetVolumeInformationW(
            volume_guid, None, 0, ctypes.byref(serial),
            ctypes.byref(maximum), ctypes.byref(flags), None, 0,
        ))

    @contextmanager
    def lock_and_dismount(self, volume_guid: str):
        handle = self._open(
            volume_guid.rstrip("\\"),
            GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
        )
        try:
            self._ioctl(handle, FSCTL_LOCK_VOLUME)
            self._ioctl(handle, FSCTL_DISMOUNT_VOLUME)
            yield
        finally:
            self.k.CloseHandle(handle)

    def release_cdc(self, port: str, attempt_id: str) -> None:
        if re.fullmatch(r"COM[1-9][0-9]{0,3}", port, flags=re.IGNORECASE) is None:
            raise ReleaseRejected("invalid CDC port")
        request_id = str(uuid.uuid4())
        request = (json.dumps({
            "id": request_id, "cmd": "RELEASE_STORAGE",
            "args": {"releaseAttemptId": attempt_id},
        }, separators=(",", ":")) + "\n").encode("ascii")
        handle = self._open(r"\\.\\" + port.upper(), GENERIC_READ | GENERIC_WRITE, 0)
        try:
            timeouts = COMMTIMEOUTS(0xFFFFFFFF, 0, 250, 0, 1000)
            if not self.k.SetCommTimeouts(handle, ctypes.byref(timeouts)):
                raise ReleaseRejected("CDC timeout configuration failed")
            written = wintypes.DWORD()
            out = ctypes.create_string_buffer(request)
            if not self.k.WriteFile(handle, out, len(request), ctypes.byref(written), None):
                raise ReleaseIndeterminate("CDC write failed")
            if written.value != len(request):
                raise ReleaseIndeterminate("CDC short write")
            deadline = time.monotonic() + 5.0
            reply = bytearray()
            while time.monotonic() < deadline and len(reply) < MAX_CDC_REPLY:
                buf = ctypes.create_string_buffer(1)
                got = wintypes.DWORD()
                if not self.k.ReadFile(handle, buf, 1, ctypes.byref(got), None):
                    raise ReleaseIndeterminate("CDC response transport failed")
                if got.value:
                    reply.extend(buf.raw[:got.value])
                    if reply.endswith(b"\n"):
                        break
            else:
                raise ReleaseIndeterminate("CDC response deadline or length exceeded")
            try:
                obj = json.loads(reply.decode("utf-8"))
                result = obj["result"]
                valid = (obj.get("id") == request_id and obj.get("ok") is True
                         and isinstance(result, dict)
                         and result.get("releaseAttemptId") == attempt_id
                         and result.get("accepted") is True)
            except (UnicodeError, ValueError, KeyError, TypeError, AttributeError):
                raise ReleaseIndeterminate("CDC response invalid") from None
            if not valid:
                # A negative response can be distinguished, but an invalid/mismatched
                # response cannot prove that the Device did not accept the command.
                if (obj.get("id") == request_id and obj.get("ok") is False):
                    raise ReleaseRejected("CDC release rejected")
                raise ReleaseIndeterminate("CDC release acceptance unproven")
        finally:
            self.k.CloseHandle(handle)
