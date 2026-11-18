#!/bin/bash
# W5: create the shared conda-forge env for the relay rework on Ares.
set -euo pipefail
export MAMBA_ROOT_PREFIX=$HOME/clio-relay-rework/micromamba
MM=$MAMBA_ROOT_PREFIX/bin/micromamba
if [ ! -d "$MAMBA_ROOT_PREFIX/envs/clio" ]; then
  $MM create -n clio -y -c conda-forge --override-channels \
    "python=3.12" "paraview>=6.2" "adios2=*=mpi_openmpi*" "openmpi=5" "mpi4py" \
    "fftw=*=mpi_openmpi*" cmake make pkg-config cxx-compiler fortran-compiler \
    numpy jsonschema pyyaml pytest
fi
E=$MAMBA_ROOT_PREFIX/envs/clio
$E/bin/mpicc --version | head -1
$E/bin/mpif90 --version | head -1
$E/bin/mpirun --version | head -1
$E/bin/bpls --version 2>&1 | head -2 || true
$E/bin/python -c "import adios2, mpi4py; print('adios2', adios2.__version__, 'mpi4py', mpi4py.__version__)"
ls $E/bin | grep -E "^pv" || true
