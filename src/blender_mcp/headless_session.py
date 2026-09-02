"""Checked, file-backed strict-headless Blender scene sessions."""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import math
import os
import shutil
import stat as statmod
import tempfile
import uuid
import zlib
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from blender_mcp.utils.blender_executor import (
    StrictBlenderExecutor,
    StrictExecutionError,
    StrictFailureKind,
    _await_uninterruptibly,
    plan_strict_command,
)

WIRE_VERSION = "blender-headless-session/v1"
SENTINEL_PREFIX = "BLENDER_HEADLESS_SESSION_V1:"
MAX_PROGRAM_BYTES = 1024 * 1024
# Preserve the native callable identities for capability detection; tests and
# race regressions may wrap os.rename without changing platform support.
_NATIVE_OPEN = os.open
_NATIVE_RENAME = os.rename


class ErrorCode(StrEnum):
    """Closed set of provider errors exposed at the MCP boundary."""

    INVALID_REQUEST = "InvalidRequest"
    BLENDER_EXECUTABLE_MISSING = "BlenderExecutableMissing"
    SCENE_NOT_INITIALIZED = "SceneNotInitialized"
    UNSUPPORTED_IMPORT_FORMAT = "UnsupportedImportFormat"
    UNSUPPORTED_EXPORT_FORMAT = "UnsupportedExportFormat"
    INVALID_ASSET_PATH = "InvalidAssetPath"
    INVALID_OUTPUT_PATH = "InvalidOutputPath"
    BLENDER_EXITED_NONZERO = "BlenderExitedNonZero"
    BLENDER_TIMED_OUT = "BlenderTimedOut"
    BLENDER_CANCELLED = "BlenderCancelled"
    RESULT_SENTINEL_MISSING = "ResultSentinelMissing"
    RESULT_MALFORMED = "ResultMalformed"
    SCENE_REVISION_MISSING = "SceneRevisionMissing"
    ACTIVE_CAMERA_MISSING = "ActiveCameraMissing"
    PREVIEW_MISSING = "PreviewMissing"
    EXPORT_MISSING = "ExportMissing"
    EXECUTE_CODE_GUARD_UNAVAILABLE = "ExecuteCodeGuardUnavailable"
    BLENDER_LIFECYCLE_FAILURE = "BlenderLifecycleFailure"


