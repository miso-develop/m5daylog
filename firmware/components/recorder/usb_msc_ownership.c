// Tasks #49/#87: exclusive recorder microSD ownership with Strategy 2 eject.
//
// APP -> USB publication is gated before SetConfiguration completes. With
// esp_tinyusb 2.2.1 and auto_mount_off=1, its synchronous ATTACHED device event
// is the reliable entry point into the storage transfer barrier: MOUNT_START
// wakes the recorder coordinator and blocks until recorder finalization,
// durable manifest state, and Device-FS release are proven. The legacy linker
// wraps remain compatible fallback seams but are not required for the real
// TinyUSB callback ordering. Generic detach / suspend never authorizes Device
// ownership. The only normal reverse trigger is SCSI START STOP UNIT
// (load_eject=1,start=0), observed after command completion as well as by the
// compatibility wrapper.

#include "usb_msc_ownership.h"

#ifdef ESP_PLATFORM

#include <stddef.h>
#include <stdint.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "sd_mount.h"
#include "shutdown_armed.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "tinyusb_msc.h"
#include "tusb.h"

static const char *TAG = "recorder_usb";

#define USB_BIT_ATTACH              (1u << 0)
#define USB_BIT_PREPARE_OK          (1u << 1)
#define USB_BIT_HOST_OWNED          (1u << 2)
#define USB_BIT_RELEASE_REQUESTED   (1u << 3)
#define USB_BIT_RELEASE_QUIESCED    (1u << 4)
#define USB_BIT_FAILED              (1u << 5)
#define USB_PUBLIC_BITS             (USB_BIT_ATTACH | USB_BIT_HOST_OWNED | \
                                     USB_BIT_RELEASE_REQUESTED | \
                                     USB_BIT_RELEASE_QUIESCED | USB_BIT_FAILED)
#define USB_SCSI_CMD_START_STOP_UNIT 0x1bu

static EventGroupHandle_t s_usb_events = NULL;
static volatile bool s_initialized = false;
static volatile bool s_started = false;
static volatile bool s_host_owned = false;
static volatile bool s_release_pending = false;
static bool s_wav_finalized = false;
static bool s_manifest_committed = false;
static bool s_device_fs_released = false;

static void usb_fail(const char *reason) {
    if (s_usb_events != NULL) {
        xEventGroupSetBits(s_usb_events, USB_BIT_FAILED);
    }
    ESP_LOGE(TAG, "stage: usb, result: error, reason: %s", reason);
}

