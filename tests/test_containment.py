"""Section 2.5 — adapter runtime containment (red-first).

ATV-bench runs untrusted subprocesses twice: when a harness builds a bot (the
EDIT turn, a host subprocess in ``adapters/contract.py``) and when captured bot
code executes. Section 2 gave per-run HOME isolation; this section adds a real
CONTAINMENT BOUNDARY around that host subprocess so a poisoned adapter cannot:

  * exfiltrate the seeded credential over the network (egress deny),
  * fork-bomb / memory-bomb the host (rlimit CPU/AS/NPROC), or
  * read a long-lived host credential (ephemeral, 0400, revoked post-match).

The mechanisms are chosen to be TESTABLE deterministically on this host with no
real internet and no real auth:

  * egress deny  -> ``unshare -Urn`` (unprivileged user+net namespace: a fresh
    network namespace whose loopback is down and which has no route out).
  * resource cap -> ``resource.setrlimit`` in a ``preexec_fn`` (RLIMIT_AS /
    RLIMIT_NPROC / RLIMIT_CPU).
  * cred scoping -> an ephemeral 0400 token file removed after the match.

Where a mechanism genuinely needs a privilege this sandbox lacks, the test
SKIPS with an explicit capability reason (from ``containment.capabilities()``)
rather than passing hollow.
"""
from __future__ import annotations

import os
import socket
import textwrap

import pytest

# Red: this module does not exist yet.
from atv_bench.containment import (
    ContainmentError,
    capabilities,
    contained_run,
    scoped_credential,
)


CAPS = capabilities()


# --------------------------------------------------------------------------- #
# 1. Egress denial
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not CAPS.can_deny_egress,
    reason=f"host cannot deny egress: {CAPS.egress_reason}",
)
def test_adapter_no_egress(tmp_path):
    """A contained child cannot reach a live localhost 'exfil' sink.

    We bind a real TCP sink on 127.0.0.1 in the PARENT namespace. A contained
    child (run inside a fresh network namespace) attempts to connect to it and
    send the 'stolen' secret. Because the child is in an isolated netns with no
    route to the parent's loopback, the connect must fail — the sink receives
    nothing.
    """
    sink = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sink.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sink.bind(("127.0.0.1", 0))
    sink.listen(1)
    sink.settimeout(2.0)
    host, port = sink.getsockname()

    child = textwrap.dedent(
        f"""
        import socket, sys
        s = socket.socket(); s.settimeout(2)
        try:
            s.connect(({host!r}, {port}))
            s.sendall(b"SECRET-EXFIL")
            print("CONNECTED")
        except OSError as e:
            print("BLOCKED", e)
            sys.exit(0)
        """
    )
    proc = contained_run(
        ["python", "-c", child],
        env=dict(os.environ),
        cwd=str(tmp_path),
        deny_egress=True,
        timeout=15,
    )
    # Child proves egress was blocked from inside.
    assert "CONNECTED" not in proc.stdout
    assert "BLOCKED" in proc.stdout

    # And the sink genuinely received no connection.
    with pytest.raises((socket.timeout, OSError)):
        conn, _ = sink.accept()
        conn.recv(64)
    sink.close()


def test_egress_allowed_when_disabled(tmp_path):
    """Control: with deny_egress=False the same connect SUCCEEDS.

    Proves the egress denial in the test above is caused by containment, not by
    the sink being unreachable for some incidental reason.
    """
    sink = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sink.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sink.bind(("127.0.0.1", 0))
    sink.listen(1)
    sink.settimeout(3.0)
    host, port = sink.getsockname()

    child = textwrap.dedent(
        f"""
        import socket
        s = socket.socket(); s.settimeout(2)
        s.connect(({host!r}, {port}))
        s.sendall(b"SECRET-EXFIL")
        print("CONNECTED")
        """
    )
    proc = contained_run(
        ["python", "-c", child],
        env=dict(os.environ),
        cwd=str(tmp_path),
        deny_egress=False,
        timeout=15,
    )
    assert "CONNECTED" in proc.stdout
    conn, _ = sink.accept()
    assert conn.recv(64) == b"SECRET-EXFIL"
    conn.close()
    sink.close()


