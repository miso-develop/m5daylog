"""Task #87 regression for the real TinyUSB SCSI completion eject seam.

The linker START STOP wrapper is not a reliable observation point for a callback
invoked from TinyUSB's own translation unit. The production completion callback
must recognize only START STOP UNIT(load_eject=1,start=0), emit the Strategy 2
release request once, and leave non-eject commands non-authoritative.
"""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[2]
USB_OWNERSHIP_C = REPO / "firmware/components/recorder/usb_msc_ownership.c"


def _production_eject_functions() -> str:
    source = USB_OWNERSHIP_C.read_text(encoding="utf-8")
    helper_start = source.index("static bool usb_request_explicit_eject(")
    helper_end = source.index("\n// Compatibility seam for the original implementation.", helper_start)
    cb_start = source.index("void tud_msc_scsi_complete_cb(", helper_end)
    cb_end = source.index("\nesp_err_t usb_msc_ownership_init", cb_start)
    return source[helper_start:helper_end] + "\n" + source[cb_start:cb_end]


def _build(tmp_path: Path) -> Path:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("host C compiler is unavailable")

    harness = tmp_path / "harness.c"
    harness.write_text(
        textwrap.dedent(
            r"""
            #include <stdbool.h>
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>

            #define USB_BIT_RELEASE_REQUESTED (1u << 3)
            #define USB_BIT_FAILED (1u << 5)
            #define USB_SCSI_CMD_START_STOP_UNIT 0x1bu

            typedef struct event_group { uint32_t bits; } *EventGroupHandle_t;
            static struct event_group g_events;
            static EventGroupHandle_t s_usb_events = &g_events;
            static volatile bool s_initialized = true;
            static volatile bool s_started = true;
            static volatile bool s_host_owned = true;
            static volatile bool s_release_pending = false;
            static bool g_mounted = false;
            static int g_disconnect_calls;

            #define CHECK(expr) do { \
                if (!(expr)) { \
                    fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
                    exit(2); \
                } \
            } while (0)
            #define ESP_LOGI(...) ((void)0)

            uint32_t xEventGroupSetBits(EventGroupHandle_t group, uint32_t bits) {
                group->bits |= bits;
                return group->bits;
            }
            bool sd_mount_is_mounted(void) { return g_mounted; }
            bool tud_disconnect(void) { g_disconnect_calls++; return true; }
            static void usb_fail(const char *reason) {
                (void)reason;
                xEventGroupSetBits(s_usb_events, USB_BIT_FAILED);
            }
            """
        )
        + "\n"
        + _production_eject_functions()
        + "\n"
        + textwrap.dedent(
            r"""
            static void reset_state(void) {
                memset(&g_events, 0, sizeof(g_events));
                s_initialized = true;
                s_started = true;
                s_host_owned = true;
                s_release_pending = false;
                g_mounted = false;
                g_disconnect_calls = 0;
            }

            int main(int argc, char **argv) {
                uint8_t cdb[16] = {0};
                CHECK(argc == 2);
                reset_state();

                if (strcmp(argv[1], "eject") == 0) {
                    cdb[0] = USB_SCSI_CMD_START_STOP_UNIT;
                    cdb[4] = 0x02u; /* LOEJ=1, START=0 */
                    tud_msc_scsi_complete_cb(0, cdb);
                    CHECK(s_release_pending);
                    CHECK(g_disconnect_calls == 1);
                    CHECK((g_events.bits & USB_BIT_RELEASE_REQUESTED) != 0);
                    CHECK((g_events.bits & USB_BIT_FAILED) == 0);

                    /* Duplicate observation is idempotent. */
                    tud_msc_scsi_complete_cb(0, cdb);
                    CHECK(g_disconnect_calls == 1);
                } else if (strcmp(argv[1], "start") == 0) {
                    cdb[0] = USB_SCSI_CMD_START_STOP_UNIT;
                    cdb[4] = 0x03u; /* LOEJ=1, START=1: not release */
                    tud_msc_scsi_complete_cb(0, cdb);
                    CHECK(!s_release_pending);
                    CHECK(g_disconnect_calls == 0);
                    CHECK(g_events.bits == 0);
                } else if (strcmp(argv[1], "other") == 0) {
                    cdb[0] = 0x00u;
                    tud_msc_scsi_complete_cb(0, cdb);
                    CHECK(!s_release_pending);
                    CHECK(g_disconnect_calls == 0);
                    CHECK(g_events.bits == 0);
                } else {
                    CHECK(false);
                }
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )

    binary = tmp_path / "usb_scsi_eject_callback"
    subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", str(harness), "-o", str(binary)],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize("scenario", ["eject", "start", "other"])
def test_production_scsi_completion_authorizes_only_explicit_eject(
    tmp_path: Path, scenario: str
) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
