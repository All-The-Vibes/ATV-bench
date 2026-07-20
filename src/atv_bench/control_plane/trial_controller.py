"""Local end-to-end control plane for one scheduled OCI trial.

The controller is intentionally local and self-attested. It validates identities,
drives the OCI lifecycle, grades only after lifecycle validation, stores internal
evidence in the content-addressed store, exports canonical protocol documents, and
records every state transition in an append-only hash-chained ledger.

It never manufactures official attestations or a TrustedRunnerLifecycleReceipt.
The OCI runner may prove a bidirectional protocol roundtrip, but this controller's
evidence remains local and self-attested, so every result remains unofficial and
non-rankable.
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import enum
import json
import os
import shutil
import stat
import struct
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Sequence

from atv_bench.eval import (
    AnalysisMode,
    Decision,
    EvidenceArtifact,
    EvidenceDocument,
    FileAssertionsGrader,
    GradeResult,
    GraderEvidence,
    HarnessStatus,
    InfrastructureStatus,
    PairedAnalysis,
    ProtocolExport,
    ProtocolExportEvidence,
    QualityGateFailure,
    RunnerEvidence,
    ScheduledTrial,
    TaskPackage,
    TrialOutcome,
    TrialSpec,
    TrustedPostRunGrader,
    TrustedRunnerLifecycleReceipt,
    export_protocol_bundle,
)
from atv_bench.eval.bundle import ContentAddressedStore, TrialBundle
from atv_bench.eval.protocol_export import (
    budget_analysis_id,
    model_policy_analysis_id,
)
from atv_bench.eval.stats import PairedTaskEffect
from atv_bench.harness_manifest import (
    LoadedHarnessManifest,
    create_oci_adapter_plan,
)
from atv_bench.protocol import (
    SchemaKind,
    canonical_digest,
    canonical_json_bytes,
    canonical_jsonl,
    default_schema_store,
    sha256_bytes,
)
from atv_bench.sandbox import (
    OciNetworkPolicy,
    OciRunnerLifecycleReceipt,
    OciTrack,
    OciTrialRequest,
    OciTrialResult,
    OciTrialRunner,
    OciTrialStatus,
)
from atv_bench.security import (
    Authorization,
    BrokerError,
    CredentialBroker,
    OpaqueTrialHandle,
    TrialBudget,
    TrialPolicy,
    UnderreportPolicy,
)

CONTROLLER_ID = "atv-local-trial-controller/v1"
LEDGER_SCHEMA = "atv.controller-ledger-entry/v1"
_ZERO_USAGE_FIELDS = (
    "model_input_tokens",
    "model_output_tokens",
    "model_total_tokens",
    "model_calls",
    "cost_microusd",
)
_SNAPSHOT_MAGIC = b"ATVOUT2\n"
_SNAPSHOT_COUNT = struct.Struct(">I")
_SNAPSHOT_ENTRY = struct.Struct(">IQ32s")
_MAX_PORTABLE_PATH_BYTES = 4_096
_MAX_ENGINE_CAPTURE_BYTES = 8 * 1024 * 1024
_REPRODUCTION_SCHEMA = "atv.reproduction-evidence/v1"
_REPRODUCTION_GRADER_SCHEMA = "atv.grader.file-assertions/v1"
_SNAPSHOT_SCHEMA_V1 = "atv.output-snapshot/v1"
_SNAPSHOT_SCHEMA_V2 = "atv.output-snapshot/v2"


def _oci_protocol_metadata(oci: OciTrialResult | None) -> Mapping[str, Any]:
    if oci is None:
        return {}
    value = getattr(oci.evidence, "protocol", {})
    return value if isinstance(value, Mapping) else {}


class ControllerState(str, enum.Enum):
    CREATED = "created"
    VALIDATING = "validating"
    VALIDATED = "validated"
    PROTOCOL_READY = "protocol_ready"
    CAPABILITY_ISSUED = "capability_issued"
    OCI_RUNNING = "oci_running"
    OCI_COMPLETED = "oci_completed"
    LIFECYCLE_VALIDATED = "lifecycle_validated"
    GRADING = "grading"
    GRADED = "graded"
    OUTCOME_CLASSIFIED = "outcome_classified"
    BUNDLED = "bundled"
    EXPORTED = "exported"
    COMPLETED = "completed"
    FAILED = "failed"


_ALLOWED_TRANSITIONS: Mapping[ControllerState, frozenset[ControllerState]] = {
    ControllerState.CREATED: frozenset(
        {ControllerState.VALIDATING, ControllerState.FAILED}
    ),
    ControllerState.VALIDATING: frozenset(
        {ControllerState.VALIDATED, ControllerState.FAILED}
    ),
    ControllerState.VALIDATED: frozenset(
        {ControllerState.PROTOCOL_READY, ControllerState.FAILED}
    ),
    ControllerState.PROTOCOL_READY: frozenset(
        {
            ControllerState.CAPABILITY_ISSUED,
            ControllerState.OCI_RUNNING,
            ControllerState.FAILED,
        }
    ),
    ControllerState.CAPABILITY_ISSUED: frozenset(
        {ControllerState.OCI_RUNNING, ControllerState.FAILED}
    ),
    ControllerState.OCI_RUNNING: frozenset(
        {ControllerState.OCI_COMPLETED, ControllerState.FAILED}
    ),
    ControllerState.OCI_COMPLETED: frozenset(
        {
            ControllerState.LIFECYCLE_VALIDATED,
            ControllerState.OUTCOME_CLASSIFIED,
            ControllerState.FAILED,
        }
    ),
    ControllerState.LIFECYCLE_VALIDATED: frozenset(
        {
            ControllerState.GRADING,
            ControllerState.OUTCOME_CLASSIFIED,
            ControllerState.FAILED,
        }
    ),
    ControllerState.GRADING: frozenset(
        {ControllerState.GRADED, ControllerState.FAILED}
    ),
    ControllerState.GRADED: frozenset(
        {ControllerState.OUTCOME_CLASSIFIED, ControllerState.FAILED}
    ),
    ControllerState.OUTCOME_CLASSIFIED: frozenset(
        {ControllerState.BUNDLED, ControllerState.FAILED}
    ),
    ControllerState.BUNDLED: frozenset(
        {ControllerState.EXPORTED, ControllerState.FAILED}
    ),
    ControllerState.EXPORTED: frozenset(
        {ControllerState.COMPLETED, ControllerState.FAILED}
    ),
    ControllerState.COMPLETED: frozenset(),
    ControllerState.FAILED: frozenset(),
}


@dataclasses.dataclass(frozen=True, slots=True)
class ControllerProblem(Exception):
    code: str
    problem: str
    cause: str
    fix: str
    evidence: str
    infrastructure: bool
    retryable: bool

    def __str__(self) -> str:
        return (
            f"{self.code}\nProblem: {self.problem}\nCause: {self.cause}\n"
            f"Fix: {self.fix}\nEvidence: {self.evidence}"
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class ControllerNotice:
    code: str
    problem: str
    cause: str
    fix: str
    evidence: str

    def to_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class ControllerModelPolicy:
    id: str
    version: str
    model_required: bool
    allowed_models: tuple[str, ...]
    allowed_route_ids: tuple[str, ...]
    gateway: str
    budget: TrialBudget
    max_retries: int = 0
    underreport_policy: UnderreportPolicy = UnderreportPolicy.REJECT
    handle_ttl_seconds: float = 300.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_models", tuple(self.allowed_models))
        object.__setattr__(self, "allowed_route_ids", tuple(self.allowed_route_ids))
        if not self.id or not self.version:
            raise ValueError("model policy id/version must be non-empty")
        if not self.allowed_models or not self.allowed_route_ids:
            raise ValueError("model policy requires models and route ids")
        if self.handle_ttl_seconds <= 0:
            raise ValueError("handle_ttl_seconds must be positive")

    def trial_policy(self, attempt_id: str, trial_id: str) -> TrialPolicy:
        return TrialPolicy(
            trial_id=trial_id,
            attempt_id=attempt_id,
            allowed_route_ids=self.allowed_route_ids,
            budget=self.budget,
            max_retries=self.max_retries,
            underreport_policy=self.underreport_policy,
        )

    @property
    def digest(self) -> str:
        return self.trial_policy("attempt", "trial").policy_digest

    @classmethod
    def model_free(cls) -> "ControllerModelPolicy":
        return cls(
            id="model-free",
            version="1.0.0",
            model_required=False,
            allowed_models=("none/model-free",),
            allowed_route_ids=("model-free",),
            gateway="model-gateway.invalid:443",
            budget=TrialBudget(0, 0, 0, 0, 0),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ControllerTaskSet:
    id: str
    version: str
    manifest_digest: str

    def to_protocol_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "manifest_digest": {
                "algorithm": "sha256",
                "value": self.manifest_digest,
            },
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ControllerRunRequest:
    scheduled: ScheduledTrial
    task: TaskPackage
    harness: LoadedHarnessManifest
    model_policy: ControllerModelPolicy
    task_set: ControllerTaskSet
    run_id: str
    track: OciTrack = OciTrack.CONTROLLED
    network: OciNetworkPolicy = dataclasses.field(default_factory=OciNetworkPolicy.none)
    selected_model: str | None = None
    prior_attempt_ids: tuple[str, ...] = ()
    retry_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "prior_attempt_ids", tuple(self.prior_attempt_ids))
        if not self.run_id:
            raise ValueError("run_id must be non-empty")


@dataclasses.dataclass(frozen=True, slots=True)
class LedgerEntry:
    sequence: int
    trial_id: str
    attempt_id: str
    state: ControllerState
    recorded_at: str
    evidence_digest: str | None
    problem: Mapping[str, Any] | None
    previous_digest: str | None
    entry_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": LEDGER_SCHEMA,
            "sequence": self.sequence,
            "trial_id": self.trial_id,
            "attempt_id": self.attempt_id,
            "state": self.state.value,
            "recorded_at": self.recorded_at,
            "evidence_digest": self.evidence_digest,
            "problem": dict(self.problem) if self.problem else None,
            "previous_digest": self.previous_digest,
            "entry_digest": self.entry_digest,
        }


@contextlib.contextmanager
def _exclusive_ledger_lock(path: Path) -> Iterator[None]:
    """Serialize ledger refresh-and-append across processes."""

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    locked = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ControllerProblem(
                "ledger_lock_unsafe",
                "The controller ledger lock path is not a regular file.",
                str(path),
                "Use a dedicated regular ledger and lock file.",
                str(path),
                True,
                False,
            )
        if os.name == "nt":
            import msvcrt

            if metadata.st_size == 0:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.01)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class ControllerLedger:
    """Append-only canonical JSONL ledger with a digest chain and idempotency."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._clock = clock or _utc_now
        self._lock = threading.RLock()
        self._entries: list[LedgerEntry] = []
        self._attempt_entries: dict[str, list[LedgerEntry]] = {}
        self._state_entries: dict[tuple[str, ControllerState], LedgerEntry] = {}
        self._loaded_size = 0
        self._file_identity: tuple[int, int] | None = None
        with _exclusive_ledger_lock(self._lock_path):
            self._replace_entries(self._load())

    def _load(self) -> list[LedgerEntry]:
        if not self.path.exists():
            self._loaded_size = 0
            self._file_identity = None
            return []
        if self.path.is_symlink() or not self.path.is_file():
            raise ControllerProblem(
                "ledger_path_unsafe",
                "The controller ledger path is not a regular file.",
                str(self.path),
                "Use a dedicated regular JSONL ledger file.",
                str(self.path),
                True,
                False,
            )
        metadata = os.stat(self.path, follow_symlinks=False)
        data = self.path.read_bytes()
        if data and not data.endswith(b"\n"):
            raise ControllerProblem(
                "ledger_corrupt",
                "The append-only controller ledger has an incomplete trailing entry.",
                str(self.path),
                "Restore the ledger from a verified copy.",
                str(self.path),
                True,
                False,
            )
        self._loaded_size = len(data)
        self._file_identity = (int(metadata.st_dev), int(metadata.st_ino))
        return self._decode_lines(data.splitlines(), start_sequence=0, previous=None)

    def _decode_lines(
        self,
        lines: Sequence[bytes],
        *,
        start_sequence: int,
        previous: str | None,
    ) -> list[LedgerEntry]:
        entries: list[LedgerEntry] = []
        for offset, line in enumerate(lines):
            index = start_sequence + offset
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ControllerProblem(
                    "ledger_corrupt",
                    "The append-only controller ledger is malformed.",
                    f"line {index + 1}: {type(exc).__name__}",
                    "Restore the ledger from a verified copy.",
                    str(self.path),
                    True,
                    False,
                ) from exc
            supplied = value.pop("entry_digest", None)
            observed = sha256_bytes(canonical_json_bytes(value))
            if supplied != observed or value.get("previous_digest") != previous:
                raise ControllerProblem(
                    "ledger_chain_invalid",
                    "The controller ledger digest chain is invalid.",
                    f"line {index + 1}",
                    "Do not edit ledger history; restore a verified append-only copy.",
                    str(self.path),
                    True,
                    False,
                )
            entry = LedgerEntry(
                sequence=int(value["sequence"]),
                trial_id=str(value["trial_id"]),
                attempt_id=str(value["attempt_id"]),
                state=ControllerState(value["state"]),
                recorded_at=str(value["recorded_at"]),
                evidence_digest=value.get("evidence_digest"),
                problem=value.get("problem"),
                previous_digest=value.get("previous_digest"),
                entry_digest=supplied,
            )
            if entry.sequence != index:
                raise ControllerProblem(
                    "ledger_sequence_invalid",
                    "The controller ledger sequence is not contiguous.",
                    f"expected {index}, received {entry.sequence}",
                    "Restore the append-only ledger.",
                    str(self.path),
                    True,
                    False,
                )
            entries.append(entry)
            previous = supplied
        return entries

    def _replace_entries(self, entries: Sequence[LedgerEntry]) -> None:
        self._entries = list(entries)
        self._attempt_entries = {}
        self._state_entries = {}
        for entry in self._entries:
            self._register_entry(entry)

    def _register_entry(self, entry: LedgerEntry) -> None:
        self._attempt_entries.setdefault(entry.attempt_id, []).append(entry)
        self._state_entries[(entry.attempt_id, entry.state)] = entry

    def _refresh_locked(self) -> None:
        if not self.path.exists():
            if self._entries:
                raise ControllerProblem(
                    "ledger_truncated",
                    "The append-only controller ledger disappeared.",
                    str(self.path),
                    "Restore the complete ledger before appending.",
                    str(self.path),
                    True,
                    False,
                )
            self._loaded_size = 0
            self._file_identity = None
            return
        if self.path.is_symlink() or not self.path.is_file():
            raise ControllerProblem(
                "ledger_path_unsafe",
                "The controller ledger path is not a regular file.",
                str(self.path),
                "Use a dedicated regular JSONL ledger file.",
                str(self.path),
                True,
                False,
            )
        metadata = os.stat(self.path, follow_symlinks=False)
        identity = (int(metadata.st_dev), int(metadata.st_ino))
        if (
            self._file_identity is not None
            and identity == self._file_identity
            and metadata.st_size == self._loaded_size
        ):
            return
        if (
            self._file_identity is None
            or identity != self._file_identity
            or metadata.st_size < self._loaded_size
        ):
            self._replace_entries(self._load())
            return
        with self.path.open("rb") as stream:
            stream.seek(self._loaded_size)
            suffix = stream.read()
        if not suffix or not suffix.endswith(b"\n"):
            raise ControllerProblem(
                "ledger_corrupt",
                "The append-only controller ledger has an incomplete trailing entry.",
                str(self.path),
                "Restore the ledger from a verified copy.",
                str(self.path),
                True,
                False,
            )
        previous = self._entries[-1].entry_digest if self._entries else None
        appended = self._decode_lines(
            suffix.splitlines(),
            start_sequence=len(self._entries),
            previous=previous,
        )
        for entry in appended:
            self._entries.append(entry)
            self._register_entry(entry)
        self._loaded_size += len(suffix)
        self._file_identity = identity

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        with self._lock:
            with _exclusive_ledger_lock(self._lock_path):
                self._refresh_locked()
                return tuple(self._entries)

    def entries_for(self, attempt_id: str) -> tuple[LedgerEntry, ...]:
        with self._lock:
            with _exclusive_ledger_lock(self._lock_path):
                self._refresh_locked()
                return tuple(self._attempt_entries.get(attempt_id, ()))

    def append(
        self,
        *,
        trial_id: str,
        attempt_id: str,
        state: ControllerState,
        evidence_digest: str | None = None,
        problem: ControllerProblem | None = None,
    ) -> LedgerEntry:
        with self._lock:
            with _exclusive_ledger_lock(self._lock_path):
                self._refresh_locked()
                existing = self._state_entries.get((attempt_id, state))
                problem_dict = problem.to_dict() if problem else None
                if existing is not None:
                    if (
                        existing.evidence_digest != evidence_digest
                        or existing.problem != problem_dict
                    ):
                        raise ControllerProblem(
                            "ledger_idempotency_conflict",
                            "The same attempt/state was recorded with different evidence.",
                            state.value,
                            "Use a new attempt identity for changed evidence.",
                            str(self.path),
                            True,
                            False,
                        )
                    return existing
                payload = {
                    "schema": LEDGER_SCHEMA,
                    "sequence": len(self._entries),
                    "trial_id": trial_id,
                    "attempt_id": attempt_id,
                    "state": state.value,
                    "recorded_at": self._clock(),
                    "evidence_digest": evidence_digest,
                    "problem": problem_dict,
                    "previous_digest": (
                        self._entries[-1].entry_digest if self._entries else None
                    ),
                }
                digest = sha256_bytes(canonical_json_bytes(payload))
                entry = LedgerEntry(
                    sequence=payload["sequence"],
                    trial_id=trial_id,
                    attempt_id=attempt_id,
                    state=state,
                    recorded_at=payload["recorded_at"],
                    evidence_digest=evidence_digest,
                    problem=problem_dict,
                    previous_digest=payload["previous_digest"],
                    entry_digest=digest,
                )
                line = canonical_json_bytes(entry.to_dict()) + b"\n"
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
                flags |= getattr(os, "O_BINARY", 0)
                descriptor = os.open(self.path, flags, 0o600)
                try:
                    remaining = memoryview(line)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("ledger append made no progress")
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                    metadata = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                self._entries.append(entry)
                self._register_entry(entry)
                self._loaded_size = int(metadata.st_size)
                self._file_identity = (
                    int(metadata.st_dev),
                    int(metadata.st_ino),
                )
                return entry

    def canonical_bytes(self, attempt_id: str | None = None) -> bytes:
        with self._lock:
            with _exclusive_ledger_lock(self._lock_path):
                self._refresh_locked()
                entries: Sequence[LedgerEntry] = (
                    self._entries
                    if attempt_id is None
                    else self._attempt_entries.get(attempt_id, ())
                )
                return b"".join(
                    canonical_json_bytes(entry.to_dict()) + b"\n"
                    for entry in entries
                )


