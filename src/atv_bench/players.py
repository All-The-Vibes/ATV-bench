"""Harness-driven CodeClash player lifecycle.

``iterative`` is the paper-faithful mode: every CodeClash edit phase starts a
fresh harness process/model context against the persistent codebase produced by
the prior round and consumes only the trusted competition logs that CodeClash
copied into ``/logs/rounds/<previous>``.

``frozen-artifact`` is an explicit non-adaptive control. It invokes the harness
once after a successful build and replays that artifact in later nested rounds.
Its cache is instance-local, so no artifact crosses a tournament trial.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol, runtime_checkable

from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    Budget,
    EvidenceSource,
    HarnessAdapter,
    Usage,
)
from atv_bench.adapters.snapshot import capture_diff, seed_base
from atv_bench.capture import scan_captured_tree

ADAPTATION_ITERATIVE = "iterative"
ADAPTATION_FROZEN = "frozen-artifact"
ADAPTATION_MODES = (ADAPTATION_ITERATIVE, ADAPTATION_FROZEN)
MAX_FEEDBACK_BYTES = 64 * 1024
MAX_PLAYER_TREE_FILES = 2_048
MAX_PLAYER_TREE_TOTAL_BYTES = 32 * 1024 * 1024
MAX_PLAYER_TREE_FILE_BYTES = 4 * 1024 * 1024
MAX_PLAYER_TREE_ENTRIES = 8_192
MAX_PLAYER_TREE_DIRECTORIES = 4_096
_PRIVATE_PATH_PARTS = {".git", "opponent_codebases", "players", "private"}
_CODE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".swift",
    ".ts",
    ".tsx",
}


@runtime_checkable
class TreeContainerLike(Protocol):
    """Minimal slice of CodeClash's persistent player environment."""

    def read_tree(self) -> dict[str, str]: ...
    def write_tree(self, files: dict[str, str]) -> None: ...


@dataclasses.dataclass(frozen=True, slots=True)
class FrozenArtifactIdentity:
    harness_manifest_digest: str
    harness_config_digest: str
    adapter_version: str
    model_policy_digest: str
    budget: Mapping[str, int]
    task_digest: str
    base_tree_digest: str
    prompt_digest: str
    player_id: str
    game: str
    protocol_version: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "atv.frozen-artifact-identity/v1",
            "harness_manifest_digest": self.harness_manifest_digest,
            "harness_config_digest": self.harness_config_digest,
            "adapter_version": self.adapter_version,
            "model_policy_digest": self.model_policy_digest,
            "budget": dict(self.budget),
            "task_digest": self.task_digest,
            "base_tree_digest": self.base_tree_digest,
            "prompt_digest": self.prompt_digest,
            "player_id": self.player_id,
            "game": self.game,
            "protocol_version": self.protocol_version,
        }

    @property
    def digest(self) -> str:
        return _json_digest(self.to_dict())


@dataclasses.dataclass(frozen=True, slots=True)
class CompetitionFeedback:
    previous_round: int | None
    files: Mapping[str, str]
    digest: str

    @property
    def present(self) -> bool:
        return bool(self.files)

    def prompt_text(self) -> str:
        if not self.files or self.previous_round is None:
            return ""
        rows = [
            "",
            f"Trusted competition feedback from round {self.previous_round}:",
            "These are arena-authored logs only. Opponent private code is not included.",
        ]
        for path, content in sorted(self.files.items()):
            rows.extend((f"--- {path} ---", content))
        return "\n".join(rows)


@dataclasses.dataclass(frozen=True, slots=True)
class RoundEvidence:
    round: int
    observation_unit: str
    adaptation: str
    fresh_harness_process: bool
    fresh_model_context: bool
    harness_memory_enabled: bool
    adapter: str
    adapter_version: str
    requested_model: str
    reported_model: str
    model_source: str
    model_verified: bool
    status: str
    usage: Mapping[str, object]
    runtime: Mapping[str, object]
    diff: str
    diff_sha256: str
    input_tree_sha256: str
    output_tree_sha256: str
    feedback_round: int | None
    feedback_sha256: str
    feedback_files: tuple[str, ...]
    frozen_identity_sha256: str | None
    replayed_from_round: int | None
    error: str | None

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class _FrozenArtifact:
    identity_sha256: str
    tree: Mapping[str, str]
    result: AdapterResult
    diff: str
    source_round: int


