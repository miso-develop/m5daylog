#include "release_cycle_fence.h"

#ifdef ESP_PLATFORM

#include <stdint.h>
#include <stddef.h>
#include <stdatomic.h>

#include "nvs.h"
#include "recorder_nvs.h"

// Values are deliberately distinct from a missing key. This additional key
// never changes the existing shutdown_armed lifecycle semantics.
static const char *const k_namespace = "m5daylog";
static const char *const k_fence_key = "release_boot";
static const char *const k_lifecycle_key = "shutdown_armed";

enum {
    FENCE_UNQUALIFIED = 0x51u,
    FENCE_BOOT_ACTIVE = 0x52u,
    FENCE_READY_AFTER_RELEASE = 0x53u,
    FENCE_FU_STAGED = 0x54u,
    FENCE_FU_READY_AFTER_RELEASE = 0x55u,
    LIFECYCLE_SHUTDOWN_ARMED = 1u,
    LIFECYCLE_HOST_UNRESOLVED = 2u,
};

static bool s_boot_checked;
static bool s_boot_admitted;
static bool s_this_boot_qualified;
static bool s_saw_host_owned;
static const char *s_boot_origin;
static _Atomic bool s_first_use_staged;
static _Atomic bool s_first_use_release_accepted;

esp_err_t release_cycle_fence_begin_boot(bool manual_wake) {
    nvs_handle_t handle = 0;
    uint8_t lifecycle = 0;
    uint8_t fence = FENCE_UNQUALIFIED;
    esp_err_t err;

    // The latch cannot be acquired a second time in the same process boot.
    if (s_boot_checked) {
        return ESP_ERR_INVALID_STATE;
    }
    s_boot_checked = true;
    s_boot_admitted = false;
    s_this_boot_qualified = false;
    s_saw_host_owned = false;
    s_boot_origin = NULL;
    atomic_store(&s_first_use_staged, false);
    atomic_store(&s_first_use_release_accepted, false);
    if (!manual_wake) {
        return ESP_OK;
    }

    err = recorder_nvs_lock();
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_open(k_namespace, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        goto done;
    }
    // Holding the same recorder NVS lock across checking lifecycle and
    // consuming READY prevents a second recorder-owned write from racing.
    err = nvs_get_u8(handle, k_lifecycle_key, &lifecycle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        err = ESP_OK; // legacy/erased NVS: not enrolled
        goto done;
    }
    if (err != ESP_OK) {
        goto done;
    }
    err = nvs_get_u8(handle, k_fence_key, &fence);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        err = ESP_OK; // no implicit first-use enrollment
        goto done;
    }
    if (err != ESP_OK) {
        goto done;
    }
    if (lifecycle != LIFECYCLE_SHUTDOWN_ARMED ||
        (fence != FENCE_READY_AFTER_RELEASE &&
         fence != FENCE_FU_READY_AFTER_RELEASE)) {
        // NORMAL, HOST_UNRESOLVED, interrupted WAKE, stale BOOT_ACTIVE,
        // corrupt/unknown fence: recorder recovery may proceed, but this
        // process boot cannot be authorized for unattended release.
        err = fence == FENCE_UNQUALIFIED ||
                      fence == FENCE_BOOT_ACTIVE ||
                      fence == FENCE_READY_AFTER_RELEASE ||
                      fence == FENCE_FU_STAGED ||
                      fence == FENCE_FU_READY_AFTER_RELEASE
                  ? ESP_OK : ESP_ERR_INVALID_STATE;
        goto done;
    }
    // Durable used-permit write MUST precede current-boot qualification
    // and precede #87's WAKE_RECOVERY_PENDING -> NORMAL transition.
    err = nvs_set_u8(handle, k_fence_key, FENCE_BOOT_ACTIVE);
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    if (err == ESP_OK) {
        s_boot_admitted = true;
        s_boot_origin = fence == FENCE_FU_READY_AFTER_RELEASE
            ? "SUPERVISED_FIRST_USE" : "NORMAL_WAKE";
    }

done:
    if (handle != 0) {
        nvs_close(handle);
    }
    recorder_nvs_unlock();
    return err;
}

