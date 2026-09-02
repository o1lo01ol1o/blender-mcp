"""Comprehensive Blender script executor with extensive error handling and logging."""

import asyncio
import contextlib
import json

# Third-party imports
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeVar

import psutil

from ..compat import *

logger = logging.getLogger(__name__)
from ..config import BLENDER_EXECUTABLE, validate_blender_executable
from ..exceptions import BlenderNotFoundError, BlenderScriptError

# Type variable for the BlenderExecutor class
T = TypeVar("T", bound="BlenderExecutor")

# Global instance of BlenderExecutor
_blender_executor_instance = None


def get_blender_executor(blender_executable: str | None = None, headless: bool = True) -> "BlenderExecutor":
    """Get or create a singleton instance of BlenderExecutor.

    Args:
        blender_executable: Path to the Blender executable or command name.
                          If None, uses the configured BLENDER_EXECUTABLE.
        headless: Whether to run Blender in headless mode (default: True)

    Returns:
        BlenderExecutor: The singleton instance of the BlenderExecutor.
    """
    global _blender_executor_instance

    # Reset singleton if mode changes
    if _blender_executor_instance is not None and _blender_executor_instance.headless != headless:
        _blender_executor_instance = None

    if _blender_executor_instance is None:
        # Use the configured path if no explicit path is provided
        executable_to_use = blender_executable or BLENDER_EXECUTABLE
        _blender_executor_instance = BlenderExecutor(executable_to_use, headless)

    return _blender_executor_instance


