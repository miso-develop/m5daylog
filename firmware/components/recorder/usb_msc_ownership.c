// Tasks #49/#87: exclusive recorder microSD ownership with Strategy 2 eject.
//
// APP -> USB publication uses two host enumerations. With esp_tinyusb 2.2.1
// and auto_mount_off=1, the first ATTACHED event is provisional only. Espressif
// invokes that event from tud_mount_cb before TinyUSB sends the status stage for
// SET_CONFIGURATION, so disconnecting inside ATTACHED aborts configuration on
// Windows. Instead, a completed MSC SCSI command proves both configuration and
// class binding. Only then does the recorder coordinator soft-disconnect outside
// the USB callback, finalize recording, persist HOST_UNRESOLVED, switch the
// storage APP -> USB, and reconnect with an already USB-owned LUN. Generic
// detach / suspend never authorizes Device ownership. The only normal reverse
// trigger is START STOP UNIT(load_eject=1,start=0), observed after its SCSI
// status transaction completes.

#include "usb_msc_ownership.h"

#ifdef ESP_PLATFORM

#include <stddef.h>
#include <stdint.h>
#include <stdatomic.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sd_mount.h"
#include "shutdown_armed.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "tinyusb_msc.h"
#include "tusb.h"

static const char *TAG = "recorder_usb";

#define USB_BIT_ATTACH              (1u << 0)
#define USB_BIT_HOST_OWNED          (1u << 2)
#define USB_BIT_RELEASE_REQUESTED   (1u << 3)
#define USB_BIT_RELEASE_QUIESCED    (1u << 4)
#define USB_BIT_FAILED              (1u << 5)
#define USB_PUBLIC_BITS             (USB_BIT_ATTACH | USB_BIT_HOST_OWNED | \
                                     USB_BIT_RELEASE_REQUESTED | \
                                     USB_BIT_RELEASE_QUIESCED | USB_BIT_FAILED)
#define USB_SCSI_CMD_TEST_UNIT_READY      0x00u
#define USB_SCSI_CMD_START_STOP_UNIT      0x1bu
#define USB_SCSI_CMD_PREVENT_ALLOW        0x1eu
#define USB_SCSI_CMD_SYNCHRONIZE_CACHE_10 0x35u
#define USB_SCSI_CMD_SYNCHRONIZE_CACHE_16 0x91u
#define USB_REENUM_DISCONNECT_MS           1000u
#define USB_SCSI_SENSE_NOT_READY      0x02u
#define USB_SCSI_ASC_MEDIUM_NOT_PRESENT 0x3au
#define USB_SCSI_ASCQ_NONE            0x00u

// esp_tinyusb owns these callbacks. Linker wrapping lets Strategy 2 reject
// newly admitted media/backend commands after post-status explicit eject while
// preserving the original callbacks before the release gate.
bool __real_tud_msc_test_unit_ready_cb(uint8_t lun);
void __real_tud_msc_capacity_cb(uint8_t lun,
                                uint32_t *block_count,
                                uint16_t *block_size);
int32_t __real_tud_msc_read10_cb(uint8_t lun,
                                 uint32_t lba,
                                 uint32_t offset,
                                 void *buffer,
                                 uint32_t bufsize);
int32_t __real_tud_msc_write10_cb(uint8_t lun,
                                  uint32_t lba,
                                  uint32_t offset,
                                  uint8_t *buffer,
                                  uint32_t bufsize);
bool __real_tud_msc_start_stop_cb(uint8_t lun,
                                  uint8_t power_condition,
                                  bool start,
                                  bool load_eject);

static EventGroupHandle_t s_usb_events = NULL;
static volatile bool s_initialized = false;
static volatile bool s_starting = false;
static volatile bool s_started = false;
static volatile bool s_storage_usb_owned = false;
static volatile bool s_host_owned = false;
// Shared D-031 release gate. The first successful transition to true blocks
// wrapped MSC backend admission before any CDC success response is emitted.
static _Atomic bool s_release_pending = false;
static _Atomic bool s_release_waiting_response = false;
static char s_release_attempt_id[USB_MSC_RELEASE_ATTEMPT_MAX_BYTES + 1u];
static volatile bool s_provisional_attached = false;
static volatile bool s_publish_triggered = false;
static volatile bool s_prepare_disconnected = false;
static volatile bool s_transfer_authorized = false;
static bool s_wav_finalized = false;
static bool s_manifest_committed = false;
static bool s_device_fs_released = false;

