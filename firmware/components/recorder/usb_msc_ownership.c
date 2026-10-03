// Tasks #49/#87: USB MSC ownership gate for the recorder microSD.
//
// Security/integrity invariant: Device FAT/VFS and host MSC are never usable at
// the same time. APP -> USB publication blocks inside the TinyUSB ATTACHED
// callback until recorder finalize + manifest commit + logical filesystem
// release are proven. In the reverse direction, suspend/detach are only
// triggers: the coordinator must tear down TinyUSB and prove no deferred host
// write remains before APP ownership is rebuilt. Logs contain lifecycle
// metadata only.

#include "usb_msc_ownership.h"

#ifdef ESP_PLATFORM

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "tinyusb_msc.h"
#include "tusb.h"

#include "sd_mount.h"

static const char *TAG = "recorder_usb";

#define USB_BIT_ATTACH            (1u << 0)
#define USB_BIT_PREPARE_OK        (1u << 1)
#define USB_BIT_HOST_OWNED        (1u << 2)
#define USB_BIT_BARRIER_REQUIRED  (1u << 3)
#define USB_BIT_DETACH            (1u << 4)
#define USB_BIT_FAILED            (1u << 5)
#define USB_PUBLIC_BITS           (USB_BIT_ATTACH | USB_BIT_HOST_OWNED | \
                                   USB_BIT_BARRIER_REQUIRED | USB_BIT_DETACH | \
                                   USB_BIT_FAILED)

