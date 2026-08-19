"""Generate the ready-to-upload ParametricHammer STEP and IGES fixtures."""

from pathlib import Path

from cad_engine.occt_engine import export_shape, make_parametric_hammer_shape


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "samples" / "parametric"
VALUES = {
    "L1": 300.0,
    "L2": 80.0,
    "L3": 220.0,
    "HandleDiameter": 32.0,
    "HeadWidth": 120.0,
    "HeadHeight": 60.0,
    "HeadThickness": 42.0,
    "ClawAngle": 25.0,
}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    shape = make_parametric_hammer_shape(VALUES)
    (OUTPUT_DIR / "ParametricHammer.step").write_bytes(export_shape(shape, "step"))
    (OUTPUT_DIR / "ParametricHammer.iges").write_bytes(export_shape(shape, "iges"))
    print(f"Generated STEP and IGES fixtures in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
