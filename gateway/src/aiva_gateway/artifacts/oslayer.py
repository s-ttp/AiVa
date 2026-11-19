"""OS-level isolation for the render sandbox child (ADR-0017 hardening).

The audit hook in `artifacts.render` sees only Python-level events; native code (lxml/libxml2, or any C
extension) makes its own system calls. This layer is enforced by the kernel, so it holds for native code:

- **seccomp-bpf (required).** After `PR_SET_NO_NEW_PRIVS`, which needs no privilege, the child installs a
  filter that fails with `EPERM`: socket creation and every socket call, `execve`/`execveat`, process
  creation (`fork`, `vfork`, and `clone` without `CLONE_THREAD`; `clone3` returns `ENOSYS` so the C library
  falls back to the checked `clone`), signals to other processes (`kill`, `tkill`, `tgkill`,
  `pidfd_send_signal`), `pidfd_open`/`pidfd_getfd` (which could take over the parent's open sockets),
  `io_uring`, `ptrace`, `process_vm_readv`/`writev`, `mount`, `unshare`, `setns`. Threads keep working. A
  foreign architecture or the x32 ABI kills the process. Filters cannot be removed, and they survive into
  any child.
- **Non-dumpable parent.** `make_undumpable()` is called once in the gateway process before the first
  render (`PR_SET_DUMPABLE` 0), so a same-uid child cannot open `/proc/<parent>/mem` or attach to it.
- **Network namespace (best effort).** `unshare(CLONE_NEWUSER | CLONE_NEWNET)` gives the child an empty
  network stack where the kernel allows unprivileged user namespaces. Container runtimes usually refuse it
  (Docker's default seccomp profile, HF Spaces), so it is reported rather than required. Without it the
  child shares the gateway's network namespace: seccomp refuses new sockets and the pidfd calls that could
  reach the parent's existing ones, and the parent is non-dumpable.

x86_64 and aarch64 are supported. This module uses `ctypes`, so it runs before the audit hook is installed
(the hook refuses `ctypes` afterwards).
"""

import ctypes
import errno
import os
import platform
import struct

# prctl(2) and seccomp(2) constants (linux/prctl.h, linux/seccomp.h, linux/filter.h, linux/audit.h).
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000
_LD_W_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ_K = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE_K = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_JSET_K = 0x45  # BPF_JMP | BPF_JSET | BPF_K
_RET_K = 0x06  # BPF_RET | BPF_K
_OFFSET_NR, _OFFSET_ARCH, _OFFSET_ARG0 = 0, 4, 16  # struct seccomp_data, little endian
_CLONE_THREAD = 0x00010000
_X32_SYSCALL_BIT = 0x40000000

# Per architecture: audit arch, the clone/clone3 numbers, and the syscalls refused with EPERM.
_ARCHES: dict[str, tuple[int, int, int, dict[str, int]]] = {
    "x86_64": (
        0xC000003E,
        56,
        435,
        {
            "socket": 41,
            "connect": 42,
            "accept": 43,
            "sendto": 44,
            "recvfrom": 45,
            "sendmsg": 46,
            "recvmsg": 47,
            "bind": 49,
            "listen": 50,
            "socketpair": 53,
            "fork": 57,
            "vfork": 58,
            "execve": 59,
            "ptrace": 101,
            "mount": 165,
            "kill": 62,
            "tkill": 200,
            "tgkill": 234,
            "pidfd_send_signal": 424,
            "pidfd_open": 434,
            "pidfd_getfd": 438,
            "unshare": 272,
            "accept4": 288,
            "recvmmsg": 299,
            "sendmmsg": 307,
            "setns": 308,
            "process_vm_readv": 310,
            "process_vm_writev": 311,
            "execveat": 322,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
        },
    ),
    "aarch64": (
        0xC00000B7,
        220,
        435,
        {
            "mount": 40,
            "kill": 129,
            "tkill": 130,
            "tgkill": 131,
            "pidfd_send_signal": 424,
            "pidfd_open": 434,
            "pidfd_getfd": 438,
            "unshare": 97,
            "ptrace": 117,
            "socket": 198,
            "socketpair": 199,
            "bind": 200,
            "listen": 201,
            "accept": 202,
            "connect": 203,
            "sendto": 206,
            "recvfrom": 207,
            "sendmsg": 211,
            "recvmsg": 212,
            "execve": 221,
            "accept4": 242,
            "recvmmsg": 243,
            "setns": 268,
            "sendmmsg": 269,
            "process_vm_readv": 270,
            "process_vm_writev": 271,
            "execveat": 281,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
        },
    ),
}


