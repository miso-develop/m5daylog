#include "release_cycle_button.h"
#include <string.h>

bool release_cycle_button_detector_step(release_cycle_button_detector_t *s,
    bool pressed, bool custody, uint64_t now_us,
    uint32_t release_edges, uint32_t session_epoch) {
    if (s == NULL) return false;
    if (!pressed || !custody ||
        (s->tracking && (now_us < s->start_us ||
                         s->release_edges != release_edges ||
                         s->session_epoch != session_epoch))) {
        // A bounce/release or lost custody cannot reuse earlier hold time.
        memset(s, 0, sizeof(*s));
        return false;
    }
    if (s->fired) return false;
    if (!s->tracking) {
        s->start_us = now_us;
        s->release_edges = release_edges;
        s->session_epoch = session_epoch;
        s->tracking = true;
        return false;
    }
    if (now_us - s->start_us < RELEASE_CYCLE_BUTTON_HOLD_US) {
        return false;
    }
    s->fired = true;
    return true;
}

#ifdef ESP_PLATFORM

#include <stdatomic.h>
#include "driver/gpio.h"
#include "esp_intr_alloc.h"
#include "esp_timer.h"

#ifndef RECORDER_WAKE_BUTTON_PIN
#define RECORDER_WAKE_BUTTON_PIN 42
#endif

static _Atomic uint32_t s_release_edges;
static _Atomic uint32_t s_session_epoch;
static release_cycle_button_detector_t s_detector;
static bool s_initialized;

static void IRAM_ATTR release_cycle_gpio_released(void *ctx) {
    (void)ctx;
    // Rising edge is active-low button release, including short bounces
    // that a 250ms USB coordinator poll would otherwise miss.
    (void)atomic_fetch_add_explicit(&s_release_edges, 1u,
                                    memory_order_relaxed);
}

esp_err_t release_cycle_button_init(void) {
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << RECORDER_WAKE_BUTTON_PIN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_POSEDGE,
    };
    esp_err_t err;
    if (s_initialized) return ESP_OK;
    memset(&s_detector, 0, sizeof(s_detector));
    err = gpio_install_isr_service(ESP_INTR_FLAG_IRAM);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) return err;
    err = gpio_config(&config);
    if (err != ESP_OK) return err;
    err = gpio_isr_handler_add((gpio_num_t)RECORDER_WAKE_BUTTON_PIN,
                               release_cycle_gpio_released, NULL);
    if (err != ESP_OK) return err;
    err = gpio_intr_enable((gpio_num_t)RECORDER_WAKE_BUTTON_PIN);
    if (err != ESP_OK) {
        (void)gpio_isr_handler_remove((gpio_num_t)RECORDER_WAKE_BUTTON_PIN);
        return err;
    }
    s_initialized = true;
    return ESP_OK;
}

void release_cycle_button_note_session_cutoff(void) {
    (void)atomic_fetch_add_explicit(&s_session_epoch, 1u,
                                    memory_order_release);
}

uint32_t release_cycle_button_session_epoch(void) {
    return atomic_load_explicit(&s_session_epoch, memory_order_acquire);
}

bool release_cycle_button_poll(bool custody, uint32_t *witness_epoch) {
    uint32_t epoch;
    uint32_t edges;
    int level;
    int64_t now;
    bool ready;
    if (witness_epoch != NULL) *witness_epoch = 0u;
    if (!s_initialized) return false;
    epoch = release_cycle_button_session_epoch();
    edges = atomic_load_explicit(&s_release_edges, memory_order_acquire);
    level = gpio_get_level((gpio_num_t)RECORDER_WAKE_BUTTON_PIN);
    now = esp_timer_get_time();
    ready = release_cycle_button_detector_step(&s_detector,
        level == 0, custody && level >= 0 && now >= 0,
        now >= 0 ? (uint64_t)now : 0u, edges, epoch);
    // A disconnect during sampling cannot count as uninterrupted custody.
    if (release_cycle_button_session_epoch() != epoch) {
        memset(&s_detector, 0, sizeof(s_detector));
        return false;
    }
    if (ready && witness_epoch != NULL) *witness_epoch = epoch;
    return ready;
}

#endif // ESP_PLATFORM
