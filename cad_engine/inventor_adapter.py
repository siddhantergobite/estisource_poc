"""Small, isolated Autodesk Inventor automation adapter.

The neutral OCCT path remains the default for STEP/IGES.  This module is used
only when a native Inventor part is supplied.  It deliberately works on a
working copy so a user's original CAD source is never overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
import re
import shutil
from queue import Queue
from threading import Event, Thread
from typing import Any
from math import pi
from uuid import uuid4

import pythoncom
import win32com.client


INVENTOR_PROG_ID = "Inventor.Application"
STEP_TRANSLATOR_ID = "{90AF7F40-0C01-11D5-8E83-0010B541CD80}"
FILE_BROWSE_IO = 13059
PARAMETER_REFERENCE_RE = re.compile(r"\b[a-zA-Z]\d+\b")


class InventorAdapterError(RuntimeError):
    """A user-facing native Inventor automation failure."""


@dataclass(frozen=True)
class InventorParameter:
    name: str
    expression: str
    value: float
    display_value: float
    units: str
    comment: str
    editable: bool
    label: str | None = None
    feature_bindings: list[dict[str, Any]] = field(default_factory=list)
    mapping_status: str = "unmapped"

    @property
    def mapping_description(self) -> str:
        if not self.feature_bindings:
            return "Inventor model parameter; feature relationship was not detected."
        names = ", ".join(binding["feature_name"] for binding in self.feature_bindings)
        return f"Controls {names}. Geometry highlighting is available when a preview face mapping is resolved."

    def to_dict(self) -> dict[str, Any]:
        record = asdict(self)
        record["label"] = self.label or self.name
        record["mapping_description"] = self.mapping_description
        record["preview_face_ids"] = []
        return record


def _com_error_message(exc: Exception) -> str:
    return str(exc).strip() or exc.__class__.__name__


def _display_value(value: float, units: str) -> float:
    """Convert Inventor's internal database units to the parameter's displayed units."""

    normalized = units.lower().strip()
    if normalized == "in":
        return value / 2.54
    if normalized == "mm":
        return value * 10.0
    if normalized == "cm":
        return value
    if normalized == "deg":
        return value * 180.0 / pi
    return value


def _safe_attr(value: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(value, name)
    except Exception:  # noqa: BLE001 - Inventor COM properties vary by feature type
        return default


def _safe_name(value: Any, default: str = "") -> str:
    name = _safe_attr(value, "Name", default)
    return str(name or default).strip()


def _collection_items(collection: Any) -> list[Any]:
    if collection is None:
        return []
    try:
        count = int(collection.Count)
    except Exception:  # noqa: BLE001 - optional Inventor collection
        return []
    items = []
    for index in range(1, count + 1):
        try:
            items.append(collection.Item(index))
        except Exception:  # noqa: BLE001 - one unavailable item should not abort extraction
            continue
    return items


_FEATURE_COLLECTION_TYPES = (
    ("ExtrudeFeatures", "Extrusion"),
    ("RevolveFeatures", "Revolution"),
    ("HoleFeatures", "Hole"),
    ("FilletFeatures", "Fillet"),
    ("ChamferFeatures", "Chamfer"),
    ("ShellFeatures", "Shell"),
    ("LoftFeatures", "Loft"),
    ("SweepFeatures", "Sweep"),
    ("CoilFeatures", "Coil"),
    ("RibFeatures", "Rib"),
    ("EmbossFeatures", "Emboss"),
    ("ThickenFeatures", "Thicken"),
    ("RectangularPatternFeatures", "Rectangular pattern"),
    ("CircularPatternFeatures", "Circular pattern"),
    ("MirrorFeatures", "Mirror"),
    ("MoveFeatures", "Move"),
    ("FaceFeatures", "Face feature"),
)

_FEATURE_PARAMETER_PATHS = {
    "Extrusion": ("Extent.Distance", "Extent.DistanceTwo", "TaperAngle"),
    "Revolution": ("AngleExtent.Angle", "AngleExtent.AngleTwo"),
    "Hole": (
        "HoleDiameter",
        "CBoreDiameter",
        "CBoreDepth",
        "CSinkDiameter",
        "CSinkAngle",
        "SpotFaceDiameter",
        "SpotFaceDepth",
        "BottomTipAngle",
    ),
    "Fillet": ("ConstantRadiusEdgeSet.Radius", "VariableRadiusEdgeSet.StartRadius", "VariableRadiusEdgeSet.EndRadius"),
    "Chamfer": ("Definition.Distance", "Definition.DistanceOne", "Definition.DistanceTwo", "Definition.Angle"),
    "Shell": ("Definition.Thickness",),
    "Loft": ("StartCondition.Distance", "EndCondition.Distance"),
    "Sweep": ("TaperAngle",),
    "Thicken": ("Distance",),
    "Rectangular pattern": ("RowCount", "ColumnCount", "RowOffset", "ColumnOffset"),
    "Circular pattern": ("Count", "AngleOffset"),
}


def _safe_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        value = _safe_attr(value, part)
        if value is None:
            return None
    return value


def _feature_type_map(component_definition: Any) -> dict[str, str]:
    features = _safe_attr(component_definition, "Features")
    result: dict[str, str] = {}
    for collection_name, display_type in _FEATURE_COLLECTION_TYPES:
        for feature in _collection_items(_safe_attr(features, collection_name)):
            name = _safe_name(feature)
            if name:
                result[name] = display_type
    return result


def _feature_face_count(feature: Any) -> int:
    faces = _safe_attr(feature, "Faces")
    if faces is not None:
        try:
            return int(faces.Count)
        except Exception:  # noqa: BLE001
            pass
    total = 0
    for body in _collection_items(_safe_attr(feature, "SurfaceBodies")):
        total += len(_collection_items(_safe_attr(body, "Faces")))
    return total


def _point_coordinates(point: Any) -> list[float] | None:
    if point is None:
        return None
    try:
        return [float(point.X), float(point.Y), float(point.Z)]
    except Exception:  # noqa: BLE001 - optional COM geometry data
        return None


def _range_box_signature(range_box: Any) -> dict[str, list[float]] | None:
    minimum = _point_coordinates(_safe_attr(range_box, "MinPoint"))
    maximum = _point_coordinates(_safe_attr(range_box, "MaxPoint"))
    if minimum is None or maximum is None:
        return None
    return {"min": minimum, "max": maximum}


def _face_signature(face: Any) -> dict[str, Any] | None:
    range_box = _safe_attr(face, "RangeBox")
    box_signature = _range_box_signature(range_box)
    if box_signature is None:
        return None
    evaluator = _safe_attr(face, "Evaluator")
    area = _safe_attr(evaluator, "Area")
    try:
        area = float(area)
    except (TypeError, ValueError):
        area = None
    return {
        **box_signature,
        "area": area,
        "surface_type": str(_safe_attr(face, "SurfaceType", "")),
    }


def _model_bounds(component_definition: Any) -> dict[str, list[float]] | None:
    range_box = _safe_attr(component_definition, "RangeBox")
    minimum = _point_coordinates(_safe_attr(range_box, "MinPoint"))
    maximum = _point_coordinates(_safe_attr(range_box, "MaxPoint"))
    if minimum is None or maximum is None:
        return None
    return {"min": minimum, "max": maximum}


def _feature_bindings(document: Any) -> dict[str, list[dict[str, Any]]]:
    """Associate model parameters with features without guessing geometry IDs.

    Inventor exposes a feature's own Parameters collection and feature output
    faces. This is a reliable semantic relationship. Mapping those faces to
    the separately exported OCCT mesh is intentionally a later step because
    STEP transfer can change topology ordering.
    """

    component_definition = _safe_attr(document, "ComponentDefinition")
    features = _safe_attr(component_definition, "Features")
    if component_definition is None or features is None:
        return {}
    type_by_name = _feature_type_map(component_definition)
    model_bounds = _model_bounds(component_definition)
    bindings: dict[str, list[dict[str, Any]]] = {}
    for feature_index, feature in enumerate(_collection_items(features), start=1):
        feature_name = _safe_name(feature, f"Feature {feature_index}")
        feature_type = type_by_name.get(feature_name)
        if not feature_type:
            feature_type = re.sub(r"\d+$", "", feature_name).strip() or "Inventor feature"
        feature_faces = _collection_items(_safe_attr(feature, "Faces"))
        face_signatures = [signature for face in feature_faces if (signature := _face_signature(face)) is not None]
        face_count = len(feature_faces) or _feature_face_count(feature)
        feature_bounds = _range_box_signature(_safe_attr(feature, "RangeBox"))
        roles = _FEATURE_PARAMETER_PATHS.get(feature_type, ())
        parameters = _collection_items(_safe_attr(feature, "Parameters"))
        for parameter in parameters:
            parameter_name = _safe_name(parameter)
            if not parameter_name:
                continue
            role = None
            for path in roles:
                candidate = _safe_path(feature, path)
                if _safe_name(candidate) == parameter_name:
                    role = path.rsplit(".", 1)[-1]
                    break
            bindings.setdefault(parameter_name, []).append(
                {
                    "feature_name": feature_name,
                    "feature_type": feature_type,
                    "feature_index": feature_index,
                    "parameter_role": role,
                    "face_count": face_count,
                    "face_signatures": face_signatures,
                    "feature_bounds": feature_bounds,
                    "model_bounds": model_bounds,
                    "preview_face_ids": [],
                }
            )
    return bindings


def _parameter_label(name: str, bindings: list[dict[str, Any]]) -> str:
    if not bindings:
        return name
    binding = bindings[0]
    role = binding.get("parameter_role")
    if role:
        readable_role = re.sub(r"(?<!^)([A-Z])", r" \1", role).strip()
        return f"{binding['feature_name']} · {readable_role}"
    return f"{binding['feature_name']} · {name}"


class InventorAdapter:
    """Automate a licensed local Inventor installation through its COM API."""

    def __init__(self, *, visible: bool = False, reuse_active: bool = False) -> None:
        pythoncom.CoInitialize()
        try:
            if reuse_active:
                self.application = win32com.client.GetActiveObject(INVENTOR_PROG_ID)
                self._owns_application = False
            else:
                self.application = win32com.client.DispatchEx(INVENTOR_PROG_ID)
                self._owns_application = True
            self.application.Visible = visible
            # Prevent Inventor's modal warnings/dialogs from blocking an API
            # request when an assembly has unresolved optional references.
            self.application.SilentOperation = True
        except Exception as exc:  # noqa: BLE001 - normalize COM failures
            pythoncom.CoUninitialize()
            raise InventorAdapterError(
                "Autodesk Inventor could not be started or connected. "
                "Install and activate the full Inventor desktop application. "
                f"({_com_error_message(exc)})"
            ) from exc

    def close(self) -> None:
        try:
            if self._owns_application:
                self.application.Quit()
        finally:
            pythoncom.CoUninitialize()

    def __enter__(self) -> "InventorAdapter":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _open_document(self, source_path: Path):
        if not source_path.is_file():
            raise InventorAdapterError(f"Native Inventor file not found: {source_path}")
        try:
            return self.application.Documents.Open(str(source_path), False)
        except Exception as exc:  # noqa: BLE001
            raise InventorAdapterError(f"Inventor could not open {source_path}: {_com_error_message(exc)}") from exc

    @staticmethod
    def _parameters(document):
        try:
            parameters = document.ComponentDefinition.Parameters
            try:
                return parameters.ModelParameters
            except Exception:
                # AssemblyComponentDefinition exposes the same parameter
                # collection directly in some Inventor versions.
                return parameters
        except Exception as exc:  # noqa: BLE001
            raise InventorAdapterError(
                "The native document does not expose an Inventor parameter table. "
                f"({_com_error_message(exc)})"
            ) from exc

    def discover(self, source_path: str | Path) -> list[InventorParameter]:
        """Read native model parameters and their editability metadata."""

        document = self._open_document(Path(source_path))
        try:
            return self._parameter_records(self._parameters(document), document)
        finally:
            document.Close(False)

    @staticmethod
    def _parameter_records(parameters, document=None) -> list[InventorParameter]:
        bindings_by_name = _feature_bindings(document) if document is not None else {}
        result: list[InventorParameter] = []
        for index in range(1, parameters.Count + 1):
            parameter = parameters.Item(index)
            expression = str(parameter.Expression)
            name = str(parameter.Name)
            references = set(PARAMETER_REFERENCE_RE.findall(expression)) - {name}
            feature_bindings = bindings_by_name.get(name, [])
            result.append(
                InventorParameter(
                    name=name,
                    expression=expression,
                    value=float(parameter.Value),
                    display_value=_display_value(float(parameter.Value), str(parameter.Units)),
                    units=str(parameter.Units),
                    comment=str(parameter.Comment or ""),
                    editable=not references,
                    label=_parameter_label(name, feature_bindings),
                    feature_bindings=feature_bindings,
                    mapping_status="feature" if feature_bindings else "parameter_only",
                )
            )
        return result

    def rebuild_to_step(
        self,
        source_path: str | Path,
        output_step: str | Path,
        updates: dict[str, str | float | int],
        *,
        working_directory: str | Path | None = None,
    ) -> list[InventorParameter]:
        """Apply native expressions to a working copy and export a STEP file."""

        source = Path(source_path)
        destination = Path(output_step)
        working_root = Path(working_directory) if working_directory else destination.parent
        working_root.mkdir(parents=True, exist_ok=True)
        working_copy = working_root / f"{source.stem}_working_{id(self)}{source.suffix}"
        shutil.copy2(source, working_copy)
        document = self._open_document(working_copy)
        try:
            parameters = self._parameters(document)
            by_name = {str(parameters.Item(index).Name): parameters.Item(index) for index in range(1, parameters.Count + 1)}
            unknown = sorted(set(updates) - set(by_name))
            if unknown:
                raise InventorAdapterError(f"Unknown Inventor parameters: {', '.join(unknown)}")
            for name, requested in updates.items():
                parameter = by_name[name]
                if isinstance(requested, (int, float)):
                    expression = f"{requested} {parameter.Units}" if parameter.Units not in {"ul", "unitless"} else str(requested)
                else:
                    expression = str(requested)
                parameter.Expression = expression
            update2 = _safe_attr(document, "Update2")
            if update2 is not None:
                try:
                    update2(True)
                except Exception:
                    document.Update()
            else:
                document.Update()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.unlink(missing_ok=True)
            translator = self.application.ApplicationAddIns.ItemById(STEP_TRANSLATOR_ID)
            translator.Activate()
            context = self.application.TransientObjects.CreateTranslationContext()
            context.Type = FILE_BROWSE_IO
            options = self.application.TransientObjects.CreateNameValueMap()
            data_medium = self.application.TransientObjects.CreateDataMedium()
            data_medium.FileName = str(destination)
            translator.SaveCopyAs(document, context, options, data_medium)
            if not destination.is_file() or destination.stat().st_size == 0:
                raise InventorAdapterError("Inventor did not produce a STEP export.")
            return self._parameter_records(parameters, document)
        except InventorAdapterError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise InventorAdapterError(f"Inventor rebuild/export failed: {_com_error_message(exc)}") from exc
        finally:
            document.Close(False)
            try:
                working_copy.unlink(missing_ok=True)
            except OSError:
                pass


class InventorWorker:
    """Keep one Inventor COM session on one dedicated thread.

    Inventor is an out-of-process desktop application and its COM objects are
    apartment-bound.  A dedicated worker thread lets the API reuse the same
    Inventor process and working document safely across HTTP requests.
    """

    def __init__(self) -> None:
        self._calls: Queue = Queue()
        self._ready = Event()
        self._thread = Thread(target=self._run, name="inventor-worker", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        adapter: InventorAdapter | None = None
        document = None
        parameters = None
        source: Path | None = None
        working_copy: Path | None = None
        startup_error: Exception | None = None
        try:
            adapter = InventorAdapter(visible=False)
        except Exception as exc:  # noqa: BLE001 - return startup failures to callers
            startup_error = exc
        self._ready.set()

        while True:
            operation, args, result = self._calls.get()
            if operation == "close":
                self._close_session(document, working_copy)
                if adapter is not None:
                    adapter.close()
                result["value"] = None
                result["event"].set()
                return
            try:
                if startup_error is not None:
                    raise startup_error
                if adapter is None:
                    raise InventorAdapterError("Inventor worker could not start.")
                if operation == "discover":
                    self._ensure_session(adapter, args[0], source, document, parameters, working_copy)
                    source, document, parameters, working_copy = self._session_state
                    result["value"] = adapter._parameter_records(parameters, document)
                elif operation == "rebuild":
                    source_path, output_step, updates = args
                    self._ensure_session(adapter, source_path, source, document, parameters, working_copy)
                    source, document, parameters, working_copy = self._session_state
                    by_name = {
                        str(parameters.Item(index).Name): parameters.Item(index)
                        for index in range(1, parameters.Count + 1)
                    }
                    unknown = sorted(set(updates) - set(by_name))
                    if unknown:
                        raise InventorAdapterError(f"Unknown Inventor parameters: {', '.join(unknown)}")
                    for name, requested in updates.items():
                        parameter = by_name[name]
                        if isinstance(requested, (int, float)):
                            expression = (
                                f"{requested} {parameter.Units}"
                                if parameter.Units not in {"ul", "unitless"}
                                else str(requested)
                            )
                        else:
                            expression = str(requested)
                        parameter.Expression = expression
                    update2 = _safe_attr(document, "Update2")
                    if update2 is not None:
                        try:
                            update2(True)
                        except Exception:
                            document.Update()
                    else:
                        document.Update()
                    self._export_step(adapter, document, Path(output_step))
                    result["value"] = adapter._parameter_records(parameters, document)
                elif operation == "export_ipt":
                    source_path, output_ipt = args
                    self._ensure_session(adapter, source_path, source, document, parameters, working_copy)
                    source, document, parameters, working_copy = self._session_state
                    destination = Path(output_ipt)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.unlink(missing_ok=True)
                    document.SaveAs(str(destination), True)
                    if not destination.is_file() or destination.stat().st_size == 0:
                        raise InventorAdapterError("Inventor did not produce an IPT export.")
                    result["value"] = destination.read_bytes()
                elif operation == "discard":
                    self._close_session(document, working_copy)
                    source = document = parameters = working_copy = None
                    result["value"] = None
                else:
                    raise InventorAdapterError(f"Unknown Inventor worker operation: {operation}")
            except Exception as exc:  # noqa: BLE001 - pass COM failures to API
                if operation == "rebuild":
                    self._close_session(document, working_copy)
                    source = document = parameters = working_copy = None
                result["error"] = exc
            finally:
                result["event"].set()

    _session_state: tuple[Path | None, Any, Any, Path | None] = (None, None, None, None)

    def _ensure_session(
        self,
        adapter: InventorAdapter,
        source_path: str | Path,
        source: Path | None,
        document,
        parameters,
        working_copy: Path | None,
    ) -> None:
        requested_source = Path(source_path).resolve()
        if source == requested_source and document is not None and parameters is not None:
            self._session_state = (source, document, parameters, working_copy)
            return
        self._close_session(document, working_copy)
        working_copy = requested_source.with_name(f".{requested_source.stem}_{uuid4().hex}_working{requested_source.suffix}")
        shutil.copy2(requested_source, working_copy)
        document = adapter._open_document(working_copy)
        parameters = adapter._parameters(document)
        self._session_state = (requested_source, document, parameters, working_copy)

    @staticmethod
    def _export_step(adapter: InventorAdapter, document, output_step: Path) -> None:
        output_step.parent.mkdir(parents=True, exist_ok=True)
        translator = adapter.application.ApplicationAddIns.ItemById(STEP_TRANSLATOR_ID)
        translator.Activate()
        context = adapter.application.TransientObjects.CreateTranslationContext()
        context.Type = FILE_BROWSE_IO
        options = adapter.application.TransientObjects.CreateNameValueMap()
        data_medium = adapter.application.TransientObjects.CreateDataMedium()
        data_medium.FileName = str(output_step)
        translator.SaveCopyAs(document, context, options, data_medium)
        if not output_step.is_file() or output_step.stat().st_size == 0:
            raise InventorAdapterError("Inventor did not produce a STEP export.")

    @staticmethod
    def _close_session(document, working_copy: Path | None) -> None:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                pass
        if working_copy is not None:
            try:
                working_copy.unlink(missing_ok=True)
            except OSError:
                pass

    def _request(self, operation: str, *args):
        result = {"event": Event()}
        self._calls.put((operation, args, result))
        result["event"].wait()
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def discover(self, source_path: str | Path) -> list[InventorParameter]:
        return self._request("discover", source_path)

    def rebuild_to_step(
        self,
        source_path: str | Path,
        output_step: str | Path,
        updates: dict[str, str | float | int],
    ) -> list[InventorParameter]:
        return self._request("rebuild", source_path, output_step, updates)

    def discard_session(self) -> None:
        self._request("discard")

    def export_to_native(self, source_path: str | Path, output_native: str | Path) -> bytes:
        return self._request("export_ipt", source_path, output_native)

    def export_to_ipt(self, source_path: str | Path, output_ipt: str | Path) -> bytes:
        """Backward-compatible alias for native Inventor part export."""

        return self.export_to_native(source_path, output_ipt)


_WORKER: InventorWorker | None = None


def get_inventor_worker() -> InventorWorker:
    global _WORKER
    if _WORKER is None:
        _WORKER = InventorWorker()
    return _WORKER
