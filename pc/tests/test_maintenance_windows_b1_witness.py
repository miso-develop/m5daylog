"""#126 B1a: offline, artificial FAT32-sector and mock Win32 session probes.

No raw device, actual Win32 API, volume handle, or release transport is used.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import struct
import unittest

SECTOR = 512
FILE_NAME = b"STATE   TXT"
PAYLOAD = b"clear-state-synthetic"


class Unproven(Exception):
    """Fail closed for absent or contradictory fixture evidence."""


@dataclass(frozen=True)
class Identity:
    volume: str = "vol-fixture"
    disk: int = 4
    physical_usb: str = "usb-fixture"
    msc: str = "msc-fixture"
    cdc: str = "cdc-fixture"
    history: str = "a" * 64


@dataclass(frozen=True)
class Assessment:
    verdict: str
    reason: str
    release_authorized: bool = False
    native_qualification: str = "NOT_RUN"


def fixture_image(data: bytes = PAYLOAD) -> bytes:
    """Minimal generated FAT32-like volume; *not* a production FAT32 decoder."""
    if not 0 < len(data) <= SECTOR:
        raise ValueError("bounded fixture content required")
    boot = bytearray(SECTOR)
    boot[11:13] = struct.pack("<H", SECTOR)
    boot[13] = 1  # sectors per cluster
    boot[14:16] = struct.pack("<H", 1)  # reserved sectors
    boot[16] = 1  # FAT copies (tiny synthetic fixture)
    boot[32:36] = struct.pack("<I", 8)  # total sectors
    boot[36:40] = struct.pack("<I", 1)  # FAT size in sectors
    boot[44:48] = struct.pack("<I", 2)  # root cluster
    boot[510:512] = b"\x55\xaa"
    fat = bytearray(SECTOR)
    for cluster, entry in enumerate((0x0FFFFFF8, 0x0FFFFFFF,
                                     0x0FFFFFFF, 0x0FFFFFFF)):
        struct.pack_into("<I", fat, cluster * 4, entry)
    root = bytearray(SECTOR)
    root[:11] = FILE_NAME
    root[11] = 0x20  # normal file
    root[26:28] = struct.pack("<H", 3)  # file begins at cluster 3
    root[28:32] = struct.pack("<I", len(data))
    return b"".join([bytes(boot), bytes(fat), bytes(root),
                     data.ljust(SECTOR, b"\x00"), bytes(SECTOR * 4)])


class FakeLockedHandle:
    """Immutable token identity represents a *mock*, not an OS handle."""
    def __init__(self, image: bytes):
        self.image = image
        self.token = object()
        self.live = True
        self.locked = True
        self.dismounted = True
        self.read_error = False

    def read(self, token: object, offset: int, size: int) -> bytes:
        if not self.live or token is not self.token or not self.locked or not self.dismounted:
            raise Unproven("LOCK_SESSION_LOST")
        if (type(offset) is not int or type(size) is not int or size <= 0 or
                offset < 0 or offset % SECTOR or size % SECTOR):
            raise Unproven("RAW_READ_UNALIGNED")
        if self.read_error or offset + size > len(self.image):
            raise Unproven("RAW_READ_INCOMPLETE")
        return self.image[offset:offset + size]


def parse_bounded_fixture(handle: FakeLockedHandle, token: object) -> bytes:
    """Parse only a deliberately constrained 8-sector FAT32-like fixture."""
    boot = handle.read(token, 0, SECTOR)
    if (boot[510:512] != b"\x55\xaa" or
            struct.unpack_from("<H", boot, 11)[0] != SECTOR or
            boot[13] != 1 or struct.unpack_from("<H", boot, 14)[0] != 1 or
            boot[16] != 1 or struct.unpack_from("<I", boot, 32)[0] != 8 or
            struct.unpack_from("<I", boot, 36)[0] != 1 or
            struct.unpack_from("<I", boot, 44)[0] != 2):
        raise Unproven("FAT32_GEOMETRY_UNSUPPORTED")
    fat = handle.read(token, SECTOR, SECTOR)
    root = handle.read(token, 2 * SECTOR, SECTOR)
    if (struct.unpack_from("<I", fat, 8)[0] & 0x0FFFFFFF < 0x0FFFFFF8 or
            struct.unpack_from("<I", fat, 12)[0] & 0x0FFFFFFF < 0x0FFFFFF8 or
            root[:11] != FILE_NAME or root[11] != 0x20 or root[32] != 0 or
            struct.unpack_from("<H", root, 20)[0] != 0 or
            struct.unpack_from("<H", root, 26)[0] != 3):
        raise Unproven("FAT32_DIRECTORY_OR_FAT_INVALID")
    length = struct.unpack_from("<I", root, 28)[0]
    if not 0 < length <= SECTOR:
        raise Unproven("FAT32_FILE_BOUNDS_INVALID")
    return handle.read(token, 3 * SECTOR, SECTOR)[:length]


def assess(first: Identity, observed: Identity, handle: FakeLockedHandle,
           token: object, *, leases: int = 0, writer_excluded: bool = False,
           namespace_unchanged: bool = True, expected: bytes = PAYLOAD) -> Assessment:
    """Source-only outcome. A fixture's writer claim is never native proof."""
    if first != observed or first.history != "a" * 64:
        return Assessment("BLOCKED", "TARGET_OR_HISTORY_DRIFT")
    if type(leases) is not int or leases != 0 or not writer_excluded:
        return Assessment("BLOCKED", "WRITER_OR_LEASE_UNPROVEN")
    if namespace_unchanged is not True:
        return Assessment("BLOCKED", "NAMESPACE_UNPROVEN")
    try:
        actual = parse_bounded_fixture(handle, token)
    except (Unproven, ValueError, struct.error) as exc:
        return Assessment("BLOCKED", str(exc))
    if hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest():
        return Assessment("BLOCKED", "MEDIA_CONTENT_DRIFT")
    return Assessment("SOURCE_TEST_ONLY", "MOCK_FIXTURE_MATCH_NOT_WRITE_ATTESTATION")