class _StateMachine:
    def __init__(
        self,
        scheduled: ScheduledTrial,
        ledger: ControllerLedger,
    ) -> None:
        self.scheduled = scheduled
        self.ledger = ledger
        self.state = ControllerState.CREATED
        self.ledger.append(
            trial_id=scheduled.spec.trial_id,
            attempt_id=scheduled.attempt.attempt_id,
            state=self.state,
        )

    def transition(
        self,
        state: ControllerState,
        *,
        evidence_digest: str | None = None,
        problem: ControllerProblem | None = None,
    ) -> None:
        if state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ControllerProblem(
                "state_transition_invalid",
                "The controller attempted an invalid state transition.",
                f"{self.state.value} -> {state.value}",
                "Follow the explicit trial-controller lifecycle.",
                self.scheduled.attempt.attempt_id,
                True,
                False,
            )
        self.state = state
        self.ledger.append(
            trial_id=self.scheduled.spec.trial_id,
            attempt_id=self.scheduled.attempt.attempt_id,
            state=state,
            evidence_digest=evidence_digest,
            problem=problem,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class TrialControllerResult:
    request: ControllerRunRequest
    state: ControllerState
    outcome: TrialOutcome | None
    grade: GradeResult | None
    oci_result: OciTrialResult | None
    internal_bundle: TrialBundle | None
    protocol_export: ProtocolExport | None
    problem: ControllerProblem | None
    limitations: tuple[ControllerNotice, ...]
    capability_lineage: Mapping[str, Any] | None
    ledger_entries: tuple[LedgerEntry, ...]

    @property
    def trust_tier(self) -> str:
        return "local-self-attested"

    @property
    def rankable(self) -> bool:
        return False

    @property
    def official_verified(self) -> bool:
        return False

    @property
    def retryable(self) -> bool:
        return bool(
            self.problem
            and self.problem.infrastructure
            and self.problem.retryable
            and self.outcome is not None
            and self.outcome.infrastructure_status is not InfrastructureStatus.OK
        )


class _TerminalizingBroker:
    """Adapter that closes every attempt terminally, including failure revocation."""

    def __init__(self, broker: CredentialBroker) -> None:
        self.broker = broker

    def complete(self, handle: OpaqueTrialHandle | str) -> None:
        self.broker.complete(handle)

    def revoke(self, handle: OpaqueTrialHandle | str) -> None:
        # A completed broker attempt cannot be replayed and can parent an explicit
        # infrastructure retry. Completion is terminal, not a success claim.
        try:
            self.broker.complete(handle)
        except BrokerError:
            self.broker.revoke(handle)


_SNAPSHOT_EXPORTER = (
    "import hashlib,os,pathlib,stat,struct,sys\n"
    "root=pathlib.Path('/output')\n"
    "rows=[]\n"
    "for path in sorted(root.rglob('*')):\n"
    " st=os.lstat(path)\n"
    " rel=path.relative_to(root).as_posix()\n"
    " if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):\n"
    "  raise SystemExit('unsafe-output:'+rel)\n"
    " if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:\n"
    "  raise SystemExit('unsafe-output-hardlink:'+rel)\n"
    " if stat.S_ISREG(st.st_mode):\n"
    "  data=path.read_bytes()\n"
    "  encoded=rel.encode('utf-8')\n"
    "  if not encoded or len(encoded)>4096:\n"
    "   raise SystemExit('unsafe-output-path:'+rel)\n"
    "  rows.append((encoded,data,hashlib.sha256(data).digest()))\n"
    "stream=sys.stdout.buffer\n"
    f"stream.write({_SNAPSHOT_MAGIC!r})\n"
    "stream.write(struct.pack('>I',len(rows)))\n"
    "for rel,data,digest in rows:\n"
    " stream.write(struct.pack('>IQ32s',len(rel),len(data),digest))\n"
    " stream.write(rel)\n"
    " stream.write(data)\n"
    "stream.flush()\n"
)
SNAPSHOT_EXPORT_COMMAND = ("python", "-c", _SNAPSHOT_EXPORTER)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _relaxed_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _plus_seconds(value: str, seconds: int) -> str:
    observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (observed + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _protocol_bound_task(task: TaskPackage) -> tuple[TaskPackage, str]:
    manifest_digest = canonical_digest(task.manifest)["value"]
    return dataclasses.replace(task, digest=manifest_digest), manifest_digest


def _usage_template(*, model_free: bool) -> dict[str, int | None]:
    return {
        "wall_time_ms": 0,
        "cpu_time_ms": None,
        "model_input_tokens": 0 if model_free else None,
        "model_output_tokens": 0 if model_free else None,
        "model_total_tokens": 0 if model_free else None,
        "model_calls": 0 if model_free else None,
        "cost_microusd": 0 if model_free else None,
        "tool_calls": None,
        "memory_bytes": None,
        "storage_bytes": 0,
        "pids": None,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "artifact_bytes": 0,
    }


def _build_trial_request(
    request: ControllerRunRequest,
    *,
    task: TaskPackage,
    handle_required: bool,
    issued_at: str,
) -> dict[str, Any]:
    scheduled = request.scheduled
    spec = scheduled.spec
    manifest = request.harness.as_dict()
    budget = dict(task.manifest["budget_limits"])
    budget.update(
        {
            "wall_time_ms": spec.budget_profile.budget.wall_time_seconds * 1_000,
            "model_total_tokens": spec.budget_profile.budget.max_model_tokens,
            "model_calls": spec.budget_profile.budget.max_model_calls,
            "cost_microusd": spec.budget_profile.budget.max_cost_microusd,
        }
    )
    half = spec.budget_profile.budget.max_model_tokens // 2
    budget["model_input_tokens"] = half
    budget["model_output_tokens"] = spec.budget_profile.budget.max_model_tokens - half
    network = (
        {
            "mode": "model-gateway-only",
            "allowed_destinations": [request.model_policy.gateway],
        }
        if handle_required
        else {"mode": "none", "allowed_destinations": []}
    )
    credentials = (
        [
            {
                "name": "ATV_MODEL_GATEWAY_HANDLE",
                "handle": (
                    f"atv-credential://{scheduled.attempt.attempt_id}/model-gateway"
                ),
            }
        ]
        if handle_required
        else []
    )
    prompt = task.prompt_path.read_text(encoding="utf-8")
    document = {
        "schema": "atv.trial-request/v1",
        "protocol_version": 1,
        "benchmark_release": spec.benchmark_release,
        "track": request.track.value,
        "run_id": request.run_id,
        "trial_id": spec.trial_id,
        "attempt_id": scheduled.attempt.attempt_id,
        "schedule_id": spec.schedule_id,
        "task_set": request.task_set.to_protocol_dict(),
        "issued_at": issued_at,
        "expires_at": _plus_seconds(issued_at, 3_600),
        "nonce": scheduled.attempt.fresh_nonce,
        "task": {
            "id": spec.task.id,
            "version": spec.task.version,
            "manifest_digest": {
                "algorithm": "sha256",
                "value": spec.task.digest,
            },
        },
        "harness": {
            "id": spec.harness.id,
            "version": spec.harness.version,
            "manifest_digest": {
                "algorithm": "sha256",
                "value": spec.harness.digest,
            },
        },
        "model_policy": {
            "id": spec.model_policy.id,
            "version": spec.model_policy.version,
            "policy_digest": {
                "algorithm": "sha256",
                "value": spec.model_policy.digest,
            },
            "allowed_models": list(request.model_policy.allowed_models),
            "parameters_digest": {
                "algorithm": "sha256",
                "value": sha256_bytes(
                    canonical_json_bytes(
                        {
                            "models": list(request.model_policy.allowed_models),
                            "gateway": request.model_policy.gateway,
                        }
                    )
                ),
            },
            "retry_policy_digest": {
                "algorithm": "sha256",
                "value": sha256_bytes(
                    canonical_json_bytes(
                        {
                            "max_retries": request.model_policy.max_retries,
                            "underreport_policy": (
                                request.model_policy.underreport_policy.value
                            ),
                        }
                    )
                ),
            },
            "subagent_policy_digest": None,
            "gateway": request.model_policy.gateway,
        },
        "workspace": {
            "path": "/workspace",
            "artifacts_path": "/artifacts",
            "clean": True,
            "base_tree_digest": task.manifest["source"]["tree_digest"],
        },
        "prompt": {
            "text": prompt,
            "encoding": "utf-8",
            "digest": {
                "algorithm": "sha256",
                "value": sha256_bytes(prompt.encode("utf-8")),
            },
        },
        "budget_limits": budget,
        "protocol_limits": {
            "max_line_bytes": 262_144,
            "max_total_bytes": 33_554_432,
            "max_events": 20_000,
            "max_depth": 32,
            "max_nodes": 100_000,
            "max_object_properties": 256,
        },
        "cancellation": {
            "soft_signal": "sigterm",
            "grace_period_ms": 5_000,
            "hard_kill": True,
            "destroy_execution_cell": True,
        },
        "policy": {
            "tools": task.manifest["policy"]["tools"],
            "network": network,
            "writable_paths": ["/workspace", "/artifacts"],
            "credentials": credentials,
        },
        "seed": spec.schedule_seed,
        "order_assignment": {
            "block": scheduled.sequence_index,
            "repetition": spec.repetition,
            "position": scheduled.order_index,
            "side": "none",
            "worker_class": scheduled.worker_id,
        },
        "output": task.manifest["output"],
        "required_capabilities": manifest["capabilities"],
        "forbidden_capabilities": ["browser"],
    }
    default_schema_store().validate(document, SchemaKind.TRIAL_REQUEST)
    return document


def _snapshot_stdout_limit(task: TaskPackage, current_limit: int) -> int:
    contract = task.manifest["output"]
    max_files = int(contract["max_files"])
    if max_files > 0xFFFFFFFF:
        raise ControllerProblem(
            "grader_snapshot_capacity_exceeded",
            "The output contract cannot be encoded by the snapshot transport.",
            f"max_files={max_files}",
            "Reduce the task output file bound below 2^32.",
            task.id,
            True,
            False,
        )
    if bool(contract.get("allow_any_relative_path", False)):
        bounded_files = max_files
        path_bytes = bounded_files * _MAX_PORTABLE_PATH_BYTES
    else:
        allowed_lengths = sorted(
            (
                len(str(path).encode("utf-8"))
                for path in contract["allowed_paths"]
            ),
            reverse=True,
        )
        bounded_files = min(max_files, len(allowed_lengths))
        path_bytes = sum(allowed_lengths[:bounded_files])
    required = (
        len(_SNAPSHOT_MAGIC)
        + _SNAPSHOT_COUNT.size
        + int(contract["max_total_bytes"])
        + bounded_files * _SNAPSHOT_ENTRY.size
        + path_bytes
    )
    if required > _MAX_ENGINE_CAPTURE_BYTES:
        raise ControllerProblem(
            "grader_snapshot_capacity_exceeded",
            "The valid task output cannot fit the local OCI capture channel.",
            (
                f"required={required} engine_limit={_MAX_ENGINE_CAPTURE_BYTES} "
                f"output_max={contract['max_total_bytes']}"
            ),
            "Reduce the task output bound or use a runner with a larger evidence channel.",
            task.id,
            True,
            False,
        )
    return max(int(current_limit), required)


def _encode_output_snapshot(root: Path) -> bytes:
    rows: list[tuple[bytes, bytes, bytes]] = []
    for path in sorted(root.rglob("*")):
        metadata = os.lstat(path)
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise ValueError(f"unsafe output path: {relative}")
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise ValueError(f"unsafe output hardlink: {relative}")
            path_bytes = relative.encode("utf-8")
            if not path_bytes or len(path_bytes) > _MAX_PORTABLE_PATH_BYTES:
                raise ValueError(f"unsafe output path length: {relative}")
            content = path.read_bytes()
            rows.append(
                (
                    path_bytes,
                    content,
                    bytes.fromhex(sha256_bytes(content)),
                )
            )
    payload = bytearray(_SNAPSHOT_MAGIC)
    payload.extend(_SNAPSHOT_COUNT.pack(len(rows)))
    for path_bytes, content, digest in rows:
        payload.extend(
            _SNAPSHOT_ENTRY.pack(len(path_bytes), len(content), digest)
        )
        payload.extend(path_bytes)
        payload.extend(content)
    return bytes(payload)


def _snapshot_limits(
    output_contract: Mapping[str, Any] | None,
) -> tuple[int, int]:
    if output_contract is None:
        return 4_096, 64 * 1024 * 1024
    return (
        int(output_contract["max_files"]),
        int(output_contract["max_total_bytes"]),
    )


def _snapshot_relative_path(value: Any, *, evidence: str) -> PurePosixPath:
    relative = PurePosixPath(str(value))
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in str(value)
        or (relative.parts and ":" in relative.parts[0])
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for part in relative.parts
            for character in part
        )
    ):
        raise ControllerProblem(
            "grader_snapshot_path_unsafe",
            "A post-run snapshot path escapes the output tree.",
            str(value),
            "Use confined forward-slash relative paths.",
            evidence,
            True,
            False,
        )
    return relative


