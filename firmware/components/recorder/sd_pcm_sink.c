// microSD `.part` sink — Task #44. Stdio + optional esp_timer latency.

#include "sd_pcm_sink.h"

#ifdef ESP_PLATFORM
#include "esp_timer.h"
#endif

static uint32_t now_us(void) {
#ifdef ESP_PLATFORM
    return (uint32_t)esp_timer_get_time();
#else
    return 0;
#endif
}

bool sd_pcm_sink_open(sd_pcm_sink_t *sink, const char *path) {
    if (sink == NULL) {
        return false;
    }
    sink->chunks_written = 0;
    sink->write_errors = 0;
    sink->max_latency_us = 0;
    sink->open = wav_part_open(&sink->wav, path, RECORDER_SAMPLE_RATE_HZ,
                               RECORDER_CHANNELS,
                               RECORDER_BITS_PER_SAMPLE);
    return sink->open;
}

bool sd_pcm_sink_write_chunk(sd_pcm_sink_t *sink, const uint8_t *data,
                             size_t len, uint32_t *out_latency_us) {
    uint32_t start_us;
    uint32_t latency_us;
    bool ok;

    if (sink == NULL || !sink->open) {
        return false;
    }
    if (data == NULL || len != RECORDER_BUFFER_BYTES) {
        sink->write_errors++;
        return false;
    }
    start_us = now_us();
    ok = wav_part_write(&sink->wav, data, len);
    latency_us = now_us() - start_us;
    if (out_latency_us != NULL) {
        *out_latency_us = latency_us;
    }
    if (!ok) {
        sink->write_errors++;
        return false;
    }
    sink->chunks_written++;
    if (latency_us > sink->max_latency_us) {
        sink->max_latency_us = latency_us;
    }
    return true;
}

bool sd_pcm_sink_flush(sd_pcm_sink_t *sink) {
    if (sink == NULL || !sink->open) {
        return false;
    }
    return wav_part_flush(&sink->wav);
}

bool sd_pcm_sink_close(sd_pcm_sink_t *sink) {
    bool ok;

    if (sink == NULL || !sink->open) {
        return true;
    }
    ok = wav_part_close(&sink->wav);
    sink->open = false;
    return ok;
}
