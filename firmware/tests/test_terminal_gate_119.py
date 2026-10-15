"""#119: compiled-production-C terminal publication/failure injection matrix.

Uses the actual recorder gate, PCM pipeline, SD sink, WAV part and finalize
modules. Capture scheduling is stimulated by ordered host events, not an
ESP32 I2S/FreeRTOS simulation or evidence of physical USB/battery behavior.
"""
from pathlib import Path
import shutil
import subprocess
import wave

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "firmware/components/recorder"
INC = COMP / "include"
MAIN = ROOT / "firmware/main/main.c"

HARNESS = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "recorder_terminal_gate.h"
#include "sd_pcm_sink.h"
#include "wav_rotation.h"

static uint8_t slot0[RECORDER_BUFFER_BYTES];
static uint8_t slot1[RECORDER_BUFFER_BYTES];
static uint8_t signal_bytes[RECORDER_BUFFER_BYTES * 2u + 4096u];

static int feed(pcm_pipeline_t *p, size_t count) {
    if (pcm_pipeline_produce(p, signal_bytes, count) != 0) {
        return -1;
    }
    return 0;
}
static int drain(pcm_pipeline_t *p, sd_pcm_sink_t *sink, size_t *total) {
    while (pcm_pipeline_has_full(p)) {
        uint32_t latency = 0;
        size_t n = 0;
        const uint8_t *pcm = pcm_pipeline_peek_full(p, &n);
        if (pcm == NULL || n == 0 || n > RECORDER_BUFFER_BYTES ||
            n % RECORDER_BYTES_PER_SAMPLE != 0) return 31;
        if (!sd_pcm_sink_write_chunk(sink, pcm, n, &latency)) {
            pcm_pipeline_note_sd_error(p);
            return 32;
        }
        pcm_pipeline_note_sd_write(p, latency);
        pcm_pipeline_release_full(p);
        *total += n;
    }
    return 0;
}

