from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from pathlib import Path

import psutil
import pytest

from blender_mcp.headless_session import (
    SENTINEL_PREFIX,
    WIRE_VERSION,
    ErrorCode,
    ExportFormat,
    ImportFormat,
    ProviderError,
    SceneSession,
    _directory_capabilities,
    _parse_execute_result,
    _parse_scene_info,
    _parse_sentinel,
    _safe_dirfd_operations_available,
    admit_headless_bpy_program,
    build_provider_script,
    decode_request,
    decode_result,
    encode_preview_image_result,
    encode_request,
    encode_result,
    parse_asset_path,
    parse_export_format,
    parse_export_scene_request,
    parse_import_format,
    parse_preview_size,
    validate_preview_png,
)
from blender_mcp.utils.blender_executor import (
    RawBlenderObservation,
    StrictBlenderExecutor,
    StrictCommand,
    StrictExecutionError,
    StrictFailureKind,
    plan_strict_command,
)


class FakeExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.commands = []
        self.active_process = None

    async def execute(self, command, *, timeout, execute_code, cwd):
        self.commands.append((command, execute_code, cwd))
        operation = command.argv[-1]
        script = Path(command.argv[command.argv.index("--python") + 1]).read_text(encoding="utf-8")
        if self.fail:
            return RawBlenderObservation(1, "", "failed")
        if operation in {"execute_code", "import_asset"}:
            matches = re.findall(r"(?:filepath=|_save_and_inspect\()([\"'])(.+?)\1", script)
            revision_path = Path(matches[0][1])
            revision_path.write_bytes(b"BLENDER-v402" + b"\0" * 32)
        if operation == "execute_code":
            data = {
                "scene_revision": 1,
                "saved": True,
                "inspected": True,
                "stdout": "ok\n",
            }
        elif operation == "import_asset":
            data = {
                "scene_revision": 1,
                "imported_object_names": ["AssetRoot"],
                "format": "glb",
                "saved": True,
                "inspected": True,
            }
        elif operation == "get_scene_info":
            data = {
                "scene_name": "Scene",
                "frame_current": 1,
                "frame_start": 1,
                "frame_end": 250,
                "active_camera": None,
                "objects": [],
            }
        else:
            data = {"preview": True}
        sentinel = {"wire_version": "blender-headless-session/v1", "ok": True, "data": data}
        return RawBlenderObservation(0, "BLENDER_HEADLESS_SESSION_V1:" + json.dumps(sentinel), "")


@pytest.mark.unit
def test_strict_planner_has_only_headless_flags(tmp_path: Path):
    command = plan_strict_command("/Applications/Blender.app/Contents/MacOS/Blender", None, tmp_path / "op.py")
    assert command.argv[:3] == (
        "/Applications/Blender.app/Contents/MacOS/Blender",
        "--background",
        "--factory-startup",
    )
    assert "--python" in command.argv
    assert "--" in command.argv
    probe = plan_strict_command(
        "/Applications/Blender.app/Contents/MacOS/Blender", None, None, version_probe=True
    )
    assert probe.argv == (
        "/Applications/Blender.app/Contents/MacOS/Blender",
        "--background",
        "--factory-startup",
        "--version",
    )
    with pytest.raises(ValueError, match="strict-headless flags"):
        StrictCommand(
            executable="/Applications/Blender.app/Contents/MacOS/Blender",
            argv=(
                "/Applications/Blender.app/Contents/MacOS/Blender",
                "--factory-startup",
                "--background",
                "--version",
            ),
        )
    with pytest.raises(ValueError, match="GUI-mode flags"):
        StrictCommand(
            executable="/Applications/Blender.app/Contents/MacOS/Blender",
            argv=(
                "/Applications/Blender.app/Contents/MacOS/Blender",
                "--background",
                "--factory-startup",
                "--window-geometry",
            ),
        )


def test_prototype_sources_contain_no_gui_or_virtual_display_path():
    root = Path(__file__).parents[2]
    sources = [
        root / "src/blender_mcp/headless_server.py",
        root / "src/blender_mcp/headless_session.py",
        root / "src/blender_mcp/tools/headless_session_tools.py",
    ]
    prototype_source = "\n".join(path.read_text() for path in sources)
    for forbidden in (
        "Xvfb",
        "bpy.app.timers",
        "screenshot_viewport",
        "bpy.ops.render.opengl",
        "--window-geometry",
        "--no-background",
    ):
        assert forbidden not in prototype_source


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "",
        "import os",
        "import subprocess",
        "import builtins; builtins.__import__('os')",
        "from json import __builtins__ as caps; caps['exec']('import os')",
        "from json import *",
        "f = __import__; f('os')",
        "f = eval; f('1')",
        "f = getattr; f(object, '__subclasses__')",
        "try:\n  1 / 0\nexcept Exception as error:\n  caps = error.__traceback__.tb_frame.f_back.f_builtins",
        "import sys",
        "import importlib",
        "import operator; operator.attrgetter('__import__')",
        "import os; os.execv('/Applications/Blender.app/Contents/MacOS/Blender', [])",
        "eval('x')",
        "bpy.ops.wm.window_new()",
        "bpy.ops.script.python_file_run(filepath='/tmp/unsafe.py')",
        "bpy.ops.preferences.addon_enable(module='unsafe')",
        "bpy.ops.wm.save_as_mainfile(filepath='/tmp/outside.blend')",
    ],
)
def test_program_admission_rejects_unsafe_or_empty_source(source: str):
    with pytest.raises(ProviderError) as caught:
        admit_headless_bpy_program(source)
    assert caught.value.code is ErrorCode.INVALID_REQUEST


