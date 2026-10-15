"""#119: native production-C terminal PCM drain regression tests.

Host-only synthetic PCM. The test executable links the unmodified runtime
source files used by the ESP-IDF recorder, with portable stdio WAV storage.
No microphone, physical USB session, or NVS/flash access is involved.
"""
from pathlib import Path
import shutil
import subprocess
import wave

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "firmware/components/recorder"
INCLUDE = COMP / "include"
MAIN = ROOT / "firmware/main/main.c"
TASK49 = ROOT / "firmware/main/task49_runtime.c"

HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include "pcm_pipeline.h"
#include "sd_pcm_sink.h"

static uint8_t slot0[RECORDER_BUFFER_BYTES];
static uint8_t slot1[RECORDER_BUFFER_BYTES];

static int drain_ready(pcm_pipeline_t *p, sd_pcm_sink_t *sink,
                       size_t *persisted) {
    while (pcm_pipeline_has_full(p)) {
        size_t len = 0;
        uint32_t latency = 0;
        const uint8_t *ptr = pcm_pipeline_peek_full(p, &len);
        if (ptr == NULL || len == 0 || len > RECORDER_BUFFER_BYTES ||
            (len % RECORDER_BYTES_PER_SAMPLE) != 0) return 20;
        if (!sd_pcm_sink_write_chunk(sink, ptr, len, &latency)) return 21;
        pcm_pipeline_note_sd_write(p, latency);
        pcm_pipeline_release_full(p);
        *persisted += len;
    }
    return 0;
}

int main(int argc, char **argv) {
    size_t size, offset = 0, persisted = 0;
    pcm_pipeline_t p;
    sd_pcm_sink_t sink;
    int rc;
    if (argc != 3) return 1;
    size = (size_t)strtoul(argv[2], NULL, 10);
    if (size > 200000 || size % RECORDER_BYTES_PER_SAMPLE != 0) return 2;
    pcm_pipeline_init(&p, slot0, slot1);
    memset(&sink, 0, sizeof(sink));
    if (!sd_pcm_sink_open(&sink, argv[1])) return 3;

    // Emulate I2S chunks with a deterministic waveform and the existing
    // live writer's full-slot drain. No unbounded RAM or file fixtures.
    while (offset < size) {
        uint8_t block[4096];
        size_t n = size - offset;
        size_t i;
        if (n > sizeof(block)) n = sizeof(block);
        for (i = 0; i < n; ++i)
            block[i] = (uint8_t)((offset + i) % 251u);
        if (pcm_pipeline_produce(&p, block, n) != 0) return 4;
        offset += n;
        if ((rc = drain_ready(&p, &sink, &persisted)) != 0) return rc;
    }

    // Production terminal operation: capture is already quiescent before
    // seal; previously these valid bytes were invisible to peek_full().
    {
        size_t tail = pcm_pipeline_seal_partial(&p);
        if (tail != size % RECORDER_BUFFER_BYTES) return 5;
        if (pcm_pipeline_seal_partial(&p) != 0) return 6;
        if ((rc = drain_ready(&p, &sink, &persisted)) != 0) return rc;
    }
    if (pcm_pipeline_has_full(&p)) return 7;
    if (persisted != size ||
        p.counters.samples_captured != size / RECORDER_BYTES_PER_SAMPLE ||
        p.counters.buffer_drop_bytes != 0 ||
        p.counters.dma_drop_bytes != 0 ||
        p.counters.sd_write_errors != 0) return 8;
    if (!sd_pcm_sink_close(&sink)) return 9;
    printf("pcm_bytes=%zu\n", persisted);
    return 0;
}
"""


@pytest.fixture(scope="module")
def pcm_stop_native(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc")
    assert cc is not None, "native C compiler is required (no silent skip)"
    work = tmp_path_factory.mktemp("pcm-stop-native-119")
    harness = work / "harness.c"
    executable = work / "pcm-stop"
    harness.write_text(HARNESS, encoding="utf-8")
    cmd = [
        cc, "-std=c11", "-O0", "-Wall", "-Wextra", "-Werror",
        "-I", str(INCLUDE),
        str(COMP / "pcm_pipeline.c"),
        str(COMP / "sd_pcm_sink.c"),
        str(COMP / "wav_part.c"),
        str(harness), "-o", str(executable),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return executable


@pytest.mark.parametrize("nbytes", [
    0, 2, 2048, 4096, 16384, 32766, 32768, 32770, 40000,
    65534, 65536, 65538, 115200,
])
def test_native_stop_drains_exact_pcm_without_losing_tail(
    pcm_stop_native, tmp_path, nbytes
):
    output = tmp_path / "synthetic.wav.part"
    result = subprocess.run(
        [str(pcm_stop_native), str(output), str(nbytes)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == f"pcm_bytes={nbytes}"
    # WAV format, total sample count and every sample byte are checked,
    # not just whether the 44-byte header is parseable.
    assert output.stat().st_size == 44 + nbytes
    with wave.open(str(output), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.getnframes() == nbytes // 2
        actual = wav.readframes(wav.getnframes())
    assert actual == bytes(i % 251 for i in range(nbytes))


def test_real_firmware_writer_waits_for_capture_before_finalization():
    main = MAIN.read_text(encoding="utf-8")
    runtime = TASK49.read_text(encoding="utf-8")
    writer = main[main.index("static void recorder_writer_task(void *arg) {"):]
    first_join = writer.index("REC_BIT_CAPTURE_DONE")
    seal = writer.index("pcm_pipeline_seal_partial")
    terminal_final = writer.rindex("wav_rotation_finalize_once")
    assert first_join < seal < terminal_final
    assert "terminal_drain_ok" in writer
    assert "terminal drain failed" in writer
    assert "REC_BIT_CAPTURE_DONE" in runtime
    assert "recorder_task49_delete" in runtime
    assert "REC_BIT_WRITER_FINALIZED" in runtime


def test_no_unapproved_usb_release_or_wav_source_rewrite():
    main = MAIN.read_text(encoding="utf-8")
    assert "RELEASE_STORAGE" not in main
    assert "pcm_pipeline_seal_partial" in main
    assert "recorder_request_usb_stop()" in (
        ROOT / "firmware/main/task49_runtime.c"
    ).read_text(encoding="utf-8")