// Human-Gate diagnostic trace. These fields never authorize release. TinyUSB
// callbacks update only lock-free RAM counters; the recorder coordinator is
// the sole context that persists a bounded snapshot to NVS.
static volatile uint32_t s_trace_session = 0;
static volatile uint32_t s_trace_start_stop_requests = 0;
static volatile uint32_t s_trace_start_stop_completions = 0;
static volatile uint32_t s_trace_prevent_allow_completions = 0;
static volatile uint32_t s_trace_sync_cache_completions = 0;
static volatile uint32_t s_trace_last_start_stop_power = 0;
static volatile uint32_t s_trace_last_start_stop_flags = 0;
static volatile uint32_t s_trace_last_control_opcode = 0;
static volatile uint32_t s_trace_last_control_byte4 = 0;
static volatile uint32_t s_trace_update_seq = 0;
static uint32_t s_trace_flushed_seq = 0;

static uint8_t usb_scsi_trace_u8(uint32_t value) {
    return value > UINT8_MAX ? UINT8_MAX : (uint8_t)value;
}

static void usb_scsi_trace_mark_dirty(void) {
    (void)__atomic_add_fetch(&s_trace_update_seq, 1u, __ATOMIC_RELEASE);
}

static void usb_scsi_trace_begin_session(void) {
    uint32_t session =
        __atomic_add_fetch(&s_trace_session, 1u, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_start_stop_requests, 0u, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_start_stop_completions, 0u, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_prevent_allow_completions, 0u, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_sync_cache_completions, 0u, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_last_start_stop_power, 0u, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_last_start_stop_flags, 0u, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_last_control_opcode, 0u, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_last_control_byte4, 0u, __ATOMIC_RELAXED);
    usb_scsi_trace_mark_dirty();
    ESP_LOGI(TAG,
             "stage: usb-trace, result: session-start, session: %lu",
             (unsigned long)session);
}

static void usb_scsi_trace_note_start_stop_request(uint8_t power_condition,
                                                   bool start,
                                                   bool load_eject) {
    uint32_t flags;

    if (!s_host_owned) {
        return;
    }
    flags = (load_eject ? 0x02u : 0u) | (start ? 0x01u : 0u);
    (void)__atomic_add_fetch(&s_trace_start_stop_requests, 1u,
                             __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_last_start_stop_power,
                     (uint32_t)(power_condition & 0x0fu), __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_last_start_stop_flags, flags, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_last_control_opcode,
                     USB_SCSI_CMD_START_STOP_UNIT, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_last_control_byte4,
                     ((uint32_t)(power_condition & 0x0fu) << 4) | flags,
                     __ATOMIC_RELAXED);
    usb_scsi_trace_mark_dirty();
}

static void usb_scsi_trace_note_command_complete(
    uint8_t const scsi_cmd[16]) {
    uint8_t opcode;

    if (!s_host_owned || scsi_cmd == NULL) {
        return;
    }
    opcode = scsi_cmd[0];
    if (opcode == USB_SCSI_CMD_START_STOP_UNIT) {
        (void)__atomic_add_fetch(&s_trace_start_stop_completions, 1u,
                                 __ATOMIC_RELAXED);
    } else if (opcode == USB_SCSI_CMD_PREVENT_ALLOW) {
        (void)__atomic_add_fetch(&s_trace_prevent_allow_completions, 1u,
                                 __ATOMIC_RELAXED);
    } else if (opcode == USB_SCSI_CMD_SYNCHRONIZE_CACHE_10 ||
               opcode == USB_SCSI_CMD_SYNCHRONIZE_CACHE_16) {
        (void)__atomic_add_fetch(&s_trace_sync_cache_completions, 1u,
                                 __ATOMIC_RELAXED);
    } else {
        return;
    }
    __atomic_store_n(&s_trace_last_control_opcode, opcode, __ATOMIC_RELAXED);
    __atomic_store_n(&s_trace_last_control_byte4, scsi_cmd[4],
                     __ATOMIC_RELAXED);
    usb_scsi_trace_mark_dirty();
}

static esp_err_t usb_scsi_trace_finish_nvs(esp_err_t operation_err) {
    esp_err_t deinit_err = nvs_flash_deinit();
    if (operation_err != ESP_OK) {
        return operation_err;
    }
    if (deinit_err == ESP_OK) {
        return ESP_OK;
    }
#ifdef ESP_ERR_NVS_NOT_INITIALIZED
    if (deinit_err == ESP_ERR_NVS_NOT_INITIALIZED) {
        return ESP_OK;
    }
#endif
    return deinit_err;
}

