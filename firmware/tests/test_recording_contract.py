"""Task #44 scope-boundary tests: format, fail-loud, and out-of-scope guards.

Stdlib only. Ensures the #44 implementation keeps the fixed 16kHz/16bit/
mono contract, stays fail-loud (no silent recording state), writes only
`.part` files, and does NOT absorb Task #45/#46/#47 responsibilities
(rotation+finalize, power-loss recovery/quarantine, manifest/retention).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "firmware/components/recorder"
MAIN = REPO / "firmware/main/main.c"


def _sources():
    return {
        str(p.relative_to(REPO)): p.read_text(encoding="utf-8")
        for p in COMP.rglob("*")
        if p.is_file() and p.suffix in (".c", ".h")
    }


def test_format_contract_present():
    srcs = _sources()
    blob = "\n".join(srcs.values())
    assert "16000" in blob
    assert "I2S_DATA_BIT_WIDTH_16BIT" in blob
    assert "I2S_SLOT_MODE_MONO" in blob
    assert "RECORDER_SAMPLE_RATE_HZ" in blob


def test_part_suffix_discipline():
    srcs = _sources()
    blob = "\n".join(srcs.values())
    assert ".part" in blob
    # #44 never finalizes to bare `.wav`: no rename/remove in the component.
    assert "rename(" not in blob
    assert "remove(" not in blob


def test_fail_loud_symbols():
    main = MAIN.read_text(encoding="utf-8")
    for marker in (
        "result: error",
        "mic init",
        "part open",
        "sd write",
        "buffer overflow",
        "dma overrun",
    ):
        assert marker in main, marker
    # Unset-pin guard: no silent recording when board pins are missing.
    capture = (COMP / "i2s_pdm_capture.c").read_text(encoding="utf-8")
    assert "pdm pins unset" in capture
    assert "ESP_ERR_INVALID_ARG" in capture


def test_no_scope_creep_into_later_tasks():
    srcs = _sources()
    blob = "\n".join(srcs.values()).lower()
    # Rotation / finalize (#45) policy keywords must not be implemented here.
    for keyword in ("quarantine", "manifest", "device.json", "acks/"):
        assert keyword not in blob, keyword
    # This component documents that mount/rotation/recovery are out of scope.
    readme = (COMP / "README.md").read_text(encoding="utf-8").lower()
    for marker in ("out of scope", "#45", "#46", "never deletes"):
        assert marker in readme, marker


def test_no_credentials_or_private_data_patterns():
    srcs = _sources()
    blob = "\n".join(srcs.values())
    for forbidden in ("BEGIN PRIVATE KEY", "ghp_", "AKIA", "password="):
        assert forbidden not in blob, forbidden
    main = MAIN.read_text(encoding="utf-8")
    assert "password" not in main.lower()