class IsolationError(OSError):
    """The required OS layer could not be installed: the sandbox must not run."""


def _stmt(code: int, k: int) -> tuple[int, int, int, int]:
    return (code, 0, 0, k)


def _jump(code: int, k: int, jt: int, jf: int) -> tuple[int, int, int, int]:
    return (code, jt, jf, k)


def filter_program(machine: str) -> list[tuple[int, int, int, int]]:
    """The BPF program as (code, jt, jf, k) instructions. Exposed for tests."""
    if machine not in _ARCHES:
        raise IsolationError(errno.ENOSYS, f"no seccomp filter for architecture {machine!r}")
    audit_arch, clone_nr, clone3_nr, denied = _ARCHES[machine]
    eperm = _RET_ERRNO | errno.EPERM
    program = [
        _stmt(_LD_W_ABS, _OFFSET_ARCH),
        _jump(_JEQ_K, audit_arch, 1, 0),
        _stmt(_RET_K, _RET_KILL_PROCESS),
        _stmt(_LD_W_ABS, _OFFSET_NR),
    ]
    if machine == "x86_64":  # x32 ABI numbers would bypass the table below
        program += [_jump(_JGE_K, _X32_SYSCALL_BIT, 0, 1), _stmt(_RET_K, _RET_KILL_PROCESS)]
    for number in sorted(set(denied.values())):
        program += [_jump(_JEQ_K, number, 0, 1), _stmt(_RET_K, eperm)]
    program += [
        _jump(_JEQ_K, clone3_nr, 0, 1),
        _stmt(_RET_K, _RET_ERRNO | errno.ENOSYS),
        # clone: threads only. A clears to the low half of the flags argument for this branch only.
        _jump(_JEQ_K, clone_nr, 0, 4),
        _stmt(_LD_W_ABS, _OFFSET_ARG0),
        _jump(_JSET_K, _CLONE_THREAD, 1, 0),
        _stmt(_RET_K, eperm),
        _stmt(_RET_K, _RET_ALLOW),
        _stmt(_RET_K, _RET_ALLOW),
    ]
    return program


def install_seccomp() -> None:
    """Install the filter on the calling thread (the child is single-threaded). Raises on failure."""
    program = filter_program(platform.machine())
    raw = b"".join(struct.pack("=HBBI", *instruction) for instruction in program)
    buffer = ctypes.create_string_buffer(raw, len(raw))

    class _SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    fprog = _SockFprog(len(program), ctypes.cast(buffer, ctypes.c_void_p))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise IsolationError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.addressof(fprog), 0, 0) != 0:
        raise IsolationError(ctypes.get_errno(), "seccomp filter not installed")


def make_undumpable() -> None:
    """In the gateway process, before it starts a render child: no same-uid process can then open its
    `/proc/<pid>/mem`, attach to it or take its file descriptors. Raises if the kernel refuses."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise IsolationError(ctypes.get_errno(), "PR_SET_DUMPABLE failed")


def enter_network_namespace() -> str:
    """Best effort: an empty network namespace. Returns `isolated` or why it is unavailable."""
    try:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
    except OSError as exc:
        return f"unavailable ({errno.errorcode.get(exc.errno or 0, exc.errno)})"
    return "isolated"


def isolate() -> dict[str, str]:
    """Network namespace if possible, then the required seccomp filter. Returns what is in force."""
    namespace = enter_network_namespace()  # before seccomp, which refuses unshare
    install_seccomp()
    return {"seccomp": "active", "network_namespace": namespace}