esp_err_t usb_msc_ownership_flush_scsi_trace(void) {
    nvs_handle_t handle;
    esp_err_t err;
    uint32_t seq;
    uint32_t session;
    uint32_t ss_req;
    uint32_t ss_done;
    uint32_t pa_done;
    uint32_t sync_done;
    uint32_t ss_power;
    uint32_t ss_flags;
    uint32_t last_op;
    uint32_t last_b4;

    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    seq = __atomic_load_n(&s_trace_update_seq, __ATOMIC_ACQUIRE);
    if (seq == s_trace_flushed_seq) {
        return ESP_OK;
    }

    session = __atomic_load_n(&s_trace_session, __ATOMIC_RELAXED);
    ss_req = __atomic_load_n(&s_trace_start_stop_requests, __ATOMIC_RELAXED);
    ss_done =
        __atomic_load_n(&s_trace_start_stop_completions, __ATOMIC_RELAXED);
    pa_done =
        __atomic_load_n(&s_trace_prevent_allow_completions, __ATOMIC_RELAXED);
    sync_done =
        __atomic_load_n(&s_trace_sync_cache_completions, __ATOMIC_RELAXED);
    ss_power =
        __atomic_load_n(&s_trace_last_start_stop_power, __ATOMIC_RELAXED);
    ss_flags =
        __atomic_load_n(&s_trace_last_start_stop_flags, __ATOMIC_RELAXED);
    last_op =
        __atomic_load_n(&s_trace_last_control_opcode, __ATOMIC_RELAXED);
    last_b4 =
        __atomic_load_n(&s_trace_last_control_byte4, __ATOMIC_RELAXED);

    err = nvs_flash_init();
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_open("m5daylog", NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return usb_scsi_trace_finish_nvs(err);
    }

#define TRACE_SET_U8(key, value)                                      \
    do {                                                               \
        if (err == ESP_OK) {                                           \
            err = nvs_set_u8(handle, (key), usb_scsi_trace_u8(value)); \
        }                                                              \
    } while (0)
    TRACE_SET_U8("scsi_valid", 1u);
    TRACE_SET_U8("scsi_session", session);
    TRACE_SET_U8("scsi_ss_req", ss_req);
    TRACE_SET_U8("scsi_ss_done", ss_done);
    TRACE_SET_U8("scsi_ss_power", ss_power);
    TRACE_SET_U8("scsi_ss_flags", ss_flags);
    TRACE_SET_U8("scsi_pa_done", pa_done);
    TRACE_SET_U8("scsi_sync_done", sync_done);
    TRACE_SET_U8("scsi_last_op", last_op);
    TRACE_SET_U8("scsi_last_b4", last_b4);
#undef TRACE_SET_U8

    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    err = usb_scsi_trace_finish_nvs(err);
    if (err != ESP_OK) {
        return err;
    }
    if (__atomic_load_n(&s_trace_update_seq, __ATOMIC_ACQUIRE) == seq) {
        s_trace_flushed_seq = seq;
    }
    ESP_LOGI(TAG,
             "stage: usb-trace, result: persisted, session: %lu, ss_req: %lu, ss_done: %lu, pa_done: %lu, sync_done: %lu, last_op: 0x%02lx, last_b4: 0x%02lx",
             (unsigned long)session, (unsigned long)ss_req,
             (unsigned long)ss_done, (unsigned long)pa_done,
             (unsigned long)sync_done, (unsigned long)last_op,
             (unsigned long)last_b4);
    return ESP_OK;
}

static void usb_fail(const char *reason) {
    if (s_usb_events != NULL) {
        xEventGroupSetBits(s_usb_events, USB_BIT_FAILED);
    }
    ESP_LOGE(TAG, "stage: usb, result: error, reason: %s", reason);
}

static bool usb_msc_publish(bool wav_finalized,
                            bool manifest_committed,
                            bool device_fs_released) {
    esp_err_t err;

    if (!wav_finalized || !manifest_committed || !device_fs_released ||
        s_usb_events == NULL || s_storage_usb_owned || s_host_owned ||
        s_release_pending || !s_provisional_attached || !s_publish_triggered ||
        !s_prepare_disconnected || !sd_mount_is_mounted()) {
        usb_fail("publish gate incomplete");
        return false;
    }

    // A completed provisional MSC command has already proved host class
    // binding, and the coordinator has soft-disconnected outside TinyUSB's
    // SetConfiguration/SCSI callbacks. Make unresolved ownership boot-visible
    // before the storage can ever become USB-owned.
    if (shutdown_armed_mark_host_unresolved() != ESP_OK) {
        usb_fail("persist unresolved ownership");
        return false;
    }

    s_wav_finalized = wav_finalized;
    s_manifest_committed = manifest_committed;
    s_device_fs_released = device_fs_released;
    s_transfer_authorized = true;

    // Switch APP -> USB while logically disconnected. esp_tinyusb performs
    // this synchronously and emits MOUNT_START/MOUNT_COMPLETE around the real
    // FAT/VFS unmount. Doing this outside tud_mount_cb avoids publishing a
    // SetConfiguration whose MSC LUN changes ownership re-entrantly.
    err = sd_mount_transfer_to_usb();
    s_transfer_authorized = false;
    if (err != ESP_OK || !s_storage_usb_owned || sd_mount_is_mounted()) {
        usb_fail("host ownership transfer");
        return false;
    }

    // Force a fresh host enumeration only after the LUN is USB-owned. Espressif
    // uses the same disconnect/delay/connect pattern in its USB host-device
    // tests (their MSC reconnect case uses 1000 ms). Matching that proven
    // interval avoids Windows retaining the provisional configuration that had
    // no host-accessible medium.
    vTaskDelay(pdMS_TO_TICKS(USB_REENUM_DISCONNECT_MS));
    if (!tud_connect()) {
        usb_fail("publication reconnect");
        return false;
    }

    ESP_LOGI(TAG,
             "stage: usb, result: publication-reconnect, owner: host, mount: usb");
    return true;
}

