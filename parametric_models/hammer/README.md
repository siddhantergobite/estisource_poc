# Parametric hammer test model

This folder defines the native parametric test source for the project.

1. Install FreeCAD 1.0 or newer.
2. Open `tools/build_parametric_hammer.FCMacro` in FreeCAD's Macro editor.
3. Run the macro.
4. It creates three files in FreeCAD's current working directory:
   - `ParametricHammer.FCStd` — the editable master model;
   - `ParametricHammer.step` — neutral STEP export;
   - `ParametricHammer.iges` — neutral IGES export.
5. Open the `Parameters` spreadsheet in the `.FCStd` file and change `L1` or
   `L2`. `L3` is calculated as `L1 - L2`.

The neutral exports contain the resulting geometry, while the `.FCStd` file
retains the named constraints and construction logic. The JSON file in this
folder is the application-side parameter contract for a future native-master
adapter.

For the current application test profile, keep the generated neutral filename
exactly `ParametricHammer.step` or `ParametricHammer.iges` when uploading it.
That filename activates the hammer schema and rebuild recipe. Other STEP/IGES
files continue to use the existing free-form workflow.

If FreeCAD is unavailable, run
`uv run python tools/generate_parametric_hammer_samples.py` from the repository
root. This creates ready-to-upload neutral fixtures in `samples/parametric/`.
