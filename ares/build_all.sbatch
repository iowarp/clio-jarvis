#!/bin/bash
#SBATCH -J w5-build
#SBATCH -p compute
#SBATCH -N 1
#SBATCH -n 1
#SBATCH -c 16
#SBATCH -t 01:00:00
#SBATCH -o /home/jcernudagarcia/clio-relay-rework/w5-scratch/build-%j.log
# W5: build ADIOS2 Gray-Scott, 2DECOMP&FFT 2.0.4 (ADIOS2 IO, BP5 backport) and Incompact3d v5.0 against the clio env.
set -euo pipefail
W=$HOME/clio-relay-rework
E=$W/micromamba/envs/clio
export PATH=$E/bin:$PATH
export CC=mpicc CXX=mpicxx FC=mpifort
B=$W/build
O=$W/opt
mkdir -p $B $O
cd $B
J=16
step() { echo; echo "===== $* ($(date +%T))"; }

step "fetch"
[ -f ADIOS2-2.12.1.tar.gz ] || curl -sSL -o ADIOS2-2.12.1.tar.gz https://github.com/ornladios/ADIOS2/archive/refs/tags/v2.12.1.tar.gz
[ -f 2decomp-fft-2.0.4.tar.gz ] || curl -sSL -o 2decomp-fft-2.0.4.tar.gz https://github.com/2decomp-fft/2decomp-fft/archive/refs/tags/v2.0.4.tar.gz
[ -f Incompact3d-5.0.tar.gz ] || curl -sSL -o Incompact3d-5.0.tar.gz https://github.com/xcompact3d/Incompact3d/archive/refs/tags/v5.0.tar.gz
sha256sum *.tar.gz

step "gray-scott"
rm -rf gray-scott && mkdir gray-scott
tar -xzf ADIOS2-2.12.1.tar.gz -C gray-scott --strip-components=4 ADIOS2-2.12.1/examples/simulations/gray-scott
cmake -S gray-scott -B gray-scott/build -DCMAKE_BUILD_TYPE=Release -DADIOS2_DIR=$E/lib/cmake/adios2 \
  -DCMAKE_INSTALL_PREFIX=$O/gray-scott -DCMAKE_INSTALL_RPATH=$E/lib -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON > gs-cmake.log 2>&1 || { tail -30 gs-cmake.log; exit 1; }
cmake --build gray-scott/build -j$J > gs-build.log 2>&1 || { tail -40 gs-build.log; exit 1; }
cmake --install gray-scott/build > /dev/null 2>&1 || true
ls gray-scott/build | head -20
find gray-scott/build -maxdepth 2 -type f -perm -u+x -name "*gray*" -o -maxdepth 2 -type f -perm -u+x -name "pdf*"

step "2decomp-fft"
rm -rf 2decomp-fft-2.0.4 && tar -xzf 2decomp-fft-2.0.4.tar.gz
sed -i 's/ext = ".bp4"/ext = ".bp4"\n      else if (io%engine_type == "BP5") then\n         ext = ".bp5"/' 2decomp-fft-2.0.4/src/io.f90
sed -i '/Unkown engine type/,/stop/c\         ext = ""' 2decomp-fft-2.0.4/src/io.f90
grep -n -A2 'ext = ".bp4"' 2decomp-fft-2.0.4/src/io.f90 | head -6
cmake -S 2decomp-fft-2.0.4 -B 2decomp-fft-2.0.4/build -DCMAKE_BUILD_TYPE=Release -DIO_BACKEND=adios2 \
  -Dadios2_DIR=$E/lib/cmake/adios2 -DCMAKE_INSTALL_PREFIX=$O/2decomp-fft > 2d-cmake.log 2>&1 || { tail -30 2d-cmake.log; exit 1; }
cmake --build 2decomp-fft-2.0.4/build -j$J > 2d-build.log 2>&1 || { grep -m5 -B2 -A8 -i "error" 2d-build.log; exit 1; }
cmake --install 2decomp-fft-2.0.4/build > 2d-install.log 2>&1
D2=$O/2decomp-fft/lib/decomp2d
test -f $D2/decomp2d-config.cmake
echo "decomp2d_DIR=$D2"

step "incompact3d"
rm -rf Incompact3d-5.0 && tar -xzf Incompact3d-5.0.tar.gz
cmake -S Incompact3d-5.0 -B Incompact3d-5.0/build -DCMAKE_BUILD_TYPE=Release -DIO_BACKEND=adios2 \
  -Dadios2_DIR=$E/lib/cmake/adios2 -Ddecomp2d_DIR=$D2 -DBUILD_TESTING=OFF \
  -DCMAKE_INSTALL_PREFIX=$O/incompact3d -DCMAKE_INSTALL_RPATH="$E/lib;$O/2decomp-fft/lib" > x3-cmake.log 2>&1 || { tail -30 x3-cmake.log; exit 1; }
cmake --build Incompact3d-5.0/build -j$J > x3-build.log 2>&1 || { grep -m5 -B2 -A8 -i "error" x3-build.log; exit 1; }
cmake --install Incompact3d-5.0/build > x3-install.log 2>&1 || tail -5 x3-install.log
find $O/incompact3d Incompact3d-5.0/build -name xcompact3d -type f | head
ls Incompact3d-5.0/examples
step "done"
