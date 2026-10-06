# Human Gate Command Failure Playbook

version: 1

## 1. Purpose

This playbook records command, shell, parser, toolchain, and host-observation failure modes that have invalidated or nearly invalidated Human Gate evidence.

It supplements `.agent/HUMAN_GATE/COMMAND_RULES.md` and `.agent/HUMAN_GATE/EXECUTION_RULES.md`.

The goal is prevention and correct classification. A failure described here is not, by itself, evidence that the product violated an Acceptance Criterion.

## 2. Classification invariant

When a command wrapper, parser, shell, toolchain, host API, serial collector, or evidence-processing step fails:

1. preserve the underlying product observation when it is independently valid;
2. record the command/harness defect separately from the Human Gate acceptance disposition; a tooling defect does not by itself force the acceptance result to `BLOCKED`;
3. classify the affected acceptance observation as `BLOCKED` only when the defect prevents a valid observation or prevents the preserved evidence from being reconstructed with sufficient confidence;
4. when independently valid evidence remains sufficient to evaluate the Acceptance Criterion, record `PASS` or `FAIL` according to that evidence even if the wrapper/parser itself failed or emitted a wrong verdict;
5. use `NOT_RUN` only when the intended phase was never attempted;
6. do not rerun destructive or physical actions merely because post-processing failed when immutable evidence can be reparsed;
7. correct an earlier harness-generated verdict when later evidence proves that the parser or wrapper misclassified the same observation.

The tooling diagnosis and the acceptance disposition are separate records. A parser/wrapper failure may therefore remain documented as a harness defect while the acceptance result is corrected to `PASS` or `FAIL` from independently valid evidence. Conversely, when the defect actually prevents a valid acceptance observation, the acceptance disposition remains `BLOCKED`.

A corrected harness verdict does not erase the raw observation. It changes only the interpretation that was shown to be invalid.

## 3. Failure record format

When adding a new reusable failure mode to this file, record:

- **Symptom**: what the operator or Agent sees;
- **Cause**: the command/harness mechanism that produced it;
- **Risk**: how it can invalidate or misclassify Human Gate evidence;
- **Prevention**: the required command-design rule;
- **Recovery**: how to continue without destroying valid evidence.

Keep examples public-safe. Do not commit workstation paths, usernames, private repository metadata, device serial numbers, credentials, or raw personal product data.

## 4. PowerShell empty-file `.Trim()` failure

### Symptom

A PowerShell command reads a file with `Get-Content -Raw` and then throws because `.Trim()` is invoked on `$null`.

### Cause

For an empty file, `Get-Content -Raw` may produce `$null` rather than an empty string in some PowerShell situations.

### Risk

A valid empty artifact can be misclassified as a product or evidence failure because the wrapper crashes before it reports the actual state.

### Prevention

Do not call string methods directly on an unverified `Get-Content -Raw` result.

Prefer one of:

- `[string](Get-Content -Raw -LiteralPath <path>)` before calling string methods; or
- `[System.IO.File]::ReadAllText(<path>)` when exact text semantics are required.

### Recovery

Repair only the parser/wrapper and re-read the existing file. Do not rerun the underlying physical action solely because the text parser failed.

## 5. Unreliable native exit-code capture through `Start-Process`

### Symptom

A child tool prints expected output, but the wrapper reports a blank, stale, or otherwise unreliable exit code.

### Cause

`Start-Process -PassThru` and wrapper patterns around it can make exit-code capture fragile, especially when process waiting, redirection, or object lifetime is handled inconsistently.

### Risk

A successful native command may become `BLOCKED` or `FAIL`, or a failed command may appear successful.

### Prevention

For commands whose native exit code is acceptance-relevant:

- prefer direct invocation when timeout/concurrency control is not required;
- otherwise use `System.Diagnostics.Process` with explicit `WaitForExit`, stdout/stderr capture, and the concrete `ExitCode` from the completed process;
- initialize the summary field to `NOT_RUN` and set it only from a completed native process.

### Recovery

If the underlying immutable stdout/stderr and native completion state can be established independently, reparse them. Otherwise rerun only the command phase, not unrelated physical actions.

## 6. ESP-IDF `activate.py --export` output misunderstood

### Symptom

A wrapper expects `activate.py --export` to print shell environment assignments, but the subsequent ESP-IDF command still runs without the intended environment.

### Cause

The supported ESP-IDF activation flow may emit or identify a generated shell script rather than returning environment assignments in the form the wrapper assumed.

### Risk

The wrong Python, toolchain, or IDF installation can be used even though activation appeared to succeed.

### Prevention

Treat activation output according to the exact ESP-IDF version in use. When activation yields a generated PowerShell script, source that script in the current shell and then verify the resolved critical tool paths and versions.

Do not infer environment activation solely from a successful activation helper exit code.

### Recovery

Regenerate the activation export if necessary, source the produced script, verify the resolved environment, and rerun only the blocked toolchain phase.

