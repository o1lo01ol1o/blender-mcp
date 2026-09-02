"""Dedicated stdio entry point for the strict-headless SceneSession contract."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from blender_mcp.headless_session import SceneSession
from blender_mcp.tools.headless_session_tools import register_headless_session_tools

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _session_lifespan(session: SceneSession):
    try:
        yield {"scene_session": session}
    finally:
        await session.teardown()


def create_headless_server(
    blender_executable: str | None = None,
    *,
    session: SceneSession | None = None,
) -> FastMCP:
    """Construct an app with exactly the five strict-headless tools."""
    owns_session = session is None
    owned_session = session or SceneSession(blender_executable=blender_executable)
    try:
        app = FastMCP(
            name="blender-headless-session",
            version="1",
            instructions="Strict-headless Blender SceneSession compatibility server.",
            lifespan=lambda _server: _session_lifespan(owned_session),
            mask_error_details=False,
        )
        register_headless_session_tools(app, owned_session)
    except Exception:
        if owns_session:
            # Construction is synchronous, so this startup bracket has not
            # entered FastMCP's async lifespan yet.
            asyncio.run(owned_session.teardown())
        raise
    # This is intentionally an implementation attribute: tests and embedding
    # hosts can close an app-created session without changing the wire surface.
    app._headless_scene_session = owned_session
    return app


def main() -> None:
    """Run strict MCP over stdio; stdout remains reserved for MCP framing."""
    logging.basicConfig(
        level=os.environ.get("BLENDER_MCP_LOG_LEVEL", "INFO"),
        stream=sys.stderr,
        format="%(asctime)s | %(levelname)-8s | %(name)s - %(message)s",
    )
    session: SceneSession | None = None
    try:
        app = create_headless_server()
        session = app._headless_scene_session
        app.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        logger.info("Headless Blender MCP server stopped")
    except Exception as exc:
        # Startup errors are intentionally human-readable on stderr and never
        # leak into stdout, which is reserved for MCP framing.
        logger.error("Headless Blender MCP startup/run failed: %s", exc)
        raise
    finally:
        if session is not None:
            asyncio.run(session.teardown())


main_stdio = main


if __name__ == "__main__":
    main()
