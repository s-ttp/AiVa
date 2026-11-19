"""Sandbox entrypoint: open a generated DOCX/PPTX and report its structure and text as JSON.

Run only by `artifacts.sandbox` as `python -I -m aiva_gateway.artifacts.render <docx|pptx>`, with the file
on stdin, an empty environment and a timeout. Before it reads stdin or imports any document code, `main`
sets resource limits and installs an audit hook that refuses network, process (subprocess, system, exec,
spawn, fork), file-write, file-metadata and `ctypes` events, and imports of `ctypes` and `_ctypes`. So a
malformed file cannot reach the network or the disk through Python even if the parser misbehaves. Native
code (lxml, libxml2) does its own I/O and is not seen by the hook: the hash check in `artifacts.sandbox` is
the primary control, and the OS layer (`artifacts.oslayer`, installed first) holds for native code too: a
seccomp filter refuses sockets, exec and process creation at the kernel, plus a network namespace where the
kernel allows one. If the seccomp filter cannot be installed the child renders nothing. `--probe [port]`
reports what the sandbox blocks, including native-level attempts made before the audit hook exists.

This module imports only the standard library, the two document libraries and `artifacts.oslayer`.
"""

import json
import os
import resource
import sys
from collections.abc import Callable
from typing import Any

MEMORY_BYTES = 1024 * 1024 * 1024
CPU_SECONDS = 15
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_BLOCKED_PREFIXES = (
    "socket.",
    "subprocess.",
    "os.system",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.fork",
    "os.forkpty",
    "os.kill",
    "urllib.Request",
    "http.client.connect",
    "ftplib.",
    "smtplib.",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.mkdir",
    "os.truncate",
    "os.symlink",
    "os.link",
    "os.chmod",
    "os.lchmod",
    "os.chown",
    "os.lchown",
    "os.utime",
    "os.chflags",
    "os.lchflags",
    "os.setxattr",
    "os.removexattr",
    "shutil.",
    "ctypes.",
)


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


_BLOCKED_IMPORTS = frozenset({"ctypes", "_ctypes"})


def _deny(event: str, args: tuple[Any, ...]) -> None:
    if event.startswith(_BLOCKED_PREFIXES):
        raise PermissionError(f"blocked in the artifact sandbox: {event}")
    if event == "import" and args and args[0] in _BLOCKED_IMPORTS:
        raise PermissionError(f"blocked in the artifact sandbox: import {args[0]}")
    if event == "open" and len(args) > 2:
        # `open()` passes a mode string; `os.open` passes mode None and carries the intent in its flags.
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS
        ):
            raise PermissionError("blocked in the artifact sandbox: file write")


def _probe() -> dict[str, str]:
    """Each attempt must be refused; reports what happened."""
    import importlib
    import socket
    import subprocess

    def native_call() -> object:
        import ctypes  # refused as an import, and loading a library is refused too (`ctypes.dlopen`)

        return ctypes.CDLL(None).getpid()

    target = "aiva-sandbox-probe"  # in the throwaway working directory
    results: dict[str, str] = {}
    attempts: dict[str, Callable[[], object]] = {
        "network": lambda: socket.create_connection(("127.0.0.1", 9), timeout=0.5),
        "socket": lambda: socket.socket(),
        "dns": lambda: socket.getaddrinfo("example.com", 443),
        "subprocess": lambda: subprocess.Popen(["true"]),  # noqa: S607 - must be refused
        "os_system": lambda: os.system("true"),  # noqa: S605, S607 - must be refused
        "os_exec": lambda: os.execv("/bin/true", ["true"]),  # noqa: S606 - must be refused
        "os_spawn": lambda: os.posix_spawn("/bin/true", ["true"], {}),
        "os_fork": os.fork,
        "file_write": lambda: open(target, "w"),  # noqa: SIM115
        "file_create": lambda: os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
        "truncate": lambda: os.truncate(target, 0),
        "symlink": lambda: os.symlink("/etc/hostname", target),
        "chmod": lambda: os.chmod(".", 0o777),  # noqa: S103 - must be refused
        "ctypes": native_call,
        "import_ctypes": lambda: importlib.import_module("ctypes"),
        "import__ctypes": lambda: importlib.import_module("_ctypes"),
    }
    for name, attempt in attempts.items():
        try:
            attempt()
            results[name] = "allowed"
        except (PermissionError, ImportError):  # ImportError: ctypes is unimportable after isolation
            results[name] = "blocked"
        except OSError as exc:
            results[name] = f"failed: {type(exc).__name__}"
    return results


