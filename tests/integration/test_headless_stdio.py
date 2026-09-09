from __future__ import annotations

import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
import zlib
from pathlib import Path

import psutil
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from blender_mcp.headless_session import _is_plausible_blend_file, validate_preview_png
from blender_mcp.utils.blender_executor import plan_strict_command

EXPLICIT_BLENDER = Path("/Applications/Blender.app/Contents/MacOS/Blender")


def _png_has_non_black_pixel(data: bytes) -> bool:
    """Decode Blender's 8-bit non-interlaced PNG scanlines for real evidence."""
    offset = 8
    width = height = channels = 0
    compressed = bytearray()
    while offset < len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        chunk = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            channels = {2: 3, 6: 4}[chunk[9]]
        elif kind == b"IDAT":
            compressed.extend(chunk)
        elif kind == b"IEND":
            break
    raw = zlib.decompress(bytes(compressed))
    stride = width * channels
    previous = bytearray(stride)
    for row_index in range(height):
        row_start = row_index * (stride + 1)
        filter_kind = raw[row_start]
        encoded = raw[row_start + 1 : row_start + 1 + stride]
        decoded = bytearray(stride)
        for index, value in enumerate(encoded):
            left = decoded[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_kind == 0:
                predictor = 0
            elif filter_kind == 1:
                predictor = left
            elif filter_kind == 2:
                predictor = up
            elif filter_kind == 3:
                predictor = (left + up) // 2
            else:
                estimate = left + up - upper_left
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - upper_left))
                predictor = (left, up, upper_left)[distances.index(min(distances))]
            decoded[index] = (value + predictor) & 0xFF
        if any(max(decoded[index : index + 3]) > 2 for index in range(0, stride, channels)):
            return True
        previous = decoded
    return False


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(not EXPLICIT_BLENDER.is_file(), reason="explicit Blender executable is unavailable")
async def test_headless_stdio_initializes_and_lists_only_compatibility_tools():
    root = Path(__file__).parents[2]
    src = root / "src"
    env = {
        **os.environ,
        "PYTHONPATH": str(src),
        "BLENDER_EXECUTABLE": str(EXPLICIT_BLENDER),
    }
    transport = StdioTransport(
        sys.executable, ["-m", "blender_mcp.headless_server"], env=env, keep_alive=False
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
    assert [tool.name for tool in tools] == [
        "blender_get_scene_info",
        "blender_execute_code",
        "blender_camera_render_preview",
        "blender_import_asset",
        "blender_export_scene",
    ]
    schema_golden = (
        root / "tests/golden/blender_headless_session_v1/tools-list.input-schemas.json"
    )
    assert {tool.name: tool.inputSchema for tool in tools} == json.loads(
        schema_golden.read_text()
    )


def test_headless_stdio_missing_explicit_blender_fails_clearly():
    root = Path(__file__).parents[2]
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    env.pop("BLENDER_EXECUTABLE", None)
    result = subprocess.run(
        [sys.executable, "-m", "blender_mcp.headless_server"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "BlenderExecutableMissing" in result.stderr or "Blender executable is missing" in result.stderr


@pytest.mark.integration
@pytest.mark.skipif(not EXPLICIT_BLENDER.is_file(), reason="explicit Blender executable is unavailable")
def test_headless_stdio_sigterm_removes_private_workspace(tmp_path: Path):
    root = Path(__file__).parents[2]
    session_temp = tmp_path / "session-temp"
    session_temp.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
        "BLENDER_EXECUTABLE": str(EXPLICIT_BLENDER),
        "TMPDIR": str(session_temp),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "blender_mcp.headless_server"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        workspaces: list[Path] = []
        while time.monotonic() < deadline and process.poll() is None:
            workspaces = list(session_temp.glob("blender-headless-session-*"))
            if workspaces:
                break
            time.sleep(0.05)
        assert len(workspaces) == 1, "server did not create its private workspace"
        # Signal immediately after acquisition to exercise ownership transfer,
        # before FastMCP app construction is guaranteed to have completed.
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        assert stdout == ""
        assert not list(session_temp.glob("blender-headless-session-*"))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(not EXPLICIT_BLENDER.is_file(), reason="explicit supported Blender executable is unavailable")
async def test_real_fastmcp_stdio_headless_scene_flow(tmp_path: Path):
    root = Path(__file__).parents[2]
    approved = tmp_path / "approved"
    approved.mkdir()
    audit = tmp_path / "process-audit.jsonl"
    session_temp = tmp_path / "session-temp"
    session_temp.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
        "BLENDER_EXECUTABLE": str(EXPLICIT_BLENDER),
        "BLENDER_MCP_APPROVED_OUTPUT_ROOTS": str(approved),
        "BLENDER_MCP_PROCESS_AUDIT_PATH": str(audit),
        "TMPDIR": str(session_temp),
    }
    transport = StdioTransport(
        sys.executable, ["-m", "blender_mcp.headless_server"], env=env, keep_alive=False
    )
    async with Client(transport) as client:
        first = await client.call_tool(
            "blender_execute_code",
            {
                "code": (
                    "import bpy\n"
                    "from mathutils import Vector\n"
                    "bpy.ops.mesh.primitive_cube_add()\n"
                    "bpy.context.object.name = 'Cube_A'\n"
                    "bpy.ops.object.camera_add(location=(5, -5, 4))\n"
                    "cam = bpy.context.object\n"
                    "cam.name = 'CAM_MAIN'\n"
                    "cam.rotation_euler = (Vector((0, 0, 0)) - cam.location).to_track_quat('-Z', 'Y').to_euler()\n"
                    "bpy.context.scene.camera = cam\n"
                    "bpy.ops.object.light_add(location=(2, -2, 5))\n"
                )
            },
        )
        assert json.loads(first.content[0].text)["scene_revision"] == 1
        second = await client.call_tool(
            "blender_execute_code",
            {
                "code": (
                    "import bpy\n"
                    "bpy.data.objects['Cube_A'].location.x = 2.0\n"
                    "bpy.ops.mesh.primitive_cube_add(location=(-1, 0, 0))\n"
                    "bpy.context.object.name = 'Cube_B'\n"
                )
            },
        )
        assert json.loads(second.content[0].text)["scene_revision"] == 2
        info = await client.call_tool("blender_get_scene_info", {})
        info_data = json.loads(info.content[0].text)
        assert info_data["scene_revision"] == 2
        assert {obj["name"] for obj in info_data["objects"]} >= {"Cube_A", "Cube_B", "CAM_MAIN"}
        cube_a = next(obj for obj in info_data["objects"] if obj["name"] == "Cube_A")
        assert cube_a["location"][0] == pytest.approx(2.0)

        preview = await client.call_tool("blender_camera_render_preview", {"max_size": 128})
        assert len(preview.content) == 1 and preview.content[0].type == "image"
        png = base64.b64decode(preview.content[0].data)
        width, height = validate_preview_png(png, 128)
        assert (width, height) == (128, 72)
        assert _png_has_non_black_pixel(png)

        exported = approved / "scene.blend"
        export_result = await client.call_tool(
            "blender_export_scene", {"path": str(exported), "format": "blend", "selection_only": False}
        )
        assert json.loads(export_result.content[0].text)["scene_revision"] == 2
        assert _is_plausible_blend_file(exported)

    assert not list(session_temp.glob("blender-headless-session-*")), (
        "stdio shutdown must remove the private scene-session workspace"
    )

    reopen_script = tmp_path / "reopen.py"
    reopen_script.write_text(
        "import bpy; print('REOPEN:' + ','.join(sorted(obj.name for obj in bpy.context.scene.objects)))\n",
        encoding="utf-8",
    )
    reopen = plan_strict_command(str(EXPLICIT_BLENDER), exported, reopen_script, ("reopen",))
    reopened = subprocess.run(reopen.argv, capture_output=True, text=True, check=False, timeout=120)
    assert reopened.returncode == 0, reopened.stderr
    assert "REOPEN:" in reopened.stdout and "Cube_A" in reopened.stdout and "Cube_B" in reopened.stdout

    version = subprocess.run(
        [str(EXPLICIT_BLENDER), "--background", "--factory-startup", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert re.search(r"Blender (?:4\.2\.3|5\.2\.\d+)(?: LTS)?", version.stdout)
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(records) == 6
    assert records[0]["planned_argv"][-1] == "--version"
    assert all(record["direct_process_gone"] and record["descendants_gone"] for record in records)
    for record in records:
        planned = record["planned_argv"]
        assert planned[0] == str(EXPLICIT_BLENDER)
        assert "--background" in planned and "--factory-startup" in planned
        assert Path(record["launch_argv"][0]).name == "sandbox-exec"
        direct = record["direct_process"]
        assert direct is not None
        assert record["provider_descendants"] == [], "provider operations must not spawn descendants"
        assert record["provider_processes"] == [direct]
        command_line = direct["cmdline"]
        assert command_line, "the actual provider process command line must be observed"
        joined = " ".join(command_line)
        assert "Xvfb" not in joined
        assert "blender" in joined.lower() or "sandbox-exec" in joined
        assert "--background" in command_line and "--factory-startup" in command_line
        assert not psutil.pid_exists(direct["pid"])
