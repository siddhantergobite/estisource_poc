# Open CAD Engine

Open CAD Engine is a browser-based CAD inspection and editing proof of concept.
It provides one workspace for:

- native Autodesk Inventor parts (`.ipt`) and assemblies (`.iam`);
- neutral STEP files (`.step`, `.stp`);
- neutral IGES files (`.iges`, `.igs`); and
- interactive 3D preview, orbit, zoom, pan, hover highlighting, parameter editing, reset, and export.

The Autodesk Inventor workflow is the primary demo workflow. Inventor remains
the source of truth for native feature history. Open CASCADE is used to create
the browser preview and to validate the rebuilt result.

## 1. Executive summary for the demo

For an Inventor file, the application performs this sequence:

```text
User selects .ipt or .iam
        |
        v
FastAPI receives the file and stores an immutable original
        |
        v
Inventor COM API opens a session working copy
        |
        v
Inventor parameters and feature relationships are extracted
        |
        v
Inventor rebuilds the working copy and exports a temporary STEP preview
        |
        v
Open CASCADE reads the STEP, validates it, measures it, and creates a mesh
        |
        v
React + Three.js displays the model and generated parameter controls
        |
        v
Hovering an input or model face highlights the matching preview faces
        |
        v
User edits a value -> Inventor rebuilds -> STEP preview refreshes -> UI updates
```

The uploaded production file is never overwritten during a normal editing
session. Native editing uses a session-scoped working copy.

## 2. Autodesk Inventor workflow

### 2.1 Requirements

Native Inventor automation requires:

- Windows;
- the full Autodesk Inventor desktop application installed and activated;
- the same Windows user running Inventor and the FastAPI backend;
- all required assembly references available when opening an `.iam`; and
- COM access to `Inventor.Application`.

STEP and IGES processing does not require Autodesk Inventor.

### 2.2 Upload and extraction

When an `.ipt` or `.iam` file is uploaded:

1. The frontend sends the file as multipart form data to `POST /api/documents`.
2. The backend writes the original bytes to a session-specific temporary path.
3. `InventorAdapter` connects to Autodesk Inventor through `pywin32`.
4. The file is copied to a working path and opened by Inventor.
5. The adapter reads `ComponentDefinition.Parameters` and the native
   `ModelParameters` collection when available.
6. Each parameter is converted into application metadata:
   - native name, such as `d0`;
   - Inventor expression, such as `10 mm`;
   - displayed numeric value;
   - unit, such as `mm`, `in`, `deg`, or `ul`;
   - comment;
   - editable/read-only status; and
   - feature relationship and label.
7. Parameters whose expressions reference other parameters are treated as
   formula-driven and read-only. This prevents the UI from overwriting native
   dependency formulas.

### 2.3 Feature-based names

The adapter inspects Inventor feature collections, including:

- extrusions;
- revolutions;
- holes;
- fillets;
- chamfers;
- shells;
- lofts;
- sweeps;
- coils;
- ribs;
- embosses;
- rectangular and circular patterns;
- mirrors;
- move features; and
- face features.

Feature parameter paths are read where Inventor exposes them. For example:

```text
Extrusion1 + Extent.Distance       -> Extrusion1 · Distance
Extrusion1 + TaperAngle            -> Extrusion1 · Taper Angle
Hole6 + HoleDiameter               -> Hole6 · Hole Diameter
Revolution1 + AngleExtent.Angle    -> Revolution1 · Angle
```

If Inventor exposes a parameter but the adapter cannot safely associate it with
a feature, the application keeps the native name, for example `d0`, and marks
the relationship as `parameter_only`. This is intentional: an incorrect CAD
feature name is worse than a conservative native name.

### 2.4 Rebuilding an edited Inventor model

When the user clicks **Update 3D model**:

1. The frontend submits the editable parameter values.
2. The backend starts from the last known complete native parameter state.
3. Changed numeric values are converted back into Inventor expressions with
   their original units.
4. Inventor receives the expressions through the COM API.
5. Inventor executes `Update2(True)` and falls back to `Update()` when needed.
6. The Inventor STEP translator exports a new temporary STEP file.
7. Open CASCADE imports the exported STEP file.
8. The backend checks that the result contains faces, triangles, and vertices.
9. The preview mesh, measurements, parameter values, and mappings are replaced
   only after the candidate result passes validation.
10. The original uploaded `.ipt` or `.iam` remains unchanged.

The backend uses `InventorWorker`, a dedicated COM worker thread. This is
necessary because Inventor COM objects are apartment-bound and should not be
used concurrently from arbitrary HTTP threads.

### 2.5 No-op protection