class ProviderError(Exception):
    """Expected, structured provider failure."""

    def __init__(self, code: ErrorCode, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class HeadlessBpyProgram:
    """Admitted Python source, checked before it can enter a provider script."""

    source: str


# These are deliberately narrow.  The source admission check is defense in
# depth, while this table is the capability actually handed to author code.
_SAFE_IMPORT_ROOTS = frozenset({"bpy", "mathutils", "math", "json"})
_SAFE_BUILTIN_NAMES = (
    "__build_class__",
    "abs",
    "all",
    "any",
    "ascii",
    "bin",
    "bool",
    "bytes",
    "bytearray",
    "callable",
    "chr",
    "classmethod",
    "complex",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "hash",
    "hex",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "memoryview",
    "min",
    "next",
    "object",
    "oct",
    "ord",
    "pow",
    "print",
    "property",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "staticmethod",
    "str",
    "sum",
    "super",
    "tuple",
    "type",
    "zip",
)


class _ForbiddenSource(RuntimeError):
    """Internal marker used while walking the admitted source tree."""


def _source_error(message: str) -> ProviderError:
    return ProviderError(ErrorCode.INVALID_REQUEST, message)


def _reject_unsafe_source(tree: ast.AST) -> None:
    """Reject syntax that could recover host or interpreter capabilities.

    This is intentionally conservative: safe bpy authoring does not require
    reflection, dynamic code, filesystem access, process APIs, or GUI APIs.
    In particular, rejecting the attribute itself closes aliasing paths such
    as ``f = __builtins__[\"__import__\"]`` in addition to direct calls.
    """
    forbidden_modules = {
        "builtins",
        "sys",
        "importlib",
        "runpy",
        "os",
        "subprocess",
        "multiprocessing",
        "ctypes",
        "socket",
        "tkinter",
        "_tkinter",
        "pygame",
        "pyautogui",
        "Xlib",
        "wx",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
    }
    forbidden_names = {
        "__builtins__",
        "__import__",
        "builtins",
        "sys",
        "importlib",
        "runpy",
        "os",
        "subprocess",
        "multiprocessing",
        "ctypes",
        "socket",
        "eval",
        "exec",
        "compile",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
        "dir",
        "help",
        "input",
        "open",
    }
    forbidden_attributes = {
        "__builtins__",
        "__class__",
        "__dict__",
        "__getattr__",
        "__getattribute__",
        "__globals__",
        "__loader__",
        "__module__",
        "__name__",
        "__subclasses__",
        "__spec__",
        "__traceback__",
        "tb_frame",
        "tb_next",
        "f_back",
        "f_builtins",
        "f_globals",
        "gi_frame",
        "cr_frame",
        "ag_frame",
        "compile",
        "eval",
        "exec",
        "getattr",
        "setattr",
        "delattr",
        "import_module",
        "exec_module",
        "module_from_spec",
        "spec_from_file_location",
        "run_path",
        "run_module",
        "python_file_run",
        "execfile",
        "load_scripts",
        "as_module",
        "addon_install",
        "addon_enable",
        "addon_disable",
        "addon_remove",
        "save_as_mainfile",
        "save_mainfile",
        "execv",
        "execve",
        "execl",
        "execlp",
        "execle",
        "execlpe",
        "execvp",
        "execvpe",
        "spawnl",
        "spawnlp",
        "spawnv",
        "spawnvp",
        "fork",
        "forkpty",
        "system",
        "popen",
        "window_new",
        "window_open",
        "window_new_main",
        "window_duplicate",
        "userpref_show",
        "quit_blender",
        "open_mainfile",
        "read_factory_settings",
        "window_manager",
        "windows",
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [(node.module or "")]
            for name in names:
                root = name.split(".", 1)[0]
                if root in forbidden_modules:
                    raise _ForbiddenSource("process, display, and socket modules are not admitted")
                if name not in _SAFE_IMPORT_ROOTS:
                    raise _ForbiddenSource(f"module '{name}' is not admitted")
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*" or alias.name.startswith("_") or alias.name in forbidden_names:
                        raise _ForbiddenSource(f"imported name '{alias.name}' is not admitted")
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            raise _ForbiddenSource(f"unsafe builtin '{node.id}' is not admitted")
        if isinstance(node, ast.Attribute) and (
            node.attr in forbidden_attributes or node.attr.startswith("_")
        ):
            raise _ForbiddenSource(f"unsafe attribute '{node.attr}' is not admitted")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_names:
                raise _ForbiddenSource(f"unsafe builtin '{node.func.id}' is not admitted")
            if isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_attributes:
                raise _ForbiddenSource(f"unsafe attribute '{node.func.attr}' is not admitted")



@dataclass(frozen=True)
class PreviewSize:
    pixels: int


@dataclass(frozen=True)
class GetSceneInfoRequest:
    """Checked empty request for scene info."""


@dataclass(frozen=True)
class ExecuteCodeRequest:
    program: HeadlessBpyProgram


@dataclass(frozen=True)
class CameraRenderPreviewRequest:
    size: PreviewSize


@dataclass(frozen=True)
class ImportAssetRequest:
    path: Path
    format: ImportFormat


@dataclass(frozen=True)
class ExportSceneRequest:
    path: Path
    format: ExportFormat
    selection_only: bool


class ImportFormat(StrEnum):
    GLB = "glb"
    GLTF = "gltf"
    OBJ = "obj"
    FBX = "fbx"
    USD = "usd"
    USDC = "usdc"
    USDZ = "usdz"


class ExportFormat(StrEnum):
    BLEND = "blend"
    GLB = "glb"
    GLTF = "gltf"
    OBJ = "obj"
    FBX = "fbx"
    USD = "usd"
    USDC = "usdc"


class ObjectKind(StrEnum):
    MESH = "MESH"
    CURVE = "CURVE"
    SURFACE = "SURFACE"
    META = "META"
    FONT = "FONT"
    ARMATURE = "ARMATURE"
    LATTICE = "LATTICE"
    EMPTY = "EMPTY"
    GPENCIL = "GPENCIL"
    GREASEPENCIL = "GREASEPENCIL"
    CAMERA = "CAMERA"
    LIGHT = "LIGHT"
    LIGHT_PROBE = "LIGHT_PROBE"
    SPEAKER = "SPEAKER"
    VOLUME = "VOLUME"
    POINTCLOUD = "POINTCLOUD"
    CURVES = "CURVES"
    HAIR = "HAIR"


@dataclass(frozen=True)
class SceneObject:
    name: str
    type: ObjectKind
    location: tuple[float, float, float]
    rotation_euler: tuple[float, float, float]
    scale: tuple[float, float, float]
    visible: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type.value,
            "location": list(self.location),
            "rotation_euler": list(self.rotation_euler),
            "scale": list(self.scale),
            "visible": self.visible,
        }


@dataclass(frozen=True)
class SceneInfo:
    scene_name: str
    scene_revision: int
    frame_current: int
    frame_start: int
    frame_end: int
    active_camera: str | None
    objects: tuple[SceneObject, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_name": self.scene_name,
            "scene_revision": self.scene_revision,
            "frame_current": self.frame_current,
            "frame_start": self.frame_start,
            "frame_end": self.frame_end,
            "active_camera": self.active_camera,
            "objects": [item.to_dict() for item in self.objects],
        }


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ProviderError(ErrorCode.INVALID_REQUEST, f"{field} must be a string", details={"field": field})
    return value


def admit_headless_bpy_program(value: Any) -> HeadlessBpyProgram:
    """Parse and admit the narrow source language used by execute-code."""
    code = _require_string(value, "code")
    if not code.strip():
        raise ProviderError(ErrorCode.INVALID_REQUEST, "code must not be empty", details={"field": "code"})
    if len(code.encode("utf-8")) > MAX_PROGRAM_BYTES:
        raise ProviderError(
            ErrorCode.INVALID_REQUEST,
            f"code exceeds the {MAX_PROGRAM_BYTES}-byte limit",
            details={"field": "code", "max_bytes": MAX_PROGRAM_BYTES},
        )
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise ProviderError(
            ErrorCode.INVALID_REQUEST,
            "code is not valid Python",
            details={"field": "code", "line": exc.lineno, "offset": exc.offset},
        ) from exc

    try:
        _reject_unsafe_source(tree)
    except _ForbiddenSource as exc:
        raise _source_error(str(exc)) from exc

    return HeadlessBpyProgram(code)


parse_headless_code = admit_headless_bpy_program


def parse_preview_size(value: Any) -> PreviewSize:
    if isinstance(value, bool) or not isinstance(value, int) or not 64 <= value <= 2048:
        raise ProviderError(
            ErrorCode.INVALID_REQUEST,
            "max_size must be an integer between 64 and 2048",
            details={"field": "max_size", "minimum": 64, "maximum": 2048},
        )
    return PreviewSize(value)


def parse_import_format(value: Any) -> ImportFormat:
    value = _require_string(value, "format")
    try:
        return ImportFormat(value.lower())
    except ValueError as exc:
        raise ProviderError(ErrorCode.UNSUPPORTED_IMPORT_FORMAT, f"unsupported import format: {value}") from exc


def parse_export_format(value: Any) -> ExportFormat:
    value = _require_string(value, "format")
    try:
        return ExportFormat(value.lower())
    except ValueError as exc:
        raise ProviderError(ErrorCode.UNSUPPORTED_EXPORT_FORMAT, f"unsupported export format: {value}") from exc


def _absolute_path(value: Any, field: str, error: ErrorCode) -> Path:
    raw = _require_string(value, field)
    if not raw or "\x00" in raw:
        raise ProviderError(error, f"{field} must be an absolute path", details={"field": field})
    path = Path(raw)
    if not path.is_absolute():
        raise ProviderError(error, f"{field} must be an absolute path", details={"field": field})
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise ProviderError(error, f"invalid {field}", details={"field": field}) from exc


def _resolve_roots(
    roots: Iterable[str | Path] | None,
    environment_name: str,
    error: ErrorCode,
) -> tuple[Path, ...]:
    """Resolve configured roots once and reject missing/ambiguous roots."""
    if roots is None:
        raw = os.environ.get(environment_name, "")
        if not raw:
            return ()
        values: Iterable[str | Path] = raw.split(os.pathsep)
    else:
        values = roots
    resolved: list[Path] = []
    for value in values:
        if isinstance(value, Path):
            raw_value = str(value)
        elif isinstance(value, str):
            raw_value = value
        else:
            raise ProviderError(error, "configured path roots must be strings")
        if not raw_value or "\x00" in raw_value or not Path(raw_value).is_absolute():
            raise ProviderError(error, "configured path roots must be absolute")
        try:
            root = Path(raw_value).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProviderError(error, f"configured root does not exist: {raw_value}") from exc
        if not root.is_dir():
            raise ProviderError(error, f"configured root is not a directory: {root}")
        if root not in resolved:
            resolved.append(root)
    return tuple(resolved)


@dataclass
class _DirectoryCapability:
    """Held directory descriptor used for race-free path operations."""

    path: Path
    fd: int | None

    def close(self) -> None:
        if self.fd is not None:
            with suppress(OSError):
                os.close(self.fd)
            self.fd = None


def _safe_dirfd_operations_available() -> bool:
    """Whether this host can perform the required no-follow *at operations."""
    supported = getattr(os, "supports_dir_fd", set())
    return bool(
        getattr(os, "O_NOFOLLOW", 0)
        and getattr(os, "O_DIRECTORY", 0)
        and _NATIVE_OPEN in supported
        and _NATIVE_RENAME in supported
    )


def _directory_capabilities(roots: tuple[Path, ...], error: ErrorCode) -> tuple[_DirectoryCapability, ...]:
    """Open configured roots once; later operations never reopen by pathname."""
    if roots and not _safe_dirfd_operations_available():
        raise ProviderError(error, "this platform lacks safe descriptor-relative path operations")
    capabilities: list[_DirectoryCapability] = []
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for root in roots:
        try:
            capabilities.append(_DirectoryCapability(root, os.open(root, directory_flags)))
        except OSError as exc:
            for capability in capabilities:
                capability.close()
            raise ProviderError(error, f"configured root cannot be opened safely: {root}") from exc
    return tuple(capabilities)


def _relative_to_capability(path: Path, capability: _DirectoryCapability, error: ErrorCode) -> tuple[str, ...]:
    try:
        relative = path.relative_to(capability.path)
    except ValueError as exc:
        raise ProviderError(error, f"path is outside configured roots: {path}") from exc
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ProviderError(error, f"path is not a regular descendant: {path}")
    return parts


def _open_beneath(capability: _DirectoryCapability, path: Path, flags: int, error: ErrorCode) -> int:
    """Open a path below a held root without following any component symlink."""
    parts = _relative_to_capability(path, capability, error)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if capability.fd is None:
        raise ProviderError(error, "configured path capability is closed")
    directory_fd: int | None = None
    try:
        directory_fd = os.dup(capability.fd)
        try:
            for part in parts[:-1]:
                child_fd = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
            result = os.open(parts[-1], flags | nofollow, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        stat = os.fstat(result)
    except (OSError, ValueError, TypeError) as exc:
        if directory_fd is not None:
            with suppress(OSError):
                os.close(directory_fd)
        raise ProviderError(error, f"path cannot be opened safely: {path}") from exc
    if not statmod.S_ISREG(stat.st_mode):
        with suppress(OSError):
            os.close(result)
        raise ProviderError(error, f"path is not a regular file: {path}")
    return result


def _copy_fd_to_path(source_fd: int, destination: Path, error: ErrorCode) -> None:
    """Copy bytes from an already-open source into a private exclusive file."""
    try:
        source = os.fdopen(source_fd, "rb", closefd=True)
        try:
            with destination.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        finally:
            source.close()
    except OSError as exc:
        with suppress(OSError):
            os.close(source_fd)
        raise ProviderError(error, f"could not copy input into the private workspace: {destination}") from exc


def _contained_path(path: Path, roots: tuple[Path, ...], error: ErrorCode) -> Path:
    """Return a canonical path only when it is strictly inside a root."""
    if not roots:
        raise ProviderError(error, "no approved path roots are configured")
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProviderError(error, f"cannot resolve path safely: {path}") from exc
    for root in roots:
        try:
            canonical.relative_to(root)
        except ValueError:
            continue
        return canonical
    raise ProviderError(error, f"path is outside configured roots: {path}")


def parse_asset_path(value: Any, roots: Iterable[str | Path] | None = None) -> Path:
    path = _absolute_path(value, "path", ErrorCode.INVALID_ASSET_PATH)
    configured = _resolve_roots(roots, "BLENDER_MCP_STAGED_ASSET_ROOTS", ErrorCode.INVALID_ASSET_PATH)
    path = _contained_path(path, configured, ErrorCode.INVALID_ASSET_PATH)
    if not path.is_file() or path.is_symlink() or not os.path.isfile(path):
        raise ProviderError(ErrorCode.INVALID_ASSET_PATH, f"asset is not an existing regular file: {path}")
    return path


def parse_output_path(value: Any, roots: Iterable[str | Path] | None = None) -> Path:
    path = _absolute_path(value, "path", ErrorCode.INVALID_OUTPUT_PATH)
    configured = _resolve_roots(roots, "BLENDER_MCP_APPROVED_OUTPUT_ROOTS", ErrorCode.INVALID_OUTPUT_PATH)
    path = _contained_path(path, configured, ErrorCode.INVALID_OUTPUT_PATH)
    if not path.parent.is_dir():
        raise ProviderError(ErrorCode.INVALID_OUTPUT_PATH, f"output parent does not exist: {path.parent}")
    if path.exists() and (path.is_dir() or not path.is_file()):
        raise ProviderError(ErrorCode.INVALID_OUTPUT_PATH, f"output path is not a regular file: {path}")
    return path


def _capability_for_path(
    path: Path, capabilities: tuple[_DirectoryCapability, ...], error: ErrorCode
) -> _DirectoryCapability:
    for capability in capabilities:
        try:
            path.relative_to(capability.path)
        except ValueError:
            continue
        return capability
    raise ProviderError(error, f"path is outside configured roots: {path}")


def _copy_checked_file(
    path: Path,
    capabilities: tuple[_DirectoryCapability, ...],
    destination: Path,
    error: ErrorCode,
) -> None:
    capability = _capability_for_path(path, capabilities, error)
    source_fd = _open_beneath(capability, path, os.O_RDONLY, error)
    _copy_fd_to_path(source_fd, destination, error)


def _private_file_size(path: Path, error: ErrorCode) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            result = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ProviderError(error, f"output cannot be opened safely: {path}") from exc
    if not statmod.S_ISREG(result.st_mode) or result.st_size <= 0:
        raise ProviderError(error, f"output is not a non-empty regular file: {path}")
    return int(result.st_size)


def _open_directory_beneath(capability: _DirectoryCapability, path: Path, error: ErrorCode) -> int:
    """Open an existing destination parent below a held root."""
    if path == capability.path:
        if capability.fd is None:
            raise ProviderError(error, "configured path capability is closed")
        try:
            return os.dup(capability.fd)
        except OSError as exc:
            raise ProviderError(error, f"output parent cannot be opened safely: {path}") from exc
    parts = _relative_to_capability(path, capability, error)
    if capability.fd is None:
        raise ProviderError(error, "configured path capability is closed")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current: int | None = None
    try:
        current = os.dup(capability.fd)
        for part in parts:
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except (OSError, TypeError) as exc:
        if current is not None:
            with suppress(OSError):
                os.close(current)
        raise ProviderError(error, f"output parent cannot be opened safely: {path}") from exc


def _publish_private_file(
    source: Path,
    destination: Path,
    capabilities: tuple[_DirectoryCapability, ...],
    error: ErrorCode,
) -> int:
    """Atomically publish a private result using directory descriptors."""
    if not _safe_dirfd_operations_available():
        raise ProviderError(error, "this platform lacks safe descriptor-relative publication")
    capability = _capability_for_path(destination, capabilities, error)
    parent_fd = _open_directory_beneath(capability, destination.parent, error)
    parent_stat = os.fstat(parent_fd)
    parent_identity = (parent_stat.st_dev, parent_stat.st_ino, parent_stat.st_mode)
    source_fd: int | None = None
    source_parent_fd: int | None = None
    published_fd: int | None = None
    verification_parent_fd: int | None = None
    try:
        # Open the private source without following a late symlink swap, then
        # rename by descriptor-relative names into the held approved parent.
        source_parent_fd = os.open(
            source.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        source_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_parent_fd)
        source_stat = os.fstat(source_fd)
        if not statmod.S_ISREG(source_stat.st_mode) or source_stat.st_size <= 0:
            raise ProviderError(error, "private output is not a non-empty regular file")
        source_identity = (source_stat.st_dev, source_stat.st_ino)
        os.rename(
            source.name,
            destination.name,
            src_dir_fd=source_parent_fd,
            dst_dir_fd=parent_fd,
        )
        # Ensure the checked destination parent still names the descriptor
        # acquired before publication.  A late replacement becomes a typed
        # failure rather than publishing into a detached or attacker-chosen
        # directory.
        verification_parent_fd = _open_directory_beneath(capability, destination.parent, error)
        verified_stat = os.fstat(verification_parent_fd)
        if (verified_stat.st_dev, verified_stat.st_ino, verified_stat.st_mode) != parent_identity:
            raise ProviderError(error, "destination parent changed during publication")
        # Verify the published object through the already-held destination
        # descriptor, never by reopening the absolute destination pathname.
        published_fd = os.open(destination.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        published_stat = os.fstat(published_fd)
        if (
            not statmod.S_ISREG(published_stat.st_mode)
            or published_stat.st_size != source_stat.st_size
            or (published_stat.st_dev, published_stat.st_ino) != source_identity
        ):
            raise ProviderError(error, "published output is not the verified private file")
        return int(published_stat.st_size)
    except ProviderError:
        raise
    except (OSError, TypeError) as exc:
        raise ProviderError(error, f"could not publish output atomically: {destination}") from exc
    finally:
        if source_fd is not None:
            with suppress(OSError):
                os.close(source_fd)
        if source_parent_fd is not None:
            with suppress(OSError):
                os.close(source_parent_fd)
        if published_fd is not None:
            with suppress(OSError):
                os.close(published_fd)
        if verification_parent_fd is not None:
            with suppress(OSError):
                os.close(verification_parent_fd)
        with suppress(OSError):
            os.close(parent_fd)


def parse_get_scene_info_request() -> GetSceneInfoRequest:
    return GetSceneInfoRequest()


def parse_execute_code_request(code: Any) -> ExecuteCodeRequest:
    return ExecuteCodeRequest(admit_headless_bpy_program(code))


def parse_camera_render_preview_request(max_size: Any) -> CameraRenderPreviewRequest:
    return CameraRenderPreviewRequest(parse_preview_size(max_size))


def parse_import_asset_request(
    path: Any,
    format: Any,
    staged_asset_roots: Iterable[str | Path] | None = None,
) -> ImportAssetRequest:
    return ImportAssetRequest(parse_asset_path(path, staged_asset_roots), parse_import_format(format))


def parse_export_scene_request(
    path: Any,
    format: Any,
    selection_only: Any = False,
    approved_output_roots: Iterable[str | Path] | None = None,
) -> ExportSceneRequest:
    if not isinstance(selection_only, bool):
        raise ProviderError(ErrorCode.INVALID_REQUEST, "selection_only must be a boolean")
    export_format = parse_export_format(format)
    if selection_only and export_format not in {
        ExportFormat.GLB,
        ExportFormat.GLTF,
        ExportFormat.OBJ,
        ExportFormat.FBX,
        ExportFormat.USD,
        ExportFormat.USDC,
    }:
        raise ProviderError(
            ErrorCode.INVALID_REQUEST,
            f"selection_only is unsupported for {export_format.value}",
            details={"field": "selection_only", "format": export_format.value},
        )
    return ExportSceneRequest(parse_output_path(path, approved_output_roots), export_format, selection_only)


def _finite_vector(value: Any, field: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, f"{field} must have exactly three numbers")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) for item in value):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, f"{field} must contain finite numbers")
    return tuple(float(item) for item in value)  # type: ignore[return-value]