int main(int argc, char **argv) {
    pcm_pipeline_t pipe;
    sd_pcm_sink_t sink;
    recorder_terminal_gate_t gate = { true, true, true, true };
    wav_rotation_state_t rotation;
    char wav_path[RECORDER_MAX_PATH_LEN];
    size_t part_len, expected = 0, written = 0;
    int rc;
    bool expect_success;

    if (argc != 3) return 1;
    part_len = strlen(argv[1]);
    if (part_len < 5u || part_len >= sizeof(wav_path) ||
        strcmp(argv[1] + part_len - 5u, ".part") != 0) return 2;
    memcpy(wav_path, argv[1], part_len - 5u);
    wav_path[part_len - 5u] = '\0';

    for (size_t i = 0; i < sizeof(signal_bytes); ++i) {
        signal_bytes[i] = (uint8_t)(i % 251u);
    }
    pcm_pipeline_init(&pipe, slot0, slot1);
    memset(&sink, 0, sizeof(sink));
    if (!sd_pcm_sink_open(&sink, argv[1])) return 3;

    // A STOP may occur before any read, after a sub-slot read, after one full
    // slot, or during an in-flight 4KB read. The production gate refuses
    // publication until the producer has quiesced.
    if (!strcmp(argv[2], "valid_zero") ||
        !strcmp(argv[2], "usb_before_first") ||
        !strcmp(argv[2], "timeout_only")) {
        if (!strcmp(argv[2], "timeout_only")) {
            pcm_pipeline_note_read_stall(&pipe);
        }
    } else if (!strcmp(argv[2], "valid_short")) {
        expected = 2048;
        if (feed(&pipe, expected)) return 4;
    } else if (!strcmp(argv[2], "valid_full")) {
        expected = RECORDER_BUFFER_BYTES;
        if (feed(&pipe, expected)) return 4;
    } else if (!strcmp(argv[2], "valid_full_tail")) {
        expected = RECORDER_BUFFER_BYTES + 2048u;
        if (feed(&pipe, expected)) return 4;
    } else if (!strcmp(argv[2], "usb_inflight")) {
        // STOP requests cannot discard a read already in progress.
        if (feed(&pipe, 2048u)) return 4;
        gate.capture_done = false;
        if (recorder_terminal_can_finalize(&gate, &pipe)) return 5;
        if (pcm_pipeline_produce(&pipe, signal_bytes + 2048u, 4096u) != 0)
            return 6;
        expected = 6144u;
        gate.capture_done = true;
    } else if (!strcmp(argv[2], "mic_init_error") ||
               !strcmp(argv[2], "usb_before_ready")) {
        gate.capture_started = false;
        gate.lifecycle_healthy = false;
    } else if (!strcmp(argv[2], "capture_not_done")) {
        gate.capture_done = false;
        expected = 2048u;
        if (feed(&pipe, expected)) return 4;
    } else if (!strcmp(argv[2], "i2s_teardown_error") ||
               !strcmp(argv[2], "i2s_read_error") ||
               !strcmp(argv[2], "bad_usb_ownership")) {
        gate.lifecycle_healthy = false;
        expected = 2048u;
        if (feed(&pipe, expected)) return 4;
    } else if (!strcmp(argv[2], "dma_overflow")) {
        pcm_pipeline_note_driver_overflow(&pipe, 1u, 4096u);
    } else if (!strcmp(argv[2], "software_overflow")) {
        if (feed(&pipe, RECORDER_BUFFER_BYTES * 2u)) return 4;
        // 2 full slots, no consumer: explicit 4096-byte software drop.
        if (pcm_pipeline_produce(&pipe, signal_bytes, 4096u) != 4096u)
            return 7;
    } else if (!strcmp(argv[2], "prior_sd_write_error")) {
        // A real prefix may have reached the .part before a later error.
        // Never replay the staged slot and turn the prefix into healthy WAV.
        if (feed(&pipe, 2048u)) return 4;
        if (!sd_pcm_sink_write_chunk(&sink, signal_bytes, 1024u, NULL))
            return 8;
        gate.writer_io_ok = false;
        pcm_pipeline_note_sd_error(&pipe);
    } else if (!strcmp(argv[2], "prior_flush_error")) {
        if (feed(&pipe, 2048u)) return 4;
        // Failed checkpoint is a sticky error even if an eventual close can
        // succeed. Simulated failure flag; not physical f_sync injection.
        gate.writer_io_ok = false;
    } else if (!strcmp(argv[2], "terminal_sd_error")) {
        if (feed(&pipe, 2048u)) return 4;
        // Simulate terminal writer rejecting an SD write; the main loop
        // records this in sd_write_errors and leaves the .part unindexed.
        pcm_pipeline_note_sd_error(&pipe);
    } else {
        return 9;
    }

    expect_success = gate.capture_started && gate.capture_done &&
                     gate.writer_io_ok && gate.lifecycle_healthy &&
                     pipe.counters.buffer_overflow == 0 &&
                     pipe.counters.buffer_drop_bytes == 0 &&
                     pipe.counters.dma_overrun_events == 0 &&
                     pipe.counters.dma_drop_bytes == 0 &&
                     pipe.counters.sd_write_errors == 0;
    if (recorder_terminal_can_finalize(&gate, &pipe) != expect_success)
        return 10;

    if (expect_success) {
        (void)pcm_pipeline_seal_partial(&pipe);
        rc = drain(&pipe, &sink, &written);
        if (rc != 0) return rc;
        if (written != expected ||
            pipe.counters.samples_captured * RECORDER_BYTES_PER_SAMPLE != expected ||
            pcm_pipeline_has_full(&pipe)) return 11;
        // The same production admission is checked after drain, immediately
        // before the production WAV close+fsync+rename and manifest admission.
        if (!recorder_terminal_can_finalize(&gate, &pipe)) return 12;
        wav_rotation_state_init(&rotation, argv[1], wav_path);
        if (!wav_rotation_finalize_once(&rotation, &sink)) return 13;
        if (!rotation.finalized_ok ||
            !wav_rotation_finalize_once(&rotation, &sink)) return 14;
        printf("published=1 pcm_bytes=%zu\n", written);
    } else {
        // Error disposition: close the recoverable .part but DO NOT invoke
        // wav_rotation_finalize_once or publish a healthy manifest entry.
        if (!sd_pcm_sink_close(&sink)) return 15;
        FILE *fp = fopen(wav_path, "rb");
        if (fp != NULL) {
            fclose(fp);
            return 16;
        }
        printf("published=0 pcm_bytes=%zu\n", written);
    }
    return 0;
}
"""

@pytest.fixture(scope="module")
def terminal_native(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc")
    assert cc, "C compiler is mandatory for production-C gate regression"
    folder = tmp_path_factory.mktemp("terminal-gate-119")
    harness = folder / "harness.c"
    executable = folder / "terminal-gate"
    harness.write_text(HARNESS, encoding="utf-8")
    cmd = [
        cc, "-std=c11", "-O0", "-Wall", "-Wextra", "-Werror",
        "-I", str(INC),
        str(COMP / "pcm_pipeline.c"),
        str(COMP / "recorder_terminal_gate.c"),
        str(COMP / "sd_pcm_sink.c"),
        str(COMP / "wav_part.c"),
        str(COMP / "wav_rotation.c"),
        str(harness), "-o", str(executable),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return executable

@pytest.mark.parametrize("scenario,bytes_expected", [
    ("valid_zero", 0),
    ("usb_before_first", 0),
    ("timeout_only", 0),
    ("valid_short", 2048),
    ("valid_full", 32768),
    ("valid_full_tail", 34816),
    ("usb_inflight", 6144),
])
def test_healthy_capture_terminal_path(terminal_native, tmp_path, scenario, bytes_expected):
    part = tmp_path / "capture.wav.part"
    wav_path = tmp_path / "capture.wav"
    result = subprocess.run(
        [str(terminal_native), str(part), scenario],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == f"published=1 pcm_bytes={bytes_expected}"
    assert not part.exists()
    assert wav_path.stat().st_size == 44 + bytes_expected
    with wave.open(str(wav_path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.getnframes() == bytes_expected // 2
        data = wav.readframes(wav.getnframes())
    if scenario == "usb_inflight":
        assert data == bytes(i % 251 for i in range(6144))
    else:
        assert data == bytes(i % 251 for i in range(bytes_expected))

@pytest.mark.parametrize("scenario", [
    "mic_init_error", "usb_before_ready", "capture_not_done",
    "i2s_teardown_error", "i2s_read_error", "bad_usb_ownership",
    "dma_overflow", "software_overflow", "prior_sd_write_error",
    "prior_flush_error", "terminal_sd_error",
])
def test_faults_never_publish_healthy_wav(
    terminal_native, tmp_path, scenario
):
    part = tmp_path / "capture.wav.part"
    wav_path = tmp_path / "capture.wav"
    result = subprocess.run(
        [str(terminal_native), str(part), scenario],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "published=0 pcm_bytes=0"
    assert not wav_path.exists(), scenario
    assert part.exists(), "original staged bytes must remain for recovery"

def test_production_integration_has_sticky_failure_and_two_gate_checks():
    source = MAIN.read_text(encoding="utf-8")
    writer = source[source.index("static void recorder_writer_task(void *arg) {"):]
    assert "bool writer_io_ok = true" in writer
    assert writer.count("writer_io_ok = false") >= 2
    assert writer.count("recorder_terminal_can_finalize(&gate, &s_pipeline)") == 2
    assert writer.index("REC_BIT_CAPTURE_DONE") < writer.index("pcm_pipeline_seal_partial")
    assert writer.index("pcm_pipeline_seal_partial") < writer.rindex("wav_rotation_finalize_once")
    assert writer.index("if (!terminal_drain_ok)") < writer.rindex("wav_rotation_finalize_once")
    assert "REC_BIT_RECORDING_READY" in writer
    assert "RECORDER_STATE_ERROR" in writer