@pytest.mark.unit
def test_paths_are_root_contained_and_fail_closed(tmp_path: Path):
    staged = tmp_path / "staged"
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    staged.mkdir()
    approved.mkdir()
    outside.mkdir()
    asset = staged / "asset.glb"
    asset.write_bytes(b"glTF")
    outside_asset = outside / "asset.glb"
    outside_asset.write_bytes(b"glTF")
    (staged / "inside-link.glb").symlink_to(asset)
    (staged / "escape.glb").symlink_to(outside_asset)

    assert parse_asset_path(str(asset), [staged]) == asset
    assert parse_asset_path(str(staged / "inside-link.glb"), [staged]) == asset
    with pytest.raises(ProviderError) as caught:
        parse_asset_path(str(staged / "escape.glb"), [staged])
    assert caught.value.code is ErrorCode.INVALID_ASSET_PATH
    with pytest.raises(ProviderError) as caught:
        parse_asset_path(str(asset))
    assert caught.value.code is ErrorCode.INVALID_ASSET_PATH
    assert parse_export_scene_request(str(approved / "scene.blend"), "blend", False, [approved]).path == approved / "scene.blend"
    with pytest.raises(ProviderError) as caught:
        parse_export_scene_request(str(tmp_path / "scene.blend"), "blend", False, [approved])
    assert caught.value.code is ErrorCode.INVALID_OUTPUT_PATH


def test_constructor_fails_closed_without_safe_dirfd_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    staged = tmp_path / "staged"
    staged.mkdir()
    monkeypatch.setattr(os, "supports_dir_fd", set())
    assert _safe_dirfd_operations_available() is False
    with pytest.raises(ProviderError) as caught:
        SceneSession(
            "/fake/blender",
            workspace=tmp_path / "session",
            executor=FakeExecutor(),
            staged_asset_roots=[staged],
        )
    assert caught.value.code is ErrorCode.INVALID_ASSET_PATH


def test_constructor_closes_opened_root_descriptors_on_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    staged = tmp_path / "staged"
    approved = tmp_path / "approved"
    staged.mkdir()
    approved.mkdir()
    real_directory_capabilities = _directory_capabilities
    opened = []

    def open_capabilities(roots, error):
        if not opened:
            capabilities = real_directory_capabilities(roots, error)
            opened.extend(capabilities)
            return capabilities
        raise ProviderError(error, "simulated later root failure")

    monkeypatch.setattr(
        "blender_mcp.headless_session._directory_capabilities", open_capabilities
    )
    with pytest.raises(ProviderError, match="simulated later root failure"):
        SceneSession(
            "/fake/blender",
            workspace=tmp_path / "session",
            executor=FakeExecutor(),
            staged_asset_roots=[staged],
            approved_output_roots=[approved],
        )
    assert opened and all(capability.fd is None for capability in opened)


def test_checked_formats_and_preview_bounds():
    assert parse_import_format("glb").value == "glb"
    assert parse_export_format("blend").value == "blend"
    assert parse_preview_size(64).pixels == 64
    with pytest.raises(ProviderError):
        parse_preview_size(63)
    with pytest.raises(ProviderError) as caught:
        parse_import_format("blend")
    assert caught.value.code is ErrorCode.UNSUPPORTED_IMPORT_FORMAT


def test_mutation_scripts_reopen_revision_before_reporting_success(tmp_path: Path):
    script = build_provider_script(
        "execute_code",
        revision_number=1,
        revision_path=tmp_path / "revision.blend",
        author_code=admit_headless_bpy_program("print('ok')"),
    )
    call = f"_save_and_inspect({str(tmp_path / 'revision.blend')!r})"
    assert "bpy.ops.wm.open_mainfile(filepath=path)" in script
    assert script.index(call) < script.index("'inspected': True")


def test_usd_selection_is_admitted_and_planned(tmp_path: Path):
    approved = tmp_path / "approved"
    approved.mkdir()
    request = parse_export_scene_request(
        str(approved / "scene.usdc"), "usdc", True, [approved]
    )
    assert request.selection_only is True
    script = build_provider_script(
        "export_scene",
        revision_number=1,
        output_path=request.path,
        export_format=ExportFormat.USDC,
        selection_only=True,
    )
    assert "selected_objects_only=True" in script


