"""wattracker - local cycling training analyzer."""

try:
    # setuptools-scm generates this file from the same Git metadata as the
    # wheel; PyInstaller includes it as an ordinary package module.
    from ._scm_version import __version__
except ImportError:
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("wattracker")
    except PackageNotFoundError:
        # An uninstalled source checkout has no distribution metadata to read.
        __version__ = "0+unknown"