def decode_output_snapshot(
    data: bytes,
    destination: Path,
    *,
    output_contract: Mapping[str, Any] | None = None,
) -> Path:
    max_files, max_total_bytes = _snapshot_limits(output_contract)
    evidence = sha256_bytes(data)
    decoded: list[tuple[PurePosixPath, bytes]] = []
    seen: set[str] = set()
    total_bytes = 0

    if data.startswith(_SNAPSHOT_MAGIC):
        offset = len(_SNAPSHOT_MAGIC)
        if len(data) < offset + _SNAPSHOT_COUNT.size:
            raise ControllerProblem(
                "grader_snapshot_invalid",
                "The OCI post-run snapshot exporter returned truncated data.",
                "missing file count",
                "Use the controller-owned binary snapshot exporter.",
                evidence,
                True,
                True,
            )
        (file_count,) = _SNAPSHOT_COUNT.unpack_from(data, offset)
        offset += _SNAPSHOT_COUNT.size
        if file_count > max_files:
            raise ControllerProblem(
                "grader_snapshot_files_invalid",
                "The OCI post-run snapshot exceeds the task file bound.",
                f"{file_count} > {max_files}",
                "Return no more than the task output contract permits.",
                evidence,
                True,
                False,
            )
        previous_path: str | None = None
        for _ in range(file_count):
            if len(data) < offset + _SNAPSHOT_ENTRY.size:
                raise ControllerProblem(
                    "grader_snapshot_invalid",
                    "The OCI post-run snapshot exporter returned truncated metadata.",
                    "incomplete file header",
                    "Use the controller-owned binary snapshot exporter.",
                    evidence,
                    True,
                    True,
                )
            path_size, content_size, supplied_digest = _SNAPSHOT_ENTRY.unpack_from(
                data, offset
            )
            offset += _SNAPSHOT_ENTRY.size
            if (
                path_size <= 0
                or path_size > _MAX_PORTABLE_PATH_BYTES
                or len(data) < offset + path_size + content_size
            ):
                raise ControllerProblem(
                    "grader_snapshot_entry_invalid",
                    "A post-run snapshot entry is malformed or truncated.",
                    f"path_bytes={path_size} content_bytes={content_size}",
                    "Use bounded path and content lengths.",
                    evidence,
                    True,
                    True,
                )
            try:
                path_text = data[offset : offset + path_size].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ControllerProblem(
                    "grader_snapshot_path_unsafe",
                    "A post-run snapshot path is not valid UTF-8.",
                    type(exc).__name__,
                    "Use portable UTF-8 relative paths.",
                    evidence,
                    True,
                    False,
                ) from exc
            offset += path_size
            content = data[offset : offset + content_size]
            offset += content_size
            relative = _snapshot_relative_path(path_text, evidence=evidence)
            text = relative.as_posix()
            if text in seen or (
                previous_path is not None and text <= previous_path
            ):
                raise ControllerProblem(
                    "grader_snapshot_path_duplicate",
                    "The post-run snapshot paths are duplicated or unsorted.",
                    text,
                    "Emit each output file once in canonical path order.",
                    evidence,
                    True,
                    False,
                )
            seen.add(text)
            previous_path = text
            total_bytes += len(content)
            if total_bytes > max_total_bytes:
                raise ControllerProblem(
                    "grader_snapshot_entry_invalid",
                    "The post-run snapshot exceeds the task byte bound.",
                    f"{total_bytes} > {max_total_bytes}",
                    "Return output within the task output contract.",
                    evidence,
                    True,
                    False,
                )
            if bytes.fromhex(sha256_bytes(content)) != supplied_digest:
                raise ControllerProblem(
                    "grader_snapshot_digest_mismatch",
                    "A post-run snapshot file does not match its declared digest.",
                    text,
                    "Re-run the trusted snapshot exporter.",
                    evidence,
                    True,
                    True,
                )
            decoded.append((relative, content))
        if offset != len(data):
            raise ControllerProblem(
                "grader_snapshot_entry_invalid",
                "The post-run snapshot contains trailing bytes.",
                f"{len(data) - offset} unexpected bytes",
                "Use the canonical binary snapshot framing.",
                evidence,
                True,
                False,
            )
    else:
        decoded = _decode_legacy_output_snapshot(
            data,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
        )

    destination.mkdir(parents=True, exist_ok=False)
    for relative, content in decoded:
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return destination


