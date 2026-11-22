# clio-jarvis

JARVIS packages for the clio relay. Each package carries a contract (RL40) that
relay reads before it runs anything, so relay never has to guess a package's
parameters, resources, outputs, progress, ports or provenance.

clio-jarvis layers on top of [jarvis-cd](https://github.com/grc-iit/jarvis-cd)
and does not change it. Its packages install through jarvis-cd's Spack
installer (`install_method: spack`), so they need a jarvis-cd with that
installer and `jarvis ppl install`: jarvis-cd 4007fd14 or newer (branch
`jaimecernuda-execution-lifecycle`, after 1.8.1). No jarvis-cd release has it
yet, so `pyproject.toml` can only say `jarvis-cd>=1.8.1`; pvserver checks for
`ServiceProtocol.TCP` when it starts and stops with an error on an older one.

| Package | Kind | What it does |
|---|---|---|
| `clio_jarvis.gray_scott` | application | ADIOS2 Gray-Scott reaction-diffusion. Writes U and V to a growing BP5 output. |
| `clio_jarvis.xcompact3d` | application | Xcompact3d flow past any STL body (immersed boundary), or the Taylor-Green vortex at Re 1600 without one. Writes ux, uy, uz, pp, vort, critq (and ep, the body) to a growing BP5 output. |
| `clio_jarvis.pvserver` | service | A ParaView 5.13.1 `pvserver` bound to 127.0.0.1, with a ready record and a stop record. |

## Install

```bash
git clone https://github.com/iowarp/clio-jarvis
pip install -e clio-jarvis[test]
jarvis repo add $PWD/clio-jarvis/clio_jarvis
```

The packages read `schemas/` and `spack/` from the checkout, so install from a
checkout.

### Software

Every package declares its software as a Spack spec in its contract's
`install` part, and its configure menu defaults to that spec:

| Package | `install_query` | `install_repos` |
|---|---|---|
| `gray_scott` | `gray-scott@2.10.2 +clio ^adios2+mpi` | `../../../spack` (the repository's `spack/`) |
| `xcompact3d` | `xcompact3d@5.0 +clio ^decomp2d-fft io=adios2 ^adios2+mpi+fortran` | `../../../spack` (the repository's `spack/`) |
| `pvserver` | `paraview@5.13.1 +mpi +adios2 +python ~qt ^[virtuals=gl] osmesa` | none (Spack's builtin recipe) |

```bash
jarvis ppl install +check   # what is already there, what would be built
jarvis ppl install          # build what is missing into the pipeline's Spack environment
```

jarvis-cd finds each spec first and builds only what is missing, in a Spack
environment per pipeline with its `spack.lock`. A user Spack that has the
site's Spack database as an upstream, or the site's modules as externals,
reuses what the site already has, such as its MPI, ADIOS2 and Mesa, and builds
the rest, ParaView included. MPI is always the `mpi` virtual and never pinned:
when more than one MPI fits, `jarvis ppl install` asks which one, and the
answer reaches the launcher through `JARVIS_MPI_*`. The run then finds the
programs on the PATH the Spack environment gives it. There is no other install
path: no build scripts, no `executable` paths and no environment scripts.

`install_repos` entries are relative to the package directory (the one holding
`pkg.py`). jarvis-cd resolves them against that directory on the host that
loads the package, which is the site: relay stages this repository there and
runs `jarvis repo add`, so no path of the configuring machine is stored. Each
entry must hold a `repo.yaml`.

`spack/` is a Spack repository with namespace `clio`, written for Spack
0.23.1:

| Recipe | What it adds |
|---|---|
| `decomp2d-fft@2.0.4` | 2DECOMP&FFT, variants `io=mpi\|adios2` and `fft=generic\|fftw`. With `io=adios2` it fixes the `.bp5` extension of ADIOS2 output. `+clio` (needs `io=adios2`) adds the VTX metadata routines that `xcompact3d +clio` calls. |
| `xcompact3d@5.0` | Incompact3d v5.0. `+clio` applies the VTX attribute patch (so ParaView's ADIOS2VTXReader opens `data.bp5`) and the geometry patch (the STL immersed boundary) in `patch()`. Without `+clio` the Taylor-Green case still runs. |
| `gray-scott@2.10.2` | ADIOS2's Gray-Scott example, built against an installed ADIOS2 2.10. `+clio` adds a double `time` and points the `vtk.xml` TIME array at it, so the reader shows model time. |

A package's sha256 covers its Spack repositories too, so a recipe change is a
new package version.

## The contract

Each package directory holds `pkg.py` (the JARVIS class), `contract.yaml`,
`progress.py` and `artifacts.py` (the jarvis-cd providers) and, when it needs
more than the generic checks, `selftest.py`. `contract.yaml` follows
`schemas/package-contract.v1.json` and has these parts:

| Part | Holds |
|---|---|
| `install` | The Spack spec (`query`) and the Spack repositories (`repos`, relative to the checkout) the package installs from. They become the menu defaults `install_method: spack`, `install_query` and `install_repos` (rewritten relative to the package directory). An MPI pinned in the query is refused. |
| `parameters` | Limits on the `pkg.py` menu, the fields to show for review, and fill rules such as `run_dir: "{shared_dir}/run"`. The menu stays the single list of parameters. `cases` holds rules for one case only, such as a parameter that case uses and the others ignore, so it is checked only when the configuration matches the case's `when`. |
| `resources` | PSI/J process count, processes per node, duration and queue, as parameter references. |
| `sizing` | How the case maps to ranks and nodes: the mesh, the decomposition (`pencil2d`, `block3d`, `data-parallel` or `none`), the rules for choosing `nprocs`, processes per node (`param`, `default: physical_cores` and `cores`, the parameter with the target partition's physical cores per node), the node count (`ceil_div: [nprocs, ppn]`) and an optional cost rule. |
| `outputs` | One entry per output: path, format, role and, for snapshots, reader, expected steps and display. Snapshots become view descriptors; `statistics` and `restart` outputs are reported as artifacts when the run has written them. |
| `progress` | Stdout patterns for steps, completion and readiness. `announces: before-write` means the step line comes before that step is readable. |
| `service` | Services only: ports, health check, protection, stop signal and stop record, the jarvis-cd service runtime it reports (`runtime`: record, lifecycle, endpoint), maximum lifetime. |
| `provenance` | Input files hashed into `clio-contract.json`, and the upstream code. |
| `selftest` | Parameters and time limit for the self-test, and an optional extra check script. |

A package missing any part is refused, and the error names the part:

```
package 'gray_scott' refused: contract part 'outputs' is missing
```

### View descriptors

Every snapshots output yields a document that follows `schemas/view-descriptor.v1.json`
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
  "producer": {"package": "clio_jarvis.xcompact3d", "version": "0.3.0", "sha256": "..."}
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
python -m clio_jarvis selftest PACKAGE [--set name=value ...] [--work DIR]
```

- `validate` loads every contract and exits nonzero if any package is refused.
- `describe` prints the document relay reads before a run: install spec,
  parameter schema, resources, sizing, view descriptors, other outputs and
  service ports.
- `selftest` runs the package through jarvis-cd in a private `JARVIS_ROOT`,
  with the contract's `selftest.params` plus any `--set`: `jarvis ppl install
  +check` must find the package's software installed, then `jarvis ppl run`
  runs it. It writes `selftest-report.json` in the work directory and exits 0
  only if every check passes.

The self-test checks:

- The package's Spack spec is installed (`jarvis ppl install +check`); if not,
  it stops and says to run `jarvis ppl install`.
- `clio-contract.json` matches the package hash and holds input digests.
- Applications: progress steps 1 to N in order, with valid growing view
  descriptors; a last `completed` event with `growing: false`; a finalized
  artifact. With `pvpython` on PATH (`spack load paraview`), pvpython opens
  the output with the descriptor's reader and checks the step count, arrays,
  time and a changing field, then saves a screenshot.
- Services: one `ready` event with a port, and a SIGTERM stop that writes the
  stop record.
- The package's own `selftest.py`. For pvserver, two pvpython clients in
  turn connect, render a sphere on the server and disconnect; after each one
  the supervisor must have a new pvserver listening on the same port.

Run the unit tests with `pytest`.

## Packages

### Ranks per node

Every package takes `ppn` (ranks per node) and `cores_per_node`. An empty
`ppn` is filled at configure from `cores_per_node`, the target partition's
physical cores per node, which relay's `describe` reports for each partition
and passes in. If both are empty, the launch decides inside the job, on the
job's node: the physical cores from `lscpu` (or `/proc/cpuinfo`), capped by
`SLURM_CPUS_ON_NODE`. Outside a batch allocation it uses this host's physical
cores only when every rank runs here, and otherwise leaves placement to
`mpiexec`. The configuring host, usually a login node, never sets it.

### gray_scott

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs`, `ppn` | 4, empty | MPI ranks and ranks per node; see [Ranks per node](#ranks-per-node) |
| `L` | 128 | Grid cells per side |
| `steps`, `plotgap` | 15000, 150 | Simulation steps, and steps per output step (100 output steps) |
| `checkpoint_freq` | 0 | Steps between checkpoints in `{run_dir}/ckpt.bp`, the `checkpoint` (restart) output; 0 means none |
| `run_dir` | `{shared_dir}/run` | Output goes to `{run_dir}/gs.bp` |

On 4 Ares ranks the defaults write an output step about every 2.7 s, for
about 4.5 minutes. The software is the `clio.gray-scott +clio` recipe.

### xcompact3d

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs`, `ppn` | 4, empty | MPI ranks and ranks per node; see [Ranks per node](#ranks-per-node) |
| `geometry` | empty | STL file of the body (binary or ASCII, closed, any units). Relative paths are from the directory jarvis runs in; `{pkg_dir}` names the package. Empty means the Taylor-Green case |
| `inflow` | 1.0 | Inflow speed U along +x |
| `angle` | 0 | Angle of attack in degrees, nose up (a turn about z) |
| `resolution` | 24 | Grid points per body length L |
| `reference_length` | 0 | L in STL units; 0 means the STL's x extent |
| `span` | auto | `periodic` (a section extruded along z, run as a thin periodic slab), `free` (a whole 3D body) or `auto` |
| `upstream`, `downstream`, `lateral` | 3, 10, 3 | Domain margins in body lengths |
| `n` | 65 | Grid points per side, Taylor-Green case only (33, 65, 129 or 257) |
| `ilast`, `ioutput`, `dt` | 2400, 20, 0.005 | Iterations, iterations per snapshot, time step |
| `icheckpoint` | 0 | Iterations between restart files (`checkpoint.bp5`, the `restart` output) and statistics writes; 0 means none |
| `initstat` | 0 | Iteration from which statistics are averaged into `statistics.bp5` (the `statistics` output); 0 means none |
| `re` | 1600 | Reynolds number; U L / nu in the body case |
| `run_dir` | `{shared_dir}/run` | Output goes to `{run_dir}/data.bp5` |

**Body case.** Incompact3d's cylinder wake case (`itype = 5`) with the body
from the STL: it is scaled to L = 1, turned by `angle`, and voxelised (ray
parity, pure Python) on the solver grid into `clio-geometry.txt` and
`clio-geometry.bin`, which the clio geometry patch reads as the immersed
boundary and writes back as `ep`. The grid spacing is 1 / `resolution`; the
counts are rounded up to FFT-friendly sizes. Configure refuses a `dt` past CFL
0.2 or the viscous limit of the hyperviscous scheme and names the largest safe
value. The contract summary, which relay shows the agent, carries the setup
rules so `dt` can be worked out before the job is approved: CFL = U dt / h at
most 0.2 (aim for 0.1) and dt nu (2 / h^2 + 1 / dz^2) at most 0.0075, with h =
1 / `resolution`, nu = U / `re`, and dz = 2h for a periodic span of at least 16
/ `resolution` (finer for a shorter one, since the span gets at least 8 points)
or h for a free one. It also covers run length, snapshot count and a walltime estimate of about
nx ny nz `ilast` / (throughput `nprocs`) seconds, with the site's throughput
per rank measured from a short run; the contract's `sizing` part gives the
mesh, pencil-grid and node rules. `clio-case.json` records the grid, body placement, solid points and
stability numbers, `body.stl` is the body in solver coordinates, and
`forces.dat` gets time, drag and lift from a control volume round the body. A
NACA 4412 section at 20 degrees, Re 1000 and resolution 24 (361 x 160 x 8)
sheds a vortex street; 7500 steps at dt 0.004 take about 6 minutes on 20 Ares
ranks.

**Taylor-Green case.** The vortex DNS at Re 1600 from the Incompact3d examples
(`config/input_DNS_Re1600.i3d`, BSD-3, see `NOTICE`). Its vortex sheets roll up
and break into turbulence by about t = 9. On 4 Ares ranks the defaults write a
snapshot about every 3.2 s, for about 6.4 minutes.

**Software.** The `clio.xcompact3d@5.0 +clio` recipe, over
`clio.decomp2d-fft@2.0.4 io=adios2 +clio` and the site's ADIOS2 when it has
one. Statistics and restart files are off by default (`icheckpoint` and
`initstat` 0); when a run writes them they are reported as `statistics` and
`restart` artifacts beside the `snapshots` view.

**Live output on a shared file system.** ADIOS2 does not sync, so a node that
writes `data.bp5` keeps new steps in its page cache and writes them back in
bursts. Each rank therefore starts through `clio_jarvis/_contract/flushwrap.py`
(standard library only, run with the Python that runs JARVIS): on every node
that the MPI launch uses, the node-local rank 0 forks a flusher that syncs the
data files, then `md.0`, `mmd.0` and `md.idx`, every second, and each rank then
execs the solver. A viewer elsewhere sees each step as it lands, whichever
nodes `host_slice` gives the solver. The node-local rank comes from the
launcher's environment (Open MPI, Hydra, PALS, MVAPICH2 or srun); without one
every rank flushes. The run's verdict reads `md.idx` through an open
descriptor, which makes a network file system client fetch fresh attributes,
and, when the solver announced snapshots, keeps reading it for up to 10 s, so
a run written from another node is not judged `no_output` from a stale size.

### pvserver

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs`, `ppn` | 1, empty | pvserver ranks and ranks per node; see [Ranks per node](#ranks-per-node). Above 1 rank, jarvis-cd's MPI executor builds the launch, as it does for an MPI application: the chosen MPI's launcher, the package's hosts (its hostfile, narrowed by `host_slice`) with rank 0 on the node that runs JARVIS, or that node alone, and the resource graph's network. The supervisor runs that launch with the port it picks. `pvserver-launch.json` in `state_dir` is jarvis-cd's `launch_record()` (`jarvis.mpi-launch.v1`, jarvis-cd 2d26101c or newer) plus the launcher's `env`, so the launch's site-fact `advisories` and `reason_codes` stay as jarvis wrote them; a start failure in `pvserver-stop.json` carries them too |
| `port` | 0 | TCP port; 0 picks a free one, reported in the ready event and the service runtime |
| `dataset` | empty | Absolute path, on the site, of the dataset clients open (e.g. xcompact3d's `data.bp5`), named in the service runtime's dataset descriptor; empty means `state_dir` |
| `connect_id` | 0 | ParaView connect id the client must give; 0 for none |
| `persist` | true | Start a new pvserver on the same port when a client disconnects. pvserver serves one client session, then exits; this package does not use `--multi-clients`. |
| `hold` | false | A background start (jarvis-cd's default for a service, `start_mode: auto`) always holds, like builtin.paraview's service mode; this setting is for a foreground start. Off: `start` returns once the server is ready, and a later `stop` (from another `jarvis` process too) stops it. This is how a service runner serves it: `jarvis ppl start`, wait, then `jarvis ppl stop` on TERM, INT or HUP. On: `start` returns only when the server stops, for a hand-run `jarvis ppl run` that should serve until JARVIS gets SIGTERM, SIGINT or SIGHUP, which it passes on. Without hold, `ppl run` starts the server and stops it at once. |
| `max_lifetime` | none | Seconds before the server stops itself. Absent means it runs until it is stopped. When given, it must be positive, with no upper cap. |
| `walltime` | none | Scheduler wall-time limit in seconds. Absent means no limit. When given, it must be positive, with no upper cap. |

`pvserver` binds to 127.0.0.1 only. Reach it over an SSH tunnel. The software
is Spack's builtin `paraview@5.13.1 +mpi +adios2 +python ~qt` with OSMesa
offscreen rendering; 5.13.1 is the version the CLIO ParaView MCP client
builds, and the one Spack 0.23.1 prefers. The
supervisor writes `pvserver-ready.json` when the port is listening and
`pvserver-stop.json` when it stops (`clio.service-stop.v1`, with the reason).
They go to `state_dir`, `{shared_dir}/pvserver` by default.

When JARVIS binds a service runtime (`JARVIS_SERVICE_RUNTIME_PATH`), the
supervisor reports through jarvis-cd's `ServiceRuntimeReporter`: `starting`
once pvserver is launched, `ready` once rank 0 accepts connections, `degraded`
then `ready` around a persist restart, `stopping` on SIGTERM or at
`max_lifetime`, and `stopped` (or `failed`) at the end. The endpoint is the
node running rank 0 and the port; pvserver listens on 127.0.0.1 there, so a
client opens `cs://<host>:<port>` through an SSH tunnel to that node. The
record's protocol is `tcp` (`base_url` `tcp://<host>:<port>`, jarvis-cd
4007fd14 or newer); the messages name the `cs://` endpoint. `ready()` reads that record, so in a
pipeline such as pvserver then xcompact3d, jarvis-cd starts xcompact3d only
once pvserver is ready. A SIGTERM to the job's process tree stops every rank:
the supervisor TERMs pvserver's process group (mpiexec and the local ranks),
mpiexec passes it to the other nodes, and the stop grace (15 s) is within
jarvis-cd's 20 s before KILL.

`jarvis ppl start` and `jarvis ppl stop` don't call configure. Every package
resolves its fill rules (`state_dir`, `run_dir`) when it starts or stops
unconfigured, and `start` also writes the inputs and `clio-contract.json`.
The fills depend only on the pipeline's directories, so a `ppl stop` in
another process finds the server that `ppl start` launched.

Every package's `walltime` works the same way. There is no default and no
cap, absent means no limit, and the resolved record then gives no
`resources.duration`.

## License

BSD-3-Clause. Xcompact3d inputs are from Incompact3d (BSD-3), see
`clio_jarvis/xcompact3d/NOTICE`.
