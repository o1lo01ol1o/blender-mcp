from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from fastmcp.exceptions import ValidationError as FastMCPValidationError

from blender_mcp.headless_server import create_headless_server
from blender_mcp.headless_session import ErrorCode, ProviderError, SceneSession
from blender_mcp.utils.blender_executor import MacOSDescendantProcessGuard, build_macos_process_profile


@pytest.mark.unit
def test_invalid_explicit_executable_fails_before_workspace_creation(tmp_path: Path):
    workspace = tmp_path / "must-not-exist"
    with pytest.raises(ProviderError) as caught:
        SceneSession(str(tmp_path / "missing-blender"), workspace=workspace)
    assert caught.value.code is ErrorCode.BLENDER_EXECUTABLE_MISSING
    assert not workspace.exists()


def test_non_blender_executable_fails_before_workspace_creation(tmp_path: Path):
    workspace = tmp_path / "must-not-exist"
    with pytest.raises(ProviderError) as caught:
        SceneSession(sys.executable, workspace=workspace)
    assert caught.value.code is ErrorCode.BLENDER_EXECUTABLE_MISSING
    assert not workspace.exists()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_headless_entry_point_exposes_exactly_five_tools(tmp_path: Path):
    session = SceneSession("/fake/blender", workspace=tmp_path / "session", executor=object())
    app = create_headless_server(session=session)
    listed_tools = await app.list_tools()
    assert [tool.name for tool in listed_tools] == [
        "blender_get_scene_info",
        "blender_execute_code",
        "blender_camera_render_preview",
        "blender_import_asset",
        "blender_export_scene",
    ]
    schema_golden = (
        Path(__file__).parents[1]
        / "golden/blender_headless_session_v1/tools-list.input-schemas.json"
    )
    assert {tool.name: tool.parameters for tool in listed_tools} == json.loads(
        schema_golden.read_text()
    )
    tools = {tool.name: tool for tool in listed_tools}
    assert tools["blender_export_scene"].annotations.readOnlyHint is False
    assert tools["blender_export_scene"].annotations.destructiveHint is True
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_boundary_errors_are_structured_and_happen_before_execution(tmp_path: Path):
    session = SceneSession("/fake/blender", workspace=tmp_path / "session", executor=object())
    app = create_headless_server(session=session)
    result = await app.call_tool("blender_execute_code", {"code": "import subprocess"})
    assert result.content[0].text == '{"error":{"code":"InvalidRequest","message":"process, display, and socket modules are not admitted"}}'
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_all_invalid_boundaries_skip_the_executor(tmp_path: Path):
    class CountingExecutor:
        def __init__(self):
            self.calls = 0
            self.active_process = None

        async def execute(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("invalid boundary reached Blender")

    staged = tmp_path / "staged"
    approved = tmp_path / "approved"
    staged.mkdir()
    approved.mkdir()
    executor = CountingExecutor()
    session = SceneSession(
        "/fake/blender",
        workspace=tmp_path / "session",
        executor=executor,
        staged_asset_roots=[staged],
        approved_output_roots=[approved],
    )
    app = create_headless_server(session=session)
    provider_invalid = [
        ("blender_execute_code", {"code": "import builtins; builtins.__import__('os')"}),
        ("blender_import_asset", {"path": str(staged / "missing.glb"), "format": "glb"}),
        ("blender_export_scene", {"path": str(tmp_path / "outside.blend"), "format": "blend", "selection_only": False}),
    ]
    for name, arguments in provider_invalid:
        result = await app.call_tool(name, arguments)
        assert result.content and json.loads(result.content[0].text)["error"]["code"]

    schema_invalid = [
        ("blender_execute_code", {"code": 123}),
        ("blender_camera_render_preview", {"max_size": 63}),
        ("blender_camera_render_preview", {"max_size": True}),
        ("blender_camera_render_preview", {"max_size": 2049}),
        ("blender_import_asset", {"path": str(staged / "missing.glb"), "format": "blend"}),
        ("blender_export_scene", {"path": str(approved / "scene.blend"), "format": "bad", "selection_only": False}),
        ("blender_export_scene", {"path": str(approved / "scene.blend"), "format": "blend", "selection_only": "no"}),
        ("blender_export_scene", {"path": str(approved / "scene.blend"), "format": "blend"}),
    ]
    for name, arguments in schema_invalid:
        with pytest.raises(FastMCPValidationError):
            await app.call_tool(name, arguments)
    assert executor.calls == 0
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_preview_tool_returns_one_image_content_item(tmp_path: Path):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP438DwHwAGgAJ/EEwb4QAAAABJRU5ErkJggg=="
    )
    session = SceneSession("/fake/blender", workspace=tmp_path / "session", executor=object())
    session.camera_render_preview = lambda _size: _async_bytes(png)  # type: ignore[method-assign]
    app = create_headless_server(session=session)
    result = await app.call_tool("blender_camera_render_preview", {"max_size": 64})
    assert len(result.content) == 1
    assert result.content[0].type == "image"
    assert result.content[0].mimeType == "image/png"
    assert not hasattr(result.content[0], "text") or result.content[0].text is None
    golden_path = (
        Path(__file__).parents[1]
        / "golden/blender_headless_session_v1/camera_render_preview.result.json"
    )
    golden = json.loads(golden_path.read_text())
    assert {
        "content": [
            {
                "type": result.content[0].type,
                "data": result.content[0].data,
                "mimeType": result.content[0].mimeType,
            }
        ]
    } == golden
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_text_result_goldens_cross_the_live_fastmcp_boundary(tmp_path: Path):
    root = Path(__file__).parents[1] / "golden/blender_headless_session_v1"
    staged = tmp_path / "staged"
    approved = tmp_path / "approved"
    staged.mkdir()
    approved.mkdir()
    asset = staged / "asset.glb"
    asset.write_bytes(b"glTF")
    session = SceneSession(
        "/fake/blender",
        workspace=tmp_path / "session",
        executor=object(),
        staged_asset_roots=[staged],
        approved_output_roots=[approved],
    )

    async def golden_result(operation: str) -> dict:
        return json.loads((root / f"{operation}.result.json").read_text())

    session.get_scene_info = lambda: golden_result("get_scene_info")  # type: ignore[method-assign]
    session.execute_code = lambda _program: golden_result("execute_code")  # type: ignore[method-assign]
    session.import_asset = lambda _path, _format: golden_result("import_asset")  # type: ignore[method-assign]
    session.export_scene = lambda _path, _format, _selection: golden_result("export_scene")  # type: ignore[method-assign]
    app = create_headless_server(session=session)
    calls = [
        ("blender_get_scene_info", {}, "get_scene_info"),
        (
            "blender_execute_code",
            json.loads((root / "execute_code.request.json").read_text()),
            "execute_code",
        ),
        ("blender_import_asset", {"path": str(asset), "format": "glb"}, "import_asset"),
        (
            "blender_export_scene",
            {"path": str(approved / "scene.blend"), "format": "blend", "selection_only": False},
            "export_scene",
        ),
    ]
    for tool, arguments, operation in calls:
        result = await app.call_tool(tool, arguments)
        assert json.loads(result.content[0].text) == await golden_result(operation)
    await session.teardown()


async def _async_bytes(value: bytes) -> bytes:
    return value


def test_macos_guard_profile_denies_fork_and_restricts_exec():
    profile = build_macos_process_profile("/Applications/Blender.app/Contents/MacOS/Blender")
    assert "(deny process-fork)" in profile
    assert "(deny process-exec)" in profile
    assert '(allow process-exec (literal "/Applications/Blender.app/Contents/MacOS/Blender"))' in profile
    assert MacOSDescendantProcessGuard("/Applications/Blender.app/Contents/MacOS/Blender").blender_executable.endswith("Blender")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_execute_code_fails_closed_without_process_guard(tmp_path: Path):
    class NoGuard:
        def available(self):
            return False

    from blender_mcp.utils.blender_executor import StrictBlenderExecutor

    executor = StrictBlenderExecutor("/bin/sh", process_guard=NoGuard())
    session = SceneSession("/bin/sh", workspace=tmp_path / "session", executor=executor)
    with pytest.raises(ProviderError) as caught:
        await session.execute_code("print('no child')")
    assert caught.value.code is ErrorCode.EXECUTE_CODE_GUARD_UNAVAILABLE
    await session.teardown()
