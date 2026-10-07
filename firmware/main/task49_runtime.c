// Tasks #49/#87 runtime coordinator.
//
// The proven recorder implementation remains in main.c. Strategy 2 overlays
// only USB ownership/shutdown orchestration and a recovery-completion hook.
// After pending recovery, SHUTDOWN_ARMED is replaced by a durable fail-closed
// wake-recovery marker that is cleared only after a fresh recording is proven.

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
#include "rtc_correction.h"
#include "recorder_nvs.h"
#include "task87_wake_recovery.h"
#include "usb_cdc_protocol.h"
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
#define USB_TRACE_FLUSH_INTERVAL_MS 250u

static task87_wake_recovery_t s_wake_recovery;
static TaskHandle_t s_usb_event_task = NULL;

static usb_cdc_release_result_t recorder_cdc_release_accept(
    const char *release_attempt_id,
    void *ctx) {
    usb_msc_release_storage_result_t result;
    (void)ctx;

    result = usb_msc_ownership_accept_release_storage(release_attempt_id);
    switch (result) {
        case USB_MSC_RELEASE_STORAGE_ACCEPTED:
            return USB_CDC_RELEASE_ACCEPTED;
        case USB_MSC_RELEASE_STORAGE_INVALID_ARGS:
            return USB_CDC_RELEASE_INVALID_ARGS;
        case USB_MSC_RELEASE_STORAGE_WRONG_STATE:
            return USB_CDC_RELEASE_WRONG_STATE;
        case USB_MSC_RELEASE_STORAGE_CONFLICT:
        default:
            return USB_CDC_RELEASE_CONFLICT;
    }
}

static bool recorder_cdc_release_response_complete(
    const char *release_attempt_id,
    void *ctx) {
    (void)ctx;
    return usb_msc_ownership_release_response_complete(release_attempt_id);
}

static bool recorder_cdc_command_admission_open(void *ctx) {
    (void)ctx;
    return usb_msc_ownership_release_command_admission_open();
}

