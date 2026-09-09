from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import psutil
import pytest

from blender_mcp.headless_session import ErrorCode, ProviderError, SceneSession
from blender_mcp.utils.blender_executor import StrictBlenderExecutor, plan_strict_command

EXPLICIT_BLENDER = Path("/Applications/Blender.app/Contents/MacOS/Blender")


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(not EXPLICIT_BLENDER.is_file(), reason="explicit supported Blender executable is unavailable")
@pytest.mark.asyncio
async def test_real_supported_blender_scene_session(tmp_path: Path):
    approved_root = tmp_path / "approved"
    approved_root.mkdir()
    session = SceneSession(
        str(EXPLICIT_BLENDER),
        workspace=tmp_path / "session",
        timeout_seconds=120,
        approved_output_roots=[approved_root],
    )
    try:
        first = await session.execute_code(
            """
import bpy
from mathutils import Vector
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
bpy.ops.mesh.primitive_cube_add()
bpy.context.object.name = 'Cube_A'
bpy.ops.object.camera_add(location=(5.0, -5.0, 4.0))
cam = bpy.context.object
cam.name = 'CAM_MAIN'
cam.rotation_euler = (Vector((0, 0, 0)) - cam.location).to_track_quat('-Z', 'Y').to_euler()
bpy.context.scene.camera = cam
bpy.ops.object.light_add(type='AREA', location=(2.0, -2.0, 5.0))
bpy.context.object.name = 'KEY_LIGHT'
bpy.context.object.data.energy = 1000
"""
        )
        assert first["scene_revision"] == 1
        second = await session.execute_code(
            """
import bpy
bpy.data.objects['Cube_A'].location.x = 2.0
bpy.ops.mesh.primitive_cube_add(location=(-1.0, 0.0, 0.0))
bpy.context.object.name = 'Cube_B'
"""
        )
        assert second["scene_revision"] == 2
        info = await session.get_scene_info()
        assert info["scene_revision"] == 2
        assert {obj["name"] for obj in info["objects"]} >= {"Cube_A", "Cube_B", "CAM_MAIN"}
        cube_a = next(obj for obj in info["objects"] if obj["name"] == "Cube_A")
        assert cube_a["location"][0] == pytest.approx(2.0)
        preview = await session.camera_render_preview(128)
        assert preview.startswith(b"\x89PNG\r\n\x1a\n") and len(preview) > 100
        assert any(byte != 0 for byte in preview[64:])

        exported = approved_root / "scene.blend"
        result = await session.export_scene(exported, "blend")
        assert result["scene_revision"] == 2 and result["bytes"] > 0

        reopen_script = tmp_path / "reopen.py"
        reopen_script.write_text(
            "import bpy, json; print('REOPEN:' + json.dumps(sorted(o.name for o in bpy.context.scene.objects)))\n",
            encoding="utf-8",
        )
        command = plan_strict_command(str(EXPLICIT_BLENDER), exported, reopen_script, ("reopen",))
        reopened = subprocess.run(command.argv, capture_output=True, text=True, check=False, timeout=120)
        assert reopened.returncode == 0, reopened.stderr
        assert "Cube_A" in reopened.stdout and "Cube_B" in reopened.stdout
        assert command.argv[1:3] == ("--background", "--factory-startup")
    finally:
        workspace = session.workspace
        await session.teardown()
        assert not workspace.exists()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(not EXPLICIT_BLENDER.is_file(), reason="explicit supported Blender executable is unavailable")
@pytest.mark.asyncio
async def test_failed_malicious_mutation_cannot_overwrite_previous_revision(tmp_path: Path):
    session = SceneSession(
        str(EXPLICIT_BLENDER), workspace=tmp_path / "session", timeout_seconds=120
    )
    try:
        await session.execute_code(
            "import bpy\n"
            "bpy.ops.mesh.primitive_cube_add()\n"
            "bpy.ops.object.camera_add(location=(0, -5, 0))\n"
            "bpy.context.scene.camera = bpy.context.object\n"
        )
        current = session.current_scene
        assert current is not None
        previous_bytes = current.read_bytes()
        with pytest.raises(ProviderError) as failed:
            await session.execute_code(
                "import bpy\n"
                "# This used to inherit the published .blend filepath.\n"
                "bpy.context.scene.render.filepath = bpy.data.filepath\n"
                "bpy.ops.render.render(write_still=True)\n"
                "raise RuntimeError('intentional failed mutation')\n"
            )
        assert failed.value.code is ErrorCode.RESULT_MALFORMED
        assert session.revision == 1
        assert session.current_scene == current
        assert current.read_bytes() == previous_bytes
    finally:
        await session.teardown()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(not EXPLICIT_BLENDER.is_file(), reason="explicit supported Blender executable is unavailable")
@pytest.mark.asyncio
async def test_teardown_reaps_active_real_blender_child(tmp_path: Path):
    session = SceneSession(
        str(EXPLICIT_BLENDER), workspace=tmp_path / "session", timeout_seconds=120
    )
    operation = asyncio.create_task(session.execute_code("while True:\n    pass"))
    deadline = asyncio.get_running_loop().time() + 30
    while session.active_process is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert session.active_process is not None
    child_pid = session.active_process.pid
    await session.teardown()
    assert operation.done()
    assert not psutil.pid_exists(child_pid)
    assert not session.workspace.exists()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(not EXPLICIT_BLENDER.is_file(), reason="explicit supported Blender executable is unavailable")
@pytest.mark.asyncio
async def test_macos_guard_denies_descendant_process_launch(tmp_path: Path):
    script = tmp_path / "attempt-child.py"
    script.write_text(
        "import subprocess\n"
        "try:\n"
        "    subprocess.run(['/bin/sh', '-c', 'echo CHILD_MARKER'], check=True)\n"
        "    print('CHILD_ALLOWED')\n"
        "except Exception as exc:\n"
        "    print('CHILD_DENIED:' + type(exc).__name__)\n",
        encoding="utf-8",
    )
    executor = StrictBlenderExecutor(str(EXPLICIT_BLENDER))
    command = plan_strict_command(str(EXPLICIT_BLENDER), None, script, ("guard-test",))
    result = await executor.execute(command, timeout=120, execute_code=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "CHILD_MARKER" not in result.stdout
    assert "CHILD_DENIED" in result.stdout