static void usb_storage_event_cb(tinyusb_msc_storage_handle_t handle,
                                 tinyusb_msc_event_t *event,
                                 void *arg) {
    (void)handle;
    (void)arg;
    if (event == NULL || s_usb_events == NULL) {
        return;
    }

    if (event->id == TINYUSB_MSC_EVENT_MOUNT_START) {
        if (event->mount_point != TINYUSB_MSC_STORAGE_MOUNT_APP ||
            !s_publish_triggered || !s_transfer_authorized ||
            s_storage_usb_owned || s_host_owned || s_release_pending ||
            !sd_mount_is_mounted() ||
            !s_wav_finalized || !s_manifest_committed ||
            !s_device_fs_released) {
            usb_fail("unexpected ownership switch");
            return;
        }

        // Recorder finalization, Device-FS release, and durable
        // HOST_UNRESOLVED were completed before calling the synchronous
        // esp_tinyusb storage switch. This callback is now a validation seam;
        // it never blocks TinyUSB's SetConfiguration path.
        ESP_LOGI(TAG,
                 "stage: usb, result: transfer-start, owner: device-released, mount: app");
        return;
    }

    if (event->id == TINYUSB_MSC_EVENT_MOUNT_COMPLETE) {
        if (event->mount_point == TINYUSB_MSC_STORAGE_MOUNT_USB &&
            s_transfer_authorized) {
            sd_mount_note_usb_owned();
            s_storage_usb_owned = true;
            ESP_LOGI(TAG,
                     "stage: usb, result: storage-ready, owner: host-reserved, mount: usb");
        } else {
            // Strategy 2 never permits a same-session USB -> APP remount or an
            // ownership switch that bypassed the durable publication barrier.
            usb_fail("unexpected mount complete");
        }
        return;
    }

    if (event->id == TINYUSB_MSC_EVENT_MOUNT_FAILED ||
        event->id == TINYUSB_MSC_EVENT_FORMAT_REQUIRED ||
        event->id == TINYUSB_MSC_EVENT_FORMAT_FAILED) {
        usb_fail("mount ownership switch");
    }
}

