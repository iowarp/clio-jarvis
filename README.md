# clio-jarvis

JARVIS packages for the clio relay. Each package carries a contract (RL40) that
relay reads before it runs anything, so relay never has to guess a package's
parameters, resources, outputs, progress, ports or provenance.

clio-jarvis is a plain JARVIS repository: every package is a
`jarvis_cd.core.pkg.Application` or `Service` with a JARVIS menu, and the
contract is a declaration next to it that relay validates before it submits
anything. There is no clio base class, and no package uses JARVIS's builtin
repository. The packages need jarvis-cd's `relay_ready` branch (after 1.8.1),
which has `jarvis ppl install` with the Spack installer and the PSI/J
submission.

| Package | Kind | What it does |
|---|---|---|
| `clio_jarvis.gray_scott` | application | ADIOS2 Gray-Scott reaction-diffusion. Writes U and V to a growing BP5 output. |
| `clio_jarvis.wrf` | application | WRF ARW 4.6 on a real-data case (real.exe's output, such as NCAR's CONUS 12 km benchmark). Writes one netCDF history file (wrfout_d01_*) per history interval. |
| `clio_jarvis.warpx` | application | WarpX 24.10, 3D electromagnetic particle-in-cell, running any inputs file as written. Writes one AMReX plotfile of the fields (plotfiles/plt*) per plot interval, each published whole. |
| `clio_jarvis.lammps` | application | LAMMPS molecular dynamics, running any input script as written. Writes one VTK PolyData frame of the atoms (frames/frame_*.vtp) per dump interval. |
| `clio_jarvis.gromacs` | application | GROMACS 2024.3 biomolecular dynamics on the person's mdp, structure and topology, with the preparation stages as gmx commands. Writes one VTK PolyData frame of the atoms per frame interval. |
| `clio_jarvis.pvserver` | service | A ParaView 5.13.1 `pvserver` bound to 127.0.0.1, with a ready record and a stop record. |

## Install

A site needs three steps:

```bash
uv tool install "jarvis-cd @ git+https://github.com/grc-iit/jarvis-cd@relay_ready"
jarvis repo rm builtin
jarvis repo add https://github.com/iowarp/clio-jarvis   # or the path of a clio_jarvis directory
```

A git URL is cloned into the JARVIS root as `repos/clio_jarvis`. A path must
end in a directory named `clio_jarvis`, the name of the Python package inside
it. The contract helpers in `_contract/` and the Spack recipes in `_spack/`
ship inside that directory, so JARVIS loads them the way it loads any repo.
Nothing is pip installed on the site.

### Software

Every package names its software in its `pkg.py` menu, as the defaults of
jarvis-cd's `install_method`, `install_query` and `install_repos`:

