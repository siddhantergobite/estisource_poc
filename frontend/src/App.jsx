import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import {
  Box,
  Check,
  Download,
  FilePlus2,
  Layers3,
  Maximize2,
  RotateCcw,
  Ruler,
  Upload,
  X,
} from "lucide-react";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";
const ACCEPTED_EXTENSIONS = ["step", "stp", "iges", "igs", "ipt", "iam"];

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function inputValue(value, digits = 3) {
  const number = Number(value);
  return Number.isFinite(number) ? Number(number.toFixed(digits)) : "";
}

function CadViewer({ mesh, highlightedFaceIds = [], onFaceHover, hoverLabel = "" }) {
  const mountRef = useRef(null);
  const canvasHostRef = useRef(null);
  const highlightMeshRef = useRef(null);
  const onFaceHoverRef = useRef(onFaceHover);
  const [hoverPoint, setHoverPoint] = useState(null);

  useEffect(() => {
    onFaceHoverRef.current = onFaceHover;
  }, [onFaceHover]);

  useEffect(() => {
    const highlightMesh = highlightMeshRef.current;
    if (!highlightMesh || !mesh?.indices?.length) return;
    const highlighted = new Set(highlightedFaceIds);
    const triangleFaceIds = mesh.triangle_face_ids ?? [];
    const highlightIndices = [];
    for (let triangleIndex = 0; triangleIndex < triangleFaceIds.length; triangleIndex += 1) {
      if (!highlighted.has(triangleFaceIds[triangleIndex])) continue;
      const offset = triangleIndex * 3;
      highlightIndices.push(mesh.indices[offset], mesh.indices[offset + 1], mesh.indices[offset + 2]);
    }
    highlightMesh.geometry.setAttribute("position", new THREE.Float32BufferAttribute(mesh.vertices, 3));
    highlightMesh.geometry.setIndex(highlightIndices);
    highlightMesh.geometry.computeVertexNormals();
    highlightMesh.geometry.center();
    highlightMesh.visible = highlightIndices.length > 0;
  }, [highlightedFaceIds, mesh]);

  useEffect(() => {
    const mount = canvasHostRef.current;
    const viewer = mountRef.current;
    if (!mount || !viewer || !mesh?.vertices?.length || !mesh?.indices?.length) return undefined;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#0a304d");
    const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100000);
    camera.position.set(150, 120, 170);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setClearColor("#0a304d");
    mount.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.enableRotate = true;
    controls.enableZoom = true;
    controls.enablePan = true;
    controls.screenSpacePanning = true;
    controls.target.set(0, 0, 0);
    renderer.domElement.style.touchAction = "none";

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(mesh.vertices, 3));
    geometry.setIndex(mesh.indices);
    geometry.computeVertexNormals();
    geometry.computeBoundingBox();
    geometry.center();

    const box = geometry.boundingBox;
    const size = box ? box.getSize(new THREE.Vector3()) : new THREE.Vector3(100, 100, 100);
    const maxSize = Math.max(size.x, size.y, size.z, 1);
    camera.position.set(maxSize * 1.55, maxSize * 1.2, maxSize * 1.7);
    camera.near = maxSize / 1000;
    camera.far = maxSize * 100;
    camera.updateProjectionMatrix();
    controls.target.set(0, 0, 0);
    controls.update();

    const model = new THREE.Mesh(
      geometry,
      new THREE.MeshStandardMaterial({ color: "#68bced", roughness: 0.5, metalness: 0.08, side: THREE.DoubleSide }),
    );
    const highlightMesh = new THREE.Mesh(
      new THREE.BufferGeometry(),
      new THREE.MeshBasicMaterial({ color: "#ffd166", transparent: true, opacity: 0.78, side: THREE.DoubleSide, depthWrite: false }),
    );
    highlightMesh.visible = false;
    highlightMeshRef.current = highlightMesh;
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(geometry, 28),
      new THREE.LineBasicMaterial({ color: "#b9eaff", transparent: true, opacity: 0.28 }),
    );
    scene.add(model, highlightMesh, edges);

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const handlePointerMove = (event) => {
      const bounds = renderer.domElement.getBoundingClientRect();
      setHoverPoint({ x: event.clientX - bounds.left, y: event.clientY - bounds.top });
      pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
      pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const intersection = raycaster.intersectObject(model, false)[0];
      const faceIndex = intersection?.faceIndex;
      const faceId = faceIndex === undefined ? null : (mesh.triangle_face_ids ?? [])[faceIndex] ?? null;
      onFaceHoverRef.current?.(faceId);
    };
    const handlePointerLeave = () => {
      setHoverPoint(null);
      onFaceHoverRef.current?.(null);
    };
    renderer.domElement.addEventListener("pointermove", handlePointerMove);
    renderer.domElement.addEventListener("pointerleave", handlePointerLeave);

    const gridSize = Math.max(maxSize * 2.6, 100);
    const grid = new THREE.GridHelper(gridSize, 16, "#28617c", "#164661");
    grid.position.y = -size.y / 2;
    scene.add(grid);
    scene.add(new THREE.HemisphereLight("#d9f5ff", "#09253a", 2.1));
    const keyLight = new THREE.DirectionalLight("#ffffff", 2.6);
    keyLight.position.set(maxSize, maxSize * 2, maxSize * 1.4);
    scene.add(keyLight);
    const fillLight = new THREE.DirectionalLight("#54b7e7", 1.3);
    fillLight.position.set(-maxSize, maxSize * 0.6, -maxSize);
    scene.add(fillLight);

    const resize = () => {
      const width = viewer.clientWidth || 1;
      const height = viewer.clientHeight || 1;
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(viewer);
    resize();

    let frame;
    const render = () => {
      controls.update();
      renderer.render(scene, camera);
      frame = requestAnimationFrame(render);
    };
    render();

    return () => {
      cancelAnimationFrame(frame);
      renderer.domElement.removeEventListener("pointermove", handlePointerMove);
      renderer.domElement.removeEventListener("pointerleave", handlePointerLeave);
      observer.disconnect();
      controls.dispose();
      geometry.dispose();
      model.material.dispose();
      highlightMesh.geometry.dispose();
      highlightMesh.material.dispose();
      highlightMeshRef.current = null;
      edges.geometry.dispose();
      edges.material.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [mesh]);

  if (!mesh?.vertices?.length) {
    return <div className="viewer-empty"><Box size={28} /><strong>Upload a STEP, IGES, or Inventor model</strong><span>The 3D solid will appear here.</span></div>;
  }
  const viewerWidth = mountRef.current?.clientWidth ?? 0;
  const labelStyle = hoverPoint ? {
    left: Math.min(hoverPoint.x + 14, Math.max(8, viewerWidth - 300)),
    top: Math.max(8, hoverPoint.y - 44),
  } : { left: 12, top: 12 };
  return <div ref={mountRef} className="cad-viewer" aria-label="Interactive 3D CAD model viewer">
    <div ref={canvasHostRef} className="cad-canvas-host" />
    {hoverLabel && <div className="cad-hover-label" style={labelStyle}>{hoverLabel}</div>}
  </div>;
}

