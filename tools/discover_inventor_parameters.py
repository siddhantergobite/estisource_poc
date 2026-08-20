"""Generate a native Inventor parameter catalog for a model family."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cad_engine.inventor_adapter import InventorAdapter


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with InventorAdapter(reuse_active=True) as adapter:
        parameters = [item.to_dict() for item in adapter.discover(args.source)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"source": str(args.source), "parameters": parameters}, indent=2), encoding="utf-8")
    print(f"Wrote {len(parameters)} parameters to {args.output}")


if __name__ == "__main__":
    main()

