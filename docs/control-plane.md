# Programmatic Control

A `ControlClient` reaches the monitor from inside the process being monitored.
It can mark the trace, and it can suspend gcmon's polling of that process.

## Import and Setup

```python
from gcmon.control.control_client import ControlClient

# The address comes from GCMON_CONTROL_ADDRESS.
client = ControlClient()
```

## Start/Stop Monitoring

```python
client.stop_monitoring()
# ... setup code ...
client.start_monitoring()
```

Stopping stops gcmon reading that pid. The target keeps collecting. CPython's
buffer holds the newest few records
([How gcmon reads a process](monitoring.md)), so a gap wider than a few
collections overwrites them, and the first poll after `start_monitoring` reads
counters spanning the whole gap. The gap becomes one loss window
([Trace Formats](formats.md)): gcmon counts the collections and has lost the
records that described them.

Both calls are events in the trace, so a stopped stretch reads as deliberate
rather than blank.

## Context Manager

`pause_monitoring` sends the pair around a block:

```python
with client.pause_monitoring():
    # ... code gcmon does not poll through ...
```

## Custom Instant Messages

```python
client.instant_msg("request_start")
# ... handle request ...
client.instant_msg("request_end")
```

Each message becomes an instant on the process's track, beside its GC
activity.

### Sending an instant after the fact

`instant_msg` stamps the message when it is sent. Pass `ts` to say when it
happened instead:

```python
started = time.monotonic_ns()
# ... the work you want bracketed ...
stopped = time.monotonic_ns()

client.instant_msg("work_start", ts=started)
client.instant_msg("work_end", ts=stopped)
```

The measured code pays two clock reads and nothing else; the send happens
outside it.

Use `time.monotonic_ns`: a GC record is stamped from the same clock, so your
instants sit on the same timeline as the records.

## When to Use

Mark a region to record what your application was doing, then decide what to
count when you read the trace. Everything outside the marks stays in the
trace.

Stop polling to spend less of gcmon's time on a process you do not care about:
a long idle stretch, or one whose GC you have already characterised. gcmon
reads from outside, so the target was never paying for it, and the saving is
gcmon's own.

## Prerequisites

The client needs the monitor's address, and where that comes from depends on
the subcommand.

`gcmon run` starts the target itself, so it sets `GCMON_CONTROL_ADDRESS` in
that process and `ControlClient()` reads it with no argument.

`gcmon monitor` attaches to a process already running, whose environment was
fixed before gcmon reached it, so it sets nothing. Name the control plane
instead and build the address in the target:

```python
import sys

NAME = "my-app"  # gcmon monitor <pid> --control-name my-app
address = rf"\\.\pipe\gcmon-{NAME}" if sys.platform == "win32" else f"/tmp/gcmon-{NAME}"
client = ControlClient(address)
```

Without `--control-name` the address is random and the snippet has nothing to
build. See [`--control-name`](cli.md#--control-name).

With no address the client never connects, and every send is logged at debug
level and dropped.
