"""Open CASCADE based STEP/IGES loading, inspection, meshing, and edits."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from math import cos, radians, sin
from pathlib import Path
from threading import Lock
from tempfile import NamedTemporaryFile

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.IGESControl import IGESControl_Reader, IGESControl_Writer
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.TopLoc import TopLoc_Location
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDocStd import TDocStd_Document
from OCP.TDF import TDF_LabelSequence
from OCP.XCAFDoc import XCAFDoc_Dimension, XCAFDoc_DocumentTool
from OCP.gp import gp_GTrsf, gp_Mat, gp_XYZ


SUPPORTED_FORMATS = {"step", "stp", "iges", "igs"}
_OCCT_NATIVE_OUTPUT_LOCK = Lock()


@contextmanager
def _quiet_native_stdout():
    """Mute native OCCT reader chatter without changing application logging."""

    with _OCCT_NATIVE_OUTPUT_LOCK:
        saved_stdout = os.dup(1)
        null_stdout = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_stdout, 1)
            yield
        finally:
            os.dup2(saved_stdout, 1)
            os.close(null_stdout)
            os.close(saved_stdout)


@dataclass(frozen=True)
class Cad3DAnalysis:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float
    solid_count: int
    face_count: int
    edge_count: int
    vertex_count: int
    valid: bool
    mesh: dict

    @property
    def length(self) -> float:
        return max(0.0, self.max_x - self.min_x)

    @property
    def breadth(self) -> float:
        return max(0.0, self.max_y - self.min_y)

    @property
    def height(self) -> float:
        return max(0.0, self.max_z - self.min_z)


def _shape_from_file(path: Path, source_format: str):
    if source_format in {"step", "stp"}:
        reader = STEPControl_Reader()
    else:
        reader = IGESControl_Reader()
    with _quiet_native_stdout():
        status = reader.ReadFile(str(path))
    if status != 1:  # IFSelect_RetDone
        raise ValueError(f"Could not read the {source_format.upper()} file.")
    transferred = reader.TransferRoots()
    if transferred <= 0:
        raise ValueError(f"The {source_format.upper()} file contains no transferable geometry.")
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError(f"The {source_format.upper()} file produced an empty model.")
    return shape


def load_shape(data: bytes, source_format: str):
    """Load a STEP or IGES byte stream into an OCCT shape."""

    normalized = source_format.lower().lstrip(".")
    if normalized not in SUPPORTED_FORMATS:
        raise ValueError("Supported 3D CAD formats are STEP and IGES.")
    if not data:
        raise ValueError("The uploaded CAD file is empty.")
    suffix = ".step" if normalized in {"step", "stp"} else ".iges"
    with NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    try:
        return _shape_from_file(temporary_path, normalized)
    finally:
        temporary_path.unlink(missing_ok=True)


def _string_value(value) -> str:
    """Convert an OCCT string wrapper to ordinary text."""

    if value is None:
        return ""
    try:
        return value.ToCString().strip() if hasattr(value, "ToCString") else str(value).strip()
    except AttributeError:
        return str(value).strip()


def _pmi_type_label(dimension_type) -> str:
    type_name = getattr(dimension_type, "name", str(dimension_type))
    type_name = type_name.removeprefix("XCAFDimTolObjects_DimensionType_")
    return type_name.replace("_", " ").strip().title()


def _read_step_pmi_file(path: Path) -> list[dict]:
    """Read semantic AP242 dimensions from a STEP file using XCAF.

    STEPControl_Reader deliberately reads the shape only.  STEPCAFControl_Reader
    is used here as a second pass so PMI can be reported without changing the
    geometry import path.  PMI is source metadata; it is not automatically a
    rebuild recipe for an arbitrary B-Rep.
    """

    document = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    reader = STEPCAFControl_Reader()
    reader.SetGDTMode(True)
    reader.SetNameMode(True)
    reader.SetPropsMode(True)
    with _quiet_native_stdout():
        status = reader.ReadFile(str(path))
        if status != 1 or not reader.Transfer(document):
            return []

    dim_tol_tool = XCAFDoc_DocumentTool.DimTolTool_s(document.Main())
    labels = TDF_LabelSequence()
    dim_tol_tool.GetDimensionLabels(labels)
    dimensions: list[dict] = []
    for index in range(1, labels.Length() + 1):
        dimension = XCAFDoc_Dimension.Set_s(labels.Value(index)).GetObject()
        value = float(dimension.GetValue())
        dimension_type = dimension.GetType()
        type_name = getattr(dimension_type, "name", str(dimension_type))
        # Presentation-only labels and empty location labels are not input
        # values, so do not turn them into misleading fields.
        if value == 0.0 and "Presentation" in type_name:
            continue
        if value == 0.0 and type_name.endswith("Location_None"):
            continue
        lower_bound = float(dimension.GetLowerBound())
        upper_bound = float(dimension.GetUpperBound())
        lower_tolerance = float(dimension.GetLowerTolValue())
        upper_tolerance = float(dimension.GetUpperTolValue())
        semantic_name = _string_value(dimension.GetSemanticName())
        dimensions.append(
            {
                "key": f"pmi_{len(dimensions) + 1}",
                "label": semantic_name or f"PMI {len(dimensions) + 1} · {_pmi_type_label(dimension_type)}",
                "type": _pmi_type_label(dimension_type),
                "value": round(value, 6),
                "unit": "deg" if "Angular" in type_name else "model units",
                "lower_tolerance": round(lower_tolerance, 6) if lower_tolerance else None,
                "upper_tolerance": round(upper_tolerance, 6) if upper_tolerance else None,
                "lower_bound": round(lower_bound, 6) if lower_bound else None,
                "upper_bound": round(upper_bound, 6) if upper_bound else None,
                "source": "AP242 semantic PMI",
                "editable": False,
            }
        )
    return dimensions


def extract_pmi(data: bytes, source_format: str) -> list[dict]:
    """Extract real semantic PMI values when the uploaded file contains them."""

    if source_format not in {"step", "stp"} or not data:
        return []
    with NamedTemporaryFile(suffix=".step", delete=False) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    try:
        return _read_step_pmi_file(temporary_path)
    except Exception:  # PMI is optional; never make a valid geometry upload fail.
        return []
    finally:
        temporary_path.unlink(missing_ok=True)


def export_shape(shape, source_format: str) -> bytes:
    """Export an OCCT shape to the requested neutral CAD format."""

    normalized = source_format.lower().lstrip(".")
    suffix = ".step" if normalized in {"step", "stp"} else ".iges"
    with NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        output_path = Path(temporary.name)
    try:
        if normalized in {"step", "stp"}:
            writer = STEPControl_Writer()
            status = writer.Transfer(shape, STEPControl_AsIs)
            if status != 1 or writer.Write(str(output_path)) != 1:
                raise ValueError("Could not export the edited STEP model.")
        else:
            writer = IGESControl_Writer()
            writer.AddShape(shape)
            if not writer.Write(str(output_path)):
                raise ValueError("Could not export the edited IGES model.")
        return output_path.read_bytes()
    finally:
        output_path.unlink(missing_ok=True)


def _count_shapes(shape, shape_type) -> int:
    explorer = TopExp_Explorer(shape, shape_type)
    count = 0
    while explorer.More():
        count += 1
        explorer.Next()
    return count


def _mesh_shape(shape, deflection: float = 0.8, max_triangles: int = 120_000) -> dict:
    """Create a compact triangle mesh suitable for a Three.js preview."""

    BRepMesh_IncrementalMesh(shape, deflection, True)
    vertices: list[float] = []
    indices: list[int] = []
    triangle_count = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More() and triangle_count < max_triangles:
        face = TopoDS.Face_s(explorer.Current())
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is not None:
            transformation = location.Transformation()
            reversed_face = face.Orientation() == TopAbs_REVERSED
            for triangle_index in range(1, triangulation.NbTriangles() + 1):
                if triangle_count >= max_triangles:
                    break
                n1, n2, n3 = triangulation.Triangle(triangle_index).Get()
                node_ids = (n1, n3, n2) if reversed_face else (n1, n2, n3)
                start_index = len(vertices) // 3
                for node_id in node_ids:
                    point = triangulation.Node(node_id).Transformed(transformation)
                    vertices.extend((point.X(), point.Y(), point.Z()))
                indices.extend((start_index, start_index + 1, start_index + 2))
                triangle_count += 1
        explorer.Next()
    return {"vertices": vertices, "indices": indices, "triangle_count": triangle_count}


def analyze_shape(shape, include_mesh: bool = True) -> Cad3DAnalysis:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    min_x, min_y, min_z, max_x, max_y, max_z = box.Get()
    return Cad3DAnalysis(
        min_x=min_x,
        min_y=min_y,
        min_z=min_z,
        max_x=max_x,
        max_y=max_y,
        max_z=max_z,
        solid_count=_count_shapes(shape, TopAbs_SOLID),
        face_count=_count_shapes(shape, TopAbs_FACE),
        edge_count=_count_shapes(shape, TopAbs_EDGE),
        vertex_count=_count_shapes(shape, TopAbs_VERTEX),
        valid=bool(BRepCheck_Analyzer(shape).IsValid()),
        mesh=_mesh_shape(shape) if include_mesh else {"vertices": [], "indices": [], "triangle_count": 0},
    )


def detect_geometry_features(shape) -> list[dict]:
    """Return measurements inferred from the imported topology.

    These are intentionally reported as detected facts. They are not exposed
    as editable feature parameters unless a model schema maps them to a safe
    reconstruction operation.
    """

    surface_types = {
        GeomAbs_SurfaceType.GeomAbs_Plane: "Planar faces",
        GeomAbs_SurfaceType.GeomAbs_Cylinder: "Cylindrical faces",
        GeomAbs_SurfaceType.GeomAbs_Cone: "Conical faces",
        GeomAbs_SurfaceType.GeomAbs_Sphere: "Spherical faces",
        GeomAbs_SurfaceType.GeomAbs_Torus: "Toroidal faces",
        GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution: "Revolved faces",
        GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "Free-form faces",
        GeomAbs_SurfaceType.GeomAbs_BezierSurface: "Bezier faces",
        GeomAbs_SurfaceType.GeomAbs_OffsetSurface: "Offset faces",
        GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion: "Extruded faces",
    }
    counts = {key: 0 for key in surface_types}
    cylinder_diameters: list[float] = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        surface = BRepAdaptor_Surface(face, True)
        surface_type = surface.GetType()
        if surface_type in counts:
            counts[surface_type] += 1
        if surface_type == GeomAbs_SurfaceType.GeomAbs_Cylinder:
            cylinder_diameters.append(float(surface.Cylinder().Radius() * 2.0))
        explorer.Next()

    features = [
        {"key": f"surface_{surface_type.name.lower()}", "label": label, "value": count, "unit": "faces", "editable": False}
        for surface_type, label in surface_types.items()
        if (count := counts[surface_type]) > 0
    ]
    if cylinder_diameters:
        features.append({"key": "cylinder_diameter_min", "label": "Smallest cylindrical diameter", "value": round(min(cylinder_diameters), 6), "unit": "model units", "editable": False})
        features.append({"key": "cylinder_diameter_max", "label": "Largest cylindrical diameter", "value": round(max(cylinder_diameters), 6), "unit": "model units", "editable": False})
    return features


def apply_freeform_parameters(shape, current: dict[str, float], target: dict[str, float]):
    """Scale a shape to target dimensions and rotate it around its Z axis.

    This is the generic/free-form mode. Model-specific feature constraints will
    be layered on top once a designer defines a parameter schema for a model.
    """

    current_length = float(current["length"])
    current_breadth = float(current["breadth"])
    current_height = float(current["height"])
    target_length = float(target["length"])
    target_breadth = float(target["breadth"])
    target_height = float(target["height"])
    if min(target_length, target_breadth, target_height) <= 0:
        raise ValueError("Length, breadth, and height must be greater than zero.")
    if min(current_length, current_breadth, current_height) <= 0:
        raise ValueError("The model must have measurable length, breadth, and height.")

    sx = target_length / current_length
    sy = target_breadth / current_breadth
    sz = target_height / current_height
    angle = radians(float(target["angle"]) - float(current.get("angle", 0.0)))
    cosine, sine = cos(angle), sin(angle)
    matrix = gp_Mat(
        cosine * sx,
        -sine * sy,
        0.0,
        sine * sx,
        cosine * sy,
        0.0,
        0.0,
        0.0,
        sz,
    )
    analysis = analyze_shape(shape, include_mesh=False)
    center_x = (analysis.min_x + analysis.max_x) / 2
    center_y = (analysis.min_y + analysis.max_y) / 2
    center_z = (analysis.min_z + analysis.max_z) / 2
    transformed_center_x = cosine * sx * center_x - sine * sy * center_y
    transformed_center_y = sine * sx * center_x + cosine * sy * center_y
    transformed_center_z = sz * center_z
    transform = gp_GTrsf()
    transform.SetVectorialPart(matrix)
    transform.SetTranslationPart(
        gp_XYZ(center_x - transformed_center_x, center_y - transformed_center_y, center_z - transformed_center_z)
    )
    return BRepBuilderAPI_GTransform(shape, transform, True).Shape()
