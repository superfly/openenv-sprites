"""OpenEnv runtime support for Fly.io Sprites."""

from importlib.metadata import PackageNotFoundError, version

from .provider import SpritesProvider, SpritesProviderError

try:
    __version__ = version("openenv-sprites")
except PackageNotFoundError:  # source tree imported without installation
    __version__ = "0+unknown"

__all__ = ["SpritesProvider", "SpritesProviderError", "__version__"]
