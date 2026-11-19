#!/bin/bash
#SBATCH -J x3body-build
#SBATCH -p compute
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 16
#SBATCH -o /home/jcernudagarcia/clio-relay-rework/w17/x3body-build-%j.log
# Build Xcompact3d with both clio patches (VTX and geometry) into $W/opt/incompact3d-clio.
set -euo pipefail
W=$HOME/clio-relay-rework
E=$W/micromamba/envs/clio
export PATH=$E/bin:$PATH
export adios2_DIR=$E/lib/cmake/adios2
bash $W/clio-jarvis/clio_jarvis/clio_jarvis/xcompact3d/build/build.sh $W/opt/incompact3d-clio 16