function StatCard({ icon: Icon, label, value, detail }) {
  return <div className="stat-card"><div className="stat-icon"><Icon size={16} /></div><div><span className="stat-label">{label}</span><strong>{value}</strong><small>{detail}</small></div></div>;
}

function ExportModal({ documentId, sourceFormat, nativeFormat, onClose }) {
  function downloadAs(format) {
    window.location.href = `${API_BASE}/api/documents/${documentId}/download?format=${format}`;
    onClose();
  }

  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="export-modal" role="dialog" aria-modal="true" aria-labelledby="export-title">
      <div className="modal-heading"><div><div className="section-eyebrow">Download edited model</div><h2 id="export-title">Choose export format</h2></div><button className="modal-close" onClick={onClose} aria-label="Close export dialog"><X size={17} /></button></div>
      <p>The current edited geometry will be downloaded in the selected CAD format.</p>
      <div className="export-options">{sourceFormat === "inventor" && <button onClick={() => downloadAs(nativeFormat)}><strong>{nativeFormat?.toUpperCase()}</strong><span>Native Autodesk Inventor {nativeFormat === "iam" ? "assembly" : "part"}</span><em>Preserves the Inventor feature history and parameters</em></button>}<button onClick={() => downloadAs("step")}><strong>STEP</strong><span>Neutral solid exchange format</span><em>{sourceFormat === "step" ? "Original format" : "Convert from Inventor or IGES"}</em></button><button onClick={() => downloadAs("iges")}><strong>IGES</strong><span>Surface and legacy CAD exchange format</span><em>{sourceFormat === "iges" ? "Original format" : "Convert from Inventor or STEP"}</em></button><button className="export-disabled" disabled><strong>TBD</strong><span>Additional CAD formats</span><em>Coming later</em></button></div>
      <button className="button subtle full modal-cancel" onClick={onClose}>Cancel</button>
    </div>
  </div>;
}

