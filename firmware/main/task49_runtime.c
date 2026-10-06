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
#include "task87_wake_recovery.h"
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

#define REC_BIT_WRITER_FINALIZED (1u << 4)
#define REC_BIT_CAPTURE_DONE     (1u << 5)
#define REC_BIT_BATTERY_DONE     (1u << 6)
#define REC_TASK_DONE_MASK       (REC_BIT_WRITER_FINALIZED | \
                                  REC_BIT_CAPTURE_DONE | \
                                  REC_BIT_BATTERY_DONE)

static task87_wake_recovery_t s_wake_recovery;
static TaskHandle_t s_usb_event_task = NULL;

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
    if (!ok || !task87_wake_recovery_pending(&s_wake_recovery)) {
        return ok;
    }

    // main.c calls manifest sync only after Device FAT/VFS is mounted and the
    // recovery scan has completed. The production seam executes Task #50's
    // pending RTC flush (when linked) before clearing SHUTDOWN_ARMED. It keeps
    // this boot pending until a new recordingId has reached RECORDING.
    return task87_wake_recovery_complete_device_recovery(
               &s_wake_recovery, RECORDER_EVENTS_PATH) == ESP_OK;
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
            // main.c writes s_seg_rec_id before the writer publishes READY and
            // capture transitions RECOVER -> RECORDING. For a manual-WAKE boot,
            // requiring this non-empty per-boot ID prevents stale recovery proof
            // from authorizing USB publication without a fresh recorder session.
            bool fresh_recording_id_present = s_seg_rec_id[0] != '\0';
            if (task87_wake_recovery_note_recording_started(
                    &s_wake_recovery, fresh_recording_id_present) != ESP_OK) {
                recorder_enter_error(RECORDER_REASON_INTERNAL);
                return false;
            }
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

    // A successful quiesce now includes the durable lifecycle transition from
    // HOST_UNRESOLVED to SHUTDOWN_ARMED. Failure at USB teardown, deferred-write
    // proof, storage release, or NVS commit therefore leaves the device
    // fail-closed and never reaches HOLD release.
    if (usb_msc_ownership_complete_release_quiesce() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    // Armed intent is already durable before power shutdown. With USB still
    // present deep sleep is a functional shutdown; after cable removal HOLD=0
    // permits the board's power circuit to switch off. A HOLD/wakeup-disable
    // failure must remain visible and fail-closed rather than entering an
    // unwakeable sleep with an unproven power state.
    if (recorder_power_shutdown() != ESP_OK) {
        ESP_LOGE(TAG, "stage: power, result: error, reason: shutdown prepare");
        recorder_enter_error(RECORDER_REASON_INTERNAL);
        return;
    }
}

static void recorder_shutdown_armed_now(void) {
    if (recorder_power_shutdown() != ESP_OK) {
        ESP_LOGE(TAG, "stage: power, result: error, reason: shutdown prepare");
    }
}

static void recorder_usb_event_task(void *arg) {
    (void)arg;

    // This coordinator must already be runnable when TinyUSB starts. The driver
    // can emit ATTACHED before tinyusb_driver_install() returns, and its storage
    // MOUNT_START callback blocks until this task completes recorder finalization
    // and publishes USB_BIT_PREPARE_OK through the ownership module.
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

void app_main(void) {
    bool manual_wake = false;
    shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN;

    // Maintain power before any persistent-state inspection. On an armed boot,
    // only the physical active-low WAKE button may authorize fresh recovery.
    // HOST_UNRESOLVED always classifies as STAY_SHUTDOWN, even with WAKE held.
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
    task87_wake_recovery_init(
        &s_wake_recovery,
        action == SHUTDOWN_ARMED_BOOT_MANUAL_RESUME);

    if (xTaskCreate(recorder_base_task, "rec_base", 6144, NULL, 2, NULL) !=
        pdPASS) {
        if (task87_wake_recovery_requires_shutdown(&s_wake_recovery)) {
            recorder_shutdown_armed_now();
        }
        return;
    }
    if (!recorder_wait_initial_recording()) {
        if (task87_wake_recovery_requires_shutdown(&s_wake_recovery)) {
            recorder_shutdown_armed_now();
        }
        return;
    }

    // Manual-WAKE USB publication is a fresh-session privilege: even if future
    // changes accidentally return from the recording wait early, the ownership
    // stack cannot be rearmed until the production recovery seam says complete.
    if (!task87_wake_recovery_usb_rearm_allowed(&s_wake_recovery)) {
        recorder_enter_error(RECORDER_REASON_INTERNAL);
        recorder_shutdown_armed_now();
        return;
    }

    if (usb_msc_ownership_init() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    // Start the coordinator before installing TinyUSB. A first host
    // SET_CONFIGURATION can race ahead of tinyusb_driver_install() returning;
    // the storage callback must have another runnable task available to satisfy
    // its recorder-finalization barrier rather than deadlocking the install.
    if (xTaskCreate(recorder_usb_event_task, "rec_usb", 4096, NULL, 3,
                    &s_usb_event_task) != pdPASS) {
        s_usb_event_task = NULL;
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (usb_msc_ownership_start() != ESP_OK) {
        vTaskDelete(s_usb_event_task);
        s_usb_event_task = NULL;
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    // USB lifecycle ownership now belongs to rec_usb; returning retires the
    // ESP-IDF main task without removing the coordinator.
}