def _decode_legacy_output_snapshot(
    data: bytes,
    *,
    max_files: int,
    max_total_bytes: int,
) -> list[tuple[PurePosixPath, bytes]]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerProblem(
            "grader_snapshot_invalid",
            "The OCI post-run snapshot exporter returned malformed data.",
            type(exc).__name__,
            "Use the controller-owned snapshot exporter without stdout pollution.",
            sha256_bytes(data),
            True,
            True,
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema") != "atv.output-snapshot/v1":
        raise ControllerProblem(
            "grader_snapshot_schema_invalid",
            "The OCI post-run snapshot has an unsupported schema.",
            repr(payload.get("schema") if isinstance(payload, dict) else None),
            "Return atv.output-snapshot/v1 from the trusted snapshot phase.",
            sha256_bytes(data),
            True,
            True,
        )
    files = payload.get("files")
    if not isinstance(files, list):
        raise ControllerProblem(
            "grader_snapshot_files_invalid",
            "The OCI post-run snapshot does not contain a file list.",
            type(files).__name__,
            "Return a canonical files array.",
            sha256_bytes(data),
            True,
            True,
        )
    if len(files) > max_files:
        raise ControllerProblem(
            "grader_snapshot_files_invalid",
            "The OCI post-run snapshot exceeds the task file bound.",
            f"{len(files)} > {max_files}",
            "Return no more than the task output contract permits.",
            sha256_bytes(data),
            True,
            False,
        )
    seen: set[str] = set()
    decoded: list[tuple[PurePosixPath, bytes]] = []
    total_bytes = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "size",
            "sha256",
            "data_b64",
        }:
            raise ControllerProblem(
                "grader_snapshot_entry_invalid",
                "A post-run snapshot entry is malformed.",
                repr(item),
                "Return path, size, sha256, and data_b64 only.",
                sha256_bytes(data),
                True,
                True,
            )
        relative = _snapshot_relative_path(
            item["path"],
            evidence=sha256_bytes(data),
        )
        text = relative.as_posix()
        if text in seen:
            raise ControllerProblem(
                "grader_snapshot_path_duplicate",
                "The post-run snapshot repeats an output path.",
                text,
                "Emit each output file exactly once.",
                sha256_bytes(data),
                True,
                False,
            )
        seen.add(text)
        try:
            content = base64.b64decode(item["data_b64"], validate=True)
        except Exception as exc:
            raise ControllerProblem(
                "grader_snapshot_base64_invalid",
                "A post-run snapshot file is not valid Base64.",
                type(exc).__name__,
                "Emit canonical Base64 file content.",
                text,
                True,
                False,
            ) from exc
        if (
            not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
            or len(content) != item["size"]
            or sha256_bytes(content) != item["sha256"]
        ):
            raise ControllerProblem(
                "grader_snapshot_digest_mismatch",
                "A post-run snapshot file does not match its declared digest.",
                text,
                "Re-run the trusted snapshot exporter.",
                sha256_bytes(data),
                True,
                True,
            )
        total_bytes += len(content)
        if total_bytes > max_total_bytes:
            raise ControllerProblem(
                "grader_snapshot_entry_invalid",
                "The post-run snapshot exceeds the task byte bound.",
                f"{total_bytes} > {max_total_bytes}",
                "Return output within the task output contract.",
                sha256_bytes(data),
                True,
                False,
            )
        decoded.append((relative, content))
    return decoded


