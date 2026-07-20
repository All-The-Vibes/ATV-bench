"""Load and validate the versioned protocol schemas."""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

try:  # jsonschema >= 4.18
    from referencing import Registry, Resource
except ImportError:  # pragma: no cover - exercised by the compatibility subprocess test
    Registry = None  # type: ignore[assignment]
    Resource = None  # type: ignore[assignment]

from .canonical import strict_json_loads
from .errors import SchemaLoadError, SchemaValidationError


class SchemaKind(str, Enum):
    HARNESS = "atv.harness/v1"
    TASK = "atv.task/v1"
    TRIAL_REQUEST = "atv.trial-request/v1"
    EVENT = "atv.event/v1"
    TRIAL_RESULT = "atv.trial-result/v1"
    BUNDLE = "atv.bundle/v1"


SCHEMA_FILE_NAMES: dict[SchemaKind, str] = {
    SchemaKind.HARNESS: "atv.harness.v1.schema.json",
    SchemaKind.TASK: "atv.task.v1.schema.json",
    SchemaKind.TRIAL_REQUEST: "atv.trial-request.v1.schema.json",
    SchemaKind.EVENT: "atv.event.v1.schema.json",
    SchemaKind.TRIAL_RESULT: "atv.trial-result.v1.schema.json",
    SchemaKind.BUNDLE: "atv.bundle.v1.schema.json",
}
COMMON_SCHEMA_FILE = "atv.common.v1.schema.json"