def _docx(data: bytes) -> dict[str, Any]:
    import io

    from docx import Document

    document = Document(io.BytesIO(data))
    pages: list[dict[str, Any]] = []
    current: dict[str, Any] = {"index": 1, "kind": "section", "title": None, "blocks": []}
    stats = {"headings": 0, "paragraphs": 0, "tables": 0, "images": 0, "text_frames": 0}
    body = document.element.body
    tables = iter(document.tables)
    paragraphs = iter(document.paragraphs)
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = next(paragraphs)
            text = paragraph.text.strip()
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            stats["images"] += len(child.findall(".//{*}drawing")) + len(child.findall(".//{*}pict"))
            if not text:
                continue
            stats["text_frames"] += 1
            if style == "Title" or style.startswith("Heading"):
                stats["headings"] += 1
                level = 0 if style == "Title" else int(style.rsplit(" ", 1)[-1] or 1)
                if level <= 1 and (current["blocks"] or current["title"]):
                    pages.append(current)
                    current = {"index": len(pages) + 1, "kind": "section", "title": None, "blocks": []}
                if level <= 1:
                    current["title"] = text
                current["blocks"].append({"kind": "heading", "level": level, "text": text})
            elif style.startswith("List"):
                stats["paragraphs"] += 1
                level = int(style.rsplit(" ", 1)[-1]) - 1 if style[-1].isdigit() else 0
                current["blocks"].append({"kind": "bullet", "level": level, "text": text})
            else:
                stats["paragraphs"] += 1
                current["blocks"].append({"kind": "paragraph", "text": text})
        elif tag == "tbl":
            table = next(tables)
            stats["tables"] += 1
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            current["blocks"].append({"kind": "table", "rows": rows})
    pages.append(current)
    settings = document.settings.element
    protected = bool(settings.findall(".//{*}documentProtection"))
    return {"pages": pages, "stats": stats, "protected": protected, "title": document.core_properties.title}


def _pptx(data: bytes) -> dict[str, Any]:
    import io

    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    presentation = Presentation(io.BytesIO(data))
    pages: list[dict[str, Any]] = []
    stats = {"headings": 0, "paragraphs": 0, "tables": 0, "images": 0, "text_frames": 0}
    for index, slide in enumerate(presentation.slides, start=1):
        title_shape = slide.shapes.title
        title = title_shape.text_frame.text.strip() if title_shape is not None else None
        blocks: list[dict[str, Any]] = []
        if title:
            stats["headings"] += 1
            blocks.append({"kind": "heading", "level": 1, "text": title})
        for shape in slide.shapes:
            if (
                shape.shape_type == MSO_SHAPE_TYPE.PICTURE
                or shape.shape_type == MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT
            ):
                stats["images"] += 1
            if shape.has_table:
                stats["tables"] += 1
                rows = [[cell.text.strip() for cell in row.cells] for row in shape.table.rows]
                blocks.append({"kind": "table", "rows": rows})
            elif shape.has_text_frame and shape is not title_shape:
                stats["text_frames"] += 1
                for paragraph in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in paragraph.runs).strip()
                    if text:
                        stats["paragraphs"] += 1
                        blocks.append({"kind": "bullet", "level": paragraph.level, "text": text})
        pages.append({"index": index, "kind": "slide", "title": title, "blocks": blocks})
    return {"pages": pages, "stats": stats, "protected": False, "title": presentation.core_properties.title}