static void usb_device_event_cb(tinyusb_event_t *event, void *arg) {
    (void)arg;
    if (event == NULL || s_usb_events == NULL) {
        return;
    }

    if (event->id == TINYUSB_EVENT_ATTACHED) {
        if (!s_storage_usb_owned) {
            // esp_tinyusb emits ATTACHED from tud_mount_cb before TinyUSB sends
            // the SET_CONFIGURATION status stage. Record provisional
            // configuration only; disconnecting here aborts that control
            // transaction and can prevent Windows from binding USBSTOR.
            if (!s_initialized || (!s_started && !s_starting) ||
                s_host_owned || s_release_pending || s_publish_triggered ||
                s_provisional_attached || !sd_mount_is_mounted()) {
                usb_fail("attach ownership state");
                return;
            }
            s_provisional_attached = true;
            ESP_LOGI(TAG,
                     "stage: usb, result: provisional-configured, owner: device, action: wait-msc-command");
            return;
        }

        if (!s_host_owned) {
            // This is the deliberate second enumeration. Storage ownership was
            // transferred while detached; only this fresh SetConfiguration
            // promotes the volatile lifecycle to host-configured / USB_SYNC.
            if (!s_initialized || (!s_started && !s_starting) ||
                !s_publish_triggered || !s_prepare_disconnected ||
                s_release_pending || sd_mount_is_mounted()) {
                (void)tud_disconnect();
                usb_fail("publication reattach state");
                return;
            }
            s_host_owned = true;
            usb_scsi_trace_begin_session();
            s_provisional_attached = false;
            s_prepare_disconnected = false;
            xEventGroupSetBits(s_usb_events, USB_BIT_HOST_OWNED);
            ESP_LOGI(TAG,
                     "stage: usb, result: configured, owner: host, mount: usb");
            return;
        }

        // A later physical reconnect may reuse the same unresolved host-owned
        // session so the PC can still perform the required explicit eject.
        if (!s_release_pending && !sd_mount_is_mounted()) {
            ESP_LOGI(TAG,
                     "stage: usb, result: reconfigured, owner: host, mount: usb");
            return;
        }
        (void)tud_disconnect();
        usb_fail("configured ownership mismatch");
        return;
    }

#ifdef CONFIG_TINYUSB_SUSPEND_CALLBACK
    if (event->id == TINYUSB_EVENT_SUSPENDED) {
        if (s_host_owned) {
            ESP_LOGW(TAG,
                     "stage: usb, result: ambiguous-suspend, owner: host, action: none");
        }
        return;
    }
#endif

    if (event->id == TINYUSB_EVENT_DETACHED) {
        if (!s_storage_usb_owned && !s_host_owned &&
            s_provisional_attached && !s_publish_triggered) {
            // The host never reached the no-media TEST UNIT READY proof, so it
            // never owned the card and no durable publication transition
            // started. Retire only the provisional configuration and allow a
            // later cable attach to retry while recording continues.
            s_provisional_attached = false;
            ESP_LOGI(TAG,
                     "stage: usb, result: provisional-detach, owner: device, action: retry-allowed");
            return;
        }
        if (s_host_owned) {
            ESP_LOGW(TAG,
                     "stage: usb, result: ambiguous-detach, owner: host, action: none");
        }
    }
}
static bool usb_note_initial_msc_command_complete(void) {
    if (!s_initialized || (!s_started && !s_starting) ||
        !s_provisional_attached || s_storage_usb_owned || s_host_owned ||
        s_release_pending || s_publish_triggered || !sd_mount_is_mounted()) {
        return false;
    }

    // TinyUSB invokes tud_msc_scsi_complete_cb only after the command status
    // transaction has completed. This is the earliest semantic proof that the
    // host finished SET_CONFIGURATION and bound the MSC class.
    s_publish_triggered = true;
    xEventGroupSetBits(s_usb_events, USB_BIT_ATTACH);
    ESP_LOGI(TAG,
             "stage: usb, result: msc-command-complete, owner: device, action: prepare");
    return true;
}

static bool usb_release_attempt_id_valid(const char *value) {
    size_t i;
    size_t len;

    if (value == NULL) {
        return false;
    }
    len = strlen(value);
    if (len == 0u || len > USB_MSC_RELEASE_ATTEMPT_MAX_BYTES) {
        return false;
    }
    for (i = 0u; i < len; ++i) {
        char c = value[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || c == '.' || c == '_' ||
              c == ':' || c == '-')) {
            return false;
        }
    }
    return true;
}

bool usb_msc_ownership_release_command_admission_open(void) {
    return s_initialized && s_started && s_storage_usb_owned && s_host_owned &&
           !sd_mount_is_mounted() &&
           !atomic_load_explicit(&s_release_pending, memory_order_acquire);
}

usb_msc_release_storage_result_t usb_msc_ownership_accept_release_storage(
    const char *release_attempt_id) {
    bool expected = false;

    if (!usb_release_attempt_id_valid(release_attempt_id)) {
        return USB_MSC_RELEASE_STORAGE_INVALID_ARGS;
    }
    if (!s_initialized || !s_started || !s_storage_usb_owned ||
        !s_host_owned || sd_mount_is_mounted()) {
        return USB_MSC_RELEASE_STORAGE_WRONG_STATE;
    }

    // This CAS is the D-031 acceptance boundary. The MSC wrappers observe the
    // same atomic gate, so later block I/O cannot reach esp_tinyusb storage even
    // while CDC remains alive long enough to return the success response.
    if (!atomic_compare_exchange_strong_explicit(
            &s_release_pending, &expected, true,
            memory_order_acq_rel, memory_order_acquire)) {
        return USB_MSC_RELEASE_STORAGE_CONFLICT;
    }

    memcpy(s_release_attempt_id, release_attempt_id,
           strlen(release_attempt_id) + 1u);
    atomic_store_explicit(&s_release_waiting_response, true,
                          memory_order_release);
    ESP_LOGI(TAG,
             "stage: usb, result: release-command-accepted, owner: host, action: gate-backend");
    return USB_MSC_RELEASE_STORAGE_ACCEPTED;
}

bool usb_msc_ownership_release_response_complete(
    const char *release_attempt_id) {
    if (!usb_release_attempt_id_valid(release_attempt_id) ||
        !atomic_load_explicit(&s_release_pending, memory_order_acquire) ||
        !atomic_load_explicit(&s_release_waiting_response,
                              memory_order_acquire) ||
        !s_initialized || !s_started || !s_host_owned ||
        strcmp(s_release_attempt_id, release_attempt_id) != 0) {
        return false;
    }

    // Teardown is signalled only after the accepted CDC response has been
    // flushed. The backend gate has already been closed since acceptance.
    atomic_store_explicit(&s_release_waiting_response, false,
                          memory_order_release);
    xEventGroupSetBits(s_usb_events, USB_BIT_RELEASE_REQUESTED);
    ESP_LOGI(TAG,
             "stage: usb, result: release-response-complete, owner: host, action: release-quiesce");
    return true;
}

