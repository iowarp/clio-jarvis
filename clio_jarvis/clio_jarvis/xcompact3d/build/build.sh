#!/bin/bash
# Build Xcompact3d (Incompact3d v5.0) with ADIOS2 output for clio_jarvis.xcompact3d.
#
#   build.sh <prefix> [jobs]
#
# Needs MPI compilers (mpicc, mpifort), CMake 3.20 or newer, Python 3, curl
# and ADIOS2 2.9 or newer with Fortran and MPI. Point adios2_DIR at its CMake
# directory if CMake cannot find it. Builds 2DECOMP&FFT 2.0.4 with the ADIOS2
# backend (and the BP5 file extension), applies the clio VTX patch (vtk.xml and
# time, so ParaView follows the output) and the clio geometry patch (the body
# mask from clio-geometry.txt and .bin, written as ep), then installs
# <prefix>/bin/xcompact3d. Set the package's executable to that path, or put
# <prefix>/bin on PATH. Sources are cached in <prefix>/src.
set -euo pipefail
PREFIX=$(realpath -m "${1:?usage: build.sh <prefix> [jobs]}")
JOBS=${2:-8}
HERE=$(cd "$(dirname "$0")" && pwd)
export CC=${CC:-mpicc} CXX=${CXX:-mpicxx} FC=${FC:-mpifort}
ADIOS=()
[ -n "${adios2_DIR:-}" ] && ADIOS=(-Dadios2_DIR="$adios2_DIR")
mkdir -p "$PREFIX/src"
cd "$PREFIX/src"
step() { echo; echo "===== $* ($(date +%T))"; }

step fetch
[ -f 2decomp-fft-2.0.4.tar.gz ] || curl -fsSL -o 2decomp-fft-2.0.4.tar.gz https://github.com/2decomp-fft/2decomp-fft/archive/refs/tags/v2.0.4.tar.gz
[ -f Incompact3d-5.0.tar.gz ] || curl -fsSL -o Incompact3d-5.0.tar.gz https://github.com/xcompact3d/Incompact3d/archive/refs/tags/v5.0.tar.gz
rm -rf 2decomp-fft-2.0.4 Incompact3d-5.0
tar -xzf 2decomp-fft-2.0.4.tar.gz
tar -xzf Incompact3d-5.0.tar.gz

step patch
# 2DECOMP&FFT 2.0.4 knows BP4 only; name BP5 output .bp5 as later releases do.
sed -i 's/ext = ".bp4"/ext = ".bp4"\n      else if (io%engine_type == "BP5") then\n         ext = ".bp5"/' 2decomp-fft-2.0.4/src/io.f90
sed -i '/Unkown engine type/,/stop/c\         ext = ""' 2decomp-fft-2.0.4/src/io.f90
python3 "$HERE/clio_vtx_patch.py" 2decomp-fft-2.0.4 Incompact3d-5.0
python3 "$HERE/clio_geometry_patch.py" Incompact3d-5.0

step 2decomp-fft
cmake -S 2decomp-fft-2.0.4 -B 2decomp-fft-2.0.4/build -DCMAKE_BUILD_TYPE=Release -DIO_BACKEND=adios2 \
  "${ADIOS[@]}" -DCMAKE_INSTALL_PREFIX="$PREFIX" > 2decomp-cmake.log 2>&1 || { tail -30 2decomp-cmake.log; exit 1; }
cmake --build 2decomp-fft-2.0.4/build -j"$JOBS" > 2decomp-build.log 2>&1 || { grep -m5 -B2 -A8 -i error 2decomp-build.log; exit 1; }
cmake --install 2decomp-fft-2.0.4/build > 2decomp-install.log 2>&1

step incompact3d
cmake -S Incompact3d-5.0 -B Incompact3d-5.0/build -DCMAKE_BUILD_TYPE=Release -DIO_BACKEND=adios2 \
  "${ADIOS[@]}" -Ddecomp2d_DIR="$PREFIX/lib/decomp2d" -DBUILD_TESTING=OFF -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_INSTALL_RPATH="$PREFIX/lib${adios2_DIR:+;$(realpath -m "$adios2_DIR/../..")}" > x3d-cmake.log 2>&1 || { tail -30 x3d-cmake.log; exit 1; }
cmake --build Incompact3d-5.0/build -j"$JOBS" > x3d-build.log 2>&1 || { grep -m5 -B2 -A8 -i error x3d-build.log; exit 1; }
cmake --install Incompact3d-5.0/build > x3d-install.log 2>&1
test -x "$PREFIX/bin/xcompact3d"
step "done: $PREFIX/bin/xcompact3d"
