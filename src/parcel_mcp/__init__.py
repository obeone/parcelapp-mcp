"""MCP server for the Parcel delivery tracking app API."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("parcelapp-mcp")
except PackageNotFoundError:  # running straight from a source tree
    __version__ = "0.0.0+unknown"

from .server import main, mcp

__all__ = ["__version__", "main", "mcp"]