class BlenderExecutor:
    """Comprehensive Blender script executor with robust error handling."""

    def __init__(self, blender_executable: str | None = None, headless: bool = True):
        # Use the provided executable, or fall back to the one from config
        self.blender_executable = blender_executable or BLENDER_EXECUTABLE
        self.blender_version = None
        self.blender_path = None
        self.temp_dir = None
        self.process_timeout = 300
        self.max_retries = 3
        self.headless = headless  # Whether to run in headless mode
        self._initialized = False

    def _initialize_executor(self) -> None:
        """Initialize executor with comprehensive validation and setup."""
        if self._initialized:
            return

        # Validate the Blender executable before initialization
        if not validate_blender_executable():
            raise BlenderNotFoundError(f"Blender executable not found at: {self.blender_executable}")

        try:
            logger.info(f"Initializing Blender executor with executable: {self.blender_executable}")

            # Find and validate Blender executable
            self._locate_and_validate_blender()

            # Setup temporary directory
            self._setup_temp_directory()

            # Test basic Blender functionality
            self._test_blender_functionality()

            self._initialized = True
            logger.info(f"Blender executor initialized successfully - Version: {self.blender_version}")

        except Exception as e:
            logger.error(f"Failed to initialize Blender executor: {e!s}")
            raise BlenderNotFoundError(f"Executor initialization failed: {e!s}")

    def _locate_and_validate_blender(self) -> None:
        """Locate and validate the Blender executable."""
        # First, check the explicitly configured path
        if self.blender_executable:
            # Try with .exe extension if not already present on Windows
            if os.name == "nt" and not self.blender_executable.lower().endswith(".exe"):
                exe_path = f"{self.blender_executable}.exe"
                if os.path.isfile(exe_path):
                    self.blender_path = Path(exe_path).resolve()
                    logger.info(f"Using configured Blender executable: {self.blender_path}")
                    self._validate_blender_version()
                    return

            # Try the path as-is
            if os.path.isfile(self.blender_executable):
                self.blender_path = Path(self.blender_executable).resolve()
                logger.info(f"Using configured Blender executable: {self.blender_path}")
                self._validate_blender_version()
                return

            # If we have a directory, look for the executable inside it
            if os.path.isdir(self.blender_executable):
                # Check for common executable names in the directory
                for exe_name in ["blender", "blender.exe"]:
                    exe_path = os.path.join(self.blender_executable, exe_name)
                    if os.path.isfile(exe_path):
                        self.blender_path = Path(exe_path).resolve()
                        logger.info(f"Found Blender in configured directory: {self.blender_path}")
                        self._validate_blender_version()
                        return

        # If we get here, the configured path didn't work, try other methods
        # Common Blender executable names and paths
        blender_names = ["blender", "blender.exe"]

        # Common installation paths - include the configured path in the error message
        common_paths = [
            str(self.blender_executable) if self.blender_executable else "<configured path>",
            "C:\\Program Files\\Blender Foundation\\Blender 4.4\\blender.exe",
            "C:\\Program Files\\Blender Foundation\\Blender 4.0\\blender.exe",
            "C:\\Program Files\\Blender Foundation\\Blender 4.1\\blender.exe",
            "C:\\Program Files\\Blender Foundation\\Blender 4.2\\blender.exe",
            "C:\\Program Files\\Blender Foundation\\Blender 3.6\\blender.exe",
            "/usr/bin/blender",
            "/usr/local/bin/blender",
            "/Applications/Blender.app/Contents/MacOS/Blender",
        ]

        # Check if the executable is in PATH
        for name in blender_names:
            try:
                result = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    self.blender_path = Path(shutil.which(name)).resolve()
                    logger.info(f"Found Blender in PATH: {self.blender_path}")
                    self._validate_blender_version()
                    return
            except (subprocess.SubprocessError, FileNotFoundError):
                continue

        # Check common installation paths
        for path in common_paths:
            if not path or path == "<configured path>":
                continue

            # Try with .exe extension if not already present on Windows
            if os.name == "nt" and not path.lower().endswith(".exe"):
                exe_path = f"{path}.exe"
                if os.path.isfile(exe_path):
                    self.blender_path = Path(exe_path).resolve()
                    logger.info(f"Found Blender at common location: {self.blender_path}")
                    self._validate_blender_version()
                    return

            # Try the path as-is
            if os.path.isfile(path):
                self.blender_path = Path(path).resolve()
                logger.info(f"Found Blender at common location: {self.blender_path}")
                self._validate_blender_version()
                return

        # If we get here, we couldn't find Blender
        error_msg = "No valid Blender installation found. "
        if self.blender_executable:
            error_msg += f"Configured path: {self.blender_executable} does not exist. "
        error_msg += f"Tried: {[p for p in common_paths if p and p != '<configured path>']}"

        raise BlenderNotFoundError(error_msg)

    def _validate_blender_version(self) -> None:
        """Validate the Blender version and set the version attribute."""
        try:
            result = subprocess.run([str(self.blender_path), "--version"], capture_output=True, text=True, timeout=10)

            if result.returncode == 0 and "Blender" in result.stdout:
                version_line = result.stdout.split("\n")[0]
                self.blender_version = version_line.strip()
                logger.info(f"Validated Blender version: {self.blender_version}")
            else:
                logger.warning(f"Could not determine Blender version: {result.stderr}")
                self.blender_version = "unknown"

        except Exception as e:
            logger.error(f"Error validating Blender version: {e!s}")
            raise BlenderScriptError("", f"Blender version validation failed: {e!s}")

    def _setup_temp_directory(self) -> None:
        """Setup temporary directory for Blender operations."""
        try:
            self.temp_dir = tempfile.mkdtemp(prefix="blender_mcp_")
            logger.debug(f"Created temp directory: {self.temp_dir}")

            # Verify temp directory is writable
            test_file = os.path.join(self.temp_dir, "test_write.txt")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)

        except Exception as e:
            logger.error(f"Failed to setup temp directory: {e!s}")
            raise BlenderScriptError("", f"Temp directory setup failed: {e!s}")

    def _test_blender_functionality(self) -> None:
        """Test basic Blender functionality with a simple script."""
        try:
            test_script = """
import bpy
import sys

try:
    # Test basic Blender operations
    scene_count = len(bpy.data.scenes)
    object_count = len(bpy.context.scene.objects)

    print(f"BLENDER_TEST_SUCCESS: Scenes={scene_count}, Objects={object_count}")
    print(f"BLENDER_PYTHON_VERSION: {sys.version}")

except Exception as e:
    print(f"BLENDER_TEST_ERROR: {str(e)}")
    sys.exit(1)
"""

            # Create a temporary Python script file to avoid command line length issues
            test_script_path = os.path.join(self.temp_dir, "test_blender_functionality.py")
            with open(test_script_path, "w", encoding="utf-8") as f:
                f.write(test_script)

            blender_cmd = [
                str(self.blender_path),
            ]
            if self.headless:
                blender_cmd.extend(["--background", "--factory-startup"])
            else:
                blender_cmd.extend(["--factory-startup"])
            blender_cmd.extend(["--python", test_script_path, "--"])

            logger.debug(f"Running command: {' '.join(blender_cmd)}")
            result = subprocess.run(
                blender_cmd, capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL
            )

            if result.returncode != 0:
                logger.error(f"Blender functionality test failed: {result.stderr}")
                raise BlenderScriptError(test_script, f"Functionality test failed: {result.stderr}")

            # Check for success marker in output
            if "BLENDER_TEST_SUCCESS" not in result.stdout:
                logger.error("Blender test script did not complete successfully")
                raise BlenderScriptError(test_script, "Test script did not complete successfully")

            logger.debug("Blender functionality test passed")

        except subprocess.TimeoutExpired as e:
            logger.error(f"Blender functionality test timed out: {e!s}")
            raise BlenderScriptError(test_script, "Functionality test timed out")
        except Exception as e:
            logger.error(f"Blender functionality test error: {e!s}")
            raise BlenderScriptError(test_script, f"Functionality test error: {e!s}")

    async def execute_script(
        self,
        script: str,
        blend_file: str | None = None,
        timeout: int | None = None,
        retry_count: int = 0,
        script_name: str | None = None,
    ) -> str:
        """Execute Python script in Blender with comprehensive error handling."""
        self._initialize_executor()

        if timeout is None:
            timeout = self.process_timeout

        script_id = script_name or f"script_{int(time.time() * 1000)}"

        try:
            logger.info(f"Executing Blender script: {script_id} (timeout: {timeout}s)")

            # Validate script is not empty
            if not script or not script.strip():
                raise BlenderScriptError(script, "Empty or whitespace-only script provided")

            # Create temporary script file with error handling wrapper
            wrapped_script = self._wrap_script_with_error_handling(script, script_id)
            script_path = self._write_temp_script(wrapped_script, script_id)

            try:
                # Validate blend file if provided
                if blend_file and not os.path.exists(blend_file):
                    logger.warning(f"Blend file not found, using factory startup: {blend_file}")
                    blend_file = None

                # Build comprehensive command
                cmd = self._build_blender_command(script_path, blend_file)

                # Execute with process monitoring
                stdout, stderr = await self._execute_with_monitoring(cmd, timeout, script_id)

                # Process and validate output
                result = self._process_script_output(stdout, stderr, script_id)

                logger.info(f"Blender script completed successfully: {script_id}")
                return result

            finally:
                # Always clean up temp script file
                self._cleanup_temp_file(script_path)

        except BlenderScriptError:
            # Re-raise Blender-specific errors
            raise
        except TimeoutError:
            error_msg = f"Script execution timed out after {timeout}s"
            logger.error(f"{error_msg}: {script_id}")

            # Retry logic for timeout
            if retry_count < self.max_retries:
                logger.warning(f"🔄 Retrying script execution ({retry_count + 1}/{self.max_retries}): {script_id}")
                await asyncio.sleep(2)  # Brief delay before retry
                return await self.execute_script(script, blend_file, timeout, retry_count + 1, script_name)

            raise BlenderScriptError(script, error_msg)
        except Exception as e:
            error_msg = f"Unexpected error during script execution: {e!s}"
            logger.error(f"{error_msg}: {script_id}")
            raise BlenderScriptError(script, error_msg)

    def _wrap_script_with_error_handling(self, script: str, script_id: str) -> str:
        """Wrap user script with comprehensive error handling."""
        return f'''
import sys
import traceback
import bpy

SCRIPT_ID = "{script_id}"

print(f"BLENDER_SCRIPT_START: {{SCRIPT_ID}}")

try:
    # User script starts here
{self._indent_script(script, 4)}

    print(f"BLENDER_SCRIPT_SUCCESS: {{SCRIPT_ID}}")

except Exception as user_error:
    print(f"BLENDER_SCRIPT_ERROR: {{SCRIPT_ID}} - {{str(user_error)}}")
    print(f"BLENDER_SCRIPT_TRACEBACK: {{SCRIPT_ID}} - {{traceback.format_exc()}}")
    sys.exit(1)
'''

    def _indent_script(self, script: str, spaces: int) -> str:
        """Indent script lines for proper nesting."""
        indent = " " * spaces
        lines = script.split("\n")
        return "\n".join(f"{indent}{line}" if line.strip() else line for line in lines)

    def _write_temp_script(self, script: str, script_id: str) -> str:
        """Write script to temporary file with proper encoding."""
        try:
            script_path = os.path.join(self.temp_dir, f"{script_id}.py")

            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script)

            logger.debug(f"Written script to: {script_path}")
            return script_path

        except Exception as e:
            logger.error(f"Failed to write temp script: {e!s}")
            raise BlenderScriptError(script, f"Failed to write temp script: {e!s}")

    def _build_blender_command(self, script_path: str, blend_file: str | None) -> list[str]:
        """Build comprehensive Blender command with all necessary flags."""
        cmd = [
            self.blender_executable,
        ]

        # Add headless flag only if running in headless mode
        if self.headless:
            cmd.extend(
                [
                    "--background",  # No GUI
                    "--factory-startup",  # Clean startup
                    "--enable-autoexec",  # Allow script execution
                ]
            )
        else:
            # GUI mode - minimal flags for visual operation
            cmd.extend(
                [
                    "--factory-startup",  # Clean startup
                    "--enable-autoexec",  # Allow script execution
                ]
            )

        if blend_file and os.path.exists(blend_file):
            cmd.append(blend_file)

        cmd.extend(
            [
                "--python",
                script_path,
                "--",  # End of Blender args
            ]
        )

        mode = "headless" if self.headless else "GUI"
        logger.debug(f"🔧 Blender command ({mode}): {' '.join(cmd)}")
        return cmd

    async def _execute_with_monitoring(self, cmd: list[str], timeout: int, script_id: str) -> tuple[str, str]:
        """Execute command with process monitoring and resource tracking."""

        process = None
        try:
            # Create subprocess with proper settings
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.temp_dir,
                env=os.environ.copy(),
            )

            logger.debug(f"🚀 Started Blender process PID: {process.pid} for script: {script_id}")

            # Monitor process execution
            start_time = time.time()
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

                execution_time = time.time() - start_time
                logger.debug(f"Script execution completed in {execution_time:.2f}s: {script_id}")

            except TimeoutError:
                # Kill process on timeout
                logger.error(f"Script execution timed out, killing process {process.pid}: {script_id}")

                try:
                    if process.pid:
                        parent = psutil.Process(process.pid)
                        for child in parent.children(recursive=True):
                            logger.debug(f"Killing child process: {child.pid}")
                            child.kill()
                        parent.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    logger.warning(f"Could not kill process cleanly: {e!s}")

                process.kill()
                await process.wait()
                raise TimeoutError(f"Process timed out after {timeout}s")

            # Check process return code
            if process.returncode != 0:
                stderr_str = stderr.decode("utf-8", errors="replace")
                stdout_str = stdout.decode("utf-8", errors="replace")

                # Check if this is just a TBBmalloc warning (harmless on Windows)
                is_tbbmalloc_warning = "TBBmalloc" in stderr_str or "TBBmalloc" in stdout_str

                # Check if script output indicates success (e.g., "SUCCESS" in output)
                script_succeeded = "SUCCESS" in stdout_str or "Successfully" in stdout_str

                if is_tbbmalloc_warning and script_succeeded:
                    # TBBmalloc warning but script succeeded - log warning but don't raise error
                    logger.warning(
                        f"Blender exited with code {process.returncode} due to TBBmalloc warning, "
                        f"but script appears to have succeeded: {script_id}"
                    )
                elif is_tbbmalloc_warning:
                    # TBBmalloc warning - check if we can determine success from output
                    # For export operations, check if output file was mentioned in stdout
                    logger.warning(
                        f"Blender exited with code {process.returncode} due to TBBmalloc warning: {script_id}"
                    )
                    # Don't raise error for TBBmalloc - let caller check file existence
                    # But log the warning
                else:
                    # Real error - raise exception
                    logger.error(f"Blender process failed with return code {process.returncode}: {script_id}")
                    raise BlenderScriptError("", f"Blender process failed (code {process.returncode}): {stderr_str}")

            return stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")

        except Exception as e:
            if process and process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except Exception as cleanup_error:
                    logger.error(f"Error during process cleanup: {cleanup_error!s}")

            raise e

    def _process_script_output(self, stdout: str, stderr: str, script_id: str) -> str:
        """Process and validate script output with comprehensive error checking."""

        # Log all output for debugging
        if stdout.strip():
            logger.debug(f"📤 Script stdout: {script_id}")
            for line in stdout.split("\n"):
                if line.strip():
                    logger.debug(f"  {line}")

        if stderr.strip():
            logger.debug(f"📤 Script stderr: {script_id}")
            for line in stderr.split("\n"):
                if line.strip():
                    logger.debug(f"  {line}")

        # Check for script execution markers
        if f"BLENDER_SCRIPT_START: {script_id}" not in stdout:
            logger.error(f"Script did not start properly: {script_id}")
            raise BlenderScriptError("", f"Script did not start properly: {stderr}")

        # Check for errors
        error_lines = []
        traceback_lines = []

        for line in stdout.split("\n"):
            if f"BLENDER_SCRIPT_ERROR: {script_id}" in line:
                error_msg = line.split(" - ", 1)[1] if " - " in line else "Unknown error"
                error_lines.append(error_msg)
            elif f"BLENDER_SCRIPT_TRACEBACK: {script_id}" in line:
                traceback_msg = line.split(" - ", 1)[1] if " - " in line else ""
                traceback_lines.append(traceback_msg)

        if error_lines:
            full_error = f"Script errors: {'; '.join(error_lines)}"
            if traceback_lines:
                full_error += f"\nTraceback: {'; '.join(traceback_lines)}"
            logger.error(f"Script execution errors: {script_id} - {full_error}")
            raise BlenderScriptError("", full_error)

        # Check for success marker
        if f"BLENDER_SCRIPT_SUCCESS: {script_id}" not in stdout:
            logger.warning(f"Script completed without success marker: {script_id}")
            # Don't fail here, as script might have completed successfully without marker

        return stdout

    def _cleanup_temp_file(self, file_path: str) -> None:
        """Clean up temporary files with error handling."""
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.debug(f"Cleaned up temp file: {file_path}")
        except Exception as e:
            logger.warning(f"Could not clean up temp file {file_path}: {e!s}")

    def cleanup(self) -> None:
        """Clean up executor resources."""
        try:
            if self.temp_dir and os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
                logger.debug(f"Cleaned up temp directory: {self.temp_dir}")
        except Exception as e:
            logger.warning(f"Could not clean up temp directory: {e!s}")

    def __del__(self):
        """Destructor to ensure cleanup."""
        try:
            self.cleanup()
        except Exception:
            pass  # Ignore errors in destructor


