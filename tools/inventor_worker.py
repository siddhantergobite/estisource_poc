"""Command-line worker boundary for native Inventor jobs.

The API can run this in a dedicated Windows worker process so COM automation
does not share the FastAPI process or a user's interactive Inventor session.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cad_engine.inventor_adapter import InventorAdapter


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an Autodesk Inventor native CAD job")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover")
    discover.add_argument("source", type=Path)
    discover.add_argument("output", type=Path)

    rebuild = subparsers.add_parser("rebuild")
    rebuild.add_argument("source", type=Path)
    rebuild.add_argument("output_step", type=Path)
    rebuild.add_argument("--updates", required=True, help="JSON object, e.g. {\"d78\": \"0.650 in\"}")
    rebuild.add_argument("--catalog-output", type=Path)

    args = parser.parse_args()
    with InventorAdapter(visible=False) as adapter:
        if args.command == "discover":
            payload = {"source": str(args.source), "parameters": [item.to_dict() for item in adapter.discover(args.source)]}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return

        updates = json.loads(args.updates)
        updated = adapter.rebuild_to_step(args.source, args.output_step, updates)
        if args.catalog_output:
            args.catalog_output.parent.mkdir(parents=True, exist_ok=True)
            args.catalog_output.write_text(
                json.dumps({"source": str(args.source), "parameters": [item.to_dict() for item in updated]}, indent=2),
                encoding="utf-8",
            )
        print(json.dumps({"output_step": str(args.output_step), "parameters": len(updated)}))


if __name__ == "__main__":
    main()

