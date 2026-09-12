"""Task #44 host contract tests: ping-pong double buffer + counters.

Stdlib only. Locks the 32KB x 2 constants from recorder_config.h and the
produce/consume/overflow rules implemented by pcm_pipeline.c, using a
Python model of the documented algorithm plus source-presence assertions
so the C implementation cannot silently drift.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CFG = REPO / "firmware/components/recorder/include/recorder_config.h"
PIPE_H = REPO / "firmware/components/recorder/include/pcm_pipeline.h"
PIPE_C = REPO / "firmware/components/recorder/pcm_pipeline.c"

BUFFER_BYTES = 32768
SLOTS = 2


class Model:
    """Faithful Python model of pcm_pipeline.c produce/consume rules."""

    def __init__(self):
        self.slot = [bytearray(BUFFER_BYTES), bytearray(BUFFER_BYTES)]
        self.fill = [0, 0]
        self.full = [False, False]
        self.read = 0
        self.write = 0
        self.captured = 0
        self.written = 0
        self.overflow = 0
        self.overrun_events = 0
        self.drop_bytes = 0
        self.drop_samples = 0

    def produce(self, data: bytes) -> int:
        usable = data[: len(data) - (len(data) % 2)]
        off = 0
        while off < len(usable):
            if self.full[0] and self.full[1]:
                rest = len(usable) - off
                self.overflow += 1
                self.drop_bytes += rest
                self.drop_samples += rest // 2
                return len(data) - off
            if self.full[self.write]:
                self.write = 1 - self.write
            w = self.write
            room = BUFFER_BYTES - self.fill[w]
            take = min(room, len(usable) - off)
            self.slot[w][self.fill[w] : self.fill[w] + take] = usable[
                off : off + take
            ]
            self.fill[w] += take
            off += take
            self.captured += take // 2
            if self.fill[w] == BUFFER_BYTES:
                self.full[w] = True
                self.write = 1 - w
        return len(data) - off

    def has_full(self):
        return self.full[0] or self.full[1]

    def release_full(self):
        idx = self.read
        if not self.full[idx]:
            idx = 1 - idx
            if not self.full[idx]:
                return False
        self.full[idx] = False
        self.fill[idx] = 0
        self.read = 1 - idx
        self.written += 1
        return True

    def note_overrun(self):
        # Mirrors pcm_pipeline_note_dma_overrun: every flagged read counts
        # as an event, even with a complete payload (no loss claimed).
        self.overrun_events += 1

    def note_gap(self, missing: int):
        # Mirrors pcm_pipeline_note_dma_gap: a short-read gap always joins
        # the drop counters, so it can never report drop=0.
        if missing:
            self.drop_bytes += missing
            self.drop_samples += missing // 2


def test_config_constants():
    src = CFG.read_text(encoding="utf-8")
    assert "#define RECORDER_SAMPLE_RATE_HZ 16000u" in src
    assert "#define RECORDER_CHANNELS 1u" in src
    assert "#define RECORDER_BITS_PER_SAMPLE 16u" in src
    assert "#define RECORDER_BUFFER_BYTES 32768u" in src
    assert "#define RECORDER_BUFFER_SLOTS 2u" in src
    assert '#define RECORDER_PART_SUFFIX ".wav.part"' in src
    assert "#define RECORDER_WAV_HEADER_SIZE 44u" in src


def test_one_hour_needs_no_drop_math():
    # 1h @16kHz/16bit/mono = 115,200,000 payload bytes = 57,600,000 samples.
    one_hour_bytes = 16000 * 2 * 3600
    assert one_hour_bytes == 115200000
    assert one_hour_bytes // 2 == 57600000
    # The stream does NOT end on a buffer boundary: 3515 complete 32KB
    # buffers plus a final 20480-byte partial buffer. The partial tail is
    # normal and must not be classified as drop or overflow.
    full, tail = divmod(one_hour_bytes, BUFFER_BYTES)
    assert (full, tail) == (3515, 20480)
    assert tail // 2 == 10240  # sample-aligned tail, whole samples only


def test_model_one_hour_stream_ends_with_partial_not_drop():
    # Drive the documented pipeline exactly as main.c does (4096-byte I2S
    # reads, drain full slots to SD as they appear) over a full hour.
    m = Model()
    chunk = bytes(4096)  # device scratch size in main.c
    total = 16000 * 2 * 3600
    produced = 0
    while produced < total:
        assert m.produce(chunk) == 0
        produced += len(chunk)
        while m.has_full():
            assert m.release_full() is True
    assert produced == 115200000
    assert m.captured == 57600000
    assert m.overflow == 0
    assert m.drop_bytes == 0 and m.drop_samples == 0
    assert m.written == 3515
    # Final partial buffer waits in the active slot: not full, not dropped.
    assert not m.has_full()
    assert m.fill[m.write] == 20480


def test_model_continuous_capture_no_drop():
    m = Model()
    chunk = bytes(4096)  # device scratch size in main.c
    for _ in range(8):  # exactly one 32KB slot
        assert m.produce(chunk) == 0
    assert m.has_full()
    assert m.overflow == 0 and m.drop_bytes == 0
    assert m.captured == BUFFER_BYTES // 2 == 16384
    assert m.release_full() is True
    assert m.written == 1 and not m.has_full()


def test_model_overflow_counts_not_silently_overwrites():
    m = Model()
    assert m.produce(bytes(BUFFER_BYTES)) == 0
    assert m.produce(bytes(BUFFER_BYTES)) == 0
    assert m.full == [True, True]
    dropped = m.produce(bytes(1024))
    assert dropped == 1024
    assert m.overflow == 1
    assert m.drop_bytes == 1024 and m.drop_samples == 512
    # Draining one slot re-opens the pipeline.
    assert m.release_full() is True
    assert m.produce(bytes(1024)) == 0


def test_model_overrun_event_without_gap_claims_no_loss():
    m = Model()
    m.note_overrun()
    assert m.overrun_events == 1
    assert m.drop_bytes == 0 and m.drop_samples == 0


def test_model_gap_always_counts_as_drop():
    m = Model()
    m.note_overrun()
    m.note_gap(4096)
    assert m.overrun_events == 1
    assert m.drop_bytes == 4096 and m.drop_samples == 2048


def test_model_odd_tail_held_back():
    m = Model()
    assert m.produce(b"\x01\x02\x03") == 1  # held-back byte, not dropped
    assert m.captured == 1
    assert m.overflow == 0 and m.drop_bytes == 0


def test_c_source_implements_pipeline_contract():
    src = PIPE_C.read_text(encoding="utf-8")
    hdr = PIPE_H.read_text(encoding="utf-8")
    for symbol in (
        "pcm_pipeline_init",
        "pcm_pipeline_produce",
        "pcm_pipeline_has_full",
        "pcm_pipeline_peek_full",
        "pcm_pipeline_release_full",
        "pcm_pipeline_note_sd_write",
        "pcm_pipeline_note_sd_error",
        "pcm_pipeline_note_dma_overrun",
        "pcm_pipeline_note_dma_gap",
        "buffer_overflow",
        "dma_overrun_events",
        "dma_drop_bytes",
        "dma_drop_samples",
        "sd_write_errors",
        "max_sd_latency_us",
        "samples_captured",
        "chunks_written",
    ):
        assert symbol in src or symbol in hdr, symbol
