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
    VolumeBusy,
    VolumeDismountError,
    VolumeLockError,
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
    physical_usb_instance_id: str | None


@dataclass(frozen=True)
class CdcInterface:
    """A present COM interface's exact PnP path, not a mutable COM-name alias."""

    device_path: str
    instance_id: str
    physical_usb_instance_id: str


class Win32DeviceApi(Protocol):
    def volume_guid(self, mount_root: str) -> str: ...
    def volume_disk_numbers(self, volume_guid: str) -> tuple[int, ...]: ...
    def disk_interfaces(self) -> list[DiskInterface]: ...
    def filesystem_accessible(self, volume_guid: str) -> bool: ...
    def disk_instance_present(self, instance_id: str) -> bool: ...
    def mount_point_present(self, mount_root: str) -> bool: ...
    def lock_and_dismount(self, volume_guid: str) -> ContextManager[None]: ...
    def release_cdc(self, port: str, attempt_id: str, binding: CdcInterface) -> None: ...
    def cdc_interface(self, port: str) -> CdcInterface: ...
    def set_time(self, port: str, binding: CdcInterface, utc_text: str) -> None: ...


class WindowsHostReleasePort:
    """Pure orchestration of a mockable Win32 boundary and canonical metadata."""

    def __init__(self, api: Win32DeviceApi, *, contracts_dir: str | None = None):
        self.api = api
        self.contracts_dir = contracts_dir
        # Freeze once for this port/session; a later re-enumeration may never
        # silently replace the CDC endpoint selected before the I/O cutoff.
        self._cdc_binding: tuple[str, str, CdcInterface] | None = None

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
        if (not only.instance_id or not only.removable_instance_id
                or not only.physical_usb_instance_id):
            raise UnsupportedTarget("removable device boundary not proven")
        return EjectTarget(
            expected_device_id, mount_root, guid, disks[0],
            only.instance_id, only.removable_instance_id,
            only.physical_usb_instance_id,
        )

    def revalidate_target(self, target: EjectTarget, cdc_port: str) -> None:
        # Device metadata must still match even when drive-letter reuse occurs.
        try:
            current_id = inspect_device(
                target.mount_root, contracts_dir=self.contracts_dir
            )["deviceId"]
        except (OSError, ValueError, KeyError):
            raise WrongTarget("target Device metadata no longer validated") from None
        if current_id != target.expected_device_id:
            raise WrongTarget("device identity changed")
        current = self.resolve_target(target.mount_root, target.expected_device_id)
        if current != target:
            raise WrongTarget("frozen target topology changed")
        endpoint = self.api.cdc_interface(cdc_port)
        if endpoint.physical_usb_instance_id != target.usb_physical_instance_id:
            raise WrongTarget("CDC port is not on the frozen USB device")
        binding = (target.usb_physical_instance_id, cdc_port.upper(), endpoint)
        if self._cdc_binding is not None and self._cdc_binding != binding:
            raise WrongTarget("CDC endpoint changed within the release session")
        self._cdc_binding = binding

    def volume_available(self, target: EjectTarget) -> bool:
        return self.api.filesystem_accessible(target.volume_guid)

    def quiesce(self, target: EjectTarget) -> ContextManager[None]:
        return self.api.lock_and_dismount(target.volume_guid)

    def release_storage(self, cdc_port: str, attempt_id: str, target: EjectTarget) -> None:
        # Do not acquire a new binding after seal; this must be the exact
        # endpoint frozen while the ordinary MSC lease was still open.
        binding = self._cdc_binding
        if (binding is None or binding[0] != target.usb_physical_instance_id
                or binding[1] != cdc_port.upper()
                or self.api.cdc_interface(cdc_port) != binding[2]):
            raise WrongTarget("CDC endpoint changed identity before release")
        self.api.release_cdc(cdc_port, attempt_id, binding[2])

    def set_time(self, cdc_port: str, utc_text: str) -> None:
        """Current-session #50 pre-release SET_TIME on the frozen CDC path.

        Must only be called between ordinary target validation and I/O seal.
        The native adapter rejects missing/changed endpoint identity.
        """
        binding = self._cdc_binding
        if (binding is None or binding[1] != cdc_port.upper()
                or self.api.cdc_interface(cdc_port) != binding[2]):
            raise WrongTarget("RTC CDC endpoint not frozen")
        self.api.set_time(cdc_port, binding[2], utc_text)

    def volume_disappeared(self, target: EjectTarget) -> bool:
        # No post-acceptance CreateFile, storage IOCTL, or filesystem query.
        # Observe only host PnP and DOS namespace state. Dismount alone keeps
        # the disk interface present and is NOT sufficient release proof.
        if self.api.disk_instance_present(target.disk_instance_id):
            return False
        return not self.api.mount_point_present(target.mount_root)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
    ]


