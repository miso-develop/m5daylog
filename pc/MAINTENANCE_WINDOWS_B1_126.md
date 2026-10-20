# #126 Phase B1a/B1b — non-authoritative Windows feasibility fixtures

Authority: [Integration's exact four-file B1 reservation](https://github.com/miso-develop/m5daylog/issues/126#issuecomment-6287483172); [B0 feasibility assessment](https://github.com/miso-develop/m5daylog/issues/126#issuecomment-6286983317); accepted [D-031 maintenance exception](https://github.com/miso-develop/m5daylog/issues/105#issuecomment-6267701110) and [S-004 lifecycle amendment](https://github.com/miso-develop/m5daylog/issues/37#issuecomment-6267717379).

This change adds **only tests, a source note, and isolated CI**. It does not implement a trusted `prove_locked_snapshot`, alter any host production code, or create a release executor. PR #125 still owns the unmerged maintenance coordinator and guard API. It is not a substitute for live OS, device, or Human Gate evidence. `SOURCE_TEST_ONLY` means exactly this test fixture matched: `release_authorized=False`, `native_qualification=NOT_RUN`, and guard directory-entry power-loss durability `NOT_RUN` remain independent facts.

## B1a: the intentionally limited fake witness

`test_maintenance_windows_b1_witness.py` creates a tiny 8-sector, FAT32-*like* byte image wholly in memory. It parses only fixed boot parameters, one FAT sector, one root directory sector, and one bounded file payload, using a mock retained-handle **object identity**, 512-byte aligned reads and a simulated lock+dismount state. It deliberately does **not** implement a general FAT32 filesystem verifier: it does not validate FAT32 cluster-count classification, FSInfo, directory recursion, long filenames, multi-FAT consistency, all allocation chains, recovery/unindexed sectors, concurrent content mutation, cache ordering or the actual Windows `ReadFile` ABI. Rejecting its narrow inconsistent cases demonstrates only that this *toy decoder* can fail closed.

The tests cover target volume/disk/MSC/CDC/USB/history changes, mock original-handle replacement, lost lock/dismount, nonzero outstanding leases, writer uncertainty, namespace swaps, sector-alignment and incomplete read errors, corrupt image, and changed payload. A counterexample constructs an intermediate unauthorized write followed by restoration of the **identical original final bytes**: even a bit-for-bit post-lock match cannot prove *there was no intervening write*. A simulated `writer_excluded` flag is test input, not an OS/Device attestation.

**Native proof obligations unresolved:** retained original volume file object after real `FSCTL_LOCK_VOLUME -> FSCTL_DISMOUNT_VOLUME`, post-dismount same-handle raw sector read support, exact logical FAT32 snapshot/namespace/physical medium binding, Windows lease closure and separate Device/external-writer quiescence, inability to change any observed byte after the proof and before send. The actual native ABI and every physical/virtual-volume IOCTL experiment are **NOT_RUN**. If no credible no-intervening-write witness exists, this remains `BLOCKED`, not an opportunity to relax D-031.

## B1b: process-level journal fixture, not power-loss durability

`test_maintenance_windows_b1_guard.py` creates a fixed-name marker **only under `tempfile.TemporaryDirectory`**. Test-only `os.open(O_CREAT|O_EXCL)` demonstrates competing process creation under a fixed authorized directory; artificial faults before/after create, write, flush and file `fsync` must fail closed. A different attempt ID cannot create a second marker. Invalid UUID/history/host token, alternate directory, injected ACL/permission denial, POSIX symlink/portable Windows reparse attributes, partial marker and attempted replay are denied. On Windows, a real NTFS `mklink /J` fixture must fail closed (CI sets `M5DAYLOG_REQUIRE_B1_JUNCTION=1`, so a Windows runner cannot silently skip a failed junction setup).

The fixture only checks preexisting namespace attributes at specific moments. It is **not** an atomic Windows directory ancestry/ACL/mountpoint assurance mechanism; an adversarial concurrent namespace swap, hard-link/volume-alias replay, permission ACL change, or stolen capability is not excluded. The `HOST` label is a synthetic test value, **not** an authenticated stable physical host-volume identity. File `fsync` and exclusive create show neither a durable *new directory entry* nor hardware cache/power-loss resilience, under NTFS or FAT32. No subprocess sends CDC bytes; loss of ACK is modeled only by refusal to replay a consumed/uncertain marker. The fault tests delete **only disposable fixture files** between subcases, never a production journal. No Windows host crash, reboot, power-cut, actual Device volume lock/dismount, raw disk access or physical acceptance occurs.

## Exact evidence matrix and integration stop line

| Gate | Evidence at B1 | Status |
| --- | --- | --- |
| Toy lock/session, bounded image parser, negative state tests | Pure simulated Python | `SOURCE_TEST_ONLY` if CI passes |
| Fake lease/target/MSC/CDC correlation | Mock values, never Device inventory | `SOURCE_TEST_ONLY` if CI passes |
| Same original **real** post-dismount volume handle `ReadFile` + authoritative full FAT32 inventory | Not exercised | `NOT_RUN` |
| No intervening/preflight-to-lock or physical Device/external write | Counterexample proves state equivalence insufficient | `BLOCKED` |
| Disposable host directory `O_EXCL` race/recovery/reparse checks | Synthetic/real temp-file operations only | `SOURCE_TEST_ONLY` if CI passes |
| Canonical persistent host-volume binding, Windows ACL/reparse/alias under adversarial races | No native trustworthy implementation | `BLOCKED` |
| NTFS/FAT32 directory-entry & controller-cache crash/power-loss durability | File flush only; no power-loss acceptance | `BLOCKED`, physical test `NOT_RUN` |
| Running D-031 firmware provenance / maintenance attempt approval | Out of scope | `BLOCKED_PROVENANCE` / `NOT_RUN` |
| Genuine LOCK/DISMOUNT, CDC, Device/physical acceptance | Never executed | `NOT_RUN`; 0 accepted cycles |

This matrix may be updated with **actual exact-head CI results** in the PR evidence. Tests in Windows CI run on Windows host *with synthetic inputs*; they are not a Win32 locked-volume qualification. Official API references: [FSCTL_LOCK_VOLUME](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_lock_volume), [FSCTL_DISMOUNT_VOLUME](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_dismount_volume), [FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers), [write-cache qualification](https://learn.microsoft.com/en-us/windows-hardware/drivers/storage/querying-for-the-write-cache-property).

**Stop rule:** never promote a fake handle, byte-equal image, journal `fsync`, fake write-exclusion flag or test process race into `prove_locked_snapshot=True`, release authorization, power-loss durability acceptance or `MAINTENANCE_RELEASE_ONLY`. Production witness/guard integration (B1c), modifying #125-owned paths or source modules, and all actual host-volume/Device commands require fresh Integration ownership, independently checked Engineering/Security, and, for host/physical manipulations, a separately approved bounded Human Gate. #126 remains open.