_decode_output_snapshot = decode_output_snapshot


def _local_analysis(
    spec: TrialSpec,
    trial_request: Mapping[str, Any],
) -> PairedAnalysis:
    return PairedAnalysis(
        harness_a=spec.harness.id,
        harness_b="not-evaluated",
        model_policy_id=model_policy_analysis_id(
            trial_request["model_policy"]
        ),
        budget_profile_id=budget_analysis_id(
            spec.budget_profile.id,
            trial_request["budget_limits"],
        ),
        effects=(
            PairedTaskEffect(
                task_id=spec.task.id,
                repetitions=(),
                mean_a=0.0,
                mean_b=0.0,
                difference=0.0,
            ),
        ),
        mean_difference=0.0,
        confidence=0.95,
        ci_low=0.0,
        ci_high=0.0,
        equivalence_margin=0.05,
        descriptive_decision=Decision.INCONCLUSIVE,
        publication_decision=Decision.INCONCLUSIVE,
        publication_eligible=False,
        quality_gate_failures=(
            QualityGateFailure(
                code="single_trial_control_plane",
                message="one local trial cannot support a comparative publication claim",
            ),
        ),
        analysis_mode=AnalysisMode.SIMULATION,
        paired_permutation_p_value=1.0,
        direction_stability=0.0,
        bootstrap_samples=0,
        rankable_trial_count=0,
        infrastructure_exclusions=(),
    )


