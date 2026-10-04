// Tasks #49/#87: exclusive recorder microSD ownership with Strategy 2 eject.
//
// Ambiguous suspend/detach never authorizes Device ownership. The only normal
// reverse trigger is SCSI START STOP UNIT(load_eject=1,start=0), intercepted by
// the linker's one-to-one wrapper around esp_tinyusb 2.2.1's callback.

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
        s_usb_events == NULL) {
        usb_fail("publish gate incomplete");
        return false;
    }

    // The boot-visible unresolved marker must be durable before PREPARE_OK can
    // admit the APP -> USB storage transfer. A reset from this point onward can
    // therefore never forget that explicit host release is still required.
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
        ESP_LOGI(TAG, "stage: usb, result: ownership-switch-start");
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
    (void)arg;
    if (event == NULL || s_usb_events == NULL) {
        return;
    }

    if (event->id == TINYUSB_EVENT_ATTACHED) {
        esp_err_t err;

        // Accidental cable removal before safe-eject leaves the medium owned by
        // USB. A later physical reconnect may re-enumerate that same host-owned
        // session so the PC can perform the missing explicit eject.
        if (s_host_owned && !s_release_pending) {
            ESP_LOGW(TAG,
                     "stage: usb, result: reattach, owner: host, release: unproven");
            return;
        }
        if (s_host_owned || s_release_pending || !sd_mount_is_mounted()) {
            (void)tud_disconnect();
            usb_fail("attach state");
            return;
        }

        s_wav_finalized = false;
        s_manifest_committed = false;
        s_device_fs_released = false;
        xEventGroupClearBits(s_usb_events, USB_BIT_PREPARE_OK);
        xEventGroupSetBits(s_usb_events, USB_BIT_ATTACH);
        ESP_LOGI(TAG,
                 "stage: usb, result: attach, owner: device, mount: app");

        // Do not finish SetConfiguration until recorder finalization, durable
        // metadata, boot-visible unresolved ownership, and logical Device-FS
        // release are all proven.
        (void)xEventGroupWaitBits(s_usb_events, USB_BIT_PREPARE_OK,
                                  pdTRUE, pdTRUE, portMAX_DELAY);
        err = sd_mount_transfer_to_usb();
        if (err != ESP_OK || !s_host_owned || sd_mount_is_mounted()) {
            (void)tud_disconnect();
            usb_fail("host ownership transfer");
        }
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

// GNU ld --wrap gives a one-to-one seam over the strong callback provided by
// esp_tinyusb 2.2.1 without defining a duplicate tud_msc_start_stop_cb symbol.
extern bool __real_tud_msc_start_stop_cb(uint8_t lun,
                                         uint8_t power_condition,
                                         bool start,
                                         bool load_eject);

bool __wrap_tud_msc_start_stop_cb(uint8_t lun,
                                  uint8_t power_condition,
                                  bool start,
                                  bool load_eject) {
    if (load_eject && !start) {
        if (!s_initialized || !s_started || !s_host_owned ||
            s_release_pending || sd_mount_is_mounted()) {
            usb_fail("explicit eject state");
            return false;
        }

        // Mark first so no concurrent/repeated eject can open another release
        // path. Logical disconnect blocks new host command admission; runtime
        // then tears down TinyUSB before releasing the storage object.
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

    return __real_tud_msc_start_stop_cb(lun, power_condition, start, load_eject);
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
