# Strict-Headless Scene Session Specification

**Status:** implementation specification for `prototype/strict-headless-poly-compat`
**Baseline:** `o1lo01ol1o/blender-mcp` at `6b51a6b302a149354d5c7820d077827f1129bb5c`
**Scope:** the narrow MCP provider used by the one-shot Blender prototype

## Related specifications

- [Global cross-repository rollout](https://gitlab.outstandinglabs.ai/o1lo01ol1o/futures-research/-/blob/futures-research-blender/docs/plan-blender-headless-mcp-integration.md)
- [Poly-org typed camera-preview protocol](https://gitlab.outstandinglabs.ai/o1lo01ol1o/poly-org-agda2hs/-/blob/blender/headless-camera-preview/plans/blender-headless-camera-preview.md)
- [Cinema Blender capability server](https://gitlab.outstandinglabs.ai/o1lo01ol1o/cinema-formalization-agda2hs/-/blob/blender/plan-reconcile/plans/blender-capability-server.md)

The global document owns cross-repository sequencing. This document owns only
the provider process, scene-session, and wire contracts.

## 1. Required outcome

Provide a reliable stdio MCP server that lets the typed poly-org Blender author
inspect and mutate one scene across several calls without starting an
interactive Blender process.

Every Blender invocation MUST use both:

```text
--background --factory-startup
```

No invocation may use GUI mode. The prototype MUST NOT start or depend on
Xvfb, a viewport, `bpy.app.timers`, a socket bridge inside a persistent Blender
process, or any host-visible Blender window. Visual observation is a camera
render produced by background Blender.

The Blender executable is supplied through `BLENDER_EXECUTABLE`. The real
local acceptance target is the explicit application path and either the 4.2.3
baseline or the reviewed 5.2 LTS compatibility line:

```text
/Applications/Blender.app/Contents/MacOS/Blender
Blender 4.2.3 LTS or Blender 5.2.x LTS
```

Private revisions and `.blend` exports are saved uncompressed. This makes the
legacy 12-byte or Blender 5 17-byte versioned header directly inspectable;
arbitrary zstd data is not accepted as a scene merely because it is compressed.

Discovery of another installation may remain available to the rest of this
repository, but the prototype entry point fails clearly when its explicit
`BLENDER_EXECUTABLE` is absent or invalid.

## 2. Architecture

### 2.1 One MCP process, fresh Blender subprocesses

The MCP server is the session owner. Blender is not the session owner.

```text
poly-org client
  ↕ MCP over stdio
headless-session MCP server
  ↕ checked request/result values
file-backed SceneSession
  ↕ command plan
BlenderExecutor
  ↕ fresh subprocess per call
blender --background --factory-startup ...
```

The MCP server persists scene continuity in files under a private session
workspace. Each request starts a fresh Blender subprocess, loads the current
scene revision when one exists, performs exactly one checked operation, and
exits. No Blender process survives a completed request.

### 2.2 File-backed scene state

A `SceneSession` owns:

- an unguessable private workspace created for the MCP process;
- a current `.blend` revision, if one has been produced;
- a monotonically increasing revision number;
- a single-operation lock; and
- the one currently running child-process handle, if any.

The initial state has no scene file and revision zero. A mutation runs against
a temporary next-revision path. It advances the session only after Blender
exits successfully and the next `.blend` file exists and can be inspected.
Publication uses an atomic rename within the session workspace. A failed or
cancelled mutation leaves the previous revision current.

Read operations consume the current revision and do not advance it. A read
requiring a scene before the first successful mutation returns a typed
`SceneNotInitialized` error.

Concurrent scene mutations are not merged. The session serializes all five
prototype operations so each request observes one definite predecessor.

## 3. Public MCP surface

The prototype entry point registers exactly the following compatibility
operations. Existing unrelated Fleet tools remain outside this entry point.
Tool selection is therefore structural rather than a runtime allowlist over
the repository's broad tool catalog.

### 3.1 `blender_get_scene_info`

Request:

```json
{}
```

Checked result data:

```json
{
  "scene_name": "Scene",
  "scene_revision": 2,
  "frame_current": 1,
  "frame_start": 1,
  "frame_end": 250,
  "active_camera": "CAM_MAIN",
  "objects": [
    {
      "name": "Cube",
      "type": "MESH",
      "location": [0.0, 0.0, 0.0],
      "rotation_euler": [0.0, 0.0, 0.0],
      "scale": [1.0, 1.0, 1.0],
      "visible": true
    }
  ]
}
```

`scene_revision` is a non-negative session revision. Frame bounds are
positive integral frames with `frame_start <= frame_end`. Vector fields have
exactly three finite numbers. Object type is parsed into the provider's
closed Blender object-kind enum before the result is emitted. `active_camera`
is either a non-empty object name or `null`.

### 3.2 `blender_execute_code`

Request:

```json
{ "code": "import bpy\n..." }
```

The boundary parses `code` as a non-empty bounded `HeadlessBpyProgram`. In
addition to syntax parsing, this prototype boundary rejects process-launch,
dynamic-code-loading, display/window, and Xvfb access (`os`, `subprocess`,
`multiprocessing`, `ctypes`, `socket`, dynamic import/eval/exec/compile, and
window-manager creation APIs). The provider repeats this check even when the
poly client has already admitted the program. This is the narrow invariant
needed by the headless prototype, not a claim to provide a general Python
security sandbox.

The provider then constructs, rather than accepts from the caller, the wrapper that loads
the current scene when present, executes the program, saves the next scene
revision, and emits the result sentinel.

Checked result data:

```json
{
  "scene_revision": 3,
  "stdout": "...",
  "saved": true
}
```

A success always means that a new `.blend` revision was atomically published.
A Blender process returning zero without the expected saved file is
`SceneRevisionMissing`, not success.

### 3.3 `blender_camera_render_preview`

Request:

```json
{ "max_size": 512 }
```

`max_size` parses into a checked bounded preview size. The supported range is
64–2048 pixels, matching the poly-org `PreviewSize` boundary. The renderer
preserves aspect ratio and constrains the largest output dimension to
`max_size`.

The operation loads the current scene, requires an active camera, renders one
PNG in background mode, and returns exactly one MCP image content item with
base64 PNG bytes and MIME type `image/png`. Revision/camera metadata is read
through `blender_get_scene_info`; adding a text content item here would violate
the client’s single-image result type.

It does not capture a viewport and does not advance the scene revision.
Missing camera and invalid render output are distinct typed errors.

### 3.4 `blender_import_asset`

Request:

```json
{
  "path": "/absolute/staged/asset.glb",
  "format": "glb"
}
```

`path` parses into an existing regular staged-asset path. `format` parses into
a closed import format:

```text
glb | gltf | obj | fbx | usd | usdc | usdz
```

The selected enum determines one total import-plan branch. The operation
loads the current scene when present, imports the asset, saves a new scene
revision, and returns:

```json
{
  "scene_revision": 4,
  "imported_object_names": ["AssetRoot"],
  "format": "glb"
}
```

The returned object-name list is non-empty on success.

### 3.5 `blender_export_scene`

Request:

```json
{
  "path": "/absolute/approved/output/scene.blend",
  "format": "blend",
  "selection_only": false
}
```

`path` parses into an approved output path whose parent exists. `format`
parses into a closed export format:

```text
blend | glb | gltf | obj | fbx | usd | usdc
```

`selection_only` is admitted only for formats whose Blender exporter supports
it. The format-indexed checked request determines the export implementation;
there is no string dispatch in the executor.

Checked result data:

```json
{
  "scene_revision": 4,
  "path": "/absolute/approved/output/scene.blend",
  "format": "blend",
  "bytes": 12345
}
```

Export is a read of the current session revision and does not replace it.
Success requires a non-empty regular output file.

## 4. Boundary and error model

Raw MCP JSON is decoded once into operation-specific request models. Each
request model is parsed into an invariant-bearing internal value before a
command is planned. Core session and command code never inspects raw JSON or
branches on format strings.

The result path is the inverse:

```text
Blender sentinel/stdout/files
  → raw observation
  → parse to checked operation result
  → versioned MCP result encoder
```

The wire schema is versioned as `blender-headless-session/v1`. Print/parse
round trips are golden-tested for each request and result. Malformed JSON,
wrong field types, unsupported formats, invalid paths, invalid numeric bounds,
and malformed Blender output have distinct structured errors.

Required provider error cases are:

```text
InvalidRequest
BlenderExecutableMissing
SceneNotInitialized
UnsupportedImportFormat
UnsupportedExportFormat
InvalidAssetPath
InvalidOutputPath
BlenderExitedNonZero
BlenderTimedOut
BlenderCancelled
ResultSentinelMissing
ResultMalformed
SceneRevisionMissing
ActiveCameraMissing
PreviewMissing
ExportMissing
```

The MCP boundary renders those cases for clients. Core logic does not collapse
them to an unstructured exception string.

## 5. Blender command construction

One command-planning function is the source of truth for every subprocess.
It produces an argv value equivalent to:

```text
$BLENDER_EXECUTABLE
  --background
  --factory-startup
  [CURRENT_SCENE.blend]
  --python GENERATED_OPERATION_SCRIPT.py
  --
  OPERATION_ARGUMENTS...
```

All five operations call this planner. Direct `subprocess` calls from the
compatibility tools are forbidden. The planner's checked type does not expose
a GUI/headless boolean: prototype commands have only the strict-headless
form.

The generated operation script is provider-owned. Checked author code is data
embedded between provider-controlled load/result/save phases. For
`blender_execute_code`, the host launcher additionally denies descendant
process creation (on the accepted macOS host, by a tested process policy such
as a `sandbox-exec` profile); if that guard is unavailable, the execute-code
tool fails closed. Thus admitted code cannot start another Blender or Xvfb
process outside the canonical planner. The script emits one unique, versioned
sentinel record so incidental Blender logging cannot be mistaken for the
result.

## 6. Stdio initialization

The current baseline imports an app object and constructs HTTP state at module
load. The prototype stdio entry point instead has one explicit initialization
path:

1. configure logging to stderr;
2. construct or obtain the FastMCP app;
3. register only the five headless-session tools;
4. construct one `SceneSession` and attach its teardown bracket;
5. run FastMCP with stdio transport; and
6. close the session on EOF, cancellation, signal, or startup failure.

Stdout is reserved exclusively for MCP framing. Imports must not emit banners,
logs, or diagnostics to stdout. Importing the entry-point module for a test
must not start a server.

The entry point has a process-level smoke test that starts it, performs MCP
initialization and tool listing, and closes stdin without an unbound/lazily
initialized `app` error.

## 7. Process ownership and cancellation

`SceneSession` owns each Blender subprocess from spawn through wait. The
request uses a bracket/finally region:

- normal completion waits and parses the result;
- client cancellation sends terminate, waits a bounded grace period, then
  kills and waits if required;
- timeout follows the same reap path and returns `BlenderTimedOut`;
- MCP shutdown cancels the active request, reaps the child, and removes the
  private session workspace; and
- failed spawn cleans operation scripts and temporary revision files.

No detached child, shell-mediated background process, or global Blender
process is permitted. Tests inspect the process handle and workspace after
normal completion, failure, cancellation, and server shutdown.

This direct-child ownership and reaping is baseline resource correctness and
is mandatory in B1. “Lifecycle hardening” deferred by the global plan means
production policy—multi-tenant quotas, service restart recovery, tuned grace
periods, and remote supervision—not permission to leak a prototype child.

## 8. Files in scope

Expected implementation locations are:

```text
src/blender_mcp/server.py                         # reliable explicit stdio entry
src/blender_mcp/headless_session.py               # checked session state/lifecycle
src/blender_mcp/headless_session_tools.py         # five narrow MCP registrations
src/blender_mcp/utils/blender_executor.py         # one strict argv planner/executor
tests/unit/test_headless_session.py
tests/unit/test_headless_session_tools.py
tests/integration/test_headless_blender_session.py
tests/golden/blender_headless_session_v1/*
```

The exact split may follow existing package conventions, but there must be one
canonical command planner and one canonical session transition implementation.
The broad `script_execute` tool remains available to other application modes;
the prototype compatibility entry point does not register it in addition to
`blender_execute_code`.

## 9. Acceptance

### Unit and golden tests

- Importing and constructing the stdio app succeeds deterministically.
- Tool listing exposes exactly the five operations in this specification.
- Every planned argv starts with the configured executable and contains
  `--background` and `--factory-startup`.
- No prototype source or argv contains Xvfb, a viewport capture, or a GUI-mode
  switch.
- Empty code, forbidden process/display constructs, out-of-range preview size,
  unsupported formats, and invalid paths fail in the boundary parser before
  Blender is invoked.
- Execute-code fails closed when the descendant-process guard is unavailable;
  a negative test attempts a process launch and proves it cannot start.
- Request/result v1 goldens round-trip.
- A failed mutation preserves the preceding scene revision.
- Cancellation and timeout reap the child and do not publish a revision.

### Real supported-Blender session

Using the explicit macOS Blender executable:

1. Start the MCP server over stdio.
2. Execute code that creates `Cube_A`, `CAM_MAIN`, and a light; receive
   revision 1.
3. Execute a second request that loads revision 1, moves `Cube_A`, creates
   `Cube_B`, and publishes revision 2.
4. Read scene info and prove both objects and the new transform are present at
   revision 2.
5. Render a camera preview and parse a non-empty, non-black PNG.
6. Export revision 2 to `.blend` and reopen it in a separate background
   Blender process.
7. Shut down MCP and prove no Blender child or session workspace remains.

The test records the entire provider descendant process tree and fails if any
Blender process lacks `--background --factory-startup` or any unexpected child
process appears. During the test there must be no Blender UI process, Blender
window, viewport capture, or Xvfb process.

Run the repository suite as well:

```text
uv run pytest tests/ -q
```

## 10. Explicit deferrals

This prototype MR deliberately does not solve:

- adversarial Python sandboxing beyond the mandatory prototype admission check
  and descendant-process denial;
- a production-complete allowlist for `bpy` operations;
- retry policy for mutating scripts;
- timeout policy tuning beyond preserving existing bounded execution;
- multi-client or multi-scene sessions;
- crash recovery across MCP server restarts;
- remote execution, VM isolation, or container packaging;
- resource accounting; or
- compatibility with the historical 0xwagmi/Xvfb bridge.

These costs are not hidden: the current milestone proves strict-headless local
operation and a checked wire contract only. The cinema Proposed → Validated →
Applied gate remains responsible for admitting the resulting scene artifact.