function ParameterPanel({ analysis, parameterValues, isBusy, onSubmit, onChange, onReset, hoveredParameterKey, onParameterHover }) {
  const editingAvailable = Boolean(analysis?.editing_available);
  const parameters = analysis?.parameters ?? [];
  const isComplex = analysis?.complexity === "complex";
  const isParametric = analysis?.mode === "parametric";
  const isNativeInventor = analysis?.mode === "native_parametric";
  const isSchemaProfile = analysis?.mode === "schema";
  const unitLabel = (parameter) => parameter.unit === "deg" ? "deg" : parameter.unit ?? analysis?.units?.symbol ?? "u";
  const hasEditableValues = parameters.filter((parameter) => parameter.editable).every((parameter) => {
    const value = Number(parameterValues[parameter.key]);
    return parameterValues[parameter.key] !== "" && Number.isFinite(value);
  });

  return <section className="side-section">
    <div className="section-heading"><div className="section-eyebrow">{isNativeInventor ? "Native Inventor parameters" : isParametric ? "Parametric hammer dimensions" : editingAvailable ? isComplex ? "Editable overall dimensions" : "Editable parameters" : "Detected measurements"}</div><Ruler size={15} /></div>
    {editingAvailable ? <form onSubmit={onSubmit} className="parameter-form">
      <div className="parameter-scroll" aria-label="Model parameters">
        {parameters.map((parameter) => <div key={parameter.key} className={`parameter-row ${hoveredParameterKey === parameter.key ? "is-hovered" : ""}`} onMouseEnter={() => onParameterHover?.(parameter.key)} onMouseLeave={() => onParameterHover?.(null)}>
          <label htmlFor={`parameter-${parameter.key}`}>{parameter.label}<span>{parameter.unit}</span></label>
          <div className="input-wrap"><input id={`parameter-${parameter.key}`} type="number" min={isNativeInventor || parameter.key === "angle" ? undefined : "0.000001"} step="any" value={parameterValues[parameter.key] ?? ""} onChange={(event) => onChange(parameter.key, event.target.value)} disabled={isBusy} readOnly={!parameter.editable} placeholder="Upload a model" /><span>{unitLabel(parameter)}</span></div>
        </div>)}
      </div>
      <p>{isNativeInventor ? "These values are read from the native Inventor parameter table. Changes rebuild the Inventor feature history, export a new STEP, and refresh the OCCT preview; the original .ipt remains unchanged." : isParametric ? "L3 is calculated as L1 - L2. L1 must be greater than L2; the original master file remains unchanged." : isSchemaProfile ? "These schema-driven controls use the named OCCT affine rebuild recipe for this model. AP242 PMI values remain source-only until feature mappings are defined." : isComplex ? "These controls scale and rotate the complete imported model. Named feature parameters require a designer-defined schema. The original uploaded file remains unchanged." : "These controls are generated for this simple model. The original uploaded file remains unchanged."}</p>
      <button className="button primary full" type="submit" disabled={!analysis || isBusy || !hasEditableValues}><Check size={16} /> {isBusy ? "Rebuilding model..." : "Update 3D model"}</button>
    </form> : analysis ? <div className="measurement-panel">
      <div className="complex-badge">Complex model · measurement only</div>
      <div className="detected-parameter-list">{parameters.map((parameter) => <div key={parameter.key}>
        <span>{parameter.label}</span>
        <strong>{formatNumber(parameter.value, 6)} <small>{unitLabel(parameter)}</small></strong>
        <em>Detected from model</em>
      </div>)}</div>
      <p>Generic inputs are hidden because this model is complex or surface-heavy. A designer-defined parameter schema is required before editing.</p>
    </div> : <div className="side-empty">Upload a model to detect its measurements.</div>}
    <button className="button subtle full" onClick={onReset} disabled={!analysis || isBusy}><RotateCcw size={15} /> Reset to production</button>
  </section>;
}

