"""Private content-addressed storage used before canonical protocol export.

This module's internal manifest is not a publication or reproduction contract.
Public evidence must be exported and verified through ``atv.bundle/v1``.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ._canonical import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    TreeLimits,
    UnsafePathError,
    canonical_json_bytes,
    read_stable_confined_regular_file,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
    sha256_json,
    snapshot_regular_tree,
    tree_digest_from_snapshots,
)
from .grader import GradeResult
from .trial import (
    HarnessStatus,
    InfrastructureStatus,
    TrialAttempt,
    TrialOutcome,
    TrialSpec,
)


class BundleIntegrityError(RuntimeError):
    """Content-addressed evidence is missing, malformed, or tampered."""


_RESERVED_TRUST_CLAIMS = {
    "attested",
    "official",
    "official_verified",
    "trust_tier",
    "verified",
}


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker and checker())


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _directory_identity(st: os.stat_result) -> tuple[int, int, int]:
    return (int(st.st_dev), int(st.st_ino), int(stat.S_IFMT(st.st_mode)))


def _reject_unsafe_directory(st: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(st.st_mode) or _is_reparse(st):
        raise BundleIntegrityError(f"content store path is a link: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise BundleIntegrityError(f"content store path is not a directory: {path}")


def _ensure_directory_chain(
    path: Path,
) -> tuple[tuple[Path, tuple[int, int, int]], ...]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    rows: list[tuple[Path, tuple[int, int, int]]] = []
    for part in absolute.parts[1:]:
        current = current / part
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current)
            except FileExistsError:
                pass
            except OSError as exc:
                raise BundleIntegrityError(
                    f"cannot create content store directory: {current}"
                ) from exc
            try:
                current_stat = os.lstat(current)
            except OSError as exc:
                raise BundleIntegrityError(
                    f"content store directory disappeared: {current}"
                ) from exc
        except OSError as exc:
            raise BundleIntegrityError(
                f"content store directory is unreadable: {current}"
            ) from exc
        _reject_unsafe_directory(current_stat, current)
        rows.append((current, _directory_identity(current_stat)))
    if not rows:
        raise BundleIntegrityError("filesystem root cannot be a content store")
    _verify_directory_chain(tuple(rows))
    return tuple(rows)


def _verify_directory_chain(
    chain: tuple[tuple[Path, tuple[int, int, int]], ...],
) -> None:
    for path, expected in chain:
        try:
            observed = os.lstat(path)
        except OSError as exc:
            raise BundleIntegrityError(
                f"content store path changed or disappeared: {path}"
            ) from exc
        _reject_unsafe_directory(observed, path)
        if _directory_identity(observed) != expected:
            raise BundleIntegrityError(
                f"content store path was replaced: {path}"
            )


def _regular_identity(st: os.stat_result) -> tuple[int, int, int]:
    return (int(st.st_dev), int(st.st_ino), int(stat.S_IFMT(st.st_mode)))


def _reject_unsafe_object(st: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(st.st_mode) or _is_reparse(st):
        raise BundleIntegrityError(f"content object is a link: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise BundleIntegrityError(f"content object is not a regular file: {path}")
    if getattr(st, "st_nlink", 1) > 1:
        raise BundleIntegrityError(f"content object is a hardlink: {path}")


def _reject_reserved_trust_claims(value: Any, *, path: str = "runner_metadata") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _RESERVED_TRUST_CLAIMS:
                raise BundleIntegrityError(
                    f"{path}.{key} is a reserved trust claim"
                )
            _reject_reserved_trust_claims(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_reserved_trust_claims(item, path=f"{path}[{index}]")


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    path: str
    sha256: str
    size: int
    media_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "media_type": self.media_type,
        }


class ContentAddressedStore:
    """A sha256 object store with create-if-absent writes and read verification."""

    def __init__(
        self,
        root: Path | str,
        *,
        max_object_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        if (
            not isinstance(max_object_bytes, int)
            or isinstance(max_object_bytes, bool)
            or max_object_bytes <= 0
        ):
            raise ValueError("max_object_bytes must be a positive integer")
        self.max_object_bytes = max_object_bytes
        self.root = Path(os.path.abspath(os.fspath(root)))
        self._root_chain = _ensure_directory_chain(self.root)
        self.objects = self.root / "objects" / "sha256"
        self._objects_chain = _ensure_directory_chain(self.objects)
        self._verify_store_roots()

    def _verify_store_roots(self) -> None:
        _verify_directory_chain(self._root_chain)
        _verify_directory_chain(self._objects_chain)

    def _prefix_directory(
        self,
        digest: str,
        *,
        create: bool,
    ) -> tuple[Path, tuple[int, int, int]]:
        prefix = self.objects / digest[:2]
        self._verify_store_roots()
        try:
            prefix_stat = os.lstat(prefix)
        except FileNotFoundError:
            if not create:
                raise BundleIntegrityError(f"missing content object: {digest}")
            try:
                os.mkdir(prefix)
            except FileExistsError:
                pass
            except OSError as exc:
                raise BundleIntegrityError(
                    f"cannot create content object prefix: {prefix}"
                ) from exc
            try:
                prefix_stat = os.lstat(prefix)
            except OSError as exc:
                raise BundleIntegrityError(
                    f"content object prefix disappeared: {prefix}"
                ) from exc
        except OSError as exc:
            raise BundleIntegrityError(
                f"content object prefix is unreadable: {prefix}"
            ) from exc
        _reject_unsafe_directory(prefix_stat, prefix)
        self._verify_store_roots()
        return prefix, _directory_identity(prefix_stat)

    def _verify_prefix(
        self,
        prefix: Path,
        expected: tuple[int, int, int],
    ) -> None:
        self._verify_store_roots()
        try:
            observed = os.lstat(prefix)
        except OSError as exc:
            raise BundleIntegrityError(
                f"content object prefix changed: {prefix}"
            ) from exc
        _reject_unsafe_directory(observed, prefix)
        if _directory_identity(observed) != expected:
            raise BundleIntegrityError(
                f"content object prefix was replaced: {prefix}"
            )

    def object_path(self, digest: str) -> Path:
        require_sha256(digest, field="object digest")
        self._verify_store_roots()
        return self.objects / digest[:2] / digest[2:]

    def put_bytes(self, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise TypeError("content-addressed objects must be bytes")
        if len(data) > self.max_object_bytes:
            raise BundleIntegrityError(
                f"content object exceeds limit "
                f"({len(data)} > {self.max_object_bytes})"
            )
        digest = sha256_bytes(data)
        path = self.object_path(digest)
        prefix, prefix_identity = self._prefix_directory(digest, create=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        created_identity: tuple[int, int, int] | None = None
        try:
            descriptor = os.open(path, flags, 0o444)
        except FileExistsError:
            existing = self.read_bytes(digest)
            if existing != data:
                raise BundleIntegrityError(
                    f"existing object does not match its digest: {digest}"
                )
            return digest
        try:
            opened = os.fstat(descriptor)
            _reject_unsafe_object(opened, path)
            created_identity = _regular_identity(opened)
            try:
                linked = os.lstat(path)
            except OSError as exc:
                raise BundleIntegrityError(
                    "new content object disappeared before write"
                ) from exc
            _reject_unsafe_object(linked, path)
            if _regular_identity(linked) != created_identity:
                raise BundleIntegrityError(
                    "new content object path changed while opening"
                )
            self._verify_prefix(prefix, prefix_identity)

            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise BundleIntegrityError("content object write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            completed = os.fstat(descriptor)
            _reject_unsafe_object(completed, path)
            if (
                _regular_identity(completed) != created_identity
                or completed.st_size != len(data)
            ):
                raise BundleIntegrityError(
                    "content object changed while being written"
                )
            linked_after = os.lstat(path)
            _reject_unsafe_object(linked_after, path)
            if _regular_identity(linked_after) != created_identity:
                raise BundleIntegrityError(
                    "content object path was replaced while writing"
                )
            self._verify_prefix(prefix, prefix_identity)
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            if created_identity is not None:
                try:
                    observed = os.lstat(path)
                    if _regular_identity(observed) == created_identity:
                        os.unlink(path)
                except OSError:
                    pass
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if self.read_bytes(digest) != data:
            raise BundleIntegrityError("content object verification failed after write")
        return digest

    def put_file(self, path: Path | str) -> str:
        source = Path(os.path.abspath(os.fspath(path)))
        try:
            data = read_stable_confined_regular_file(
                source.parent,
                source.name,
                max_bytes=min(
                    self.max_object_bytes,
                    DEFAULT_MAX_FILE_BYTES,
                ),
            )
        except UnsafePathError as exc:
            raise BundleIntegrityError(str(exc)) from exc
        return self.put_bytes(data)

    def put_confined_file(
        self,
        root: Path,
        relative: str,
        *,
        max_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> str:
        try:
            data = read_stable_confined_regular_file(
                root,
                relative,
                max_bytes=min(max_bytes, self.max_object_bytes),
            )
        except UnsafePathError as exc:
            raise BundleIntegrityError(str(exc)) from exc
        return self.put_bytes(data)

    def read_bytes(self, digest: str) -> bytes:
        require_sha256(digest, field="object digest")
        prefix, prefix_identity = self._prefix_directory(digest, create=False)
        try:
            data = read_stable_confined_regular_file(
                self.objects,
                f"{digest[:2]}/{digest[2:]}",
                max_bytes=self.max_object_bytes,
            )
        except UnsafePathError as exc:
            raise BundleIntegrityError(str(exc)) from exc
        self._verify_prefix(prefix, prefix_identity)
        if sha256_bytes(data) != digest:
            raise BundleIntegrityError(f"content object was tampered: {digest}")
        return data


@dataclass(frozen=True, slots=True)
class TrialBundle:
    store: ContentAddressedStore
    digest: str
    _manifest_json: str

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    @property
    def manifest_path(self) -> Path:
        return self.store.object_path(self.digest)

    @classmethod
    def create(
        cls,
        store: ContentAddressedStore,
        *,
        spec: TrialSpec,
        attempt: TrialAttempt,
        outcome: TrialOutcome,
        grade: GradeResult | None,
        output_tree: Path | None,
        artifacts: Mapping[str, bytes | Path] | None = None,
        runner_metadata: Mapping[str, Any] | None = None,
        tree_limits: TreeLimits | None = None,
    ) -> "TrialBundle":
        if attempt.spec != spec:
            raise BundleIntegrityError("attempt does not bind the supplied TrialSpec")
        if outcome.trial_id != spec.trial_id:
            raise BundleIntegrityError("outcome trial_id does not match TrialSpec")
        if outcome.attempt_id != attempt.attempt_id:
            raise BundleIntegrityError("outcome attempt_id does not match TrialAttempt")
        if outcome.infrastructure_status is InfrastructureStatus.OK:
            if outcome.harness_status is HarnessStatus.COMPLETED:
                if grade is None or output_tree is None:
                    raise BundleIntegrityError(
                        "completed trials require a trusted grade and output tree"
                    )
                if grade.score != outcome.score:
                    raise BundleIntegrityError(
                        "outcome score does not match trusted grade"
                    )
            elif grade is not None:
                raise BundleIntegrityError(
                    "non-completed harness outcomes must not carry a grade"
                )
        elif grade is not None:
            raise BundleIntegrityError(
                "infrastructure failures must not carry a trusted grade"
            )

        records: list[ArtifactRecord] = []
        seen_paths: set[str] = set()

        if output_tree is not None:
            try:
                snapshots = snapshot_regular_tree(
                    Path(output_tree),
                    limits=tree_limits,
                )
            except UnsafePathError as exc:
                raise BundleIntegrityError(str(exc)) from exc
            observed_tree_digest = tree_digest_from_snapshots(snapshots)
            if grade is not None and observed_tree_digest != grade.output_tree_digest:
                raise BundleIntegrityError(
                    "output tree does not match the trusted grade"
                )
            for snapshot in snapshots:
                logical = f"output/{snapshot.path}"
                object_digest = store.put_bytes(snapshot.data)
                records.append(
                    ArtifactRecord(
                        path=logical,
                        sha256=object_digest,
                        size=snapshot.size,
                        media_type="application/octet-stream",
                    )
                )
                seen_paths.add(logical)

        for logical, source in sorted((artifacts or {}).items()):
            try:
                safe_relative_path(logical, field="artifact path")
            except UnsafePathError as exc:
                raise BundleIntegrityError(str(exc)) from exc
            if logical in seen_paths:
                raise BundleIntegrityError(f"duplicate artifact path: {logical}")
            if isinstance(source, bytes):
                data = source
                object_digest = store.put_bytes(data)
                size = len(data)
            elif isinstance(source, Path):
                object_digest = store.put_file(source)
                size = len(store.read_bytes(object_digest))
            else:
                raise TypeError("artifact values must be bytes or pathlib.Path")
            records.append(
                ArtifactRecord(
                    path=logical,
                    sha256=object_digest,
                    size=size,
                    media_type="application/octet-stream",
                )
            )
            seen_paths.add(logical)

        metadata = dict(runner_metadata or {})
        _reject_reserved_trust_claims(metadata)
        try:
            canonical_json_bytes(metadata)
        except (TypeError, ValueError) as exc:
            raise BundleIntegrityError("runner_metadata must be canonical JSON") from exc

        manifest = {
            "schema": "atv.internal-trial-bundle/v1",
            "trial": spec.to_dict(),
            "attempt": attempt.to_dict(),
            "outcome": outcome.to_dict(),
            "grade": grade.to_dict() if grade is not None else None,
            "runner_metadata": metadata,
            "artifacts": [
                record.to_dict()
                for record in sorted(records, key=lambda item: item.path)
            ],
        }
        manifest_bytes = canonical_json_bytes(manifest)
        digest = store.put_bytes(manifest_bytes)
        bundle = cls(
            store=store,
            digest=digest,
            _manifest_json=manifest_bytes.decode("utf-8"),
        )
        bundle.verify()
        return bundle

    @classmethod
    def load(
        cls,
        store: ContentAddressedStore,
        digest: str,
    ) -> "TrialBundle":
        require_sha256(digest, field="bundle digest")
        data = store.read_bytes(digest)
        try:
            manifest = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BundleIntegrityError("bundle manifest is not canonical JSON") from exc
        if canonical_json_bytes(manifest) != data:
            raise BundleIntegrityError("bundle manifest is not canonically encoded")
        bundle = cls(
            store=store,
            digest=digest,
            _manifest_json=data.decode("utf-8"),
        )
        bundle.verify()
        return bundle

    def verify(self) -> None:
        manifest_bytes = self.store.read_bytes(self.digest)
        if sha256_bytes(manifest_bytes) != self.digest:
            raise BundleIntegrityError("bundle digest does not match manifest")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BundleIntegrityError("bundle manifest is invalid JSON") from exc
        if canonical_json_bytes(manifest) != manifest_bytes:
            raise BundleIntegrityError("bundle manifest is not canonical JSON")
        if manifest.get("schema") != "atv.internal-trial-bundle/v1":
            raise BundleIntegrityError("unsupported bundle schema")
        if set(manifest) != {
            "schema",
            "trial",
            "attempt",
            "outcome",
            "grade",
            "runner_metadata",
            "artifacts",
        }:
            raise BundleIntegrityError("bundle manifest fields are invalid")

        trial = manifest["trial"]
        attempt = manifest["attempt"]
        outcome = manifest["outcome"]
        if trial.get("trial_id") != outcome.get("trial_id"):
            raise BundleIntegrityError("trial and outcome identifiers differ")
        if trial.get("trial_id") != sha256_json(
            {key: value for key, value in trial.items() if key != "trial_id"}
        ):
            raise BundleIntegrityError("TrialSpec identity is not reproducible")
        if not isinstance(attempt, dict) or set(attempt) != {
            "schema",
            "trial_id",
            "attempt_id",
            "attempt_number",
            "fresh_nonce",
            "workspace_id",
        }:
            raise BundleIntegrityError("trial attempt is malformed")
        if attempt.get("schema") != "atv.trial-attempt/v1":
            raise BundleIntegrityError("unsupported trial attempt schema")
        if attempt.get("trial_id") != trial.get("trial_id"):
            raise BundleIntegrityError("attempt does not bind the TrialSpec")
        try:
            require_sha256(attempt.get("fresh_nonce"), field="fresh nonce")
            require_sha256(attempt.get("attempt_id"), field="attempt id")
            require_sha256(attempt.get("workspace_id"), field="workspace id")
        except (TypeError, ValueError) as exc:
            raise BundleIntegrityError(str(exc)) from exc
        attempt_number = attempt.get("attempt_number")
        if (
            not isinstance(attempt_number, int)
            or isinstance(attempt_number, bool)
            or attempt_number <= 0
        ):
            raise BundleIntegrityError("attempt_number must be positive")
        expected_attempt_id = sha256_json(
            {
                "schema": "atv.trial-attempt/v1",
                "trial_id": trial["trial_id"],
                "attempt_number": attempt_number,
                "fresh_nonce": attempt["fresh_nonce"],
            }
        )
        if attempt["attempt_id"] != expected_attempt_id:
            raise BundleIntegrityError("trial attempt identity is not reproducible")
        expected_workspace_id = sha256_json(
            {
                "schema": "atv.fresh-workspace/v1",
                "attempt_id": attempt["attempt_id"],
            }
        )
        if attempt["workspace_id"] != expected_workspace_id:
            raise BundleIntegrityError("fresh workspace identity is not reproducible")
        if outcome.get("attempt_id") != attempt.get("attempt_id"):
            raise BundleIntegrityError("outcome does not bind the trial attempt")

        artifacts = manifest["artifacts"]
        if not isinstance(artifacts, list):
            raise BundleIntegrityError("artifacts must be a list")
        seen: set[str] = set()
        observed_paths: list[str] = []
        output_files: list[dict[str, Any]] = []
        for record in artifacts:
            if not isinstance(record, dict) or set(record) != {
                "path",
                "sha256",
                "size",
                "media_type",
            }:
                raise BundleIntegrityError("artifact record is malformed")
            try:
                safe_relative_path(record["path"], field="artifact path")
                require_sha256(record["sha256"], field="artifact digest")
            except (TypeError, ValueError, UnsafePathError) as exc:
                raise BundleIntegrityError(str(exc)) from exc
            if record["path"] in seen:
                raise BundleIntegrityError("duplicate artifact path")
            seen.add(record["path"])
            observed_paths.append(record["path"])
            data = self.store.read_bytes(record["sha256"])
            if len(data) != record["size"]:
                raise BundleIntegrityError("artifact size does not match content")
            if record["path"].startswith("output/"):
                output_files.append(
                    {
                        "path": record["path"][len("output/") :],
                        "size": record["size"],
                        "sha256": record["sha256"],
                    }
                )

        grade = manifest["grade"]
        infrastructure_status = outcome.get("infrastructure_status")
        harness_status = outcome.get("harness_status")
        valid_infrastructure = {status.value for status in InfrastructureStatus}
        valid_harness = {status.value for status in HarnessStatus}
        if infrastructure_status not in valid_infrastructure:
            raise BundleIntegrityError("unknown infrastructure status")
        if harness_status not in valid_harness:
            raise BundleIntegrityError("unknown harness status")
        if outcome.get("rankable") is not (
            infrastructure_status == InfrastructureStatus.OK.value
        ):
            raise BundleIntegrityError("rankable flag contradicts infrastructure status")
        if observed_paths != sorted(observed_paths):
            raise BundleIntegrityError("artifact records must be sorted by path")
        if infrastructure_status != InfrastructureStatus.OK.value:
            if outcome.get("score") is not None or grade is not None:
                raise BundleIntegrityError(
                    "infrastructure failure carries rankable evidence"
                )
        elif harness_status == HarnessStatus.COMPLETED.value:
            if not isinstance(grade, dict):
                raise BundleIntegrityError("completed trial is missing grade")
            grade_payload = {
                key: value for key, value in grade.items() if key != "result_digest"
            }
            if sha256_json(grade_payload) != grade.get("result_digest"):
                raise BundleIntegrityError("grade result digest is invalid")
            try:
                require_sha256(
                    grade.get("lifecycle_receipt_digest"),
                    field="lifecycle receipt digest",
                )
            except (TypeError, ValueError) as exc:
                raise BundleIntegrityError(str(exc)) from exc
            if grade.get("official_verified") is not (
                grade.get("trust_tier") == "official-attested"
            ):
                raise BundleIntegrityError(
                    "grade trust tier contradicts official_verified"
                )
            if grade.get("score") != outcome.get("score"):
                raise BundleIntegrityError("grade and outcome scores differ")
            reconstructed_tree_digest = sha256_json(
                {"files": sorted(output_files, key=lambda item: item["path"])}
            )
            if reconstructed_tree_digest != grade.get("output_tree_digest"):
                raise BundleIntegrityError("output artifacts do not match grade")
        else:
            if harness_status == HarnessStatus.NOT_RUN.value:
                raise BundleIntegrityError(
                    "infrastructure OK cannot leave the harness not_run"
                )
            if grade is not None:
                raise BundleIntegrityError(
                    "harness failure unexpectedly carries grade"
                )
            if outcome.get("score") != 0.0:
                raise BundleIntegrityError(
                    "rankable harness failure must carry score 0.0"
                )
