# 0042: Name the process-session interface for its role, and trim it to it

- **Status:** Not started
- **Kind:** feature (cleanup)
- **Effort:** S
- **Origin:** code structure review of `src/gcmon`, 2026-08-15
- **Respects:** [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md)
  (a monitored pid's lifetime),
  [ADR-0012](../docs/adr/0012-trace-output-formats.md); neither is affected;
  listed because both name the monitored process

## 1. Problem statement

The abstraction over "the process gcmon is monitoring" is named for something
it is not, and the names cost a reader more than the abstraction saves.
`ProcessFactory` has `start`, `returncode`, `wait` and the context-manager
pair: it is a session that owns a process for the length of a run, and it is a
factory only in that one of its five members returns something.
`ProcessRunnerFactory` is then a factory *of* that, so both commands write the
same three-line closure to satisfy it.

The two adapters behind the seam do not have the same shape, which is what
makes the naming bite. Attaching to an existing pid is served by a class that
subclasses the `TargetProcess` protocol *and* satisfies `ProcessFactory`,
returning itself from `start`. Spawning a child is served by a class that
satisfies `ProcessFactory`, is not a `TargetProcess`, and returns a separate
one. A reader has to hold both arrangements to know what
`factory(control_address).start()` gives them.

The interface is also missing a fact its caller depends on: an attached
target's `returncode` is permanently `None` and its `wait` does nothing, so
`gcmon monitor <pid>` always exits 0 whatever the target does. That is correct
(gcmon does not own a process it attached to) but it is discoverable only by
reading both adapters.

## 2. Solution

Nothing changes for an operator: the same two ways to name a target, the same
exit codes, the same output. What changes is that the seam is named for the
role it fills, one adapter is not two roles at once, and the interface states
what a caller must know rather than leaving it to be inferred from the
implementations.

## 3. User stories

1. As a maintainer reading the monitoring entry point, I want the type of the
   thing being started to be named for what it is, so that I am not looking
   for a factory that is not there.
2. As a maintainer adding a third way to name a target, I want one interface
   with one shape, so that I do not have to decide whether my class should
   also be its own target.
3. As a maintainer, I want to know from the interface that an attached target
   reports no exit code, so that I do not write a caller that waits on one.
4. As a maintainer of the `run` and `monitor` commands, I want to hand the
   monitoring entry point the thing it needs directly, so that both commands
   do not carry the same closure to satisfy a name.
5. As a maintainer, I want the child-process adapter to expose the interface
   and not eight extra members, so that a caller cannot come to depend on
   something the other adapter does not have.
6. As an operator running `gcmon monitor <pid>`, I want the exit code to stay
   0 regardless of what the attached process does, so that this refactor does
   not change my CI.
7. As an operator running `gcmon run`, I want the target's exit code to keep
   propagating, so that a failing script still fails the command.

## 4. Implementation decisions

**4.1: `ProcessFactory` becomes a session.** Renamed to say so, with its five
members unchanged: start it, wait on it, read its return code, use it as a
context manager. Its docstring carries what the interface actually requires of
a caller, including that `returncode` may be `None` forever, and what that
means for the two adapters.

**4.2: `ProcessRunnerFactory` becomes what both call sites already write.** It
is `Callable[[str], <session>]`, the control address being the one thing the
session needs from the monitoring setup. Both commands stop defining an
identically-shaped local closure to satisfy a named protocol and hand over a
partially-applied constructor instead.

**4.3: The attach adapter stops being its own target.** Today it subclasses
the `TargetProcess` protocol and returns itself from `start`, so one class
fills two roles. Give it the same shape as the spawn adapter: a session that
starts and returns a target. The target for an attached pid is a two-line
object holding the pid, which the child-process path already has an equivalent
of. Two adapters, one shape.

**4.4: `TargetProcess`, `ProcessFactory` and `WaitPolicy` lose
`runtime_checkable`.** Nothing in `src/` or `tests/` calls `isinstance`
against any of them; the only protocol-adjacent `isinstance` in the suite is
against the two concrete runner classes. `runtime_checkable` on a protocol
nobody checks at runtime is a decoration that suggests a capability the code
does not use, and it silently weakens the check it appears to offer, since it
tests for method presence and not signatures.

**4.5: The child adapter is trimmed to the session interface.** It exposes
`process`, `pid`, `is_running` and `close` beyond it. `close` is an alias for
`terminate`, and the other three have no caller in the monitoring flow, only
its own tests. Trimming them makes the two adapters substitutable in fact and
not only in principle. Where a test genuinely needs the subprocess handle, it
should reach for it deliberately rather than through a public property that
exists for its benefit.

**Rejected: merge `TargetProcess` into the session.** They are different
lifetimes. A session exists before there is a process and after it exits; a
target is a pid that currently exists and is what the monitor holds.
Collapsing them is what produced the attach adapter's double role.

**Rejected: rename only, and leave the shapes alone.** The names are the
smaller half. A reader's actual difficulty is that `start()` returns `self` on
one path and a different object on the other, and no name fixes that.

**Open, to settle when picked up:** whether the session's `wait` keeps its
`timeout` parameter. Its one caller passes 2.0 seconds with a comment
explaining why, and the attach adapter ignores it entirely. This was to be
settled by whether the monitoring entry point still needs a bounded wait after
0038 reorganized shutdown. 0038 has since landed and left that wait alone: the
loop keeps only the clock and the stop event, and the 2.0-second wait after it
still guards reading a return code from a process mid-finalization, so the
question is now answerable from the code rather than blocked on anything.

## 5. Seams and testing decisions

- **Seam:** `tests/monitoring/test_loop_runner.py`, at the monitoring
  entry point, the highest seam that can observe the change, because what
  these types are *for* is being handed to that function and started.
  `tests/test_child_process_runner.py` and
  `tests/monitoring/test_monitor_cmd.py` cover the two adapters at their own
  level.
- **New seam needed:** none.
- **What makes a good test here:** exercise both adapters through the same
  entry point and assert the observable difference is only the one that should
  exist: the exit code. A test asserting that a class implements a protocol
  proves the type checker ran; assert behaviour instead.
- **Prior art:** `tests/monitoring/test_loop_runner.py` for driving the
  entry point with a substituted session; `tests/monitoring/conftest.py` for
  the existing fakes; `tests/test_child_process_runner.py` for the spawn
  adapter's lifecycle.
- **Cases:**
  1. `gcmon run` propagates a failing script's exit code, as today.
  2. `gcmon monitor <pid>` exits 0 whatever the attached process does, as
     today, the property section 1 says is undocumented, pinned by a test so
     that documenting it does not change it.
  3. Both adapters satisfy the session interface with the same shape:
     `start()` returns a target that is not the session itself.
  4. Regression guard: the full suite passes; only imports, names and the
     trimmed members change.

## 6. Out of scope

- How a target is discovered or spawned. The subprocess construction, the
  environment injection and the termination escalation are untouched.
- The termination utilities, which are already a separate module with one job.
- 0038's reorganization of the monitor and the loop, which has landed. This
  spec touches what is handed *to* the monitoring entry point; that one
  touched what happens inside the loop. They were independent in either order,
  and nothing here depends on the result.
- Adding a third way to name a target, such as attaching by process name.
- The control address itself, which stays the one argument the session factory
  takes.

## 7. Further notes

This is the smallest spec in the set and the one most likely to be objected to
as churn. The argument for it is section 4.3: two adapters at one seam that do
not have the same shape is not a naming problem, and the naming is what makes
it hard to see. If only one of the five items is taken, take that one.
