// Task #49: USB MSC ownership gate for the recorder microSD.
//
// Security/integrity invariant: the USB host never receives block-device
// ownership while the recorder can still access FAT/VFS. TinyUSB calls the
// MSC MOUNT_START callback before it unregisters the application filesystem;
// that callback blocks until the recorder proves finalize, manifest commit,
// and Device-side filesystem release. Logs contain lifecycle metadata only.

#include "usb_msc_ownership.h"

#ifdef ESP_PLATFORM

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "tinyusb_msc.h"

#include "sd_mount.h"

static const char *TAG = "recorder_usb";

#define USB_BIT_ATTACH        (1u << 0)
#define USB_BIT_PREPARE_OK    (1u << 1)
#define USB_BIT_HOST_OWNED    (1u << 2)
#define USB_BIT_DETACH        (1u << 3)
#define USB_BIT_FAILED        (1u << 4)
#define USB_PUBLIC_BITS       (USB_BIT_ATTACH | USB_BIT_HOST_OWNED | \
                               USB_BIT_DETACH | USB_BIT_FAILED)

static EventGroupHandle_t s_usb_events = NULL;
static volatile bool s_initialized = false;
static volatile bool s_started = false;
static volatile bool s_host_owned = false;
static bool s_wav_finalized = false;
static bool s_manifest_committed = false;
static bool s_device_fs_released = false;

static bool usb_msc_publish(bool wav_finalized,
                            bool manifest_committed,
                            bool device_fs_released) {
    if (!wav_finalized || !manifest_committed || !device_fs_released ||
        s_usb_events == NULL) {
        ESP_LOGE(TAG,
                 "stage: usb, result: error, reason: publish gate incomplete");
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

    if (event->id == TINYUSB_MSC_EVENT_MOUNT_START &&
        event->mount_point == TINYUSB_MSC_STORAGE_MOUNT_APP) {
        // Host attach: TinyUSB has not unmounted VFS yet. Wake the recorder
        // coordinator and block this TinyUSB task until all Device file I/O
        // is complete. This is the critical no-dual-ownership barrier.
        s_wav_finalized = false;
        s_manifest_committed = false;
        s_device_fs_released = false;
        xEventGroupClearBits(s_usb_events, USB_BIT_PREPARE_OK);
        xEventGroupSetBits(s_usb_events, USB_BIT_ATTACH);
        ESP_LOGI(TAG,
                 "stage: usb, result: attach, owner: device, mount: app");
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
            sd_mount_note_app_owned();
            s_host_owned = false;
            if (s_started) {
                xEventGroupSetBits(s_usb_events, USB_BIT_DETACH);
                ESP_LOGI(TAG,
                         "stage: usb, result: detach, owner: device, mount: app");
            }
        }
        return;
    }

    if (event->id == TINYUSB_MSC_EVENT_MOUNT_FAILED ||
        event->id == TINYUSB_MSC_EVENT_FORMAT_REQUIRED ||
        event->id == TINYUSB_MSC_EVENT_FORMAT_FAILED) {
        xEventGroupSetBits(s_usb_events, USB_BIT_FAILED);
        ESP_LOGE(TAG,
                 "stage: usb, result: error, reason: mount ownership switch");
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
    s_initialized = true;
    return ESP_OK;
}

esp_err_t usb_msc_ownership_start(void) {
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_started) {
        return ESP_OK;
    }
    tinyusb_config_t config = TINYUSB_DEFAULT_CONFIG();
    esp_err_t err = tinyusb_driver_install(&config);
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

bool usb_msc_ownership_is_host_owned(void) {
    return false;
}

#endif