esp_err_t release_cycle_fence_note_recorder_ready(void) {
    nvs_handle_t handle = 0;
    uint8_t lifecycle = 0;
    uint8_t fence = 0;
    esp_err_t err;
    if (!s_boot_admitted || s_this_boot_qualified) {
        return ESP_ERR_INVALID_STATE;
    }
    err = recorder_nvs_lock();
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_open(k_namespace, NVS_READONLY, &handle);
    if (err == ESP_OK) {
        err = nvs_get_u8(handle, k_lifecycle_key, &lifecycle);
    }
    if (err == ESP_OK) {
        err = nvs_get_u8(handle, k_fence_key, &fence);
    }
    if (err == ESP_OK) {
        if (lifecycle != 0u /* NORMAL, fresh #87 recorder-ready */ ||
            fence != FENCE_BOOT_ACTIVE) {
            err = ESP_ERR_INVALID_STATE;
        } else {
            s_this_boot_qualified = true;
        }
    }
    if (handle != 0) {
        nvs_close(handle);
    }
    recorder_nvs_unlock();
    return err;
}

bool release_cycle_fence_boot_qualified(void) {
    return s_this_boot_qualified;
}

const char *release_cycle_fence_current_origin(void) {
    // A volatile origin exists only after a durable used-permit transition
    // and #87's recorder recovery. Never infer origin from UUID or RTC.
    return s_this_boot_qualified ? s_boot_origin : NULL;
}

esp_err_t release_cycle_fence_stage_first_use(void) {
    nvs_handle_t handle = 0;
    uint8_t lifecycle = 0;
    uint8_t fence = 0;
    uint8_t readback = 0;
    esp_err_t err;

    // Only a normal, unqualified process boot may stage; it cannot reuse
    // a completed/used boot or a prior interrupted staging attempt.
    if (!s_boot_checked || s_boot_admitted || s_this_boot_qualified ||
        atomic_load(&s_first_use_staged)) {
        return ESP_ERR_INVALID_STATE;
    }
    err = recorder_nvs_lock();
    if (err != ESP_OK) return err;
    err = nvs_open(k_namespace, NVS_READWRITE, &handle);
    if (err != ESP_OK) goto stage_done;
    err = nvs_get_u8(handle, k_lifecycle_key, &lifecycle);
    if (err != ESP_OK) goto stage_done;
    err = nvs_get_u8(handle, k_fence_key, &fence);
    // Missing NVS is NOT a positive virginity proof. The exceptional #119
    // route needs an explicitly present, previously qualified marker.
    if (err != ESP_OK) goto stage_done;
    if (lifecycle != LIFECYCLE_HOST_UNRESOLVED ||
        fence != FENCE_UNQUALIFIED) {
        err = ESP_ERR_INVALID_STATE;
        goto stage_done;
    }
    err = nvs_set_u8(handle, k_fence_key, FENCE_FU_STAGED);
    if (err == ESP_OK) err = nvs_commit(handle);
    if (err == ESP_OK) err = nvs_get_u8(handle, k_fence_key, &readback);
    if (err == ESP_OK && readback != FENCE_FU_STAGED) {
        err = ESP_ERR_INVALID_STATE;
    }
    if (err == ESP_OK) atomic_store(&s_first_use_staged, true);

stage_done:
    if (handle != 0) nvs_close(handle);
    recorder_nvs_unlock();
    return err;
}

bool release_cycle_fence_first_use_staged(void) {
    return atomic_load(&s_first_use_staged);
}

void release_cycle_fence_note_canonical_release_accepted(void) {
    if (atomic_load(&s_first_use_staged)) {
        atomic_store(&s_first_use_release_accepted, true);
    }
}

void release_cycle_fence_note_physical_session_cutoff(void) {
    // If release was already accepted, TinyUSB teardown is expected and
    // must not revoke its committed one-shot. Before acceptance, an actual
    // cable/session cutoff burns the current staged intent in RAM; NVS
    // FU_STAGED remains non-eligible and cannot auto-restage.
    if (!atomic_load(&s_first_use_release_accepted)) {
        atomic_store(&s_first_use_staged, false);
    }
}