## 7. ESP-IDF activation/dependency checks exceed an arbitrary short timeout

### Symptom

A valid ESP-IDF activation or dependency check is terminated after a short fixed timeout even though it is still progressing normally.

### Cause

First-run dependency inspection, Python environment initialization, filesystem latency, or antivirus scanning can make activation materially slower than subsequent runs.

### Risk

A healthy toolchain is misclassified as unavailable and the Human Gate becomes unnecessarily blocked.

### Prevention

All waits remain bounded, but deadlines must be realistic for the operation. Use separate deadlines for environment activation, build, flash, serial observation, and short host queries rather than one universal timeout.

Capture progress when available so a slow operation can be distinguished from a hung operation.

### Recovery

Retry the same non-destructive toolchain phase with a justified bounded deadline after confirming no stale child process remains.

## 8. Windows application-alias Python selected instead of ESP-IDF Python

### Symptom

ESP-IDF activation or tooling fails unexpectedly even though `python` appears to exist on PATH.

### Cause

A Windows application alias or shim can resolve ahead of the Python interpreter intended by the ESP-IDF managed environment.

### Risk

Commands run under an unsupported interpreter, producing misleading dependency, module, or activation failures.

### Prevention

After applying the ESP-IDF environment, resolve and record the actual Python executable used for acceptance-relevant tools. Reject known launcher/alias shims when the project requires the managed ESP-IDF interpreter.

Prefer the interpreter path established by the activated ESP-IDF environment rather than an unverified global `python` command.

### Recovery

Correct interpreter resolution and rerun the blocked command phase. Do not classify the firmware or hardware as failed from the interpreter-selection failure.

## 9. `parttool.py` hides the underlying esptool error

### Symptom

An NVS or partition read fails with a Python `CalledProcessError`, while the useful esptool failure text is not visible in the wrapper's stderr.

### Cause

The partition wrapper may redirect the underlying esptool stdout/stderr into another stream or output target and expose only its own traceback.

### Risk

The operator sees only a generic wrapper exception and cannot distinguish missing serial port, ROM-mode state, permission, transport, or esptool failure.

### Prevention

When raw flash-read diagnostics are required:

- preserve the wrapped tool's complete output if available;
- if the wrapper obscures the native failure, use the authoritative lower-level esptool read command directly with the exact known partition offset and size;
- report the native exit code, bytes read, and a bounded failure tail in the final summary.

Do not replace partition offsets with guessed values. Obtain them from the exact tested partition table/build evidence.

### Recovery

Retry the read-only partition observation through the lower-level tool. Keep the original wrapper result as `BLOCKED`, not product `FAIL`.

## 10. `Get-PnpDevice -Class Ports` enumeration failure

### Symptom

PowerShell throws while enumerating the `Ports` class, so the wrapper reports that no serial device can be found even though Windows device enumeration may still be functioning.

### Cause

The class-specific PnP query can fail independently of broader PnP or CIM enumeration.

### Risk

A host-query failure is mistaken for physical device absence.

### Prevention

Use layered discovery when USB presence matters:

1. preferred PnP query;
2. broader `Get-PnpDevice -PresentOnly` filtering by stable hardware identifiers;
3. CIM `Win32_PnPEntity` fallback when the PnP cmdlet path fails.

Keep host-query failure separate from `device not enumerated`.

### Recovery

Repeat the observation through an independent enumeration API. Only conclude that the device is absent when a functioning observation path reports no matching device.

## 11. USB serial/JTAG disappears during reset or re-enumeration

### Symptom

A serial read throws, a COM port temporarily disappears, or the same interface returns after reset.

### Cause

USB serial/JTAG endpoints can be destroyed and recreated as the target resets or switches boot/application states.

### Risk

A normal reset can be misclassified as a device crash or collector failure can truncate the only boot evidence.

### Prevention

Serial collectors spanning reset must:

- tolerate expected disconnects;
- poll for re-enumeration over a bounded window;
- reopen the endpoint when it returns;
- record reconnect events and preserve ordering;
- keep collector failure distinct from product state.

### Recovery

Reconnect within the bounded window and continue the same observation session when ordering remains unambiguous. If the session boundary is lost, classify the affected observation as `BLOCKED` and start a fresh observation rather than guessing.

## 12. Pre-reset bytes concatenate with post-reset boot output

### Symptom

One decoded serial line contains a pre-reset application fragment followed immediately by the ROM banner, for example an old state token directly adjacent to `ESP-ROM`.

### Cause

Reset occurs between serial bytes before the collector has observed a line terminator.

### Risk

A parser that searches the whole decoded line can attribute the pre-reset state to the new boot session and generate a false product failure.

### Prevention

Every reset-sensitive parser must establish an explicit session boundary using the commanded action and subsequent authoritative boot marker.

When a line straddles the boundary:

- do not classify the pre-boundary prefix as post-reset evidence;
- split or discard the stale prefix for new-session classification;
- preserve the raw line separately for diagnostics.