class WitnessFixtureTests(unittest.TestCase):
    def setUp(self):
        self.identity = Identity()
        self.handle = FakeLockedHandle(fixture_image())

    def inspect(self, **kwargs):
        return assess(self.identity, self.identity, self.handle,
                      self.handle.token, writer_excluded=True, **kwargs)

    def test_synthetic_match_never_authorizes_real_release(self):
        result = self.inspect()
        self.assertEqual(result.verdict, "SOURCE_TEST_ONLY")
        self.assertFalse(result.release_authorized)
        self.assertEqual(result.native_qualification, "NOT_RUN")

    def test_identity_changes_rejected(self):
        for name, replacement in (("volume", "other"), ("disk", 5),
                                  ("msc", "other"), ("cdc", "other"),
                                  ("physical_usb", "other"), ("history", "b" * 64)):
            with self.subTest(name=name):
                drift = replace(self.identity, **{name: replacement})
                self.assertEqual(assess(self.identity, drift, self.handle,
                    self.handle.token, writer_excluded=True).verdict, "BLOCKED")

    def test_no_writer_claim_no_send(self):
        self.assertEqual(assess(self.identity, self.identity, self.handle,
                         self.handle.token).verdict, "BLOCKED")
        self.assertEqual(self.inspect(leases=1).verdict, "BLOCKED")
        self.assertEqual(self.inspect(leases=True).verdict, "BLOCKED")

    def test_namespace_swap(self):
        self.assertEqual(self.inspect(namespace_unchanged=False).verdict, "BLOCKED")

    def test_lock_failure_and_lost_handle(self):
        for attribute in ("locked", "dismounted", "live"):
            with self.subTest(attribute=attribute):
                setattr(self.handle, attribute, False)
                self.assertEqual(self.inspect().verdict, "BLOCKED")
                setattr(self.handle, attribute, True)
        self.assertEqual(assess(self.identity, self.identity, self.handle,
            object(), writer_excluded=True).verdict, "BLOCKED")

    def test_alignment_and_bounds(self):
        for offset, count in ((1, 512), (0, 5), (-512, 512), (4096, 512)):
            with self.subTest(offset=offset, count=count):
                with self.assertRaises(Unproven):
                    self.handle.read(self.handle.token, offset, count)

    def test_partial_read_error(self):
        self.handle.read_error = True
        self.assertEqual(self.inspect().verdict, "BLOCKED")
        self.handle.read_error = False
        self.handle.image = self.handle.image[:3 * SECTOR]
        self.assertEqual(self.inspect().verdict, "BLOCKED")

    def test_corrupt_boot_or_fat(self):
        for offset in (11, 13, 16, 32, 36, 44, 510, SECTOR + 12):
            with self.subTest(offset=offset):
                image = bytearray(fixture_image())
                image[offset] ^= 0xFF
                self.handle.image = bytes(image)
                self.assertEqual(self.inspect().verdict, "BLOCKED")

    def test_corrupt_root_directory(self):
        for offset in (2 * SECTOR, 2 * SECTOR + 11, 2 * SECTOR + 26,
                       2 * SECTOR + 28, 2 * SECTOR + 32):
            with self.subTest(offset=offset):
                image = bytearray(fixture_image())
                image[offset] ^= 0xFF
                self.handle.image = bytes(image)
                self.assertEqual(self.inspect().verdict, "BLOCKED")

    def test_content_change_rejected(self):
        self.handle.image = fixture_image(b"unexpected-new-recording")
        self.assertEqual(self.inspect().verdict, "BLOCKED")

    def test_reverted_write_is_undetectable_in_final_image(self):
        # Deliberate counterexample: state equality cannot prove historical no-write.
        before = fixture_image()
        temporary = fixture_image(b"unauthorized-intermediate")
        self.assertNotEqual(before, temporary)
        self.handle.image = before  # fake writer reverted all changed sectors
        result = self.inspect()
        self.assertEqual(result.verdict, "SOURCE_TEST_ONLY")
        self.assertFalse(result.release_authorized)

    def test_device_writer_not_excluded_by_host_lock(self):
        result = assess(self.identity, self.identity, self.handle,
                        self.handle.token, writer_excluded=False)
        self.assertEqual(result.verdict, "BLOCKED")


if __name__ == "__main__":
    unittest.main()