static bool usb_request_explicit_eject(void) {
    bool expected = false;

    // Optional compatibility path: qualified post-status SCSI eject converges
    // on the same atomic backend gate and coordinator quiescence path.
    if (atomic_load_explicit(&s_release_pending, memory_order_acquire)) {
        return true;
    }
    if (!s_initialized || !s_started || !s_host_owned ||
        sd_mount_is_mounted()) {
        usb_fail("explicit eject state");
        return false;
    }
    if (!atomic_compare_exchange_strong_explicit(
            &s_release_pending, &expected, true,
            memory_order_acq_rel, memory_order_acquire)) {
        return true;
    }

    s_release_attempt_id[0] = '\0';
    atomic_store_explicit(&s_release_waiting_response, false,
                          memory_order_release);
    xEventGroupSetBits(s_usb_events, USB_BIT_RELEASE_REQUESTED);
    ESP_LOGI(TAG,
             "stage: usb, result: explicit-eject, owner: host, action: release-quiesce");
    return true;
}

bool __wrap_tud_msc_start_stop_cb(uint8_t lun,
                                  uint8_t power_condition,
                                  bool start,
                                  bool load_eject) {
    // Observation only. Release authority remains exclusively in the
    // post-status tud_msc_scsi_complete_cb path below.
    usb_scsi_trace_note_start_stop_request(power_condition, start, load_eject);
    return __real_tud_msc_start_stop_cb(lun, power_condition, start,
                                        load_eject);
}

void tud_msc_scsi_complete_cb(uint8_t lun, uint8_t const scsi_cmd[16]) {
    bool load_eject;
    bool start;

    (void)lun;
    if (scsi_cmd == NULL) {
        return;
    }

    // Before publication, the first completed SCSI command is deliberately
    // consumed only as class-binding proof. Storage remains APP-owned and
    // esp_tinyusb reports the medium not ready to the host.
    if (!s_storage_usb_owned && s_provisional_attached &&
        !s_publish_triggered) {
        // Wait specifically for TEST UNIT READY. esp_tinyusb answers it with
        // MEDIUM NOT PRESENT while the storage is APP-owned, so the host has
        // explicitly observed the pre-publication no-media state before the
        // coordinator disconnects.
        if (scsi_cmd[0] == USB_SCSI_CMD_TEST_UNIT_READY) {
            (void)usb_note_initial_msc_command_complete();
        }
        return;
    }

    usb_scsi_trace_note_command_complete(scsi_cmd);
    if (scsi_cmd[0] != USB_SCSI_CMD_START_STOP_UNIT) {
        return;
    }

    load_eject = (scsi_cmd[4] & 0x02u) != 0;
    start = (scsi_cmd[4] & 0x01u) != 0;
    if (load_eject && !start) {
        (void)usb_request_explicit_eject();
    }
}

esp_err_t usb_msc_ownership_init(void) {
    esp_err_t err;
    if (s_initialized) {
        return ESP_OK;
    }
    if (!sd_mount_is_mounted()) {
        return ESP_ERR_INVALID_STATE;
    }
    s_usb_events = xEventGroupCreate();
    if (s_usb_events == NULL) {
        return ESP_ERR_NO_MEM;
    }
    err = tinyusb_msc_set_storage_callback(usb_storage_event_cb, NULL);
    if (err != ESP_OK) {
        vEventGroupDelete(s_usb_events);
        s_usb_events = NULL;
        return err;
    }
    s_starting = false;
    s_started = false;
    s_storage_usb_owned = false;
    s_host_owned = false;
    atomic_store_explicit(&s_release_pending, false, memory_order_release);
    atomic_store_explicit(&s_release_waiting_response, false,
                          memory_order_release);
    s_release_attempt_id[0] = '\0';
    s_provisional_attached = false;
    s_publish_triggered = false;
    s_prepare_disconnected = false;
    s_transfer_authorized = false;
    s_initialized = true;
    return ESP_OK;
}

static void usb_msc_reject_post_eject_command(uint8_t lun) {
    (void)tud_msc_set_sense(lun,
                            USB_SCSI_SENSE_NOT_READY,
                            USB_SCSI_ASC_MEDIUM_NOT_PRESENT,
                            USB_SCSI_ASCQ_NONE);
}

