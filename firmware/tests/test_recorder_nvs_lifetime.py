"""Task #50 REV-83-12 shared default-NVS lifetime regressions."""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
INCLUDE = COMP / "include"


def test_recorder_nvs_process_lifetime_and_serialization(tmp_path: Path) -> None:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("host C compiler is unavailable")

    stubs = tmp_path / "stubs"
    (stubs / "freertos").mkdir(parents=True)
    (stubs / "esp_err.h").write_text(
        textwrap.dedent(
            r"""
            #pragma once
            typedef int esp_err_t;
            #define ESP_OK 0
            #define ESP_FAIL 1
            #define ESP_ERR_INVALID_STATE 2
            #define ESP_ERR_NO_MEM 3
            """
        ),
        encoding="utf-8",
    )
    (stubs / "freertos" / "FreeRTOS.h").write_text(
        textwrap.dedent(
            r"""
            #pragma once
            #include <stdint.h>
            typedef int BaseType_t;
            typedef uint32_t TickType_t;
            #define pdTRUE 1
            #define pdFALSE 0
            #define portMAX_DELAY UINT32_MAX
            """
        ),
        encoding="utf-8",
    )
    (stubs / "freertos" / "semphr.h").write_text(
        textwrap.dedent(
            r"""
            #pragma once
            #include "freertos/FreeRTOS.h"
            struct test_sem;
            typedef struct test_sem *SemaphoreHandle_t;
            SemaphoreHandle_t xSemaphoreCreateMutex(void);
            BaseType_t xSemaphoreTake(SemaphoreHandle_t sem, TickType_t ticks);
            BaseType_t xSemaphoreGive(SemaphoreHandle_t sem);
            """
        ),
        encoding="utf-8",
    )
    (stubs / "nvs_flash.h").write_text(
        textwrap.dedent(
            r"""
            #pragma once
            #include "esp_err.h"
            esp_err_t nvs_flash_init(void);
            esp_err_t nvs_flash_deinit(void);
            """
        ),
        encoding="utf-8",
    )

    harness = tmp_path / "recorder_nvs_harness.c"
    harness.write_text(
        textwrap.dedent(
            r"""
            #include <pthread.h>
            #include <stdbool.h>
            #include <stdio.h>
            #include <stdlib.h>
            #include <time.h>

            #include "freertos/FreeRTOS.h"
            #include "freertos/semphr.h"
            #include "recorder_nvs.h"

            #define CHECK(expr) do {                 if (!(expr)) {                     fprintf(stderr, "CHECK failed %s:%d: %s\n",                             __FILE__, __LINE__, #expr);                     exit(2);                 }             } while (0)

            struct test_sem {
                pthread_mutex_t mutex;
            };

            static pthread_mutex_t g_state_mutex = PTHREAD_MUTEX_INITIALIZER;
            static pthread_cond_t g_state_cv = PTHREAD_COND_INITIALIZER;
            static int g_init_calls;
            static int g_deinit_calls;
            static bool g_rtc_inside;
            static bool g_release_rtc;
            static bool g_trace_attempted;
            static bool g_trace_entered;
            static bool g_overlap;

            SemaphoreHandle_t xSemaphoreCreateMutex(void) {
                struct test_sem *sem = calloc(1u, sizeof(*sem));
                CHECK(sem != NULL);
                CHECK(pthread_mutex_init(&sem->mutex, NULL) == 0);
                return sem;
            }

            BaseType_t xSemaphoreTake(SemaphoreHandle_t sem, TickType_t ticks) {
                (void)ticks;
                return pthread_mutex_lock(&sem->mutex) == 0 ? pdTRUE : pdFALSE;
            }

            BaseType_t xSemaphoreGive(SemaphoreHandle_t sem) {
                return pthread_mutex_unlock(&sem->mutex) == 0 ? pdTRUE : pdFALSE;
            }

            esp_err_t nvs_flash_init(void) {
                pthread_mutex_lock(&g_state_mutex);
                g_init_calls++;
                pthread_mutex_unlock(&g_state_mutex);
                return ESP_OK;
            }

            esp_err_t nvs_flash_deinit(void) {
                pthread_mutex_lock(&g_state_mutex);
                g_deinit_calls++;
                pthread_mutex_unlock(&g_state_mutex);
                return ESP_OK;
            }

            static void *rtc_pending_commit(void *arg) {
                (void)arg;
                CHECK(recorder_nvs_lock() == ESP_OK);
                pthread_mutex_lock(&g_state_mutex);
                g_rtc_inside = true;
                pthread_cond_broadcast(&g_state_cv);
                while (!g_release_rtc) {
                    pthread_cond_wait(&g_state_cv, &g_state_mutex);
                }
                g_rtc_inside = false;
                pthread_mutex_unlock(&g_state_mutex);
                recorder_nvs_unlock();
                return NULL;
            }

            static void *scsi_trace_flush(void *arg) {
                (void)arg;
                pthread_mutex_lock(&g_state_mutex);
                g_trace_attempted = true;
                pthread_cond_broadcast(&g_state_cv);
                pthread_mutex_unlock(&g_state_mutex);
                CHECK(recorder_nvs_lock() == ESP_OK);
                pthread_mutex_lock(&g_state_mutex);
                if (g_rtc_inside) {
                    g_overlap = true;
                }
                g_trace_entered = true;
                pthread_cond_broadcast(&g_state_cv);
                pthread_mutex_unlock(&g_state_mutex);
                recorder_nvs_unlock();
                return NULL;
            }

            int main(void) {
                pthread_t rtc_thread;
                pthread_t trace_thread;
                struct timespec pause = {.tv_sec = 0, .tv_nsec = 30000000L};

                /* Production boot initializes the default partition once. */
                CHECK(recorder_nvs_init() == ESP_OK);
                CHECK(recorder_nvs_init() == ESP_OK);
                CHECK(g_init_calls == 1);
                CHECK(g_deinit_calls == 0);

                /* Production ordering: a diagnostic transaction followed by
                 * the later SET_TIME durable-pending transaction remains valid
                 * in the same process-lifetime NVS instance. */
                CHECK(recorder_nvs_lock() == ESP_OK);
                recorder_nvs_unlock();
                CHECK(recorder_nvs_lock() == ESP_OK);
                recorder_nvs_unlock();
                CHECK(g_init_calls == 1);
                CHECK(g_deinit_calls == 0);

                /* Concurrency model: SCSI persistence cannot enter while the
                 * RTC durable-pending transaction owns the recorder NVS lock. */
                CHECK(pthread_create(&rtc_thread, NULL,
                                     rtc_pending_commit, NULL) == 0);
                pthread_mutex_lock(&g_state_mutex);
                while (!g_rtc_inside) {
                    pthread_cond_wait(&g_state_cv, &g_state_mutex);
                }
                pthread_mutex_unlock(&g_state_mutex);

                CHECK(pthread_create(&trace_thread, NULL,
                                     scsi_trace_flush, NULL) == 0);
                pthread_mutex_lock(&g_state_mutex);
                while (!g_trace_attempted) {
                    pthread_cond_wait(&g_state_cv, &g_state_mutex);
                }
                pthread_mutex_unlock(&g_state_mutex);
                nanosleep(&pause, NULL);

                pthread_mutex_lock(&g_state_mutex);
                CHECK(g_trace_attempted);
                CHECK(!g_trace_entered);
                g_release_rtc = true;
                pthread_cond_broadcast(&g_state_cv);
                pthread_mutex_unlock(&g_state_mutex);

                CHECK(pthread_join(rtc_thread, NULL) == 0);
                CHECK(pthread_join(trace_thread, NULL) == 0);

                pthread_mutex_lock(&g_state_mutex);
                CHECK(g_trace_entered);
                CHECK(!g_overlap);
                CHECK(g_init_calls == 1);
                CHECK(g_deinit_calls == 0);
                pthread_mutex_unlock(&g_state_mutex);

                puts("recorder NVS lifetime/serialization: PASS");
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )

    exe = tmp_path / "recorder_nvs_lifetime"
    build = subprocess.run(
        [
            cc,
            "-std=c11",
            "-D_POSIX_C_SOURCE=200809L",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-DESP_PLATFORM",
            "-pthread",
            "-I",
            str(stubs),
            "-I",
            str(INCLUDE),
            str(COMP / "recorder_nvs.c"),
            str(harness),
            "-o",
            str(exe),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr

    run = subprocess.run(
        [str(exe)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "recorder NVS lifetime/serialization: PASS" in run.stdout


def test_recorder_nvs_clients_use_one_guard_without_deinit() -> None:
    runtime = (REPO / "firmware/main/task49_runtime.c").read_text(encoding="utf-8")
    rtc = (COMP / "rtc_correction.c").read_text(encoding="utf-8")
    shutdown = (COMP / "shutdown_armed.c").read_text(encoding="utf-8")
    ownership = (COMP / "usb_msc_ownership.c").read_text(encoding="utf-8")
    cmake = (COMP / "CMakeLists.txt").read_text(encoding="utf-8")

    assert '"recorder_nvs.c"' in cmake
    assert '#include "recorder_nvs.h"' in runtime
    assert runtime.index("recorder_nvs_init()") < runtime.index(
        "shutdown_armed_boot_action("
    )

    for source in (rtc, shutdown, ownership):
        assert '#include "recorder_nvs.h"' in source
        assert "nvs_flash_init" not in source
        assert "nvs_flash_deinit" not in source
        assert "recorder_nvs_lock()" in source
        assert "recorder_nvs_unlock()" in source

    apply_at = rtc.index("rtc_correction_result_t rtc_correction_apply(")
    clear_at = rtc.index("static esp_err_t rtc_correction_clear_pending_nvs", apply_at)
    apply = rtc[apply_at:clear_at]
    assert apply.index("recorder_nvs_lock()") < apply.index(
        "nvs_open(RTC_NVS_NAMESPACE"
    )
    assert apply.index("nvs_commit(handle)") < apply.index(
        "recorder_nvs_unlock()"
    )

    trace_at = ownership.index("esp_err_t usb_msc_ownership_flush_scsi_trace")
    trace = ownership[trace_at:]
    assert trace.index("recorder_nvs_lock()") < trace.index(
        'nvs_open("m5daylog"'
    )
    commit_at = trace.index("nvs_commit(handle)")
    assert trace.index("recorder_nvs_unlock()", commit_at) > commit_at