# ---------------------------------------------------------------------------
# Strict-headless compatibility executor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrictCommand:
    """The only command representation accepted by the strict entry point."""

    executable: str
    argv: tuple[str, ...]

    def __iter__(self):
        return iter(self.argv)

    def __len__(self) -> int:
        return len(self.argv)

    def __getitem__(self, index):
        return self.argv[index]

    def __post_init__(self) -> None:
        if not self.executable:
            raise ValueError("strict command requires a Blender executable")
        prefix = (self.executable, "--background", "--factory-startup")
        if self.argv[:3] != prefix:
            raise ValueError("strict command must begin with executable and strict-headless flags")
        forbidden = {"--no-background", "--window-geometry", "--window-border", "--start-console"}
        if forbidden.intersection(self.argv):
            raise ValueError("strict command cannot contain GUI-mode flags")
        if self.argv[3:] == ("--version",):
            return
        try:
            python_index = self.argv.index("--python")
        except ValueError as exc:
            raise ValueError("strict operation command requires a provider script") from exc
        if python_index not in {3, 4}:
            raise ValueError("strict operation command has an invalid scene position")
        if python_index == 4 and not Path(self.argv[3]).is_absolute():
            raise ValueError("strict command scene path must be absolute")
        if python_index + 2 >= len(self.argv) or self.argv[python_index + 2] != "--":
            raise ValueError("strict operation command must delimit operation arguments")
        if not Path(self.argv[python_index + 1]).is_absolute():
            raise ValueError("strict command provider script must be absolute")


