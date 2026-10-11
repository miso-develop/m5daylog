# Human Gate Command Rules

version: 2

## 1. Purpose

These rules prevent command, shell, logging, toolchain, and device-observation failures from invalidating Human Gate evidence or being misclassified as product failures.

They are based on failure modes observed during real Human Gate execution, generalized so they do not contain machine-specific information.

Reusable command/harness failure modes and their recovery patterns are maintained in `.agent/HUMAN_GATE/COMMAND_FAILURE_PLAYBOOK.md`.

## 2. General command contract

Every operator command used for Human Gate execution SHOULD:

1. be self-contained for one phase or one observation;
2. validate prerequisites before mutation;
3. fail with an explicit `STOP=` or equivalent diagnostic when continuing would make evidence ambiguous;
4. use bounded waits rather than indefinite blocking;
5. record the exact revision or artifact identity when relevant;
6. capture command output to evidence when it affects acceptance;
7. report the underlying native process exit code when a native tool is used;
8. avoid embedding machine-specific paths or identifiers in committed documentation;
9. finish with one compact machine-readable summary even when the phase is `BLOCKED` or stopped early, whenever the shell itself remains usable.

Prefer deriving paths from `<repo-root>`, the current worktree, tool configuration, or operator-provided variables rather than copying a workstation-specific absolute path into repository policy.

## 3. PowerShell command packet and final summary

Human Gate PowerShell operator commands MUST use one outer `& { ... }` block so prerequisites, execution, error classification, cleanup, and the final summary share one explicit scope.

The command SHOULD initialize a result object before executing acceptance-relevant work and gate subsequent steps with a continuation flag rather than using early `return` paths that skip evidence reporting.

When a phase cannot continue:

- print a concise `STOP=` or equivalent diagnostic;
- set a diagnosis and disposition in the result object;
- skip invalid later steps;
- still reach the common summary footer whenever feasible.

The minimum final summary is:

- phase identifier;
- exact tested `HEAD` or artifact identity when relevant;
- native exit code(s), or the key observation that prevented native execution;
- concise diagnosis;
- final `PASS` / `FAIL` / `BLOCKED` / `NOT_RUN` disposition.

Include additional acceptance-specific observations when they materially determine the verdict.

A summary is evidence metadata, not a replacement for raw logs. When parsing or session boundaries are disputed, retain enough raw output or a bounded raw tail to reconstruct the classification.

## 4. PowerShell text and native-process safety

### Empty text input

Do not invoke string methods directly on an unverified `Get-Content -Raw` result. An empty file may surface as `$null` in some PowerShell situations.

Use an explicit string conversion or `System.IO.File.ReadAllText()` when exact text semantics matter.

### Native stderr

Some native tools write normal progress or verbose test output to stderr even when they succeed. Python `unittest -v` is one example.

Windows PowerShell can surface such stderr text as `NativeCommandError` when `$ErrorActionPreference = "Stop"`, especially when native output is piped or merged with `2>&1`.

Do NOT treat the presence of native stderr as command failure.

For native commands:

- determine success primarily from the native process exit code;
- capture stdout and stderr separately when practical;
- avoid `2>&1 | Tee-Object` under terminating PowerShell error semantics for tools known to use stderr normally;
- if `$ErrorActionPreference = "Stop"` is used for PowerShell operations, isolate native execution so expected stderr cannot terminate the wrapper;
- after execution, parse log content only as supplemental evidence, not as a substitute for the exit code unless the tool lacks a reliable exit code.

For unit-test evidence, record both the native exit code and the test framework summary. A verbose stream alone is not a PASS or FAIL verdict.

### Native process exit codes

For simple native commands, prefer direct invocation when it provides a reliable exit code.

When timeout/concurrency/redirection control requires an explicit child process, prefer `System.Diagnostics.Process` with explicit `WaitForExit`, separately captured stdout/stderr, and the completed process's concrete `ExitCode`.

