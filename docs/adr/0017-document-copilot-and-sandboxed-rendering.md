# ADR-0017: Confidential document copilot — sovereign drafting, gateway-built files, sandboxed rendering

**Context.** LLD §15 #10 asks for a confidential DOCX/PPTX copilot with "sovereign direct route, editable
outputs, critical figures preserved". §16 benchmarks document outputs: open the generated file and check
structure, figures, citations and editability. §17 requires that cross-tenant artifact access is denied
and audited. PLAN §3 settles the LLD §20 open decision on rendering isolation: a sandboxed, network-less
subprocess, and the POC renders only files it generated.

**Decision.**
- **One governed run drafts the text** (`artifacts/service.py`). It is an ordinary chat run with policy,
  reservation, egress checks, assurance (output DLP) and metering. Confidential sources therefore take
  the sovereign direct path: GCT-Qwen only, the CIM bypassed, no external provider. The prompt asks for
  Markdown sections that cite each figure's source ID, and lists the critical figures to keep exactly.
- **The gateway builds the file** (`artifacts/builder.py`, python-docx 1.2 and python-pptx 1.0.2). It
  uses native, unprotected Office text and tables only: no images, macros or embedded objects. The
  gateway adds three things itself, so the model can neither drop nor alter them:
  - a provenance note: synthetic data, classification, and the draft's provenance and model;
  - a key-figures table copied from the request;
  - the sources list.
- **Checks run on the file as the sandbox opened it**, not on the draft (`artifacts/checks.py`):
  - structure;
  - **figures**: every critical figure appears exactly in the drafted text, ignoring thousands
    separators and never rounding. The gateway's own table is not counted;
  - **numbers**: no number in the drafted text that is absent from the request and its sources;
  - **citations**: every source cited, no unknown source;
  - editability: unprotected, no images, native text;
  - output DLP;
  - sandbox render.

  A document that fails any check is stored with status `failed_checks`. Simulated drafts fail honestly
  and are never shown as complete.
- **Sandbox** (`artifacts/sandbox.py`, `artifacts/render.py`), with independent layers:
  - Only bytes whose SHA-256 matches the recorded artifact are rendered (`ARTIFACT_NOT_GENERATED`). There
    is no upload path.
  - The child runs as `python -I`, with an empty environment (no proxies or credentials) and a throwaway
    working directory.
  - An audit hook installed before any document library is imported refuses network, DNS, subprocess,
    file-write (`open` modes and `os.open` flags), file-metadata (truncate, link, symlink, chmod, chown,
    utime, xattrs) and `ctypes` events.
  - rlimits cap address space at 1 GB, CPU at 15 s, file size at 0, open files at 64 and core dumps at 0.
    The child sets them before it reads its input (no `preexec_fn`, which can deadlock in a multi-threaded
    gateway). A 20 s wall-clock timeout kills the child.
  - `--probe` shows each refusal and is tested.
  - Unreadable bytes fail closed (`ARTIFACT_RENDER_FAILED`).
  - The preview is the sandbox's structured text blocks, never HTML.
- **Storage** (migration `scn_0002`). Each `artifact` row gains tenant, application, owner (tester
  pseudonym, or the application for an application credential), file metadata, SHA-256, status,
  classification, provenance and the check results (IDs only, never document text). The bytes live in
  `artifact_blob`, in Postgres, because Space filesystems are ephemeral. Downloads re-verify the hash
  (`409 ARTIFACT_INTEGRITY_FAILED`).
- **Access is owner-only.** Every other caller, including other users of the tenant, its administrators,
  the application itself and every other tenant, gets the same `404` as for a missing artifact.
  Existence is never disclosed. The refusal is written to the owning tenant's audit log as
  `artifact.access_denied`, with the reason (`cross_tenant` or `not_owner`) and the caller's tenant.
  Creation is audited too.

**Consequences.**
- The demo's figures pass only when the drafting model really preserves them. With GCT-Qwen simulated,
  artifacts are built but fail their checks until Endpoint B is live. CI scripts the sovereign model's
  text (labelled `simulated`) to exercise both faithful and faulty drafts.
- python-pptx brings Pillow. With Pillow installed, the google-genai SDK's content union no longer admits
  `list[Content]` under mypy, so the Gemini adapter casts at the call site. There is no runtime change.
