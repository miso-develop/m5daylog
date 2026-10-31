// #144 R3: compiled real release_cycle_fence + shutdown_armed transitions.
// Each invocation is one process boot; the Python driver restarts with the
// persistent NVS byte pair after every injected crash/write failure.
// Synthetic NVS errors do not qualify real ESP32 power-loss durability.
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "nvs.h"
#include "recorder_nvs.h"
#include "shutdown_armed.h"
#include "release_cycle_fence.h"

#define CHECK(e) do { if (!(e)) { \
    fprintf(stderr,"CHECK %d %s\n",__LINE__,#e); exit(2); } } while (0)

static uint8_t lifecycle, fence, pending_value;
static bool staged, staged_lifecycle, locked;
static int write_number, fail_write, fail_phase; // 1=set, 2=pre-commit, 3=post-commit

esp_err_t recorder_nvs_init(void) { return ESP_OK; }
esp_err_t recorder_nvs_lock(void) { CHECK(!locked); locked=true; return ESP_OK; }
void recorder_nvs_unlock(void) { CHECK(locked); locked=false; }
esp_err_t nvs_open(const char *ns, int mode, nvs_handle_t *out) {
    CHECK(locked && strcmp(ns,"m5daylog")==0);
    CHECK(mode == NVS_READONLY || mode == NVS_READWRITE);
    *out=7; return ESP_OK;
}
void nvs_close(nvs_handle_t h) { CHECK(h==7 && locked); }
esp_err_t nvs_get_u8(nvs_handle_t h, const char *key, uint8_t *out) {
    CHECK(h==7 && locked);
    if (strcmp(key,"shutdown_armed")==0) { *out=lifecycle; return ESP_OK; }
    CHECK(strcmp(key,"release_boot")==0);
    *out=fence; return ESP_OK;
}
esp_err_t nvs_set_u8(nvs_handle_t h, const char *key, uint8_t value) {
    CHECK(h==7 && locked && !staged);
    CHECK(strcmp(key,"shutdown_armed")==0 || strcmp(key,"release_boot")==0);
    write_number++;
    if (write_number==fail_write && fail_phase==1) return ESP_FAIL;
    staged_lifecycle=strcmp(key,"shutdown_armed")==0;
    pending_value=value;
    staged=true;
    return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t h) {
    CHECK(h==7 && locked && staged);
    if (write_number==fail_write && fail_phase==2) {
        staged=false; return ESP_FAIL;
    }
    if (staged_lifecycle) lifecycle=pending_value;
    else fence=pending_value;
    staged=false;
    if (write_number==fail_write && fail_phase==3) return ESP_FAIL;
    return ESP_OK;
}
int main(int argc, char **argv) {
    esp_err_t result=ESP_OK;
    bool qualified_once=false, host_once=false;
    shutdown_armed_boot_action_t action=SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN;
    CHECK(argc==6);
    CHECK(strcmp(argv[1],"cycle")==0 || strcmp(argv[1],"reboot")==0 ||
          strcmp(argv[1],"fu_cycle")==0);
    lifecycle=(uint8_t)atoi(argv[2]); fence=(uint8_t)atoi(argv[3]);
    fail_write=atoi(argv[4]);
    if (strcmp(argv[5],"set")==0) fail_phase=1;
    else if (strcmp(argv[5],"before")==0) fail_phase=2;
    else if (strcmp(argv[5],"after")==0) fail_phase=3;
    else CHECK(strcmp(argv[5],"none")==0);
    if (strcmp(argv[1],"fu_cycle")==0) {
        // One process, one supervised staging/maintenance attempt. This
        // models the qualifying physical hold separately tested by the
        // production GPIO detector and the existing canonical accepted
        // release boundary, never a host-originated enrollment command.
        do {
            result=shutdown_armed_boot_action(false,&action);
            if (result!=ESP_OK || action!=SHUTDOWN_ARMED_BOOT_NORMAL) {
                result=ESP_ERR_INVALID_STATE; break;
            }
            result=release_cycle_fence_begin_boot(false);
            if (result!=ESP_OK) break;
            result=shutdown_armed_mark_host_unresolved(); // write 1
            if (result!=ESP_OK) break;
            result=release_cycle_fence_stage_first_use(); // write 2
            if (result!=ESP_OK) break;
            CHECK(release_cycle_fence_first_use_staged());
            release_cycle_fence_note_canonical_release_accepted();
            result=shutdown_armed_commit(); // write 3
            if (result!=ESP_OK) break;
            result=release_cycle_fence_note_release_completed(); // write 4
        } while (0);
    } else do {
        result=shutdown_armed_boot_action(true,&action);
        if (result!=ESP_OK) break;
        if (action!=SHUTDOWN_ARMED_BOOT_MANUAL_RESUME) {
            result=ESP_ERR_INVALID_STATE; break;
        }
        result=release_cycle_fence_begin_boot(true); // write 1 BOOT_ACTIVE
        if (result!=ESP_OK) break;
        result=shutdown_armed_mark_wake_recovery_pending(); // write 2 PENDING
        if (result!=ESP_OK) break;
        result=shutdown_armed_complete_wake_recovery(); // write 3 NORMAL
        if (result!=ESP_OK) break;
        result=release_cycle_fence_note_recorder_ready(); // RTC + fresh recorder assumed
        if (result!=ESP_OK) break;
        qualified_once=release_cycle_fence_boot_qualified();
        CHECK(qualified_once);
        if (strcmp(argv[1],"reboot")==0) break;
        result=shutdown_armed_mark_host_unresolved(); // write 4 HOST_UNRESOLVED
        if (result!=ESP_OK) break;
        result=release_cycle_fence_note_host_owned();
        if (result!=ESP_OK) break;
        host_once=release_cycle_fence_host_owned_qualified();
        CHECK(host_once);
        // Model an independently accepted D-031 release only AFTER host-owned.
        result=shutdown_armed_commit(); // write 5 ARMED
        if (result!=ESP_OK) break;
        result=release_cycle_fence_note_release_completed(); // write 6 READY
    } while (0);
    printf("result=%d lifecycle=%u fence=%u writes=%d qualified=%d host=%d live=%d\n",
           result, lifecycle, fence, write_number, qualified_once ? 1 : 0,
           host_once ? 1 : 0, release_cycle_fence_boot_qualified() ? 1 : 0);
    CHECK(!locked);
    return 0;
}