def _parse_scene_info(payload: dict[str, Any], revision: int) -> SceneInfo:
    _require_exact_fields(
        payload,
        {"scene_name", "frame_current", "frame_start", "frame_end", "active_camera", "objects"},
        "scene-info",
    )
    try:
        scene_name = payload["scene_name"]
        current = payload["frame_current"]
        start = payload["frame_start"]
        end = payload["frame_end"]
        camera = payload.get("active_camera")
        raw_objects = payload["objects"]
    except (KeyError, TypeError) as exc:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene-info sentinel is missing required fields") from exc
    if not isinstance(scene_name, str) or not scene_name:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene_name must be non-empty")
    if any(isinstance(frame, bool) or not isinstance(frame, int) for frame in (current, start, end)):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene frames must be integral")
    if start <= 0 or end <= 0 or start > end:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene frame bounds are invalid")
    if camera is not None and (not isinstance(camera, str) or not camera):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "active_camera must be a non-empty name or null")
    if not isinstance(raw_objects, list):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "objects must be a list")

    objects: list[SceneObject] = []
    for raw in raw_objects:
        if not isinstance(raw, dict):
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene object must be an object")
        if set(raw) != {"name", "type", "location", "rotation_euler", "scale", "visible"}:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene object fields are invalid")
        try:
            name = raw["name"]
            kind = ObjectKind(raw["type"])
            location = _finite_vector(raw["location"], "location")
            rotation = _finite_vector(raw["rotation_euler"], "rotation_euler")
            scale = _finite_vector(raw["scale"], "scale")
            visible = raw["visible"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene object fields are invalid") from exc
        if not isinstance(name, str) or not name or not isinstance(visible, bool):
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene object name or visibility is invalid")
        objects.append(SceneObject(name, kind, location, rotation, scale, visible))
    if any(item.name == "" for item in objects) or len({item.name for item in objects}) != len(objects):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene object names must be non-empty and unique")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene revision must be a non-negative integer")
    return SceneInfo(scene_name, revision, current, start, end, camera, tuple(objects))