bool __wrap_tud_msc_test_unit_ready_cb(uint8_t lun) {
    if (!s_release_pending) {
        return __real_tud_msc_test_unit_ready_cb(lun);
    }
    usb_msc_reject_post_eject_command(lun);
    return false;
}

void __wrap_tud_msc_capacity_cb(uint8_t lun,
                                uint32_t *block_count,
                                uint16_t *block_size) {
    if (!s_release_pending) {
        __real_tud_msc_capacity_cb(lun, block_count, block_size);
        return;
    }
    if (block_count != NULL) {
        *block_count = 0;
    }
    if (block_size != NULL) {
        *block_size = 0;
    }
    usb_msc_reject_post_eject_command(lun);
}

int32_t __wrap_tud_msc_read10_cb(uint8_t lun,
                                 uint32_t lba,
                                 uint32_t offset,
                                 void *buffer,
                                 uint32_t bufsize) {
    if (!s_release_pending) {
        return __real_tud_msc_read10_cb(lun, lba, offset, buffer, bufsize);
    }
    usb_msc_reject_post_eject_command(lun);
    return -1;
}

int32_t __wrap_tud_msc_write10_cb(uint8_t lun,
                                  uint32_t lba,
                                  uint32_t offset,
                                  uint8_t *buffer,
                                  uint32_t bufsize) {
    if (!s_release_pending) {
        return __real_tud_msc_write10_cb(lun, lba, offset, buffer, bufsize);
    }
    usb_msc_reject_post_eject_command(lun);
    return -1;
}

esp_err_t usb_msc_ownership_start(void) {
    tinyusb_config_t config;
    esp_err_t err;

    if (!s_initialized || !sd_mount_is_mounted() || s_storage_usb_owned ||
        s_host_owned || s_release_pending || s_starting) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_started) {
        return ESP_OK;
    }
    config = (tinyusb_config_t)TINYUSB_DEFAULT_CONFIG();
    config.event_cb = usb_device_event_cb;
    config.event_arg = NULL;

    // The driver can publish ATTACHED from its USB task before install returns.
    // Admit that callback only for this exact bounded start operation.
    s_starting = true;
    err = tinyusb_driver_install(&config);
    if (err == ESP_OK) {
        s_started = true;
        // ATTACHED/SCSI probing may already have started before install
        // returns. Publication disconnect and storage ownership work remain in
        // the lower-priority recorder coordinator, never inside TinyUSB
        // SetConfiguration/SCSI callbacks.
        ESP_LOGI(TAG, "stage: usb, result: driver-ready");
    }
    s_starting = false;
    return err;
}

usb_msc_ownership_event_t usb_msc_ownership_wait_event(uint32_t timeout_ms) {
    EventBits_t bits;
    TickType_t ticks;
    EventBits_t selected = 0;
    usb_msc_ownership_event_t event = USB_MSC_EVENT_NONE;

    if (s_usb_events == NULL) {
        return USB_MSC_EVENT_FAILED;
    }
    ticks = timeout_ms == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);
    bits = xEventGroupWaitBits(s_usb_events, USB_PUBLIC_BITS,
                               pdFALSE, pdFALSE, ticks);
    if ((bits & USB_BIT_FAILED) != 0) {
        selected = USB_BIT_FAILED;
        event = USB_MSC_EVENT_FAILED;
    } else if ((bits & USB_BIT_ATTACH) != 0) {
        selected = USB_BIT_ATTACH;
        event = USB_MSC_EVENT_ATTACH;
    } else if ((bits & USB_BIT_HOST_OWNED) != 0) {
        selected = USB_BIT_HOST_OWNED;
        event = USB_MSC_EVENT_HOST_OWNED;
    } else if ((bits & USB_BIT_RELEASE_REQUESTED) != 0) {
        selected = USB_BIT_RELEASE_REQUESTED;
        event = USB_MSC_EVENT_RELEASE_REQUESTED;
    } else if ((bits & USB_BIT_RELEASE_QUIESCED) != 0) {
        selected = USB_BIT_RELEASE_QUIESCED;
        event = USB_MSC_EVENT_RELEASE_QUIESCED;
    }
    if (selected != 0) {
        xEventGroupClearBits(s_usb_events, selected);
    }
    return event;
}

esp_err_t usb_msc_ownership_begin_prepare(void) {
    if (!s_initialized || (!s_started && !s_starting) ||
        !s_provisional_attached || !s_publish_triggered ||
        s_storage_usb_owned || s_host_owned || s_release_pending ||
        !sd_mount_is_mounted()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_prepare_disconnected) {
        return ESP_OK;
    }

    // This runs in recorder_usb_event_task after an MSC command's CSW/status
    // has completed. It must never run from tud_mount_cb or a SCSI callback.
    if (!tud_disconnect()) {
        usb_fail("publication disconnect");
        return ESP_FAIL;
    }
    s_prepare_disconnected = true;
    ESP_LOGI(TAG,
             "stage: usb, result: publication-disconnect, owner: device, action: finalize");
    return ESP_OK;
}