static EventGroupHandle_t s_usb_events = NULL;
static volatile bool s_initialized = false;
static volatile bool s_started = false;
static volatile bool s_host_owned = false;
static volatile bool s_barrier_pending = false;
static volatile bool s_barrier_app_mounted = true;
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
            s_barrier_app_mounted = false;
            xEventGroupSetBits(s_usb_events, USB_BIT_HOST_OWNED);
            ESP_LOGI(TAG,
                     "stage: usb, result: mounted, owner: host, mount: usb");
        } else {
            sd_mount_note_app_owned();
            s_host_owned = false;
            s_barrier_app_mounted = true;
            ESP_LOGI(TAG,
                     "stage: usb, result: ownership-return, owner: device, mount: app");
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

        if (s_host_owned || s_barrier_pending || !sd_mount_is_mounted()) {
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

        // SetConfiguration is not allowed to finish until the recorder proves
        // it has finalized metadata and relinquished all Device filesystem I/O.
        (void)xEventGroupWaitBits(s_usb_events, USB_BIT_PREPARE_OK,
                                  pdTRUE, pdTRUE, portMAX_DELAY);
        err = sd_mount_transfer_to_usb();
        if (err != ESP_OK || !s_host_owned || sd_mount_is_mounted()) {
            // The TinyUSB task is still inside the attach callback, so force a
            // logical disconnect before returning and before it can service
            // any host MSC request against an unproven ownership transition.
            (void)tud_disconnect();
            usb_fail("host ownership transfer");
        }
        return;
    }

#ifdef CONFIG_TINYUSB_SUSPEND_CALLBACK
    if (event->id == TINYUSB_EVENT_SUSPENDED) {
        if (s_host_owned && !s_barrier_pending) {
            s_barrier_pending = true;
            // Suspend/bus-loss is ambiguous. It may only remove the device
            // logically and request the explicit quiescence barrier; it never
            // authorizes APP mount by itself.
            if (!tud_disconnect()) {
                usb_fail("logical disconnect");
                return;
            }
            ESP_LOGI(TAG,
                     "stage: usb, result: barrier-start, trigger: suspend, owner: host");
            xEventGroupSetBits(s_usb_events, USB_BIT_BARRIER_REQUIRED);
        }
        return;
    }
#endif

    if (event->id == TINYUSB_EVENT_DETACHED) {
        if (s_host_owned && !s_barrier_pending) {
            s_barrier_pending = true;
            ESP_LOGI(TAG,
                     "stage: usb, result: barrier-start, trigger: detach, owner: host");
            xEventGroupSetBits(s_usb_events, USB_BIT_BARRIER_REQUIRED);
        }
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
    s_barrier_pending = false;
    s_barrier_app_mounted = true;
    s_initialized = true;
    return ESP_OK;
}

esp_err_t usb_msc_ownership_start(void) {
    tinyusb_config_t config;
    esp_err_t err;

    if (!s_initialized || !sd_mount_is_mounted() || s_host_owned) {
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
    } else if ((bits & USB_BIT_BARRIER_REQUIRED) != 0) {
        selected = USB_BIT_BARRIER_REQUIRED;
        event = USB_MSC_EVENT_BARRIER_REQUIRED;
    } else if ((bits & USB_BIT_DETACH) != 0) {
        selected = USB_BIT_DETACH;
        event = USB_MSC_EVENT_DETACH;
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

esp_err_t usb_msc_ownership_complete_disconnect_barrier(void) {
    esp_err_t err;

    if (!s_initialized || !s_started || !s_host_owned ||
        !s_barrier_pending || s_barrier_app_mounted) {
        return ESP_ERR_INVALID_STATE;
    }

    // No APP mount is attempted unless the complete TinyUSB device-side I/O
    // engine and PHY have been torn down successfully.
    err = tinyusb_driver_uninstall();
    if (err != ESP_OK) {
        usb_fail("usb teardown");
        return err;
    }
    s_started = false;
    ESP_LOGI(TAG,
             "stage: usb, result: host-io-quiesced, owner: host-released");

    // sd_mount_transfer_to_app() first deletes the USB-owned storage object.
    // esp_tinyusb refuses that deletion while deferred writes are pending, so
    // APP rebuild is additionally gated on a zero-pending-write proof.
    err = sd_mount_transfer_to_app();
    if (err != ESP_OK) {
        usb_fail("app ownership rebuild");
        return err;
    }
    if (!s_barrier_app_mounted || !sd_mount_is_mounted() || s_host_owned) {
        usb_fail("app ownership proof");
        return ESP_FAIL;
    }

    s_barrier_pending = false;
    xEventGroupClearBits(s_usb_events,
                         USB_BIT_ATTACH | USB_BIT_PREPARE_OK |
                             USB_BIT_HOST_OWNED | USB_BIT_BARRIER_REQUIRED);
    xEventGroupSetBits(s_usb_events, USB_BIT_DETACH);
    ESP_LOGI(TAG,
             "stage: usb, result: barrier-complete, owner: device, mount: app");
    return ESP_OK;
}

esp_err_t usb_msc_ownership_rearm(void) {
    esp_err_t err;

    if (!s_initialized || s_started || s_host_owned || s_barrier_pending ||
        !s_barrier_app_mounted || !sd_mount_is_mounted()) {
        return ESP_ERR_INVALID_STATE;
    }

    xEventGroupClearBits(s_usb_events,
                         USB_PUBLIC_BITS | USB_BIT_PREPARE_OK);
    s_wav_finalized = false;
    s_manifest_committed = false;
    s_device_fs_released = false;

    err = usb_msc_ownership_start();
    if (err != ESP_OK) {
        usb_fail("fresh usb session");
        return err;
    }
    ESP_LOGI(TAG,
             "stage: usb, result: rearmed, owner: device, session: fresh");
    return ESP_OK;
}

bool usb_msc_ownership_is_host_owned(void) {
    return s_host_owned;
}

#else

esp_err_t usb_msc_ownership_init(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t usb_msc_ownership_start(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

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

esp_err_t usb_msc_ownership_complete_disconnect_barrier(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t usb_msc_ownership_rearm(void) {
    return ESP_ERR_NOT_SUPPORTED;
}

bool usb_msc_ownership_is_host_owned(void) {
    return false;
}

#endif
