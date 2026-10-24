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
        # Match setuptools-scm's configured fallback when no Git metadata or
        # installed distribution metadata is available.
        __version__ = "0.0.0"