COMPORT_INTERFACE_GUID = GUID(
    0x86E0D1E0, 0x8089, 0x11D0,
    (ctypes.c_ubyte * 8)(0x9C, 0xE4, 0x08, 0x00, 0x3E, 0x30, 0x1F, 0x73),
)

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
        self.k.QueryDosDeviceW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ]
        self.k.QueryDosDeviceW.restype = wintypes.DWORD

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

    def _physical_usb_id(self, devinst: int) -> str | None:
        # Prefer the nearest USB physical-device node; shared hub ancestors
        # must not be allowed to correlate different USB devices.
        for _ in range(32):
            instance = self._instance_id(devinst)
            if re.fullmatch(
                r"USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}\\[^\\]+",
                instance, flags=re.IGNORECASE,
            ):
                return instance.upper()
            parent = wintypes.DWORD()
            if self.cm.CM_Get_Parent(ctypes.byref(parent), devinst, 0) != CR_SUCCESS:
                break
            devinst = parent.value
        return None

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
                    self._physical_usb_id(info.DevInst),
                ))
                index += 1
        finally:
            self.s.SetupDiDestroyDeviceInfoList(devs)
        return discovered

    def disk_instance_present(self, instance_id: str) -> bool:
        """Poll read-only PnP metadata for the frozen disk instance.

        This MUST NOT open a disk handle or issue a storage DeviceIoControl
        after RELEASE_ACCEPTED.
        """
        if not instance_id:
            raise HostQuiesceError("missing frozen disk identity")
        devs = self.s.SetupDiGetClassDevsW(
            ctypes.byref(DISK_INTERFACE_GUID), None, None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
        )
        if devs is None or devs == INVALID_HANDLE_VALUE:
            raise HostQuiesceError("PnP presence query failed")
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
                        return False
                    raise HostQuiesceError("PnP presence iteration failed")
                detail = ctypes.create_string_buffer(4096)
                struct.pack_into(
                    "<I", detail, 0,
                    8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6,
                )
                needed = wintypes.DWORD()
                info = SP_DEVINFO_DATA()
                info.cbSize = ctypes.sizeof(info)
                if not self.s.SetupDiGetDeviceInterfaceDetailW(
                    devs, ctypes.byref(interface), detail, len(detail),
                    ctypes.byref(needed), ctypes.byref(info),
                ):
                    raise HostQuiesceError("PnP presence identity unavailable")
                if self._instance_id(info.DevInst).casefold() == instance_id.casefold():
                    return True
                index += 1
        finally:
            self.s.SetupDiDestroyDeviceInfoList(devs)

    def mount_point_present(self, mount_root: str) -> bool:
        """Query only the host DOS-device symbolic-link namespace.

        QueryDosDeviceW does not open a volume or inspect its filesystem.
        Unknown errors fail closed rather than proving disappearance.
        """
        if re.fullmatch(r"[A-Za-z]:\\", mount_root) is None:
            raise HostQuiesceError("frozen mount root invalid")
        buffer = ctypes.create_unicode_buffer(32768)
        if self.k.QueryDosDeviceW(mount_root[:2], buffer, len(buffer)):
            return True
        if ctypes.get_last_error() in (2, 3):  # not found / path not found
            return False
        raise HostQuiesceError("host mount namespace query failed")

    def _cdc_interface(self, port: str) -> CdcInterface:
        # The CDC COM port must be a member of the SAME USB physical device
        # as the validated MSC disk, not just a COM number supplied by the user.
        import winreg

        if re.fullmatch(r"COM[1-9][0-9]{0,3}", port, re.IGNORECASE) is None:
            raise WrongTarget("invalid CDC port")
        devs = self.s.SetupDiGetClassDevsW(
            ctypes.byref(COMPORT_INTERFACE_GUID), None, None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
        )
        if devs is None or devs == INVALID_HANDLE_VALUE:
            raise WrongTarget("CDC interface enumeration failed")
        matched: list[CdcInterface] = []
        try:
            index = 0
            while True:
                interface = SP_DEVICE_INTERFACE_DATA()
                interface.cbSize = ctypes.sizeof(interface)
                if not self.s.SetupDiEnumDeviceInterfaces(
                    devs, None, ctypes.byref(COMPORT_INTERFACE_GUID),
                    index, ctypes.byref(interface),
                ):
                    if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                        break
                    raise WrongTarget("CDC enumeration failed")
                detail = ctypes.create_string_buffer(4096)
                struct.pack_into(
                    "<I", detail, 0, 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6,
                )
                required = wintypes.DWORD()
                info = SP_DEVINFO_DATA()
                info.cbSize = ctypes.sizeof(info)
                if not self.s.SetupDiGetDeviceInterfaceDetailW(
                    devs, ctypes.byref(interface), detail, len(detail),
                    ctypes.byref(required), ctypes.byref(info),
                ):
                    raise WrongTarget("CDC interface details unavailable")
                try:
                    path = ("SYSTEM\\CurrentControlSet\\Enum\\"
                            + self._instance_id(info.DevInst) + "\\Device Parameters")
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                        assigned, _ = winreg.QueryValueEx(key, "PortName")
                except (OSError, ValueError):
                    assigned = None
                if isinstance(assigned, str) and assigned.upper() == port.upper():
                    physical = self._physical_usb_id(info.DevInst)
                    if physical is None:
                        raise WrongTarget("CDC physical USB identity unavailable")
                    # Keep the OS-published PnP interface path for the actual
                    # CreateFileW call. Never reopen the reusable COMx alias.
                    device_path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
                    if not device_path.startswith("\\\\?\\"):
                        raise WrongTarget("CDC device interface path invalid")
                    matched.append(CdcInterface(
                        device_path, self._instance_id(info.DevInst), physical,
                    ))
                index += 1
        finally:
            self.s.SetupDiDestroyDeviceInfoList(devs)
        if len(matched) != 1:
            raise WrongTarget("CDC port mapping is missing or ambiguous")
        return matched[0]

    def cdc_interface(self, port: str) -> CdcInterface:
        return self._cdc_interface(port)

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
        try:
            handle = self._open(
                volume_guid.rstrip("\\"),
                GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
            )
        except HostQuiesceError:
            if ctypes.get_last_error() in (32, 33):
                raise VolumeBusy("volume has outstanding host access") from None
            raise VolumeLockError("exact volume could not be opened") from None
        try:
            try:
                self._ioctl(handle, FSCTL_LOCK_VOLUME)
            except HostQuiesceError:
                if ctypes.get_last_error() in (32, 33):
                    raise VolumeBusy("volume lock vetoed by active I/O") from None
                raise VolumeLockError("exact volume lock failed") from None
            try:
                self._ioctl(handle, FSCTL_DISMOUNT_VOLUME)
            except HostQuiesceError:
                raise VolumeDismountError("exact volume dismount failed") from None
            yield
        finally:
            self.k.CloseHandle(handle)

    def release_cdc(self, port: str, attempt_id: str, binding: CdcInterface) -> None:
        # A PnP interface path identifies the endpoint independently of COMx.
        # A COM alias may be reassigned between the adapter's last check and
        # CreateFileW; it must never select the endpoint receiving the command.
        endpoint = self._cdc_interface(port)
        if endpoint != binding:
            raise WrongTarget("CDC release endpoint no longer matches frozen binding")
        request_id = str(uuid.uuid4())
        request = (json.dumps({
            "id": request_id, "cmd": "RELEASE_STORAGE",
            "args": {"releaseAttemptId": attempt_id},
        }, separators=(",", ":")) + "\n").encode("ascii")
        handle = self._open(endpoint.device_path, GENERIC_READ | GENERIC_WRITE, 0)
        try:
            # If the port disappeared or was re-enumerated between resolution
            # and handle opening, abort before any CDC protocol bytes are sent.
            if self._cdc_interface(port) != binding:
                raise WrongTarget("CDC endpoint changed during handle acquisition")
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
                if obj.get("id") == request_id and obj.get("ok") is False:
                    raise ReleaseRejected("CDC release rejected")
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
                raise ReleaseIndeterminate("CDC release acceptance unproven")
        finally:
            self.k.CloseHandle(handle)

    def read_status(self, port: str) -> dict[str, object]:
        """#135 read-only GET_STATUS; no release authority or filesystem write.

        Exact present CDC interface is frozen before open and verified again
        after CreateFile. The caller separately validates its MSC physical
        parent and treats every uncertain response as unqualified.
        """
        endpoint = self._cdc_interface(port)
        request_id = str(uuid.uuid4())
        request = (json.dumps({
            "id": request_id, "cmd": "GET_STATUS", "args": {},
        }, separators=(",", ":")) + "\n").encode("ascii")
        handle = self._open(endpoint.device_path, GENERIC_READ | GENERIC_WRITE, 0)
        try:
            if self._cdc_interface(port) != endpoint:
                raise WrongTarget("CDC endpoint changed before status query")
            timeouts = COMMTIMEOUTS(0xFFFFFFFF, 0, 250, 0, 1000)
            if not self.k.SetCommTimeouts(handle, ctypes.byref(timeouts)):
                raise HostQuiesceError("CDC status timeout configuration failed")
            out = ctypes.create_string_buffer(request)
            written = wintypes.DWORD()
            if not self.k.WriteFile(handle, out, len(request), ctypes.byref(written), None):
                raise HostQuiesceError("CDC status write failed")
            if written.value != len(request):
                raise HostQuiesceError("CDC status short write")
            deadline = time.monotonic() + 5.0
            reply = bytearray()
            while time.monotonic() < deadline and len(reply) < MAX_CDC_REPLY:
                buf = ctypes.create_string_buffer(1)
                got = wintypes.DWORD()
                if not self.k.ReadFile(handle, buf, 1, ctypes.byref(got), None):
                    raise HostQuiesceError("CDC status response lost")
                if got.value:
                    reply.extend(buf.raw[:got.value])
                    if reply.endswith(b"\n"):
                        break
            else:
                raise HostQuiesceError("CDC status response incomplete")
            try:
                response = json.loads(reply.decode("utf-8"))
            except (UnicodeError, ValueError):
                raise HostQuiesceError("CDC status invalid envelope") from None
            if (not isinstance(response, dict)
                    or response.get("id") != request_id
                    or response.get("ok") is not True
                    or not isinstance(response.get("result"), dict)):
                raise HostQuiesceError("CDC status not qualified")
            result = response["result"]
            if (not isinstance(result.get("state"), str)
                    or not isinstance(result.get("rtcCorrectionPending"), bool)):
                raise HostQuiesceError("CDC status invalid fields")
            parsed = {
                "state": result["state"],
                "rtcCorrectionPending": result["rtcCorrectionPending"],
            }
            # R3 #143: a Device-authored optional witness is passed through
            # only if it is a strict lowercase canonical UUID. An absent or
            # malformed witness does NOT block safe read-only WAV intake, but
            # never qualifies a release. Caller must independently verify
            # current-boot origin, finalized manifest and exact CDC/MSC target.
            cycle = result.get("releaseCycleId")
            if isinstance(cycle, str):
                try:
                    canonical = str(uuid.UUID(cycle))
                except (ValueError, AttributeError):
                    canonical = None
                if canonical is not None and cycle == canonical:
                    parsed["releaseCycleId"] = canonical
            return parsed
        finally:
            self.k.CloseHandle(handle)

    def set_time(self, port: str, binding: CdcInterface, utc_text: str) -> None:
        """#21/#50 qualified pre-seal RTC mutation on a frozen CDC endpoint.

        This is a source-only adapter. Caller MUST first prove the current
        authorized MSC/CDC session and must not retry after indeterminate I/O.
        No unattended caller is wired until an adequate release session fence
        and current-running Device qualification both exist.
        """
        from datetime import datetime

        if (not isinstance(utc_text, str)
                or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
                                r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z", utc_text) is None):
            raise ReleaseRejected("RTC input must be whole-second UTC")
        try:
            datetime.strptime(utc_text, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ReleaseRejected("RTC input is invalid") from None
        if not isinstance(binding, CdcInterface):
            raise WrongTarget("frozen CDC endpoint required")
        if self._cdc_interface(port) != binding:
            raise WrongTarget("RTC endpoint no longer matches frozen binding")
        request_id = str(uuid.uuid4())
        request = (json.dumps({
            "id": request_id, "cmd": "SET_TIME", "args": {"time": utc_text},
        }, separators=(",", ":")) + "\n").encode("ascii")
        handle = self._open(binding.device_path, GENERIC_READ | GENERIC_WRITE, 0)
        try:
            if self._cdc_interface(port) != binding:
                raise WrongTarget("RTC endpoint changed during handle acquisition")
            timeouts = COMMTIMEOUTS(0xFFFFFFFF, 0, 250, 0, 1000)
            if not self.k.SetCommTimeouts(handle, ctypes.byref(timeouts)):
                raise ReleaseRejected("RTC CDC timeout configuration failed")
            written = wintypes.DWORD()
            out = ctypes.create_string_buffer(request)
            if not self.k.WriteFile(handle, out, len(request), ctypes.byref(written), None):
                raise ReleaseIndeterminate("RTC CDC write outcome unproven")
            if written.value != len(request):
                raise ReleaseIndeterminate("RTC CDC short write")
            deadline = time.monotonic() + 5.0
            reply = bytearray()
            while time.monotonic() < deadline and len(reply) < MAX_CDC_REPLY:
                buf = ctypes.create_string_buffer(1)
                got = wintypes.DWORD()
                if not self.k.ReadFile(handle, buf, 1, ctypes.byref(got), None):
                    raise ReleaseIndeterminate("RTC CDC response lost")
                if got.value:
                    reply.extend(buf.raw[:got.value])
                    if reply.endswith(b"\n"):
                        break
            else:
                raise ReleaseIndeterminate("RTC CDC response incomplete")
            try:
                response = json.loads(reply.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise ReleaseIndeterminate("RTC CDC response malformed") from None
            if not isinstance(response, dict) or response.get("id") != request_id:
                raise ReleaseIndeterminate("RTC CDC response identity mismatch")
            if response.get("ok") is False:
                raise ReleaseRejected("RTC mutation rejected")
            result = response.get("result")
            if (response.get("ok") is not True or not isinstance(result, dict)
                    or result.get("time") != utc_text
                    or result.get("eventPending") is not True):
                raise ReleaseIndeterminate("RTC pending event not proven")
        finally:
            self.k.CloseHandle(handle)
