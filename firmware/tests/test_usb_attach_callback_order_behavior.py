"""Task #87 regression for real TinyUSB SetConfiguration callback ordering.

esp_tinyusb 2.2.1 publishes ``TINYUSB_EVENT_ATTACHED`` from its strong
``tud_mount_cb``.  TinyUSB invokes that callback while handling
SET_CONFIGURATION and before completing the control request.  With
``auto_mount_off=1`` the storage is still APP-owned at this point.

The production device-event callback must therefore enter the existing
APP -> USB transfer barrier from this ATTACHED callback.  Treating this order as
an error reproduces the physical failure seen at Human Gate: RECORDING -> ERROR
without USB_PREPARE and without durable HOST_UNRESOLVED.
"""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[2]
USB_OWNERSHIP_C = REPO / "firmware/components/recorder/usb_msc_ownership.c"


def _production_device_event_callback() -> str:
    source = USB_OWNERSHIP_C.read_text(encoding="utf-8")
    start = source.index("static void usb_device_event_cb(")
    end = source.index("\n// esp_tinyusb 2.2.1", start)
    return source[start:end]


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

            typedef int esp_err_t;
            #define ESP_OK 0
            #define ESP_FAIL 1

            typedef enum {
                TINYUSB_EVENT_ATTACHED = 1,
                TINYUSB_EVENT_DETACHED = 2,
                TINYUSB_EVENT_SUSPENDED = 3,
            } tinyusb_event_id_t;
            typedef struct { tinyusb_event_id_t id; } tinyusb_event_t;

            #define USB_BIT_ATTACH (1u << 0)
            #define USB_BIT_FAILED (1u << 5)

            typedef struct event_group {
                uint32_t bits;
            } *EventGroupHandle_t;

            static struct event_group g_events;
            static EventGroupHandle_t s_usb_events = &g_events;
            static volatile bool s_host_owned = false;
            static volatile bool s_release_pending = false;
            static bool g_mounted = true;
            static int g_transfer_calls;
            static int g_disconnect_calls;
            static esp_err_t g_transfer_result = ESP_OK;

            #define CHECK(expr) do { \
                if (!(expr)) { \
                    fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
                    exit(2); \
                } \
            } while (0)

            #define ESP_LOGI(...) ((void)0)
            #define ESP_LOGW(...) ((void)0)
            #define ESP_LOGE(...) ((void)0)

            uint32_t xEventGroupSetBits(EventGroupHandle_t group, uint32_t bits) {
                group->bits |= bits;
                return group->bits;
            }

            bool sd_mount_is_mounted(void) {
                return g_mounted;
            }

            esp_err_t sd_mount_transfer_to_usb(void) {
                g_transfer_calls++;
                if (g_transfer_result != ESP_OK) {
                    return g_transfer_result;
                }

                /* Model production MOUNT_START -> coordinator ATTACH and the
                 * eventual MOUNT_COMPLETE after release proof is supplied. */
                xEventGroupSetBits(s_usb_events, USB_BIT_ATTACH);
                g_mounted = false;
                s_host_owned = true;
                return ESP_OK;
            }

            bool tud_disconnect(void) {
                g_disconnect_calls++;
                return true;
            }

            static void usb_fail(const char *reason) {
                (void)reason;
                xEventGroupSetBits(s_usb_events, USB_BIT_FAILED);
            }
            """
        )
        + "\n"
        + _production_device_event_callback()
        + "\n"
        + textwrap.dedent(
            r"""
            static void reset_state(void) {
                memset(&g_events, 0, sizeof(g_events));
                s_host_owned = false;
                s_release_pending = false;
                g_mounted = true;
                g_transfer_calls = 0;
                g_disconnect_calls = 0;
                g_transfer_result = ESP_OK;
            }

            int main(int argc, char **argv) {
                tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };
                CHECK(argc == 2);
                reset_state();

                if (strcmp(argv[1], "real-order") == 0) {
                    usb_device_event_cb(&attached, NULL);
                    CHECK(g_transfer_calls == 1);
                    CHECK(g_disconnect_calls == 0);
                    CHECK((g_events.bits & USB_BIT_ATTACH) != 0);
                    CHECK((g_events.bits & USB_BIT_FAILED) == 0);
                    CHECK(s_host_owned);
                    CHECK(!g_mounted);
                } else if (strcmp(argv[1], "transfer-failure") == 0) {
                    g_transfer_result = ESP_FAIL;
                    usb_device_event_cb(&attached, NULL);
                    CHECK(g_transfer_calls == 1);
                    CHECK(g_disconnect_calls == 1);
                    CHECK((g_events.bits & USB_BIT_FAILED) != 0);
                    CHECK(!s_host_owned);
                    CHECK(g_mounted);
                } else {
                    CHECK(false);
                }
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )

    binary = tmp_path / "usb_attach_callback_order"
    subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", str(harness), "-o", str(binary)],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize("scenario", ["real-order", "transfer-failure"])
def test_production_attached_callback_enters_transfer_barrier(
    tmp_path: Path, scenario: str
) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
