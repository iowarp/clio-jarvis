from spack.package import *


class GrayScott(CMakePackage):
    """ADIOS2's Gray-Scott reaction-diffusion example
    (examples/simulations/gray-scott), built unchanged on its own against an
    installed ADIOS2 of the same release series. Spack has no recipe for it,
    and its adios2 recipe does not build the examples.
    """

    homepage = "https://github.com/ornladios/ADIOS2/tree/master/examples/simulations/gray-scott"
    url = "https://github.com/ornladios/ADIOS2/archive/v2.10.2.tar.gz"
    git = "https://github.com/ornladios/ADIOS2.git"

    license("Apache-2.0")

    version("2.10.2", sha256="14cf0bcd94772194bce0f2c0e74dba187965d1cffd12d45f801c32929158579e")

    root_cmakelists_dir = join_path("examples", "simulations", "gray-scott")

    depends_on("c", type="build")
    depends_on("cxx", type="build")
    depends_on("cmake@3.12:", type="build")

    depends_on("mpi")
    depends_on("adios2@2.10+mpi", when="@2.10")

    def cmake_args(self):
        # The example installs to CMAKE_INSTALL_BINDIR without including GNUInstallDirs.
        return [self.define("CMAKE_INSTALL_BINDIR", "bin")]