def _require_exact_fields(data: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(data) != fields:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, f"{label} result fields are invalid")


def _parse_execute_result(data: dict[str, Any], revision: int) -> dict[str, Any]:
    _require_exact_fields(data, {"scene_revision", "saved", "inspected", "stdout"}, "execute-code")
    if (
        isinstance(data["scene_revision"], bool)
        or not isinstance(data["scene_revision"], int)
        or data["scene_revision"] != revision
    ):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "execute-code revision metadata is invalid")
    if data["saved"] is not True or data["inspected"] is not True or not isinstance(data["stdout"], str):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "execute-code result types are invalid")
    return {"scene_revision": revision, "stdout": data["stdout"], "saved": True}


def _parse_import_result(data: dict[str, Any], revision: int, import_format: ImportFormat) -> dict[str, Any]:
    _require_exact_fields(
        data,
        {"scene_revision", "imported_object_names", "format", "saved", "inspected"},
        "import",
    )
    names = data["imported_object_names"]
    if (
        isinstance(data["scene_revision"], bool)
        or not isinstance(data["scene_revision"], int)
        or data["scene_revision"] != revision
        or data["saved"] is not True
        or data["inspected"] is not True
        or data["format"] != import_format.value
        or not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) and bool(name) for name in names)
        or len(set(names)) != len(names)
    ):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "import result metadata is invalid")
    return {"scene_revision": revision, "imported_object_names": names, "format": import_format.value}


def _parse_export_result(data: dict[str, Any], revision: int, output_path: Path, export_format: ExportFormat) -> dict[str, Any]:
    _require_exact_fields(data, {"scene_revision", "path", "format"}, "export")
    if (
        isinstance(data["scene_revision"], bool)
        or not isinstance(data["scene_revision"], int)
        or data["scene_revision"] != revision
        or data["path"] != str(output_path)
        or not isinstance(data["path"], str)
        or data["format"] != export_format.value
    ):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "export result metadata is invalid")
    return {"scene_revision": revision, "path": str(output_path), "format": export_format.value}


def _is_plausible_blend_file(path: Path) -> bool:
    """Recognize Blender's fixed 12-byte file signature, not just bytes."""
    try:
        with path.open("rb") as stream:
            header = stream.read(12)
    except OSError:
        return False
    return (
        len(header) == 12
        and header[:7] == b"BLENDER"
        and header[7:8] in {b"-", b"_"}
        and header[8:9] in {b"v", b"V"}
        and header[9:12].isdigit()
    )


def _validate_png(data: bytes, max_size: int) -> tuple[int, int]:
    """Parse PNG structure, CRCs, zlib stream, and bounded dimensions."""
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview is not a PNG")
    offset = len(signature)
    ihdr: bytes | None = None
    idat = bytearray()
    saw_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview PNG has a truncated chunk")
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data) or len(chunk_type) != 4:
            raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview PNG has an invalid chunk length")
        chunk = data[offset + 8 : offset + 8 + length]
        expected_crc = int.from_bytes(data[offset + 8 + length : end], "big")
        if (zlib.crc32(chunk_type + chunk) & 0xFFFFFFFF) != expected_crc:
            raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview PNG has an invalid chunk CRC")
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13 or offset != len(signature):
                raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview PNG has an invalid IHDR")
            ihdr = chunk
        elif chunk_type == b"IDAT":
            idat.extend(chunk)
        elif chunk_type == b"IEND":
            if length != 0 or not idat or ihdr is None or end != len(data):
                raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview PNG has an invalid IEND")
            saw_iend = True
            break
        offset = end
    if not saw_iend or ihdr is None:
        raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview PNG is missing required chunks")
    width = int.from_bytes(ihdr[0:4], "big")
    height = int.from_bytes(ihdr[4:8], "big")
    bit_depth = ihdr[8]
    color_type = ihdr[9]
    interlace = ihdr[12]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if (
        not width
        or not height
        or max(width, height) > max_size
        or bit_depth != 8
        or channels is None
        or interlace != 0
    ):
        raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview PNG dimensions or encoding are unsupported")
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(idat)) + decompressor.flush()
        if not decompressor.eof or decompressor.unused_data:
            raise zlib.error("incomplete or trailing zlib data")
    except zlib.error as exc:
        raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview PNG image data is not decompressible") from exc
    row_size = 1 + width * channels
    if len(decoded) != row_size * height or any(decoded[row * row_size] > 4 for row in range(height)):
        raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview PNG scanlines are invalid")
    return width, height