Inventor can accept an expression that is valid but not connected to the final
visible solid. In that situation, the parameter table changes but the preview
does not.

The backend fingerprints the previous and candidate preview meshes using:

- topology counts;
- model bounds;
- triangle count;
- mesh vertices; and
- mesh indices.

If a user changes a parameter and the exported preview is identical, the
backend rejects the update, preserves the previous preview, and reports that
the parameter is not connected to the visible geometry. This prevents the UI
from falsely reporting a successful model change.

## 3. Inventor preview, highlighting, and labels

Autodesk Inventor and the browser preview do not share face IDs. The
application therefore creates a conservative correspondence between them.

### 3.1 Backend mapping

For each Inventor feature, the adapter collects:

- feature name and type;
- feature output-face count;
- feature bounding box;
- model bounding box;
- face bounding boxes;
- face surface types; and
- face areas when Inventor exposes them.

After Inventor exports STEP, Open CASCADE assigns its own face IDs to the
preview shape. The mapping code compares normalized feature/face bounding-box
signatures and accepts only matches within a conservative tolerance.

The mapping statuses are:

- `preview_mapped`: a safe preview face match was found;
- `feature`: a native feature relationship exists but no preview face match is
  currently safe; or
- `parameter_only`: Inventor exposed a parameter but no feature relationship
  was detected.

Mapping status does not determine whether a parameter can rebuild the model.
A parameter can correctly change geometry while still lacking a safe highlight
mapping.

### 3.2 Frontend highlighting

The frontend uses Three.js to:

1. render the OCCT triangle mesh;
2. retain `triangle_face_ids` for every triangle;
3. raycast the model under the mouse pointer;
4. identify the intersected preview face;
5. find parameters mapped to that face;
6. render a separate yellow highlight mesh; and
7. render the related feature names in the green label near the pointer.

Hovering an input field uses the same mapping in the opposite direction:

```text
Input parameter
    -> preview_face_ids
    -> yellow Three.js overlay
    -> green feature-name label
```

When multiple native features share the same preview region, multiple names can
appear in the label. This reflects the native feature relationships returned by
Inventor; it is not a guessed CAD history generated by Three.js.

## 4. STEP workflow

STEP is a neutral solid exchange format. It normally preserves geometry and
topology, but it does not reliably preserve the original Inventor feature
history or editable feature parameters.

The STEP workflow is:

1. Upload `.step` or `.stp`.
2. Open CASCADE imports the STEP B-Rep.
3. The backend extracts solids, faces, edges, vertices, bounds, units, and
   measurements.
4. Open CASCADE triangulates the B-Rep for browser display.
5. Each triangle stores its source preview face ID.
6. The frontend renders the interactive model.
7. Simple models can use generic editable dimensions.
8. Complex models use whole-model dimension/rotation editing unless a
   model-specific schema exists.
9. The result can be exported as STEP or converted to IGES.

For a neutral STEP file, arbitrary feature-history controls cannot be inferred
reliably from B-Rep geometry. A production-safe named feature editor requires
a designer-defined schema or a native CAD backend.

## 5. IGES workflow

IGES is primarily a neutral surface/legacy exchange format. The application
uses the same Open CASCADE import, analysis, meshing, preview, and export path:

1. Upload `.iges` or `.igs`.
2. Open CASCADE imports the IGES entities and builds the available shape.
3. The backend analyzes bounds, topology, and mesh data.
4. Three.js displays the result.
5. Supported generic edits are applied through Open CASCADE.
6. The edited result can be exported as IGES or STEP.

IGES generally does not provide a native feature-history table like Inventor.
Therefore, IGES editing is geometry-level editing unless an explicit parameter
schema is supplied.

## 6. PMI and measurement extraction

For supported STEP AP242 data, the backend reads semantic PMI through the
Open CASCADE XCAF document tools. PMI values can include:

- nominal dimensions;
- tolerances;
- lower and upper limits; and
- dimension type metadata.

PMI is displayed as source information and is read-only by default. A PMI value
is not automatically a safe edit instruction. It becomes editable only when a
model-specific rebuild operation connects it to real geometry.

For all imported models, the backend also reports:

- solid count;
- face count;
- edge count;
- vertex count;
- overall length;
- overall breadth;
- overall height; and
- mesh triangle count.

## 7. Libraries and what each one does

### Backend Python libraries

Declared in `pyproject.toml`:

- `cadquery-ocp` - Python bindings and packaged Open CASCADE Technology (OCCT)
  kernel used for STEP/IGES import, B-Rep access, topology traversal, bounds,
  shape validation, meshing, mesh face IDs, PMI/XCAF access, transforms, and
  STEP/IGES export.
