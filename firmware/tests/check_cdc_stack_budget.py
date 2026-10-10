#!/usr/bin/env python3
"""Conservative CDC JSON worker stack-budget check from Xtensa .su output."""

from __future__ import annotations

import pathlib
import re
import sys

MIN_STACK_MARGIN_BYTES = 2048

REQUIRED_FUNCTIONS = (
    "usb_cdc_worker_task",
    "usb_cdc_protocol_process_line",
    "cJSON_ParseWithOpts",
    "cJSON_ParseWithLengthOpts",
    "parse_value",
    "parse_object",
    "parse_array",
    "parse_string",
    "parse_number",
    "cJSON_Delete",
)


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"CDC stack budget: FAIL: {message}")


def read_define(path: pathlib.Path, name: str) -> int:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"^\s*#define\s+{re.escape(name)}\s+(\d+)[uUlL]*\s*$",
        text,
        re.MULTILINE,
    )
    if match is None:
        fail(f"missing numeric {name} in {path}")
    return int(match.group(1))


def read_cmake_limit(path: pathlib.Path) -> int:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"^\s*set\(M5DAYLOG_CJSON_NESTING_LIMIT\s+(\d+)\)\s*$",
        text,
        re.MULTILINE,
    )
    if match is None:
        fail(f"missing M5DAYLOG_CJSON_NESTING_LIMIT in {path}")
    return int(match.group(1))


def base_function_name(name: str) -> str:
    for suffix in (".constprop.", ".isra.", ".part."):
        if suffix in name:
            return name.split(suffix, 1)[0]
    return name


def load_stack_usage(build_dir: pathlib.Path) -> dict[str, int]:
    found: dict[str, int] = {}
    files = list(build_dir.rglob("*.su"))
    if not files:
        fail(f"no -fstack-usage output found below {build_dir}")

    for path in files:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = raw_line.split("\t")
            if len(fields) < 3:
                continue
            descriptor, byte_text, qualifier = fields[0], fields[1], fields[2]
            try:
                stack_bytes = int(byte_text)
            except ValueError:
                continue
            function = base_function_name(descriptor.rsplit(":", 1)[-1])
            if function not in REQUIRED_FUNCTIONS:
                continue
            if qualifier.strip() != "static":
                fail(
                    f"{function} has non-static stack classification "
                    f"{qualifier!r} in {path}"
                )
            found[function] = max(found.get(function, 0), stack_bytes)

    missing = [name for name in REQUIRED_FUNCTIONS if name not in found]
    if missing:
        fail("missing stack-usage entries: " + ", ".join(missing))
    return found


def main() -> int:
    if len(sys.argv) != 2:
        fail("usage: check_cdc_stack_budget.py <firmware-build-dir>")

    build_dir = pathlib.Path(sys.argv[1]).resolve()
    firmware_dir = pathlib.Path(__file__).resolve().parents[1]
    protocol_source = firmware_dir / "components" / "recorder" / "usb_cdc_protocol.c"
    project_cmake = firmware_dir / "CMakeLists.txt"

    worker_stack = read_define(protocol_source, "CDC_WORKER_STACK_BYTES")
    nesting_limit = read_cmake_limit(project_cmake)
    if nesting_limit < 2:
        fail(f"nesting limit {nesting_limit} cannot represent the v1 root+args envelope")

    usage = load_stack_usage(build_dir)
    worker = usage["usb_cdc_worker_task"]
    process = usage["usb_cdc_protocol_process_line"]
    parse_opts = usage["cJSON_ParseWithOpts"]
    parse_length = usage["cJSON_ParseWithLengthOpts"]
    parse_value = usage["parse_value"]
    container = max(usage["parse_object"], usage["parse_array"])
    leaf = max(usage["parse_string"], usage["parse_number"])
    delete = usage["cJSON_Delete"]

    base = worker + process + parse_opts + parse_length

    # Accepted deepest input: D container frames, then a primitive leaf. There
    # are D+1 parse_value frames because the leaf is itself a value.
    accepted_parser_peak = (
        base
        + (nesting_limit + 1) * parse_value
        + nesting_limit * container
        + leaf
    )

    # Rejection boundary: an input may enter one additional container parser
    # frame before CJSON_NESTING_LIMIT rejects it. Add deletion frames as a
    # deliberately conservative allowance for cJSON's failure unwinding.
    rejection_parser_peak = (
        base
        + (nesting_limit + 1) * parse_value
        + (nesting_limit + 1) * container
        + (nesting_limit + 1) * delete
    )

    # Valid-tree cleanup happens after parser frames have returned.
    valid_delete_peak = worker + process + (nesting_limit + 1) * delete

    estimated_peak = max(accepted_parser_peak, rejection_parser_peak, valid_delete_peak)
    margin = worker_stack - estimated_peak

    print(f"CDC worker stack bytes: {worker_stack}")
    print(f"cJSON nesting limit: {nesting_limit}")
    for name in REQUIRED_FUNCTIONS:
        print(f"stack {name}: {usage[name]} bytes")
    print(f"accepted parser conservative peak: {accepted_parser_peak} bytes")
    print(f"rejection parser conservative peak: {rejection_parser_peak} bytes")
    print(f"valid delete conservative peak: {valid_delete_peak} bytes")
    print(f"CDC conservative peak: {estimated_peak} bytes")
    print(f"CDC stack margin: {margin} bytes")
    print(f"required unmodelled-call reserve: {MIN_STACK_MARGIN_BYTES} bytes")

    if margin < MIN_STACK_MARGIN_BYTES:
        fail(
            f"margin {margin} < required reserve {MIN_STACK_MARGIN_BYTES}; "
            f"increase CDC worker stack or reduce bounded parser usage"
        )

    print("CDC stack budget: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