@pytest.mark.asyncio
@pytest.mark.unit
async def test_initial_scene_read_fails_without_invoking_blender(tmp_path: Path):
    executor = FakeExecutor()
    session = SceneSession("/fake/blender", workspace=tmp_path / "session", executor=executor)
    with pytest.raises(ProviderError) as caught:
        await session.get_scene_info()
    assert caught.value.code is ErrorCode.SCENE_NOT_INITIALIZED
    assert executor.commands == []
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_failed_mutation_does_not_publish_revision(tmp_path: Path):
    executor = FakeExecutor(fail=True)
    session = SceneSession("/fake/blender", workspace=tmp_path / "session", executor=executor)
    with pytest.raises(ProviderError) as caught:
        await session.execute_code("print('hello')")
    assert caught.value.code is ErrorCode.BLENDER_EXITED_NONZERO
    assert session.revision == 0
    assert session.current_scene is None
    assert list(session.workspace.iterdir()) == []
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_failed_spawn_cleans_operation_and_temporary_revision_files(tmp_path: Path):
    class SpawnFailureExecutor(FakeExecutor):
        async def execute(self, command, *, timeout, execute_code, cwd):
            raise StrictExecutionError(StrictFailureKind.SPAWN_FAILED, "could not start Blender")

    session = SceneSession(
        "/fake/blender", workspace=tmp_path / "session", executor=SpawnFailureExecutor()
    )
    with pytest.raises(ProviderError) as caught:
        await session.execute_code("print('never ran')")
    assert caught.value.code is ErrorCode.BLENDER_EXECUTABLE_MISSING
    assert list(session.workspace.iterdir()) == []
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_import_uses_open_source_descriptor_after_path_admission(tmp_path: Path):
    staged = tmp_path / "staged"
    outside = tmp_path / "outside"
    staged.mkdir()
    outside.mkdir()
    asset = staged / "asset.glb"
    asset.write_bytes(b"glTF")
    outside_asset = outside / "asset.glb"
    outside_asset.write_bytes(b"outside")
    session = SceneSession(
        "/fake/blender",
        workspace=tmp_path / "session",
        executor=FakeExecutor(),
        staged_asset_roots=[staged],
    )
    admitted = parse_asset_path(str(asset), [staged])
    asset.unlink()
    asset.symlink_to(outside_asset)
    with pytest.raises(ProviderError) as caught:
        await session._run("import_asset", mutation=True, asset_path=admitted, import_format=ImportFormat.GLB)
    assert caught.value.code is ErrorCode.INVALID_ASSET_PATH
    assert session.executor.commands == []
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_export_publishes_through_held_parent_descriptor_after_path_admission(tmp_path: Path):
    approved = tmp_path / "approved"
    nested = approved / "nested"
    outside = tmp_path / "outside"
    approved.mkdir()
    nested.mkdir()
    outside.mkdir()
    destination = nested / "scene.blend"
    admitted = parse_export_scene_request(str(destination), "blend", False, [approved]).path
    class ExportExecutor(FakeExecutor):
        async def execute(self, command, *, timeout, execute_code, cwd):
            if execute_code:
                return await super().execute(
                    command, timeout=timeout, execute_code=execute_code, cwd=cwd
                )
            self.commands.append((command, execute_code, cwd))
            script = Path(command.argv[command.argv.index("--python") + 1]).read_text()
            target = Path(re.search(r"_path = '([^']+)'", script).group(1))
            target.write_bytes(b"private-export")
            payload = {
                "wire_version": WIRE_VERSION,
                "ok": True,
                "data": {"scene_revision": 1, "path": str(target), "format": "blend"},
            }
            return RawBlenderObservation(0, SENTINEL_PREFIX + json.dumps(payload), "")

    session = SceneSession(
        "/fake/blender",
        workspace=tmp_path / "session",
        executor=ExportExecutor(),
        approved_output_roots=[approved],
    )
    nested.rename(approved / "nested-real")
    nested.symlink_to(outside, target_is_directory=True)
    await session.execute_code("print('one')")
    with pytest.raises(ProviderError) as caught:
        await session._run(
            "export_scene",
            output_path=admitted,
            export_format=ExportFormat.BLEND,
        )
    assert caught.value.code is ErrorCode.INVALID_OUTPUT_PATH
    assert not (outside / "scene.blend").exists()
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_export_rejects_parent_swap_after_descriptor_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    approved = tmp_path / "approved"
    nested = approved / "nested"
    outside = tmp_path / "outside"
    approved.mkdir()
    nested.mkdir()
    outside.mkdir()
    destination = nested / "scene.blend"
    admitted = parse_export_scene_request(str(destination), "blend", False, [approved]).path

    class ExportExecutor(FakeExecutor):
        async def execute(self, command, *, timeout, execute_code, cwd):
            if execute_code:
                return await super().execute(
                    command, timeout=timeout, execute_code=execute_code, cwd=cwd
                )
            self.commands.append((command, execute_code, cwd))
            script = Path(command.argv[command.argv.index("--python") + 1]).read_text()
            target = Path(re.search(r"_path = '([^']+)'", script).group(1))
            target.write_bytes(b"private-export")
            payload = {
                "wire_version": WIRE_VERSION,
                "ok": True,
                "data": {"scene_revision": 1, "path": str(target), "format": "blend"},
            }
            return RawBlenderObservation(0, SENTINEL_PREFIX + json.dumps(payload), "")

    executor = ExportExecutor()
    session = SceneSession(
        "/fake/blender",
        workspace=tmp_path / "session",
        executor=executor,
        approved_output_roots=[approved],
    )
    await session.execute_code("print('one')")
    real_rename = os.rename
    swapped = False

    def swap_before_rename(source, target, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal swapped
        if target == "scene.blend" and not swapped:
            swapped = True
            real_rename(nested, approved / "nested-real")
            nested.symlink_to(outside, target_is_directory=True)
        return real_rename(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", swap_before_rename)
    with pytest.raises(ProviderError) as caught:
        await session._run(
            "export_scene",
            output_path=admitted,
            export_format=ExportFormat.BLEND,
        )
    assert caught.value.code is ErrorCode.INVALID_OUTPUT_PATH
    assert not (outside / "scene.blend").exists()
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_export_rejects_same_size_destination_substitution_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    approved = tmp_path / "approved"
    approved.mkdir()
    destination = approved / "scene.blend"
    substitute = approved / "substitute.blend"
    exported_content = b"exported!"
    attacker_content = b"attacker?"
    substitute.write_bytes(attacker_content)
    admitted = parse_export_scene_request(str(destination), "blend", False, [approved]).path

    class ExportExecutor(FakeExecutor):
        async def execute(self, command, *, timeout, execute_code, cwd):
            if execute_code:
                return await super().execute(
                    command, timeout=timeout, execute_code=execute_code, cwd=cwd
                )
            self.commands.append((command, execute_code, cwd))
            script = Path(command.argv[command.argv.index("--python") + 1]).read_text()
            target = Path(re.search(r"_path = '([^']+)'", script).group(1))
            target.write_bytes(exported_content)
            payload = {
                "wire_version": WIRE_VERSION,
                "ok": True,
                "data": {"scene_revision": 1, "path": str(target), "format": "blend"},
            }
            return RawBlenderObservation(0, SENTINEL_PREFIX + json.dumps(payload), "")

    executor = ExportExecutor()
    session = SceneSession(
        "/fake/blender",
        workspace=tmp_path / "session",
        executor=executor,
        approved_output_roots=[approved],
    )
    await session.execute_code("print('one')")
    real_rename = os.rename
    substituted = False

    def substitute_after_rename(source, target, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal substituted
        result = real_rename(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if target == "scene.blend" and not substituted:
            substituted = True
            real_rename(substitute, destination)
        return result

    monkeypatch.setattr(os, "rename", substitute_after_rename)
    with pytest.raises(ProviderError) as caught:
        await session._run(
            "export_scene",
            output_path=admitted,
            export_format=ExportFormat.BLEND,
        )
    assert caught.value.code is ErrorCode.INVALID_OUTPUT_PATH
    assert destination.read_bytes() == attacker_content
    assert not substitute.exists()
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_import_asset_starts_from_factory_and_publishes_revision(tmp_path: Path):
    staged = tmp_path / "staged"
    staged.mkdir()
    asset = staged / "asset.glb"
    asset.write_bytes(b"glTF")
    executor = FakeExecutor()
    session = SceneSession(
        "/fake/blender",
        workspace=tmp_path / "session",
        executor=executor,
        staged_asset_roots=[staged],
    )
    result = await session.import_asset(asset, "glb")
    assert result == {
        "scene_revision": 1,
        "imported_object_names": ["AssetRoot"],
        "format": "glb",
    }
    assert session.current_scene is not None
    command = executor.commands[0][0]
    assert command.argv[:3] == ("/fake/blender", "--background", "--factory-startup")
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_mutation_revision_and_scene_info_are_serialized(tmp_path: Path):
    executor = FakeExecutor()
    session = SceneSession("/fake/blender", workspace=tmp_path / "session", executor=executor)
    assert (await session.execute_code("print('hello')"))["scene_revision"] == 1
    info = await session.get_scene_info()
    assert info["scene_revision"] == 1
    assert len(executor.commands) == 2
    await session.teardown()
    assert not session.workspace.exists()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_read_queued_behind_initial_mutation_observes_revision_one(tmp_path: Path):
    started = asyncio.Event()
    release = asyncio.Event()

    class SerializedExecutor(FakeExecutor):
        async def execute(self, command, *, timeout, execute_code, cwd):
            if execute_code:
                started.set()
                await release.wait()
            return await super().execute(command, timeout=timeout, execute_code=execute_code, cwd=cwd)

    executor = SerializedExecutor()
    session = SceneSession("/fake/blender", workspace=tmp_path / "session", executor=executor)
    mutation = asyncio.create_task(session.execute_code("print('first')"))
    await started.wait()
    read = asyncio.create_task(session.get_scene_info())
    await asyncio.sleep(0)
    assert not read.done()
    release.set()
    assert (await mutation)["scene_revision"] == 1
    assert (await read)["scene_revision"] == 1
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_queued_call_rechecks_closed_state_after_teardown(tmp_path: Path):
    started = asyncio.Event()
    release = asyncio.Event()

    class SerializedExecutor(FakeExecutor):
        async def execute(self, command, *, timeout, execute_code, cwd):
            started.set()
            await release.wait()
            return await super().execute(command, timeout=timeout, execute_code=execute_code, cwd=cwd)

    executor = SerializedExecutor()
    session = SceneSession("/fake/blender", workspace=tmp_path / "session", executor=executor)
    first = asyncio.create_task(session.execute_code("print('first')"))
    await started.wait()
    queued = asyncio.create_task(session.execute_code("print('queued')"))
    await asyncio.sleep(0)
    await session.teardown()
    release.set()
    with pytest.raises(ProviderError) as caught:
        await queued
    assert caught.value.code is ErrorCode.INVALID_REQUEST
    assert len(executor.commands) == 0
    assert first.done()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_existing_export_is_removed_before_invocation(tmp_path: Path):
    approved = tmp_path / "approved"
    approved.mkdir()
    output = approved / "scene.blend"
    output.write_bytes(b"stale")
    session = SceneSession(
        "/fake/blender",
        workspace=tmp_path / "session",
        executor=FakeExecutor(),
        approved_output_roots=[approved],
    )
    await session.execute_code("print('one')")

    class ExportExecutor(FakeExecutor):
        async def execute(self, command, *, timeout, execute_code, cwd):
            script = Path(command.argv[command.argv.index("--python") + 1]).read_text()
            target = Path(re.search(r"_path = '([^']+)'", script).group(1))
            assert not target.exists()
            target.write_bytes(b"export")
            data = {
                "scene_revision": 1,
                "path": str(target),
                "format": "blend",
            }
            sentinel = {"wire_version": "blender-headless-session/v1", "ok": True, "data": data}
            return RawBlenderObservation(0, "BLENDER_HEADLESS_SESSION_V1:" + json.dumps(sentinel), "")

    session.executor = ExportExecutor()
    result = await session.export_scene(output, "blend")
    assert result["bytes"] == len(b"export")
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_corrupt_second_mutation_preserves_first_revision(tmp_path: Path):
    class CorruptSecondExecutor(FakeExecutor):
        async def execute(self, command, *, timeout, execute_code, cwd):
            result = await super().execute(command, timeout=timeout, execute_code=execute_code, cwd=cwd)
            if execute_code and len(self.commands) == 2:
                revision_path = next(cwd.glob(".scene-*.blend"))
                revision_path.write_bytes(b"not-a-blend")
            return result

    session = SceneSession("/fake/blender", workspace=tmp_path / "session", executor=CorruptSecondExecutor())
    assert (await session.execute_code("print('one')"))["scene_revision"] == 1
    with pytest.raises(ProviderError) as caught:
        await session.execute_code("print('two')")
    assert caught.value.code is ErrorCode.SCENE_REVISION_MISSING
    assert session.revision == 1
    assert session.current_scene is not None and session.current_scene.read_bytes().startswith(b"BLENDER-v402")
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_teardown_cancels_active_task(tmp_path: Path):
    started = asyncio.Event()

    class HangingExecutor(FakeExecutor):
        async def execute(self, command, *, timeout, execute_code, cwd):
            started.set()
            await asyncio.Event().wait()

    session = SceneSession("/fake/blender", workspace=tmp_path / "session", executor=HangingExecutor())
    task = asyncio.create_task(session.execute_code("print('hello')"))
    await started.wait()
    await session.teardown()
    assert task.done()
    assert not session.workspace.exists()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_concurrent_teardown_callers_wait_for_complete_cleanup(tmp_path: Path):
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_cleanup = asyncio.Event()

    class SlowCancellationExecutor(FakeExecutor):
        async def execute(self, command, *, timeout, execute_code, cwd):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release_cleanup.wait()
                raise

    session = SceneSession(
        "/fake/blender", workspace=tmp_path / "session", executor=SlowCancellationExecutor()
    )
    operation = asyncio.create_task(session.execute_code("print('active')"))
    await started.wait()
    first = asyncio.create_task(session.teardown())
    await cancellation_seen.wait()
    second = asyncio.create_task(session.teardown())
    await asyncio.sleep(0)
    assert not second.done()
    release_cleanup.set()
    await asyncio.gather(first, second)
    assert operation.done()
    assert session._teardown_complete is True
    assert not session.workspace.exists()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_descendants_are_captured_and_reaped_after_parent_exit(tmp_path: Path):
    executable = tmp_path / "fake-blender"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('CHILD:' + str(child.pid), flush=True)\n"
        "time.sleep(0.2)\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | 0o111)
    script = tmp_path / "op.py"
    script.write_text("", encoding="utf-8")
    audit = tmp_path / "audit.jsonl"

    class AllowGuard:
        def available(self):
            return True

        def wrap(self, argv):
            return argv

    executor = StrictBlenderExecutor(
        str(executable), process_guard=AllowGuard(), reap_grace_seconds=0.1, audit_path=audit
    )
    command = plan_strict_command(str(executable), None, script)
    result = await executor.execute(command, timeout=5, execute_code=True)
    assert result.returncode == 0
    record = json.loads(audit.read_text().splitlines()[0])
    assert record["provider_descendants"]
    assert record["descendants_gone"] is True
    assert all(not psutil.pid_exists(item["pid"]) for item in record["provider_descendants"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_process_group_reaps_immediate_orphan_before_descendant_poll(tmp_path: Path):
    executable = tmp_path / "fake-blender"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import os, time; os.close(1); os.close(2); time.sleep(30)'])\n"
        "with open('child.pid', 'w') as stream:\n"
        "    stream.write(str(child.pid))\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | 0o111)
    script = tmp_path / "op.py"
    script.write_text("", encoding="utf-8")

    class AllowGuard:
        def available(self):
            return True

        def wrap(self, argv):
            return argv

    executor = StrictBlenderExecutor(str(executable), process_guard=AllowGuard(), reap_grace_seconds=0.2)

    async def no_descendant_poll(*args, **kwargs):
        return

    executor._track_descendants = no_descendant_poll
    command = plan_strict_command(str(executable), None, script)
    result = await executor.execute(command, timeout=5, cwd=tmp_path, execute_code=True)
    assert result.returncode == 0
    child_pid = int((tmp_path / "child.pid").read_text())
    assert not psutil.pid_exists(child_pid)
    assert executor.active_process is None
    assert executor.active_process_group_id is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_descendant_ownership_survives_failed_reap_until_teardown_retry(tmp_path: Path):
    executable = tmp_path / "fake-blender"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | 0o111)

    class AllowGuard:
        def available(self):
            return True

        def wrap(self, argv):
            return argv

    executor = StrictBlenderExecutor(str(executable), process_guard=AllowGuard(), reap_grace_seconds=0.05)
    session = SceneSession(
        str(executable), workspace=tmp_path / "session", executor=executor, timeout_seconds=30
    )
    original_reap = executor._reap_captured
    failures = [4]

    async def fail_first_reaps(captured, *, force):
        if failures[0] and captured:
            failures[0] -= 1
            raise StrictExecutionError(StrictFailureKind.REAP_FAILED, "adversarial reap failure")
        return await original_reap(captured, force=force)

    executor._reap_captured = fail_first_reaps
    operation = asyncio.create_task(session.execute_code("print('active')"))
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while (
            (executor.active_process is None or not executor.active_descendants)
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        assert executor.active_process is not None
        descendant_pids = set(executor.active_descendants)
        assert descendant_pids
        operation.cancel()
        with pytest.raises(ProviderError) as caught:
            await operation
        assert caught.value.code is ErrorCode.BLENDER_LIFECYCLE_FAILURE
        assert set(executor.active_descendants) == descendant_pids
        # Group cleanup may already have signalled the OS process; the
        # invariant is that captured handles survive the failed bracket and
        # remain available to teardown retry.
        # The first operation failed hard, but teardown can retry using the
        # executor-owned descendant handles rather than a discarded local map.
        executor._reap_captured = original_reap
        await session.teardown()
        assert executor.active_process is None
        assert all(not psutil.pid_exists(pid) for pid in descendant_pids)
    finally:
        executor._reap_captured = original_reap
        if not session._teardown_complete:
            await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_session_cancellation_preserves_existing_revision(tmp_path: Path):
    executable = tmp_path / "fake-blender"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, re, sys, time\n"
        "state = pathlib.Path(sys.argv[0] + '.count')\n"
        "count = int(state.read_text()) if state.exists() else 0\n"
        "state.write_text(str(count + 1))\n"
        "if count == 0:\n"
        "    script = pathlib.Path(sys.argv[sys.argv.index('--python') + 1]).read_text()\n"
        "    revision = pathlib.Path(re.search(r\"_save_and_inspect\\('([^']+)'\\)\", script).group(1))\n"
        "    revision.write_bytes(b'BLENDER-v402' + b'\\0' * 32)\n"
        "    data = {'scene_revision': 1, 'saved': True, 'inspected': True, 'stdout': ''}\n"
        "    payload = {'wire_version': 'blender-headless-session/v1', 'ok': True, 'data': data}\n"
        "    print('BLENDER_HEADLESS_SESSION_V1:' + json.dumps(payload))\n"
        "else:\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | 0o111)

    class AllowGuard:
        def available(self):
            return True

        def wrap(self, argv):
            return argv

    executor = StrictBlenderExecutor(str(executable), process_guard=AllowGuard(), reap_grace_seconds=0.1)
    session = SceneSession(str(executable), workspace=tmp_path / "session", executor=executor, timeout_seconds=30)
    first = await session.execute_code("print('one')")
    assert first["scene_revision"] == 1
    current = session.current_scene
    session.timeout_seconds = 0.05
    with pytest.raises(ProviderError) as timed_out:
        await session.execute_code("print('timeout')")
    assert timed_out.value.code is ErrorCode.BLENDER_TIMED_OUT
    assert session.revision == 1 and session.current_scene == current
    assert executor.active_process is None

    session.timeout_seconds = 30
    task = asyncio.create_task(session.execute_code("print('two')"))
    while executor.active_process is None:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(ProviderError) as caught:
        await task
    assert caught.value.code is ErrorCode.BLENDER_CANCELLED
    assert session.revision == 1 and session.current_scene == current
    assert executor.active_process is None
    assert not list(session.workspace.glob(".scene-00000002-*.blend"))
    await session.teardown()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hard_reap_failure_retains_executor_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = tmp_path / "fake-blender"
    script = tmp_path / "op.py"
    executable.write_text("", encoding="utf-8")
    script.write_text("", encoding="utf-8")

    class HostileProcess:
        pid = 99_999_991
        returncode = None

        async def communicate(self):
            raise TimeoutError

        async def wait(self):
            raise TimeoutError

    process = HostileProcess()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    class NoGuard:
        def available(self):
            return False

    executor = StrictBlenderExecutor(str(executable), process_guard=NoGuard(), reap_grace_seconds=0.01)

    async def hostile_reap(*args, **kwargs):
        raise StrictExecutionError(StrictFailureKind.REAP_FAILED, "hostile child did not reap")

    monkeypatch.setattr(executor, "_terminate_and_reap", hostile_reap)
    command = plan_strict_command(str(executable), None, script)
    with pytest.raises(StrictExecutionError) as caught:
        await executor.execute(command, timeout=0.01)
    assert caught.value.kind is StrictFailureKind.REAP_FAILED
    assert executor.active_process is process
    executor.active_process = None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_timeout_and_cancellation_reap_the_direct_child(tmp_path: Path):
    executable = tmp_path / "fake-blender"
    executable.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | 0o111)
    script = tmp_path / "op.py"
    script.write_text("", encoding="utf-8")
    class NoGuard:
        def available(self):
            return False

    executor = StrictBlenderExecutor(
        str(executable), process_guard=NoGuard(), reap_grace_seconds=0.1
    )
    command = plan_strict_command(str(executable), None, script)
    with pytest.raises(StrictExecutionError, match="timed out") as timed_out:
        await executor.execute(command, timeout=0.05)
    assert timed_out.value.kind is StrictFailureKind.TIMED_OUT
    assert executor.active_process is None

    task = asyncio.create_task(executor.execute(command, timeout=30))
    while executor.active_process is None:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(StrictExecutionError, match="cancelled") as cancelled:
        await task
    assert cancelled.value.kind is StrictFailureKind.CANCELLED
    assert executor.active_process is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_reap_bracket_survives_repeated_cancellation(tmp_path: Path):
    executable = tmp_path / "fake-blender"
    executable.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | 0o111)
    script = tmp_path / "op.py"
    script.write_text("", encoding="utf-8")

    class NoGuard:
        def available(self):
            return False

    executor = StrictBlenderExecutor(str(executable), process_guard=NoGuard(), reap_grace_seconds=0.1)
    original_reap = executor._terminate_and_reap
    reap_started = asyncio.Event()
    release_reap = asyncio.Event()

    async def delayed_reap(*args, **kwargs):
        reap_started.set()
        await release_reap.wait()
        return await original_reap(*args, **kwargs)

    executor._terminate_and_reap = delayed_reap
    command = plan_strict_command(str(executable), None, script)
    operation = asyncio.create_task(executor.execute(command, timeout=30))
    try:
        while executor.active_process is None:
            await asyncio.sleep(0)
        operation.cancel()
        await reap_started.wait()
        operation.cancel()
        release_reap.set()
        with pytest.raises(StrictExecutionError) as caught:
            await operation
        assert caught.value.kind is StrictFailureKind.CANCELLED
        assert executor.active_process is None
    finally:
        executor._terminate_and_reap = original_reap
        if executor.active_process is not None:
            await original_reap(executor.active_process)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_cancellation_during_spawn_publishes_then_reaps_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = tmp_path / "fake-blender"
    executable.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | 0o111)
    script = tmp_path / "op.py"
    script.write_text("", encoding="utf-8")
    spawned = asyncio.Event()
    release_spawn = asyncio.Event()
    child: list[asyncio.subprocess.Process] = []
    real_spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        child.append(process)
        spawned.set()
        await release_spawn.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)

    class NoGuard:
        def available(self):
            return False

    executor = StrictBlenderExecutor(
        str(executable), process_guard=NoGuard(), reap_grace_seconds=0.1
    )
    command = plan_strict_command(str(executable), None, script)
    task = asyncio.create_task(executor.execute(command, timeout=30))
    await spawned.wait()
    task.cancel()
    # A second cancellation while ownership is being transferred must not
    # interrupt the shielded spawn/reap bracket.
    task.cancel()
    release_spawn.set()
    with pytest.raises(StrictExecutionError, match="cancelled during spawn") as cancelled:
        await task
    assert cancelled.value.kind is StrictFailureKind.CANCELLED
    assert child and not psutil.pid_exists(child[0].pid)
    assert executor.active_process is None


def test_result_wire_goldens_round_trip_by_operation():
    root = Path(__file__).parents[1] / "golden" / "blender_headless_session_v1"
    for operation in ("execute_code", "import_asset", "export_scene"):
        value = decode_result((root / f"{operation}.result.json").read_text(encoding="utf-8"), operation)
        assert decode_result(encode_result(value, operation), operation) == value
    scene = decode_result((root / "get_scene_info.result.json").read_text(encoding="utf-8"), "get_scene_info")
    assert decode_result(encode_result(scene, "get_scene_info"), "get_scene_info") == scene


def test_request_wire_goldens_round_trip(tmp_path: Path):
    root = Path(__file__).parents[1] / "golden" / "blender_headless_session_v1"
    requests = {
        "get_scene_info": decode_request("get_scene_info", (root / "get_scene_info.request.json").read_text()),
        "execute_code": decode_request("execute_code", (root / "execute_code.request.json").read_text()),
        "camera_render_preview": decode_request(
            "camera_render_preview", (root / "camera_render_preview.request.json").read_text()
        ),
    }
    staged = tmp_path / "staged"
    approved = tmp_path / "approved"
    staged.mkdir()
    approved.mkdir()
    asset = staged / "asset.glb"
    asset.write_bytes(b"glTF")
    import_golden = json.loads((root / "import_asset.request.json").read_text())
    assert import_golden == {"path": "/absolute/staged/asset.glb", "format": "glb"}
    import_golden["path"] = str(asset)
    requests["import_asset"] = decode_request(
        "import_asset", json.dumps(import_golden), staged_asset_roots=[staged]
    )
    export_golden = json.loads((root / "export_scene.request.json").read_text())
    assert export_golden == {
        "path": "/absolute/approved/output/scene.blend",
        "format": "blend",
        "selection_only": False,
    }
    export_golden["path"] = str(approved / "scene.blend")
    requests["export_scene"] = decode_request(
        "export_scene", json.dumps(export_golden), approved_output_roots=[approved]
    )
    for operation, request in requests.items():
        assert decode_request(
            operation,
            encode_request(operation, request),
            staged_asset_roots=[staged],
            approved_output_roots=[approved],
        ) == request


def test_preview_result_golden_is_one_image_content_item():
    root = Path(__file__).parents[1] / "golden" / "blender_headless_session_v1"
    encoded = (root / "camera_render_preview.result.json").read_text()
    content = decode_result(encoded, "camera_render_preview")
    png = base64.b64decode(content["content"][0]["data"])
    assert json.loads(encode_preview_image_result(png, 64)) == content
    assert decode_result(encode_result(content, "camera_render_preview"), "camera_render_preview") == content
    assert len(content["content"]) == 1 and content["content"][0]["type"] == "image"
    assert "text" not in content["content"][0]


def test_sentinel_errors_are_distinct_and_structured():
    with pytest.raises(ProviderError) as missing:
        _parse_sentinel("ordinary Blender logging")
    assert missing.value.code is ErrorCode.RESULT_SENTINEL_MISSING

    with pytest.raises(ProviderError) as malformed:
        _parse_sentinel(SENTINEL_PREFIX + "not-json")
    assert malformed.value.code is ErrorCode.RESULT_MALFORMED

    active_camera_error = {
        "wire_version": WIRE_VERSION,
        "ok": False,
        "error_code": "ActiveCameraMissing",
        "error_message": "scene has no active camera",
    }
    with pytest.raises(ProviderError) as camera:
        _parse_sentinel(SENTINEL_PREFIX + json.dumps(active_camera_error))
    assert camera.value.code is ErrorCode.ACTIVE_CAMERA_MISSING

    with pytest.raises(ProviderError) as float_revision:
        _parse_execute_result(
            {"scene_revision": 1.0, "saved": True, "inspected": True, "stdout": ""}, 1
        )
    assert float_revision.value.code is ErrorCode.RESULT_MALFORMED


def test_unknown_scene_object_kind_is_rejected():
    payload = {
        "scene_name": "Scene",
        "frame_current": 1,
        "frame_start": 1,
        "frame_end": 250,
        "active_camera": None,
        "objects": [
            {
                "name": "mystery",
                "type": "NOT_A_BLENDER_KIND",
                "location": [0, 0, 0],
                "rotation_euler": [0, 0, 0],
                "scale": [1, 1, 1],
                "visible": True,
            }
        ],
    }
    with pytest.raises(ProviderError):
        _parse_scene_info(payload, 1)


def test_corrupt_preview_png_is_rejected():
    root = Path(__file__).parents[1] / "golden" / "blender_headless_session_v1"
    content = json.loads((root / "camera_render_preview.result.json").read_text())
    png = bytearray(base64.b64decode(content["content"][0]["data"]))
    png[-1] ^= 1
    with pytest.raises(ProviderError) as caught:
        validate_preview_png(bytes(png), 64)
    assert caught.value.code is ErrorCode.PREVIEW_MISSING