esp_err_t release_cycle_fence_note_host_owned(void) {
    nvs_handle_t handle = 0;
    uint8_t lifecycle = 0;
    uint8_t fence = 0;
    esp_err_t err;
    if (!s_this_boot_qualified || s_saw_host_owned) {
        return ESP_ERR_INVALID_STATE;
    }
    err = recorder_nvs_lock();
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_open(k_namespace, NVS_READONLY, &handle);
    if (err == ESP_OK) {
        err = nvs_get_u8(handle, k_lifecycle_key, &lifecycle);
    }
    if (err == ESP_OK) {
        err = nvs_get_u8(handle, k_fence_key, &fence);
    }
    if (err == ESP_OK) {
        if (lifecycle != 2u /* HOST_UNRESOLVED */ ||
            fence != FENCE_BOOT_ACTIVE) {
            err = ESP_ERR_INVALID_STATE;
        } else {
            s_saw_host_owned = true;
        }
    }
    if (handle != 0) {
        nvs_close(handle);
    }
    recorder_nvs_unlock();
    return err;
}

bool release_cycle_fence_host_owned_qualified(void) {
    return s_this_boot_qualified && s_saw_host_owned;
}

esp_err_t release_cycle_fence_note_release_completed(void) {
    nvs_handle_t handle = 0;
    uint8_t lifecycle = 0;
    uint8_t fence = 0;
    esp_err_t err;

    const bool first_use = atomic_load(&s_first_use_staged) &&
        atomic_load(&s_first_use_release_accepted);
    if (!first_use && (!s_this_boot_qualified || !s_saw_host_owned)) {
        return ESP_ERR_INVALID_STATE;
    }
    // After an accepted D-031 request, #87 has already transitioned
    // HOST_UNRESOLVED -> SHUTDOWN_ARMED. This last step re-arms the NEXT
    // physical WAKE; a crash between the two commits leaves BOOT_ACTIVE
    // and cannot reinterpret stale SHUTDOWN_ARMED as a fresh opportunity.
    err = recorder_nvs_lock();
    if (err != ESP_OK) {
        s_this_boot_qualified = false;
        return err;
    }
    err = nvs_open(k_namespace, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        goto done;
    }
    err = nvs_get_u8(handle, k_lifecycle_key, &lifecycle);
    if (err != ESP_OK) {
        goto done;
    }
    err = nvs_get_u8(handle, k_fence_key, &fence);
    if (err != ESP_OK) {
        goto done;
    }
    if (lifecycle != LIFECYCLE_SHUTDOWN_ARMED ||
        fence != (first_use ? FENCE_FU_STAGED : FENCE_BOOT_ACTIVE)) {
        err = ESP_ERR_INVALID_STATE;
        goto done;
    }
    err = nvs_set_u8(handle, k_fence_key, first_use
                     ? FENCE_FU_READY_AFTER_RELEASE
                     : FENCE_READY_AFTER_RELEASE);
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }

done:
    if (handle != 0) {
        nvs_close(handle);
    }
    recorder_nvs_unlock();
    // Even if NVS reports failure after a partial commit, NEVER retry from
    // this boot. The next boot will assess the durable value independently.
    s_this_boot_qualified = false;
    s_saw_host_owned = false;
    atomic_store(&s_first_use_staged, false);
    atomic_store(&s_first_use_release_accepted, false);
    return err;
}

#else

esp_err_t release_cycle_fence_begin_boot(bool manual_wake) {
    (void)manual_wake;
    return ESP_ERR_NOT_SUPPORTED;
}
esp_err_t release_cycle_fence_note_recorder_ready(void) {
    return ESP_ERR_NOT_SUPPORTED;
}
bool release_cycle_fence_boot_qualified(void) {
    return false;
}
const char *release_cycle_fence_current_origin(void) {
    return NULL;
}
esp_err_t release_cycle_fence_stage_first_use(void) {
    return ESP_ERR_NOT_SUPPORTED;
}
bool release_cycle_fence_first_use_staged(void) {
    return false;
}
void release_cycle_fence_note_canonical_release_accepted(void) {}
void release_cycle_fence_note_physical_session_cutoff(void) {}
esp_err_t release_cycle_fence_note_host_owned(void) {
    return ESP_ERR_NOT_SUPPORTED;
}
bool release_cycle_fence_host_owned_qualified(void) {
    return false;
}
esp_err_t release_cycle_fence_note_release_completed(void) {
    return ESP_ERR_NOT_SUPPORTED;
}
#endif
