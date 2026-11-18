#!/bin/bash
#SBATCH -J w5-x3
#SBATCH -p debug
#SBATCH -N 1
#SBATCH -n 4
#SBATCH -c 1
#SBATCH -t 00:20:00
#SBATCH -o /home/jcernudagarcia/clio-relay-rework/w5-scratch/x3-smoke-%j.log
# W5: TGV Re1600 short run on 4 ranks, check the BP with ParaView's ADIOS2VTXReader.
set -eu
W=$HOME/clio-relay-rework
E=$W/micromamba/envs/clio
S=$W/w5-scratch
export PATH=$E/bin:$PATH
export OMPI_MCA_pml=ob1 OMPI_MCA_btl=self,sm
X=$W/build/Incompact3d-5.0/examples/TGV-Taylor-Green-vortex
ILAST=${ILAST:-200}
IOUT=${IOUT:-20}
step() { echo; echo "===== $* ($(date +%T))"; }

R=$S/x3-smoke
rm -rf $R && mkdir -p $R/data && cd $R
sed -e 's/<engine type="BP4">/<engine type="BP5">/' $X/adios2_config.xml > adios2_config.xml
sed -e "s/^ilast *=.*/ilast = $ILAST/" -e "s/^ioutput *=.*/ioutput = $IOUT/" \
    -e "s/^icheckpoint *=.*/icheckpoint = 1000000/" $X/input_DNS_Re1600.i3d > input.i3d
python - <<'EOF'
import math
n = 65; d = math.pi / (n - 1)
arrays = ["ux", "uy", "uz", "pp", "vort", "critq"]
das = "\n".join(f'        <DataArray Name="{a}" />' for a in arrays)
ext = f"0 {n-1} 0 {n-1} 0 {n-1}"
open("clio-vtk.xml", "w").write(f'''<?xml version="1.0"?>
<VTKFile type="ImageData" version="0.1" byte_order="LittleEndian">
  <ImageData WholeExtent="{ext}" Origin="0 0 0" Spacing="{d!r} {d!r} {d!r}">
    <Piece Extent="{ext}">
      <PointData Scalars="ux">
{das}
        <DataArray Name="TIME">time</DataArray>
      </PointData>
    </Piece>
  </ImageData>
</VTKFile>
''')
EOF
grep -n "ilast\|ioutput\|icheckpoint" input.i3d

step "run"
T0=$(date +%s.%N)
timeout 900 mpirun -n 4 $W/opt/incompact3d/bin/xcompact3d input.i3d 2>&1 | \
  awk -v t0=$T0 '{ c = "date +%s.%N"; c | getline now; close(c); printf "%8.3f %s\n", now - t0, $0; fflush() }' | \
  grep -i -E "clio|snapshot|time step =|error|warn|abort|elapsed|total" | head -80
ls -la $R $R/data | head -30

step "bpls"
bpls -la data.bp5 | head -30
bpls -A data.bp5 | head -10
bpls -D -v ux data.bp5 | head -20

step "pvpython"
source $W/paraview/pv-env.sh
pvpython --force-offscreen-rendering $S/vtxcheck.py $R/data.bp5 vort $R/x3-last.png 2>&1 | grep -v '^$' | tail -20
step "done"
