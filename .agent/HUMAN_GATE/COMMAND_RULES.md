# Human Gate Command Rules

version: 1

## 1. Purpose

These rules prevent command, shell, logging, toolchain, and device-observation failures from invalidating Human Gate evidence or being misclassified as product failures.

They are based on failure modes observed during real Human Gate execution, generalized so they do not contain machine-specific information.

## 2. General command contract

Every operator command used for Human Gate execution SHOULD:

1. be self-contained for one phase or one observation;
2. validate prerequisites before mutation;
3. fail with an explicit `STOP:`-style message when continuing would make evidence ambiguous;
4. use bounded waits rather than indefinite blocking;
5. record the exact revision or artifact identity when relevant;
6. capture command output to evidence when it affects acceptance;
7. report the underlying native process exit code when a native tool is used;
8. avoid embedding machine-specific paths or identifiers in committed documentation.

Prefer deriving paths from `<repo-root>`, the current worktree, tool configuration, or operator-provided variables rather than copying a workstation-specific absolute path into repository policy.

## 3. PowerShell and native stderr

### Known failure pattern

Some native tools write normal progress or verbose test output to stderr even when they succeed. Python `unittest -v` is one example.

Windows PowerShell can surface such stderr text as `NativeCommandError` when `$ErrorActionPreference = "Stop"`, especially when native output is piped or merged with `2>&1`.

### Required behavior

Do NOT treat the presence of native stderr as command failure.

For native commands:

- determine success primarily from the native process exit code;
- capture stdout and stderr separately when practical;
- avoid `2>&1 | Tee-Object` under terminating PowerShell error semantics for tools known to use stderr normally;
- if `$ErrorActionPreference = "Stop"` is used for PowerShell operations, isolate native execution so expected stderr cannot terminate the wrapper;
- after execution, parse log content only as supplemental evidence, not as a substitute for the exit code unless the tool lacks a reliable exit code.

For unit-test evidence, record both the native exit code and the test framework summary. A verbose stream alone is not a PASS or FAIL verdict.

## 4. Process execution and timeout discipline

Use direct native invocation for simple bounded commands when it provides a reliable exit code.

Use an explicit child process only when concurrency, live file-size monitoring, interruption testing, or timeout control requires it.

When managing a child process:

- redirect stdout and stderr to files rather than leaving ambiguous interactive pipes;
- keep the process handle / PID;
- wait with a defined deadline;
- after the deadline, classify the harness as blocked before terminating the child;
- confirm no relevant child process remains before retrying;
- do not wait indefinitely for a wrapper process whose underlying test has already completed.

A wrapper hang after the underlying test prints a successful summary is a harness failure until the process exit state is established independently.

## 5. Toolchain resolution before build

Before a build used for Human Gate evidence:

- print the resolved executable path for critical build tools when multiple installations may exist;
- print and validate required tool versions;
- load the project's supported toolchain environment before invoking the build;
- reject an unsupported resolved version before compilation begins.

If a failed configure/build created an incomplete build directory, do not assume the normal clean command can safely delete it. Validate the target directory identity first, then remove only that exact generated directory when cleanup is necessary.

Never use broad recursive deletion against an inferred path.

## 6. Evidence parser separation

The command that runs a test and the command that validates previously captured evidence should be separable.

This allows a completed native test to remain usable even if the original wrapper, display pipeline, or post-processing step fails.

An evidence parser should:

- read immutable captured logs;
- check the expected test count / summary / absence of failure markers as applicable;
- write a compact machine-readable result;
- never rerun the underlying physical or destructive action merely because post-processing failed.

## 7. Device enumeration and reset behavior

### Known failure pattern

USB serial/JTAG and similar interfaces may disappear and re-enumerate during reset. A serial read can fail at that moment even though the firmware continues booting normally.

### Required behavior

A serial collector used across reset MUST tolerate temporary disconnect and re-enumeration when the platform is expected to do so.

It should:

- observe device/interface state over a bounded window;
- reopen the serial endpoint when it returns;
- preserve timestamps or ordering of disconnect/reconnect observations;
- classify a transient collector disconnect as harness behavior until product state proves otherwise.

Do not stop diagnosis at the first `ClearCommError`, disconnected handle, or equivalent host-side serial exception.

## 8. Do not infer firmware mode from one USB identifier alone

A USB VID/PID or COM-port presence can be shared by ROM, USB Serial/JTAG, bootloader, or application states depending on the platform configuration.

Before concluding that a device is in ROM download mode or normal application mode, use corroborating evidence such as:

- boot/application logs;
- expected product USB interface publication;
- firmware-specific heartbeat/state output;
- the authoritative platform boot-mode indicator.

Friendly names localized by the operating system are diagnostic only. Prefer stable identifiers and product behavior for acceptance decisions.

## 9. Observation before destructive recovery

Before erase, factory reset, persistent-state deletion, filesystem repair, or other destructive recovery:

1. capture the current observable state;
2. preserve diagnostic logs needed to explain the failure;
3. establish why the destructive action is necessary;
4. state whether subsequent results form a fresh baseline rather than continuation of the previous cycle.

A destructive recovery never retroactively makes the preceding cycle PASS.

## 10. Bounded polling

Any loop waiting for a drive, serial port, USB interface, process exit, file-size threshold, sleep/resume completion, or other external state MUST have a deadline.

On deadline expiry:

- stop the phase;
- print the last observed state;
- preserve logs;
- classify the phase according to `.agent/HUMAN_GATE/EXECUTION_RULES.md`:
  - `FAIL` when valid preconditions and a valid observation path remained present for the required window but the expected product state or completion event did not occur;
  - `BLOCKED` when tooling, harness, environment, dependency, hardware availability, or loss of the observation path prevented a valid acceptance observation;
  - `NOT_RUN` only when the required phase was never attempted.

Deadline expiry by itself does not determine the disposition; the validity of the test preconditions and observation path does.

Never leave a Human Gate command with an unbounded polling loop.

## 11. Physical interruption tests

For interruption tests such as cable removal during copy / sync:

- establish the measurable trigger before starting;
- use a deterministic threshold or observable condition;
- tell the operator exactly when to act;
- preserve the partial-state evidence;
- verify that the interrupted process exits or is cleanly bounded;
- verify the intended fail-closed product state before reconnect/recovery;
- verify recovery and idempotency independently after reconnect.

Do not use the interruption itself as proof of a separate lifecycle event unless the approved Human Gate matrix explicitly allows combined evidence.

## 12. Command review checklist

Before sending an operator command, verify:

- Does it mutate only what this phase requires?
- Is every destructive target identity checked?
- Can normal stderr be mistaken for failure?
- Is the native exit code preserved?
- Can the command hang indefinitely?
- Can USB/serial re-enumeration invalidate the collector?
- Is the expected success condition observable?
- Is failure classified as harness vs product correctly?
- Does any committed example expose machine-specific or user-specific information?

If any answer is unsafe or ambiguous, revise the command before asking the human to run it.
