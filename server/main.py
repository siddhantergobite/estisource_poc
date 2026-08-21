"""HTTP API for STEP/IGES and native Inventor upload, inspection, editing, and export."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import isfinite
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy

from cad_engine.occt_engine import (
    Cad3DAnalysis,
    SUPPORTED_FORMATS,
    analyze_shape,
    apply_freeform_parameters,
    detect_geometry_features,
    export_shape,
    extract_pmi,
    detect_model_units,
    load_shape,
    make_parametric_hammer_shape,
    resolve_preview_face_ids,
)
from cad_engine.inventor_adapter import InventorAdapterError, get_inventor_worker


MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_DOCUMENTS = 20
NATIVE_WORK_ROOT = Path(tempfile.gettempdir()) / "open-cad-engine-native"
PARAMETER_METADATA_PATH = Path(__file__).resolve().parents[1] / ".cad_parameter_metadata.json"

HAMMER_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "parametric_models" / "hammer" / "hammer_schema.json"
HAMMER_CONTRACT = json.loads(HAMMER_SCHEMA_PATH.read_text(encoding="utf-8"))
HAMMER_SCHEMA = HAMMER_CONTRACT["parameters"]
HAMMER_DEFAULTS = {parameter["key"]: float(parameter["default"]) for parameter in HAMMER_SCHEMA}
HAMMER_CONSTRAINTS = HAMMER_CONTRACT["constraints"]

NIST_FTC_06_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "parametric_models" / "nist_ftc_06" / "nist_ftc_06_schema.json"
NIST_FTC_06_CONTRACT = json.loads(NIST_FTC_06_SCHEMA_PATH.read_text(encoding="utf-8"))
NIST_FTC_06_SCHEMA = NIST_FTC_06_CONTRACT["parameters"]
NIST_FTC_06_CONSTRAINTS = NIST_FTC_06_CONTRACT["constraints"]


@dataclass
class StoredDocument:
    document_id: str
    filename: str
    original_bytes: bytes
    working_bytes: bytes
    source_format: str
    geometry_format: str
    parameter_state: dict[str, float]
    pmi_dimensions: list[dict]
    unit_info: dict[str, str | bool]
    profile: str | None = None
    profile_state: dict[str, float] | None = None
    native_source_path: Path | None = None
    native_parameters: list[dict] | None = None
    shape: object | None = None
    analysis: Cad3DAnalysis | None = None
    original_shape: object | None = None
    original_analysis: Cad3DAnalysis | None = None


class ParameterRequest(BaseModel):
    length: float | None = Field(default=None, gt=0)
    breadth: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    angle: float = 0.0
    values: dict[str, float] | None = None


DOCUMENTS: dict[str, StoredDocument] = {}


def _load_parameter_metadata() -> dict[str, dict[str, float]]:
    try:
        raw = json.loads(PARAMETER_METADATA_PATH.read_text(encoding="utf-8"))
        return {str(file_hash): {str(key): float(value) for key, value in values.items()} for file_hash, values in raw.items()}
    except (OSError, ValueError, TypeError):
        return {}


EXPORTED_PARAMETER_METADATA: dict[str, dict[str, float]] = _load_parameter_metadata()


def _remember_parameter_metadata(file_hash: str, state: dict[str, float]) -> None:
    EXPORTED_PARAMETER_METADATA[file_hash] = dict(state)
    try:
        PARAMETER_METADATA_PATH.write_text(json.dumps(EXPORTED_PARAMETER_METADATA, indent=2), encoding="utf-8")
    except OSError:
        # The in-memory registry still supports the current application session.
        pass

app = FastAPI(title="Open CAD Engine 3D API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _canonical_format(filename: str) -> str:
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension in {"ipt", "iam"}:
        return "inventor"
    if extension not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail="Supported 3D CAD files are .step, .stp, .iges, .igs, .ipt, and .iam.")
    return "step" if extension in {"step", "stp"} else "iges"


def _profile_for_filename(filename: str) -> str | None:
    stem = Path(filename).stem.lower().replace("-", "_").replace(" ", "_")
    if "parametrichammer" in stem or "parametric_hammer" in stem:
        return "parametric_hammer"
    if "nist_ftc_06_asme1_ap242_e2" in stem:
        return "nist_ftc_06_ap242"
    return None


_HAMMER_FILENAME_KEYS = {parameter["key"].lower(): parameter["key"] for parameter in HAMMER_SCHEMA}
_HAMMER_FILENAME_PARAMETER_RE = re.compile(
    r"(?:^|_)(L1|L2|L3|HandleDiameter|HeadWidth|HeadHeight|HeadThickness|ClawAngle)-(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _hammer_state_for_filename(filename: str) -> dict[str, float]:
    """Recover exported hammer parameters encoded in the neutral-file name.

    STEP and IGES preserve geometry, but not this application's model-specific
    parameter contract. Exported parametric filenames therefore carry a small,
    human-readable parameter manifest so a downloaded file can be re-uploaded
    without falling back to the schema defaults.
    """

    state = dict(HAMMER_DEFAULTS)
    parsed: dict[str, float] = {}
    for match in _HAMMER_FILENAME_PARAMETER_RE.finditer(Path(filename).stem):
        key = _HAMMER_FILENAME_KEYS[match.group(1).lower()]
        parsed[key] = float(match.group(2))
    if not parsed:
        return state
    state.update(parsed)
    if state["L1"] <= state["L2"]:
        return dict(HAMMER_DEFAULTS)
    state["L3"] = round(state["L1"] - state["L2"], 6)
    return state


def _filename_has_hammer_parameters(filename: str) -> bool:
    return _HAMMER_FILENAME_PARAMETER_RE.search(Path(filename).stem) is not None


def _hammer_export_name(filename: str, state: dict[str, float], extension: str) -> str:
    """Build a neutral export name that carries the editable parameter state."""

    stem = Path(filename).stem
    stem = re.sub(r"_(?:L1|L2|L3|HandleDiameter|HeadWidth|HeadHeight|HeadThickness|ClawAngle)-[-\d.]+", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"(?:_edited)+$", "", stem, flags=re.IGNORECASE)
    ordered_keys = ("L1", "L2", "L3", "HandleDiameter", "HeadWidth", "HeadHeight", "HeadThickness", "ClawAngle")
    tokens = []
    for key in ordered_keys:
        value = format(float(state[key]), ".6f").rstrip("0").rstrip(".")
        tokens.append(f"{key}-{value}")
    return f"{stem}_edited_{'_'.join(tokens)}.{extension}"


def _load_document_shape(document: StoredDocument):
    if document.shape is not None:
        return document.shape
    try:
        document.shape = load_shape(document.working_bytes, document.geometry_format)
        return document.shape
    except Exception as exc:  # noqa: BLE001 - return parser details to the client
        raise HTTPException(status_code=400, detail=f"Could not read the 3D CAD model: {exc}") from exc


def _initial_parameters(shape, analysis: Cad3DAnalysis | None = None) -> dict[str, float]:
    analysis = analysis or analyze_shape(shape, include_mesh=False)
    return {"length": round(analysis.length, 6), "breadth": round(analysis.breadth, 6), "height": round(analysis.height, 6), "angle": 0.0}


def _analyze_preview_shape(shape) -> Cad3DAnalysis:
    """Analyze a copy so the cached editable shape stays safe to transform.

    OCCT's mesher annotates the shape with triangulation data. Keeping that
    data off the working shape avoids native crashes in transforms/exports for
    some imported IGES models while still caching the expensive analysis.
    """

    return analyze_shape(BRepBuilderAPI_Copy(shape).Shape())


def _preview_geometry_fingerprint(analysis: Cad3DAnalysis | None) -> str | None:
    """Create a stable fingerprint for the currently displayed preview.

    Native Inventor can accept a parameter expression that is valid but is not
    connected to the visible solid (for example, an unused construction
    parameter). Comparing only the parameter table would incorrectly report
    such an edit as a successful model update. The preview mesh is already
    produced for the native workflow, so hashing its topology, bounds, and
    coordinates gives us a cheap, deterministic no-op check.
    """

    if analysis is None or not analysis.mesh.get("vertices"):
        return None
    payload = {
        "topology": [analysis.solid_count, analysis.face_count, analysis.edge_count, analysis.vertex_count],
        "bounds": [analysis.min_x, analysis.min_y, analysis.min_z, analysis.max_x, analysis.max_y, analysis.max_z],
        "triangles": analysis.mesh.get("triangle_count", 0),
        "vertices": analysis.mesh.get("vertices", []),
        "indices": analysis.mesh.get("indices", []),
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_simple_model(analysis) -> bool:
    """Permit generic scaling only for small, single-part geometry."""

    return analysis.face_count <= 20 and analysis.edge_count <= 80 and analysis.solid_count <= 1


def _resolve_native_preview_bindings(native_parameters: list[dict] | None, shape) -> None:
    """Attach only conservative feature-to-preview face matches."""

    resolved_features: dict[int, tuple[list[int], float | None]] = {}
    for parameter in native_parameters or []:
        preview_face_ids: set[int] = set()
        for binding in parameter.get("feature_bindings", []):
            feature_index = binding.get("feature_index")
            if isinstance(feature_index, int) and feature_index in resolved_features:
                face_ids, score = resolved_features[feature_index]
            else:
                face_ids, score = resolve_preview_face_ids(
                    shape,
                    binding.get("face_signatures", []),
                    binding.get("model_bounds"),
                    binding.get("feature_bounds"),
                )
                if isinstance(feature_index, int):
                    resolved_features[feature_index] = (face_ids, score)
            binding["preview_face_ids"] = face_ids
            if face_ids and score is not None:
                binding["preview_match_score"] = round(score, 6)
                preview_face_ids.update(face_ids)
        parameter["preview_face_ids"] = sorted(preview_face_ids)
        if preview_face_ids:
            parameter["mapping_status"] = "preview_mapped"
            parameter["mapping_description"] = "Feature relationship resolved to the highlighted OCCT preview faces."


def _analysis_payload(document: StoredDocument, shape=None, analysis: Cad3DAnalysis | None = None) -> dict:
    shape = shape or _load_document_shape(document)
    analysis = analysis or document.analysis or analyze_shape(shape)
    document.shape = shape
    document.analysis = analysis
    values = document.parameter_state
    is_simple = _is_simple_model(analysis)
    is_parametric = document.profile == "parametric_hammer"
    is_nist_ftc_06 = document.profile == "nist_ftc_06_ap242"
    is_native_inventor = document.source_format == "inventor"
    editing_available = True
    detected_features = detect_geometry_features(shape)
    unit_info = document.unit_info
    detected_features = [
        {**feature, "unit": unit_info["symbol"] if feature["unit"] == "model units" else feature["unit"]}
        for feature in detected_features
    ]
    pmi_dimensions = [
        {**dimension, "unit": unit_info["symbol"] if dimension["unit"] == "model units" else dimension["unit"]}
        for dimension in document.pmi_dimensions
    ]
    dimension_label = "Length" if is_simple else "Overall length"
    breadth_label = "Breadth" if is_simple else "Overall breadth"
    height_label = "Height" if is_simple else "Overall height"
    if is_native_inventor:
        parameters = [
            {
                "key": parameter["name"],
                "label": parameter.get("label") or f"Inventor {parameter['name']}",
                "value": parameter["display_value"],
                "unit": "deg" if parameter["units"] == "deg" else parameter["units"],
                "editable": parameter["editable"],
                "native_parameter": parameter["name"],
                "expression": parameter["expression"],
                "feature_bindings": [
                    {key: value for key, value in binding.items() if key not in {"face_signatures", "feature_bounds", "model_bounds"}}
                    for binding in parameter.get("feature_bindings", [])
                ],
                "mapping_status": parameter.get("mapping_status", "parameter_only"),
                "mapping_description": parameter.get("mapping_description", ""),
                "preview_face_ids": parameter.get("preview_face_ids", []),
            }
            for parameter in (document.native_parameters or [])
        ]
    elif is_parametric:
        profile_values = document.profile_state or HAMMER_DEFAULTS
        parameters = [
            {
                **parameter,
                "value": profile_values[parameter["key"]],
                "unit": "deg" if parameter["unit"] == "deg" else unit_info["symbol"],
            }
            for parameter in HAMMER_SCHEMA
        ]
    elif is_nist_ftc_06:
        parameters = [
            {
                **parameter,
                "value": values[parameter["key"]],
                "unit": "deg" if parameter["unit"] == "deg" else unit_info["symbol"],
            }
            for parameter in NIST_FTC_06_SCHEMA
        ]
    else:
        parameters = [
            {"key": "length", "label": dimension_label, "value": values["length"], "unit": unit_info["symbol"], "editable": editing_available},
            {"key": "breadth", "label": breadth_label, "value": values["breadth"], "unit": unit_info["symbol"], "editable": editing_available},
            {"key": "height", "label": height_label, "value": values["height"], "unit": unit_info["symbol"], "editable": editing_available},
            {"key": "angle", "label": "Z angle", "value": values["angle"], "unit": "deg", "editable": editing_available},
        ]
    return {
        "document_id": document.document_id,
        "filename": document.filename,
        "source_format": document.source_format,
        "native_format": Path(document.filename).suffix.lower().lstrip(".") if is_native_inventor else None,
        "engine": "inventor+occt" if is_native_inventor else "occt",
        "mode": "native_parametric" if is_native_inventor else "parametric" if is_parametric else "schema" if is_nist_ftc_06 else "freeform",
        "editing_available": editing_available,
        "complexity": "simple" if is_simple else "complex",
        "complexity_reason": "Native Inventor model parameters are linked to the Inventor feature history; OCCT displays the rebuilt STEP." if is_native_inventor else "Named hammer parameters are linked to a rebuild recipe." if is_parametric else "NIST FTC-06 schema maps overall dimensions to a named OCCT affine rebuild; AP242 PMI remains source-only until feature recipes are available." if is_nist_ftc_06 else "Simple geometry supports free-form editing." if is_simple else "Complex geometry supports whole-model scaling and rotation. Named feature parameters require a designer-defined schema.",
        "units": unit_info,
        "length": values["length"],
        "breadth": values["breadth"],
        "height": values["height"],
        "angle": values["angle"],
        "parameters": parameters,
        "parameter_schema": document.native_parameters if is_native_inventor else HAMMER_SCHEMA if is_parametric else NIST_FTC_06_SCHEMA if is_nist_ftc_06 else None,
        "native_runtime": "autodesk_inventor" if is_native_inventor else None,
        "profile": document.profile,
        "detected_features": detected_features,
        "pmi_dimensions": pmi_dimensions,
        "pmi_summary": {
            "count": len(document.pmi_dimensions),
            "source": "AP242 semantic PMI" if document.pmi_dimensions else None,
            "editable_count": sum(1 for dimension in pmi_dimensions if dimension["editable"]),
        },
        "bounds": {
            "min": [analysis.min_x, analysis.min_y, analysis.min_z],
            "max": [analysis.max_x, analysis.max_y, analysis.max_z],
            "length": analysis.length,
            "breadth": analysis.breadth,
            "height": analysis.height,
        },
        "solid_count": analysis.solid_count,
        "face_count": analysis.face_count,
        "edge_count": analysis.edge_count,
        "vertex_count": analysis.vertex_count,
        "valid": analysis.valid,
        "mesh": analysis.mesh,
        "constraints": {
            "valid": True,
            "message": "Native Inventor parameters rebuild the feature history." if is_native_inventor else "L3 = L1 - L2; L1 must be greater than L2." if is_parametric else "NIST FTC-06 schema: overall dimensions use the OCCT affine rebuild; AP242 PMI remains read-only." if is_nist_ftc_06 else "Free-form mode: dimensions and Z rotation may change independently.",
            "rules": HAMMER_CONSTRAINTS if is_parametric else NIST_FTC_06_CONSTRAINTS if is_nist_ftc_06 else [],
        },
    }


def _get_document(document_id: str) -> StoredDocument:
    document = DOCUMENTS.get(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="CAD document not found or session expired.")
    return document


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "engine": "occt+inventor", "formats": "STEP, IGES, Inventor IPT, Inventor IAM"}


@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...)) -> dict:
    filename = Path(file.filename or "model.step").name
    source_format = _canonical_format(filename)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded CAD file is empty.")
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="3D CAD files are limited to 50 MB in this PoC.")
    if len(DOCUMENTS) >= MAX_DOCUMENTS:
        DOCUMENTS.pop(next(iter(DOCUMENTS)))
    document_id = uuid4().hex
    native_source_path: Path | None = None
    native_parameters: list[dict] | None = None
    geometry_format = source_format
    working_bytes = data
    if source_format == "inventor":
        native_source_path = NATIVE_WORK_ROOT / f"{document_id}{Path(filename).suffix.lower()}"
        native_step_path = NATIVE_WORK_ROOT / f"{document_id}.step"
        NATIVE_WORK_ROOT.mkdir(parents=True, exist_ok=True)
        native_source_path.write_bytes(data)
        try:
            worker = get_inventor_worker()
            native_parameters = [parameter.to_dict() for parameter in worker.rebuild_to_step(native_source_path, native_step_path, {})]
            working_bytes = native_step_path.read_bytes()
            geometry_format = "step"
        except InventorAdapterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        shape = load_shape(working_bytes, geometry_format)
        analysis = _analyze_preview_shape(shape)
        if source_format == "inventor":
            _resolve_native_preview_bindings(native_parameters, shape)
    except Exception as exc:  # noqa: BLE001 - return parser details to the client
        raise HTTPException(status_code=400, detail=f"Could not read the {source_format.upper()} model: {exc}") from exc
    profile = _profile_for_filename(filename)
    profile_state = None
    if profile == "parametric_hammer":
        if _filename_has_hammer_parameters(filename):
            profile_state = _hammer_state_for_filename(filename)
        else:
            profile_state = EXPORTED_PARAMETER_METADATA.get(hashlib.sha256(data).hexdigest(), dict(HAMMER_DEFAULTS))
    document = StoredDocument(
        document_id,
        filename,
        data,
        working_bytes,
        source_format,
        geometry_format,
        _initial_parameters(shape, analysis),
        extract_pmi(data, source_format) if source_format != "inventor" else [],
        detect_model_units(data, source_format) if source_format != "inventor" else detect_model_units(working_bytes, geometry_format),
        profile,
        profile_state,
        native_source_path,
        native_parameters,
        shape,
        analysis,
        shape,
        analysis,
    )
    DOCUMENTS[document_id] = document
    return _analysis_payload(document, shape, analysis)


@app.post("/api/documents/{document_id}/parameters")
def apply_parameters(document_id: str, request: ParameterRequest) -> dict:
    document = _get_document(document_id)
    if document.source_format == "inventor":
        if document.native_source_path is None or not document.native_source_path.is_file():
            raise HTTPException(status_code=409, detail="The native Inventor working source is no longer available.")
        if request.values is None:
            raise HTTPException(status_code=400, detail="Native Inventor edits must use a values object.")
        native_catalog = {parameter["name"]: parameter for parameter in (document.native_parameters or [])}
        unknown = sorted(set(request.values) - set(native_catalog))
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown Inventor parameters: {', '.join(unknown)}")
        # Rebuild from the complete last-known editable state. This matters
        # after a failed Inventor update, because the worker may have had to
        # discard its session and reopen the immutable source.
        updates: dict[str, float] = {
            key: float(parameter["display_value"])
            for key, parameter in native_catalog.items()
            if parameter["editable"]
        }
        for key, value in request.values.items():
            if not native_catalog[key]["editable"]:
                raise HTTPException(status_code=400, detail=f"Inventor parameter {key} is formula-driven and read-only.")
            numeric_value = float(value)
            if not isfinite(numeric_value):
                raise HTTPException(status_code=400, detail=f"Parameter {key} must be a finite number.")
            updates[key] = numeric_value
        changed_parameter_names = [
            key
            for key, value in updates.items()
            if key in native_catalog and abs(float(value) - float(native_catalog[key]["display_value"])) > 1e-9
        ]
        native_step_path = NATIVE_WORK_ROOT / f"{document.document_id}_edited.step"
        try:
            worker = get_inventor_worker()
            updated_catalog = [parameter.to_dict() for parameter in worker.rebuild_to_step(document.native_source_path, native_step_path, updates)]
            candidate_bytes = native_step_path.read_bytes()
            candidate_shape = load_shape(candidate_bytes, "step")
            candidate_analysis = _analyze_preview_shape(candidate_shape)
            _resolve_native_preview_bindings(updated_catalog, candidate_shape)
            if (
                candidate_analysis.face_count <= 0
                or candidate_analysis.mesh["triangle_count"] <= 0
                or not candidate_analysis.mesh["vertices"]
            ):
                worker.discard_session()
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Inventor rebuilt the part, but the exported STEP contains no previewable geometry. "
                        "The previous preview was preserved. Try a smaller value or use Reset to production."
                    ),
                )
            previous_fingerprint = _preview_geometry_fingerprint(document.analysis)
            candidate_fingerprint = _preview_geometry_fingerprint(candidate_analysis)
            if changed_parameter_names and previous_fingerprint and previous_fingerprint == candidate_fingerprint:
                worker.discard_session()
                labels = ", ".join(
                    native_catalog[name].get("label") or name
                    for name in changed_parameter_names
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Inventor accepted the value, but the preview geometry did not change for {labels}. "
                        "That parameter is not connected to the visible solid in this model, so the previous preview was preserved."
                    ),
                )
            document.working_bytes = candidate_bytes
            document.shape = candidate_shape
            document.analysis = candidate_analysis
            document.native_parameters = updated_catalog
            shape = candidate_shape
            document.parameter_state = _initial_parameters(shape, candidate_analysis)
            return _analysis_payload(document, shape, candidate_analysis)
        except InventorAdapterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize native/OCCT candidate failures
            raise HTTPException(
                status_code=422,
                detail=(
                    "Inventor rebuilt the part, but OCCT could not create a valid preview. "
                    "The previous preview was preserved. Try a smaller value or use Reset to production."
                ),
            ) from exc

    if document.profile == "parametric_hammer":
        if request.values is None:
            raise HTTPException(status_code=400, detail="Parametric hammer edits must use a values object.")
        target = dict(document.profile_state or HAMMER_DEFAULTS)
        allowed = {parameter["key"] for parameter in HAMMER_SCHEMA if parameter["editable"]}
        unknown = sorted(set(request.values) - allowed - {"L3"})
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown hammer parameters: {', '.join(unknown)}")
        for key, value in request.values.items():
            if key in allowed:
                numeric_value = float(value)
                if not isfinite(numeric_value):
                    raise HTTPException(status_code=400, detail=f"Parameter {key} must be a finite number.")
                target[key] = round(numeric_value, 6)
        if "L3" in request.values and abs(float(request.values["L3"]) - (target["L1"] - target["L2"])) > 1e-6:
            raise HTTPException(status_code=400, detail="Constraint failed: L3 must equal L1 - L2.")
        if target["L1"] <= target["L2"]:
            raise HTTPException(status_code=400, detail="Constraint failed: L1 must be greater than L2.")
        target["L3"] = round(target["L1"] - target["L2"], 6)
        try:
            updated = make_parametric_hammer_shape(target)
            working_bytes = export_shape(updated, document.source_format)
        except Exception as exc:  # noqa: BLE001 - CAD kernel errors are client-edit errors
            raise HTTPException(status_code=400, detail=f"Could not rebuild the parametric hammer: {exc}") from exc
        updated_analysis = _analyze_preview_shape(updated)
        document.working_bytes = working_bytes
        document.shape = updated
        document.analysis = updated_analysis
        document.profile_state = target
        document.parameter_state = _initial_parameters(updated, updated_analysis)
        return _analysis_payload(document, updated, updated_analysis)

    shape = _load_document_shape(document)
    if request.length is None or request.breadth is None or request.height is None:
        raise HTTPException(status_code=400, detail="Length, breadth, and height are required for free-form editing.")
    target = {
        "length": round(float(request.length), 6),
        "breadth": round(float(request.breadth), 6),
        "height": round(float(request.height), 6),
        "angle": round(float(request.angle), 6),
    }
    try:
        updated = apply_freeform_parameters(shape, document.parameter_state, target, document.analysis)
        working_bytes = export_shape(updated, document.source_format)
    except Exception as exc:  # noqa: BLE001 - CAD kernel errors are client-edit errors
        raise HTTPException(status_code=400, detail=f"Could not update the CAD model: {exc}") from exc
    updated_analysis = _analyze_preview_shape(updated)
    document.working_bytes = working_bytes
    document.shape = updated
    document.analysis = updated_analysis
    document.parameter_state = target
    return _analysis_payload(document, updated, updated_analysis)


@app.post("/api/documents/{document_id}/reset")
def reset_document(document_id: str) -> dict:
    document = _get_document(document_id)
    if document.source_format == "inventor":
        if document.native_source_path is None:
            raise HTTPException(status_code=409, detail="The native Inventor working source is no longer available.")
        native_step_path = NATIVE_WORK_ROOT / f"{document.document_id}_reset.step"
        try:
            worker = get_inventor_worker()
            # Reopen the immutable source so Reset does not reuse the already
            # edited Inventor document held by the persistent COM worker.
            worker.discard_session()
            document.native_parameters = [parameter.to_dict() for parameter in worker.rebuild_to_step(document.native_source_path, native_step_path, {})]
            document.working_bytes = native_step_path.read_bytes()
            shape = load_shape(document.working_bytes, "step")
            analysis = _analyze_preview_shape(shape)
            _resolve_native_preview_bindings(document.native_parameters, shape)
            document.shape = shape
            document.analysis = analysis
            document.parameter_state = _initial_parameters(shape, analysis)
            return _analysis_payload(document, shape, analysis)
        except InventorAdapterError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    document.working_bytes = document.original_bytes
    shape = document.original_shape or load_shape(document.working_bytes, document.geometry_format)
    analysis = document.original_analysis or _analyze_preview_shape(shape)
    document.shape = shape
    document.analysis = analysis
    document.parameter_state = _initial_parameters(shape, analysis)
    if document.profile == "parametric_hammer":
        document.profile_state = dict(HAMMER_DEFAULTS)
    return _analysis_payload(document, shape, analysis)


@app.get("/api/documents/{document_id}/download")
def download_document(document_id: str, format: str | None = Query(default=None)) -> Response:
    document = _get_document(document_id)
    native_format = Path(document.filename).suffix.lower().lstrip(".") if document.source_format == "inventor" else None
    requested_format = ("step" if document.source_format == "inventor" else document.source_format) if format is None else format.lower().lstrip(".")
    if requested_format in {"stp", "step"}:
        requested_format = "step"
    elif requested_format in {"igs", "iges"}:
        requested_format = "iges"
    elif requested_format in {"ipt", "iam", "inventor"}:
        requested_format = native_format if requested_format == "inventor" else requested_format
    else:
        raise HTTPException(status_code=400, detail="Export format must be STEP, IGES, or IPT.")
    try:
        if requested_format in {"ipt", "iam"}:
            if document.source_format != "inventor" or document.native_source_path is None:
                raise ValueError("Native Inventor export is available only for native Inventor models.")
            if requested_format != native_format:
                raise ValueError(f"This model is a native .{native_format} file; it cannot be exported as .{requested_format}.")
            native_path = NATIVE_WORK_ROOT / f"{document.document_id}_edited.{native_format}"
            output_bytes = get_inventor_worker().export_to_native(document.native_source_path, native_path)
        else:
            output_bytes = document.working_bytes if requested_format == document.geometry_format else export_shape(_load_document_shape(document), requested_format)
    except Exception as exc:  # noqa: BLE001 - return export details to the client
        raise HTTPException(status_code=400, detail=f"Could not export the model as {requested_format.upper()}: {exc}") from exc
    media_type = {"step": "application/step", "iges": "application/iges", "ipt": "application/x-inventor", "iam": "application/x-inventor"}[requested_format]
    extension = requested_format
    if document.profile == "parametric_hammer":
        _remember_parameter_metadata(hashlib.sha256(output_bytes).hexdigest(), document.profile_state or HAMMER_DEFAULTS)
    export_stem = re.sub(r"(?:_edited)+$", "", Path(document.filename).stem, flags=re.IGNORECASE)
    output_name = f"{export_stem}_edited.{extension}"
    return Response(
        content=output_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{output_name}"'},
    )