def _format_path(error: ValidationError) -> str:
    path = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _absolutize_local_refs(value: Any, base_uri: str) -> Any:
    """Make local refs usable by jsonschema 4.0's legacy resolver."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str) and item.startswith("#"):
                result[key] = base_uri + item
            else:
                result[key] = _absolutize_local_refs(item, base_uri)
        return result
    if isinstance(value, list):
        return [_absolutize_local_refs(item, base_uri) for item in value]
    return value


def discover_schema_directory() -> Path:
    configured = os.environ.get("ATV_BENCH_SCHEMA_DIR")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    module_path = Path(__file__).resolve()
    if len(module_path.parents) > 3:
        candidates.append(module_path.parents[3] / "schemas")
    try:
        packaged = resources.files("atv_bench.protocol.schemas")
        candidates.append(Path(str(packaged)))
    except (ModuleNotFoundError, TypeError):
        pass
    candidates.append(Path.cwd() / "schemas")
    for candidate in candidates:
        if (candidate / COMMON_SCHEMA_FILE).is_file():
            return candidate.resolve()
    rendered = ", ".join(str(path) for path in candidates)
    raise SchemaLoadError(
        "ATV protocol schemas were not found; checked: "
        f"{rendered}. Set ATV_BENCH_SCHEMA_DIR to the schema directory."
    )


@dataclass(frozen=True)
class SchemaStore:
    directory: Path
    documents_by_id: Mapping[str, Mapping[str, Any]]
    documents_by_kind: Mapping[SchemaKind, Mapping[str, Any]]
    registry: Any | None

    @classmethod
    def from_directory(cls, directory: str | Path) -> "SchemaStore":
        root = Path(directory).resolve()
        file_names = [COMMON_SCHEMA_FILE, *SCHEMA_FILE_NAMES.values()]
        texts: dict[str, str] = {}
        for file_name in file_names:
            path = root / file_name
            if not path.is_file():
                raise SchemaLoadError(f"required schema file is missing: {path}")
            try:
                texts[file_name] = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeError) as exc:
                raise SchemaLoadError(f"cannot read schema {path}: {exc}") from exc
        return cls.from_texts(texts, directory=root)

    @classmethod
    def from_texts(
        cls,
        texts: Mapping[str, str],
        *,
        directory: str | Path = "<embedded>",
    ) -> "SchemaStore":
        root = Path(directory)
        file_names = [COMMON_SCHEMA_FILE, *SCHEMA_FILE_NAMES.values()]
        documents_by_id: dict[str, Mapping[str, Any]] = {}
        documents_by_kind: dict[SchemaKind, Mapping[str, Any]] = {}
        documents_by_file: dict[str, Mapping[str, Any]] = {}
        schema_resources: list[tuple[str, Any]] = []

        for file_name in file_names:
            text = texts.get(file_name)
            if text is None:
                raise SchemaLoadError(f"required schema text is missing: {file_name}")
            document = strict_json_loads(text)
            if not isinstance(document, dict):
                raise SchemaLoadError(
                    f"schema file is not a JSON object: {file_name}"
                )
            schema_id = document.get("$id")
            if not isinstance(schema_id, str) or not schema_id:
                raise SchemaLoadError(
                    f"schema file has no non-empty $id: {file_name}"
                )
            if schema_id in documents_by_id:
                raise SchemaLoadError(f"duplicate schema $id {schema_id!r}")
            try:
                Draft202012Validator.check_schema(document)
            except (SchemaError, ValueError) as exc:
                raise SchemaLoadError(
                    f"invalid schema {file_name}: {exc}"
                ) from exc
            resource = (
                Resource.from_contents(document)
                if Resource is not None
                else None
            )
            documents_by_id[schema_id] = document
            documents_by_file[file_name] = document
            if resource is not None:
                schema_resources.append((schema_id, resource))

        for kind, file_name in SCHEMA_FILE_NAMES.items():
            documents_by_kind[kind] = documents_by_file[file_name]

        registry = (
            Registry().with_resources(schema_resources)
            if Registry is not None
            else None
        )
        return cls(
            directory=root,
            documents_by_id=documents_by_id,
            documents_by_kind=documents_by_kind,
            registry=registry,
        )

    def schema(self, kind: SchemaKind | str) -> Mapping[str, Any]:
        normalized = kind if isinstance(kind, SchemaKind) else SchemaKind(kind)
        return self.documents_by_kind[normalized]

    def validate(self, instance: Any, kind: SchemaKind | str) -> None:
        normalized = kind if isinstance(kind, SchemaKind) else SchemaKind(kind)
        schema = self.schema(normalized)
        if self.registry is not None:
            validator = Draft202012Validator(
                schema,
                registry=self.registry,
                format_checker=FormatChecker(),
            )
        else:  # jsonschema 4.0-4.17 compatibility
            from jsonschema import RefResolver

            legacy_store = {
                uri: _absolutize_local_refs(document, uri)
                for uri, document in self.documents_by_id.items()
            }
            schema_id = schema["$id"]
            legacy_schema = legacy_store[schema_id]
            validator = Draft202012Validator(
                legacy_schema,
                resolver=RefResolver.from_schema(
                    legacy_schema,
                    store=legacy_store,
                ),
                format_checker=FormatChecker(),
            )
        errors = sorted(
            validator.iter_errors(instance),
            key=lambda item: (
                tuple(str(part) for part in item.absolute_path),
                item.message,
            ),
        )
        if errors:
            first = errors[0]
            path = _format_path(first)
            message = first.message
            if first.validator in {"additionalProperties", "unevaluatedProperties"}:
                message = f"unknown fields: {message}"
            raise SchemaValidationError(
                f"{normalized.value} validation failed at {path}: {message}",
                path=path,
            )

    def validate_inferred(self, instance: Any) -> SchemaKind:
        if not isinstance(instance, dict):
            raise SchemaValidationError("schema document must be a JSON object")
        schema_name = instance.get("schema")
        try:
            kind = SchemaKind(schema_name)
        except (TypeError, ValueError) as exc:
            raise SchemaValidationError(
                f"unknown or missing protocol schema identifier: {schema_name!r}"
            ) from exc
        self.validate(instance, kind)
        return kind


@lru_cache(maxsize=4)
def default_schema_store(directory: str | None = None) -> SchemaStore:
    if directory:
        return SchemaStore.from_directory(Path(directory).resolve())
    try:
        root = discover_schema_directory()
    except SchemaLoadError:
        from ._embedded_schemas import embedded_schema_texts

        return SchemaStore.from_texts(embedded_schema_texts())
    return SchemaStore.from_directory(root)