def clear_artifact_cache() -> None:
    """Compatibility hook.

    Frozen artifacts are intentionally instance-local. There is no process-wide
    cache to clear and no artifact can cross a tournament trial.
    """


class HarnessPlayerCore:
    def __init__(
        self,
        adapter: HarnessAdapter,
        container: TreeContainerLike,
        *,
        bot_file: str = "main.py",
        goal: str,
        model: str = "auto",
        budget: Budget | None = None,
        player_id: str | None = None,
        game: str = "lightcycles",
        prompt_version: str = "edit@1",
        adaptation: str = ADAPTATION_ITERATIVE,
        adapter_factory: Callable[[], HarnessAdapter] | None = None,
        adapter_version: str = "1.0.0",
        harness_manifest_digest: str = "0" * 64,
        harness_config_digest: str = "0" * 64,
        model_policy_digest: str = "0" * 64,
        task_digest: str = "0" * 64,
        prompt_digest: str | None = None,
        protocol_version: str = "atv.harness/v1",
        manifest_capabilities: Mapping[str, object] | None = None,
    ) -> None:
        if adaptation not in ADAPTATION_MODES:
            raise ValueError(
                f"adaptation must be one of {', '.join(ADAPTATION_MODES)}"
            )
        self.adapter = adapter
        self.adapter_factory = adapter_factory
        self.container = container
        self.bot_file = bot_file
        self.goal = goal
        self.model = model
        self.budget = budget or Budget()
        self.player_id = player_id or "anonymous-player"
        self.game = game
        self.prompt_version = prompt_version
        self.adaptation = adaptation
        self.adapter_version = adapter_version
        self.harness_manifest_digest = harness_manifest_digest
        self.harness_config_digest = harness_config_digest
        self.model_policy_digest = model_policy_digest
        self.task_digest = task_digest
        self.prompt_digest = prompt_digest or hashlib.sha256(
            goal.encode("utf-8")
        ).hexdigest()
        self.protocol_version = protocol_version
        self.manifest_capabilities = dict(manifest_capabilities or {})
        self.harness_memory_enabled = bool(
            self.manifest_capabilities.get("resumable", False)
        )
        self.last_result: AdapterResult | None = None
        self.last_diff = ""
        self.last_round_evidence: RoundEvidence | None = None
        self.round_evidence: list[RoundEvidence] = []
        self._initial_base_tree_digest: str | None = None
        self._frozen_artifact: _FrozenArtifact | None = None

    def frozen_identity(
        self,
        *,
        base_tree_digest: str | None = None,
    ) -> FrozenArtifactIdentity:
        base = (
            base_tree_digest
            or self._initial_base_tree_digest
            or _tree_digest(self.container.read_tree())
        )
        return FrozenArtifactIdentity(
            harness_manifest_digest=self.harness_manifest_digest,
            harness_config_digest=self.harness_config_digest,
            adapter_version=self.adapter_version,
            model_policy_digest=self.model_policy_digest,
            budget=self.budget.to_dict(),
            task_digest=self.task_digest,
            base_tree_digest=base,
            prompt_digest=self.prompt_digest,
            player_id=self.player_id,
            game=self.game,
            protocol_version=self.protocol_version,
        )

    def edit_turn(self, *, round_number: int | None = None) -> AdapterResult:
        """Execute or replay one nested CodeClash edit round."""

        round_number = round_number or len(self.round_evidence) + 1
        if round_number < 1:
            raise ValueError("round_number must be positive")
        original_tree = self.container.read_tree()
        input_digest = _tree_digest(original_tree)
        if self._initial_base_tree_digest is None:
            self._initial_base_tree_digest = input_digest
        feedback = self._read_feedback(round_number - 1)
        identity = self.frozen_identity()

        if (
            self.adaptation == ADAPTATION_FROZEN
            and self._frozen_artifact is not None
            and self._frozen_artifact.identity_sha256 == identity.digest
        ):
            frozen = self._frozen_artifact
            self.container.write_tree(dict(frozen.tree))
            evidence = self._round_record(
                round_number=round_number,
                result=frozen.result,
                diff=frozen.diff,
                input_digest=input_digest,
                output_digest=_tree_digest(frozen.tree),
                feedback=feedback,
                fresh_process=False,
                identity=identity,
                replayed_from_round=frozen.source_round,
                error=None,
            )
            self._remember(frozen.result, frozen.diff, evidence)
            return frozen.result

        result, diff, captured_tree, error = self._invoke_harness(
            original_tree=original_tree,
            feedback=feedback,
        )
        if result.status is AdapterStatus.OK:
            self.container.write_tree(captured_tree)
        output_tree = (
            captured_tree if result.status is AdapterStatus.OK else original_tree
        )
        evidence = self._round_record(
            round_number=round_number,
            result=result,
            diff=diff,
            input_digest=input_digest,
            output_digest=_tree_digest(output_tree),
            feedback=feedback,
            fresh_process=True,
            identity=identity if self.adaptation == ADAPTATION_FROZEN else None,
            replayed_from_round=None,
            error=error,
        )
        if self.adaptation == ADAPTATION_FROZEN and result.status is AdapterStatus.OK:
            self._frozen_artifact = _FrozenArtifact(
                identity_sha256=identity.digest,
                tree=dict(captured_tree),
                result=result,
                diff=diff,
                source_round=round_number,
            )
        self._remember(result, diff, evidence)
        return result

    def _invoke_harness(
        self,
        *,
        original_tree: dict[str, str],
        feedback: CompetitionFeedback,
    ) -> tuple[AdapterResult, str, dict[str, str], str | None]:
        adapter = self._adapter_for_invocation()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._materialize(repo, original_tree)
            subprocess.run(
                ["git", "init", "-q"],
                cwd=repo,
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "config", "core.autocrlf", "false"],
                cwd=repo,
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "config", "core.eol", "lf"],
                cwd=repo,
                check=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "add", "-A"],
                cwd=repo,
                check=True,
                timeout=30,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=a@b.c",
                    "-c",
                    "user.name=atv",
                    "commit",
                    "-qm",
                    "init",
                ],
                cwd=repo,
                check=True,
                timeout=30,
            )
            base = seed_base(repo)
            request = AdapterRequest(
                repo_path=str(repo),
                goal=self.goal + feedback.prompt_text(),
                model=self.model,
                budget=self.budget,
                bot_file=self.bot_file,
            )
            error: str | None = None
            try:
                result = adapter.run(request)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                result = AdapterResult(
                    status=AdapterStatus.ERROR,
                    diff="",
                    log=error,
                    usage=Usage(source=EvidenceSource.UNAVAILABLE),
                    model=self.model,
                    model_source=EvidenceSource.HARNESS_REPORTED,
                    model_verified=False,
                )
            diff = capture_diff(repo, base)
            captured_tree = original_tree
            if result.status is AdapterStatus.OK:
                scan_captured_tree(
                    repo,
                    max_files=MAX_PLAYER_TREE_FILES,
                    max_total_bytes=MAX_PLAYER_TREE_TOTAL_BYTES,
                    max_file_bytes=MAX_PLAYER_TREE_FILE_BYTES,
                    max_entries=MAX_PLAYER_TREE_ENTRIES,
                    max_directories=MAX_PLAYER_TREE_DIRECTORIES,
                    allowed_text_suffixes=None,
                )
                captured_tree = self._read_repo_tree(repo)
            return result, diff, captured_tree, error

    def _adapter_for_invocation(self) -> HarnessAdapter:
        if self.adapter_factory is None:
            return self.adapter
        if self.harness_memory_enabled:
            return self.adapter
        return self.adapter_factory()

    def _read_feedback(self, previous_round: int) -> CompetitionFeedback:
        reader = getattr(self.container, "read_feedback", None)
        raw = reader(previous_round) if callable(reader) else {}
        accepted: dict[str, str] = {}
        total = 0
        for path, content in sorted(dict(raw or {}).items()):
            portable = PurePosixPath(str(path).replace("\\", "/"))
            if portable.is_absolute() or ".." in portable.parts:
                continue
            lowered = {part.lower() for part in portable.parts}
            if lowered & _PRIVATE_PATH_PARTS or portable.suffix.lower() in _CODE_SUFFIXES:
                continue
            encoded = str(content).encode("utf-8", errors="replace")
            if total + len(encoded) > MAX_FEEDBACK_BYTES:
                break
            total += len(encoded)
            accepted[portable.as_posix()] = str(content)
        digest = _json_digest({"round": previous_round, "files": accepted})
        return CompetitionFeedback(
            previous_round=previous_round if accepted else None,
            files=accepted,
            digest=digest,
        )

    def _round_record(
        self,
        *,
        round_number: int,
        result: AdapterResult,
        diff: str,
        input_digest: str,
        output_digest: str,
        feedback: CompetitionFeedback,
        fresh_process: bool,
        identity: FrozenArtifactIdentity | None,
        replayed_from_round: int | None,
        error: str | None,
    ) -> RoundEvidence:
        return RoundEvidence(
            round=round_number,
            observation_unit="nested-round",
            adaptation=self.adaptation,
            fresh_harness_process=fresh_process,
            fresh_model_context=fresh_process,
            harness_memory_enabled=self.harness_memory_enabled,
            adapter=getattr(self.adapter, "name", type(self.adapter).__name__),
            adapter_version=self.adapter_version,
            requested_model=self.model,
            reported_model=result.model,
            model_source=result.model_source.value,
            model_verified=result.model_verified,
            status=result.status.value,
            usage=result.usage.to_dict(),
            runtime=result.runtime.to_dict(),
            diff=diff,
            diff_sha256=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            input_tree_sha256=input_digest,
            output_tree_sha256=output_digest,
            feedback_round=feedback.previous_round,
            feedback_sha256=feedback.digest,
            feedback_files=tuple(sorted(feedback.files)),
            frozen_identity_sha256=identity.digest if identity else None,
            replayed_from_round=replayed_from_round,
            error=error,
        )

    def _remember(
        self,
        result: AdapterResult,
        diff: str,
        evidence: RoundEvidence,
    ) -> None:
        self.last_result = result
        self.last_diff = diff
        self.last_round_evidence = evidence
        self.round_evidence.append(evidence)

    def _materialize(self, repo: Path, tree: Mapping[str, str]) -> None:
        if not tree:
            (repo / self.bot_file).parent.mkdir(parents=True, exist_ok=True)
            (repo / self.bot_file).write_text("")
            return
        for rel, content in tree.items():
            dest = repo / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content.encode("utf-8"))

    def _read_repo_tree(self, repo: Path) -> dict[str, str]:
        from atv_bench.capture import _IGNORED_DIR_PARTS, _IGNORED_SUFFIXES

        out: dict[str, str] = {}
        for path in sorted(repo.rglob("*")):
            rel_parts = path.relative_to(repo).parts
            if any(part in _IGNORED_DIR_PARTS for part in rel_parts):
                continue
            if not path.is_file() or path.suffix.lower() in _IGNORED_SUFFIXES:
                continue
            try:
                out[path.relative_to(repo).as_posix()] = path.read_bytes().decode(
                    "utf-8"
                )
            except (UnicodeDecodeError, OSError):
                continue
        return out


def _tree_digest(tree: Mapping[str, str]) -> str:
    return _json_digest({"files": dict(sorted(tree.items()))})


def _json_digest(value: Mapping[str, object]) -> str:
    blob = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