Do not assume that a `Start-Process -PassThru` wrapper pattern has produced a reliable exit code unless the process lifecycle and wait semantics have been explicitly verified.

## 5. Process execution and timeout discipline

Use direct native invocation for simple bounded commands when it provides a reliable exit code.

Use an explicit child process only when concurrency, live file-size monitoring, interruption testing, or timeout control requires it.

When managing a child process:

- capture stdout and stderr without relying on ambiguous merged interactive pipelines;
- keep the process handle / PID;
- wait with a defined deadline;
- after the deadline, classify the harness as blocked before terminating the child;
- confirm no relevant child process remains before retrying;
- do not wait indefinitely for a wrapper process whose underlying test has already completed.

A wrapper hang after the underlying test prints a successful summary is a harness failure until the process exit state is established independently.

Timeout values MUST be operation-specific and realistic. Environment activation, dependency checks, builds, flashes, serial observations, and short host queries may require materially different deadlines. A bounded wait is required; one arbitrary universal timeout is not.

## 6. Toolchain resolution before build

Before a build used for Human Gate evidence:

- print the resolved executable path for critical build tools when multiple installations may exist;
- print and validate required tool versions;
- load the project's supported toolchain environment before invoking the build;
- reject an unsupported resolved version before compilation begins;
- verify the actual Python/interpreter selected by the activated environment when Python resolution can be shadowed by operating-system launchers or aliases.

For ESP-IDF or similar toolchains, interpret environment-export helper output according to the exact supported toolchain version. If an export helper produces a generated shell script, source that script and verify the resolved tools; do not assume the helper output itself contains environment assignments.

If a failed configure/build created an incomplete build directory, do not assume the normal clean command can safely delete it. Validate the target directory identity first, then remove only that exact generated directory when cleanup is necessary.

Never use broad recursive deletion against an inferred path.

## 7. Evidence parser separation

The command that runs a test and the command that validates previously captured evidence should be separable.

This allows a completed native test to remain usable even if the original wrapper, display pipeline, or post-processing step fails.

An evidence parser should:

- read immutable captured logs;
- check the expected test count / summary / absence of failure markers as applicable;
- write a compact machine-readable result;
- never rerun the underlying physical or destructive action merely because post-processing failed;
- preserve enough raw context to diagnose a parser disagreement;
- correct a previously emitted harness verdict when later parsing proves that verdict was generated from the wrong session or marker scope.

A parser failure is a harness failure. It does not invalidate an independently valid product observation merely because the first automatic verdict was wrong.

## 8. Device enumeration and reset behavior

### USB/serial re-enumeration

USB serial/JTAG and similar interfaces may disappear and re-enumerate during reset. A serial read can fail at that moment even though the firmware continues booting normally.

A serial collector used across reset MUST tolerate temporary disconnect and re-enumeration when the platform is expected to do so.

It should:

- observe device/interface state over a bounded window;
- reopen the serial endpoint when it returns;
- preserve timestamps or ordering of disconnect/reconnect observations;
- classify a transient collector disconnect as harness behavior until product state proves otherwise.

Do not stop diagnosis at the first `ClearCommError`, disconnected handle, or equivalent host-side serial exception.

### Host enumeration fallback

A failure of one host enumeration API is not proof that the device is absent.

When device presence matters and the preferred query fails, use an independent supported observation path where available, such as broader PnP enumeration or CIM/WMI enumeration. Distinguish:

- `host query failed`; from
- `host query succeeded and device was not present`.

### Low-level read side effects

A flash/NVS read may be non-destructive to persistent data while still resetting the target, changing boot mode, or forcing USB re-enumeration.

Do not treat product runtime observations made before and after such a diagnostic command as one continuous application session unless continuity is independently proven. Re-establish the runtime baseline after the diagnostic phase.

## 9. Reset/session boundaries and boot-mode parsing

A reset-sensitive serial parser MUST scope product evidence to the boot/action session being evaluated.