esp_err_t usb_msc_ownership_note_prepare_complete(bool wav_finalized,
                                                   bool manifest_committed,
                                                   bool device_fs_released) {
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    return usb_msc_publish(wav_finalized, manifest_committed,
                           device_fs_released)
               ? ESP_OK
               : ESP_ERR_INVALID_STATE;
}

esp_err_t usb_msc_ownership_complete_release_quiesce(void) {
    esp_err_t err;

    if (!s_initialized || !s_started || !s_storage_usb_owned ||
        !s_host_owned ||
        !atomic_load_explicit(&s_release_pending, memory_order_acquire) ||
        atomic_load_explicit(&s_release_waiting_response,
                             memory_order_acquire) ||
        sd_mount_is_mounted()) {
        return ESP_ERR_INVALID_STATE;
    }

    // Stop the USB task/PHY before touching its storage object. After this
    // returns, host block-I/O callbacks cannot reach the storage backend.
    err = tinyusb_driver_uninstall();
    if (err != ESP_OK) {
        usb_fail("usb teardown");
        return err;
    }
    s_started = false;
    ESP_LOGI(TAG,
             "stage: usb, result: host-io-quiesced, owner: host-releasing");

    // esp_tinyusb rejects storage deletion while deferred writes remain. A
    // successful delete is therefore the zero-pending-write proof. No APP
    // storage or VFS mount is built in this powered session.
    err = sd_mount_release_usb_storage();
    if (err != ESP_OK) {
        usb_fail("usb storage release");
        return err;
    }

    // Retire HOST_UNRESOLVED only after the host path is unreachable and the
    // USB-owned storage object has been released. If this durable transition
    // fails, keep volatile host ownership unresolved as well; a later reboot
    // still sees HOST_UNRESOLVED and cannot mount/write the Device filesystem.
    err = shutdown_armed_commit();
    if (err != ESP_OK) {
        usb_fail("persist shutdown armed");
        return err;
    }

    s_storage_usb_owned = false;
    s_host_owned = false;
    atomic_store_explicit(&s_release_pending, false, memory_order_release);
    atomic_store_explicit(&s_release_waiting_response, false,
                          memory_order_release);
    s_release_attempt_id[0] = '\0';
    s_provisional_attached = false;
    s_publish_triggered = false;
    s_prepare_disconnected = false;
    s_transfer_authorized = false;
    xEventGroupClearBits(s_usb_events,
                         USB_BIT_ATTACH | USB_BIT_HOST_OWNED |
                             USB_BIT_RELEASE_REQUESTED);
    xEventGroupSetBits(s_usb_events, USB_BIT_RELEASE_QUIESCED);
    ESP_LOGI(TAG,
             "stage: usb, result: release-quiesced, owner: released, mount: none");
    return ESP_OK;
}

bool usb_msc_ownership_is_host_owned(void) {
    return s_host_owned;
}

#else

esp_err_t usb_msc_ownership_init(void) { return ESP_ERR_NOT_SUPPORTED; }
esp_err_t usb_msc_ownership_start(void) { return ESP_ERR_NOT_SUPPORTED; }
usb_msc_ownership_event_t usb_msc_ownership_wait_event(uint32_t timeout_ms) {
    (void)timeout_ms;
    return USB_MSC_EVENT_NONE;
}
esp_err_t usb_msc_ownership_begin_prepare(void) {
    return ESP_ERR_NOT_SUPPORTED;
}
esp_err_t usb_msc_ownership_note_prepare_complete(bool wav_finalized,
                                                   bool manifest_committed,
                                                   bool device_fs_released) {
    (void)wav_finalized;
    (void)manifest_committed;
    (void)device_fs_released;
    return ESP_ERR_NOT_SUPPORTED;
}
usb_msc_release_storage_result_t usb_msc_ownership_accept_release_storage(
    const char *release_attempt_id) {
    (void)release_attempt_id;
    return USB_MSC_RELEASE_STORAGE_WRONG_STATE;
}
bool usb_msc_ownership_release_response_complete(
    const char *release_attempt_id) {
    (void)release_attempt_id;
    return false;
}
bool usb_msc_ownership_release_command_admission_open(void) { return false; }
esp_err_t usb_msc_ownership_complete_release_quiesce(void) {
    return ESP_ERR_NOT_SUPPORTED;
}
esp_err_t usb_msc_ownership_flush_scsi_trace(void) {
    return ESP_ERR_NOT_SUPPORTED;
}
bool usb_msc_ownership_is_host_owned(void) { return false; }

#endif