- `fastapi` - backend HTTP API and route definitions.
- `python-multipart` - multipart upload parsing used by FastAPI.
- `pywin32` - Windows COM integration through `pythoncom` and
  `win32com.client`; this is the bridge to Autodesk Inventor.
- `uvicorn[standard]` - ASGI server used to run FastAPI.

Development dependencies:

- `pytest` - automated backend tests.
- `httpx2` - HTTP test support listed in the development dependency group.

### OCCT modules used by this project

- `STEPControl` and `STEPCAFControl` - STEP import/export and document data.
- `IGESControl` - IGES import/export.
- `BRep`, `BRepAdaptor`, and `BRepBuilderAPI` - B-Rep operations and safe
  analysis copies.
- `BRepBndLib` - shape and face bounding boxes.
- `BRepCheck` - shape validity checks.
- `BRepMesh` - tessellation into triangles.
- `TopExp`, `TopoDS`, and `TopAbs` - solids, faces, edges, vertices, and
  topology traversal.
- `GeomAbs` - surface classification.
- `XCAFDoc`, `TDocStd`, and `TDF` - semantic STEP AP242 PMI access.
- `gp` - points, vectors, axes, and geometric transforms.

### Native Inventor technology

- Autodesk Inventor desktop application - native feature-history rebuild,
  parameter evaluation, native save, and STEP translation.
- Inventor COM API - parameter table, feature collections, feature faces,
  expressions, update operations, and native document export.
- `InventorAdapter` - isolated project adapter for Inventor COM.
- `InventorWorker` - dedicated thread that safely reuses one COM session.

### Frontend libraries

- `react` - component-based user interface.
- `react-dom` - browser rendering.
- `three` - WebGL scene, camera, lights, materials, buffer geometry, mesh
  rendering, raycasting, and highlight overlays.
- `three/examples/jsm/controls/OrbitControls.js` - orbit, zoom, and pan.
- `lucide-react` - interface icons.
- `vite` - frontend development server and production bundler.
- `@vitejs/plugin-react` - React support for Vite.

There is no commercial CAD viewer embedded in the frontend. Three.js renders
the preview mesh produced by the backend. Autodesk Inventor is used only by the
backend native workflow.

## 8. Main application functions

### Upload

- Drag-and-drop or file selection.
- Valid extensions: STEP, STP, IGES, IGS, IPT, and IAM.
- File size limit: 50 MB per upload.
- Session limit: up to 20 stored documents in the current process.

### Inspect

- Interactive 3D model preview.
- Orbit, zoom, and pan.
- Solids, faces, length, breadth, height, and triangle statistics.
- Detected geometric measurements.
- AP242 PMI display when available.
- Inventor feature/parameter list for native files.

### Edit

- Native Inventor parameters are rebuilt by Inventor itself.
- STEP and IGES use Open CASCADE generic or schema-driven operations.
- Read-only formula parameters are protected.
- Invalid or empty rebuilt previews are rejected.
- No-op native parameter changes are rejected and the previous preview is
  preserved.

### Highlight

- Input hover highlights mapped preview faces.
- Model hover identifies the preview face under the pointer.
- Model click/tap can hold the selected highlight.
- The related feature name appears in a green label near the pointer.

### Reset

Reset reopens the immutable original source and regenerates the preview. It does
not depend on the current edited Inventor session.

### Export

For an Inventor source:

- `.ipt` or `.iam` native export through Inventor `SaveAs`;
- STEP neutral export; and
- IGES neutral export.

For STEP and IGES sources, the application exports STEP or IGES. Native Inventor
feature history cannot be reconstructed from a neutral export alone.

## 9. HTTP API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/health` | Check backend status and supported formats |
| `POST` | `/api/documents` | Upload and analyze a CAD file |
| `POST` | `/api/documents/{id}/parameters` | Rebuild with edited parameters |
| `POST` | `/api/documents/{id}/reset` | Restore the original model |
| `GET` | `/api/documents/{id}/download` | Download STEP, IGES, IPT, or IAM |

Native Inventor parameter updates use a JSON object such as:

```json
{
  "values": {
    "d0": 1.25,
    "d1": 2.0,
    "d7": 15.0
  }
}
```

The numeric values use the units shown beside each input field.

## 10. Verified Inventor test results

The following folder was tested on Windows:

```text
D:\NIST-CAD-Models\NIST-FTC-CTC-PMI-CAD-models\Inventor 2021
```

The folder contains 11 `.ipt` files. The audit changed each editable parameter
on a working copy, rebuilt the model through Inventor, compared the resulting
native geometry and preview mesh where necessary, and restored the original
expression.