static void recorder_cdc_physical_session_cutoff(void *ctx) {
    (void)ctx;
    usb_cdc_protocol_close_session();
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

static usb_cdc_set_time_result_t recorder_cdc_set_time(
    const char *requested_time,
    char *normalized,
    size_t normalized_size,
    void *ctx) {
    rtc_correction_result_t result;
    (void)ctx;

    result = rtc_correction_apply(requested_time, normalized, normalized_size);
    switch (result) {
        case RTC_CORRECTION_OK:
            return USB_CDC_SET_TIME_OK;
        case RTC_CORRECTION_INVALID_ARGS:
            return USB_CDC_SET_TIME_INVALID_ARGS;
        case RTC_CORRECTION_RANGE_ERROR:
            return USB_CDC_SET_TIME_RANGE_ERROR;
        case RTC_CORRECTION_BUSY:
            return USB_CDC_SET_TIME_BUSY;
        case RTC_CORRECTION_INTERNAL_ERROR:
        default:
            return USB_CDC_SET_TIME_INTERNAL_ERROR;
    }
}

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
    // pending RTC flush (when linked) before replacing SHUTDOWN_ARMED with the
    // durable wake-recovery gate. NORMAL is committed only after a new
    // recordingId has reached RECORDING.
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

    // Discard provisional/stale CDC state before this host session can become
    // authoritative. Transport state never grants ownership release.
    usb_cdc_protocol_reset_session();

    if (recorder_current_state() != RECORDER_STATE_RECORDING) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    // USB_MSC_EVENT_ATTACH is emitted only after a provisional MSC SCSI
    // command completed. Disconnect here, in the recorder coordinator, so the
    // host's SET_CONFIGURATION and first MSC status transactions are already
    // complete before the APP -> USB publication barrier starts.
    if (usb_msc_ownership_begin_prepare() != ESP_OK) {
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

static void recorder_flush_usb_scsi_trace(void) {
    static bool warned = false;
    esp_err_t err = usb_msc_ownership_flush_scsi_trace();

    if (err == ESP_OK) {
        warned = false;
    } else if (!warned) {
        ESP_LOGW(TAG,
                 "stage: usb-trace, result: persist-error, action: diagnostic-only");
        warned = true;
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
        return;
    }

    // D-031 command admission opens only after PC ownership / USB_SYNC.
    usb_cdc_protocol_open_session();
    recorder_flush_usb_scsi_trace();
}

static void recorder_handle_usb_host_reattached(void) {
    if (recorder_current_state() != RECORDER_STATE_USB_SYNC ||
        sd_mount_is_mounted() ||
        !usb_msc_ownership_is_host_owned() ||
        !usb_msc_ownership_release_command_admission_open()) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    // DETACHED already performed a callback-safe generation cutoff. Complete
    // the blocking command/TX drain here, outside TinyUSB callback context,
    // then publish a fresh CDC application session while leaving MSC ownership
    // unchanged and unresolved on the host side.
    usb_cdc_protocol_reset_session();
    usb_cdc_protocol_open_session();
}

static void recorder_handle_usb_release(void) {
    if (recorder_current_state() != RECORDER_STATE_USB_SYNC ||
        sd_mount_is_mounted() ||
        !usb_msc_ownership_is_host_owned()) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    // For normal RELEASE_STORAGE this event exists only after the accepted
    // response completed. Close CDC framing/admission before TinyUSB teardown.
    // Optional qualified SCSI eject converges on this same path.
    usb_cdc_protocol_reset_session();

    // Persist diagnostic request/completion counters before teardown. This
    // observation cannot authorize release and a trace-write failure does not
    // weaken the product's fail-closed ownership state.
    recorder_flush_usb_scsi_trace();

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

    // This coordinator is the only context allowed to perform the provisional
    // publication disconnect. TinyUSB callbacks only record ATTACHED / completed
    // SCSI evidence and signal this task; recorder finalization and APP -> USB
    // ownership transfer remain outside the USB stack callback path.
    for (;;) {
        switch (usb_msc_ownership_wait_event(USB_TRACE_FLUSH_INTERVAL_MS)) {
            case USB_MSC_EVENT_ATTACH:
                recorder_handle_usb_attach();
                break;
            case USB_MSC_EVENT_HOST_OWNED:
                recorder_handle_usb_host_owned();
                break;
            case USB_MSC_EVENT_HOST_REATTACHED:
                recorder_handle_usb_host_reattached();
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
        recorder_flush_usb_scsi_trace();
    }
}

void app_main(void) {
    bool manual_wake = false;
    usb_cdc_protocol_config_t cdc_config;
    shutdown_armed_boot_action_t action = SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN;

    // Maintain power before any persistent-state inspection. On an armed boot,
    // only the physical active-low WAKE button may authorize fresh recovery.
    // HOST_UNRESOLVED always classifies as STAY_SHUTDOWN, even with WAKE held.
    if (recorder_power_enable_hold() != ESP_OK) {
        return;
    }
    // Establish the single default-NVS lifetime before any recorder worker
    // task can run. Recorder-owned NVS users share recorder_nvs' mutex and no
    // recorder path deinitializes the partition during this process lifetime.
    if (recorder_nvs_init() != ESP_OK) {
        ESP_LOGE(TAG,
                 "stage: nvs, result: init-error, action: fail-closed");
        recorder_shutdown_armed_now();
        return;
    }
    if (recorder_power_manual_wake_asserted(&manual_wake) != ESP_OK ||
        shutdown_armed_boot_action(manual_wake, &action) != ESP_OK) {
        ESP_LOGE(TAG,
                 "stage: power, result: boot-gate-error, action: fail-closed");
        recorder_shutdown_armed_now();
        return;
    }
    ESP_LOGI(TAG,
             "stage: power, result: boot-gate, manual_wake: %s, action: %s",
             manual_wake ? "asserted" : "not-asserted",
             action == SHUTDOWN_ARMED_BOOT_NORMAL
                 ? "normal"
                 : (action == SHUTDOWN_ARMED_BOOT_MANUAL_RESUME
                        ? "manual-resume"
                        : "stay-shutdown"));
    if (action == SHUTDOWN_ARMED_BOOT_STAY_SHUTDOWN) {
        recorder_shutdown_armed_now();
        return;
    }
    task87_wake_recovery_init(
        &s_wake_recovery,
        action == SHUTDOWN_ARMED_BOOT_MANUAL_RESUME);

    // Manual-WAKE recovery needs the durable RTC pending record before
    // main.c reaches the Device-owned manifest/recovery seam. Normal boot does
    // not: keep #87's proven recorder startup path free of RTC/I2C bring-up and
    // initialize the application protocol only after recording is established.
    if (action == SHUTDOWN_ARMED_BOOT_MANUAL_RESUME &&
        rtc_correction_init() != ESP_OK) {
        ESP_LOGE(TAG,
                 "stage: rtc, result: pending-state-init-error, action: manual-wake-fail-closed");
        recorder_shutdown_armed_now();
        return;
    }

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

    // On a normal boot, load durable pending state only after the recorder has
    // reached its known-good RECORDING baseline. rtc_correction_init() treats
    // live RTC transport as best-effort; only an unreadable durable pending
    // state blocks CDC/MSC publication here. SET_TIME retries RTC hardware
    // bring-up at mutation time.
    if (action == SHUTDOWN_ARMED_BOOT_NORMAL &&
        rtc_correction_init() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_INTERNAL);
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

    memset(&cdc_config, 0, sizeof(cdc_config));
    cdc_config.device_id = s_device_id;
    cdc_config.status_provider = recorder_cdc_status;
    cdc_config.status_ctx = NULL;
    cdc_config.set_time = recorder_cdc_set_time;
    cdc_config.set_time_ctx = NULL;
    cdc_config.release_accept = recorder_cdc_release_accept;
    cdc_config.release_response_complete =
        recorder_cdc_release_response_complete;
    cdc_config.command_admission_open =
        recorder_cdc_command_admission_open;

    if (usb_msc_ownership_init() != ESP_OK ||
        usb_msc_ownership_set_physical_session_cutoff(
            recorder_cdc_physical_session_cutoff, NULL) != ESP_OK ||
        usb_cdc_protocol_init(&cdc_config) != ESP_OK) {
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
    if (usb_cdc_protocol_start() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    // USB lifecycle ownership now belongs to rec_usb; returning retires the
    // ESP-IDF main task without removing the coordinator.
}