@dataclass(frozen=True)
class RawBlenderObservation:
    """Uninterpreted subprocess output; parsing belongs to the session boundary."""

    returncode: int
    stdout: str
    stderr: str


class StrictFailureKind(StrEnum):
    """Closed executor failure reasons, independent of display wording."""

    COMMAND_INVALID = "command_invalid"
    GUARD_UNAVAILABLE = "guard_unavailable"
    SPAWN_FAILED = "spawn_failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class StrictProcessGuard(Protocol):
    """Capability used to deny descendant creation for execute-code."""

    def available(self) -> bool:
        ...

    def wrap(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        ...


def plan_strict_command(
    executable: str,
    scene_path: Path | None,
    script_path: Path | None,
    operation_args: tuple[str, ...] = (),
    *,
    version_probe: bool = False,
) -> StrictCommand:
    """Build the canonical argv for every strict-headless Blender invocation."""
    if not executable:
        raise ValueError("a Blender executable is required")
    argv = [executable, "--background", "--factory-startup"]
    if version_probe:
        if scene_path is not None or script_path is not None or operation_args:
            raise ValueError("a version probe cannot include scene or operation inputs")
        argv.append("--version")
        return StrictCommand(executable=executable, argv=tuple(argv))
    if script_path is None or not script_path.is_absolute():
        raise ValueError("provider scripts must use absolute paths")
    if scene_path is not None:
        if not scene_path.is_absolute():
            raise ValueError("scene paths must be absolute")
        argv.append(str(scene_path))
    argv.extend(("--python", str(script_path), "--"))
    argv.extend(operation_args)
    return StrictCommand(executable=executable, argv=tuple(argv))


# Compatibility aliases; there is still exactly one implementation.
build_strict_argv = plan_strict_command
plan_headless_argv = plan_strict_command


def _sandbox_literal(path: str) -> str:
    """Quote a literal path for the sandbox profile language."""
    return path.replace("\\\\", "\\\\\\\\").replace('"', '\\\\"')


def build_macos_process_profile(blender_executable: str) -> str:
    """Return the macOS fork/exec profile used by execute-code."""
    literal = _sandbox_literal(blender_executable)
    # The initial target launch needs this literal exception.  Author source
    # is separately admitted against os.execv and friends; all other child
    # process paths remain denied by the profile.
    return (
        '(version 1) (allow default) (deny process-fork) (deny process-exec) '
        f'(allow process-exec (literal "{literal}"))'
    )


class MacOSDescendantProcessGuard:
    """sandbox-exec capability for the strict execute-code operation."""

    def __init__(self, blender_executable: str) -> None:
        self.blender_executable = blender_executable

    def available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None

    def wrap(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        sandbox_exec = shutil.which("sandbox-exec")
        if not self.available() or sandbox_exec is None:
            raise StrictExecutionError(
                StrictFailureKind.GUARD_UNAVAILABLE, "descendant process guard is unavailable"
            )
        profile = build_macos_process_profile(self.blender_executable)
        return (sandbox_exec, "-p", profile, *argv)


class StrictExecutionError(RuntimeError):
    """Typed subprocess lifecycle failure before output can be parsed."""

    def __init__(self, kind: StrictFailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class StrictBlenderExecutor:
    """Own fresh strict-headless Blender children from spawn through reap."""

    def __init__(
        self,
        blender_executable: str,
        *,
        process_guard: StrictProcessGuard | None = None,
        reap_grace_seconds: float = 2.0,
        audit_path: str | Path | None = None,
    ) -> None:
        self.blender_executable = blender_executable
        self.process_guard = process_guard or MacOSDescendantProcessGuard(blender_executable)
        self.reap_grace_seconds = reap_grace_seconds
        configured_audit = audit_path if audit_path is not None else os.environ.get("BLENDER_MCP_PROCESS_AUDIT_PATH")
        self.audit_path = Path(configured_audit) if configured_audit else None
        self.active_process: asyncio.subprocess.Process | None = None

    async def _probe_executable(self, timeout: float) -> bool:
        command = plan_strict_command(self.blender_executable, None, None, version_probe=True)
        try:
            observation = await self.execute(command, timeout=timeout)
        except (StrictExecutionError, OSError, ValueError):
            return False
        lines = observation.stdout.splitlines()
        return observation.returncode == 0 and bool(lines) and lines[0].startswith("Blender ")

    def validate_executable(self, *, timeout: float = 20.0) -> bool:
        """Validate Blender through this executor, including ownership and audit."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._probe_executable(timeout))

        result: list[bool] = []
        failure: list[BaseException] = []

        def run_probe() -> None:
            try:
                result.append(asyncio.run(self._probe_executable(timeout)))
            except BaseException as exc:  # pragma: no cover - defensive thread handoff
                failure.append(exc)

        thread = threading.Thread(target=run_probe, name="blender-executable-probe")
        thread.start()
        thread.join()
        if failure:
            raise failure[0]
        return result == [True]

    async def execute(
        self,
        command: StrictCommand,
        *,
        timeout: float,
        execute_code: bool = False,
        cwd: Path | None = None,
    ) -> RawBlenderObservation:
        """Run one planned command without a shell and always reap its child."""
        if command.executable != self.blender_executable:
            raise StrictExecutionError(
                StrictFailureKind.COMMAND_INVALID,
                "command executable does not match configured executable",
            )
        guard_available = self.process_guard.available()
        if execute_code and not guard_available:
            self._write_process_audit(command, command.argv, {}, {}, None, None)
            raise StrictExecutionError(
                StrictFailureKind.GUARD_UNAVAILABLE, "descendant process guard is unavailable"
            )
        if guard_available:
            try:
                # On the accepted macOS host, apply the no-descendant policy
                # to every provider launch. Execute-code additionally fails
                # closed when this capability is unavailable.
                launch_argv = self.process_guard.wrap(command.argv)
            except StrictExecutionError:
                self._write_process_audit(command, command.argv, {}, {}, None, None)
                raise
        else:
            launch_argv = command.argv

        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *launch_argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd is not None else None,
                env=os.environ.copy(),
            )
        )
        try:
            process = await asyncio.shield(spawn_task)
        except asyncio.CancelledError as exc:
            # Shield process creation so cancellation cannot lose ownership of
            # a child created between the syscall and handle publication.
            try:
                process = await spawn_task
            except (FileNotFoundError, PermissionError):
                self._write_process_audit(command, launch_argv, {}, {}, None, None)
            else:
                self.active_process = process
                observed: dict[int, list[str]] = {}
                self._capture_process(process.pid, observed)
                try:
                    await asyncio.shield(
                        self._terminate_and_reap(process, observed=observed)
                    )
                finally:
                    self._write_process_audit(
                        command, launch_argv, {}, observed, process.pid, None
                    )
                    self.active_process = None
            raise StrictExecutionError(
                StrictFailureKind.CANCELLED, "Blender execution was cancelled during spawn"
            ) from exc
        except (FileNotFoundError, PermissionError) as exc:
            self._write_process_audit(command, launch_argv, {}, {}, None, None)
            raise StrictExecutionError(
                StrictFailureKind.SPAWN_FAILED, f"could not start Blender: {exc}"
            ) from exc

        self.active_process = process
        captured: dict[int, psutil.Process] = {}
        observed: dict[int, list[str]] = {}
        direct_pid = process.pid
        self._capture_process(direct_pid, observed)
        tracker = asyncio.create_task(self._track_descendants(process, captured, observed))
        observation: RawBlenderObservation | None = None
        try:
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError as exc:
                await self._terminate_and_reap(process, captured=captured, observed=observed)
                raise StrictExecutionError(
                    StrictFailureKind.TIMED_OUT, f"Blender timed out after {timeout:g}s"
                ) from exc
            except asyncio.CancelledError as exc:
                await asyncio.shield(self._terminate_and_reap(process, captured=captured, observed=observed))
                raise StrictExecutionError(
                    StrictFailureKind.CANCELLED, "Blender execution was cancelled"
                ) from exc
            # The parent may already be gone here.  Always use the captured
            # handles rather than relying on a post-exit parent lookup.
            await self._reap_descendants(process, captured=captured, observed=observed)
            await self._reap_captured(captured, force=True)
            observation = RawBlenderObservation(
                returncode=int(process.returncode or 0),
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            )
            return observation
        finally:
            tracker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tracker
            # Capture once more while the direct handle is still available,
            # then force-reap processes discovered before parent exit.
            await self._reap_descendants(process, captured=captured, observed=observed, force=True)
            await self._reap_captured(captured, force=True)
            self._write_process_audit(command, launch_argv, captured, observed, direct_pid, observation)
            self.active_process = None

    async def _track_descendants(
        self,
        process: asyncio.subprocess.Process,
        captured: dict[int, psutil.Process],
        observed: dict[int, list[str]],
    ) -> None:
        """Snapshot the direct process and descendants until parent exit."""
        pid = getattr(process, "pid", None)
        while process.returncode is None:
            if isinstance(pid, int):
                self._capture_process(pid, observed)
            self._capture_descendants(process, captured, observed)
            if isinstance(pid, int):
                try:
                    parent_status = psutil.Process(pid).status()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # communicate() can otherwise wait forever on a pipe held
                    # by an orphaned descendant.
                    await self._reap_captured(captured, force=True)
                    return
                if parent_status == psutil.STATUS_ZOMBIE:
                    await self._reap_captured(captured, force=True)
                    return
            await asyncio.sleep(0.02)
        self._capture_descendants(process, captured, observed)
        await self._reap_captured(captured, force=True)

    @staticmethod
    def _capture_process(pid: int, observed: dict[int, list[str]]) -> None:
        try:
            observed[pid] = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            observed.setdefault(pid, [])

    def _capture_descendants(
        self,
        process: asyncio.subprocess.Process,
        captured: dict[int, psutil.Process],
        observed: dict[int, list[str]],
    ) -> None:
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int):
            return
        try:
            descendants = psutil.Process(pid).children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        for child in descendants:
            captured.setdefault(child.pid, child)
            self._capture_process(child.pid, observed)

    async def _terminate_and_reap(
        self,
        process: asyncio.subprocess.Process,
        *,
        captured: dict[int, psutil.Process] | None = None,
        observed: dict[int, tuple[int, ...] | list[str]] | None = None,
    ) -> None:
        """Terminate descendants and the direct child, then wait for both."""
        captured = captured if captured is not None else {}
        await self._reap_descendants(process, captured=captured, observed=observed, force=False)
        if process.returncode is not None:
            await self._reap_captured(captured, force=True)
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=self.reap_grace_seconds)
        except (TimeoutError, ProcessLookupError):
            pass
        if process.returncode is None:
            await self._reap_descendants(process, captured=captured, observed=observed, force=True)
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=self.reap_grace_seconds)
            except (TimeoutError, ProcessLookupError):
                logger.warning("Blender child did not acknowledge the kill request")
        await self._reap_captured(captured, force=True)

    async def _reap_descendants(
        self,
        process: asyncio.subprocess.Process,
        *,
        force: bool = False,
        captured: dict[int, psutil.Process] | None = None,
        observed: dict[int, tuple[int, ...] | list[str]] | None = None,
    ) -> None:
        """Snapshot and clean children, including those captured pre-exit."""
        captured = captured if captured is not None else {}
        self._capture_descendants(process, captured, observed if observed is not None else {})
        await self._reap_captured(captured, force=force)

    async def _reap_captured(self, captured: dict[int, psutil.Process], *, force: bool) -> None:
        for child in tuple(captured.values()):
            try:
                (child.kill if force else child.terminate)()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for child in tuple(captured.values()):
            try:
                await asyncio.to_thread(child.wait, timeout=self.reap_grace_seconds)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                if not force:
                    continue
                try:
                    child.kill()
                    await asyncio.to_thread(child.wait, timeout=self.reap_grace_seconds)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                    logger.warning("descendant process %s did not reap", child.pid)

    def _write_process_audit(
        self,
        command: StrictCommand,
        launch_argv: tuple[str, ...],
        captured: dict[int, psutil.Process],
        observed: dict[int, list[str]],
        direct_pid: int | None,
        observation: RawBlenderObservation | None,
    ) -> None:
        if self.audit_path is None:
            return
        record = {
            "planned_argv": list(command.argv),
            "launch_argv": list(launch_argv),
            "direct_process": (
                {"pid": direct_pid, "cmdline": list(observed.get(direct_pid, []))}
                if direct_pid is not None
                else None
            ),
            "provider_processes": [
                {"pid": pid, "cmdline": list(cmdline)} for pid, cmdline in sorted(observed.items())
            ],
            "provider_descendants": [
                {"pid": pid, "cmdline": list(observed.get(pid, []))} for pid in sorted(captured)
            ],
            "direct_process_gone": direct_pid is None or not psutil.pid_exists(direct_pid),
            "descendants_gone": all(not psutil.pid_exists(pid) for pid in captured),
            "returncode": observation.returncode if observation is not None else None,
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError:
            logger.warning("could not write strict process audit: %s", self.audit_path)


HeadlessBlenderExecutor = StrictBlenderExecutor
StrictHeadlessExecutor = StrictBlenderExecutor
StrictHeadlessCommand = StrictCommand
