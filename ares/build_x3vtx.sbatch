#!/bin/bash
#SBATCH -J w5-x3vtx
#SBATCH -p compute
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 16
#SBATCH -t 00:40:00
#SBATCH -o /home/jcernudagarcia/clio-relay-rework/w5-scratch/x3vtx-%j.log
# W5: apply the clio-jarvis VTX patch to 2DECOMP&FFT 2.0.4 and Incompact3d v5.0, then rebuild both.
set -euo pipefail
W=$HOME/clio-relay-rework
E=$W/micromamba/envs/clio
export PATH=$E/bin:$PATH
export CC=mpicc CXX=mpicxx FC=mpifort
B=$W/build
O=$W/opt
cd $B
step() { echo; echo "===== $* ($(date +%T))"; }

step "patch"
python $W/w5-scratch/clio_vtx_patch.py $B/2decomp-fft-2.0.4 $B/Incompact3d-5.0

step "2decomp-fft"
cmake --build 2decomp-fft-2.0.4/build -j16 > 2d-build.log 2>&1 || { grep -m5 -B2 -A12 -i "error" 2d-build.log; exit 1; }
cmake --install 2decomp-fft-2.0.4/build > 2d-install.log 2>&1

step "incompact3d"
# The installed decomp2d module files changed; force Incompact3d to recompile against them.
cmake --build Incompact3d-5.0/build --target clean > /dev/null
cmake --build Incompact3d-5.0/build -j16 > x3-build.log 2>&1 || { grep -m5 -B2 -A12 -i "error" x3-build.log; exit 1; }
cmake --install Incompact3d-5.0/build > x3-install.log 2>&1 || tail -5 x3-install.log
ls -la $O/incompact3d/bin 2>/dev/null || find Incompact3d-5.0/build -name xcompact3d -type f
step "done"
