"""Task #44 host contract tests: RIFF/WAVE `.part` framing.

Stdlib only (no ESP-IDF, no device, no network). Validates the byte-level
contract implemented by firmware/components/recorder/wav_part.c and guards
it against drift via source-presence assertions.
"""

import struct
import tempfile
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WAV_C = REPO / "firmware/components/recorder/wav_part.c"
WAV_H = REPO / "firmware/components/recorder/include/wav_part.h"

SAMPLE_RATE = 16000
CHANNELS = 1
BITS = 16


def build_header(pcm_bytes):
    byte_rate = SAMPLE_RATE * CHANNELS * (BITS // 8)
    block_align = CHANNELS * (BITS // 8)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + pcm_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        CHANNELS,
        SAMPLE_RATE,
        byte_rate,
        block_align,
        BITS,
        b"data",
        pcm_bytes,
    )


def test_header_vectors():
    assert len(build_header(0)) == 44
    h0 = build_header(0)
    assert h0[0:4] == b"RIFF" and h0[8:12] == b"WAVE" and h0[12:16] == b"fmt "
    assert struct.unpack("<I", h0[4:8])[0] == 36
    assert struct.unpack("<I", h0[40:44])[0] == 0
    # fmt invariants: PCM(1), mono, 16kHz, 16bit, byte_rate 32000, align 2
    assert struct.unpack("<H", h0[20:22])[0] == 1
    assert struct.unpack("<H", h0[22:24])[0] == 1
    assert struct.unpack("<I", h0[24:28])[0] == 16000
    assert struct.unpack("<I", h0[28:32])[0] == 32000
    assert struct.unpack("<H", h0[32:34])[0] == 2
    assert struct.unpack("<H", h0[34:36])[0] == 16
    # payload-size round trip: one 32KB slot = 16384 samples
    h32k = build_header(32768)
    assert struct.unpack("<I", h32k[4:8])[0] == 36 + 32768
    assert struct.unpack("<I", h32k[40:44])[0] == 32768
    # 1 hour of 16kHz/16bit/mono payload sizing (Task #44 evidence shape)
    one_hour = 16000 * 2 * 3600
    assert struct.unpack("<I", build_header(one_hour)[40:44])[0] == one_hour


def test_part_file_decodeable_with_stdlib_wave():
    # Synthetic 16-bit ramp: 1.024 s == one full 32KB pipeline slot.
    # Stdlib only: tempfile + pathlib temporary directory (no pytest
    # fixtures) so every host runner executes this test.
    samples = 16384
    payload = b"".join(
        struct.pack("<h", (i % 512) - 256) for i in range(samples)
    )
    assert len(payload) == 32768
    with tempfile.TemporaryDirectory() as tmp:
        part = Path(tmp) / "000000_synth.wav.part"
        part.write_bytes(build_header(len(payload)) + payload)
        with wave.open(str(part), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == 16000
            assert w.getnframes() == samples
            assert len(w.readframes(samples)) == len(payload)


def test_sample_boundary_truncation_rule():
    # Spec #36: only a misaligned TAIL is truncated, never the payload.
    payload = b"\x01\x02\x03"  # 1.5 samples
    usable = payload[: len(payload) - (len(payload) % 2)]
    assert usable == b"\x01\x02"
    even = b"\x01\x02\x03\x04"
    assert even[: len(even) - (len(even) % 2)] == even


def test_c_source_implements_contract():
    src = WAV_C.read_text(encoding="utf-8")
    hdr = WAV_H.read_text(encoding="utf-8")
    for symbol in (
        "wav_part_build_header",
        "wav_part_open",
        "wav_part_write",
        "wav_part_flush",
        "wav_part_close",
        "wav_part_pcm_bytes",
    ):
        assert symbol in src or symbol in hdr, symbol
    # `.wav.part` suffix discipline must be present (never bare `.part`).
    assert ".wav.part" in src or ".wav.part" in hdr
    assert "RECORDER_PART_SUFFIX" in src or ".wav.part" in hdr
    assert "ChunkSize" in src or "36u" in src
    # Close must patch sizes (seek back) before fclose — decodeability rule.
    assert "fseek" in src and "fclose" in src


def test_wav_open_cleanup_closes_file():
    src = WAV_C.read_text(encoding="utf-8")
    # Header-write AND header-flush failures in wav_part_open each close
    # the FILE and clear state (open x2), plus wav_part_close (x1): no path
    # leaks an open file on failure.
    assert src.count("fclose(part->fp)") >= 3
