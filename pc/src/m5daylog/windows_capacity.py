"""#137: exact-validated-volume read-only Win32 capacity API boundary."""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes

from .capacity import FreeSpace64

_VOLUME_ROOT = re.compile(r"^\\\\\?\\Volume\{[0-9a-fA-F-]{36}\}\\$")
_U64 = ctypes.c_ulonglong


class WindowsCapacityReader:
    """Never discovers a drive and never opens, writes or ejects media."""

    def __init__(self, kernel32: object | None = None) -> None:
        if kernel32 is None:
            if os.name != "nt":
                raise OSError("Windows required for capacity observation")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._query = kernel32.GetDiskFreeSpaceExW
        self._query.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(_U64),
            ctypes.POINTER(_U64), ctypes.POINTER(_U64),
        ]
        self._query.restype = wintypes.BOOL

    def query(self, exact_volume_guid_root: str) -> FreeSpace64:
        if (not isinstance(exact_volume_guid_root, str)
                or not _VOLUME_ROOT.fullmatch(exact_volume_guid_root)):
            raise ValueError("verified Volume GUID root is required")
        available, total, free = _U64(), _U64(), _U64()
        ok = self._query(
            exact_volume_guid_root, ctypes.byref(available),
            ctypes.byref(total), ctypes.byref(free),
        )
        if not ok:
            # Never leak OS messages containing a user/device path.
            raise OSError("GetDiskFreeSpaceExW failed")
        return FreeSpace64(available.value, total.value, free.value)
