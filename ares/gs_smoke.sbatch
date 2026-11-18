#!/bin/bash
#SBATCH -J w5-gs
#SBATCH -p debug
#SBATCH -N 1
#SBATCH -n 4
#SBATCH -c 1
#SBATCH -t 00:20:00
#SBATCH -o /home/jcernudagarcia/clio-relay-rework/w5-scratch/gs-smoke-%j.log
# W5: patch and rebuild Gray-Scott, run 4 ranks, check the BP with ParaView's ADIOS2VTXReader.
set -eu
W=$HOME/clio-relay-rework
E=$W/micromamba/envs/clio
S=$W/w5-scratch
export PATH=$E/bin:$PATH
# conda OpenMPI 5 hangs in startup with its default pml/btl on Ares; ob1 + self,sm works.
export OMPI_MCA_pml=ob1 OMPI_MCA_btl=self,sm
B=$W/build/gray-scott
step() { echo; echo "===== $* ($(date +%T))"; }

step "patch and rebuild"
python $S/gs_vtx_patch.py $B
cmake --build $B/build -j4 > $W/build/gs-build.log 2>&1 || { grep -m5 -B2 -A12 -i error $W/build/gs-build.log; exit 1; }
cmake --install $B/build > /dev/null 2>&1 || true

step "run"
R=$S/gs-smoke
rm -rf $R && mkdir -p $R && cd $R
cp $B/adios2.xml .
cat > settings.json <<'EOF'
{
  "L": 64, "Du": 0.2, "Dv": 0.1, "F": 0.01, "k": 0.05, "dt": 2.0,
  "plotgap": 20, "steps": 400, "noise": 0.0000001,
  "output": "gs.bp", "checkpoint": false, "checkpoint_freq": 100000,
  "checkpoint_output": "ckpt.bp", "restart": false, "restart_input": "ckpt.bp",
  "adios_config": "adios2.xml", "adios_span": false, "adios_memory_selection": false,
  "mesh_type": "image"
}
EOF
T0=$(date +%s.%N)
timeout 600 mpirun -n 4 $B/build/adios2_simulations_gray-scott settings.json 2>&1 | \
  awk -v t0=$T0 '{ c = "date +%s.%N"; c | getline now; close(c); printf "%8.3f %s\n", now - t0, $0; fflush() }'
step "bpls"
bpls -la gs.bp | head -20
bpls -A gs.bp | head -20

step "pvpython"
source $W/paraview/pv-env.sh
pvpython --force-offscreen-rendering $S/vtxcheck.py $R/gs.bp V $R/gs-last.png 2>&1 | grep -v '^$' | tail -20
step "done"
