"""Section 2.5 — a real containment boundary around adapter subprocesses.

ATV-bench runs untrusted subprocesses (the EDIT turn in ``adapters/contract.py``
and captured bot code). Section 2 gave per-run HOME isolation via
``isolation.isolated_home``; this module adds a CONTAINMENT BOUNDARY around the
same host subprocess so a poisoned adapter cannot:

  * exfiltrate a seeded credential over the network (egress deny),
  * fork-bomb / memory-bomb the host (rlimit CPU / AS / NPROC), or
  * read a long-lived host credential (ephemeral 0400 token, revoked post-match).

Mechanisms (all confirmed available, unprivileged, on the CI host):

  * egress deny  -> ``unshare -Urn`` (unprivileged user+net namespace): the child
    gets a fresh network namespace whose loopback is down and which has no route
    out, so any ``connect()`` to the host loopback returns ``Network is
    unreachable``.
  * resource cap -> ``resource.setrlimit`` in a ``preexec_fn`` (RLIMIT_AS /
    RLIMIT_NPROC / RLIMIT_CPU).
  * cred scoping -> ``scoped_credential``: an ephemeral 0400 token file removed
    after use (even on error).

Where a mechanism genuinely needs a privilege the host lacks, ``capabilities()``
reports it disabled with a reason so callers/tests skip explicitly rather than
pass hollow.

Composes with Section 2: ``contained_run`` takes the env dict produced by
``isolated_home`` and runs the SAME subprocess under both isolation and
containment.
"""
from __future__ import annotations

import dataclasses
import os
import resource
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence

__all__ = [
    "ContainmentError",
    "Capabilities",
    "capabilities",
    "contained_run",
    "scoped_credential",
    "ScopedCredential",
]


class ContainmentError(RuntimeError):
    """Raised when a contained run fails to launch or exceeds its timeout."""


@dataclasses.dataclass(frozen=True)
class Capabilities:
    can_deny_egress: bool
    can_cap_memory: bool
    can_cap_nproc: bool
    egress_reason: str
    rlimit_reason: str


def _probe_userns_netns() -> tuple[bool, str]:
    """Return (available, reason) for unprivileged user+net namespace egress deny."""
    if shutil.which("unshare") is None:
        return False, "`unshare` binary not found on PATH"
    try:
        proc = subprocess.run(
            ["unshare", "-Urn", "true"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"unshare -Urn failed to launch: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return False, f"unshare -Urn not permitted: {err}"
    return True, ""


def _probe_rlimit() -> tuple[bool, str]:
    """setrlimit is always available on POSIX; report why if not."""
    if not hasattr(resource, "setrlimit"):
        return False, "resource.setrlimit unavailable on this platform"
    return True, ""


def capabilities() -> Capabilities:
    """Probe the host for the containment mechanisms this module relies on."""
    egress_ok, egress_reason = _probe_userns_netns()
    rlimit_ok, rlimit_reason = _probe_rlimit()
    return Capabilities(
        can_deny_egress=egress_ok,
        can_cap_memory=rlimit_ok,
        can_cap_nproc=rlimit_ok,
        egress_reason=egress_reason,
        rlimit_reason=rlimit_reason,
    )


def _make_preexec(
    mem_limit: Optional[int],
    nproc_limit: Optional[int],
    cpu_limit: Optional[int],
):
    """Build a preexec_fn that installs the resource caps in the child.

    Runs post-fork, pre-exec, in the child process. Each cap is best-effort: a
    setrlimit that raises (e.g. trying to raise a hard limit) is skipped rather
    than aborting the launch.
    """
    if mem_limit is None and nproc_limit is None and cpu_limit is None:
        return None

    def _preexec() -> None:
        if mem_limit is not None:
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
            except (ValueError, OSError):
                pass
        if nproc_limit is not None:
            try:
                resource.setrlimit(
                    resource.RLIMIT_NPROC, (nproc_limit, nproc_limit)
                )
            except (ValueError, OSError):
                pass
        if cpu_limit is not None:
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
            except (ValueError, OSError):
                pass

    return _preexec


def contained_run(
    cmd: Sequence[str],
    *,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
    deny_egress: bool = True,
    mem_limit: Optional[int] = None,
    nproc_limit: Optional[int] = None,
    cpu_limit: Optional[int] = None,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run ``cmd`` inside a containment boundary and return its CompletedProcess.

    * ``deny_egress`` wraps the command in ``unshare -Urn`` so it runs in a fresh
      user+network namespace with no route out (blocks exfiltration).
    * ``mem_limit`` / ``nproc_limit`` / ``cpu_limit`` install RLIMIT_AS /
      RLIMIT_NPROC / RLIMIT_CPU in the child via a ``preexec_fn``. A child killed
      by a cap exits non-zero → scored CRASH by ``contract.classify_outcome``.
    * ``env`` is passed through verbatim, so this composes with
      ``isolation.isolated_home`` (pass its yielded env here).

    Raises ``ContainmentError`` if the command exceeds ``timeout`` or fails to
    launch.
    """
    argv: list[str] = list(cmd)
    if deny_egress:
        caps = capabilities()
        if not caps.can_deny_egress:
            raise ContainmentError(
                f"egress denial requested but unavailable: {caps.egress_reason}"
            )
        # `--` guards against the child argv being parsed as unshare options.
        argv = ["unshare", "-Urn", "--"] + argv

    preexec = _make_preexec(mem_limit, nproc_limit, cpu_limit)

    try:
        return subprocess.run(
            argv,
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=preexec,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContainmentError(
            f"contained run exceeded timeout of {timeout}s"
        ) from exc
    except OSError as exc:
        raise ContainmentError(f"contained run failed to launch: {exc}") from exc


@dataclasses.dataclass(frozen=True)
class ScopedCredential:
    """An ephemeral, owner-read-only credential file valid only inside the block."""

    path: Path


@contextmanager
def scoped_credential(
    token: str, *, dir: Optional[os.PathLike] = None
) -> Iterator[ScopedCredential]:
    """Write ``token`` to a 0400 file, yield it, and revoke it on exit.

    The file is created world/group-unreadable (mode 0400) and unconditionally
    removed when the block exits — even if the body raises — so a seeded
    credential never outlives the match that needed it.
    """
    directory = Path(dir) if dir is not None else Path(tempfile.gettempdir())
    directory.mkdir(parents=True, exist_ok=True)
    # Create with no permissions, write, then lock to owner-read-only.
    fd, name = tempfile.mkstemp(prefix="atv-cred-", dir=str(directory))
    path = Path(name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(token)
        os.chmod(path, 0o400)
        yield ScopedCredential(path=path)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
