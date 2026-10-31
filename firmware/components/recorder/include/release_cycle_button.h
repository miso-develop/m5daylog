#pragma once
// R5-FU: continuous physical GPIO42 hold evidence, independent of CDC or RTC.
#include <stdbool.h>
#include <stdint.h>
#ifdef ESP_PLATFORM
#include "esp_err.h"
#else
typedef int esp_err_t;
#endif

#define RELEASE_CYCLE_BUTTON_HOLD_US UINT64_C(10000000)

typedef struct {
    uint64_t start_us;
    uint32_t release_edges;
    uint32_t session_epoch;
    bool tracking;
    bool fired;
} release_cycle_button_detector_t;

// Portable production state machine. Missing custody, any observed release
// edge, time reversal, session generation change or button-up resets the
// entire 10s interval. Only one positive result per continuous hold.
bool release_cycle_button_detector_step(release_cycle_button_detector_t *s,
    bool pressed, bool custody, uint64_t now_us,
    uint32_t release_edges, uint32_t session_epoch);

#ifdef ESP_PLATFORM
// GPIO42 rising-edge ISR captures bounce/release between task polls.
// A failure to attach a real interrupt disables first-use staging.
esp_err_t release_cycle_button_init(void);
// Callback-safe: no locks, NVS, RTC, or storage access.
void release_cycle_button_note_session_cutoff(void);
uint32_t release_cycle_button_session_epoch(void);
// Called by USB coordinator only; capture epoch alongside the proof.
bool release_cycle_button_poll(bool custody, uint32_t *witness_epoch);
#endif
