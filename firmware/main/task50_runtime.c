// Task #50 runtime coordinator: Task #49 exclusive MSC ownership + CDC JSON.
//
// main.c remains the proven recorder source-contract surface. As Task #49 did,
// this translation unit compiles it directly so the coordinator can reuse the
// private lifecycle primitives. One narrow manifest-sync wrapper flushes any
// boot-recovered RTC correction after SD recovery and before a recording
// segment is opened; this also covers power loss while USB_SYNC was active.

#include <string.h>

#include "device_manifest.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "rtc_correction.h"

static void recorder_task50_delete(TaskHandle_t task);
static bool task50_manifest_sync_wav_dir(
    const char *recordings_dir,
    const char *manifest_path,
    const char *tmp_path,
    const char *device_id,
    const char *updated_at,
    const char *state,
    uint32_t *out_added,
    uint32_t *out_skipped);

#define app_main recorder_base_app_main
#define vTaskDelete recorder_task50_delete
#define device_manifest_sync_wav_dir task50_manifest_sync_wav_dir
#include "main.c"
#undef device_manifest_sync_wav_dir
#undef vTaskDelete
#undef app_main

#include "usb_cdc_protocol.h"
#include "usb_msc_ownership.h"

#define REC_BIT_WRITER_FINALIZED (1u << 4)
#define REC_BIT_CAPTURE_DONE (1u << 5)
#define REC_BIT_BATTERY_DONE (1u << 6)
#define REC_TASK_DONE_MASK                                                     \
    (REC_BIT_WRITER_FINALIZED | REC_BIT_CAPTURE_DONE | REC_BIT_BATTERY_DONE)

static bool task50_manifest_sync_wav_dir(
    const char *recordings_dir,
    const char *manifest_path,
    const char *tmp_path,
    const char *device_id,
    const char *updated_at,
    const char *state,
    uint32_t *out_added,
    uint32_t *out_skipped) {
    if (!device_manifest_sync_wav_dir(recordings_dir, manifest_path, tmp_path,
                                      device_id, updated_at, state, out_added,
                                      out_skipped)) {
        return false;
    }
    // Boot recovery path: a pending correction can survive power loss during
    // USB_SYNC. At this point device identity/NVS and SD recovery are ready,
    // but the new recording segment has not been opened yet.
    return rtc_correction_flush_pending_event(RECORDER_EVENTS_PATH) == ESP_OK;
}

static void recorder_task50_delete(TaskHandle_t task) {
    const char *name = pcTaskGetName(NULL);
    EventBits_t done = 0;

    if (name != NULL) {
        if (strcmp(name, "rec_writer") == 0) {
            done = REC_BIT_WRITER_FINALIZED;
        } else if (strcmp(name, "rec_capture") == 0) {
            done = REC_BIT_CAPTURE_DONE;
        } else if (strcmp(name, "rec_battery") == 0) {
            done = REC_BIT_BATTERY_DONE;
        }
    }
    if (done != 0 && s_rec_events != NULL) {
        xEventGroupSetBits(s_rec_events, done);
    }
    vTaskDelete(task);
}

static void recorder_base_task(void *arg) {
    (void)arg;
    recorder_base_app_main();
    vTaskDelete(NULL);
}

static bool recorder_start_session(void) {
    if (s_rec_events == NULL || s_rec_lock == NULL) {
        return false;
    }

    xEventGroupClearBits(s_rec_events,
                         REC_BIT_SLOT_FULL | REC_BIT_STOP |
                             REC_BIT_WRITER_READY | REC_BIT_RECORDING_READY |
                             REC_TASK_DONE_MASK);
    pcm_pipeline_init(&s_pipeline, s_slot0, s_slot1);
    memset(&s_sink, 0, sizeof(s_sink));
    recorder_set_events_ready(false);

    if (xTaskCreate(recorder_writer_task, "rec_writer",
                    RECORDER_WRITER_STACK_BYTES, NULL, 4, NULL) != pdPASS) {
        return false;
    }
    if (xTaskCreate(recorder_capture_task, "rec_capture", 4096, NULL, 5,
                    NULL) != pdPASS) {
        recorder_request_stop();
        return false;
    }
    if (xTaskCreate(recorder_battery_task, "rec_battery", 3072, NULL, 3,
                    NULL) != pdPASS) {
        recorder_request_stop();
        return false;
    }
    return true;
}

