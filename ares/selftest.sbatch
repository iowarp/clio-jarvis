#!/bin/bash
#SBATCH -J w5-selftest
#SBATCH -p debug
#SBATCH -N 1
#SBATCH -n 4
#SBATCH -c 1
#SBATCH -t 00:45:00
#SBATCH -o /home/jcernudagarcia/clio-relay-rework/w5-scratch/selftest-%j.log
# W5: run the three clio-jarvis self-tests through jarvis-cd on one compute node.
set -u
W=$HOME/clio-relay-rework
S=$W/w5-scratch
export PATH=$W/opt/gray-scott/bin:$W/opt/incompact3d/bin:$W/micromamba/envs/clio/bin:$PATH
export OMPI_MCA_pml=ob1 OMPI_MCA_btl=self,sm
. $W/.venv/bin/activate
export PYTHONPATH=$W/clio-jarvis/clio_jarvis
PV=$W/paraview/pv-env.sh
cd $W/clio-jarvis
echo "node $(hostname) python $(command -v python) mpirun $(command -v mpirun)"
(source $PV && pvserver --version 2>&1 | head -1)

rc=0
for pkg in ${PKGS:-gray_scott xcompact3d pvserver}; do
  echo; echo "===== selftest $pkg ($(date +%T))"
  extra=()
  [ "$pkg" = pvserver ] && extra=(--set "env_script=$PV")
  python -m clio_jarvis selftest $pkg --work $S/selftest --pv-env $PV "${extra[@]}" || rc=1
done
echo; echo "===== done rc=$rc ($(date +%T))"
exit $rc
