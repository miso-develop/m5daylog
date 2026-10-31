#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "nvs.h"
#include "recorder_nvs.h"
#include "release_cycle_fence.h"

#define CHECK(e) do { if (!(e)) { \
    fprintf(stderr, "CHECK %s:%d %s\n", __FILE__, __LINE__, #e); \
    exit(2); } } while (0)

static bool g_has_lifecycle;
static bool g_has_fence;
static uint8_t g_lifecycle;
static uint8_t g_fence;
static uint8_t g_next;
static bool g_write;
static bool g_locked;
static bool g_commit_before;
static bool g_commit_after;
static bool g_fail_set;
static bool g_fail_lock;

esp_err_t recorder_nvs_init(void) { return ESP_OK; }
esp_err_t recorder_nvs_lock(void) {
    if (g_fail_lock) return ESP_FAIL;
    CHECK(!g_locked);
    g_locked = true;
    return ESP_OK;
}
void recorder_nvs_unlock(void) { CHECK(g_locked); g_locked = false; }
esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *out) {
    CHECK(g_locked && strcmp(name, "m5daylog") == 0);
    CHECK(mode == NVS_READONLY || mode == NVS_READWRITE);
    *out = 7;
    return ESP_OK;
}
void nvs_close(nvs_handle_t h) { CHECK(h == 7 && g_locked); }
esp_err_t nvs_get_u8(nvs_handle_t h, const char *key, uint8_t *out) {
    CHECK(h == 7 && g_locked);
    if (strcmp(key, "shutdown_armed") == 0) {
        if (!g_has_lifecycle) return ESP_ERR_NVS_NOT_FOUND;
        *out = g_lifecycle;
        return ESP_OK;
    }
    CHECK(strcmp(key, "release_boot") == 0);
    if (!g_has_fence) return ESP_ERR_NVS_NOT_FOUND;
    *out = g_fence;
    return ESP_OK;
}
esp_err_t nvs_set_u8(nvs_handle_t h, const char *key, uint8_t val) {
    CHECK(h == 7 && g_locked && strcmp(key, "release_boot") == 0);
    if (g_fail_set) return ESP_FAIL;
    g_next = val;
    g_write = true;
    return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t h) {
    CHECK(h == 7 && g_locked && g_write);
    g_write = false;
    if (g_commit_before) return ESP_FAIL;
    g_has_fence = true;
    g_fence = g_next;
    if (g_commit_after) return ESP_FAIL;
    return ESP_OK;
}
int main(int argc, char **argv) {
    esp_err_t begin;
    esp_err_t stage = ESP_ERR_NOT_SUPPORTED;
    esp_err_t host_before = 0, host = 0, release = 0, duplicate = 0;
    const bool is_fu_stage = strncmp(argv[1], "fu_", 3u) == 0;
    CHECK(argc == 5);
    g_has_lifecycle = strcmp(argv[2], "missing") != 0;
    if (g_has_lifecycle) g_lifecycle = (uint8_t)atoi(argv[2]);
    g_has_fence = strcmp(argv[3], "missing") != 0;
    if (g_has_fence) g_fence = (uint8_t)atoi(argv[3]);
    g_commit_before = strcmp(argv[4], "commit_before") == 0;
    g_commit_after = strcmp(argv[4], "commit_after") == 0;
    g_fail_set = strcmp(argv[4], "set") == 0;
    g_fail_lock = strcmp(argv[4], "lock") == 0;
    begin = release_cycle_fence_begin_boot(is_fu_stage
        ? false : strcmp(argv[1], "normal") != 0);
    if (is_fu_stage && begin == ESP_OK) {
        stage = release_cycle_fence_stage_first_use();
        if (strcmp(argv[1], "fu_complete") == 0 && stage == ESP_OK) {
            release_cycle_fence_note_canonical_release_accepted();
            g_lifecycle = 1; /* durable accepted release + SHUTDOWN_ARMED */
            release = release_cycle_fence_note_release_completed();
        } else if (strcmp(argv[1], "fu_unaccepted") == 0 &&
                   stage == ESP_OK) {
            g_lifecycle = 1;
            release = release_cycle_fence_note_release_completed();
        } else if (strcmp(argv[1], "fu_cutoff") == 0 &&
                   stage == ESP_OK) {
            release_cycle_fence_note_physical_session_cutoff();
            release_cycle_fence_note_canonical_release_accepted();
            g_lifecycle = 1;
            release = release_cycle_fence_note_release_completed();
        } else if (strcmp(argv[1], "fu_bad_hold_after_accept") == 0 &&
                   stage == ESP_OK) {
            // CDC accept may race with the post-commit GPIO recheck.
            release_cycle_fence_note_canonical_release_accepted();
            release_cycle_fence_cancel_first_use_staging();
            g_lifecycle = 1;
            release = release_cycle_fence_note_release_completed();
        } else if (strcmp(argv[1], "fu_bad_hold_before_accept") == 0 &&
                   stage == ESP_OK) {
            release_cycle_fence_cancel_first_use_staging();
            release_cycle_fence_note_canonical_release_accepted();
            g_lifecycle = 1;
            release = release_cycle_fence_note_release_completed();
        }
    }
    // Simulate a successful #87 RTC/recording transition only after a newly
    // committed used-permit. Never auto-qualify a stale BOOT_ACTIVE marker.
    if (begin == ESP_OK && g_write == false && g_has_fence &&
        g_fence == 82u && g_next == 82u && strcmp(argv[1], "normal") != 0) {
        g_lifecycle = 0; /* #87 durable NORMAL + RTC and RECORDING success */
        CHECK(release_cycle_fence_note_recorder_ready() == ESP_OK);
        g_lifecycle = 1; /* caller's previous ARMED before USB preparation */
    }
    if (strcmp(argv[1], "complete") == 0) {
        CHECK(begin == ESP_OK && release_cycle_fence_boot_qualified());
        host_before = release_cycle_fence_note_host_owned();
        g_lifecycle = 2;  /* #87 committed HOST_UNRESOLVED */
        host = release_cycle_fence_note_host_owned();
        g_lifecycle = 1;  /* #87 accepted release and committed ARMED */
        /* Fault injection at the second NVS transaction (rearm), not the
           preceding boot-consumption transaction. */
        g_commit_before = strcmp(argv[4], "rearm_commit_before") == 0;
        g_commit_after = strcmp(argv[4], "rearm_commit_after") == 0;
        g_fail_set = strcmp(argv[4], "rearm_set") == 0;
        g_fail_lock = strcmp(argv[4], "rearm_lock") == 0;
        release = release_cycle_fence_note_release_completed();
        duplicate = release_cycle_fence_note_release_completed();
    } else if (strcmp(argv[1], "nohost") == 0) {
        CHECK(begin == ESP_OK && release_cycle_fence_boot_qualified());
        release = release_cycle_fence_note_release_completed();
    }
    printf("begin=%d qualified=%d host_before=%d host=%d release=%d "
           "duplicate=%d fence=%d stage=%d first_use=%d origin=%s\n",
           begin, release_cycle_fence_boot_qualified() ? 1 : 0,
           host_before, host, release, duplicate,
           g_has_fence ? g_fence : -1,
           stage, release_cycle_fence_first_use_staged() ? 1 : 0,
           release_cycle_fence_current_origin() == NULL
             ? "NONE" : release_cycle_fence_current_origin());
    CHECK(!g_locked);
    return 0;
}
