"""SDB-1.1: compile the actual guarded DEV installer C with host POSIX stubs.

This checks the *enabled* DEV-only source, unlike the default-off IDF build.
It is not an ESP-IDF configured installer image or physical SD proof.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "components" / "recorder"


@pytest.mark.parametrize("installer_on", [True, False])
def test_actual_sdb11_installer_c_compiles(tmp_path, installer_on):
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        pytest.skip("native compiler unavailable")
    inc = tmp_path / "include"
    inc.mkdir()
    (inc / "esp_err.h").write_text(
        "#pragma once\ntypedef int esp_err_t;\n"
        "#define ESP_OK 0\n#define ESP_FAIL 1\n"
        "#define ESP_ERR_INVALID_STATE 3\n#define ESP_ERR_INVALID_ARG 2\n"
        "#define ESP_ERR_NOT_SUPPORTED 5\n")
    args = [
        cc, "-std=c11", "-D_POSIX_C_SOURCE=200809L",
        "-Wall", "-Wextra", "-Werror", "-fsyntax-only",
        "-DESP_PLATFORM", "-DCONFIG_M5DAYLOG_SDB11_DEV_INSTALL=1",
        "-DCONFIG_M5DAYLOG_SDB11_INSTALLER_MODE=" +
        ("1" if installer_on else "0"),
        "-I", str(inc), "-I", str(COMP / "include"),
        str(COMP / "dev_sdb11_installer.c"),
    ]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