Before classifying a reset or boot result:

1. record or otherwise establish the operator/action boundary;
2. identify the authoritative boot marker after that boundary;
3. classify only evidence belonging to that new boot episode;
4. preserve earlier output as setup/history, not as the result of the new action.

A decoded line may contain bytes from both sides of reset when the target resets before a newline is emitted. If a line contains a stale pre-reset state fragment concatenated with a post-reset ROM banner, do not classify the stale prefix as post-reset evidence. Preserve the raw line, but split/discard the pre-boundary prefix for the new-session verdict.

Do not use capture-global marker presence for boot-mode classification. For example, an old `waiting for download` observed before a later physical reset must not make the later normal boot classify as Download Mode.

A reset helper's own success marker proves only that the helper completed its action. It does not prove which boot mode the target entered. Verify the post-reset boot mode from the authoritative boot output or other approved platform indicator.

When transitioning from ROM Download Mode to normal application execution, use the board/platform's proven reset/strap procedure if a software control-line reset does not reliably establish the intended strap state.

## 10. Do not infer firmware mode from one USB identifier alone

A USB VID/PID or COM-port presence can be shared by ROM, USB Serial/JTAG, bootloader, or application states depending on the platform configuration.

Before concluding that a device is in ROM download mode or normal application mode, use corroborating evidence such as:

- boot/application logs;
- expected product USB interface publication;
- firmware-specific heartbeat/state output;
- the authoritative platform boot-mode indicator.

Friendly names localized by the operating system are diagnostic only. Prefer stable identifiers and product behavior for acceptance decisions.

## 11. Observation before destructive recovery

Before erase, factory reset, persistent-state deletion, filesystem repair, or other destructive recovery:

1. capture the current observable state;
2. preserve diagnostic logs needed to explain the failure;
3. establish why the destructive action is necessary;
4. state whether subsequent results form a fresh baseline rather than continuation of the previous cycle.

A destructive recovery never retroactively makes the preceding cycle PASS.

A reversible state-retirement operation, such as same-volume rename of stale test data, should still be treated as a baseline-changing mutation. Preserve the retired state and establish a fresh baseline before counting acceptance cycles.

## 12. Bounded polling

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

## 13. Physical interruption tests

For interruption tests such as cable removal during copy / sync:

- establish the measurable trigger before starting;
- use a deterministic threshold or observable condition;
- tell the operator exactly when to act;
- preserve the partial-state evidence;
- verify that the interrupted process exits or is cleanly bounded;
- verify the intended fail-closed product state before reconnect/recovery;
- verify recovery and idempotency independently after reconnect.

Do not use the interruption itself as proof of a separate lifecycle event unless the approved Human Gate matrix explicitly allows combined evidence.

## 14. Command review checklist

Before sending an operator command, verify:

- Is the command one outer PowerShell `& { ... }` block when PowerShell is used?
- Does every normal, `STOP`, and `BLOCKED` path reach the common summary when feasible?
- Does the summary include phase, exact revision/artifact, native exit/key observation, diagnosis, and disposition?
- Does it mutate only what this phase requires?
- Is every destructive target identity checked?
- Can normal stderr be mistaken for failure?
- Is the native exit code preserved reliably?
- Can an empty text input crash string post-processing?
- Can the command hang indefinitely or use an unrealistically short timeout?
- Is the actual activated interpreter/toolchain verified?
- Can USB/serial re-enumeration invalidate the collector?
- Does a low-level observation reset or otherwise alter runtime state?
- Is serial parsing scoped to the correct reset/action session?
- Can historical boot-mode markers contaminate the current verdict?
- Is the expected success condition observable?
- Is failure classified as harness vs product correctly?
- Does any committed example expose machine-specific or user-specific information?
- Is a relevant known pattern already documented in `COMMAND_FAILURE_PLAYBOOK.md`?

If any answer is unsafe or ambiguous, revise the command before asking the human to run it.
