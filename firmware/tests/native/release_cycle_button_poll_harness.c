// #144: deterministic host-linked execution of the production ESP GPIO poll.
// Inject an actual registered ISR between the edge snapshot and GPIO sample.
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "driver/gpio.h"
#include "esp_intr_alloc.h"
#include "esp_timer.h"
#include "release_cycle_button.h"

#define CHECK(x) do { if (!(x)) { \
    fprintf(stderr, "FAIL %s:%d %s\\n", __FILE__, __LINE__, #x); \
    exit(2); \
} } while (0)

static void (*g_handler)(void *);
static int64_t g_time_us;
static int g_level = 1;
static bool g_edge_during_level;
static bool g_edge_during_clock;
static bool g_cutoff_during_level;

static void release_then_repress(void) {
    CHECK(g_handler != NULL);
    g_level = 1;
    g_handler(NULL); // ISR rising-edge count, not a simulated poll argument
    g_level = 0;
}

esp_err_t gpio_install_isr_service(int flags) {
    CHECK(flags == ESP_INTR_FLAG_IRAM);
    return ESP_OK;
}
esp_err_t gpio_config(const gpio_config_t *cfg) {
    CHECK(cfg != NULL && cfg->pin_bit_mask == (UINT64_C(1) << 42));
    CHECK(cfg->intr_type == GPIO_INTR_POSEDGE);
    return ESP_OK;
}
esp_err_t gpio_isr_handler_add(gpio_num_t pin,
                              void (*handler)(void *), void *arg) {
    CHECK(pin == 42 && handler != NULL && arg == NULL);
    g_handler = handler;
    return ESP_OK;
}
esp_err_t gpio_isr_handler_remove(gpio_num_t pin) {
    CHECK(pin == 42);
    g_handler = NULL;
    return ESP_OK;
}
esp_err_t gpio_intr_enable(gpio_num_t pin) {
    CHECK(pin == 42);
    return ESP_OK;
}
int gpio_get_level(gpio_num_t pin) {
    CHECK(pin == 42);
    if (g_edge_during_level) {
        g_edge_during_level = false;
        release_then_repress();
    }
    if (g_cutoff_during_level) {
        g_cutoff_during_level = false;
        release_cycle_button_note_session_cutoff();
    }
    return g_level;
}
int64_t esp_timer_get_time(void) {
    if (g_edge_during_clock) {
        g_edge_during_clock = false;
        release_then_repress();
    }
    return g_time_us;
}
int main(void) {
    uint32_t epoch = 0;
    uint32_t stages = 0;
    CHECK(release_cycle_button_init() == ESP_OK);
    g_level = 0;
    g_time_us = INT64_C(1000000);
    CHECK(!release_cycle_button_poll(true, &epoch));
    // Counter N is read, then GPIO level sampler injects a rising ISR and
    // immediate re-press. Old code returned true at 10-second threshold.
    g_time_us += INT64_C(10000000);
    g_edge_during_level = true;
    CHECK(!release_cycle_button_poll(true, &epoch));
    CHECK(epoch == 0u);
    CHECK(!release_cycle_button_witness_still_valid(0u));
    // The interrupted hold must restart its full 10-second interval.
    ++g_time_us;
    CHECK(!release_cycle_button_poll(true, &epoch));
    g_time_us += INT64_C(9999999);
    CHECK(!release_cycle_button_poll(true, &epoch));
    ++g_time_us;
    CHECK(release_cycle_button_poll(true, &epoch));
    CHECK(epoch == release_cycle_button_session_epoch());
    CHECK(release_cycle_button_witness_still_valid(epoch));
    if (release_cycle_button_witness_still_valid(epoch)) ++stages;
    CHECK(stages == 1u);

    // A release edge after polling but before the NVS gate must invalidate
    // the ticket, even when the button is already physically re-pressed.
    release_then_repress();
    CHECK(!release_cycle_button_witness_still_valid(epoch));
    if (release_cycle_button_witness_still_valid(epoch)) ++stages;
    CHECK(stages == 1u);

    // A new session begins from zero; a later ISR between GPIO and clock
    // snapshots must be detected by the *post-step* edge comparison.
    release_cycle_button_note_session_cutoff();
    g_time_us += 1;
    CHECK(!release_cycle_button_poll(true, &epoch));
    g_time_us += INT64_C(10000000);
    g_edge_during_clock = true;
    CHECK(!release_cycle_button_poll(true, &epoch));
    CHECK(epoch == 0u);
    g_time_us += 1;
    CHECK(!release_cycle_button_poll(true, &epoch));
    g_time_us += INT64_C(10000000);
    CHECK(release_cycle_button_poll(true, &epoch));

    // An ISR exactly during pre-stage witness revalidation is not valid.
    g_edge_during_level = true;
    CHECK(!release_cycle_button_witness_still_valid(epoch));
    // Unchanged continuous pressed state is the positive control after a
    // *new* complete hold; the prior fired detector cannot be reused.
    g_level = 1;
    CHECK(!release_cycle_button_poll(false, &epoch));
    g_level = 0;
    ++g_time_us;
    CHECK(!release_cycle_button_poll(true, &epoch));
    g_time_us += INT64_C(10000000);
    CHECK(release_cycle_button_poll(true, &epoch));
    CHECK(release_cycle_button_witness_still_valid(epoch));
    g_cutoff_during_level = true;
    CHECK(!release_cycle_button_witness_still_valid(epoch));
    puts("FU_PRODUCTION_GPIO_POLL=PASS");
    return 0;
}
