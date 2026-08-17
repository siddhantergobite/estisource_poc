# Local CAD test samples

These files were downloaded for local testing of the OCCT STEP/IGES pipeline.

## Recommended first tests

- `sample_iges_1.igs` — small IGES surface/solid-style model.
- `sample_iges_2.igs` — larger IGES model with hundreds of faces.
- `nist/NIST-PMI-STEP-Files/AP203 geometry only/nist_ftc_06_asme1_rd.stp` — NIST STEP geometry test case.
- `nist/NIST-PMI-STEP-Files/nist_ftc_06_asme1_ap242-e2.stp` — STEP AP242 test case.

## More complex IGES tests

- `complex/iges_bearing_reference_sample.iges` — 213 faces and 941 edges.
- `complex/iges_hammer_reference_sample.iges` — 45 faces and 208 edges.
- `complex/bottle/Bottle Detail with Threads.IGS` — detailed NURBS bottle with 1,194 faces and 5,583 edges.
- `complex/nasa/ASSY 8-1-11.igs` — NASA aircraft assembly with 26,371 faces; this extracted file is about 222 MB and currently exceeds the API's 50 MB upload limit.

The NIST test cases are available from the [NIST CAD models page](https://www.nist.gov/ctl/smart-connected-systems-division/smart-connected-manufacturing-systems-group/mbe-pmi-0). NIST states that the test cases and STEP files can be used without restrictions, with acknowledgement appreciated.

The IGES samples came from the [Afanche sample page](https://www.afanche.com/samples) and are provided there for product testing.

The additional complex files came from [SampleFile IGES fixtures](https://samplefile.com/samples/three-d/iges/), the [Industrial Inspection & Analysis NURBS samples](https://industrial-ia.com/resources/nurbs/), and the [NASA Common Research Model](https://commonresearchmodel.larc.nasa.gov/geometry/original-cad-files/).

Two tiny STEP exchange fixtures from SampleFile.com are also in this folder, but they did not contain transferable geometry when tested with OCCT. Use the NIST STEP files first.
