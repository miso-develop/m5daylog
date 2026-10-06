"""Task #87 regression for the provisional TinyUSB ATTACHED callback.

esp_tinyusb invokes ATTACHED from tud_mount_cb before TinyUSB sends the
SET_CONFIGURATION status stage. The callback may record provisional state but
must not disconnect, signal recorder preparation, or mutate storage ownership.
A later completed MSC command supplies class-binding proof.
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
    end = source.index("\nstatic bool usb_request_explicit_eject", start)
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
            #define USB_BIT_HOST_OWNED (1u << 2)
            #define USB_BIT_FAILED (1u << 5)

            typedef struct event_group {
                uint32_t bits;
            } *EventGroupHandle_t;

            static struct event_group g_events;
            static EventGroupHandle_t s_usb_events = &g_events;
            static volatile bool s_initialized = true;
            static volatile bool s_started = true;
            static volatile bool s_starting = false;
            static volatile bool s_storage_usb_owned = false;
            static volatile bool s_host_owned = false;
            static volatile bool s_release_pending = false;
            static volatile bool s_provisional_attached = false;
            static volatile bool s_publish_triggered = false;
            static volatile bool s_prepare_disconnected = false;
            static bool g_mounted = true;
            static int g_disconnect_calls;
            static bool g_disconnect_result = true;

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

            bool tud_disconnect(void) {
                g_disconnect_calls++;
                return g_disconnect_result;
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
                s_initialized = true;
                s_started = true;
                s_starting = false;
                s_storage_usb_owned = false;
                s_host_owned = false;
                s_release_pending = false;
                s_provisional_attached = false;
                s_publish_triggered = false;
                s_prepare_disconnected = false;
                g_mounted = true;
                g_disconnect_calls = 0;
                g_disconnect_result = true;
            }

            int main(int argc, char **argv) {
                tinyusb_event_t attached = { .id = TINYUSB_EVENT_ATTACHED };
                CHECK(argc == 2);
                reset_state();

                if (strcmp(argv[1], "first-attach") == 0) {
                    usb_device_event_cb(&attached, NULL);
                    CHECK(g_disconnect_calls == 0);
                    CHECK((g_events.bits & USB_BIT_ATTACH) == 0);
                    CHECK((g_events.bits & USB_BIT_FAILED) == 0);
                    CHECK(s_provisional_attached);
                    CHECK(!s_publish_triggered);
                    CHECK(!s_host_owned);
                    CHECK(g_mounted);
                } else if (strcmp(argv[1], "start-in-progress") == 0) {
                    s_started = false;
                    s_starting = true;
                    usb_device_event_cb(&attached, NULL);
                    CHECK(g_disconnect_calls == 0);
                    CHECK((g_events.bits & USB_BIT_ATTACH) == 0);
                    CHECK((g_events.bits & USB_BIT_FAILED) == 0);
                    CHECK(s_provisional_attached);
                    CHECK(!s_publish_triggered);
                    CHECK(!s_host_owned);
                    CHECK(g_mounted);
                } else if (strcmp(argv[1], "duplicate-provisional") == 0) {
                    usb_device_event_cb(&attached, NULL);
                    usb_device_event_cb(&attached, NULL);
                    CHECK(g_disconnect_calls == 0);
                    CHECK((g_events.bits & USB_BIT_ATTACH) == 0);
                    CHECK((g_events.bits & USB_BIT_FAILED) != 0);
                    CHECK(s_provisional_attached);
                    CHECK(!s_publish_triggered);
                    CHECK(!s_host_owned);
                    CHECK(g_mounted);
                } else if (strcmp(argv[1], "provisional-detach") == 0) {
                    tinyusb_event_t detached = { .id = TINYUSB_EVENT_DETACHED };
                    usb_device_event_cb(&attached, NULL);
                    CHECK(s_provisional_attached);
                    usb_device_event_cb(&detached, NULL);
                    CHECK(!s_provisional_attached);
                    CHECK(!s_publish_triggered);
                    CHECK(g_disconnect_calls == 0);
                    CHECK(g_events.bits == 0);
                    CHECK(g_mounted);
                } else if (strcmp(argv[1], "prepared-reconnect") == 0) {
                    s_provisional_attached = true;
                    s_publish_triggered = true;
                    s_prepare_disconnected = true;
                    s_storage_usb_owned = true;
                    g_mounted = false;
                    usb_device_event_cb(&attached, NULL);
                    CHECK(g_disconnect_calls == 0);
                    CHECK((g_events.bits & USB_BIT_ATTACH) == 0);
                    CHECK((g_events.bits & USB_BIT_HOST_OWNED) != 0);
                    CHECK((g_events.bits & USB_BIT_FAILED) == 0);
                    CHECK(s_host_owned);
                    CHECK(!s_provisional_attached);
                    CHECK(!s_prepare_disconnected);
                    CHECK(!g_mounted);
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


@pytest.mark.parametrize(
    "scenario",
    [
        "first-attach",
        "start-in-progress",
        "duplicate-provisional",
        "provisional-detach",
        "prepared-reconnect",
    ]
)
def test_production_attached_callback_preserves_set_configuration_status(
    tmp_path: Path, scenario: str
) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
