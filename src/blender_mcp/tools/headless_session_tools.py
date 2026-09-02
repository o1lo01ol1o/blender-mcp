"""Exactly five MCP tools for the strict-headless compatibility entry point."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastmcp.utilities.types import Image

from blender_mcp.headless_session import (
    ErrorCode,
    ProviderError,
    SceneSession,
    check_public_result,
    parse_camera_render_preview_request,
    parse_execute_code_request,
    parse_export_scene_request,
    parse_get_scene_info_request,
    parse_import_asset_request,
    validate_preview_png,
)

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_MUTATING = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
# Export writes the caller-approved filesystem path and may replace its prior
# contents; it is not read-only even though the scene revision is unchanged.
_EXPORT = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}

T = TypeVar("T")


def _error_result(error: ProviderError) -> dict[str, Any]:
    """Render a provider error without collapsing its code into a string."""
    return {"error": error.to_dict()}


async def _boundary_call[T](
    fn: Callable[[], Awaitable[T]], operation: str | None = None
) -> T | dict[str, Any]:
    try:
        result = await fn()
        if operation is not None:
            if not isinstance(result, dict):
                raise ProviderError(ErrorCode.RESULT_MALFORMED, "text tool result must be an object")
            return check_public_result(operation, result)
        return result
    except ProviderError as exc:
        return _error_result(exc)
    except Exception as exc:
        # Unexpected implementation failures still cross the MCP boundary as
        # structured data; expected provider cases never use this branch.
        return _error_result(ProviderError(ErrorCode.RESULT_MALFORMED, "headless provider operation failed", details={"cause": str(exc)}))


def register_headless_session_tools(app: Any, session: SceneSession) -> None:
    """Register the closed five-tool surface on an otherwise empty FastMCP app."""

    @app.tool(name="blender_get_scene_info", annotations=_READ_ONLY)
    async def blender_get_scene_info() -> dict[str, Any]:
        parse_get_scene_info_request()
        result = await _boundary_call(session.get_scene_info, "get_scene_info")
        return result  # type: ignore[return-value]

    @app.tool(name="blender_execute_code", annotations=_MUTATING)
    async def blender_execute_code(code: Any) -> dict[str, Any]:
        try:
            request = parse_execute_code_request(code)
        except ProviderError as exc:
            return _error_result(exc)
        result = await _boundary_call(
            lambda: session.execute_code(request.program), "execute_code"
        )
        return result  # type: ignore[return-value]

    @app.tool(name="blender_camera_render_preview", annotations=_READ_ONLY)
    async def blender_camera_render_preview(max_size: Any) -> Image | dict[str, Any]:
        try:
            request = parse_camera_render_preview_request(max_size)
        except ProviderError as exc:
            return _error_result(exc)
        result = await _boundary_call(lambda: session.camera_render_preview(request.size))
        if isinstance(result, dict):
            if "error" in result:
                return result
            return _error_result(ProviderError(ErrorCode.RESULT_MALFORMED, "preview result is invalid"))
        try:
            validate_preview_png(result, request.size.pixels)
        except ProviderError as exc:
            return _error_result(exc)
        return Image(data=result, format="png")

    @app.tool(name="blender_import_asset", annotations=_MUTATING)
    async def blender_import_asset(path: Any, format: Any) -> dict[str, Any]:
        try:
            request = parse_import_asset_request(path, format, session.staged_asset_roots)
        except ProviderError as exc:
            return _error_result(exc)
        result = await _boundary_call(
            lambda: session.import_asset(request.path, request.format), "import_asset"
        )
        return result  # type: ignore[return-value]

    @app.tool(name="blender_export_scene", annotations=_EXPORT)
    async def blender_export_scene(path: Any, format: Any, selection_only: Any = False) -> dict[str, Any]:
        try:
            request = parse_export_scene_request(
                path, format, selection_only, session.approved_output_roots
            )
        except ProviderError as exc:
            return _error_result(exc)
        result = await _boundary_call(
            lambda: session.export_scene(request.path, request.format, request.selection_only),
            "export_scene",
        )
        return result  # type: ignore[return-value]


register = register_headless_session_tools
