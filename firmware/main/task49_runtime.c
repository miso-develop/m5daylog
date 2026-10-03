// Tasks #49/#87 runtime coordinator.
//
// The proven recorder implementation remains in main.c. Strategy 2 overlays
// only USB ownership/shutdown orchestration and a recovery-completion hook so
// SHUTDOWN_ARMED is cleared after pending recovery but before a fresh ID.

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

static void recorder_task49_delete(TaskHandle_t task);

#define app_main recorder_base_app_main
#define vTaskDelete recorder_task49_delete
#define device_manifest_sync_wav_dir recorder_task87_manifest_sync_wav_dir
#include "main.c"
#undef device_manifest_sync_wav_dir
#undef vTaskDelete
#undef app_main

#include "shutdown_armed.h"
#include "usb_msc_ownership.h"

// The real manifest function remains in the recorder component; the macro
// above only redirects calls compiled from main.c in this translation unit.
extern bool device_manifest_sync_wav_dir(const char *recordings_dir,
                                         const char *manifest_path,
                                         const char *tmp_path,
                                         const char *device_id,
                                         const char *updated_at,
                                         const char *state,
                                         uint32_t *out_added,
                                         uint32_t *out_skipped);

// Task #50 provides this symbol when integrated. Keeping the hook weak lets #87
// guarantee ordering without copying #50's still-independent implementation.
extern esp_err_t rtc_correction_flush_pending_event(const char *events_path)
    __attribute__((weak));

#define REC_BIT_WRITER_FINALIZED (1u << 4)
#define REC_BIT_CAPTURE_DONE     (1u << 5)
#define REC_BIT_BATTERY_DONE     (1u << 6)
#define REC_TASK_DONE_MASK       (REC_BIT_WRITER_FINALIZED | \
                                  REC_BIT_CAPTURE_DONE | \
                                  REC_BIT_BATTERY_DONE)

static bool s_manual_resume_pending = false;

static void recorder_task49_delete(TaskHandle_t task) {
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

static bool recorder_flush_pending_rtc_after_mount(void) {
    if (rtc_correction_flush_pending_event == NULL) {
        return true;
    }
    return rtc_correction_flush_pending_event(RECORDER_EVENTS_PATH) == ESP_OK;
}

bool recorder_task87_manifest_sync_wav_dir(const char *recordings_dir,
                                           const char *manifest_path,
                                           const char *tmp_path,
                                           const char *device_id,
                                           const char *updated_at,
                                           const char *state,
                                           uint32_t *out_added,
                                           uint32_t *out_skipped) {
    bool ok = device_manifest_sync_wav_dir(recordings_dir, manifest_path,
                                           tmp_path, device_id, updated_at,
                                           state, out_added, out_skipped);
    if (!ok || !s_manual_resume_pending) {
        return ok;
    }

    // main.c calls manifest sync only after the recovery scan and while Device
    // FAT/VFS ownership is established. Flush Task #50's durable pending event
    // exactly here, then clear armed intent before main.c generates a fresh ID.
    if (!recorder_flush_pending_rtc_after_mount()) {
        return false;
    }
    if (shutdown_armed_clear() != ESP_OK) {
        return false;
    }
    s_manual_resume_pending = false;
    return true;
}

static void recorder_base_task(void *arg) {
    (void)arg;
    recorder_base_app_main();
    vTaskDelete(NULL);
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

static void recorder_handle_usb_attach(void) {
    EventBits_t done;

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
    done = xEventGroupWaitBits(s_rec_events, REC_TASK_DONE_MASK,
                               pdFALSE, pdTRUE, portMAX_DELAY);
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
        sd_mount_is_mounted() ||
        !usb_msc_ownership_is_host_owned()) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (!recorder_transition_state(RECORDER_STATE_USB_SYNC,
                                   RECORDER_REASON_USB, 0)) {
        recorder_enter_error(RECORDER_REASON_USB);
    }
}

static void recorder_handle_usb_release(void) {
    if (recorder_current_state() != RECORDER_STATE_USB_SYNC ||
        sd_mount_is_mounted() ||
        !usb_msc_ownership_is_host_owned()) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (usb_msc_ownership_complete_release_quiesce() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (shutdown_armed_commit() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    // Armed intent is durable before HOLD is dropped. With USB still present
    // deep sleep is a functional shutdown; after cable removal HOLD=0 permits
    // the board's power circuit to switch off. This path never remounts SD.
    if (recorder_power_release_hold() != ESP_OK) {
        ESP_LOGE(TAG, "stage: power, result: error, reason: hold release");
    }
    recorder_power_enter_shutdown_sleep();
}

static void recorder_shutdown_armed_now(void) {
    if (recorder_power_release_hold() != ESP_OK) {
        ESP_LOGE(TAG, "stage: power, result: error, reason: hold release");
    }
    recorder_power_enter_shutdown_sleep();
}

void app_main(void) {
    bool manual_wake = false;
    shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN;

    // Maintain power before any persistent-state inspection. On an armed boot,
    // only the physical active-low WAKE button may authorize fresh recovery.
    if (recorder_power_enable_hold() != ESP_OK) {
        return;
    }
    if (recorder_power_manual_wake_asserted(&manual_wake) != ESP_OK ||
        shutdown_armed_boot_action(manual_wake, &action) != ESP_OK) {
        recorder_shutdown_armed_now();
        return;
    }
    if (action == SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN) {
        recorder_shutdown_armed_now();
        return;
    }
    s_manual_resume_pending = action == SHUTDOWN_ARMED_BOOT_MANUAL_RESUME;

    if (xTaskCreate(recorder_base_task, "rec_base", 6144, NULL, 2, NULL) !=
        pdPASS) {
        if (s_manual_resume_pending) {
            recorder_shutdown_armed_now();
        }
        return;
    }
    if (!recorder_wait_initial_recording()) {
        if (s_manual_resume_pending) {
            recorder_shutdown_armed_now();
        }
        return;
    }

    if (usb_msc_ownership_init() != ESP_OK ||
        usb_msc_ownership_start() != ESP_OK) {
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
            case USB_MSC_EVENT_RELEASE_REQUESTED:
                recorder_handle_usb_release();
                break;
            case USB_MSC_EVENT_RELEASE_QUIESCED:
                // The successful requester enters deep sleep synchronously.
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
