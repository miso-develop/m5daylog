"""Task #87 regression for the production APP -> USB transfer barrier.

The test extracts and executes the real ``sd_mount_transfer_to_usb`` function
body against a minimal deterministic harness. It proves detached publication is
admitted only after recorder/Device-FS release proof already exists, and that
the synchronous storage switch must retire the remaining APP mount.
"""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[2]
SD_MOUNT_C = REPO / "firmware/components/recorder/sd_mount.c"


def _production_transfer_function() -> str:
    source = SD_MOUNT_C.read_text(encoding="utf-8")
    start = source.index("esp_err_t sd_mount_transfer_to_usb(void)")
    end = source.index("\nesp_err_t sd_mount_release_usb_storage", start)
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
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>

            typedef int esp_err_t;
            #define ESP_OK 0
            #define ESP_FAIL 1
            #define ESP_ERR_INVALID_STATE 3
            #define TINYUSB_MSC_STORAGE_MOUNT_USB 2

            #define CHECK(expr) do { \
                if (!(expr)) { \
                    fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__, __LINE__, #expr); \
                    exit(2); \
                } \
            } while (0)

            static int s_owner_lock;
            #define portENTER_CRITICAL(lock) ((void)(lock))
            #define portEXIT_CRITICAL(lock) ((void)(lock))

            static bool s_mounted = true;
            static bool s_usb_release_requested = true;
            static bool s_device_fs_released = true;
            static void *s_storage = (void *)1;

            static int g_transfer_calls;
            static int g_mode;

            bool sd_mount_is_mounted(void) {
                return s_mounted;
            }

            esp_err_t tinyusb_msc_set_storage_mount_point(void *storage,
                                                          int mount_point) {
                CHECK(storage == s_storage);
                CHECK(mount_point == TINYUSB_MSC_STORAGE_MOUNT_USB);
                g_transfer_calls++;

                /* Detached publication requires recorder release proof before
                 * the APP -> USB storage switch is admitted. */
                CHECK(s_mounted);
                CHECK(s_usb_release_requested);
                CHECK(s_device_fs_released);

                if (g_mode == 2) {
                    return ESP_FAIL;
                }
                if (g_mode == 1) {
                    /* Model a broken transfer that returns success without
                     * retiring the remaining APP mount. */
                    return ESP_OK;
                }

                /* Successful synchronous APP -> USB mount-point transition. */
                s_mounted = false;
                return ESP_OK;
            }
            """
        )
        + "\n"
        + _production_transfer_function()
        + "\n"
        + textwrap.dedent(
            r"""
            int main(int argc, char **argv) {
                esp_err_t result;
                CHECK(argc == 2);

                if (strcmp(argv[1], "success") == 0) {
                    g_mode = 0;
                    result = sd_mount_transfer_to_usb();
                    CHECK(result == ESP_OK);
                    CHECK(g_transfer_calls == 1);
                    CHECK(!s_mounted);
                    CHECK(s_usb_release_requested);
                    CHECK(s_device_fs_released);
                } else if (strcmp(argv[1], "missing-proof") == 0) {
                    s_device_fs_released = false;
                    result = sd_mount_transfer_to_usb();
                    CHECK(result == ESP_ERR_INVALID_STATE);
                    CHECK(g_transfer_calls == 0);
                    CHECK(s_mounted);
                    CHECK(s_usb_release_requested);
                    CHECK(!s_device_fs_released);
                } else if (strcmp(argv[1], "not-requested") == 0) {
                    s_usb_release_requested = false;
                    result = sd_mount_transfer_to_usb();
                    CHECK(result == ESP_ERR_INVALID_STATE);
                    CHECK(g_transfer_calls == 0);
                    CHECK(s_mounted);
                    CHECK(!s_usb_release_requested);
                    CHECK(s_device_fs_released);
                } else if (strcmp(argv[1], "missing-complete") == 0) {
                    g_mode = 1;
                    result = sd_mount_transfer_to_usb();
                    CHECK(result == ESP_ERR_INVALID_STATE);
                    CHECK(g_transfer_calls == 1);
                    CHECK(s_mounted);
                    CHECK(s_usb_release_requested);
                    CHECK(s_device_fs_released);
                } else if (strcmp(argv[1], "transfer-failure") == 0) {
                    g_mode = 2;
                    result = sd_mount_transfer_to_usb();
                    CHECK(result == ESP_FAIL);
                    CHECK(g_transfer_calls == 1);
                    CHECK(s_mounted);
                    CHECK(s_usb_release_requested);
                    CHECK(s_device_fs_released);
                } else {
                    CHECK(false);
                }
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )

    binary = tmp_path / "sd_mount_transfer_barrier"
    subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", str(harness), "-o", str(binary)],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize(
    "scenario",
    ["success", "missing-proof", "not-requested", "missing-complete", "transfer-failure"],
)
def test_production_transfer_barrier_order_and_postconditions(
    tmp_path: Path, scenario: str
) -> None:
    binary = _build(tmp_path)
    subprocess.run([str(binary), scenario], check=True, capture_output=True, text=True)
