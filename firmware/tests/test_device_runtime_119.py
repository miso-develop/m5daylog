"""#119 F3-R: compile/execute the production capture STOP, writer and USB
coordinator control-flow blocks against deterministic host I/O and task mocks.

Unlike tests which construct recorder_terminal_gate_t directly, this test
compiles the real C statements extracted from main.c and task49_runtime.c.
No ESP32, NVS, media, or physical USB operation is performed.
"""
from pathlib import Path
import re
import shutil
import subprocess
import wave

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/main/main.c"
TASK49 = ROOT / "firmware/main/task49_runtime.c"
REC = ROOT / "firmware/components/recorder"


def c_block(source, anchor, start=0):
    pos = source.index(anchor, start)
    opening = source.index("{", pos)
    depth = 0
    for i in range(opening, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[pos:i + 1]
    raise AssertionError("unclosed C block: " + anchor)


def production_fragments():
    main = MAIN.read_text(encoding="utf-8")
    runtime = TASK49.read_text(encoding="utf-8")
    capture = main[main.index("static void recorder_capture_task(void *arg) {"):]
    read_start = capture.index("        size_t got = 0;")
    stop_start = capture.index("        if (recorder_stop_requested()) {", read_start)
    stop_stmt = c_block(capture, "        if (recorder_stop_requested()) {", read_start)
    capture_fragment = capture[read_start:stop_start] + stop_stmt
    teardown_start = capture.index("    // Quiescent teardown:")
    teardown_end = capture.index("\n}\n\nstatic void recorder_log_diagnostics", teardown_start)
    teardown = capture[teardown_start:teardown_end]
    assert "pdm_capture_stop_and_drain_final(capture, &snap)" in teardown
    assert "pdm_capture_deinit(capture)" in teardown
    assert "vTaskDelete(NULL)" in teardown
    mic_init = c_block(capture, "    if (pdm_capture_init(&cfg, &capture) != ESP_OK) {")

    writer = main[main.index("static void recorder_writer_task(void *arg) {"):]
    live_start = writer.index("            if (!sd_pcm_sink_write_chunk(&s_sink, full, full_len")
    live_end = writer.index("        }\n        if (recorder_stop_requested()) {", live_start)
    live_fragment = writer[live_start:live_end]

    terminal_start = writer.index("    // The producer must complete I2S teardown before the terminal drain.")
    terminal_end = writer.index("\n}\n\nvoid app_main(void)", terminal_start)
    terminal_fragment = writer[terminal_start:terminal_end]

    delete = c_block(runtime, "static void recorder_task49_delete(TaskHandle_t task) {")
    usb = c_block(runtime, "static void recorder_handle_usb_attach(void) {")
    # The host harness refuses to silently revert to copy/pasted simulated
    # behavior if the real production entrypoints are refactored.
    assert "pdm_capture_read(capture" in capture_fragment
    assert "pcm_pipeline_produce(" in stop_stmt
    assert "writer_io_ok = false" in live_fragment
    assert "sd_pcm_sink_flush(&s_sink)" in live_fragment
    assert "recorder_terminal_can_finalize(&gate, &s_pipeline)" in terminal_fragment
    assert "wav_rotation_finalize_once(&s_seg_state, &s_sink)" in terminal_fragment
    assert "recorder_manifest_record_wav(" in terminal_fragment
    assert "recorder_request_usb_stop()" in usb
    assert "REC_TASK_DONE_MASK" in usb
    assert "REC_BIT_CAPTURE_DONE" in delete
    return capture_fragment, teardown, mic_init, live_fragment, terminal_fragment, delete, usb


HARNESS = r"""
#include <inttypes.h>
#include <setjmp.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "recorder_terminal_gate.h"
#include "wav_rotation.h"

#define ESP_OK 0
#define ESP_FAIL (-1)
#define ESP_ERR_TIMEOUT 123
#define pdFALSE 0
#define pdTRUE 1
#define portMAX_DELAY 0xffffffffu
#define REC_BIT_SLOT_FULL (1u << 0)
#define REC_BIT_STOP (1u << 1)
#define REC_BIT_WRITER_READY (1u << 2)
#define REC_BIT_RECORDING_READY (1u << 3)
#define REC_BIT_WRITER_FINALIZED (1u << 4)
#define REC_BIT_CAPTURE_DONE (1u << 5)
#define REC_BIT_BATTERY_DONE (1u << 6)
#define REC_TASK_DONE_MASK (REC_BIT_WRITER_FINALIZED | REC_BIT_CAPTURE_DONE | REC_BIT_BATTERY_DONE)
#define RECORDER_FLUSH_EVERY_CHUNKS 4u
#define RECORDER_REASON_INTERNAL 1
#define RECORDER_REASON_SD_WRITE 2
#define RECORDER_REASON_SD_FLUSH 3
#define RECORDER_REASON_DMA_OVERRUN 4
#define RECORDER_REASON_BUFFER_OVERFLOW 5
#define RECORDER_REASON_I2S_READ 6
#define RECORDER_REASON_FINALIZE 7
#define RECORDER_REASON_MANIFEST 8
#define RECORDER_REASON_USB 9
#define RECORDER_REASON_MIC_INIT 10
#define DEVICE_MANIFEST_STATE_FINALIZED "FINALIZED"

typedef uint32_t EventBits_t;
typedef void *TaskHandle_t;
typedef void *pdm_capture_t;
typedef int pdm_capture_config_t;
typedef int esp_err_t;
typedef enum { RECORDER_STATE_RECORDING = 1, RECORDER_STATE_USB_PREPARE = 2,
               RECORDER_STATE_ERROR = 3, RECORDER_STATE_LOW_BATTERY_STOP = 4 } recorder_state_t;
typedef struct { uint32_t events, drop_bytes; } pdm_overflow_snapshot_t;

static uint8_t slot0[RECORDER_BUFFER_BYTES], slot1[RECORDER_BUFFER_BYTES];
static uint8_t s_dma_scratch[4096], source_pcm[RECORDER_BUFFER_BYTES * 2 + 4096];
static pcm_pipeline_t s_pipeline;
static sd_pcm_sink_t s_sink;
static wav_rotation_state_t s_seg_state;
static char s_seg_wav[RECORDER_MAX_PATH_LEN], s_seg_rec_id[] = "test-id";
static char s_seg_started[] = "2026-10-15T00:00:00+09:00";
static void *s_rec_events = (void *)1, *s_rec_lock = (void *)1;
static EventBits_t events = 0;
static recorder_state_t state = RECORDER_STATE_RECORDING;
static bool mounted = true, released = false, release_requested = false;
static bool stopped = false, writer_io_ok = true, terminal_drain_ok = true;
static bool running = true, scheduled = false;
static bool fail_terminal_write = false, fail_live_write = false, fail_live_flush = false;
static bool deny_ownership = false, mic_init_failed = false;
static uint32_t since_flush = 0, seg_bytes = 0;
static unsigned manifest_writes = 0, usb_published = 0, errors = 0;
static unsigned capture_delete = 0, writer_delete = 0;
static int s_last_battery_mv = 0;
static const char *actor = "";
static size_t next_read_len = 0, source_offset = 0;
static int next_read_err = ESP_OK;
static pdm_overflow_snapshot_t next_overflow = {0, 0};
static jmp_buf deinit_boundary;
static bool stop_io_error = false, final_dma_overflow = false;
static bool pause_during_deinit = false, deinit_pending = false;
static int deinit_failures = 0;
static unsigned stop_calls = 0, deinit_calls = 0;
static unsigned pre_done_delays = 0, coordinator_calls = 0;

static void log_unused(const char *tag, const char *fmt, ...) {
    (void)tag; (void)fmt;
}
#define TAG "host-119"
#define ESP_LOGI log_unused
#define ESP_LOGE log_unused
#define ESP_LOGW log_unused

static int xSemaphoreTake(void *lock, unsigned timeout) {
    (void)lock; (void)timeout; return pdTRUE;
}
static void xSemaphoreGive(void *lock) { (void)lock; }
static void xEventGroupSetBits(void *group, EventBits_t bits) {
    (void)group; events |= bits;
}
static EventBits_t xEventGroupGetBits(void *group) {
    (void)group; return events;
}
static bool recorder_stop_requested(void) { return stopped; }
static void recorder_request_stop(void) { stopped = true; events |= REC_BIT_STOP; }
static void recorder_request_usb_stop(void) { recorder_request_stop(); }
static recorder_state_t recorder_current_state(void) { return state; }
static void recorder_enter_error(int reason) {
    (void)reason; state = RECORDER_STATE_ERROR;
    ++errors; recorder_request_stop();
}
static bool recorder_transition_state(recorder_state_t next, int reason, int battery_mv) {
    (void)reason; (void)battery_mv;
    if (state != RECORDER_STATE_RECORDING || next != RECORDER_STATE_USB_PREPARE)
        return false;
    state = next; return true;
}
static bool sd_mount_is_mounted(void) { return mounted; }
static bool sd_mount_device_fs_released(void) { return released; }
static int sd_mount_release_for_usb(void) {
    if (!mounted || release_requested) return ESP_FAIL;
    release_requested = true; released = false; return ESP_OK;
}
static void sd_mount_unmount(void) {
    if (release_requested) released = true;
    else { mounted = false; released = true; }
}
static void recorder_set_events_ready(bool ready) { (void)ready; }
static void recorder_log_writer_stack_hw(const char *reason) { (void)reason; }
static void recorder_log_diagnostics(void) {}
static bool recorder_manifest_record_wav(const char *path, const char *id,
                                         const char *started, const char *kind) {
    (void)path; (void)id; (void)started;
    if (strcmp(kind, DEVICE_MANIFEST_STATE_FINALIZED)) return false;
    manifest_writes++; return true;
}
static void usb_cdc_protocol_reset_session(void) {}
static int usb_msc_ownership_begin_prepare(void) { return ESP_OK; }
static int usb_msc_ownership_note_prepare_complete(bool a, bool b, bool c) {
    if (!a || !b || !c || !release_requested || !released ||
        state != RECORDER_STATE_USB_PREPARE || !writer_delete || !capture_delete)
        return ESP_FAIL;
    usb_published++; return ESP_OK;
}
static const char *pcTaskGetName(void *task) {
    (void)task; return actor;
}
static void vTaskDelete(TaskHandle_t task) {
    (void)task;
    if (!strcmp(actor, "rec_capture")) capture_delete++;
    if (!strcmp(actor, "rec_writer")) writer_delete++;
}
static int pdm_capture_init(const pdm_capture_config_t *cfg, pdm_capture_t *handle) {
    (void)cfg; (void)handle; return mic_init_failed ? ESP_FAIL : ESP_OK;
}
static int pdm_capture_read(pdm_capture_t handle, void *dst, size_t size,
                            size_t *got) {
    (void)handle;
    if (next_read_len > size) return ESP_FAIL;
    memcpy(dst, source_pcm + source_offset, next_read_len);
    *got = next_read_len;
    return next_read_err;
}
static void pdm_capture_drain_overflow(pdm_capture_t handle, pdm_overflow_snapshot_t *out) {
    (void)handle; *out = next_overflow;
}
static esp_err_t pdm_capture_stop_and_drain_final(pdm_capture_t handle,
                                                  pdm_overflow_snapshot_t *out) {
    (void)handle;
    stop_calls++;
    memset(out, 0, sizeof(*out));
    if (final_dma_overflow) { out->events = 1; out->drop_bytes = 4096; }
    return stop_io_error ? ESP_FAIL : ESP_OK;
}
static esp_err_t pdm_capture_deinit(pdm_capture_t handle) {
    (void)handle; deinit_calls++;
    if (deinit_failures > 0) { deinit_failures--; return ESP_FAIL; }
    return ESP_OK;
}
static void vTaskDelay(unsigned ticks) {
    (void)ticks;
    if ((events & REC_BIT_CAPTURE_DONE) == 0) pre_done_delays++;
    // Deterministically pause inside the *production* teardown retry loop.
    // Unlike forcing the done bit, this models an uncompleted capture task.
    if (pause_during_deinit) {
        deinit_pending = true;
        longjmp(deinit_boundary, 1);
    }
}
static bool injected_write(sd_pcm_sink_t *sink, const uint8_t *data,
                           size_t len, uint32_t *latency) {
    if (fail_terminal_write || fail_live_write) {
        // A prefix may already be persisted before a short/failed fwrite.
        // It must never be replayed as a complete 32KB slot.
        size_t prefix = len > 1024 ? 1024 : len;
        (void)sd_pcm_sink_write_chunk(sink, data, prefix, latency);
        return false;
    }
    return sd_pcm_sink_write_chunk(sink, data, len, latency);
}
static bool injected_flush(sd_pcm_sink_t *sink) {
    if (fail_live_flush) return false;
    return sd_pcm_sink_flush(sink);
}

static void compiled_capture_init(void);
static void compiled_capture_step(void);
static void compiled_writer_terminal(void);
static void recorder_task49_delete(TaskHandle_t task);
static EventBits_t xEventGroupWaitBits(void *group, EventBits_t mask,
                                       int clear, int all, unsigned timeout) {
    (void)group; (void)clear; (void)all; (void)timeout;
    if (mask == REC_TASK_DONE_MASK && !scheduled) {
        scheduled = true;
        actor = "rec_capture";
        if (setjmp(deinit_boundary) == 0) {
            if (mic_init_failed) compiled_capture_init();
            else compiled_capture_step();
        }
        // A pending I2S deinit does not execute production vTaskDelete:
        // CAPTURE_DONE, writer finalization and USB publication stay absent.
        if (deinit_pending) {
            actor = "";
            return events & mask;
        }
        if (deny_ownership) released = true;
        actor = "rec_writer";
        compiled_writer_terminal();
        events |= REC_BIT_BATTERY_DONE;
        actor = "";
    }
    return events & mask;
}

// Same production deletion wrapper that runtime includes around main.c.
__DELETE__

#define vTaskDelete recorder_task49_delete
#define sd_pcm_sink_write_chunk injected_write
#define sd_pcm_sink_flush injected_flush

static void compiled_capture_init(void) {
    pdm_capture_config_t cfg = 0;
    pdm_capture_t capture = NULL;
__MIC_INIT__
}
static void compiled_capture_step(void) {
    pdm_capture_t capture = (void *)1;
    do {
__CAPTURE__
    } while (0);
__TEARDOWN__
}
static void compiled_live_writer_io(void) {
    const uint8_t *full = NULL;
    size_t full_len = 0;
    uint32_t latency_us = 0;
    full = pcm_pipeline_peek_full(&s_pipeline, &full_len);
    if (!full) return;
    do {
__LIVE__
    } while (0);
}
static void compiled_writer_terminal(void) {
__TERMINAL__
}
static void compiled_usb_attach(void) {
    // Exercise even the early ERROR rejection branches, not a skipped call.
    coordinator_calls++;
__USB_BODY__
}
#undef sd_pcm_sink_flush
#undef sd_pcm_sink_write_chunk
#undef vTaskDelete

int main(int argc, char **argv) {
    char wav_path[RECORDER_MAX_PATH_LEN];
    const char *scenario;
    if (argc != 3) return 10;
    scenario = argv[2];
    size_t len = strlen(argv[1]);
    if (len < 5 || len >= RECORDER_MAX_PATH_LEN ||
        strcmp(argv[1] + len - 5, ".part")) return 11;
    memcpy(wav_path, argv[1], len - 5);
    wav_path[len - 5] = '\0';
    for (size_t i = 0; i < sizeof(source_pcm); ++i)
        source_pcm[i] = (uint8_t)(i % 251u);
    pcm_pipeline_init(&s_pipeline, slot0, slot1);
    memset(&s_sink, 0, sizeof(s_sink));
    if (!sd_pcm_sink_open(&s_sink, argv[1])) return 12;
    wav_rotation_state_init(&s_seg_state, argv[1], wav_path);
    strcpy(s_seg_wav, wav_path);
    events |= REC_BIT_RECORDING_READY;
    stopped = false;

    if (!strcmp(scenario, "valid_zero")) {
        next_read_len = 0;
    } else if (!strcmp(scenario, "valid_short")) {
        next_read_len = 2048;
    } else if (!strcmp(scenario, "valid_full")) {
        source_offset = 28672;
        if (pcm_pipeline_produce(&s_pipeline, source_pcm, source_offset)) return 13;
        next_read_len = 4096;
    } else if (!strcmp(scenario, "valid_full_tail")) {
        source_offset = 32768;
        if (pcm_pipeline_produce(&s_pipeline, source_pcm, source_offset)) return 14;
        next_read_len = 2048;
    } else if (!strcmp(scenario, "timeout_prefix")) {
        next_read_len = 2048; next_read_err = ESP_ERR_TIMEOUT;
    } else if (!strcmp(scenario, "dma_overflow")) {
        next_read_len = 4096;
        next_overflow.events = 1; next_overflow.drop_bytes = 4096;
    } else if (!strcmp(scenario, "buffer_overflow")) {
        source_offset = 65536;
        if (pcm_pipeline_produce(&s_pipeline, source_pcm, source_offset)) return 15;
        next_read_len = 4096;
    } else if (!strcmp(scenario, "i2s_failure")) {
        next_read_err = ESP_FAIL;
    } else if (!strcmp(scenario, "i2s_stop_failure")) {
        next_read_len = 2048; stop_io_error = true;
    } else if (!strcmp(scenario, "i2s_final_dma")) {
        next_read_len = 2048; final_dma_overflow = true;
    } else if (!strcmp(scenario, "i2s_deinit_retry")) {
        next_read_len = 2048; deinit_failures = 1;
    } else if (!strcmp(scenario, "i2s_deinit_pending")) {
        next_read_len = 2048; deinit_failures = 1;
        pause_during_deinit = true;
    } else if (!strcmp(scenario, "ownership_lost")) {
        next_read_len = 2048; deny_ownership = true;
    } else if (!strcmp(scenario, "terminal_write_failure")) {
        next_read_len = 2048; fail_terminal_write = true;
    } else if (!strcmp(scenario, "mic_init_failure")) {
        mic_init_failed = true;
        events &= ~REC_BIT_RECORDING_READY;
        actor = "rec_capture"; compiled_capture_init();
        actor = "rec_writer"; compiled_writer_terminal();
    } else if (!strcmp(scenario, "live_write_failure") ||
               !strcmp(scenario, "live_flush_failure")) {
        if (pcm_pipeline_produce(&s_pipeline, source_pcm, 32768)) return 16;
        if (!strcmp(scenario, "live_write_failure")) fail_live_write = true;
        else { since_flush = RECORDER_FLUSH_EVERY_CHUNKS - 1; fail_live_flush = true; }
        actor = "rec_writer";
        compiled_live_writer_io();
        actor = "rec_capture"; compiled_capture_step();
        actor = "rec_writer"; compiled_writer_terminal();
    } else {
        return 17;
    }

    actor = "";
    compiled_usb_attach();

    printf("manifest=%u usb=%u captured=%" PRIu32 " dropped=%" PRIu32
           " dma=%" PRIu32 " sd=%" PRIu32 " errors=%u"
           " done_capture=%u done_writer=%u pcm=%" PRIu32
           " stop_calls=%u deinit_calls=%u pre_done_delay=%u coordinator=%u\n",
           manifest_writes, usb_published, s_pipeline.counters.samples_captured,
           s_pipeline.counters.buffer_drop_bytes, s_pipeline.counters.dma_drop_bytes,
           s_pipeline.counters.sd_write_errors, errors,
           capture_delete, writer_delete, s_sink.wav.pcm_bytes,
           stop_calls, deinit_calls, pre_done_delays, coordinator_calls);
    return 0;
}
"""


@pytest.fixture(scope="module")
def coordinator_native(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc")
    assert cc, "No host C compiler: production runtime test must not skip"
    capture, teardown, mic_init, live, terminal, delete, usb = production_fragments()
    # All pieces below are copied directly from the actual revision under
    # test. If the production control flow changes, its exact C changes here.
    usb_body = usb[usb.index("{") + 1:usb.rindex("}")]
    harness = (HARNESS.replace("__CAPTURE__", capture)
                     .replace("__TEARDOWN__", teardown)
                     .replace("__MIC_INIT__", mic_init)
                     .replace("__LIVE__", live)
                     .replace("__TERMINAL__", terminal)
                     .replace("__DELETE__", delete)
                     .replace("__USB_BODY__", usb_body))
    directory = tmp_path_factory.mktemp("device-runtime-119")
    source = directory / "runtime.c"
    binary = directory / "runtime"
    source.write_text(harness, encoding="utf-8")
    cmd = [
        cc, "-std=c11", "-O0", "-Wall", "-Wextra", "-Werror",
        "-Wno-unused-variable", "-Wno-unused-function",
        "-I", str(REC / "include"),
        str(REC / "pcm_pipeline.c"), str(REC / "sd_pcm_sink.c"),
        str(REC / "wav_part.c"), str(REC / "wav_rotation.c"),
        str(source), "-o", str(binary),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


@pytest.mark.parametrize("scenario,expected_bytes", [
    ("valid_zero", 0),
    ("valid_short", 2048),
    ("valid_full", 32768),
    ("valid_full_tail", 34816),
    ("timeout_prefix", 2048),
])
def test_production_usb_stop_capture_writer_finalization(
    coordinator_native, tmp_path, scenario, expected_bytes
):
    part = tmp_path / "session.wav.part"
    result = subprocess.run(
        [str(coordinator_native), str(part), scenario],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "manifest=1 usb=1" in result.stdout, result.stdout
    assert "done_capture=1 done_writer=1" in result.stdout, result.stdout
    assert "stop_calls=1 deinit_calls=1" in result.stdout, result.stdout
    assert "coordinator=1" in result.stdout, result.stdout
    assert f"pcm={expected_bytes}" in result.stdout
    assert not part.exists()
    with wave.open(str(tmp_path / "session.wav"), "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2
        assert wav.getnchannels() == 1
        assert wav.getnframes() == expected_bytes // 2
        assert wav.readframes(wav.getnframes()) == bytes(
            x % 251 for x in range(expected_bytes)
        )


@pytest.mark.parametrize("scenario", [
    "mic_init_failure", "dma_overflow", "buffer_overflow",
    "i2s_failure", "i2s_stop_failure", "i2s_final_dma",
    "i2s_deinit_retry", "i2s_deinit_pending",
    "ownership_lost", "terminal_write_failure",
    "live_write_failure", "live_flush_failure",
])
def test_production_usb_stop_errors_never_publish(
    coordinator_native, tmp_path, scenario
):
    part = tmp_path / "session.wav.part"
    result = subprocess.run(
        [str(coordinator_native), str(part), scenario],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "manifest=0 usb=0" in result.stdout, result.stdout
    assert "coordinator=1" in result.stdout, result.stdout
    if scenario == "i2s_deinit_pending":
        assert "done_capture=0 done_writer=0" in result.stdout, result.stdout
        assert "stop_calls=1 deinit_calls=1 pre_done_delay=1" in result.stdout
    else:
        assert "done_writer=1" in result.stdout, result.stdout
    if scenario == "i2s_deinit_retry":
        assert "stop_calls=1 deinit_calls=2 pre_done_delay=1" in result.stdout
        assert "errors=" in result.stdout
    if scenario == "i2s_stop_failure":
        assert "stop_calls=1 deinit_calls=1" in result.stdout
    if scenario == "i2s_final_dma":
        assert "dma=4096" in result.stdout
    assert part.exists()
    assert not (tmp_path / "session.wav").exists()
    # Faults that have staged PCM remain physically salvageable as .part,
    # never mistaken for a FINALIZED, synced manifest WAV.
    if scenario == "buffer_overflow":
        assert "dropped=4096" in result.stdout, result.stdout
        assert part.stat().st_size == 44 + 65536
    if scenario == "dma_overflow":
        assert "dma=4096" in result.stdout, result.stdout
    if scenario == "terminal_write_failure":
        assert "sd=1" in result.stdout, result.stdout
