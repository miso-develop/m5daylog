"""Regression for Task #50 review finding REV-83-09."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TRANSPORT = REPO / "firmware/components/recorder/usb_cdc_protocol.c"


def test_response_tx_remains_inside_originating_session_boundary() -> None:
    """A response must stay fenced until TX finishes for its request session."""

    src = TRANSPORT.read_text(encoding="utf-8")

    writer_at = src.index("static bool cdc_write_response")
    rx_at = src.index("static void usb_cdc_rx_callback", writer_at)
    writer = src[writer_at:rx_at]

    worker_at = src.index("static void usb_cdc_worker_task", rx_at)
    reset_at = src.index("void usb_cdc_protocol_reset_session", worker_at)
    worker = src[worker_at:reset_at]

    process_at = worker.index("usb_cdc_protocol_process_line")
    tx_at = worker.index("cdc_write_response", process_at)
    end_at = worker.index("usb_cdc_session_gate_command_end", process_at)

    # Teardown waits for active commands. Releasing the command barrier before
    # TX leaves an old-session response in a stack buffer that can be emitted
    # after a later callback marks the next transport session connected.
    assert tx_at < end_at, "command barrier is released before response TX"

    signature = writer[: writer.index("{")]
    assert "uint32_t response_generation" in signature
    assert "usb_cdc_session_gate_tx_is_current" in writer

    tx_call = worker[tx_at:end_at]
    assert "frame_generation" in tx_call