| Package | `install_query` | `install_repos` |
|---|---|---|
| `gray_scott` | `gray-scott@2.10.2 +clio ^adios2+mpi` | `../_spack` |
| `wrf` | `wrf@4.6.1 build_type=dmpar compile_type=em_real` | none (Spack's builtin recipe, so a site's WRF is reused) |
| `warpx` | `warpx@24.10 +app +mpi dims=3 compute=noacc ~qed` | `../_spack` |
| `lammps` | `lammps@20240829.1 +mpi` | none (Spack's builtin recipe, so a site's LAMMPS is reused) |
| `gromacs` | `gromacs@2024.3 +mpi` | none (Spack's builtin recipe, so a site's GROMACS is reused) |
| `pvserver` | `paraview@5.13.1 +mpi +adios2 +python ~qt ^[virtuals=gl] osmesa` | none (Spack's builtin recipe) |

```bash
jarvis ppl install +check    # what is already there, what would be built
jarvis ppl install +submit   # build what is missing in a batch job, through PSI/J
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
`pkg.py`), and jarvis-cd resolves them on the site. Each entry must hold a
`repo.yaml`.

#### Other install methods

A menu can name any method jarvis-cd installs with: `spack`, `pip` (a venv),
`conda` (an env, with Miniforge when the site has no conda) or `container` (an
image, with apptainer or rootless podman). The query is then pip requirements,
conda specs or an image, and conda takes `install_channels`. jarvis-cd owns
what each method does. The validator refuses pip and conda for a `pencil2d` or
`block3d` code and for a query that names an MPI (`mpi4py` too), so the site's
MPI is always the one used, and an MPI pinned in a Spack query. All the
packages here are MPI codes or ParaView, so they all use Spack.

`_spack/` is a Spack repository with namespace `clio`, written for Spack
0.23.1:

| Recipe | What it adds |
|---|---|
| `gray-scott@2.10.2` | ADIOS2's Gray-Scott example, built against an installed ADIOS2 2.10. `+clio` adds a double `time` and points the `vtk.xml` TIME array at it, so the reader shows model time. |
| `warpx` | Spack's builtin `warpx`, subclassed. The builtin always builds the `pip_wheel` target (an assignment inside `with when("+python")`) and always runs its `pip_install_nodeps` phase, so `~python` builds fail; this recipe does both only with `+python`. |

A package's sha256 is a digest of its directory plus the repository's `_contract/` and
`_spack/`, computed the same way however the package reached the site, so a
recipe change is a new package version.

## The contract

Each package directory holds `pkg.py` (the JARVIS class), `contract.yaml`,
`progress.py` and `artifacts.py` (the jarvis-cd providers) and, when it needs
more than the generic checks, `selftest.py`. `contract.yaml` follows
`schemas/package-contract.v1.json` and has these parts:

| Part | Holds |
|---|---|
| `title` | Optional: the software's name as a person knows it (`LAMMPS`, `GROMACS`), which relay's questions and job cards show instead of the package name. |
| `parameters` | Limits on the `pkg.py` menu, the fields to show for review, and fill rules such as `run_dir: "{shared_dir}/run"`. The menu stays the single list of parameters. `cases` holds rules for one case only, such as a parameter that case uses and the others ignore, so it is checked only when the configuration matches the case's `when`. |
| `resources` | PSI/J process count, processes per node, duration and queue, as parameter references. |
| `sizing` | How the case maps to ranks and nodes: the mesh, the decomposition (`pencil2d`, `block3d`, `data-parallel` or `none`), the rules for choosing `nprocs`, processes per node (`param`, `default: physical_cores` and `cores`, the parameter with the target partition's physical cores per node), the node count (`ceil_div: [nprocs, ppn]`) and an optional cost rule. Arithmetic (`div`, `mul`, `add`, `ceil_div`) takes two operands, each a number, a parameter name or more arithmetic, so a frame count with the first frame is `{add: [{div: [nsteps, nstout]}, 1]}`. |
| `outputs` | One entry per output: path, format, role and, for snapshots, reader, expected steps and display. Snapshots become view descriptors; `statistics`, `restart` and `result` outputs are reported as artifacts when the run has written them. |
| `progress` | Stdout patterns for steps, completion and readiness. `announces: before-write` means the step line comes before that step is readable. |
| `service` | Services only: ports, health check, protection, stop signal and stop record, the jarvis-cd service runtime it reports (`runtime`: record, lifecycle, endpoint), maximum lifetime, and `data_flow`: which way data moves between the service and the pipeline's other steps (`in`: the steps with outputs feed it, as a solver feeds pvserver; `out`: it feeds them), whatever order JARVIS starts them in. |
| `provenance` | The input files relay hashes into the job's record, and the upstream code. |
| `selftest` | Parameters and time limit for the self-test, and an optional extra check script. |

There is no `install` part: the menu says how the software is installed, and a
contract with one is refused. A package missing any part is refused, and the error names the part:

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
  "name": "fields",
  "path": "<shared_dir>/run/gs.bp",
  "path_kind": "directory",
  "format": "adios2-bp5",
  "reader": "ADIOS2VTXReader",
  "reader_properties": {},
  "growing": true,
  "follow": {"signal": "reader-time-steps", "poll_seconds": 2, "expected_steps": 100},
  "display": {"array": "V", "association": "CELLS", "representation": "Slice"},
  "producer": {"package": "clio_jarvis.gray_scott", "version": "0.2.5", "sha256": "..."}
}
```

`geometry` is optional. It lists surface files (absolute `path`, optional `label`
and `visible`, default true) that a viewer opens beside the data, in the data's own
coordinates. A contract declares them per output as `geometry: [{path, label,
visible, when}]`. `path` is a template like the output path. `when` maps parameters
to JSON schemas, and the entry is reported only when every one matches.

While the run goes, each progress event carries metadata `clio`
(`clio.step.v1`) with `step`, `visible_steps`, `expected_steps` and the view
descriptor. When the run ends, the last event has `growing: false`. Services
send one `ready` event (`clio.service-ready.v1`) with the port they chose.

The packages read none of this at run time beyond what their providers need:
JARVIS runs them as it runs any package, and relay checks the contract before
it submits.

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
- The contract's fills and limits accept the configuration JARVIS resolved.
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
`ppn` takes `cores_per_node`, the target partition's physical cores per node,
which relay's `describe` reports for each partition and passes in. If both are
empty, JARVIS's MPI launch decides inside the allocation from the scheduler's
task geometry, and outside one leaves placement to `mpiexec`.

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

**Live output on a shared file system.** ADIOS2 does not sync, so a node that
writes `gs.bp` keeps new steps in its page cache and writes them back in
bursts. Each rank therefore starts through `clio_jarvis/_contract/flushwrap.py`
(standard library only, run with the Python that runs JARVIS): on every node
that the MPI launch uses, the node-local rank 0 forks a flusher that syncs the
data files, then `md.0`, `mmd.0` and `md.idx`, every second, and each rank then
execs the solver. The node running JARVIS removed the last run's output
just before the launch, so its file system client remembers the name as
missing; while the solver runs, the package lists the output's directory
every second until that node sees the new output, which clears it for any
viewer there (say pvserver's rank 0). A viewer elsewhere sees each step as
it lands, whichever nodes `host_slice` gives the solver. The node-local rank comes from the
launcher's environment (Open MPI, Hydra, PALS, MVAPICH2 or srun); without one
every rank flushes.

### wrf

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs`, `ppn` | 40, empty | MPI ranks and ranks per node; see [Ranks per node](#ranks-per-node) |
| `case_dir` | empty | Directory on the site with real.exe's output (`wrfinput_d01`, `wrfbdy_d01`), its `namelist.input` and any physics tables (`*.dat`) the case needs |
| `run_minutes`, `history_interval` | 180, 60 | Simulated minutes from the case's start, and minutes between history files |
| `time_step` | empty | Model step in seconds; empty keeps the case's |
| `nproc_x`, `nproc_y` | -1, -1 | Patch grid; -1 lets WRF choose a near-square split |
| `run_dir` | `{shared_dir}/run` | History goes to `{run_dir}/wrfout_d01_*` |

The run directory links the install's run tables (`$WRF_HOME/run`), then the
case's files, and gets a `namelist.input` with the run length, history
interval, process grid and no restart writes. Configure refuses a run past
the case's boundary data and a process count that leaves patches under 10
cells a side, naming the largest count that fits. WRF dmpar logs to
`rsl.out.*`, not stdout, so the package follows `rsl.out.0000` and prints
`clio-wrf history N of M` after each history file, which the progress pattern
counts; `clio-run-result.json` (`run-result`) has the verdict, steps and
seconds per step. A public case is NCAR's v4.4 CONUS 12 km benchmark (425 x
300 x 35, 72 s step, 12 h); the contract summary says which members to
extract. The software is Spack's builtin `wrf` recipe with no clio patch, so a
site WRF in Spack is reused with nothing built.

### warpx

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs`, `ppn` | 8, empty | MPI ranks and ranks per node; see [Ranks per node](#ranks-per-node) |
| `inputs` | empty | WarpX inputs file on the site, in WarpX's own format, holding the whole case (`geometry.dims = 3`, `amr.n_cell`, species, lasers, boundaries) |
| `max_step`, `plot_interval` | 400, 20 | Steps to run (replacing the inputs' `max_step` and `stop_time`) and steps between plotfiles; `max_step` must be a multiple of `plot_interval` |
| `fields` | `Ex Ey Ez Bx By Bz jx jy jz rho` | Fields the plotfiles hold |
| `plot_particles` | false | Also write the species' particles into each plotfile |
| `run_dir` | `{shared_dir}/run` | Plotfiles go to `{run_dir}/plotfiles/plt*` |

The run directory gets a copy of the inputs as `inputs`, and
`clio-case.json` with the grid, the level-0 box count (the useful rank
bound), the species, lasers and the inputs' own diagnostics. The command
line sets `max_step` and adds a diagnostic `clio` beside the inputs' own;
configure refuses inputs that are not 3D or already name a diagnostic
`clio`. WarpX writes a plotfile in place, so the package points `clio` at
`writing/` and, when WarpX prints `STEP N ends.` (after that step's
diagnostics are flushed), moves each plotfile up to step N into
`plotfiles/` and prints `clio-warpx plotfile I of M`, which the progress
pattern counts. ParaView's `AMReXBoxLibGridReader` follows the series;
the contract selects the field arrays, which that reader leaves off by
default. `clio-run-result.json` (`run-result`) has the verdict, steps,
simulated time and seconds per step. The software is `_spack/`'s
`clio.warpx`, Spack's builtin `warpx` recipe with one fix: the builtin
(Spack 0.23) always runs its Python wheel steps, so every `~python` build
fails. It is built without QED (PICSAR's source would be fetched
during the build); PSATD, openPMD and embedded boundaries are optional
variants a site's build may lack.

### lammps

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs`, `ppn` | 8, empty | MPI ranks and ranks per node; see [Ranks per node](#ranks-per-node) |
| `script` | empty | LAMMPS input script on the site, in LAMMPS's own commands, holding the whole case (units, lattice or `read_data`, pair style, fixes and `run`) |
| `data_files` | empty | Other files the script reads (data files, potentials), copied beside it by name |
| `steps`, `dump_interval` | 2000, 100 | Steps the script's `run` lines take in all, and steps between frames; `steps` must be a multiple of `dump_interval` |
| `run_dir` | `{shared_dir}/run` | Frames go to `{run_dir}/frames/frame_*.vtp` |

The run directory gets the script as `in.lammps`, its files, and
`clio-case.json`. The package runs `clio-run.lammps`, the script with a
`dump clio` of every atom's id, type, position and velocity inserted
before the first `run`; configure refuses a script with no `run`, one that
already names a dump `clio`, and, when its runs are plain numbers, `steps`
that differ from their sum. A publisher reads each whole dump (every atom
row present) and writes it as `frames/frame_<step>.vtp`, VTK PolyData with
one point per atom, and prints `clio-lammps frame I of M`, which the
progress pattern counts. `clio-run-result.json` has the verdict and LAMMPS's
own performance lines. The software is Spack's builtin `lammps` recipe, so a
site's LAMMPS is reused; a script that needs an optional LAMMPS package
(`manybody`, `molecule`, `kspace` and `rigid` are on by default) names it
in the spec.

### gromacs

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs`, `ppn` | 8, empty | MPI ranks and ranks per node; see [Ranks per node](#ranks-per-node) |
| `ntomp` | 1 | OpenMP threads per mdrun rank |
| `mdp` | empty | The watched run's mdp on the site, in GROMACS's own options |
| `files` | empty | Everything else the case reads (structure, topology and its .itp, earlier stages' mdp files, an index), copied by name |
| `prepare` | empty | The stages before the watched run, one gmx command per line (`pdb2gmx`, `editconf`, `solvate`, `grompp`, `genion`, `mdrun`); `<<< words` answers a tool's prompts |
| `structure`, `topology` | `conf.gro`, `topol.top` | The watched run's grompp `-c` and `-p`, given among `files` or written by a stage |
| `restraints`, `index`, `maxwarn` | empty, empty, 0 | grompp's `-r`, `-n` and `-maxwarn` |
| `nsteps`, `frame_interval` | 50000, 500 | Steps of the watched run and steps between frames (`nstxout`); `nsteps` must be a multiple of `frame_interval` |
| `write_velocities` | true | Also write velocities (`nstvout`), so frames carry speed |
| `run_dir` | `{shared_dir}/run` | Frames go to `{run_dir}/frames/frame_*.vtp` |

Configure keeps the files as given in `run_dir/case`; each start copies them
into the run directory again, since `solvate` and `genion` edit the
topology in place. The stages run in order (mdrun on all the ranks, other
tools on one), then `grompp` of `md.mdp`, the mdp with `nsteps`, `nstxout`
and `nstvout` set by the package, and `mdrun -deffnm md`. GROMACS flushes
md.trr after each frame, so a publisher reads each whole frame and writes
`frames/frame_<step>.vtp` (atoms as points with element, residue, solvent,
velocity and speed, the time in ps) and prints `clio-gromacs frame I of M`.
`clio-run-result.json` has the verdict and mdrun's ns per day. The software
is Spack's builtin `gromacs` recipe (`gmx_mpi`), so a site's GROMACS is
reused; force fields come with it.

### pvserver

| Parameter | Default | Meaning |
|---|---|---|
| `nprocs`, `ppn` | 1, empty | pvserver ranks and ranks per node; see [Ranks per node](#ranks-per-node). Above 1 rank, jarvis-cd's MPI executor builds the launch, as it does for an MPI application: the chosen MPI's launcher, the package's hosts (its hostfile, narrowed by `host_slice`) with rank 0 on the node that runs JARVIS, or that node alone, and the resource graph's network. The supervisor runs that launch with the port it picks. `pvserver-launch.json` in `state_dir` is jarvis-cd's `launch_record()` (`jarvis.mpi-launch.v1`, jarvis-cd 2d26101c or newer) plus the launcher's `env`, so the launch's site-fact `advisories` and `reason_codes` stay as jarvis wrote them; a start failure in `pvserver-stop.json` carries them too |
| `port` | 0 | TCP port; 0 picks a free one, reported in the ready event and the service runtime |
| `dataset` | empty | Absolute path, on the site, of the dataset clients open (e.g. gray_scott's `gs.bp`), named in the service runtime's dataset descriptor; empty means `state_dir` |
| `connect_id` | 0 | ParaView connect id the client must give; 0 for none |
| `persist` | true | Start a new pvserver on the same port when a client disconnects. pvserver serves one client session, then exits; this package does not use `--multi-clients`. |
| `crash_restarts` | 3 | Times a pvserver that crashes (a nonzero exit or a signal, such as a segfault in a filter) starts again on the same port, so a client can rejoin and rebuild its scene. The supervisor kills what is left of the crashed process group and waits for the port to close first. The ready record counts `crashes` with `last_crash` (its exit and last output lines); the stop record lists every crash. 0 means a crash ends the service. |
| `hold` | false | A background start (jarvis-cd's default for a service, `start_mode: auto`) always holds, like builtin.paraview's service mode; this setting is for a foreground start. Off: `start` returns once the server is ready, and a later `stop` (from another `jarvis` process too) stops it. This is how a service runner serves it: `jarvis ppl start`, wait, then `jarvis ppl stop` on TERM, INT or HUP. On: `start` returns only when the server stops, for a hand-run `jarvis ppl run` that should serve until JARVIS gets SIGTERM, SIGINT or SIGHUP, which it passes on. Without hold, `ppl run` starts the server and stops it at once. |
| `max_lifetime` | none | Seconds before the server stops itself. Absent means it runs until it is stopped. When given, it must be positive, with no upper cap. |
| `linger` | 180 | Seconds the server stays up after the pipeline's steps (the solver it views) finish, so a following view draws the last steps and a client can still attach and look at the final output. A cancel ends it at once. 0 means it stops with the steps, and a following view loses the steps it had not drawn yet. No upper cap; it never runs past the job's end. See [Linger after the solver](#linger-after-the-solver). |
| `walltime` | none | Scheduler wall-time limit in seconds. Absent means no limit. When given, it must be positive, with no upper cap. It also bounds the linger when the scheduler gives no time left. |

`pvserver` binds to 127.0.0.1 only. Reach it over an SSH tunnel. The software
is Spack's builtin `paraview@5.13.1 +mpi +adios2 +python ~qt` with OSMesa
offscreen rendering; 5.13.1 is the version the CLIO ParaView MCP client
builds, and the one Spack 0.23.1 prefers. The
supervisor writes `pvserver-ready.json` when the port is listening and
`pvserver-stop.json` when it stops (`clio.service-stop.v1`, with the reason).
They go to `state_dir`, `{shared_dir}/pvserver` by default.

Network fallback warnings in pvserver's output (UCX `WARN`, libfabric
`<warn>`, Open MPI skipping an OpenFabrics port) are told apart from errors:
the supervisor carries its own standard-library copy of
`_contract/transport.py`'s classifier, keeps those lines out of the failure
evidence and the start-failure cause, and reports them in `pvserver-stop.json`
under `transport` as information. The start error repeats them as a
`network info, not the cause` line.

When JARVIS binds a service runtime (`JARVIS_SERVICE_RUNTIME_PATH`), the
supervisor reports through jarvis-cd's `ServiceRuntimeReporter`: `starting`
once pvserver is launched, `ready` once rank 0 accepts connections, `degraded`
then `ready` around a persist restart or a crash restart, `ready` again when it starts to
linger, `stopping` on SIGTERM, at `max_lifetime` or at the linger's end, and
`stopped` (or `failed`) at the end. The endpoint is the
node running rank 0 and the port; pvserver listens on 127.0.0.1 there, so a
client opens `cs://<host>:<port>` through an SSH tunnel to that node. The
record's protocol is `tcp` (`base_url` `tcp://<host>:<port>`); the messages name the `cs://` endpoint. `ready()` reads that record, so in a
pipeline such as pvserver then gray_scott, jarvis-cd starts gray_scott only
once pvserver is ready. A SIGTERM to the job's process tree stops every rank:
the supervisor TERMs pvserver's process group (mpiexec and the local ranks),
mpiexec passes it to the other nodes, and the stop grace (15 s) is within
jarvis-cd's 20 s before KILL.

`jarvis ppl start` and `jarvis ppl stop` don't call configure. Every package
fills an empty `state_dir` or `run_dir` when it starts or stops, and writes its
input files in configure (where relative paths are meant) or in start. The
fills depend only on the pipeline's directories, so a `ppl stop` in
another process finds the server that `ppl start` launched.

#### Linger after the solver

In a pipeline such as pvserver then gray_scott, jarvis-cd stops pvserver once
the last step returns (`after_steps`). pvserver then lingers: its `stop`
sends the supervisor SIGUSR1, and the supervisor keeps serving for `linger`
seconds, restarting after each client session as `persist` says, then stops
with reason `linger-ended`. `jarvis ppl run`, `ppl start` and `ppl submit`
all take this path, so the job ends, and stops being charged, when the
linger does.

- It lingers only after the steps returned OK, on the instance whose start
  holds. Cleanup after a failed step stops it at once, so the failure is
  reported without delay; so does a pipeline with no step.
- `jarvis ppl stop`, a cancel, the job's end or SIGTERM, SIGINT or SIGHUP to
  JARVIS stops it at once.
- It never runs past the job: it ends the stop grace and 60 s before the
  job's end. The time left comes from the scheduler (Slurm's
  `SLURM_JOB_END_TIME`, else `squeue` for `SLURM_JOB_ID`), else from
  `walltime` counted from pvserver's start. A linger it shortens says so.
- `pvserver-ready.json` carries `linger` from the start, then `lingering:
  true`, `linger_seconds`, `linger_started_at`, `linger_until` and
  `linger_capped_by`. The service runtime's `ready` message gives the same
  end, and the log line is `clio-pvserver lingering seconds=N until=...`.
- `pvserver-stop.json` carries a `linger` block: `requested_seconds`,
  `seconds`, `started_at`, `until`, `ended` (`elapsed`, or `signal` when it
  was stopped sooner), `ended_at`, `capped_by` and the job's time left. The
  describe document's `service.linger` gives the setting before the run.
- To free the nodes as soon as a following view has drawn the last step,
  cancel the job then rather than setting `linger` to 0: the linger ends at
  once, and the view keeps the last picture it drew.

The linger runs inside the job's walltime, so a walltime that leaves no room
after the solver shortens it.

Every package's `walltime` works the same way. There is no default and no
cap, absent means no limit, and the resolved record then gives no
`resources.duration`.

## License

BSD-3-Clause.
