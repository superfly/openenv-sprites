from __future__ import annotations

import inspect

from openenv.core.containers.runtime import ContainerProvider

from openenv_sprites import SpritesProvider, __version__


def test_package_exposes_version() -> None:
    assert __version__ == "0.2.0"


def test_provider_implements_openenv_container_contract() -> None:
    assert issubclass(SpritesProvider, ContainerProvider)
    assert not inspect.isabstract(SpritesProvider)

    start = inspect.signature(SpritesProvider.start_container)
    assert {"image", "port", "env_vars"} <= start.parameters.keys()