def _probe_native(port: int, parent_fd: int, syscall: Callable[..., int]) -> dict[str, str]:
    """Attempts made below the audit hook (it is not installed yet): only the OS layer can refuse them."""
    import _socket

    parent = os.getppid()

    def connect() -> None:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            sock.connect(("127.0.0.1", port))
        finally:
            sock.close()

    def fork() -> None:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)

    def raw(number: int, *args: int) -> int:
        result = syscall(number, *args)
        if result == -1:
            code = _last_errno()
            raise OSError(code, os.strerror(code))  # EPERM becomes PermissionError
        return result

    def pidfd_getfd_and_write() -> None:
        # Take the gateway's open descriptor (a connected socket in the test) and write through it.
        pidfd = os.pidfd_open(parent) if hasattr(os, "pidfd_open") else 0
        stolen = raw(_PIDFD_GETFD, pidfd, parent_fd, 0)
        os.write(stolen, b"leaked from the render sandbox")

    def parent_memory() -> None:
        with open(f"/proc/{parent}/mem", "r+b", buffering=0):
            pass

    results: dict[str, str] = {}
    attempts: dict[str, Callable[[], object]] = {
        "native_connect": connect,
        "native_fork": fork,
        "native_signal_parent": lambda: os.kill(parent, 0),
        "native_pidfd_open": lambda: os.pidfd_open(parent),
        "native_pidfd_getfd": lambda: raw(_PIDFD_GETFD, 0, max(parent_fd, 0), 0),
        "native_steal_parent_socket": pidfd_getfd_and_write,
        "native_parent_memory": parent_memory,
        # If exec were allowed this process would become /bin/true and the probe would print nothing.
        "native_exec": lambda: os.execv("/bin/true", ["true"]),  # noqa: S606 - must be refused
    }
    for name, attempt in attempts.items():
        try:
            attempt()
            results[name] = "allowed"
        except PermissionError:
            results[name] = "blocked"
        except OSError as exc:
            results[name] = f"failed: {type(exc).__name__}"
    return results


_PIDFD_GETFD = 438  # same number on x86_64 and aarch64
_errno_reader: Callable[[], int] | None = None


def _last_errno() -> int:
    return _errno_reader() if _errno_reader else 0


def _isolate(probe: bool) -> tuple[dict[str, str], Callable[..., int] | None]:
    global _errno_reader
    from aiva_gateway.artifacts import oslayer

    syscall: Callable[..., int] | None = None
    if probe:  # a raw syscall for the probe's native attempts, taken before ctypes is made unimportable
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        syscall, _errno_reader = libc.syscall, ctypes.get_errno
    layers = oslayer.isolate()
    # ctypes was needed to install the filter. Re-importing a loaded extension raises no `import` audit
    # event, so the hook alone cannot refuse it now: a None entry makes every later import of these modules
    # fail. Calls through a kept reference still raise `ctypes.*` events, which the hook refuses.
    for name in {"ctypes", "_ctypes"} | {m for m in sys.modules if m.split(".")[0] in {"ctypes", "_ctypes"}}:
        sys.modules[name] = None  # type: ignore[assignment]  # None blocks the import
    del sys.modules["aiva_gateway.artifacts.oslayer"]
    return layers, syscall


def main(argv: list[str]) -> int:
    # Limits are set here rather than in a `preexec_fn`, which can deadlock in a multi-threaded parent.
    _limits()
    probe = argv[1:2] == ["--probe"]
    try:
        layers, syscall = _isolate(probe)
    except OSError as exc:
        print(json.dumps({"error": f"sandbox isolation unavailable: {exc}"}))
        return 4
    if probe and syscall is not None:
        port = int(argv[2]) if len(argv) > 2 else 9
        parent_fd = int(argv[3]) if len(argv) > 3 else -1
        native = _probe_native(port, parent_fd, syscall)
        sys.addaudithook(_deny)
        print(json.dumps({"probe": {**native, **_probe()}, "layers": layers}))
        return 0
    sys.addaudithook(_deny)
    document_type = argv[1] if len(argv) > 1 else ""
    data = sys.stdin.buffer.read()
    try:
        if document_type == "docx":
            result = _docx(data)
        elif document_type == "pptx":
            result = _pptx(data)
        else:
            print(json.dumps({"error": "unsupported document type"}))
            return 2
    except Exception as exc:  # the file could not be opened as the declared type
        print(json.dumps({"error": f"unreadable document: {type(exc).__name__}"}))
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
