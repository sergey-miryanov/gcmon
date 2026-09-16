"""What ``--version`` and ``gcmon.__version__`` both print."""

from ..support.vocabulary import PROGRAM_NAME


def installed_version() -> str:
    """Read gcmon's version from the installed distribution's metadata.

    The read stats every ``sys.path`` entry, so it happens when someone asks for the version
    rather than when the package is imported.
    """
    import importlib.metadata

    try:
        return importlib.metadata.version(PROGRAM_NAME)
    except importlib.metadata.PackageNotFoundError:
        # A source tree with no install: nothing to read a version from.
        return "0.0.0+unknown"