function DetectedFeatures({ analysis }) {
  if (!analysis) return null;
  const features = analysis.detected_features ?? [];
  return <section className="side-section detected-features-section">
    <div className="section-heading"><div className="section-eyebrow">Detected from this model</div><Layers3 size={15} /></div>
    {features.length ? <div className="feature-list">{features.map((feature) => <div key={feature.key} className="feature-row"><span>{feature.label}</span><strong>{formatNumber(feature.value, feature.unit === "faces" ? 0 : 6)} <small>{feature.unit}</small></strong><em>Detected</em></div>)}</div> : <div className="side-empty">No additional geometric feature measurements were confidently detected.</div>}
    <p className="feature-note">These values come from the imported OCCT topology. They become editable only when a model-specific parameter schema maps them to a safe feature operation.</p>
  </section>;
}

function PmiDimensions({ analysis }) {
  if (!analysis?.pmi_dimensions?.length) return null;
  const formatTolerance = (dimension) => {
    if (dimension.lower_bound !== null || dimension.upper_bound !== null) {
      return `limits ${formatNumber(dimension.lower_bound, 4)} to ${formatNumber(dimension.upper_bound, 4)}`;
    }
    if (dimension.lower_tolerance !== null || dimension.upper_tolerance !== null) {
      return `tol -${formatNumber(dimension.lower_tolerance ?? 0, 4)} / +${formatNumber(dimension.upper_tolerance ?? 0, 4)}`;
    }
    return "nominal value";
  };

  return <section className="side-section pmi-section">
    <div className="section-heading"><div className="section-eyebrow">AP242 PMI dimensions</div><Ruler size={15} /></div>
    <div className="pmi-list">{analysis.pmi_dimensions.map((dimension) => <div className="pmi-row" key={dimension.key}>
      <label htmlFor={`pmi-${dimension.key}`}>{dimension.label}<span>{dimension.type}</span></label>
      <div className="input-wrap"><input id={`pmi-${dimension.key}`} type="text" value={formatNumber(dimension.value, 6)} readOnly disabled /><span>{dimension.unit === "deg" ? "deg" : dimension.unit}</span></div>
      <em>{formatTolerance(dimension)} · Source PMI · read-only</em>
    </div>)}</div>
    <p className="feature-note">These values are read from the STEP AP242 semantic PMI carried by the uploaded file. A PMI value becomes editable only when a model-specific rebuild operation is mapped to the geometry; changing arbitrary B-Rep faces would not be production-safe.</p>
  </section>;
}