def _parse_sentinel(stdout: str) -> dict[str, Any]:
    lines = [line[len(SENTINEL_PREFIX) :] for line in stdout.splitlines() if line.startswith(SENTINEL_PREFIX)]
    if not lines:
        raise ProviderError(ErrorCode.RESULT_SENTINEL_MISSING, "Blender did not emit a result sentinel")
    if len(lines) != 1:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "Blender emitted multiple result sentinels")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "result sentinel is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("wire_version") != WIRE_VERSION:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "result sentinel has an unsupported wire version")
    if payload.get("ok") is False:
        if set(payload) != {"wire_version", "ok", "error_code", "error_message"}:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "error sentinel payload has unknown fields")
        code_name = payload.get("error_code")
        try:
            code = ErrorCode(code_name)
        except (TypeError, ValueError):
            code = ErrorCode.RESULT_MALFORMED
        if not isinstance(payload.get("error_message"), str) or not payload["error_message"]:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "error sentinel has an invalid message")
        raise ProviderError(code, payload["error_message"])
    if set(payload) != {"wire_version", "ok", "data"} or payload.get("ok") is not True:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "result sentinel payload is invalid")
    if not isinstance(payload.get("data"), dict):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "result sentinel data must be an object")
    return payload["data"]


def _script_header() -> str:
    return f"""import bpy\nimport json\nimport sys\n\n_SENTINEL = {SENTINEL_PREFIX!r}\n_VERSION = {WIRE_VERSION!r}\n\ndef _emit(data=None, *, error_code=None, error_message=None):\n    payload = {{'wire_version': _VERSION, 'ok': error_code is None}}\n    if error_code is None:\n        payload['data'] = data or {{}}\n    else:\n        payload['error_code'] = error_code\n        payload['error_message'] = error_message or 'Blender operation failed'\n    print(_SENTINEL + json.dumps(payload, sort_keys=True, separators=(',', ':')))\n\ndef _prepare_private_scene(path):\n    # Saving once before author code makes bpy.data.filepath point at the\n    # private next-revision path; BlendData.filepath itself is read-only.\n    bpy.ops.wm.save_as_mainfile(filepath=path)\n\ndef _save_and_inspect(path):\n    bpy.ops.wm.save_as_mainfile(filepath=path)\n    bpy.ops.wm.open_mainfile(filepath=path)\n    if bpy.data.filepath != path:\n        raise RuntimeError('saved scene could not be reopened for inspection')\n\n"""


def build_provider_script(
    operation: str,
    *,
    revision_number: int = 0,
    revision_path: Path | None = None,
    author_code: HeadlessBpyProgram | None = None,
    asset_path: Path | None = None,
    import_format: ImportFormat | None = None,
    output_path: Path | None = None,
    export_format: ExportFormat | None = None,
    selection_only: bool = False,
    preview_path: Path | None = None,
    preview_size: PreviewSize | None = None,
) -> str:
    """Construct provider-owned load/operation/result/save script."""
    script = _script_header()
    if operation == "get_scene_info":
        script += """scene = bpy.context.scene\ndef _vec(value):\n    return [float(value[0]), float(value[1]), float(value[2])]\nobjects = []\nfor obj in scene.objects:\n    objects.append({'name': obj.name, 'type': obj.type, 'location': _vec(obj.location),\n                    'rotation_euler': _vec(obj.rotation_euler), 'scale': _vec(obj.scale),\n                    'visible': bool(obj.visible_get())})\n_emit({'scene_name': scene.name, 'frame_current': int(scene.frame_current),\n       'frame_start': int(scene.frame_start), 'frame_end': int(scene.frame_end),\n       'active_camera': scene.camera.name if scene.camera else None, 'objects': objects})\n"""
    elif operation == "execute_code":
        if author_code is None or revision_path is None:
            raise ValueError("execute-code requires author code and revision path")
        safe_names = repr(_SAFE_BUILTIN_NAMES)
        safe_roots = repr(tuple(sorted(_SAFE_IMPORT_ROOTS)))
        script += f"""import contextlib\nimport io\nimport builtins as _builtins\n\ndef _safe_author_import(name, globals_=None, locals_=None, fromlist=(), level=0):\n    if level or not isinstance(name, str) or name not in {safe_roots}:\n        raise ImportError('module is not admitted')\n    checked_fromlist = fromlist or ()\n    if any(not isinstance(item, str) or item == '*' or item.startswith('_') for item in checked_fromlist):\n        raise ImportError('imported name is not admitted')\n    return _builtins.__import__(name, globals_, locals_, checked_fromlist, level)\n\n_safe_author_builtins = {{name: getattr(_builtins, name) for name in {safe_names}}}\n_safe_author_builtins['__import__'] = _safe_author_import\nfor _name in ('Exception', 'RuntimeError', 'ValueError', 'TypeError', 'IndexError', 'KeyError', 'AssertionError'):\n    _safe_author_builtins[_name] = getattr(_builtins, _name)\n_user_code = {author_code.source!r}\n_output = io.StringIO()\ntry:\n    _prepare_private_scene({str(revision_path)!r})\n    with contextlib.redirect_stdout(_output):\n        exec(compile(_user_code, '<headless-bpy-program>', 'exec'), {{\n            '__name__': '__main__',\n            '__file__': '<headless-bpy-program>',\n            '__builtins__': _safe_author_builtins,\n        }})\n    _save_and_inspect({str(revision_path)!r})\n    _emit({{'scene_revision': {revision_number}, 'saved': True, 'inspected': True, 'stdout': _output.getvalue()}})\nexcept Exception as _exc:\n    _emit(error_code='ResultMalformed', error_message=str(_exc))\n    sys.exit(1)\n"""
    elif operation == "import_asset":
        if revision_path is None or asset_path is None or import_format is None:
            raise ValueError("import requires asset and revision paths")
        if import_format in (ImportFormat.GLB, ImportFormat.GLTF):
            import_call = "bpy.ops.import_scene.gltf(filepath=_path)"
        elif import_format is ImportFormat.OBJ:
            import_call = "bpy.ops.wm.obj_import(filepath=_path)"
        elif import_format is ImportFormat.FBX:
            import_call = "bpy.ops.import_scene.fbx(filepath=_path)"
        else:
            import_call = "bpy.ops.wm.usd_import(filepath=_path)"
        script += f"""# Imported scenes are staged into the private next revision.\n_before = {{obj.name for obj in bpy.context.scene.objects}}\ntry:\n    _prepare_private_scene({str(revision_path)!r})\n    _path = {str(asset_path)!r}\n    {import_call}\n    _names = [obj.name for obj in bpy.context.scene.objects if obj.name not in _before]\n    _save_and_inspect({str(revision_path)!r})\n    _emit({{'scene_revision': {revision_number}, 'imported_object_names': _names, 'format': {import_format.value!r}, 'saved': True, 'inspected': True}})\nexcept Exception as _exc:\n    _emit(error_code='ResultMalformed', error_message=str(_exc))\n    sys.exit(1)\n"""
    elif operation == "camera_render_preview":
        if preview_path is None or preview_size is None:
            raise ValueError("preview requires output path and size")
        script += f"""scene = bpy.context.scene\nif scene.camera is None:\n    _emit(error_code='ActiveCameraMissing', error_message='scene has no active camera')\n    sys.exit(1)\n_width = int(scene.render.resolution_x)\n_height = int(scene.render.resolution_y)\n_scale = min(1.0, {preview_size.pixels!r} / max(_width, _height, 1))\nscene.render.resolution_percentage = 100\nscene.render.resolution_x = max(1, int(round(_width * _scale)))\nscene.render.resolution_y = max(1, int(round(_height * _scale)))\nscene.render.image_settings.file_format = 'PNG'\nscene.render.filepath = {str(preview_path)!r}\ntry:\n    bpy.ops.render.render(write_still=True)\n    _emit({{'preview': True}})\nexcept Exception as _exc:\n    _emit(error_code='PreviewMissing', error_message=str(_exc))\n    sys.exit(1)\n"""
    elif operation == "export_scene":
        if output_path is None or export_format is None:
            raise ValueError("export requires output path and format")
        selection_flag = "True" if selection_only else "False"
        if export_format is ExportFormat.BLEND:
            export_call = "bpy.ops.wm.save_as_mainfile(filepath=_path)"
        elif export_format in (ExportFormat.GLB, ExportFormat.GLTF):
            export_mode = "GLB" if export_format is ExportFormat.GLB else "GLTF_SEPARATE"
            export_call = (
                f"bpy.ops.export_scene.gltf(filepath=_path, export_format={export_mode!r}, "
                f"export_selected_objects={selection_flag})"
            )
        elif export_format is ExportFormat.OBJ:
            export_call = f"bpy.ops.wm.obj_export(filepath=_path, export_selected_objects={selection_flag})"
        elif export_format is ExportFormat.FBX:
            export_call = f"bpy.ops.export_scene.fbx(filepath=_path, use_selection={selection_flag})"
        else:
            export_call = f"bpy.ops.wm.usd_export(filepath=_path, selected_objects_only={selection_flag})"
        script += f"""_path = {str(output_path)!r}\ntry:\n    {export_call}\n    _emit({{'scene_revision': {revision_number}, 'path': _path, 'format': {export_format.value!r}}})\nexcept Exception as _exc:\n    _emit(error_code='ExportMissing', error_message=str(_exc))\n    sys.exit(1)\n"""
    else:
        raise ValueError(f"unknown provider operation: {operation}")
    return script


