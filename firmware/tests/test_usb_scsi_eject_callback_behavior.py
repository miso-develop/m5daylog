"""Task #87 regression for TinyUSB's post-status SCSI completion seam.

Before publication, the first completed MSC TEST UNIT READY is class-binding
proof and may trigger recorder preparation. After USB ownership is established,
all SCSI completions, including START STOP UNIT(load_eject=1,start=0), are
diagnostic-only. D-031 release authority belongs exclusively to canonical CDC
RELEASE_STORAGE.
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
    helper_start = source.index("static bool usb_note_initial_msc_command_complete(")
    cdc_helpers = source.index("static bool usb_release_attempt_id_valid", helper_start)
    scsi_wrapper = source.index("bool __wrap_tud_msc_start_stop_cb", cdc_helpers)
    cb_end = source.index("\nesp_err_t usb_msc_ownership_init", scsi_wrapper)
    return source[helper_start:cdc_helpers] + "\n" + source[scsi_wrapper:cb_end]


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
            #include <stdatomic.h>
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>

            #define USB_BIT_ATTACH (1u << 0)
            #define USB_BIT_RELEASE_REQUESTED (1u << 3)
            #define USB_BIT_FAILED (1u << 5)
            #define USB_SCSI_CMD_TEST_UNIT_READY 0x00u
            #define USB_SCSI_CMD_START_STOP_UNIT 0x1bu

            typedef struct event_group { uint32_t bits; } *EventGroupHandle_t;
            static struct event_group g_events;
            static EventGroupHandle_t s_usb_events = &g_events;
            static volatile bool s_initialized = true;
            static volatile bool s_starting = false;
            static volatile bool s_started = true;
            static volatile bool s_storage_usb_owned = true;
            static volatile bool s_host_owned = true;
            static _Atomic bool s_release_pending = false;
            static volatile bool s_provisional_attached = false;
            static volatile bool s_publish_triggered = true;
            static bool g_mounted = false;
            static int g_disconnect_calls;
            static int g_trace_complete_calls;
            static uint8_t g_trace_last_opcode;
            static uint8_t g_trace_last_byte4;

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
            static void usb_scsi_trace_note_command_complete(
                uint8_t const scsi_cmd[16]) {
                g_trace_complete_calls++;
                g_trace_last_opcode = scsi_cmd[0];
                g_trace_last_byte4 = scsi_cmd[4];
            }
            static void usb_scsi_trace_note_start_stop_request(
                uint8_t power_condition, bool start, bool load_eject) {
                (void)power_condition;
                (void)start;
                (void)load_eject;
            }
            bool __real_tud_msc_start_stop_cb(uint8_t lun,
                                              uint8_t power_condition,
                                              bool start,
                                              bool load_eject) {
                (void)lun;
                (void)power_condition;
                (void)start;
                (void)load_eject;
                return true;
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
                s_starting = false;
                s_started = true;
                s_storage_usb_owned = true;
                s_host_owned = true;
                s_release_pending = false;
                s_provisional_attached = false;
                s_publish_triggered = true;
                g_mounted = false;
                g_disconnect_calls = 0;
                g_trace_complete_calls = 0;
                g_trace_last_opcode = 0;
                g_trace_last_byte4 = 0;
            }

            int main(int argc, char **argv) {
                uint8_t cdb[16] = {0};
                CHECK(argc == 2);
                reset_state();

                if (strcmp(argv[1], "probe") == 0) {
                    s_storage_usb_owned = false;
                    s_host_owned = false;
                    s_provisional_attached = true;
                    s_publish_triggered = false;
                    g_mounted = true;
                    cdb[0] = 0x00u; /* TEST UNIT READY */
                    tud_msc_scsi_complete_cb(0, cdb);
                    CHECK(s_publish_triggered);
                    CHECK((g_events.bits & USB_BIT_ATTACH) != 0);
                    CHECK((g_events.bits & USB_BIT_RELEASE_REQUESTED) == 0);
                    CHECK((g_events.bits & USB_BIT_FAILED) == 0);
                    CHECK(!s_release_pending);
                    CHECK(g_disconnect_calls == 0);
                    CHECK(g_trace_complete_calls == 0);
                } else if (strcmp(argv[1], "eject") == 0) {
                    cdb[0] = USB_SCSI_CMD_START_STOP_UNIT;
                    cdb[4] = 0x02u; /* LOEJ=1, START=0 */
                    tud_msc_scsi_complete_cb(0, cdb);
                    CHECK(!s_release_pending);
                    CHECK(g_disconnect_calls == 0);
                    CHECK((g_events.bits & USB_BIT_RELEASE_REQUESTED) == 0);
                    CHECK((g_events.bits & USB_BIT_FAILED) == 0);
                    CHECK(g_trace_complete_calls == 1);
                    CHECK(g_trace_last_opcode == USB_SCSI_CMD_START_STOP_UNIT);
                    CHECK(g_trace_last_byte4 == 0x02u);

                    /* Duplicate shell/SCSI observations remain non-authoritative. */
                    tud_msc_scsi_complete_cb(0, cdb);
                    CHECK(!s_release_pending);
                    CHECK((g_events.bits & USB_BIT_RELEASE_REQUESTED) == 0);
                    CHECK(g_disconnect_calls == 0);
                    CHECK(g_trace_complete_calls == 2);
                } else if (strcmp(argv[1], "start") == 0) {
                    cdb[0] = USB_SCSI_CMD_START_STOP_UNIT;
                    cdb[4] = 0x03u; /* LOEJ=1, START=1: not release */
                    tud_msc_scsi_complete_cb(0, cdb);
                    CHECK(!s_release_pending);
                    CHECK(g_disconnect_calls == 0);
                    CHECK(g_events.bits == 0);
                    CHECK(g_trace_complete_calls == 1);
                    CHECK(g_trace_last_byte4 == 0x03u);
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


@pytest.mark.parametrize("scenario", ["probe", "eject", "start", "other"])
def test_production_scsi_completion_is_observation_only_after_publication(
    tmp_path: Path, scenario: str
) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
