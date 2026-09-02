from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

import psutil
import pytest

from blender_mcp.headless_session import (
    SENTINEL_PREFIX,
    WIRE_VERSION,
    ErrorCode,
    ExportFormat,
    ProviderError,
    SceneSession,
    _parse_scene_info,
    _parse_sentinel,
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
    StrictExecutionError,
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
            matches = re.findall(r"filepath=([\"'])(.+?)\1", script)
            revision_path = Path(matches[0][1])
            revision_path.write_bytes(b"BLENDER-v402" + b"\0" * 32)
        if operation == "execute_code":
            data = {"scene_revision": 1, "saved": True, "stdout": "ok\n"}
        elif operation == "import_asset":
            data = {"scene_revision": 1, "imported_object_names": ["AssetRoot"], "format": "glb", "saved": True}
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


def test_checked_formats_and_preview_bounds():
    assert parse_import_format("glb").value == "glb"
    assert parse_export_format("blend").value == "blend"
    assert parse_preview_size(64).pixels == 64
    with pytest.raises(ProviderError):
        parse_preview_size(63)
    with pytest.raises(ProviderError) as caught:
        parse_import_format("blend")
    assert caught.value.code is ErrorCode.UNSUPPORTED_IMPORT_FORMAT


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
        "    revision = pathlib.Path(re.search(r\"filepath='([^']+)'\", script).group(1))\n"
        "    revision.write_bytes(b'BLENDER-v402' + b'\\0' * 32)\n"
        "    data = {'scene_revision': 1, 'saved': True, 'stdout': ''}\n"
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
async def test_timeout_and_cancellation_reap_the_direct_child(tmp_path: Path):
    executable = tmp_path / "fake-blender"
    executable.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | 0o111)
    script = tmp_path / "op.py"
    script.write_text("", encoding="utf-8")
    executor = StrictBlenderExecutor(str(executable), reap_grace_seconds=0.1)
    command = plan_strict_command(str(executable), None, script)
    with pytest.raises(StrictExecutionError, match="timed out"):
        await executor.execute(command, timeout=0.05)
    assert executor.active_process is None

    task = asyncio.create_task(executor.execute(command, timeout=30))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(StrictExecutionError, match="cancelled"):
        await task
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