# --------------------------------------------------------------------------- #
# 2. Resource caps
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not CAPS.can_cap_memory,
    reason=f"host cannot cap memory: {CAPS.rlimit_reason}",
)
def test_adapter_resource_cap_memory(tmp_path):
    """A memory-bomb child is killed by the address-space cap; host survives.

    The child requests far more than mem_limit. RLIMIT_AS makes the allocation
    fail (MemoryError / non-zero exit), and the parent process observes a
    non-success exit — scored CRASH by the Section 1 taxonomy — without the host
    itself being OOM-killed.
    """
    child = textwrap.dedent(
        """
        b = bytearray(800 * 1024 * 1024)  # 800 MiB, over the cap
        print("ALLOCATED", len(b))
        """
    )
    proc = contained_run(
        ["python", "-c", child],
        env=dict(os.environ),
        cwd=str(tmp_path),
        deny_egress=False,
        mem_limit=256 * 1024 * 1024,
        timeout=15,
    )
    assert "ALLOCATED" not in proc.stdout
    assert proc.returncode != 0  # capped -> failure -> CRASH


@pytest.mark.skipif(
    not CAPS.can_cap_nproc,
    reason=f"host cannot cap nproc: {CAPS.rlimit_reason}",
)
def test_adapter_resource_cap_nproc(tmp_path):
    """A fork-bomb child is contained by the process-count cap.

    RLIMIT_NPROC bounds the number of processes the contained uid may spawn, so
    the child hits BlockingIOError long before it can exhaust the host.
    """
    child = textwrap.dedent(
        """
        import os
        forks = 0
        pids = []
        try:
            for _ in range(500):
                pid = os.fork()
                if pid == 0:
                    os._exit(0)
                pids.append(pid); forks += 1
        except BlockingIOError:
            print("FORK_CAPPED", forks)
        finally:
            for p in pids:
                try: os.waitpid(p, 0)
                except OSError: pass
        """
    )
    proc = contained_run(
        ["python", "-c", child],
        env=dict(os.environ),
        cwd=str(tmp_path),
        deny_egress=True,  # userns gives a private uid where NPROC is meaningful
        nproc_limit=32,
        timeout=15,
    )
    assert "FORK_CAPPED" in proc.stdout


# --------------------------------------------------------------------------- #
# 3. Credential scoping
# --------------------------------------------------------------------------- #
def test_creds_not_readable_or_short_lived(tmp_path):
    """A mounted credential is 0400 (owner-read-only) and gone after the match."""
    seen_path = {}

    with scoped_credential("EPHEMERAL-TOKEN-XYZ", dir=tmp_path) as cred:
        p = cred.path
        seen_path["p"] = p
        assert p.exists()
        mode = p.stat().st_mode & 0o777
        # Owner read-only; no group/other bits at all.
        assert mode == 0o400, oct(mode)
        assert p.read_text() == "EPHEMERAL-TOKEN-XYZ"

    # Revoked / removed after the match.
    assert not seen_path["p"].exists()


def test_scoped_credential_revoked_on_error(tmp_path):
    """The credential is removed even if the match body raises."""
    captured = {}
    with pytest.raises(RuntimeError):
        with scoped_credential("TOK", dir=tmp_path) as cred:
            captured["p"] = cred.path
            assert cred.path.exists()
            raise RuntimeError("match blew up")
    assert not captured["p"].exists()


# --------------------------------------------------------------------------- #
# 4. API contract sanity
# --------------------------------------------------------------------------- #
def test_contained_run_returns_completedprocess(tmp_path):
    proc = contained_run(
        ["python", "-c", "print('hi')"],
        env=dict(os.environ),
        cwd=str(tmp_path),
        deny_egress=False,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "hi" in proc.stdout


def test_contained_run_timeout_raises(tmp_path):
    with pytest.raises(ContainmentError):
        contained_run(
            ["python", "-c", "import time; time.sleep(30)"],
            env=dict(os.environ),
            cwd=str(tmp_path),
            deny_egress=False,
            timeout=1,
        )
