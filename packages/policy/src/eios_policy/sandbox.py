"""Sandbox contract and the subprocess sandbox.

The subprocess sandbox is a *contract implementation with honest limits*:

* runs one executable by absolute path, pinned by SHA-256, never via a shell;
* scrubbed environment (no ``EIOS_*``, no secrets unless explicitly passed), private temp cwd;
* CPU, memory, file-size, process-count and open-file limits (``setrlimit``), wall-clock timeout,
  output cap, process-group kill;
* network isolation through a fresh network namespace (``unshare -rn``) when the host allows it.
  If isolation is required but unavailable the sandbox **refuses to run** (fail closed) instead of
  running unisolated.

Stronger isolation (containers, gVisor, microVMs) can implement :class:`Sandbox` later.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import resource
import shutil
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from eios_policy.root_policy import RootPolicy


class SandboxViolationError(Exception):
    """The requested execution breaks the sandbox contract and was not started."""


class SandboxUnavailableError(SandboxViolationError):
    """The isolation the request needs is not available on this host."""


@dataclass(frozen=True)
class SandboxSpec:
    argv: Sequence[str]
    sha256: str
    stdin: bytes = b""
    timeout_seconds: float = 30.0
    max_output_bytes: int = 1_000_000
    memory_mb: int = 512
    cpu_seconds: int = 30
    max_file_mb: int = 50
    # RLIMIT_NPROC counts every process of the real uid, so it is only meaningful with a dedicated
    # uid; opt-in. Fork bombs are bounded by the timeout and process-group kill instead.
    max_processes: int | None = None
    network: Literal["none"] = "none"
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool
    duration_ms: int
    network_isolated: bool


class Sandbox(Protocol):
    async def run(self, spec: SandboxSpec) -> SandboxResult: ...


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SubprocessSandbox:
    def __init__(self, root: RootPolicy, *, require_network_isolation: bool = True) -> None:
        self._root = root
        self._require_isolation = require_network_isolation
        self._unshare = shutil.which("unshare")
        self._isolation: bool | None = None

    async def network_isolation_available(self) -> bool:
        if self._isolation is None:
            self._isolation = False
            if self._unshare:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        self._unshare, "-rn", "true",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )  # fmt: skip
                    self._isolation = (await asyncio.wait_for(proc.wait(), 5)) == 0
                except (OSError, TimeoutError):
                    self._isolation = False
        return self._isolation

    def _validate(self, spec: SandboxSpec) -> Path:
        if not spec.argv:
            raise SandboxViolationError("empty command")
        exe = Path(spec.argv[0])
        if not exe.is_absolute() or ".." in exe.parts:
            raise SandboxViolationError("executable must be an absolute path")
        if exe.name.lower() in {b.lower() for b in self._root.database_client_binaries}:
            raise SandboxViolationError(f"'{exe.name}' is a database client; it may never run")
        for arg in spec.argv[1:]:
            if os.path.basename(arg).lower() in {
                b.lower() for b in self._root.database_client_binaries
            }:
                raise SandboxViolationError("database client referenced in arguments")
        resolved = exe.resolve()
        if not resolved.is_file():
            raise SandboxViolationError("executable does not exist")
        mode = resolved.stat().st_mode
        if mode & stat.S_IWOTH:
            raise SandboxViolationError("executable is world-writable")
        if not mode & stat.S_IXUSR:
            raise SandboxViolationError("file is not executable")
        if _sha256_file(resolved) != spec.sha256:
            raise SandboxViolationError("executable does not match its pinned SHA-256")
        return resolved

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        resolved = self._validate(spec)
        isolated = await self.network_isolation_available()
        if self._require_isolation and not isolated:
            raise SandboxUnavailableError(
                "network isolation is required but unavailable on this host; refusing to run"
            )
        argv = [str(resolved), *spec.argv[1:]]
        if isolated:
            argv = [str(self._unshare), "-rn", "--", *argv]
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **{
            k: v for k, v in spec.env.items() if not k.startswith("EIOS_")
        }}  # fmt: skip

        def limits() -> None:  # runs in the child between fork and exec
            os.setsid()
            mb = 1024 * 1024
            resource.setrlimit(resource.RLIMIT_CPU, (spec.cpu_seconds, spec.cpu_seconds + 1))
            resource.setrlimit(resource.RLIMIT_AS, (spec.memory_mb * mb, spec.memory_mb * mb))
            resource.setrlimit(
                resource.RLIMIT_FSIZE, (spec.max_file_mb * mb, spec.max_file_mb * mb)
            )
            if spec.max_processes is not None:
                resource.setrlimit(resource.RLIMIT_NPROC, (spec.max_processes, spec.max_processes))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        workdir = tempfile.mkdtemp(prefix="eios-sbx-")
        started = time.monotonic()
        timed_out = truncated = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
                preexec_fn=limits,
            )
            out, err = bytearray(), bytearray()

            async def pump(stream: asyncio.StreamReader | None, sink: bytearray) -> None:
                nonlocal truncated
                if stream is None:
                    return
                while chunk := await stream.read(65536):
                    room = spec.max_output_bytes - len(sink)
                    if room <= 0 or len(chunk) > room:
                        sink.extend(chunk[: max(room, 0)])
                        truncated = True
                        _kill(proc)
                        return
                    sink.extend(chunk)

            async def feed() -> None:
                if proc.stdin is None:
                    return
                try:
                    proc.stdin.write(spec.stdin)
                    await proc.stdin.drain()
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        feed(), pump(proc.stdout, out), pump(proc.stderr, err), proc.wait()
                    ),
                    timeout=spec.timeout_seconds,
                )
            except TimeoutError:
                timed_out = True
                _kill(proc)
                await proc.wait()
            return SandboxResult(
                exit_code=proc.returncode,
                stdout=bytes(out),
                stderr=bytes(err),
                timed_out=timed_out,
                truncated=truncated,
                duration_ms=int((time.monotonic() - started) * 1000),
                network_isolated=isolated,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