- The sandbox is process-level isolation, which is enough for files the gateway generated. It is not a
  boundary for untrusted uploads; those would need a separate container or seccomp profile and malware
  scanning (LLD §20), and remain out of POC scope.
- **Known limitation: no OS-level isolation.** The sandbox is a Python audit hook plus rlimits in a
  separate process, with no OS-level network isolation. Audit hooks see only Python-level calls; native
  code in lxml and libxml2 does its own I/O. So the hash check (only gateway-generated bytes are rendered)
  stays the primary control. Wave 3 hardening adds the OS boundary: a network namespace plus seccomp, or
  bubblewrap, with a read-only filesystem.

**Amendment (2026-10-15): OS-level layer (Wave 3 hardening).** The render child now installs an OS layer
before anything else runs (`artifacts/oslayer.py`), and the gateway protects itself from its children:
- **seccomp-bpf, required.** After `PR_SET_NO_NEW_PRIVS` (no privilege needed), a filter refuses with
  `EPERM`: socket creation and every socket call; `execve`/`execveat`; process creation (`fork`, `vfork`,
  `clone` without `CLONE_THREAD`; `clone3` gets `ENOSYS` so the C library falls back to the checked
  `clone`); signals (`kill`, `tkill`, `tgkill`, `pidfd_send_signal`); `pidfd_open` and `pidfd_getfd`;
  `io_uring`; `ptrace`; `process_vm_readv`/`writev`; `mount`, `unshare` and `setns`. A foreign
  architecture or the x32 ABI kills the process. x86_64 and aarch64 are supported. The kernel enforces it,
  so it binds native code (lxml/libxml2) as well. If the filter cannot be installed the child renders
  nothing (fail closed).
- **The gateway is non-dumpable.** Before its first render child the gateway process sets
  `PR_SET_DUMPABLE` to 0. A child runs as the same uid, so without this it could open
  `/proc/<gateway>/mem` read-write. Seccomp does not cover that: it is an `open`, which the child needs.
- **Network namespace, best effort.** `unshare(CLONE_NEWUSER | CLONE_NEWNET)` where the kernel allows
  unprivileged user namespaces. Container runtimes usually refuse it (verified: `EPERM` as uid 1000 under
  Docker's default seccomp profile, which is how the gateway Space image runs), so it is reported in the
  probe rather than required.
- **Without the namespace, the child shares the gateway's network stack.** Seccomp alone does not close
  every path to it: denying new sockets still left the gateway's *existing* connections reachable through
  `pidfd_open` + `pidfd_getfd`, and the gateway's memory reachable through `/proc`. The pidfd and signal
  rules plus the non-dumpable gateway close those paths (Govern review of #59).
- **Verified** in the gateway Space image (uid 1000, Docker default profile) and in the test suite:
  - a connect made below the audit hook is refused, and a live listener receives nothing
    (`test_a_native_level_connect_never_leaves_the_sandbox`);
  - taking a connected socket the gateway holds via `pidfd_getfd`, then writing to it, leaves the peer
    empty (`test_the_sandbox_cannot_take_over_a_gateway_connection`);
  - signalling the gateway and opening its memory are refused;
  - native fork and exec are refused;
  - DOCX and PPTX still render.

  A negative control (old filter, dumpable gateway) showed signalling, `pidfd_open` and the memory open
  succeeding.
- **Not done, and why.** bubblewrap and nsjail need user namespaces or `CAP_SYS_ADMIN`, which the Space
  container lacks. A read-only filesystem view needs a mount namespace (same constraint). File writes stay
  limited by `RLIMIT_FSIZE=0` and the audit hook. Native code can still *read* files the uid can read, so
  the hash check remains the primary control for what is parsed. Untrusted uploads remain out of scope.
- **ctypes.** It is loaded to install the filter, then made unimportable (`sys.modules` entries set to
  `None`), because re-importing a loaded extension raises no `import` audit event. Calls through a kept
  reference still raise `ctypes.*` events, which the hook refuses.
- **Deployed check.** An s10 document render on the deployed gateway Space is part of the post-deploy
  smoke. If HF's runtime refused seccomp filters, it fails closed with "sandbox isolation unavailable".
