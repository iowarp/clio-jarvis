from spack.package import *
from spack.pkg.builtin.warpx import Warpx as BuiltinWarpx


class Warpx(BuiltinWarpx):
    """Spack's builtin warpx with its build targets fixed for ``~python``.

    The builtin recipe (Spack 0.23) adds ``pip_wheel`` to ``build_targets``
    inside a ``with when("+python")`` block. ``when`` only scopes directives,
    so the plain assignment always runs and every ``~python`` build fails at
    ``make pip_wheel``. Everything else (versions, variants, dependencies and
    CMake arguments) is the builtin recipe's.
    """

    @property
    def build_targets(self):
        targets = ["all"]
        if self.spec.satisfies("+python"):
            targets.append("pip_wheel")
        return targets