class SceneSession:
    """One serialized, file-backed scene and its direct Blender child."""

    def __init__(
        self,
        blender_executable: str | None = None,
        *,
        workspace: Path | None = None,
        executor: StrictBlenderExecutor | Any | None = None,
        timeout_seconds: float = 300.0,
        staged_asset_roots: Iterable[str | Path] | None = None,
        approved_output_roots: Iterable[str | Path] | None = None,
        process_audit_path: str | Path | None = None,
    ) -> None:
        self.blender_executable = (
            blender_executable if blender_executable is not None else os.environ.get("BLENDER_EXECUTABLE", "")
        )
        # Injected executors are the explicit test/embedding seam. A real
        # executor proves the path is Blender before creating any workspace.
        if executor is None:
            executable = Path(self.blender_executable)
            strict_executor = StrictBlenderExecutor(
                self.blender_executable, audit_path=process_audit_path
            )
            if (
                not self.blender_executable
                or not executable.is_file()
                or not os.access(executable, os.X_OK)
                or not strict_executor.validate_executable()
            ):
                raise ProviderError(
                    ErrorCode.BLENDER_EXECUTABLE_MISSING,
                    f"Blender executable is missing or invalid: {self.blender_executable or '<unset>'}",
                )
            self.executor = strict_executor
        else:
            self.executor = executor
        self.staged_asset_roots = _resolve_roots(
            staged_asset_roots, "BLENDER_MCP_STAGED_ASSET_ROOTS", ErrorCode.INVALID_ASSET_PATH
        )
        self.approved_output_roots = _resolve_roots(
            approved_output_roots, "BLENDER_MCP_APPROVED_OUTPUT_ROOTS", ErrorCode.INVALID_OUTPUT_PATH
        )
        self._staged_capabilities: tuple[_DirectoryCapability, ...] = ()
        self._approved_capabilities: tuple[_DirectoryCapability, ...] = ()
        try:
            self._staged_capabilities = _directory_capabilities(
                self.staged_asset_roots, ErrorCode.INVALID_ASSET_PATH
            )
            self._approved_capabilities = _directory_capabilities(
                self.approved_output_roots, ErrorCode.INVALID_OUTPUT_PATH
            )
            self.workspace = workspace or Path(tempfile.mkdtemp(prefix="blender-headless-session-"))
            self.workspace.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.workspace, 0o700)
            except OSError:
                pass
        except Exception:
            # Root descriptors are capabilities.  Never leave an earlier root
            # open when a later root or workspace setup fails.
            for capability in (*self._staged_capabilities, *self._approved_capabilities):
                capability.close()
            raise
        self.revision = 0
        self.current_scene: Path | None = None
        self._lock = asyncio.Lock()
        self._teardown_lock = asyncio.Lock()
        self._active_task: asyncio.Task[Any] | None = None
        self.timeout_seconds = timeout_seconds
        self._closed = False
        self._teardown_complete = False

    @property
    def active_process(self) -> Any | None:
        return getattr(self.executor, "active_process", None)

    async def _run(
        self,
        operation: str,
        *,
        mutation: bool = False,
        author_code: HeadlessBpyProgram | None = None,
        asset_path: Path | None = None,
        import_format: ImportFormat | None = None,
        output_path: Path | None = None,
        export_format: ExportFormat | None = None,
        selection_only: bool = False,
        preview_size: PreviewSize | None = None,
    ) -> dict[str, Any]:
        if isinstance(self.executor, StrictBlenderExecutor):
            executable = Path(self.blender_executable)
            if not self.blender_executable or not executable.is_file() or not os.access(executable, os.X_OK):
                raise ProviderError(
                    ErrorCode.BLENDER_EXECUTABLE_MISSING,
                    f"Blender executable is missing or invalid: {self.blender_executable or '<unset>'}",
                )
        async with self._lock:
            # These checks must happen after lock acquisition: a read queued
            # behind the first mutation must observe its published revision,
            # while a request queued during teardown must never spawn.
            if self._closed:
                raise ProviderError(ErrorCode.INVALID_REQUEST, "scene session is closed")
            if operation in {"get_scene_info", "camera_render_preview", "export_scene"} and self.current_scene is None:
                raise ProviderError(ErrorCode.SCENE_NOT_INITIALIZED, "scene has no successful revision")
            self._active_task = asyncio.current_task()
            next_revision = self.revision + 1
            operation_dir = self.workspace / f"operation-{uuid.uuid4().hex}"
            operation_dir.mkdir(mode=0o700)
            with suppress(OSError):
                os.chmod(operation_dir, 0o700)
            revision_path = self.workspace / f"scene-{next_revision:08d}.blend" if mutation else None
            temporary_revision = operation_dir / f".scene-{next_revision:08d}.blend" if mutation else None
            preview_path = operation_dir / "preview.png" if operation == "camera_render_preview" else None
            script_path = operation_dir / "provider.py"
            scene_input_path = operation_dir / "input.blend" if self.current_scene is not None else None
            private_asset_path = (
                operation_dir / f"asset.{import_format.value}"
                if asset_path is not None and import_format is not None
                else None
            )
            private_output_path = (
                operation_dir / f"export-output.{export_format.value}"
                if output_path is not None and export_format is not None
                else None
            )
            try:
                # Never expose the published predecessor to Blender.  The
                # child receives an fd-based private copy, and mutation code
                # is pointed at the private next-revision path below.
                if scene_input_path is not None and self.current_scene is not None:
                    try:
                        source_fd = os.open(
                            self.current_scene,
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        )
                    except OSError as exc:
                        raise ProviderError(
                            ErrorCode.SCENE_REVISION_MISSING,
                            "current scene revision cannot be opened safely",
                        ) from exc
                    _copy_fd_to_path(source_fd, scene_input_path, ErrorCode.SCENE_REVISION_MISSING)
                    with suppress(OSError):
                        os.chmod(scene_input_path, 0o400)
                if private_asset_path is not None and asset_path is not None:
                    _copy_checked_file(
                        asset_path,
                        self._staged_capabilities,
                        private_asset_path,
                        ErrorCode.INVALID_ASSET_PATH,
                    )
                    with suppress(OSError):
                        os.chmod(private_asset_path, 0o400)
                if private_output_path is not None:
                    # Exclusive creation is not needed here: Blender creates
                    # the file, and the operation directory is private.
                    private_output_path.unlink(missing_ok=True)
                if mutation and temporary_revision is None:
                    raise AssertionError("mutation must have a temporary revision path")
                if author_code is not None:
                    # Provider repeats admission after the client boundary.
                    author_code = admit_headless_bpy_program(author_code.source)
                script = build_provider_script(
                    operation,
                    revision_number=next_revision if mutation else self.revision,
                    revision_path=temporary_revision,
                    author_code=author_code,
                    asset_path=private_asset_path,
                    import_format=import_format,
                    output_path=private_output_path,
                    export_format=export_format,
                    selection_only=selection_only,
                    preview_path=preview_path,
                    preview_size=preview_size,
                )
                script_path.write_text(script, encoding="utf-8")
                os.chmod(script_path, 0o600)
                command = plan_strict_command(
                    self.blender_executable,
                    scene_input_path,
                    script_path,
                    (operation,),
                )
                try:
                    observation = await self.executor.execute(
                        command,
                        timeout=self.timeout_seconds,
                        execute_code=operation == "execute_code",
                        cwd=operation_dir,
                    )
                except StrictExecutionError as exc:
                    code = {
                        StrictFailureKind.COMMAND_INVALID: ErrorCode.INVALID_REQUEST,
                        StrictFailureKind.GUARD_UNAVAILABLE: ErrorCode.EXECUTE_CODE_GUARD_UNAVAILABLE,
                        StrictFailureKind.TIMED_OUT: ErrorCode.BLENDER_TIMED_OUT,
                        StrictFailureKind.CANCELLED: ErrorCode.BLENDER_CANCELLED,
                        StrictFailureKind.SPAWN_FAILED: ErrorCode.BLENDER_EXECUTABLE_MISSING,
                        StrictFailureKind.REAP_FAILED: ErrorCode.BLENDER_LIFECYCLE_FAILURE,
                    }[exc.kind]
                    raise ProviderError(code, str(exc)) from exc
                except TimeoutError as exc:
                    raise ProviderError(ErrorCode.BLENDER_TIMED_OUT, "Blender execution timed out") from exc

                if observation.returncode != 0:
                    # A provider sentinel may carry a more precise operation error.
                    try:
                        _parse_sentinel(observation.stdout)
                    except ProviderError as error:
                        if error.code is not ErrorCode.RESULT_SENTINEL_MISSING:
                            raise
                    raise ProviderError(
                        ErrorCode.BLENDER_EXITED_NONZERO,
                        f"Blender exited with status {observation.returncode}",
                        details={"stderr": observation.stderr[-4000:]},
                    )
                data = _parse_sentinel(observation.stdout)

                if operation == "get_scene_info":
                    if self.current_scene is None:
                        raise ProviderError(ErrorCode.SCENE_NOT_INITIALIZED, "a scene revision is required")
                    return _parse_scene_info(data, self.revision).to_dict()
                if mutation:
                    if (
                        temporary_revision is None
                        or not temporary_revision.is_file()
                        or temporary_revision.stat().st_size <= 0
                        or not _is_plausible_blend_file(temporary_revision)
                    ):
                        raise ProviderError(
                            ErrorCode.SCENE_REVISION_MISSING,
                            "Blender did not produce an inspectable scene revision",
                        )
                    if operation == "execute_code":
                        checked_result = _parse_execute_result(data, next_revision)
                    elif operation == "import_asset":
                        if import_format is None:
                            raise ProviderError(ErrorCode.RESULT_MALFORMED, "import format is missing")
                        checked_result = _parse_import_result(data, next_revision, import_format)
                    else:
                        raise ProviderError(ErrorCode.RESULT_MALFORMED, "unknown mutation operation")
                    if revision_path is None:
                        raise ProviderError(ErrorCode.SCENE_REVISION_MISSING, "revision publication path is missing")
                    os.replace(temporary_revision, revision_path)
                    with suppress(OSError):
                        os.chmod(revision_path, 0o400)
                    self.revision = next_revision
                    self.current_scene = revision_path
                    return checked_result
                if operation == "camera_render_preview":
                    if data != {"preview": True}:
                        raise ProviderError(ErrorCode.RESULT_MALFORMED, "preview result is invalid")
                    if preview_path is None:
                        raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview output path is missing")
                    _private_file_size(preview_path, ErrorCode.PREVIEW_MISSING)
                    preview = preview_path.read_bytes()
                    _validate_png(preview, preview_size.pixels if preview_size is not None else 0)
                    return {"png": preview}
                if operation == "export_scene":
                    if output_path is None or export_format is None or private_output_path is None:
                        raise ProviderError(ErrorCode.RESULT_MALFORMED, "export request metadata is missing")
                    _parse_export_result(data, self.revision, private_output_path, export_format)
                    bytes_written = _publish_private_file(
                        private_output_path,
                        output_path,
                        self._approved_capabilities,
                        ErrorCode.INVALID_OUTPUT_PATH,
                    )
                    return {
                        "scene_revision": self.revision,
                        "path": str(output_path),
                        "format": export_format.value,
                        "bytes": bytes_written,
                    }
                raise ProviderError(ErrorCode.RESULT_MALFORMED, "unknown session operation")
            finally:
                # A hard lifecycle failure deliberately retains ownership and
                # the operation directory so teardown can retry reaping before
                # removing evidence or workspace bytes.
                if self.active_process is None:
                    with suppress(OSError):
                        shutil.rmtree(operation_dir)
                self._active_task = None

    async def get_scene_info(self) -> dict[str, Any]:
        return await self._run("get_scene_info")

    async def execute_code(self, code: str | HeadlessBpyProgram) -> dict[str, Any]:
        program = code if isinstance(code, HeadlessBpyProgram) else admit_headless_bpy_program(code)
        return await self._run("execute_code", mutation=True, author_code=program)

    async def camera_render_preview(self, max_size: int | PreviewSize) -> bytes:
        size = max_size if isinstance(max_size, PreviewSize) else parse_preview_size(max_size)
        result = await self._run("camera_render_preview", preview_size=size)
        return result["png"]

    async def import_asset(self, path: str | Path, format: str | ImportFormat) -> dict[str, Any]:
        asset = parse_asset_path(str(path), self.staged_asset_roots)
        import_kind = format if isinstance(format, ImportFormat) else parse_import_format(format)
        # Import is the one mutation that may start from factory-startup.
        return await self._run("import_asset", mutation=True, asset_path=asset, import_format=import_kind)

    async def export_scene(self, path: str | Path, format: str | ExportFormat, selection_only: bool = False) -> dict[str, Any]:
        output = parse_output_path(str(path), self.approved_output_roots)
        export_kind = format if isinstance(format, ExportFormat) else parse_export_format(format)
        if selection_only and export_kind not in {
            ExportFormat.GLB,
            ExportFormat.GLTF,
            ExportFormat.OBJ,
            ExportFormat.FBX,
            ExportFormat.USD,
            ExportFormat.USDC,
        }:
            raise ProviderError(
                ErrorCode.INVALID_REQUEST,
                f"selection_only is unsupported for {export_kind.value}",
                details={"field": "selection_only", "format": export_kind.value},
            )
        return await self._run(
            "export_scene",
            output_path=output,
            export_format=export_kind,
            selection_only=selection_only,
        )

    async def teardown(self) -> None:
        """Reap owned work before releasing descriptors or the workspace."""
        async with self._teardown_lock:
            if self._teardown_complete:
                return
            self._closed = True
            lifecycle_error: StrictExecutionError | None = None
            task = self._active_task
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                try:
                    await _await_uninterruptibly(task)
                except (ProviderError, asyncio.CancelledError):
                    pass
                except StrictExecutionError as exc:
                    lifecycle_error = exc

            active = self.active_process
            if active is not None:
                reap = getattr(self.executor, "_terminate_and_reap", None)
                if not callable(reap):
                    lifecycle_error = lifecycle_error or StrictExecutionError(
                        StrictFailureKind.REAP_FAILED,
                        "active Blender child has no reap capability",
                    )
                else:
                    try:
                        await _await_uninterruptibly(reap(active))
                    except StrictExecutionError as exc:
                        lifecycle_error = lifecycle_error or exc
                    else:
                        # _terminate_and_reap is also used by the session
                        # bracket; teardown owns the final clear because it
                        # called the helper outside that bracket.
                        if active.returncode is not None:
                            self.executor.active_process = None

            if self.active_process is not None:
                lifecycle_error = lifecycle_error or StrictExecutionError(
                    StrictFailureKind.REAP_FAILED,
                    "Blender child ownership could not be safely released",
                )
            if lifecycle_error is not None:
                # Keep descriptors, operation bytes, and ownership for a later
                # retry.  Claiming teardown success here would leak a child.
                raise lifecycle_error

            for capability in (*self._staged_capabilities, *self._approved_capabilities):
                capability.close()
            try:
                shutil.rmtree(self.workspace)
            except FileNotFoundError:
                pass
            self._teardown_complete = True

    async def close(self) -> None:
        """Alias for teardown used by embedding hosts."""
        await self.teardown()


