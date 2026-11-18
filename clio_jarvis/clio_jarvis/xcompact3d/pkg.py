"""Xcompact3d (Incompact3d v5.0) as a clio-jarvis package (RL40).

The default case is the Taylor-Green vortex at Re 1600 from the Incompact3d
examples. The run directory gets ``input.i3d``, ``adios2_config.xml`` (BP5) and
``clio-vtk.xml``. The clio VTX patch in ``build/clio_vtx_patch.py`` makes the
solver define ``vtk.xml`` from that file and put the simulation time in every
snapshot, so ParaView's ADIOS2VTXReader follows ``data.bp5`` while it grows.
"""

from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from clio_jarvis._contract.base import ClioApplication

CONFIG_DIR = Path(__file__).resolve().parent / "config"

#: Point fields the TGV case writes in each snapshot.
TGV_ARRAYS = ("ux", "uy", "uz", "pp", "vort", "critq")


def set_i3d(text: str, name: str, value: Any) -> str:
    """Set ``name = value`` in an i3d namelist and keep the line's comment."""
    pattern = re.compile(rf"^(\s*{re.escape(name)}\s*=\s*)([^!\n]*?)(\s*(?:!.*)?)$", re.MULTILINE)
    text, count = pattern.subn(lambda m: f"{m.group(1)}{value}{m.group(3)}", text, count=1)
    if count != 1:
        raise ValueError(f"input template has no {name!r} entry")
    return text


def i3d_value(text: str, name: str) -> str:
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*([^!\n]+)", text, re.MULTILINE)
    if match is None:
        raise ValueError(f"input template has no {name!r} entry")
    return match.group(1).strip()


def vtk_xml(n: tuple[int, int, int], spacing: tuple[float, float, float], arrays: tuple[str, ...]) -> str:
    """The ``vtk.xml`` attribute: point data on an image grid, x fastest."""
    extent = " ".join(f"0 {count - 1}" for count in n)
    data_arrays = "\n".join(f'        <DataArray Name="{name}" />' for name in arrays)
    return (
        '<?xml version="1.0"?>\n'
        '<VTKFile type="ImageData" version="0.1" byte_order="LittleEndian">\n'
        f'  <ImageData WholeExtent="{extent}" Origin="0 0 0" '
        f'Spacing="{spacing[0]!r} {spacing[1]!r} {spacing[2]!r}">\n'
        f'    <Piece Extent="{extent}">\n'
        f'      <PointData Scalars="{arrays[0]}">\n'
        f"{data_arrays}\n"
        '        <DataArray Name="TIME">time</DataArray>\n'
        "      </PointData>\n"
        "    </Piece>\n"
        "  </ImageData>\n"
        "</VTKFile>\n"
    )


class Xcompact3d(ClioApplication):
    """Incompressible Navier-Stokes DNS with a growing BP5 snapshot series."""

    clio_program = "xcompact3d"
    clio_probe_arguments = None
    clio_capabilities = ("mpi_execution", "adios2_bp5", "xcompact3d")

    def _configure_menu(self) -> list[dict[str, Any]]:
        return [
            {"name": "nprocs", "msg": "MPI processes", "type": int, "default": 4},
            {"name": "ppn", "msg": "MPI processes per node", "type": int, "default": 4},
            {
                "name": "n",
                "msg": "Grid points per side (nx = ny = nz)",
                "type": int,
                "default": 65,
                "choices": [33, 65, 129, 257],
            },
            {"name": "ilast", "msg": "Last time step", "type": int, "default": 2400},
            {"name": "ioutput", "msg": "Time steps between two snapshots", "type": int, "default": 20},
            {"name": "dt", "msg": "Time step", "type": float, "default": 0.005},
            {"name": "re", "msg": "Reynolds number", "type": float, "default": 1600.0},
            {
                "name": "input_template",
                "msg": "Incompact3d input file to start from; empty means the TGV Re1600 example",
                "type": str,
                "default": "",
            },
            {
                "name": "run_dir",
                "msg": "Run directory; empty means {shared_dir}/run",
                "type": str,
                "default": "",
            },
            {
                "name": "executable",
                "msg": "Xcompact3d program, by name on PATH or by absolute path",
                "type": str,
                "default": "xcompact3d",
            },
            {"name": "walltime", "msg": "Wall-time limit in seconds; absent means no limit", "type": int, "default": None, "required": False},
            {"name": "queue", "msg": "Batch queue; empty means the site default", "type": str, "default": ""},
        ]

    def _clio_prepare(self, config: dict[str, Any]) -> None:
        run_dir = Path(config["run_dir"])
        (run_dir / "data").mkdir(parents=True, exist_ok=True)
        text = Path(config["input_template"]).read_text(encoding="utf-8")
        n = int(config["n"])
        for name, value in (
            ("nx", n),
            ("ny", n),
            ("nz", n),
            ("ilast", int(config["ilast"])),
            ("ioutput", int(config["ioutput"])),
            # No restart files: a snapshot series is the only output.
            ("icheckpoint", 1000000),
            ("dt", repr(float(config["dt"]))),
            ("re", repr(float(config["re"]))),
        ):
            text = set_i3d(text, name, value)
        (run_dir / "input.i3d").write_text(text, encoding="utf-8")
        shutil.copyfile(CONFIG_DIR / "adios2_config.xml", run_dir / "adios2_config.xml")

        spacing = []
        for length, ncl in (("xlx", "nclx1"), ("yly", "ncly1"), ("zlz", "nclz1")):
            size = float(i3d_value(text, length))
            periodic = int(float(i3d_value(text, ncl))) == 0
            spacing.append(size / n if periodic else size / (n - 1))
        (run_dir / "clio-vtk.xml").write_text(
            vtk_xml((n, n, n), (spacing[0], spacing[1], spacing[2]), TGV_ARRAYS), encoding="utf-8"
        )

    def _output(self) -> Path:
        return Path(self.config["run_dir"]) / "data.bp5"

    def start(self) -> None:
        self._clio_ensure_configured(prepare=True)
        run_dir = Path(self.config["run_dir"])
        # A rerun starts a fresh series; a viewer must not see the last run's steps.
        shutil.rmtree(self._output(), ignore_errors=True)
        command = f"{shlex.quote(self.clio_executable())} input.i3d"
        self._clio_mpi(command, cwd=str(run_dir))

    def stop(self) -> None:
        """Xcompact3d runs to its last time step."""

    def clean(self) -> None:
        self._clio_ensure_configured(prepare=False)
        shutil.rmtree(self._output(), ignore_errors=True)
