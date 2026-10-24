"""The installed distribution's version (maturin stamps it from Cargo.toml)."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    __version__ = version = _dist_version('urio')
except PackageNotFoundError:
    __version__ = version = '0.0.0+unknown'