Never classify a new boot by searching all text accumulated before and after reset as one undifferentiated log.

### Recovery

Reparse the existing raw capture using the correct session boundary. A physical rerun is unnecessary when the raw ordering is sufficient.

## 13. Historical download-mode output misclassified as the result of a later reset

### Symptom

A capture begins while the target is already in ROM Download Mode. Later in the same capture the operator performs a reset that boots normally, but the parser returns `DOWNLOAD_MODE` because `waiting for download` appeared anywhere in the capture.

### Cause

The parser uses capture-global marker presence instead of action-scoped state transitions.

### Risk

A successful normal boot is misclassified as `BLOCKED` or as an incorrect boot mode.

### Prevention

Boot-mode classification must be scoped to the boot session after the operator action under evaluation.

For reset tests:

- record an action/reset boundary;
- identify the first authoritative boot banner after that boundary;
- classify using the boot mode associated with that new banner;
- if multiple boot banners occur, preserve their ordering and base the result on the boot episode required by the test, not on an earlier historical marker.

### Recovery

Reparse the raw ordered capture. Preserve the initial ROM state as setup context, not as the reset result.

## 14. Software hard reset does not reliably exit ROM Download Mode

### Symptom

A serial/control-line `hard_reset` reports success, but the target remains in ROM Download Mode or fails to produce the expected application boot.

### Cause

Boot strapping and USB/UART control-line behavior can differ from a physical reset path; a helper named `hard_reset` does not itself prove the required strap state for the next boot.

### Risk

The firmware is blamed for never starting even though the target did not actually perform the intended normal boot.

### Prevention

When the test requires transition from ROM Download Mode to normal application boot:

- verify the post-action ROM boot code or authoritative boot-mode indicator;
- do not treat a reset-helper success marker as proof of normal boot;
- use the board's documented physical reset/boot procedure or another proven strap-control sequence when necessary.

### Recovery

Perform a bounded observation around the documented physical normal-boot action and classify that boot session independently.

## 15. Native stderr interpreted as PowerShell failure

### Symptom

A native command succeeds but PowerShell emits `NativeCommandError` because normal progress/test output was written to stderr.

### Cause

PowerShell error semantics can elevate native stderr text independently of the native process exit status, especially in merged pipelines under terminating error behavior.

### Risk

Successful test/build evidence is discarded or incorrectly marked failed.

### Prevention

Follow `.agent/HUMAN_GATE/COMMAND_RULES.md`: use the native exit code as the primary command result, capture stdout/stderr separately when practical, and do not use stderr presence alone as failure evidence.

### Recovery

If the native exit code and immutable output are already available, reclassify from those. Rerun only when the actual process completion state is unknown.

## 16. Observation commands can mutate runtime availability

### Symptom

A read-only flash/NVS operation is logically non-destructive to persistent data but resets the target or leaves it in a different runtime/boot mode, so the previously observed application state is no longer present afterward.

### Cause

Low-level flash tooling commonly enters ROM communication mode and may reset the target as part of transport setup or completion.

### Risk

Evidence obtained before and after the tool invocation is incorrectly treated as one continuous product runtime session.

### Prevention

Distinguish **persistent-data mutation** from **runtime-state mutation**.

Before a low-level diagnostic read, state whether the tool may reset or re-enumerate the device. Do not reuse post-command runtime state as continuation evidence for a pre-command application session unless continuity is explicitly proven.

### Recovery

Treat the diagnostic read as its own phase. Re-establish a new known runtime baseline before resuming product behavior observations.

## 17. Early `STOP` prevents the required Human Gate summary

### Symptom

A PowerShell block detects a prerequisite failure and exits before printing the phase summary.

### Cause

Control flow uses `return`, `throw`, or process termination before the common summary footer.

### Risk

The operator has an error message but lacks the exact phase, revision/artifact identity, native exit status, key observation, and formal disposition required for durable evidence.

### Prevention

Human Gate PowerShell commands should use one outer `& { ... }` block, initialize a result object before work begins, gate later steps with a continuation flag, and emit one common summary footer.

Even `STOP` / `BLOCKED` paths should reach that footer whenever the PowerShell host itself remains functional.

Minimum summary fields are:

- phase;
- exact tested `HEAD` or artifact identity when relevant;
- native exit code(s) or the key observation that prevented execution;
- final `PASS` / `FAIL` / `BLOCKED` / `NOT_RUN` disposition;
- concise diagnosis.

### Recovery

If the failed command produced enough immutable evidence, summarize it manually or with a parser-only command. Do not repeat a physical action solely to obtain prettier wrapper output.

## 18. Adding future entries

Add a new entry when a command/harness defect either:

- caused a false `PASS`, `FAIL`, or `BLOCKED` classification;
- caused avoidable repetition of a physical or destructive action;
- hid the native failure needed for diagnosis;
- lost the session boundary needed to interpret device behavior; or
- is likely to recur across more than one Human Gate.

Prefer general prevention rules over workstation-specific workarounds.
