// Tasks #49/#87 runtime coordinator.
//
// Keep the proven Tasks #44-#48 recorder implementation in main.c unchanged,
// but compile it in this translation unit so this coordinator can reuse its
// private lifecycle/task primitives. vTaskDelete is intercepted only to emit
// task-completion bits before the original task destroys itself; this makes a
// fast USB ownership cycle safe to restart without overlapping old tasks.

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

static void recorder_task49_delete(TaskHandle_t task);

#define app_main recorder_base_app_main
#define vTaskDelete recorder_task49_delete
#include "main.c"
#undef vTaskDelete
#undef app_main

#include "usb_msc_ownership.h"

#define REC_BIT_WRITER_FINALIZED (1u << 4)
#define REC_BIT_CAPTURE_DONE     (1u << 5)
#define REC_BIT_BATTERY_DONE     (1u << 6)
#define REC_TASK_DONE_MASK       (REC_BIT_WRITER_FINALIZED | \
                                  REC_BIT_CAPTURE_DONE | \
                                  REC_BIT_BATTERY_DONE)

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
    if (xTaskCreate(recorder_capture_task, "rec_capture",
                    4096, NULL, 5, NULL) != pdPASS) {
        recorder_request_stop();
        return false;
    }
    if (xTaskCreate(recorder_battery_task, "rec_battery",
                    3072, NULL, 3, NULL) != pdPASS) {
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
        // Never release the blocked TinyUSB ATTACHED callback on failed proof.
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

static void recorder_handle_usb_detach(void) {
    if (recorder_current_state() != RECORDER_STATE_USB_SYNC ||
        usb_msc_ownership_is_host_owned()) {
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

    // Processed-ACK retention is deliberately Task #52. Task #87 only
    // restores exclusive Device ownership, starts a fresh recording ID, then
    // re-enables USB after RECORDING is observable again.
    if (!recorder_transition_state(RECORDER_STATE_RECOVER,
                                   RECORDER_REASON_RECOVERY, 0)) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
    if (!recorder_start_session()) {
        recorder_enter_error(RECORDER_REASON_INTERNAL);
        return;
    }
    if (!recorder_wait_initial_recording()) {
        return;
    }
    if (usb_msc_ownership_note_recording_recovered() != ESP_OK ||
        usb_msc_ownership_rearm() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_USB);
    }
}

static void recorder_handle_usb_barrier(void) {
    if (recorder_current_state() != RECORDER_STATE_USB_SYNC ||
        sd_mount_is_mounted() ||
        !usb_msc_ownership_is_host_owned()) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }

    // Success emits exactly one USB_MSC_EVENT_DETACH. The event loop consumes
    // that event and performs REMOUNT once; this handler never remounts itself.
    if (usb_msc_ownership_complete_disconnect_barrier() != ESP_OK) {
        recorder_enter_error(RECORDER_REASON_USB);
        return;
    }
}

void app_main(void) {
    if (xTaskCreate(recorder_base_task, "rec_base", 6144, NULL, 2, NULL) !=
        pdPASS) {
        return;
    }
    if (!recorder_wait_initial_recording()) {
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
            case USB_MSC_EVENT_BARRIER_REQUIRED:
                recorder_handle_usb_barrier();
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
