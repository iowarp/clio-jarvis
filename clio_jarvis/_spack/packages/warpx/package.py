from spack.package import *
from spack.pkg.builtin.warpx import Warpx as BuiltinWarpx


class Warpx(BuiltinWarpx):
    """Spack's builtin warpx with its Python steps fixed for ``~python``.

    The builtin recipe (Spack 0.23) adds ``pip_wheel`` to ``build_targets``
    inside a ``with when("+python")`` block. ``when`` only scopes directives,
    so the plain assignment always runs and every ``~python`` build fails at
    ``make pip_wheel``. Its ``pip_install_nodeps`` phase likewise always runs.
    Here both happen only with ``+python``.

    ``+qed`` (the default) needs PICSAR's QED source. WarpX cannot use an
    installed PICSAR and the builtin recipe leaves it to CMake FetchContent,
    a git clone during the build. Here Spack fetches the checksummed release
    WarpX pins as a resource, and CMake is pointed at it. Everything else
    (versions, variants, dependencies and CMake arguments) is the builtin
    recipe's.
    """

    resource(
        name="picsar",
        url="https://github.com/ECP-WarpX/picsar/archive/refs/tags/24.09.tar.gz",
        sha256="f24bf6438efad25d82462f71773978cde26fe8a20ebb05589b4c6108ba79dae1",
        placement="picsar",
        when="@24.10 +qed",
    )

    def cmake_args(self):
        args = super().cmake_args()
        if self.spec.satisfies("@24.10 +qed"):
            args.append(self.define("WarpX_picsar_src", join_path(self.stage.source_path, "picsar")))
        return args

    @property
    def build_targets(self):
        targets = ["all"]
        if self.spec.satisfies("+python"):
            targets.append("pip_wheel")
        return targets

    def pip_install_nodeps(self, spec, prefix):
        if spec.satisfies("+python"):
            super().pip_install_nodeps(spec, prefix)
