"""HTTP API for STEP/IGES upload, 3D inspection, editing, and export."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import isfinite
from pathlib import Path
import re
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from cad_engine.occt_engine import (
    SUPPORTED_FORMATS,
    analyze_shape,
    apply_freeform_parameters,
    detect_geometry_features,
    export_shape,
    extract_pmi,
    detect_model_units,
    load_shape,
    make_parametric_hammer_shape,
)


MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_DOCUMENTS = 20
PARAMETER_METADATA_PATH = Path(__file__).resolve().parents[1] / ".cad_parameter_metadata.json"

HAMMER_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "parametric_models" / "hammer" / "hammer_schema.json"
HAMMER_CONTRACT = json.loads(HAMMER_SCHEMA_PATH.read_text(encoding="utf-8"))
HAMMER_SCHEMA = HAMMER_CONTRACT["parameters"]
HAMMER_DEFAULTS = {parameter["key"]: float(parameter["default"]) for parameter in HAMMER_SCHEMA}
HAMMER_CONSTRAINTS = HAMMER_CONTRACT["constraints"]


@dataclass
class StoredDocument:
    document_id: str
    filename: str
    original_bytes: bytes
    working_bytes: bytes
    source_format: str
    parameter_state: dict[str, float]
    pmi_dimensions: list[dict]
    unit_info: dict[str, str | bool]
    profile: str | None = None
    profile_state: dict[str, float] | None = None


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
    if extension not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail="Supported 3D CAD files are .step, .stp, .iges, and .igs.")
    return "step" if extension in {"step", "stp"} else "iges"


def _profile_for_filename(filename: str) -> str | None:
    stem = Path(filename).stem.lower().replace("-", "_").replace(" ", "_")
    return "parametric_hammer" if "parametrichammer" in stem or "parametric_hammer" in stem else None


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
    try:
        return load_shape(document.working_bytes, document.source_format)
    except Exception as exc:  # noqa: BLE001 - return parser details to the client
        raise HTTPException(status_code=400, detail=f"Could not read the 3D CAD model: {exc}") from exc


def _initial_parameters(shape) -> dict[str, float]:
    analysis = analyze_shape(shape, include_mesh=False)
    return {"length": round(analysis.length, 6), "breadth": round(analysis.breadth, 6), "height": round(analysis.height, 6), "angle": 0.0}


def _is_simple_model(analysis) -> bool:
    """Permit generic scaling only for small, single-part geometry."""

    return analysis.face_count <= 20 and analysis.edge_count <= 80 and analysis.solid_count <= 1


def _analysis_payload(document: StoredDocument, shape=None) -> dict:
    shape = shape or _load_document_shape(document)
    analysis = analyze_shape(shape)
    values = document.parameter_state
    is_simple = _is_simple_model(analysis)
    is_parametric = document.profile == "parametric_hammer"
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
    if is_parametric:
        profile_values = document.profile_state or HAMMER_DEFAULTS
        parameters = [
            {
                **parameter,
                "value": profile_values[parameter["key"]],
                "unit": "deg" if parameter["unit"] == "deg" else unit_info["symbol"],
            }
            for parameter in HAMMER_SCHEMA
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
        "engine": "occt",
        "mode": "parametric" if is_parametric else "freeform",
        "editing_available": editing_available,
        "complexity": "simple" if is_simple else "complex",
        "complexity_reason": "Named hammer parameters are linked to a rebuild recipe." if is_parametric else "Simple geometry supports free-form editing." if is_simple else "Complex geometry supports whole-model scaling and rotation. Named feature parameters require a designer-defined schema.",
        "units": unit_info,
        "length": values["length"],
        "breadth": values["breadth"],
        "height": values["height"],
        "angle": values["angle"],
        "parameters": parameters,
        "parameter_schema": HAMMER_SCHEMA if is_parametric else None,
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
            "message": "L3 = L1 - L2; L1 must be greater than L2." if is_parametric else "Free-form mode: dimensions and Z rotation may change independently.",
            "rules": HAMMER_CONSTRAINTS if is_parametric else [],
        },
    }


def _get_document(document_id: str) -> StoredDocument:
    document = DOCUMENTS.get(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="CAD document not found or session expired.")
    return document


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "engine": "occt", "formats": "STEP, IGES"}


@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...)) -> dict:
    filename = Path(file.filename or "model.step").name
    source_format = _canonical_format(filename)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded CAD file is empty.")
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="3D CAD files are limited to 50 MB in this PoC.")
    try:
        shape = load_shape(data, source_format)
    except Exception as exc:  # noqa: BLE001 - return parser details to the client
        raise HTTPException(status_code=400, detail=f"Could not read the {source_format.upper()} model: {exc}") from exc
    if len(DOCUMENTS) >= MAX_DOCUMENTS:
        DOCUMENTS.pop(next(iter(DOCUMENTS)))
    document_id = uuid4().hex
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
        data,
        source_format,
        _initial_parameters(shape),
        extract_pmi(data, source_format),
        detect_model_units(data, source_format),
        profile,
        profile_state,
    )
    DOCUMENTS[document_id] = document
    return _analysis_payload(document, shape)


@app.post("/api/documents/{document_id}/parameters")
def apply_parameters(document_id: str, request: ParameterRequest) -> dict:
    document = _get_document(document_id)
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
        document.working_bytes = working_bytes
        document.profile_state = target
        document.parameter_state = _initial_parameters(updated)
        return _analysis_payload(document, updated)

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
        updated = apply_freeform_parameters(shape, document.parameter_state, target)
        working_bytes = export_shape(updated, document.source_format)
    except Exception as exc:  # noqa: BLE001 - CAD kernel errors are client-edit errors
        raise HTTPException(status_code=400, detail=f"Could not update the CAD model: {exc}") from exc
    document.working_bytes = working_bytes
    document.parameter_state = target
    return _analysis_payload(document, updated)


@app.post("/api/documents/{document_id}/reset")
def reset_document(document_id: str) -> dict:
    document = _get_document(document_id)
    document.working_bytes = document.original_bytes
    shape = _load_document_shape(document)
    document.parameter_state = _initial_parameters(shape)
    if document.profile == "parametric_hammer":
        document.profile_state = dict(HAMMER_DEFAULTS)
    return _analysis_payload(document, shape)


@app.get("/api/documents/{document_id}/download")
def download_document(document_id: str, format: str | None = Query(default=None)) -> Response:
    document = _get_document(document_id)
    requested_format = document.source_format if format is None else format.lower().lstrip(".")
    if requested_format in {"stp", "step"}:
        requested_format = "step"
    elif requested_format in {"igs", "iges"}:
        requested_format = "iges"
    else:
        raise HTTPException(status_code=400, detail="Export format must be STEP or IGES.")
    try:
        output_bytes = document.working_bytes if requested_format == document.source_format else export_shape(_load_document_shape(document), requested_format)
    except Exception as exc:  # noqa: BLE001 - return export details to the client
        raise HTTPException(status_code=400, detail=f"Could not export the model as {requested_format.upper()}: {exc}") from exc
    media_type = "application/step" if requested_format == "step" else "application/iges"
    extension = "step" if requested_format == "step" else "iges"
    if document.profile == "parametric_hammer":
        _remember_parameter_metadata(hashlib.sha256(output_bytes).hexdigest(), document.profile_state or HAMMER_DEFAULTS)
    export_stem = re.sub(r"(?:_edited)+$", "", Path(document.filename).stem, flags=re.IGNORECASE)
    output_name = f"{export_stem}_edited.{extension}"
    return Response(
        content=output_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{output_name}"'},
    )