function App() {
  const inputRef = useRef(null);
  const [analysis, setAnalysis] = useState(null);
  const [documentId, setDocumentId] = useState(null);
  const [fileMeta, setFileMeta] = useState(null);
  const [parameterValues, setParameterValues] = useState({ length: 0, breadth: 0, height: 0, angle: 0 });
  const [isBusy, setIsBusy] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [isExportOpen, setIsExportOpen] = useState(false);
  const [hoveredParameterKey, setHoveredParameterKey] = useState(null);
  const [hoveredFaceId, setHoveredFaceId] = useState(null);
  const [hoverSource, setHoverSource] = useState(null);

  async function readResponse(response) {
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail ?? "The CAD API returned an error.");
    return payload;
  }

  function syncParameters(payload) {
    const nextValues = {};
    (payload.parameters ?? []).forEach((parameter) => {
      nextValues[parameter.key] = inputValue(parameter.value, parameter.unit === "deg" ? 1 : 6);
    });
    setParameterValues(nextValues);
  }

  async function uploadFile(file) {
    if (!file) return;
    const extension = file.name.toLowerCase().split(".").pop();
    if (!ACCEPTED_EXTENSIONS.includes(extension)) {
      setError(`${file.name} is not supported. Please use STEP, IGES, or an Inventor .ipt/.iam file.`);
      setNotice("");
      return;
    }
    setIsBusy(true);
    setError("");
    setNotice(["ipt", "iam"].includes(extension) ? "Opening native Inventor model and generating OCCT preview… this may take up to a minute" : "Importing model…");
    try {
      const form = new FormData();
      form.append("file", file);
      const payload = await readResponse(await fetch(`${API_BASE}/api/documents`, { method: "POST", body: form }));
      setDocumentId(payload.document_id);
      setAnalysis(payload);
      syncParameters(payload);
      setHoveredParameterKey(null);
      setHoveredFaceId(null);
      setHoverSource(null);
      setFileMeta({ name: file.name, size: file.size });
      setNotice(payload.source_format === "inventor" ? "Inventor model loaded through Inventor + OCCT" : `${payload.source_format.toUpperCase()} model loaded through OCCT`);
    } catch (uploadError) {
      setError(uploadError.message);
    } finally {
      setIsBusy(false);
    }
  }

  function handleDragOver(event) {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setIsDragging(true);
  }

  function handleDragLeave(event) {
    if (!event.currentTarget.contains(event.relatedTarget)) setIsDragging(false);
  }

  function handleDrop(event) {
    event.preventDefault();
    setIsDragging(false);
    uploadFile(event.dataTransfer.files?.[0]);
  }

  async function applyParameters(event) {
    event.preventDefault();
    if (!documentId || !analysis) return;
    setIsBusy(true);
    setError("");
    setNotice("");
    try {
      const payload = await readResponse(await fetch(`${API_BASE}/api/documents/${documentId}/parameters`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(["parametric", "native_parametric"].includes(analysis.mode)
          ? { values: Object.fromEntries(analysis.parameters.filter((parameter) => parameter.editable).map((parameter) => [parameter.key, Number(parameterValues[parameter.key])])) }
          : {
            length: Number(parameterValues.length),
            breadth: Number(parameterValues.breadth),
            height: Number(parameterValues.height),
            angle: Number(parameterValues.angle || 0),
          }),
      }));
      setAnalysis(payload);
      syncParameters(payload);
      setHoveredParameterKey(null);
      setHoveredFaceId(null);
      setHoverSource(null);
      setNotice("3D model updated");
    } catch (updateError) {
      setError(updateError.message);
    } finally {
      setIsBusy(false);
    }
  }

  async function resetDocument() {
    if (!documentId) return;
    setIsBusy(true);
    setError("");
    try {
      const payload = await readResponse(await fetch(`${API_BASE}/api/documents/${documentId}/reset`, { method: "POST" }));
      setAnalysis(payload);
      syncParameters(payload);
      setHoveredParameterKey(null);
      setHoveredFaceId(null);
      setHoverSource(null);
      setNotice("Original model restored");
    } catch (resetError) {
      setError(resetError.message);
    } finally {
      setIsBusy(false);
    }
  }

  function clearDocument() {
    setAnalysis(null);
    setDocumentId(null);
    setFileMeta(null);
    setParameterValues({ length: 0, breadth: 0, height: 0, angle: 0 });
    setError("");
    setNotice("");
    setHoveredParameterKey(null);
    setHoveredFaceId(null);
    setHoverSource(null);
    if (inputRef.current) inputRef.current.value = "";
  }

  const hoveredParameter = analysis?.parameters?.find((parameter) => parameter.key === hoveredParameterKey);
  const highlightedFaceIds = hoverSource === "parameter" && hoveredParameter?.preview_face_ids?.length
    ? hoveredParameter.preview_face_ids
    : hoverSource === "face" && hoveredFaceId !== null ? [hoveredFaceId] : [];
  const hoveredFaceParameters = hoveredFaceId === null ? [] : (analysis?.parameters ?? []).filter((parameter) => parameter.preview_face_ids?.includes(hoveredFaceId));
  const hoveredFaceLabels = [...new Set(hoveredFaceParameters.map((parameter) => parameter.label).filter(Boolean))];
  const hoveredFeatureNames = [...new Set(hoveredFaceLabels.map((label) => String(label).split("·")[0].replace(/Â$/, "").trim()))].filter(Boolean);
  const modelHoverLabel = hoveredFaceLabels.length === 1
    ? hoveredFaceLabels[0]
    : hoveredFeatureNames.join(" · ");
  const hoverLabel = hoverSource === "parameter"
    ? hoveredParameter?.label ?? ""
    : hoverSource === "face"
      ? modelHoverLabel || `Face ${hoveredFaceId}`
      : "";
  function handleParameterHover(parameterKey) {
    setHoveredParameterKey(parameterKey);
    setHoveredFaceId(null);
    setHoverSource(parameterKey === null ? null : "parameter");
  }
  function handleFaceHover(faceId) {
    setHoveredFaceId(faceId);
    if (faceId === null) {
      setHoveredParameterKey(null);
      setHoverSource(null);
      return;
    }
    const linkedParameter = analysis?.parameters?.find((parameter) => parameter.preview_face_ids?.includes(faceId));
    setHoveredParameterKey(linkedParameter?.key ?? null);
    setHoverSource("face");
  }

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-lockup"><div className="brand-mark"><Maximize2 size={17} /></div><div><strong>Open CAD Engine</strong><span>STEP / IGES / Inventor 3D parametric workspace</span></div></div>
      <div className="topbar-actions"><span className="engine-status"><i /> OCCT engine online</span><button className="button ghost" onClick={() => inputRef.current?.click()}><FilePlus2 size={16} /> New model</button></div>
    </header>

    <main className="workspace">
      <aside className="sidebar">
        <section className="side-section project-section">
          <div className="section-eyebrow">Project</div>
          <input ref={inputRef} type="file" accept=".step,.stp,.iges,.igs,.ipt,.iam" onChange={(event) => uploadFile(event.target.files?.[0])} hidden />
          <button className={`dropzone ${analysis ? "has-file" : ""} ${isDragging ? "is-dragging" : ""}`} onClick={() => inputRef.current?.click()} onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={handleDrop} aria-label="Upload or drop a STEP, IGES, or Inventor IPT or IAM file">
            <div className="upload-icon"><Upload size={18} /></div><strong>{isDragging ? "Release to upload" : analysis ? "Replace model" : "Upload a 3D model"}</strong><span>{analysis ? fileMeta?.name : "Drag a STEP, IGES, .ipt, or .iam file here"}</span>
          </button>
          {fileMeta && <div className="file-chip"><div className="file-chip-icon">{["ipt", "iam"].includes(fileMeta.name.toLowerCase().split(".").pop()) ? fileMeta.name.toLowerCase().split(".").pop().toUpperCase() : fileMeta.name.toLowerCase().endsWith(".iges") || fileMeta.name.toLowerCase().endsWith(".igs") ? "IGES" : "STEP"}</div><div><strong>{fileMeta.name}</strong><span>{formatBytes(fileMeta.size)} · {["ipt", "iam"].includes(fileMeta.name.toLowerCase().split(".").pop()) ? "Inventor + OCCT" : "OCCT"}</span></div><button aria-label="Remove model" onClick={clearDocument}><X size={14} /></button></div>}
        </section>

        <ParameterPanel analysis={analysis} parameterValues={parameterValues} isBusy={isBusy} onSubmit={applyParameters} onChange={(key, value) => setParameterValues((current) => ({ ...current, [key]: value }))} onReset={resetDocument} hoveredParameterKey={hoveredParameterKey} onParameterHover={handleParameterHover} />
        <DetectedFeatures analysis={analysis} />
        <PmiDimensions analysis={analysis} />
        {false && <section className="side-section">
          <div className="section-heading"><div className="section-eyebrow">Editable parameters</div><Ruler size={15} /></div>
          <form onSubmit={applyParameters} className="parameter-form">
            {[["length", "Length"], ["breadth", "Breadth"], ["height", "Height"]].map(([key, label]) => <div key={key}><label htmlFor={`parameter-${key}`}>{label}<span>model units</span></label><div className="input-wrap"><input id={`parameter-${key}`} type="number" min="0.000001" step="any" value={parameterValues[key] || ""} onChange={(event) => setParameterValues((current) => ({ ...current, [key]: event.target.value }))} disabled={!analysis || isBusy} placeholder="Upload a model" /><span>u</span></div></div>)}
            <label htmlFor="parameter-angle">Z angle <span>degrees</span></label><div className="input-wrap"><input id="parameter-angle" type="number" step="any" value={parameterValues.angle ?? ""} onChange={(event) => setParameterValues((current) => ({ ...current, angle: event.target.value }))} disabled={!analysis || isBusy} placeholder="0" /><span>°</span></div>
            <p>Free-form PoC mode scales the complete solid. The original uploaded file remains unchanged.</p>
            <button className="button primary full" type="submit" disabled={!analysis || isBusy || !parameterValues.length || !parameterValues.breadth || !parameterValues.height}><Check size={16} /> {isBusy ? "Rebuilding model..." : "Update 3D model"}</button>
          </form>
          <button className="button subtle full" onClick={resetDocument} disabled={!analysis || isBusy}><RotateCcw size={15} /> Reset to production</button>
        </section>}

        <section className="side-section">
          <div className="section-heading"><div className="section-eyebrow">Editing mode</div><Layers3 size={15} /></div>
          <div className="mode-card"><strong>{analysis?.mode === "native_parametric" ? "Native Inventor" : analysis?.mode === "parametric" ? "Parametric" : analysis?.editing_available ? "Free-form" : analysis ? "Measurement-only" : "Waiting for model"}</strong><span>{analysis?.complexity_reason ?? "The available controls will be based on the uploaded model."}</span><em>{analysis?.mode === "native_parametric" ? "Inventor feature rebuild active; OCCT preview enabled" : analysis?.mode === "parametric" ? "Constraints active: L3 = L1 - L2" : analysis?.editing_available ? analysis.complexity === "complex" ? "Whole-model editing is active; named feature schemas are the next layer" : "Named parametric schemas are the next layer" : analysis ? "Configure named parameters for this model to enable editing" : "Upload STEP, IGES, IPT, or IAM to begin"}</em></div>
        </section>
        <div className="sidebar-footer"><span>Open-source 3D PoC</span><span>v0.2.0</span></div>
      </aside>

      <section className="main-panel">
        <div className="content-heading"><div><div className="section-eyebrow">Workspace / {analysis ? "3D model review" : "Getting started"}</div><h1>{analysis ? fileMeta?.name : "Build from a real 3D model"}</h1><p>{analysis ? "Inspect the imported solid, orbit the view, and edit its dimensions." : "An open-source STEP / IGES / Inventor foundation for interactive CAD editing."}</p></div><div className="heading-actions">{notice && <span className="notice"><Check size={14} /> {notice}</span>}{error && <span className="error-message">{error}</span>}{analysis && <button className="button primary" onClick={() => setIsExportOpen(true)}><Download size={16} /> Export</button>}</div></div>

        <div className="stats-grid"><StatCard icon={Box} label="Solids" value={analysis ? formatNumber(analysis.solid_count, 0) : "—"} detail="B-Rep bodies" /><StatCard icon={Layers3} label="Faces" value={analysis ? formatNumber(analysis.face_count, 0) : "—"} detail="topology" /><StatCard icon={Maximize2} label="Length" value={analysis ? `${formatNumber(analysis.length)} ${analysis.units?.symbol ?? "u"}` : "—"} detail="editable axis" /><StatCard icon={Ruler} label="Breadth" value={analysis ? `${formatNumber(analysis.breadth)} ${analysis.units?.symbol ?? "u"}` : "—"} detail="editable axis" /><StatCard icon={Box} label="Height" value={analysis ? `${formatNumber(analysis.height)} ${analysis.units?.symbol ?? "u"}` : "—"} detail="editable axis" /></div>

        <div className="model-card"><div className="card-toolbar"><div><strong>3D model preview</strong><span>{analysis ? `${formatNumber(analysis.mesh.triangle_count, 0)} triangles · orbit / zoom enabled` : "Waiting for a STEP, IGES, or Inventor model"}</span></div>{isBusy && <div className="preview-save-status" role="status" aria-live="polite"><span className="parameter-save-spinner" aria-hidden="true" /> Saving changes to the model...</div>}<span className="view-badge">OCCT mesh</span></div><CadViewer mesh={analysis?.mesh} highlightedFaceIds={highlightedFaceIds} onFaceHover={handleFaceHover} hoverLabel={hoverLabel} /></div>

        <div className="lower-grid"><section className="data-card"><div className="card-toolbar"><div><strong>Model measurements</strong><span>Values extracted from the OCCT model · {analysis?.units?.name ?? "unit not declared"}</span></div></div>{analysis ? <div className="dimension-list">{analysis.parameters.map((parameter) => <div key={parameter.key}><span>{parameter.label}</span><strong>{formatNumber(parameter.value, 6)} <small>{parameter.unit === "deg" ? "deg" : parameter.unit}</small></strong><em className={parameter.editable ? "editable-tag" : "detected-tag"}>{parameter.editable ? "Editable" : "Detected"}</em></div>)}<div><span>Edges / vertices</span><strong>{formatNumber(analysis.edge_count, 0)} / {formatNumber(analysis.vertex_count, 0)}</strong><em className="detected-tag">Detected</em></div></div> : <div className="card-empty">Measurements will populate after upload.</div>}</section><section className="data-card"><div className="card-toolbar"><div><strong>Engine status</strong><span>Current 3D PoC boundaries</span></div></div><div className="engine-notes"><div><Check size={15} /><span>STEP, IGES, and Inventor models imported through OCCT</span></div><div><Check size={15} /><span>Original production file is immutable</span></div><div><Check size={15} /><span>Edited geometry exports back to source format</span></div><div><Check size={15} /><span>{analysis?.valid ? "Shape validation passed" : "Upload a model to validate"}</span></div></div></section></div>
      </section>
      {isExportOpen && <ExportModal documentId={documentId} sourceFormat={analysis?.source_format} nativeFormat={analysis?.native_format} onClose={() => setIsExportOpen(false)} />}
    </main>
  </div>;
}

export default App;