| Model | Extracted | Editable | Read-only | Geometry-changing edits | Preview |
|---|---:|---:|---:|---:|---|
| CTC-01 | 89 | 82 | 7 | 82/82 | Valid |
| CTC-02 | 276 | 273 | 3 | 273/273 | Valid |
| CTC-03 | 135 | 132 | 3 | 132/132 | Valid |
| CTC-04 | 135 | 124 | 11 | 124/124 | Valid |
| CTC-05 | 70 | 70 | 0 | 69/70 | Valid |
| FTC-06 | 124 | 123 | 1 | 123/123 | Valid |
| FTC-07 | 0 | 0 | 0 | Not applicable | Valid geometry, no native parameter table exposed |
| FTC-08 | 180 | 152 | 28 | 152/152 | Valid |
| FTC-09 | 142 | 129 | 13 | 129/129 | Valid |
| FTC-10 | 150 | 147 | 3 | 147/147 | Preview exists; OCCT validity warning |
| FTC-11 | 14 | 14 | 0 | 14/14 | Valid |

Totals:

- 1,315 parameters extracted.
- 1,246 parameters marked editable.
- 69 parameters marked read-only or formula-driven.
- 1,245 editable parameters changed the visible rebuilt geometry.
- `CTC-05 · d0` is the one no-op parameter. It is exposed by Inventor but is
  not connected to the final visible solid. The application now rejects that
  update instead of claiming that the preview changed.
- The current audit folder contains `.ipt` files only. An `.iam` test should
  include the assembly and all referenced part files.

Feature-to-preview mapping is intentionally conservative. In the same audit,
753 of the 1,315 parameters received a resolved preview-face mapping. The
remaining parameters can still rebuild correctly; they simply do not receive a
safe yellow face mapping yet.

## 11. Installation and local run

### Backend

From the repository root:

```powershell
uv sync
uv run uvicorn server.main:app --reload --port 8000
```

### Frontend

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open the application at:

```text
http://localhost:5173
```

Useful backend URLs:

```text
http://localhost:8000/api/health
http://localhost:8000/docs
```

## 12. Build and test

Run backend tests from the repository root:

```powershell
uv run pytest -q
```

Build the frontend:

```powershell
cd frontend
npm run build
```

The production build may show a large JavaScript chunk warning because Three.js
is included in the viewer bundle. It is a bundling optimization warning, not a
build failure.

## 13. Project structure

```text
cad_engine/
  occt_engine.py             STEP/IGES import, analysis, meshing, PMI, export
  inventor_adapter.py        Inventor COM adapter, extraction, rebuild worker

server/
  main.py                    FastAPI routes, sessions, validation, downloads

frontend/
  src/App.jsx                React UI, controls, Three.js viewer, highlighting
  src/styles.css             Layout, controls, labels, highlight styling
  package.json               Frontend dependencies and scripts

tests/
  test_api.py                API upload, edit, reset, PMI, and download tests
  test_occt_engine.py        Open CASCADE geometry tests
  test_inventor_mapping.py   Inventor binding and preview-face mapping tests

parametric_models/           Controlled schema-driven sample models
samples/                     STEP, IGES, AP242, and test fixtures
tools/                       Development and model-generation utilities
pyproject.toml               Python dependencies and project configuration
uv.lock                     Resolved Python dependency versions
```

## 14. Known limitations and production work

- Native Inventor automation requires a licensed desktop Inventor installation
  on the backend machine.
- An assembly may fail if referenced parts are missing or unresolved.
- Neutral STEP and IGES files do not reliably contain editable feature history.
- PMI is source information unless a safe rebuild schema is defined.
- Topology can change after a native rebuild, so feature-to-preview mapping uses
  conservative geometric matching rather than Inventor face IDs.
- Some native parameters can be valid but unused by the final visible solid;
  these are now detected as no-op updates and rejected.
- Extreme parameter values can violate native Inventor constraints. The user
  should use values inside the model's valid design range.
- Current sessions are in-memory and temporary. Production deployment should
  add persistent job storage, authentication, background CAD jobs, cleanup,
  logging, and access control.
- A production version could improve mapping with persistent feature identity,
  richer Inventor sketch relationships, and additional native CAD adapters.

## 15. Current status

The proof-of-concept demo workflow is complete for the tested native Inventor
parts: upload, parameter extraction, feature naming, rebuild, STEP preview,
3D orbit/zoom/pan, input-to-face highlighting, model-face hover naming, reset,
no-op protection, and export are implemented.

The tested native rebuild path is:

```text
Autodesk Inventor feature history
    -> Inventor COM parameter update
    -> Inventor rebuild
    -> Inventor STEP translator
    -> Open CASCADE validation and mesh
    -> Three.js preview and highlight
```