# Wire helpers for tests and alternate transports.
def _request_to_dict(operation: str, request: Any) -> dict[str, Any]:
    if operation == "get_scene_info" and isinstance(request, GetSceneInfoRequest):
        return {}
    if operation == "execute_code" and isinstance(request, ExecuteCodeRequest):
        return {"code": request.program.source}
    if operation == "camera_render_preview" and isinstance(request, CameraRenderPreviewRequest):
        return {"max_size": request.size.pixels}
    if operation == "import_asset" and isinstance(request, ImportAssetRequest):
        return {"path": str(request.path), "format": request.format.value}
    if operation == "export_scene" and isinstance(request, ExportSceneRequest):
        return {"path": str(request.path), "format": request.format.value, "selection_only": request.selection_only}
    raise ProviderError(ErrorCode.INVALID_REQUEST, f"checked request does not match operation: {operation}")


def encode_request(operation: str, request: Any) -> str:
    """Encode an operation-specific checked request in canonical v1 JSON."""
    return json.dumps(_request_to_dict(operation, request), sort_keys=True, separators=(",", ":"))


def decode_request(
    operation: str,
    value: str,
    *,
    staged_asset_roots: Iterable[str | Path] | None = None,
    approved_output_roots: Iterable[str | Path] | None = None,
) -> Any:
    """Decode raw JSON into the checked request for exactly one operation."""
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProviderError(ErrorCode.INVALID_REQUEST, "request is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ProviderError(ErrorCode.INVALID_REQUEST, "request must be an object")
    if operation == "get_scene_info":
        if raw != {}:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "scene-info request must be empty")
        return parse_get_scene_info_request()
    if operation == "execute_code":
        if set(raw) != {"code"}:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "execute-code request fields are invalid")
        return parse_execute_code_request(raw["code"])
    if operation == "camera_render_preview":
        if set(raw) != {"max_size"}:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "preview request fields are invalid")
        return parse_camera_render_preview_request(raw["max_size"])
    if operation == "import_asset":
        if set(raw) != {"path", "format"}:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "import request fields are invalid")
        return parse_import_asset_request(raw["path"], raw["format"], staged_asset_roots)
    if operation == "export_scene":
        if set(raw) != {"path", "format", "selection_only"}:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "export request fields are invalid")
        return parse_export_scene_request(
            raw["path"], raw["format"], raw["selection_only"], approved_output_roots
        )
    raise ProviderError(ErrorCode.INVALID_REQUEST, f"unknown request operation: {operation}")


