# clio-jarvis

JARVIS packages for the clio relay. Each package carries a contract (RL40) that
relay reads before it runs anything, so relay never has to guess a package's
parameters, resources, outputs, progress, ports or provenance.

clio-jarvis layers on top of [jarvis-cd](https://github.com/grc-iit/jarvis-cd)
1.8.1 and does not change it.

| Package | Kind | What it does |
|---|---|---|
| `clio_jarvis.gray_scott` | application | ADIOS2 Gray-Scott reaction-diffusion. Writes U and V to a growing BP5 output. |
| `clio_jarvis.xcompact3d` | application | Xcompact3d Taylor-Green vortex at Re 1600 (DNS). Writes ux, uy, uz, pp, vort and critq to a growing BP5 output. |
| `clio_jarvis.pvserver` | service | A ParaView 6 `pvserver` bound to 127.0.0.1, with a ready record and a stop record. |

## Install

```bash
git clone https://github.com/iowarp/clio-jarvis
pip install -e clio-jarvis[test]
jarvis repo add $PWD/clio-jarvis/clio_jarvis
```

The packages read `schemas/` from the checkout, so install from a checkout.

## The contract

Each package directory holds `pkg.py` (the JARVIS class), `contract.yaml`,
`progress.py` and `artifacts.py` (the jarvis-cd providers) and, when it needs
more than the generic checks, `selftest.py`. `contract.yaml` follows
`schemas/package-contract.v1.json` and has these parts:

| Part | Holds |
|---|---|
| `parameters` | Limits on the `pkg.py` menu, the fields to show for review, and fill rules such as `run_dir: "{shared_dir}/run"`. The menu stays the single list of parameters. |
| `resources` | PSI/J process count, processes per node, duration and queue, as parameter references. |
| `outputs` | One entry per output: path, format, reader, expected steps and display. Each becomes a view descriptor. |
| `progress` | Stdout patterns for steps, completion and readiness. `announces: before-write` means the step line comes before that step is readable. |
| `service` | Services only: ports, health check, protection, stop signal and stop record, maximum lifetime. |
| `provenance` | Input files hashed into `clio-contract.json`, and the upstream code. |
| `selftest` | Parameters and time limit for the self-test, and an optional extra check script. |

A package missing any part is refused, and the error names the part:

```
package 'gray_scott' refused: contract part 'outputs' is missing
```

### View descriptors

Every output yields a document that follows `schemas/view-descriptor.v1.json`
(`clio.view-descriptor.v1`). The ParaView view uses it to open and follow the
output:

```json
{
  "schema": "clio.view-descriptor.v1",
  "name": "snapshots",
  "path": "<shared_dir>/run/data.bp5",
  "path_kind": "directory",
  "format": "adios2-bp5",
  "reader": "ADIOS2VTXReader",
  "reader_properties": {},
  "growing": true,
  "follow": {"signal": "reader-time-steps", "poll_seconds": 2, "expected_steps": 120},
  "display": {"array": "vort", "association": "POINTS", "representation": "Slice"},
  "producer": {"package": "clio_jarvis.xcompact3d", "version": "0.1.0", "sha256": "..."}
}
```

While the run goes, each progress event carries metadata `clio`
(`clio.step.v1`) with `step`, `visible_steps`, `expected_steps` and the view
descriptor. When the run ends, the last event has `growing: false`. Services
send one `ready` event (`clio.service-ready.v1`) with the port they chose.

At configure time each package writes `clio-contract.json` into its shared
directory, holding the resolved parameters, resources, view descriptors, input
digests and the package's sha256.

## Command line

```
python -m clio_jarvis validate [PACKAGE ...]
python -m clio_jarvis describe PACKAGE [--set name=value ...]
python -m clio_jarvis selftest PACKAGE [--set name=value ...] [--work DIR] [--pv-env SCRIPT]
```

- `validate` loads every contract and exits nonzero if any package is refused.
- `describe` prints the document relay reads before a run: parameter schema,
  resources, view descriptors and service ports.
- `selftest` runs the package through jarvis-cd (`jarvis ppl run`) in a
  private `JARVIS_ROOT`, with the contract's `selftest.params` plus any
  `--set`. It writes `selftest-report.json` in the work directory and exits 0
  only if every check passes.

The self-test checks:

- `clio-contract.json` matches the package hash and holds input digests.
- Applications: progress steps 1 to N in order, with valid growing view
  descriptors; a last `completed` event with `growing: false`; a finalized
  artifact. With `--pv-env`, pvpython opens the output with the descriptor's
  reader and checks the step count, arrays, time and a changing field, then
  saves a screenshot.
- Services: one `ready` event with a port, and a SIGTERM stop that writes the
  stop record.
- The package's own `selftest.py`. For pvserver, two pvpython clients in
  turn connect, render a sphere on the server and disconnect; after each one
  the supervisor must have a new pvserver listening on the same port.

Run the unit tests with `pytest`.

## Packages

### gray_scott

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs`, `ppn` | 4, 4 | MPI ranks and ranks per node |
| `L` | 128 | Grid cells per side |
| `steps`, `plotgap` | 15000, 150 | Simulation steps, and steps per output step (100 output steps) |
| `run_dir` | `{shared_dir}/run` | Output goes to `{run_dir}/gs.bp` |

On 4 Ares ranks the defaults write an output step about every 2.7 s, for
about 4.5 minutes. Needs `adios2_simulations_gray-scott` built with
`gray_scott/build/clio_vtx_patch.py`, which adds the `vtk.xml` attribute and
the `time` variable.

### xcompact3d

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs`, `ppn` | 4, 4 | MPI ranks and ranks per node |
| `n` | 65 | Grid points per side (33, 65, 129 or 257) |
| `ilast`, `ioutput`, `dt` | 2400, 20, 0.005 | Iterations, iterations per snapshot, time step (120 snapshots up to t = 12) |
| `re` | 1600 | Reynolds number |
| `run_dir` | `{shared_dir}/run` | Output goes to `{run_dir}/data.bp5` |

The case is the Taylor-Green vortex DNS at Re 1600 from the Incompact3d
examples (`config/input_DNS_Re1600.i3d`, BSD-3, see `NOTICE`). Its vortex sheets
roll up and break into turbulence by about t = 9, so the field changes visibly
from snapshot to snapshot. On 4 Ares ranks the defaults write a snapshot about
every 3.2 s, for about 6.4 minutes. Needs Xcompact3d with 2DECOMP&FFT 2.0.4
and ADIOS2, patched with `xcompact3d/build/clio_vtx_patch.py`.

### pvserver

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs` | 1 | pvserver ranks (`mpiexec -launcher fork` when above 1) |
| `port` | 0 | TCP port; 0 picks a free one, reported in the ready event |
| `connect_id` | 0 | ParaView connect id the client must give; 0 for none |
| `persist` | true | Start a new pvserver on the same port when a client disconnects. A ParaView 6 pvserver serves one client session, then exits (`--multi-clients` is deprecated). |
| `env_script` | empty | Script sourced before `pvserver`, such as Kitware's `pv-env.sh` |
| `max_lifetime` | 14400 | Seconds before the server stops itself |

`pvserver` binds to 127.0.0.1 only. Reach it over an SSH tunnel. The
supervisor writes `pvserver-ready.json` when the port is listening and
`pvserver-stop.json` when it stops (`clio.service-stop.v1`, with the reason).

## License

BSD-3-Clause. Xcompact3d inputs are from Incompact3d (BSD-3), see
`clio_jarvis/xcompact3d/NOTICE`.
