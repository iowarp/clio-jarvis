#!/usr/bin/env python3
"""Give ADIOS2 v2.12.1's Gray-Scott example a double ``time`` for ParaView.

Upstream defines ``vtk.xml`` (ImageData, CellData U and V) with TIME pointing
at the int variable ``step``. clio-jarvis follows one VTX contract for every
solver: TIME is a double ``time`` in model units, here ``step * dt``. This
local patch adds that variable and points TIME at it. Nothing else changes.

Usage: ``clio_vtx_patch.py <gray-scott-src>`` (the examples/simulations/gray-scott
directory). Idempotent.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARK = "clio-jarvis VTX patch"

EDITS = {
    "simulation/writer.h": [
        (
            "    adios2::Variable<int> var_step;\n",
            "    adios2::Variable<int> var_step;\n"
            f"    adios2::Variable<double> var_time; // {MARK}\n",
            1,
        ),
    ],
    "simulation/writer.cpp": [
        (
            '<DataArray Name="TIME">\n                    step\n                  </DataArray>',
            '<DataArray Name="TIME">time</DataArray>',
            1,
        ),
        (
            '    var_step = io.DefineVariable<int>("step");\n',
            '    var_step = io.DefineVariable<int>("step");\n'
            f'    var_time = io.DefineVariable<double>("time"); // {MARK}\n',
            1,
        ),
        (
            "writer.Put<int>(var_step, &step);\n",
            "writer.Put<int>(var_step, &step);\n"
            "        writer.Put<double>(var_time, step * settings.dt, adios2::Mode::Sync);\n",
            3,
        ),
    ],
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = Path(argv[1])
    for rel, edits in EDITS.items():
        path = src / rel
        text = path.read_text()
        if MARK in text:
            print(f"{path}: already patched")
            continue
        for old, new, count in edits:
            found = text.count(old)
            if found != count:
                raise SystemExit(f"{path}: expected {count} of {old[:50]!r}, found {found}")
            text = text.replace(old, new)
        path.write_text(text)
        print(f"{path}: patched")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
