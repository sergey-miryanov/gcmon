# Remote reads, per platform

What `_remote_debugging` holds while gcmon is attached to a process, what each
read goes through, and which platform can serve a read from a recycled pid.
[ADR-0020](../adr/0020-attach-to-a-process-once.md) is the decision this
backs.

Everything here lives in `Python/remote_debug.h`, read at CPython 3.15.0b4.
The three read paths are branches of one function,
`_Py_RemoteDebug_ReadRemoteMemory`.

| | Windows | macOS | Linux |
|---|---|---|---|
| An attachment holds | `HANDLE hProcess` | `mach_port_t task` | `int memfd`, normally unused |
| A read addresses | the handle | the task | **the pid** |
| A recycled pid | cannot happen while attached | read fails, gcmon recovers | read succeeds against the stranger |
| Dropping it releases | the handle, and the pid with it | **nothing** | the fd, if one was opened |

The rest of this page is where each row comes from.

## What an attachment holds

`proc_handle_t` keeps the pid everywhere, and one field beside it that differs
by platform.

| Platform | Field | Taken by |
|---|---|---|
| Windows | `HANDLE hProcess` | `OpenProcess` at attach |
| macOS | `mach_port_t task` | `pid_to_task`, which calls `task_for_pid` |
| Linux | `int memfd` | `open_proc_mem_fd` |

On Linux that second field is normally unused. `open_proc_mem_fd` runs only as
a fallback, when `process_vm_readv` is unavailable or when something is
written, so an ordinary Linux attachment holds a pid and nothing else.

## What dropping an attachment releases

`GCMonitor_dealloc` reaches `_Py_RemoteDebug_CleanupProcHandle` through
`cleanup_runtime_offsets`, so letting go of the last reference is what returns
the resource. That function has an arm for Windows and an arm for Linux and
none for macOS, so the task port a macOS attachment took is never given back.
On Windows the same call is what releases the pid, since the pin lasts exactly
as long as the handle.

[Spec 0054](../../specs/0054-macos-attachment-leaks-a-mach-task-port.md)
covers the macOS leak.

## What a read goes through

| Platform | Call | Addresses |
|---|---|---|
| Windows | `ReadProcessMemory(hProcess, ...)` | the handle |
| macOS | `mach_vm_read_overwrite(task, ...)` | the task port |
| Linux | `process_vm_readv(pid, ...)`, plain and batched | **the pid, on every call** |

## Which OS holds a pid after the process dies

| OS | Holds it for | Until |
|---|---|---|
| Windows | anyone holding a handle | the process has exited *and* every handle to it is closed |
| Linux | the parent | the parent waits for it |
| macOS | the parent | the parent waits for it |

On Windows an identifier is valid until the process has exited and all handles
to it are closed ([Process Handles and Identifiers][win32-pid]). gcmon holds a
handle for as long as it is attached, so the system cannot reissue that pid in
the meantime.

POSIX gives the other two in two pieces, and the pieces are circular on their
own. [4.17 Process ID Reuse][posix-reuse] says a process ID shall not be
reused until the process lifetime ends; 3.285 defines that lifetime as ending
when the ID is returned to the system, and says nothing about what returns it.
The parent returns it: a terminated child stays a zombie holding its pid until
it is waited for ([`wait(2)`][linux-wait]). Darwin follows the same rule.
Holding a task port or reading by pid reserves nothing on either.

POSIX reserves one more number, and it is the wrong one for gcmon.
[4.17][posix-reuse] also holds a pid that is some live process group's ID
until that group empties, and 3.282 makes a group's ID the pid of whichever
process created it. A `fork` child inherits its parent's group rather than
leading one, so its pid is nobody's group ID and the clause never reaches it.
It covers a target leading its own group, under `setsid()`, whose children
outlive it, and that is the pid gcmon worries about least.

That leaves gcmon holding a pid on the POSIX platforms in one case only, and
not by attaching. Under `gcmon run` the target is gcmon's own child, so it
stays a zombie until the run tears down and waits for it. Children of the
target, and any pid given to `gcmon monitor`, are reaped by somebody else and
can be reissued mid-run.

Linux offers a mechanism that would close this,
[`pidfd_open(2)`][linux-pidfd], which refers to a process in a way PID
recycling cannot invalidate. `_remote_debugging` does not use it: its Linux
reads take the raw pid.

## Which platform can read a recycled pid

Linux: its reads name the pid, and nothing holds that pid once the parent has
reaped it, so a stale attachment reads whatever holds the pid next.

The other two arrive somewhere safe by different routes. Windows does not
reuse the pid while gcmon is attached, so there is no successor to read. macOS
reuses it as freely as Linux, but a read addresses the task, and a dead task's
port is not rebound, so the successor is out of reach.

[Spec 0052](../../specs/0052-a-recycled-pid-can-be-read-through-a-stale-attachment.md)
covers the Linux exposure.

None of that covers the moment before the first attach. gcmon holds nothing
between the child listing naming a pid and attaching to it, so a recycle
inside that window leaves gcmon monitoring a process the listing did not mean.
It reads that process correctly and from a clean slate, so nothing is
fabricated and no loss window is drawn: the records belong to the pid, not to
the process that held it when the listing ran.

## What a failed read raises

| Platform | Condition | Type |
|---|---|---|
| Windows | target gone, by `is_process_alive` | `ProcessLookupError` |
| Windows | any other failure | `OSError` from the Windows error |
| macOS | `KERN_INVALID_ARGUMENT` and `task_info` says the task is gone | `ProcessLookupError` |
| macOS | `KERN_NO_SPACE` or `KERN_MEMORY_ERROR` | `ProcessLookupError` |
| macOS | `KERN_PROTECTION_FAILURE` | `PermissionError` |
| macOS | `KERN_INVALID_ARGUMENT` while `task_info` says the task is valid | `ValueError` |
| macOS | a short read | `OSError` |
| Linux | `ESRCH` | `ProcessLookupError` |
| Linux | any other `errno`, or a zero-length read | `OSError` |

The `ProcessLookupError` and `PermissionError` rows mean the target is gone or
closed to gcmon; the rest mean the read was wrong. ADR-0020 decides what gcmon
does with each.

`debug=True` lets an outer layer replace any of these with a `RuntimeError`
carrying the original as `__cause__`. A dead target on Windows arrives that
way, measured. `tests/monitoring/test_events_reader.py` records what the other
two do, on every CI run across `ubuntu-latest`, `macos-latest` and
`windows-latest`.

[win32-pid]: https://learn.microsoft.com/en-us/windows/win32/procthread/process-handles-and-identifiers
[posix-reuse]: https://pubs.opengroup.org/onlinepubs/9799919799/basedefs/V1_chap04.html#tag_04_17
[linux-wait]: https://man7.org/linux/man-pages/man2/wait.2.html
[linux-pidfd]: https://man7.org/linux/man-pages/man2/pidfd_open.2.html
