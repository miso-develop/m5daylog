# Human Gate Execution Rules

version: 1

## 1. Scope

These rules apply whenever acceptance depends on real hardware, a real operating system / host integration path, physical user action, power / cable behavior, manual observation, or any other condition not proven by automated tests alone.

## 2. Exact revision and prerequisites

Before starting a Human Gate:

1. Resolve the exact Issue, PR, and full commit revision under test.
2. Confirm that the revision is still current for the workflow transition that requested the Human Gate.
3. Confirm prerequisite automated checks required by the Issue / PR have passed or explicitly record which prerequisite remains intentionally pending.
4. Confirm the build / artifact used for the physical test was produced from that exact revision.
5. Do not continue a Human Gate after the tested revision changes. Start a new evidence session for the new revision.

Never infer that an earlier physical result applies to a later commit unless the approved acceptance plan explicitly permits reuse and the relevant behavior is proven unchanged.

## 3. Evidence session model

Each Human Gate run must use one bounded evidence session.

The session record should identify, using public-safe values only:

- Issue / PR;
- exact revision;
- acceptance criteria or matrix rows under test;
- automated prerequisite status;
- test phase / cycle number;
- command result where applicable;
- physical observation;
- `PASS` / `FAIL` / `BLOCKED` / `NOT_RUN` disposition;
- reason for any aborted cycle.

Local evidence files may contain richer machine-specific diagnostics, but committed durable evidence must be sanitized according to `.agent/HUMAN_GATE/README.md`.

## 4. Baseline before acceptance cycles

Before counting any acceptance cycle, establish a known baseline.

A baseline must verify the preconditions that make the cycle meaningful, for example:

- correct firmware / application revision is running;
- expected storage / network / USB / process state is present;
- required media or fixture is available;
- the device is not already in an unresolved lifecycle state from a previous attempt;
- the host can observe the interface required by the test;
- no previous command-wrapper failure is being mistaken for product state.

If the baseline fails, do not count the cycle. Diagnose or hand the failure back to the owning Role.

## 5. One phase, one purpose

Keep Human Gate phases small and observable.

Prefer this order:

1. preflight / observation;
2. build verification;
3. flash / install / setup;
4. baseline observation;
5. one physical acceptance action;
6. immediate observation and evidence capture;
7. integrity / recovery check;
8. next cycle.

Do not combine unrelated mutations and acceptance actions into one large command when failure of an intermediate step would make the final state ambiguous.

## 6. Observation before mutation

When a state is uncertain, observe before changing it.

Examples:

- enumerate interfaces before resetting a device;
- inspect the current lifecycle state before erasing persistent storage;
- inspect the toolchain resolution before rebuilding;
- capture logs before applying a recovery action.

Destructive or state-resetting actions may be used only when necessary and must not erase evidence needed to diagnose the failure that triggered them.

## 7. Physical action instructions

For every physical operator step:

- specify exactly what to press, connect, disconnect, suspend, resume, or observe;
- specify what must NOT be pressed or changed during the step when relevant;
- state whether the action counts toward the acceptance matrix;
- use bounded wait periods and observable completion conditions;
- do not ask the operator to perform multiple materially different actions before an observation point.

If timing matters, define the timing window explicitly rather than relying on phrases such as "briefly" or "after a while".

## 8. Do not conflate harness failures with product failures

Classify failures before changing product code.

### Tooling / harness failure

Examples:

- shell interpreted normal stderr output as an error;
- wrapper process hung after the underlying test already completed;
- wrong tool version was resolved from PATH;
- a log collector stopped when a serial device re-enumerated;
- the evidence parser failed even though the underlying command completed.

A tooling or harness failure that invalidates an attempted Human Gate observation is `BLOCKED`, not automatically product `FAIL`. If the required test phase was never attempted, use `NOT_RUN` instead.

### Product failure

A product failure requires product behavior that contradicts an Acceptance Criterion under valid test preconditions and with a valid observation path.

When possible, capture the first observable product error before recovery or reset changes the state.

## 9. Counting cycles

A cycle may count only when:

- the exact intended revision was under test;
- baseline preconditions were satisfied;
- the required physical sequence was actually performed;
- the required observation was captured;
- the harness did not invalidate the observation;
- the result can be mapped to the intended Acceptance Criterion / matrix row.

Aborted setup attempts, command-wrapper failures, firmware-flash attempts, diagnostic resets, and exploratory reproductions do not count unless the approved matrix explicitly says they do.

Do not double-count one physical action across multiple Acceptance Criteria unless the approved test plan explicitly allows combined evidence and each criterion's observation is independently present.

## 10. Fail closed on ambiguous physical state

If the product specification requires fail-closed behavior, ambiguous transport / cable / suspend / reset signals must not be reinterpreted as proof of a successful release, ownership transfer, synchronization, persistence, or recovery.

Use the authoritative product event or observable completion condition defined by the specification.

## 11. Revision changes after rework

When Human Gate finds an implementation defect:

1. stop the affected matrix;
2. persist the failure and diagnostic evidence durably;
3. hand the work back to the owning Role;
4. after rework, verify the new exact revision and prerequisite checks;
5. start a new Human Gate evidence session.

Do not continue counting cycles from the failed revision unless the approved specification explicitly defines reusable unaffected evidence.

## 12. Final disposition

Human Gate uses exactly these final dispositions for the stated test scope:

- `PASS`: all required physical/manual observations for the stated scope were validly obtained and satisfied the Acceptance Criteria;
- `FAIL`: valid test preconditions and a valid observation path were present, and the observed product behavior violated an Acceptance Criterion. This includes a bounded deadline expiring when the required product state or completion event was not observed even though the observation mechanism remained valid for the full required window;
- `BLOCKED`: the test was attempted, but tooling, harness, environment, dependency, unavailable hardware, or loss of the required observation path prevented a valid acceptance observation;
- `NOT_RUN`: the required test or phase has not yet been attempted.

`NOT_OBSERVED` is not a Human Gate disposition. An inability to obtain a valid observation after an attempt maps to `BLOCKED`; an unattempted test maps to `NOT_RUN`.

Never convert `BLOCKED` or `NOT_RUN` into `PASS` based on automated tests alone when physical evidence is required.
