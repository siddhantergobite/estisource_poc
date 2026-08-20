"""Small, isolated Autodesk Inventor automation adapter.

The neutral OCCT path remains the default for STEP/IGES.  This module is used
only when a native Inventor part is supplied.  It deliberately works on a
working copy so a user's original CAD source is never overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
            return self._parameter_records(self._parameters(document))
        finally:
            document.Close(False)

    @staticmethod
    def _parameter_records(parameters) -> list[InventorParameter]:
        result: list[InventorParameter] = []
        for index in range(1, parameters.Count + 1):
            parameter = parameters.Item(index)
            expression = str(parameter.Expression)
            references = set(PARAMETER_REFERENCE_RE.findall(expression)) - {str(parameter.Name)}
            result.append(
                InventorParameter(
                    name=str(parameter.Name),
                        expression=expression,
                        value=float(parameter.Value),
                        display_value=_display_value(float(parameter.Value), str(parameter.Units)),
                        units=str(parameter.Units),
                    comment=str(parameter.Comment or ""),
                    editable=not references,
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
            document.Update()
            destination.parent.mkdir(parents=True, exist_ok=True)
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
            return self._parameter_records(parameters)
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
                    result["value"] = adapter._parameter_records(parameters)
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
                    document.Update()
                    self._export_step(adapter, document, Path(output_step))
                    result["value"] = adapter._parameter_records(parameters)
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