static bool recorder_wait_initial_recording(void) {
    for (;;) {
        recorder_state_t state;
        if (s_rec_events == NULL || s_state_lock == NULL) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        state = recorder_current_state();
        if (state == RECORDER_STATE_RECORDING) {
            return true;
        }
        if (state == RECORDER_STATE_ERROR ||
            state == RECORDER_STATE_LOW_BATTERY_STOP) {
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

static bool recorder_cdc_status(usb_cdc_protocol_status_t *out, void *ctx) {
    recorder_state_t state;
    recorder_reason_t reason;
    int battery_mv;
    (void)ctx;

    if (out == NULL || s_state_lock == NULL ||
        xSemaphoreTake(s_state_lock, portMAX_DELAY) != pdTRUE) {
        return false;
    }
    state = s_rec_state.state;
    reason = s_rec_state.reason;
    battery_mv = s_last_battery_mv;
    xSemaphoreGive(s_state_lock);

    out->state = recorder_state_str(state);
    out->reason = recorder_reason_str(reason);
    out->battery_mv = battery_mv;
    out->battery_valid = battery_mv > 0;
    out->rtc_correction_pending = rtc_correction_is_pending();
    return out->state != NULL && out->reason != NULL;
}

static void recorder_handle_usb_attach(void) {
    EventBits_t done;

    // MSC MOUNT_START is the actual new USB session boundary. Discard any
    // stale partial CDC request before the host can own this session.
    usb_cdc_protocol_reset_session();

    if (recorder_current_state() != RECORDER_STATE_RECORDING) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (!recorder_transition_state(RECORDER_STATE_USB_PREPARE,
                                   RECORDER_REASON_USB, 0)) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (sd_mount_release_for_usb() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    recorder_request_usb_stop();
    done = xEventGroupWaitBits(s_rec_events, REC_TASK_DONE_MASK, pdFALSE,
                               pdTRUE, portMAX_DELAY);
    if ((done & REC_TASK_DONE_MASK) != REC_TASK_DONE_MASK ||
        (done & REC_BIT_WRITER_FINALIZED) == 0 ||
        recorder_current_state() != RECORDER_STATE_USB_PREPARE ||
        !sd_mount_device_fs_released()) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    if (usb_msc_ownership_note_prepare_complete(true, true, true) != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_USB);
    }
}

static void recorder_handle_usb_host_owned(void) {
    if (recorder_current_state() != RECORDER_STATE_USB_PREPARE ||
        sd_mount_is_mounted() || !usb_msc_ownership_is_host_owned()) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (!recorder_transition_state(RECORDER_STATE_USB_SYNC,
                                   RECORDER_REASON_USB, 0)) {
        recorder_enter_error(RECORDER_REASON_USB);
    }
}

static void recorder_handle_usb_detach(void) {
    // MSC remount-to-app completion is the actual detach boundary. Reset the
    // CDC framer before SD remount/recovery so no line spans USB sessions.
    usb_cdc_protocol_reset_session();

    if (recorder_current_state() != RECORDER_STATE_USB_SYNC) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (!recorder_transition_state(RECORDER_STATE_REMOUNT,
                                   RECORDER_REASON_USB, 0)) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (sd_mount_remount_after_usb() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_SD_MOUNT);
        return;
    }
    if (rtc_correction_flush_pending_event(RECORDER_EVENTS_PATH) != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_SD_WRITE);
        return;
    }

    if (!recorder_transition_state(RECORDER_STATE_RECOVER,
                                   RECORDER_REASON_RECOVERY, 0)) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (!recorder_start_session()) {
        recorder_enter_error(RECORDER_REASON_INTERNAL);
    }
}

void app_main(void) {
    esp_err_t rtc_preinit;
    usb_cdc_protocol_config_t cdc_config;

    // Keep the Capsule latch asserted before I2C/NVS setup; the proven base
    // app asserts it again, so this is intentionally idempotent.
    if (recorder_power_enable_hold() != ESP_OK) {
        return;
    }
    rtc_preinit = rtc_correction_init();

    if (xTaskCreate(recorder_base_task, "rec_base", 6144, NULL, 2, NULL) !=
        pdPASS) {
        return;
    }
    if (!recorder_wait_initial_recording()) {
        return;
    }

    // Retry after device_identity has established/validated NVS. Hardware or
    // NVS failure now becomes fail-loud before the CDC mutation surface opens.
    if (rtc_preinit != ESP_OK && rtc_correction_init() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_INTERNAL);
        return;
    }
    if (rtc_preinit == ESP_OK && rtc_correction_init() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_INTERNAL);
        return;
    }

    memset(&cdc_config, 0, sizeof(cdc_config));
    cdc_config.device_id = s_device_id;
    cdc_config.status_provider = recorder_cdc_status;

    if (usb_msc_ownership_init() != ESP_OK ||
        usb_cdc_protocol_init(&cdc_config) != ESP_OK ||
        usb_msc_ownership_start() != ESP_OK ||
        usb_cdc_protocol_start() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    for (;;) {
        switch (usb_msc_ownership_wait_event(UINT32_MAX)) {
            case USB_MSC_EVENT_ATTACH:
                recorder_handle_usb_attach();
                break;
            case USB_MSC_EVENT_HOST_OWNED:
                recorder_handle_usb_host_owned();
                break;
            case USB_MSC_EVENT_DETACH:
                recorder_handle_usb_detach();
                break;
            case USB_MSC_EVENT_FAILED:
                recorder_enter_error(RECORDER_REASON_USB);
                break;
            case USB_MSC_EVENT_NONE:
            default:
                break;
        }
    }
}