static bool usb_msc_publish(bool wav_finalized,
                            bool manifest_committed,
                            bool device_fs_released) {
    if (!wav_finalized || !manifest_committed || !device_fs_released ||
        s_usb_events == NULL || s_host_owned || s_release_pending ||
        !sd_mount_is_mounted()) {
        usb_fail("publish gate incomplete");
        return false;
    }

    // Make unresolved host ownership boot-visible before releasing the
    // pre-configuration storage barrier. From this point a reset can never
    // silently return to Device-owned recording while host release is unproven.
    if (shutdown_armed_mark_host_unresolved() != ESP_OK) {
        usb_fail("persist unresolved ownership");
        return false;
    }

    s_wav_finalized = wav_finalized;
    s_manifest_committed = manifest_committed;
    s_device_fs_released = device_fs_released;
    xEventGroupSetBits(s_usb_events, USB_BIT_PREPARE_OK);
    ESP_LOGI(TAG,
             "stage: usb, result: publish-ready, owner: device-released, mount: app");
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
            s_host_owned || s_release_pending || !sd_mount_is_mounted()) {
            usb_fail("unexpected ownership switch");
            return;
        }

        // esp_tinyusb invokes this callback before it unregisters APP FAT/VFS.
        // Wake the recorder coordinator, then block the TinyUSB task until all
        // Device file I/O is finalized and HOST_UNRESOLVED is durable. Only
        // after this callback returns can the storage become USB-ready and the
        // surrounding SetConfiguration handling complete.
        s_wav_finalized = false;
        s_manifest_committed = false;
        s_device_fs_released = false;
        xEventGroupClearBits(s_usb_events, USB_BIT_PREPARE_OK);
        xEventGroupSetBits(s_usb_events, USB_BIT_ATTACH);
        ESP_LOGI(TAG,
                 "stage: usb, result: prepare, owner: device, mount: app");
        (void)xEventGroupWaitBits(s_usb_events, USB_BIT_PREPARE_OK,
                                  pdTRUE, pdTRUE, portMAX_DELAY);
        return;
    }

    if (event->id == TINYUSB_MSC_EVENT_MOUNT_COMPLETE) {
        if (event->mount_point == TINYUSB_MSC_STORAGE_MOUNT_USB) {
            sd_mount_note_usb_owned();
            s_host_owned = true;
            xEventGroupSetBits(s_usb_events, USB_BIT_HOST_OWNED);
            ESP_LOGI(TAG,
                     "stage: usb, result: mounted, owner: host, mount: usb");
        } else {
            // Strategy 2 never permits a same-session USB -> APP remount.
            usb_fail("unexpected app remount");
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
    esp_err_t err;

    (void)arg;
    if (event == NULL || s_usb_events == NULL) {
        return;
    }

    if (event->id == TINYUSB_EVENT_ATTACHED) {
        // esp_tinyusb emits this synchronously from its tud_mount_cb while
        // TinyUSB is still handling SET_CONFIGURATION. With auto_mount_off=1
        // storage is still APP-owned, so enter the same blocking MOUNT_START
        // barrier here. This path does not depend on GNU ld --wrap reaching the
        // TinyUSB translation unit's internal tud_mount_cb call.
        if (!s_host_owned) {
            if (!s_initialized || !s_started || s_release_pending ||
                !sd_mount_is_mounted()) {
                (void)tud_disconnect();
                usb_fail("attach ownership state");
                return;
            }
            err = sd_mount_transfer_to_usb();
            if (err != ESP_OK || !s_host_owned || sd_mount_is_mounted()) {
                (void)tud_disconnect();
                usb_fail("host ownership transfer");
                return;
            }
        }

        // A later physical reconnect may reuse the same unresolved host-owned
        // session so the PC can perform the still-required explicit eject.
        if (!s_release_pending && !sd_mount_is_mounted()) {
            ESP_LOGI(TAG,
                     "stage: usb, result: configured, owner: host, mount: usb");
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
        if (s_host_owned) {
            ESP_LOGW(TAG,
                     "stage: usb, result: ambiguous-detach, owner: host, action: none");
        }
    }
}

// Compatibility seam for the original implementation. The current pinned
// TinyUSB keeps its tud_mount_cb call inside the same translation unit as the
// weak definition, so production correctness no longer depends on this GNU ld
// wrap being reached. If a toolchain/library layout does route through it, the
// same storage barrier remains safe and idempotent with the ATTACHED callback.
extern void __real_tud_mount_cb(void);

void __wrap_tud_mount_cb(void) {
    esp_err_t err;

    if (!s_initialized || !s_started || s_release_pending) {
        (void)tud_disconnect();
        usb_fail("mount callback state");
        return;
    }

    if (s_host_owned) {
        if (sd_mount_is_mounted()) {
            (void)tud_disconnect();
            usb_fail("reattach ownership mismatch");
            return;
        }
        __real_tud_mount_cb();
        return;
    }

    if (!sd_mount_is_mounted()) {
        (void)tud_disconnect();
        usb_fail("mount callback without device storage");
        return;
    }

    err = sd_mount_transfer_to_usb();
    if (err != ESP_OK || !s_host_owned || sd_mount_is_mounted()) {
        (void)tud_disconnect();
        usb_fail("host ownership transfer");
        return;
    }

    __real_tud_mount_cb();
}

static bool usb_request_explicit_eject(void) {
    // If a compatibility wrap already observed this exact command, the SCSI
    // completion callback must be idempotent rather than converting a valid
    // release into an error.
    if (s_release_pending) {
        return true;
    }
    if (!s_initialized || !s_started || !s_host_owned ||
        sd_mount_is_mounted()) {
        usb_fail("explicit eject state");
        return false;
    }

    // Mark first so no concurrent/repeated eject can open another release path.
    // The SCSI-completion path reaches this only after command status has been
    // transferred; logical disconnect then blocks new host command admission.
    s_release_pending = true;
    if (!tud_disconnect()) {
        usb_fail("explicit eject disconnect");
        return false;
    }
    xEventGroupSetBits(s_usb_events, USB_BIT_RELEASE_REQUESTED);
    ESP_LOGI(TAG,
             "stage: usb, result: explicit-eject, owner: host, action: release-quiesce");
    return true;
}

// Compatibility seam for the original implementation. The reliable production
// observation is tud_msc_scsi_complete_cb below, which TinyUSB exposes as a weak
// callback and esp_tinyusb 2.2.1 does not override.
extern bool __real_tud_msc_start_stop_cb(uint8_t lun,
                                         uint8_t power_condition,
                                         bool start,
                                         bool load_eject);

bool __wrap_tud_msc_start_stop_cb(uint8_t lun,
                                  uint8_t power_condition,
                                  bool start,
                                  bool load_eject) {
    if (load_eject && !start) {
        return usb_request_explicit_eject();
    }

    return __real_tud_msc_start_stop_cb(lun, power_condition, start, load_eject);
}

void tud_msc_scsi_complete_cb(uint8_t lun, uint8_t const scsi_cmd[16]) {
    bool load_eject;
    bool start;

    (void)lun;
    if (scsi_cmd == NULL || scsi_cmd[0] != USB_SCSI_CMD_START_STOP_UNIT) {
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
    s_host_owned = false;
    s_release_pending = false;
    s_initialized = true;
    return ESP_OK;
}

esp_err_t usb_msc_ownership_start(void) {
    tinyusb_config_t config;
    esp_err_t err;

    if (!s_initialized || !sd_mount_is_mounted() || s_host_owned ||
        s_release_pending) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_started) {
        return ESP_OK;
    }
    config = (tinyusb_config_t)TINYUSB_DEFAULT_CONFIG();
    config.event_cb = usb_device_event_cb;
    config.event_arg = NULL;
    err = tinyusb_driver_install(&config);
    if (err == ESP_OK) {
        s_started = true;
        ESP_LOGI(TAG,
                 "stage: usb, result: ready, owner: device, mount: app");
    }
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

    if (!s_initialized || !s_started || !s_host_owned ||
        !s_release_pending || sd_mount_is_mounted()) {
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

    s_host_owned = false;
    s_release_pending = false;
    xEventGroupClearBits(s_usb_events,
                         USB_BIT_ATTACH | USB_BIT_PREPARE_OK |
                             USB_BIT_HOST_OWNED | USB_BIT_RELEASE_REQUESTED);
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
esp_err_t usb_msc_ownership_note_prepare_complete(bool wav_finalized,
                                                   bool manifest_committed,
                                                   bool device_fs_released) {
    (void)wav_finalized;
    (void)manifest_committed;
    (void)device_fs_released;
    return ESP_ERR_NOT_SUPPORTED;
}
esp_err_t usb_msc_ownership_complete_release_quiesce(void) {
    return ESP_ERR_NOT_SUPPORTED;
}
bool usb_msc_ownership_is_host_owned(void) { return false; }

#endif