def _output_tree_artifact(output: Path) -> EvidenceArtifact:
    rows: list[dict[str, Any]] = []
    for path in sorted(output.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        rows.append(
            {
                "path": path.relative_to(output).as_posix(),
                "size": len(data),
                "sha256": sha256_bytes(data),
            }
        )
    return EvidenceArtifact(
        path="artifacts/output-tree.json",
        media_type="application/json",
        data=canonical_json_bytes({"files": rows}),
        role="output-tree",
    )


def _reproduction_documents(
    task: TaskPackage,
    grade: GradeResult,
    snapshot: bytes,
) -> tuple[EvidenceDocument, ...]:
    grader = FileAssertionsGrader.from_task(task)
    if grader.grader_digest != grade.grader_digest:
        raise ControllerProblem(
            "reproduction_grader_mismatch",
            "The canonical grader specification changed after grading.",
            (
                f"grade={grade.grader_digest} "
                f"task={grader.grader_digest}"
            ),
            "Restore the exact validated task package and rerun the trial.",
            task.id,
            True,
            False,
        )
    grader_document = EvidenceDocument.from_relaxed_json(
        schema=_REPRODUCTION_GRADER_SCHEMA,
        path="reproduction/grader.json",
        value=grader.spec,
    )
    snapshot_v2 = snapshot.startswith(_SNAPSHOT_MAGIC)
    snapshot_document = EvidenceDocument(
        schema=_SNAPSHOT_SCHEMA_V2 if snapshot_v2 else _SNAPSHOT_SCHEMA_V1,
        path=(
            "reproduction/output-snapshot.bin"
            if snapshot_v2
            else "reproduction/output-snapshot.json"
        ),
        media_type=(
            "application/octet-stream"
            if snapshot_v2
            else "application/json"
        ),
        data=snapshot,
    )
    manifest_document = EvidenceDocument.from_protocol_json(
        schema=_REPRODUCTION_SCHEMA,
        path="reproduction/manifest.json",
        value={
            "schema": _REPRODUCTION_SCHEMA,
            "task_manifest_digest": {
                "algorithm": "sha256",
                "value": canonical_digest(task.manifest)["value"],
            },
            "grader": grader_document.descriptor,
            "output_snapshot": snapshot_document.descriptor,
            "grade_result_digest": grade.result_digest,
            "grader_digest": grade.grader_digest,
            "output_tree_digest": grade.output_tree_digest,
        },
    )
    return grader_document, snapshot_document, manifest_document


def _lifecycle_document(receipt: OciRunnerLifecycleReceipt) -> EvidenceDocument:
    payload = {
        "schema": "atv.oci-runner-lifecycle-receipt/v1",
        "evidence_digest": receipt.evidence_digest,
        "execution_complete": receipt.execution_complete,
        "credential_finalized": receipt.credential_finalized,
        "hidden_inputs_mounted_after_harness_exit": (
            receipt.hidden_inputs_mounted_after_harness_exit
        ),
        "runtime_verified": receipt.runtime_verified,
        "trust_tier": receipt.trust_tier.value,
        "official_verified": False,
    }
    document = EvidenceDocument.from_protocol_json(
        schema="atv.runner-lifecycle/v1",
        path="runner/lifecycle.json",
        value=payload,
    )
    if document.descriptor["digest"]["value"] != receipt.receipt_digest:
        raise ControllerProblem(
            "lifecycle_receipt_digest_mismatch",
            "The OCI lifecycle receipt cannot be reproduced canonically.",
            "receipt digest differs from its public payload",
            "Reject the run and repair lifecycle serialization.",
            receipt.evidence_digest,
            True,
            True,
        )
    return document


def _derived_harness_result(
    outcome: TrialOutcome,
    grade: GradeResult | None,
    exit_evidence: Mapping[str, Any],
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    statuses = {
        HarnessStatus.COMPLETED: "completed",
        HarnessStatus.NO_EDIT: "no_edit",
        HarnessStatus.INVALID_ARTIFACT: "invalid_artifact",
        HarnessStatus.TIMED_OUT: "task_timeout",
        HarnessStatus.BUDGET_EXHAUSTED: "budget_exhausted",
        HarnessStatus.MODEL_UNREACHABLE: "model_unreachable",
        HarnessStatus.AUTH_FAILED: "auth_failed",
        HarnessStatus.POLICY_DENIED: "policy_denied",
        HarnessStatus.PROTOCOL_ERROR: "harness_crash",
        HarnessStatus.CRASHED: "harness_crash",
        HarnessStatus.NOT_RUN: "harness_crash",
    }
    return {
        "schema": "atv.harness-result/v1",
        "status": statuses[outcome.harness_status],
        "exit": dict(exit_evidence),
        "output_tree_digest": (
            {
                "algorithm": "sha256",
                "value": grade.output_tree_digest,
            }
            if grade is not None
            else None
        ),
        "artifacts": [],
        "reported_usage": dict(usage),
        "failure": (
            None
            if outcome.harness_status is HarnessStatus.COMPLETED
            else {
                "code": outcome.reason_code or outcome.harness_status.value,
                "scope": "harness",
                "retryable": False,
            }
        ),
    }


class TrialController:
    def __init__(
        self,
        *,
        oci_runner: OciTrialRunner,
        ledger: ControllerLedger,
        store: ContentAddressedStore,
        broker: CredentialBroker | None = None,
        grader_factory: Callable[[TaskPackage], TrustedPostRunGrader] = (
            FileAssertionsGrader.from_task
        ),
        gateway_healthcheck: Callable[
            [OpaqueTrialHandle, ControllerRunRequest], None
        ]
        | None = None,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.oci_runner = oci_runner
        self.ledger = ledger
        self.store = store
        self.broker = broker
        self.grader_factory = grader_factory
        self.gateway_healthcheck = gateway_healthcheck
        self.clock = clock
        self._results: dict[str, TrialControllerResult] = {}

    def _validate(self, request: ControllerRunRequest) -> TaskPackage:
        spec = request.scheduled.spec
        task_view, task_manifest_digest = _protocol_bound_task(request.task)
        if (
            spec.task.id != request.task.id
            or spec.task.version != request.task.version
            or spec.task.digest != task_manifest_digest
        ):
            raise ControllerProblem(
                "task_identity_mismatch",
                "Scheduled task identity does not match the validated task manifest.",
                (
                    f"scheduled={spec.task.to_dict()} "
                    f"manifest_digest={task_manifest_digest} "
                    f"package_tree_digest={request.task.digest}"
                ),
                "Schedule the canonical task-manifest digest while retaining the "
                "validated package tree digest as separate evidence.",
                str(request.task.root),
                True,
                False,
            )
        if (
            spec.harness.id != request.harness.id
            or spec.harness.version != request.harness.version
            or spec.harness.digest != request.harness.digest
        ):
            raise ControllerProblem(
                "harness_identity_mismatch",
                "Scheduled harness identity does not match the loaded manifest.",
                f"scheduled={spec.harness.to_dict()} loaded={request.harness.identity}",
                "Rebuild the schedule from the loaded immutable harness manifest.",
                str(request.harness.source_path),
                True,
                False,
            )
        if (
            spec.model_policy.id != request.model_policy.id
            or spec.model_policy.version != request.model_policy.version
            or spec.model_policy.digest != request.model_policy.digest
        ):
            raise ControllerProblem(
                "model_policy_identity_mismatch",
                "Scheduled model policy does not match controller policy.",
                spec.model_policy.digest,
                "Build the schedule from the exact broker policy digest.",
                request.model_policy.digest,
                True,
                False,
            )
        if request.harness.runtime_kind != "oci":
            raise ControllerProblem(
                "harness_runtime_unsupported",
                "The trial controller currently requires an OCI harness.",
                request.harness.runtime_kind,
                "Use a digest-pinned OCI harness manifest.",
                str(request.harness.source_path),
                True,
                False,
            )
        if request.model_policy.model_required:
            if self.broker is None:
                raise ControllerProblem(
                    "credential_broker_missing",
                    "A model-backed trial has no credential broker.",
                    "broker=None",
                    "Configure CredentialBroker and a private gateway network.",
                    spec.trial_id,
                    True,
                    True,
                )
            if request.network.mode.value != "model-gateway-only":
                raise ControllerProblem(
                    "gateway_network_missing",
                    "A model-backed trial lacks model-gateway-only network policy.",
                    request.network.mode.value,
                    "Provide an internal network with exact gateway identities.",
                    spec.trial_id,
                    True,
                    True,
                )
        elif request.network.mode.value != "none":
            raise ControllerProblem(
                "model_free_network_conflict",
                "A model-free trial requested network access.",
                request.network.mode.value,
                "Use OciNetworkPolicy.none().",
                spec.trial_id,
                True,
                False,
            )
        return task_view

    def _issue_capability(
        self,
        request: ControllerRunRequest,
    ) -> tuple[OpaqueTrialHandle | None, Mapping[str, Any] | None]:
        if not request.model_policy.model_required:
            return None, None
        assert self.broker is not None
        policy = request.model_policy.trial_policy(
            request.scheduled.attempt.attempt_id,
            request.scheduled.spec.trial_id,
        )
        try:
            if request.scheduled.attempt.attempt_number == 1:
                handle = self.broker.issue_trial(
                    policy,
                    ttl_seconds=request.model_policy.handle_ttl_seconds,
                )
            else:
                handle = self.broker.create_retry_attempt(
                    trial_id=request.scheduled.spec.trial_id,
                    attempt_id=request.scheduled.attempt.attempt_id,
                    ttl_seconds=request.model_policy.handle_ttl_seconds,
                )
            authorization: Authorization = self.broker.authorize(
                handle,
                trial_id=request.scheduled.spec.trial_id,
            )
            return handle, {
                "attempt_id": authorization.attempt_id,
                "policy_digest": authorization.policy_digest,
                "budget_identity": authorization.budget_identity.to_dict(),
                "issuance": authorization.issuance.to_dict(),
            }
        except Exception as exc:
            raise ControllerProblem(
                "model_gateway_setup_failed",
                "The attempt-scoped model capability could not be issued.",
                type(exc).__name__,
                "Retry only as typed infrastructure failure after repairing broker state.",
                request.scheduled.attempt.attempt_id,
                True,
                True,
            ) from None

    @staticmethod
    def _classify_oci(
        oci: OciTrialResult,
    ) -> tuple[InfrastructureStatus, HarnessStatus, str]:
        if oci.status is OciTrialStatus.COMPLETED:
            if oci.protocol_transcript is not None:
                status = oci.protocol_transcript.status.value
                mapping = {
                    "completed": HarnessStatus.COMPLETED,
                    "no_edit": HarnessStatus.NO_EDIT,
                    "invalid_artifact": HarnessStatus.INVALID_ARTIFACT,
                    "task_timeout": HarnessStatus.TIMED_OUT,
                    "model_unreachable": HarnessStatus.MODEL_UNREACHABLE,
                    "auth_failed": HarnessStatus.AUTH_FAILED,
                    "policy_denied": HarnessStatus.POLICY_DENIED,
                    "budget_exhausted": HarnessStatus.BUDGET_EXHAUSTED,
                    "harness_crash": HarnessStatus.CRASHED,
                    "cancelled": HarnessStatus.PROTOCOL_ERROR,
                }
                return InfrastructureStatus.OK, mapping[status], status
            return InfrastructureStatus.OK, HarnessStatus.COMPLETED, ""
        if oci.status is OciTrialStatus.TIMED_OUT:
            return InfrastructureStatus.OK, HarnessStatus.TIMED_OUT, "task_timeout"
        if oci.status is OciTrialStatus.NONZERO_EXIT:
            return InfrastructureStatus.OK, HarnessStatus.CRASHED, "nonzero_exit"
        if oci.status is OciTrialStatus.PROTOCOL_ERROR:
            return InfrastructureStatus.OK, HarnessStatus.PROTOCOL_ERROR, "protocol_error"
        if oci.status is OciTrialStatus.INVALID_OUTPUT:
            return (
                InfrastructureStatus.OK,
                HarnessStatus.INVALID_ARTIFACT,
                "invalid_output",
            )
        if oci.status is OciTrialStatus.CANCELLED:
            return (
                InfrastructureStatus.CANCELLED,
                HarnessStatus.NOT_RUN,
                "controller_cancelled",
            )
        if oci.status is OciTrialStatus.GRADER_FAILED:
            return (
                InfrastructureStatus.GRADER_FAILED,
                HarnessStatus.COMPLETED,
                "oci_grader_failed",
            )
        return (
            InfrastructureStatus.RUNNER_FAILED,
            HarnessStatus.NOT_RUN,
            oci.status.value,
        )

    @staticmethod
    def _exit_evidence(oci: OciTrialResult | None) -> dict[str, Any]:
        if oci is None or oci.evidence.harness is None:
            return {
                "code": None,
                "signal": None,
                "timed_out": False,
                "cancelled": False,
            }
        run = oci.evidence.harness.run
        return {
            "code": run.exit_code,
            "signal": None,
            "timed_out": run.timed_out,
            "cancelled": run.cancelled,
        }

    @staticmethod
    def _observed_usage(
        oci: OciTrialResult,
        *,
        model_free: bool,
    ) -> dict[str, int | None]:
        usage = _usage_template(model_free=model_free)
        harness = oci.evidence.harness
        if harness is not None:
            usage.update(
                {
                    "wall_time_ms": harness.run.duration_ms,
                    "storage_bytes": harness.storage.peak_bytes,
                    "stdout_bytes": harness.run.stdout_total_bytes,
                    "stderr_bytes": harness.run.stderr_total_bytes,
                    "artifact_bytes": int(oci.evidence.workspace.get("bytes", 0)),
                    "memory_bytes": harness.policy["resources"]["memory_bytes"],
                    "pids": harness.policy["resources"]["pids_limit"],
                }
            )
        return usage

    def _build_protocol_export(
        self,
        request: ControllerRunRequest,
        *,
        task: TaskPackage,
        trial_request: Mapping[str, Any],
        outcome: TrialOutcome,
        grade: GradeResult | None,
        output_tree: Path | None,
        oci: OciTrialResult,
        limitations: Sequence[ControllerNotice],
    ) -> ProtocolExport:
        if request.model_policy.model_required:
            raise ControllerProblem(
                "model_evidence_export_unavailable",
                "Model-backed local export lacks canonical gateway receipt assembly.",
                "The controller has no public ModelEvidence receipt set.",
                "Export after binding gateway logs and receipts to the attempt.",
                request.scheduled.attempt.attempt_id,
                True,
                True,
            )
        receipt = oci.lifecycle_receipt
        if isinstance(receipt, TrustedRunnerLifecycleReceipt) or receipt.official_verified:
            raise ControllerProblem(
                "local_trust_escalation",
                "The local controller received an official runner receipt.",
                type(receipt).__name__,
                "Use only the OCI local-self-attested lifecycle receipt.",
                request.scheduled.attempt.attempt_id,
                True,
                False,
            )
        try:
            receipt.validate_for_grading()
        except Exception as exc:
            raise ControllerProblem(
                "lifecycle_export_invalid",
                "The OCI lifecycle receipt cannot authorize export.",
                type(exc).__name__,
                "Repair container cleanup, credential finalization, and hidden-input ordering.",
                request.scheduled.attempt.attempt_id,
                True,
                True,
            ) from None
        lifecycle_document = _lifecycle_document(receipt)
        usage = self._observed_usage(oci, model_free=True)
        exit_evidence = self._exit_evidence(oci)
        # The event stream preserves the harness-authored result verbatim.  The
        # canonical exported harness result is controller-derived from observed
        # lifecycle and trusted grading evidence so an untrusted harness cannot
        # choose its authoritative output-tree digest, usage, or exit facts.
        harness_result = _derived_harness_result(
            outcome,
            grade,
            exit_evidence,
            usage,
        )
        event_bytes = (
            canonical_jsonl(oci.protocol_transcript.events)
            if oci.protocol_transcript is not None
            else canonical_json_bytes(
                {
                    "schema": "atv.controller-protocol-limitation/v1",
                    "trial_id": request.scheduled.spec.trial_id,
                    "attempt_id": request.scheduled.attempt.attempt_id,
                    "transport": _oci_protocol_metadata(oci).get(
                        "mode",
                        "unavailable",
                    ),
                    "authority_verified": bool(
                        _oci_protocol_metadata(oci).get("authority_verified")
                    ),
                    "official_eligible": bool(
                        _oci_protocol_metadata(oci).get("official_eligible")
                    ),
                }
            )
            + b"\n"
        )
        event_stream = EvidenceDocument(
            schema="atv.event/v1",
            path="trial/events.jsonl",
            media_type="application/x-ndjson",
            data=event_bytes,
        )
        output_artifact = (
            _output_tree_artifact(output_tree)
            if output_tree is not None and grade is not None
            else None
        )
        identity_digest = sha256_bytes(CONTROLLER_ID.encode("utf-8"))
        runner = RunnerEvidence(
            run_id=request.run_id,
            track=request.track.value,
            task_set=request.task_set.to_protocol_dict(),
            identity={
                "id": "atv-local-trial-controller",
                "version": "1.0.0",
                "manifest_digest": {
                    "algorithm": "sha256",
                    "value": identity_digest,
                },
            },
            platform={"os": "linux", "architecture": "amd64"},
            runtime_digest=oci.evidence.digest,
            started_at=oci.evidence.started_at,
            ended_at=self.clock(),
            duration_ms=oci.evidence.duration_ms,
            exit=exit_evidence,
            reported_usage=usage,
            observed_usage=usage,
            authoritative_usage=usage,
            lifecycle_receipt=receipt,
            lifecycle_document=lifecycle_document,
            prior_attempt_ids=request.prior_attempt_ids,
            retry_reason=request.retry_reason,
        )
        grader = (
            GraderEvidence(
                identity={
                    "id": "atv-file-assertions-grader",
                    "version": "1.0.0",
                    "manifest_digest": {
                        "algorithm": "sha256",
                        "value": grade.grader_digest,
                    },
                },
                image_digest=str(task.manifest["grader"]["image"]).split(
                    "@sha256:", 1
                )[1],
            )
            if grade is not None
            else None
        )
        logs: list[EvidenceDocument] = [
            EvidenceDocument.from_protocol_json(
                schema="atv.controller-limitations/v1",
                path="controller/limitations.json",
                value={
                    "schema": "atv.controller-limitations/v1",
                    "items": [item.to_dict() for item in limitations],
                },
            )
        ]
        if grade is not None and output_tree is not None:
            logs.extend(_reproduction_documents(task, grade, oci.grader_stdout))
        evidence = ProtocolExportEvidence(
            trust_tier="local-self-attested",
            created_at=self.clock(),
            harness_manifest=request.harness.as_dict(),
            task_manifest=task.manifest,
            trial_request=trial_request,
            event_stream=event_stream,
            harness_result=harness_result,
            runner=runner,
            models=(),
            grader=grader,
            attestations=(),
            output_tree=output_artifact,
            artifacts=(),
            logs=tuple(logs),
            model_free=True,
        )
        exported = export_protocol_bundle(
            spec=request.scheduled.spec,
            attempt=request.scheduled.attempt,
            outcome=outcome,
            grade=grade,
            analysis=_local_analysis(
                request.scheduled.spec,
                trial_request,
            ),
            evidence=evidence,
        )
        exported.verify()
        if exported.trial_result["trust_tier"] != "local-self-attested":
            raise ControllerProblem(
                "export_trust_tier_invalid",
                "The local export changed trust tier.",
                str(exported.trial_result["trust_tier"]),
                "Force local-self-attested export.",
                request.scheduled.attempt.attempt_id,
                True,
                False,
            )
        if exported.trial_result["rankable"] is not False:
            raise ControllerProblem(
                "export_rankable_invalid",
                "The local export became rankable.",
                "rankable=true",
                "Local controller results must remain non-rankable.",
                request.scheduled.attempt.attempt_id,
                True,
                False,
            )
        return exported

    def _bundle(
        self,
        request: ControllerRunRequest,
        *,
        outcome: TrialOutcome,
        grade: GradeResult | None,
        output_tree: Path | None,
        oci: OciTrialResult | None,
        problem: ControllerProblem | None,
    ) -> TrialBundle:
        artifacts: dict[str, bytes | Path] = {
            "controller/ledger.jsonl": self.ledger.canonical_bytes(
                request.scheduled.attempt.attempt_id
            ),
        }
        if oci is not None:
            artifacts.update(
                {
                    "runner/oci-evidence.json": oci.evidence.canonical_bytes,
                    "logs/harness-stdout.bin": oci.harness_stdout,
                    "logs/harness-stderr.bin": oci.harness_stderr,
                    "logs/grader-stdout.bin": oci.grader_stdout,
                    "logs/grader-stderr.bin": oci.grader_stderr,
                }
            )
        if problem is not None:
            artifacts["controller/problem.json"] = canonical_json_bytes(
                problem.to_dict()
            )
        bundle = TrialBundle.create(
            self.store,
            spec=request.scheduled.spec,
            attempt=request.scheduled.attempt,
            outcome=outcome,
            grade=grade,
            output_tree=output_tree,
            artifacts=artifacts,
            runner_metadata={
                "controller_id": CONTROLLER_ID,
                "worker_id": request.scheduled.worker_id,
                "order_index": request.scheduled.order_index,
                "block_id": request.scheduled.block_id,
                "oci_evidence_digest": oci.evidence.digest if oci else None,
                "protocol_transport": _oci_protocol_metadata(oci).get(
                    "mode",
                    "unavailable",
                ),
            },
        )
        bundle.verify()
        TrialBundle.load(self.store, bundle.digest).verify()
        return bundle

    def run(self, request: ControllerRunRequest) -> TrialControllerResult:
        attempt_id = request.scheduled.attempt.attempt_id
        if attempt_id in self._results:
            return self._results[attempt_id]
        machine = _StateMachine(request.scheduled, self.ledger)
        outcome: TrialOutcome | None = None
        grade: GradeResult | None = None
        oci: OciTrialResult | None = None
        bundle: TrialBundle | None = None
        exported: ProtocolExport | None = None
        problem: ControllerProblem | None = None
        capability_lineage: Mapping[str, Any] | None = None
        limitations = [
            ControllerNotice(
                code="local_self_attested_runner",
                problem="The controller evidence is local and not independently signed.",
                cause="This process is not an official benchmark runner or external reproducer.",
                fix="Re-run through the role-scoped signed official runner and publish its attestations.",
                evidence=request.scheduled.attempt.attempt_id,
            ),
        ]
        output_root: Path | None = None
        handle: OpaqueTrialHandle | None = None
        trial_request: Mapping[str, Any] | None = None
        task_view: TaskPackage | None = None

        try:
            machine.transition(ControllerState.VALIDATING)
            task_view = self._validate(request)
            machine.transition(ControllerState.VALIDATED)
            trial_request = _build_trial_request(
                request,
                task=task_view,
                handle_required=request.model_policy.model_required,
                issued_at=self.clock(),
            )
            machine.transition(
                ControllerState.PROTOCOL_READY,
                evidence_digest=canonical_digest(trial_request)["value"],
            )

            handle, capability_lineage = self._issue_capability(request)
            if handle is not None:
                machine.transition(
                    ControllerState.CAPABILITY_ISSUED,
                    evidence_digest=str(
                        capability_lineage["budget_identity"][
                            "budget_identity_digest"
                        ]
                    ),
                )
                if self.gateway_healthcheck is not None:
                    try:
                        self.gateway_healthcheck(handle, request)
                    except Exception:
                        assert self.broker is not None
                        self.broker.complete(handle)
                        raise ControllerProblem(
                            "model_gateway_healthcheck_failed",
                            "The model gateway failed before harness execution.",
                            "gateway healthcheck raised an exception",
                            "Repair the gateway and retry as infrastructure failure.",
                            request.scheduled.attempt.attempt_id,
                            True,
                            True,
                        ) from None

            plan = create_oci_adapter_plan(
                request.harness,
                trial_request,
                attempt=request.scheduled.attempt,
                task=task_view,
                network=request.network,
                gateway_handle=handle,
                credential_broker=(
                    _TerminalizingBroker(self.broker)
                    if handle is not None and self.broker is not None
                    else None
                ),
                model=request.selected_model,
            )
            oci_request: OciTrialRequest = dataclasses.replace(
                plan.request,
                grader_command=SNAPSHOT_EXPORT_COMMAND,
                grader_resources=dataclasses.replace(
                    plan.request.grader_resources,
                    stdout_bytes=_snapshot_stdout_limit(
                        task_view,
                        plan.request.grader_resources.stdout_bytes,
                    ),
                ),
            )
            machine.transition(ControllerState.OCI_RUNNING)
            oci = self.oci_runner.run(oci_request)
            if not bool(_oci_protocol_metadata(oci).get("authority_verified")):
                limitations.append(
                    ControllerNotice(
                        code="protocol_transport_unverified",
                        problem="The OCI run did not prove a controller/harness roundtrip.",
                        cause=str(
                            _oci_protocol_metadata(oci).get("error")
                            or "No authority-verified interactive transcript was returned."
                        ),
                        fix="Use the attached interactive OCI transport and verify exact cleanup.",
                        evidence=request.scheduled.attempt.attempt_id,
                    )
                )
            machine.transition(
                ControllerState.OCI_COMPLETED,
                evidence_digest=oci.evidence.digest,
            )
            infrastructure, harness_status, reason = self._classify_oci(oci)

            if infrastructure is InfrastructureStatus.OK:
                if not isinstance(oci.lifecycle_receipt, OciRunnerLifecycleReceipt):
                    raise ControllerProblem(
                        "lifecycle_receipt_type_invalid",
                        "The OCI runner returned the wrong lifecycle receipt type.",
                        type(oci.lifecycle_receipt).__name__,
                        "Return OciRunnerLifecycleReceipt for local execution.",
                        request.scheduled.attempt.attempt_id,
                        True,
                        True,
                    )
                if isinstance(
                    oci.lifecycle_receipt, TrustedRunnerLifecycleReceipt
                ) or oci.lifecycle_receipt.official_verified:
                    raise ControllerProblem(
                        "local_trust_escalation",
                        "The local controller cannot accept an official receipt.",
                        type(oci.lifecycle_receipt).__name__,
                        "Use local-self-attested OCI lifecycle evidence.",
                        request.scheduled.attempt.attempt_id,
                        True,
                        False,
                    )
                oci.lifecycle_receipt.validate_for_grading()
                machine.transition(
                    ControllerState.LIFECYCLE_VALIDATED,
                    evidence_digest=oci.lifecycle_receipt.receipt_digest,
                )

            if (
                infrastructure is InfrastructureStatus.OK
                and harness_status is HarnessStatus.COMPLETED
            ):
                machine.transition(ControllerState.GRADING)
                output_root = Path(
                    tempfile.mkdtemp(
                        prefix=f"atv-controller-{attempt_id[:12]}-"
                    )
                )
                reconstructed = output_root / "output"
                decode_output_snapshot(
                    oci.grader_stdout,
                    reconstructed,
                    output_contract=task_view.manifest["output"],
                )
                grader = self.grader_factory(task_view)
                grade = grader.grade(
                    task_view,
                    reconstructed,
                    lifecycle_receipt=oci.lifecycle_receipt,
                )
                machine.transition(
                    ControllerState.GRADED,
                    evidence_digest=grade.result_digest,
                )
                outcome = TrialOutcome(
                    trial_id=request.scheduled.spec.trial_id,
                    attempt_id=attempt_id,
                    infrastructure_status=InfrastructureStatus.OK,
                    harness_status=HarnessStatus.COMPLETED,
                    score=grade.score,
                )
            elif infrastructure is InfrastructureStatus.OK:
                outcome = TrialOutcome(
                    trial_id=request.scheduled.spec.trial_id,
                    attempt_id=attempt_id,
                    infrastructure_status=infrastructure,
                    harness_status=harness_status,
                    score=0.0,
                    reason_code=reason,
                )
            else:
                outcome = TrialOutcome(
                    trial_id=request.scheduled.spec.trial_id,
                    attempt_id=attempt_id,
                    infrastructure_status=infrastructure,
                    harness_status=harness_status,
                    score=None,
                    reason_code=reason,
                )
            machine.transition(
                ControllerState.OUTCOME_CLASSIFIED,
                evidence_digest=sha256_bytes(
                    _relaxed_json_bytes(outcome.to_dict())
                ),
            )
            bundle = self._bundle(
                request,
                outcome=outcome,
                grade=grade,
                output_tree=(output_root / "output") if grade is not None else None,
                oci=oci,
                problem=None,
            )
            machine.transition(
                ControllerState.BUNDLED,
                evidence_digest=bundle.digest,
            )
            exported = self._build_protocol_export(
                request,
                task=task_view,
                trial_request=trial_request,
                outcome=outcome,
                grade=grade,
                output_tree=(output_root / "output") if grade is not None else None,
                oci=oci,
                limitations=tuple(limitations),
            )
            machine.transition(
                ControllerState.EXPORTED,
                evidence_digest=canonical_digest(exported.bundle)["value"],
            )
            machine.transition(
                ControllerState.COMPLETED,
                evidence_digest=canonical_digest(exported.trial_result)["value"],
            )
        except ControllerProblem as exc:
            problem = exc
        except Exception as exc:
            problem = ControllerProblem(
                "controller_unexpected_failure",
                "The trial controller failed closed.",
                type(exc).__name__,
                "Inspect the evidence digest and repair the failing component.",
                attempt_id,
                True,
                True,
            )
        finally:
            if handle is not None and self.broker is not None:
                try:
                    self.broker.complete(handle)
                except Exception:
                    try:
                        self.broker.revoke(handle)
                    except Exception:
                        pass
            if output_root is not None:
                shutil.rmtree(output_root, ignore_errors=True)

        if problem is not None:
            if outcome is None:
                infrastructure = (
                    InfrastructureStatus.MODEL_GATEWAY_FAILED
                    if problem.code.startswith("model_gateway")
                    else InfrastructureStatus.GRADER_FAILED
                    if problem.code.startswith(("grader_", "trusted_grader"))
                    else InfrastructureStatus.RUNNER_FAILED
                )
                outcome = TrialOutcome(
                    trial_id=request.scheduled.spec.trial_id,
                    attempt_id=attempt_id,
                    infrastructure_status=infrastructure,
                    harness_status=HarnessStatus.NOT_RUN,
                    score=None,
                    reason_code=problem.code,
                )
                try:
                    machine.transition(
                        ControllerState.OUTCOME_CLASSIFIED,
                        evidence_digest=sha256_bytes(
                            _relaxed_json_bytes(outcome.to_dict())
                        ),
                    )
                except ControllerProblem:
                    pass
            if bundle is None:
                try:
                    bundle = self._bundle(
                        request,
                        outcome=outcome,
                        grade=None,
                        output_tree=None,
                        oci=oci,
                        problem=problem,
                    )
                    if machine.state is ControllerState.OUTCOME_CLASSIFIED:
                        machine.transition(
                            ControllerState.BUNDLED,
                            evidence_digest=bundle.digest,
                        )
                except Exception:
                    bundle = None
            if machine.state is not ControllerState.FAILED:
                if ControllerState.FAILED in _ALLOWED_TRANSITIONS[machine.state]:
                    machine.transition(
                        ControllerState.FAILED,
                        evidence_digest=(
                            bundle.digest if bundle is not None else None
                        ),
                        problem=problem,
                    )

        result = TrialControllerResult(
            request=request,
            state=machine.state,
            outcome=outcome,
            grade=grade,
            oci_result=oci,
            internal_bundle=bundle,
            protocol_export=exported,
            problem=problem,
            limitations=tuple(limitations),
            capability_lineage=capability_lineage,
            ledger_entries=self.ledger.entries_for(attempt_id),
        )
        self._results[attempt_id] = result
        return result

    def retry(
        self,
        previous: TrialControllerResult,
        *,
        fresh_nonce: str,
    ) -> TrialControllerResult:
        if not previous.retryable:
            raise ControllerProblem(
                "retry_not_allowed",
                "Only typed infrastructure failures may be retried.",
                (
                    previous.outcome.infrastructure_status.value
                    if previous.outcome is not None
                    else "no-outcome"
                ),
                "Fix harness failures instead of retrying them.",
                previous.request.scheduled.attempt.attempt_id,
                False,
                False,
            )
        current = previous.request.scheduled
        retried = current.retry(
            attempt_number=current.attempt.attempt_number + 1,
            fresh_nonce=fresh_nonce,
        )
        request = dataclasses.replace(
            previous.request,
            scheduled=retried,
            prior_attempt_ids=(
                *previous.request.prior_attempt_ids,
                current.attempt.attempt_id,
            ),
            retry_reason=previous.problem.code if previous.problem else "infrastructure",
        )
        return self.run(request)
