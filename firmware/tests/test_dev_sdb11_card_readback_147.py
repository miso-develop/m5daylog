"""ER-144-SDB11-01: compile and execute actual DEV SD metadata/CID guard C.

The tested function bodies are extracted verbatim from sd_mount.c to avoid
mocking TinyUSB/FatFs host drivers. POSIX metadata files are synthetic. This
does not assert that physical FAT/VFS mount itself is hardware-write-free.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

COMP = Path(__file__).resolve().parents[1] / "components" / "recorder"
GEN = "11111111-1111-4111-8111-111111111111"
DEVICE = "22222222-2222-4222-8222-222222222222"
SHA = "a" * 64


def test_actual_dev_readback_and_card_guard_deny_swaps(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        pytest.skip("C compiler unavailable")
    source = (COMP / "sd_mount.c").read_text()
    start = source.index(
        "static bool dev_sd_same_selected_card(",
        source.index("// SDB-1.1 intentionally destructive"))
    end = source.index("// PRE-INTENT read-only media verification.", start)
    media = (COMP / "dev_sdb11_media_readback.c").read_text()
    media_start = media.index("// These reads are deliberately side-effect-free:")
    media_end = media.index("#else", media_start)
    # Real production card-identity and filesystem-readback C, no mirror.
    guarded_functions = source[start:end] + media[media_start:media_end]
    root = tmp_path / "card"
    daylog = root / "M5DAYLOG"
    recording = daylog / "recordings"
    quarantine = daylog / "quarantine"
    recording.mkdir(parents=True)
    quarantine.mkdir()
    device_doc = f"ID={DEVICE}"
    empty_doc = f"EMPTY={DEVICE}|1970-01-01T00:00:00+00:00"
    marker = (
        '{"schemaVersion":1,"generation":"' + GEN
        + '","deviceId":"' + DEVICE + '","baselineSha256":"' + SHA + '"}'
    )
    (daylog / "device.json").write_text(device_doc)
    (daylog / "manifest.json").write_text(empty_doc)
    (daylog / "dev-install.json").write_text(marker)

    prefix = f"""
    #include <assert.h>
    #include <stdbool.h>
    #include <stdint.h>
    #include <stddef.h>
    #include <stdio.h>
    #include <string.h>
    #include <stdlib.h>
    #include <errno.h>
    #include <dirent.h>
    #include <unistd.h>
    #define RECORDER_SD_MOUNT_POINT {json.dumps(str(root))}
    #define RECORDER_M5DAYLOG_DIR {json.dumps(str(daylog))}
    #define RECORDER_RECORDINGS_DIR {json.dumps(str(recording))}
    #define RECORDER_QUARANTINE_DIR {json.dumps(str(quarantine))}
    #define RECORDER_DEVICE_JSON_PATH {json.dumps(str(daylog / "device.json"))}
    #define RECORDER_MANIFEST_PATH {json.dumps(str(daylog / "manifest.json"))}
    typedef struct {{ struct {{ uint32_t serial; }} cid;
                       struct {{ uint64_t capacity; }} csd; }} sdmmc_card_t;
    static bool device_identity_is_valid_uuid(const char *s) {{
        return s && strlen(s) == 36;
    }}
    static bool device_manifest_is_valid_sha256_hex(const char *s) {{
        if (!s || strlen(s) != 64) return false;
        for (int i = 0; i < 64; ++i)
            if (!((s[i] >= '0' && s[i] <= '9') ||
                  (s[i] >= 'a' && s[i] <= 'f'))) return false;
        return true;
    }}
    static bool device_identity_build_device_json(const char *id,
          const char *model, const char *firmware,
          char *out, size_t len) {{
        (void)model; (void)firmware;
        int n = snprintf(out, len, "ID=%s", id);
        return n > 0 && (size_t)n < len;
    }}
    static bool device_manifest_build_empty(const char *id, const char *time,
                                           char *out, size_t len) {{
        int n = snprintf(out, len, "EMPTY=%s|%s", id, time);
        return n > 0 && (size_t)n < len;
    }}
    """
    body = """
    static void write_doc(const char *path, const char *value) {
        FILE *f = fopen(path, "wb");
        assert(f != NULL);
        assert(fwrite(value, 1u, strlen(value), f) == strlen(value));
        assert(fclose(f) == 0);
    }
    int main(void) {
        const char *gen = "__GEN__";
        const char *id = "__ID__";
        const char *sha = "__SHA__";
        const char *manifest = RECORDER_MANIFEST_PATH;
        const char *marker = RECORDER_M5DAYLOG_DIR "/dev-install.json";
        const char *correct_marker =
            "{\\"schemaVersion\\":1,\\"generation\\":\\"__GEN__\\","
            "\\"deviceId\\":\\"__ID__\\",\\"baselineSha256\\":\\"__SHA__\\"}";
        sdmmc_card_t a = { .cid.serial = 0x12345678u,
                           .csd.capacity = 4096u };
        sdmmc_card_t b = { .cid.serial = 0x87654321u,
                           .csd.capacity = 4096u };
        sdmmc_card_t c = { .cid.serial = 0x12345678u,
                           .csd.capacity = 8192u };
        assert(dev_sd_same_selected_card(&a, 0x12345678u, 4096u));
        assert(!dev_sd_same_selected_card(&b, 0x12345678u, 4096u));
        assert(!dev_sd_same_selected_card(&c, 0x12345678u, 4096u));
        assert(!dev_sd_same_selected_card(NULL, 0x12345678u, 4096u));

        assert(dev_sdb11_media_readback_matches(gen, id, sha, true));
        assert(dev_sdb11_media_readback_matches(gen, id, sha, false));
        assert(!dev_sdb11_media_readback_matches(
           "33333333-3333-4333-8333-333333333333", id, sha, true));
        assert(!dev_sdb11_media_readback_matches(
           gen, "33333333-3333-4333-8333-333333333333", sha, true));
        assert(!dev_sdb11_media_readback_matches(
           gen, id, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", true));

        write_doc(marker, "corrupted-marker");
        assert(!dev_sdb11_media_readback_matches(gen, id, sha, true));
        assert(!dev_sdb11_media_readback_matches(gen, id, sha, false));
        write_doc(marker, correct_marker);
        assert(remove(marker) == 0);
        assert(!dev_sdb11_media_readback_matches(gen, id, sha, false));
        write_doc(marker, correct_marker);
        write_doc(RECORDER_DEVICE_JSON_PATH, "wrong-device");
        assert(!dev_sdb11_media_readback_matches(gen, id, sha, false));
        write_doc(RECORDER_DEVICE_JSON_PATH, "ID=__ID__");

        write_doc(manifest, "changed-after-recording");
        assert(!dev_sdb11_media_readback_matches(gen, id, sha, true));
        assert(dev_sdb11_media_readback_matches(gen, id, sha, false));
        write_doc(manifest, "EMPTY=__ID__|1970-01-01T00:00:00+00:00");

        write_doc(RECORDER_RECORDINGS_DIR "/old.wav", "old-bytes");
        assert(!dev_sdb11_media_readback_matches(gen, id, sha, true));
        assert(dev_sdb11_media_readback_matches(gen, id, sha, false));
        assert(remove(RECORDER_RECORDINGS_DIR "/old.wav") == 0);
        write_doc(RECORDER_M5DAYLOG_DIR "/unexpected", "data");
        assert(!dev_sdb11_media_readback_matches(gen, id, sha, true));
        assert(remove(RECORDER_M5DAYLOG_DIR "/unexpected") == 0);
        assert(dev_sdb11_media_readback_matches(gen, id, sha, true));
        puts("SDB11_SELECTED_MEDIA_DENIALS=PASS");
        return 0;
    }
    """
    body = body.replace("__GEN__", GEN).replace("__ID__", DEVICE).replace("__SHA__", SHA)
    src = tmp_path / "card_guard.c"
    src.write_text(prefix + guarded_functions + body)
    exe = tmp_path / "card_guard"
    compiled = subprocess.run(
        [cc, "-std=c11", "-D_POSIX_C_SOURCE=200809L",
         "-Wall", "-Wextra", "-Werror", str(src), "-o", str(exe)],
        text=True, capture_output=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    run = subprocess.run([str(exe)], text=True, capture_output=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "SDB11_SELECTED_MEDIA_DENIALS=PASS" in run.stdout