def check_public_result(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    if operation == "get_scene_info":
        if "scene_revision" not in result:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene-info result is missing revision")
        revision = result["scene_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene revision must be a non-negative integer")
        payload = dict(result)
        del payload["scene_revision"]
        _parse_scene_info(payload, revision)
        return result
    if operation == "execute_code":
        _require_exact_fields(result, {"scene_revision", "stdout", "saved"}, "execute-code")
        revision = result["scene_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "scene revision must be a non-negative integer")
        if not isinstance(result["stdout"], str) or result["saved"] is not True:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "execute-code result types are invalid")
        return result
    if operation == "import_asset":
        _require_exact_fields(result, {"scene_revision", "imported_object_names", "format"}, "import")
        revision = result["scene_revision"]
        names = result["imported_object_names"]
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or not isinstance(names, list)
            or not names
            or not all(isinstance(name, str) and bool(name) for name in names)
            or len(set(names)) != len(names)
            or not isinstance(result["format"], str)
        ):
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "import result types are invalid")
        try:
            ImportFormat(result["format"])
        except ValueError as exc:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "import result format is invalid") from exc
        return result
    if operation == "camera_render_preview":
        _require_exact_fields(result, {"content"}, "preview")
        content = result["content"]
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "preview must contain exactly one image")
        item = content[0]
        _require_exact_fields(item, {"type", "data", "mimeType"}, "preview image")
        if item["type"] != "image" or item["mimeType"] != "image/png" or not isinstance(item["data"], str):
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "preview image content is invalid")
        try:
            png = base64.b64decode(item["data"], validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "preview image data is not base64") from exc
        _validate_png(png, 2048)
        return result
    if operation == "export_scene":
        _require_exact_fields(result, {"scene_revision", "path", "format", "bytes"}, "export")
        revision = result["scene_revision"]
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or not isinstance(result["path"], str)
            or not result["path"]
            or not isinstance(result["format"], str)
            or not isinstance(result["bytes"], int)
            or isinstance(result["bytes"], bool)
            or result["bytes"] <= 0
        ):
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "export result types are invalid")
        try:
            ExportFormat(result["format"])
        except ValueError as exc:
            raise ProviderError(ErrorCode.RESULT_MALFORMED, "export result format is invalid") from exc
        return result
    raise ProviderError(ErrorCode.RESULT_MALFORMED, f"unknown result operation: {operation}")


def encode_result(value: dict[str, Any], operation: str | None = None) -> str:
    """Encode one checked v1 result deterministically for golden tests."""
    if not isinstance(value, dict):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "result must be an object")
    checked = check_public_result(operation, value) if operation is not None else value
    return json.dumps(checked, sort_keys=True, separators=(",", ":"))


def decode_result(value: str, operation: str | None = None) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "result is not valid JSON") from exc
    if not isinstance(result, dict):
        raise ProviderError(ErrorCode.RESULT_MALFORMED, "result must be an object")
    return check_public_result(operation, result) if operation is not None else result


def validate_preview_png(png: bytes, max_size: int) -> tuple[int, int]:
    """Validate a PNG preview before crossing the MCP image boundary."""
    size = parse_preview_size(max_size)
    if not isinstance(png, bytes):
        raise ProviderError(ErrorCode.PREVIEW_MISSING, "preview must be PNG bytes")
    return _validate_png(png, size.pixels)


def encode_preview_image_result(png: bytes, max_size: int) -> str:
    """Encode the single-image preview content item used by the wire golden."""
    _validate_png(png, parse_preview_size(max_size).pixels)
    return json.dumps(
        {
            "content": [
                {"type": "image", "data": base64.b64encode(png).decode("ascii"), "mimeType": "image/png"}
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
    )
