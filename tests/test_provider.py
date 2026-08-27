from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from sprites.exceptions import AuthenticationError, NotFoundError, SpriteError
from sprites.types import SpriteConfig, URLSettings
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from openenv_sprites.provider import (
    SpritesProvider,
    SpritesProviderError,
    _as_websocket_url,
    _AuthenticatedWebSocketProxy,
    _parse_source,
)


@dataclass
class _Result:
    returncode: int = 0
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass
class _Event:
    type: str
    data: str | None = None
    exit_code: int | None = None


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "healthy" if status_code == 200 else "not ready"


class _HTTPClient:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = statuses or [200]
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        del kwargs
        self.urls.append(url)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return _Response(status)


class _Sprite:
    def __init__(self, name: str = "openenv-test") -> None:
        self.name = name
        self.url = f"https://{name}.sprites.test"
        self.commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.services: list[dict[str, object]] = []
        self.destroyed = False
        self.destroy_calls = 0
        self.destroy_errors: list[Exception] = []
        self.command_result = _Result()
        self.command_results: dict[tuple[str, ...], _Result] = {}
        self.service_events = [_Event(type="started")]

    def run(self, *args: str, **kwargs: object) -> _Result:
        self.commands.append((args, kwargs))
        return self.command_results.get(args, self.command_result)

    def create_service(self, **kwargs: object) -> list[_Event]:
        self.services.append(kwargs)
        return self.service_events

    def destroy(self) -> None:
        self.destroy_calls += 1
        if self.destroy_errors:
            raise self.destroy_errors.pop(0)
        self.destroyed = True


class _Client:
    def __init__(self, sprite: _Sprite, statuses: list[int] | None = None) -> None:
        self.token = "secret"
        self.sprite = sprite
        self.created: list[tuple[str, dict[str, object]]] = []
        self.http_client = _HTTPClient(statuses)
        self.closed = False

    def create_sprite(self, name: str, **kwargs: object) -> _Sprite:
        self.created.append((name, kwargs))
        return self.sprite

    def close(self) -> None:
        self.closed = True


class _Proxy:
    def __init__(self, upstream: str, token: str, **kwargs: object) -> None:
        self.upstream = upstream
        self.token = token
        self.options = kwargs
        self.stopped = False

    @property
    def diagnostics(self) -> dict[str, int]:
        return {"connection_errors": 0}

    def start(self, **kwargs: object) -> str:
        del kwargs
        return "http://127.0.0.1:43210"

    def stop(self, **kwargs: object) -> None:
        del kwargs
        self.stopped = True


class _FailingStopProxy(_Proxy):
    def stop(self, **kwargs: object) -> None:
        del kwargs
        raise RuntimeError("bridge stuck")


def test_parses_hf_source_and_revision() -> None:
    source = _parse_source("hf://openenv/echo_env@main")
    assert source.clone_url == "https://huggingface.co/spaces/openenv/echo_env"
    assert source.revision == "main"


def test_rejects_oci_image_reference() -> None:
    with pytest.raises(ValueError, match="source checkout"):
        _parse_source("registry.hf.space/openenv-echo-env:latest")


def test_converts_http_schemes_to_websocket() -> None:
    assert _as_websocket_url("https://example.test/ws") == "wss://example.test/ws"
    assert _as_websocket_url("http://127.0.0.1/ws") == "ws://127.0.0.1/ws"


def test_websocket_proxy_injects_auth_and_relays_messages() -> None:
    async def scenario() -> None:
        async def upstream(connection: object) -> None:
            request = connection.request  # type: ignore[attr-defined]
            assert request.headers["Authorization"] == "Bearer secret"
            message = await connection.recv()  # type: ignore[attr-defined]
            await connection.send(f"echo:{message}")  # type: ignore[attr-defined]

        async with serve(upstream, "127.0.0.1", 0, max_size=3 * 1024 * 1024) as server:
            port = server.sockets[0].getsockname()[1]
            proxy = _AuthenticatedWebSocketProxy(
                f"http://127.0.0.1:{port}", "secret"
            )
            base_url = proxy.start()
            try:
                assert "secret" not in base_url
                payload = "x" * (2 * 1024 * 1024)
                async with connect(
                    base_url.replace("http://", "ws://") + "/ws",
                    max_size=3 * 1024 * 1024,
                ) as ws:
                    await ws.send(payload)
                    assert await ws.recv() == f"echo:{payload}"
            finally:
                proxy.stop()

            assert proxy.diagnostics["connection_errors"] == 0

    asyncio.run(scenario())


def test_start_bootstraps_service_and_stop_deletes_sprite() -> None:
    sprite = _Sprite()
    client = _Client(sprite)
    provider = SpritesProvider(client=client, sprite_name="openenv-test")

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        base_url = provider.start_container(
            "hf://openenv/echo_env", env_vars={"ENABLE_WEB_INTERFACE": "false"}
        )
        provider.wait_for_ready(base_url, timeout_s=0.1)
        proxy = provider._proxy
        provider.stop_container()

    assert base_url == "http://127.0.0.1:43210"
    assert client.created == [
        (
            "openenv-test",
            {"labels": ["openenv"], "wait_for_capacity": False},
        )
    ]
    assert sprite.commands[0][0] == (
        "git",
        "clone",
        "--depth",
        "1",
        "https://huggingface.co/spaces/openenv/echo_env",
        "/srv/openenv",
    )
    assert sprite.commands[1][0] == ("git", "rev-parse", "HEAD")
    assert sprite.commands[2][0] == (
        "curl",
        "-LsSf",
        "https://astral.sh/uv/0.12.6/install.sh",
        "-o",
        "/tmp/openenv-uv-install.sh",
    )
    assert sprite.commands[3][0] == (
        "env",
        "UV_UNMANAGED_INSTALL=/opt/openenv/bin",
        "sh",
        "/tmp/openenv-uv-install.sh",
    )
    assert sprite.commands[4][0] == ("/opt/openenv/bin/uv", "--version")
    assert sprite.commands[5][0] == ("test", "-f", "/srv/openenv/uv.lock")
    assert sprite.commands[6][0] == (
        "/opt/openenv/bin/uv",
        "sync",
        "--frozen",
    )
    assert sprite.commands[6][1]["cwd"] == "/srv/openenv"
    assert sprite.services == [
        {
            "service_name": "openenv",
            "cmd": "/opt/openenv/bin/uv",
            "args": [
                "run",
                "server",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ],
            "env": {"ENABLE_WEB_INTERFACE": "false"},
            "dir": "/srv/openenv",
            "http_port": 8000,
            "duration": 2.0,
        }
    ]
    assert client.http_client.urls == [
        "https://openenv-test.sprites.test/health"
    ]
    assert proxy is not None
    assert proxy.upstream == "https://openenv-test.sprites.test"
    assert proxy.token == "secret"
    assert proxy.stopped
    assert sprite.destroyed
    assert provider.diagnostics["sprite_delete_attempted"] is True
    assert provider.diagnostics["sprite_deleted"] is True
    assert {
        "sprite_create",
        "source_clone",
        "uv_install",
        "dependency_sync",
        "service_register",
        "websocket_bridge",
        "provision_total",
        "readiness",
    } <= provider.timings.keys()
    assert provider.diagnostics["last_health_status"] == 200
    assert provider.diagnostics["health_attempts"] == 1
    assert "last_health_error" not in provider.diagnostics


def test_failed_bootstrap_deletes_created_sprite() -> None:
    sprite = _Sprite()
    sprite.command_result = _Result(returncode=1, stderr=b"clone failed")
    provider = SpritesProvider(client=_Client(sprite), sprite_name="openenv-test")

    with pytest.raises(SpritesProviderError, match="clone failed"):
        provider.start_container("hf://openenv/echo_env")

    assert sprite.destroyed
    assert provider.sprite is None
    assert provider.diagnostics["sprite_deleted"] is True
    assert provider.diagnostics["failed_stage"] == "source_clone"
    assert provider.diagnostics["error_type"] == "SpritesProviderError"


def test_revision_checkout_supports_commit_refs_and_records_resolved_commit() -> None:
    sprite = _Sprite()
    sprite.command_results[("git", "rev-parse", "HEAD")] = _Result(
        stdout=b"abc123\n"
    )
    provider = SpritesProvider(client=_Client(sprite), sprite_name="openenv-test")

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        provider.start_container("hf://openenv/echo_env@abc123")
        provider.stop_container()

    commands = [command for command, _ in sprite.commands]
    assert commands[:4] == [
        ("git", "init", "/srv/openenv"),
        (
            "git",
            "-C",
            "/srv/openenv",
            "remote",
            "add",
            "origin",
            "https://huggingface.co/spaces/openenv/echo_env",
        ),
        (
            "git",
            "-C",
            "/srv/openenv",
            "fetch",
            "--depth",
            "1",
            "origin",
            "abc123",
        ),
        (
            "git",
            "-C",
            "/srv/openenv",
            "checkout",
            "--detach",
            "FETCH_HEAD",
        ),
    ]
    assert provider.diagnostics["source_commit"] == "abc123"


def test_source_url_rejects_embedded_credentials() -> None:
    with pytest.raises(ValueError, match="must not contain credentials"):
        _parse_source("https://user:token@example.test/environment.git")


def test_missing_lockfile_uses_unfrozen_dependency_sync() -> None:
    sprite = _Sprite()
    sprite.command_results[("test", "-f", "/srv/openenv/uv.lock")] = _Result(
        returncode=1
    )
    provider = SpritesProvider(client=_Client(sprite), sprite_name="openenv-test")

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        provider.start_container("hf://openenv/echo_env")
        provider.stop_container()

    commands = [command for command, _ in sprite.commands]
    assert ("/opt/openenv/bin/uv", "sync") in commands
    assert provider.diagnostics["dependency_lockfile"] is False
    assert provider.diagnostics["dependency_mode"] == "unfrozen"


def test_prepared_allocation_skips_source_bootstrap() -> None:
    class _PreparedProvider(SpritesProvider):
        def _allocate_sprite(self, client: object, name: str) -> tuple[object, bool]:
            return client.create_sprite(name), False  # type: ignore[attr-defined]

    sprite = _Sprite()
    provider = _PreparedProvider(client=_Client(sprite), sprite_name="openenv-test")

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        provider.start_container("hf://openenv/echo_env")
        provider.stop_container()

    assert sprite.commands == []
    assert sprite.services == []
    assert sprite.destroyed


def test_readiness_timeout_records_failed_stage() -> None:
    sprite = _Sprite()
    provider = SpritesProvider(client=_Client(sprite), sprite_name="openenv-test")

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        base_url = provider.start_container("hf://openenv/echo_env")
        with pytest.raises(TimeoutError, match="was not ready"):
            provider.wait_for_ready(base_url, timeout_s=0)
        provider.stop_container()

    assert provider.diagnostics["failed_stage"] == "readiness"
    assert provider.diagnostics["error_type"] == "TimeoutError"


def test_readiness_fails_fast_when_sprite_token_is_rejected() -> None:
    sprite = _Sprite()
    provider = SpritesProvider(
        client=_Client(sprite, statuses=[401]),
        sprite_name="openenv-test",
    )

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        base_url = provider.start_container("hf://openenv/echo_env")
        with pytest.raises(SpritesProviderError, match="rejected the health check"):
            provider.wait_for_ready(base_url, timeout_s=10)
        provider.stop_container()

    assert provider.diagnostics["health_attempts"] == 1


def test_start_accepts_resource_options_and_safe_command_override() -> None:
    sprite = _Sprite()
    client = _Client(sprite)
    config = SpriteConfig(ram_mb=8192, cpus=4)
    url_settings = URLSettings(auth="sprite", private_access="admins")
    provider = SpritesProvider(
        client=client,
        sprite_name="openenv-test",
        sprite_config=config,
        url_settings=url_settings,
        sprite_runtime="dev",
    )

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        provider.start_container(
            "hf://openenv/echo_env",
            cmd="python -m uvicorn server.app:app --workers {workers}",
            workers=2,
        )
        provider.stop_container()

    assert client.created[0][1] == {
        "labels": ["openenv"],
        "wait_for_capacity": False,
        "config": config,
        "url_settings": url_settings,
        "runtime": "dev",
    }
    assert sprite.services[0]["cmd"] == "python"
    assert sprite.services[0]["args"][-2:] == ["--workers", "2"]


def test_workers_requires_explicit_command_placeholder() -> None:
    provider = SpritesProvider(
        client=_Client(_Sprite()),
        sprite_name="openenv-test",
    )

    with pytest.raises(ValueError, match=r"containing \{workers\}"):
        provider.start_container("hf://openenv/echo_env", workers=2)


@pytest.mark.parametrize("port", [0, -1, 65536, True, "8000"])
def test_start_rejects_invalid_ports(port: object) -> None:
    provider = SpritesProvider(
        client=_Client(_Sprite()),
        sprite_name="openenv-test",
    )

    with pytest.raises(ValueError, match="port must be between"):
        provider.start_container("hf://openenv/echo_env", port=port)  # type: ignore[arg-type]


def test_command_failures_redact_provider_token() -> None:
    sprite = _Sprite()
    sprite.command_result = _Result(
        returncode=1,
        stderr=b"request rejected for provider-secret-value",
    )
    client = _Client(sprite)
    client.token = "provider-secret-value"
    provider = SpritesProvider(client=client, sprite_name="openenv-test")

    with pytest.raises(SpritesProviderError) as caught:
        provider.start_container("hf://openenv/echo_env")

    assert "provider-secret-value" not in str(caught.value)
    assert "provider-secret-value" not in provider.diagnostics["error"]


def test_service_errors_redact_provider_and_environment_secrets() -> None:
    sprite = _Sprite()
    sprite.service_events = [
        _Event(
            type="error",
            data="token=provider-secret-value key=environment-secret-value",
        )
    ]
    client = _Client(sprite)
    client.token = "provider-secret-value"
    provider = SpritesProvider(client=client, sprite_name="openenv-test")

    with pytest.raises(SpritesProviderError) as caught:
        provider.start_container(
            "hf://openenv/echo_env",
            env_vars={"API_KEY": "environment-secret-value"},
        )

    assert "provider-secret-value" not in str(caught.value)
    assert "environment-secret-value" not in str(caught.value)
    assert "provider-secret-value" not in provider.diagnostics["error"]
    assert "environment-secret-value" not in provider.diagnostics["error"]


def test_retained_sprite_is_not_deleted() -> None:
    sprite = _Sprite()
    provider = SpritesProvider(
        client=_Client(sprite),
        sprite_name="openenv-test",
        delete_on_stop=False,
    )

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        provider.start_container("hf://openenv/echo_env")
        provider.stop_container()

    assert not sprite.destroyed
    assert provider.diagnostics["sprite_delete_attempted"] is False
    assert provider.diagnostics["sprite_deleted"] is False


def test_delete_retries_transient_server_failure() -> None:
    sprite = _Sprite()
    sprite.destroy_errors = [SpriteError("delete failed (status 500)")]
    provider = SpritesProvider(
        client=_Client(sprite),
        sprite_name="openenv-test",
        delete_retry_delay_s=0,
    )

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        provider.start_container("hf://openenv/echo_env")
        provider.stop_container()

    assert sprite.destroyed
    assert sprite.destroy_calls == 2
    assert provider.diagnostics["sprite_delete_attempts"] == 2
    assert provider.diagnostics["sprite_delete_outcome"] == "deleted"
    assert len(provider.diagnostics["sprite_delete_transient_errors"]) == 1


def test_delete_treats_not_found_as_already_absent() -> None:
    sprite = _Sprite()
    sprite.destroy_errors = [NotFoundError("not found")]
    provider = SpritesProvider(client=_Client(sprite), sprite_name="openenv-test")

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        provider.start_container("hf://openenv/echo_env")
        provider.stop_container()

    assert sprite.destroy_calls == 1
    assert provider.diagnostics["sprite_deleted"] is True
    assert provider.diagnostics["sprite_delete_outcome"] == "already_absent"


def test_cleanup_failure_does_not_mask_active_exception() -> None:
    sprite = _Sprite()
    sprite.destroy_errors = [AuthenticationError("bad token")]
    provider = SpritesProvider(client=_Client(sprite), sprite_name="openenv-test")

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        with pytest.raises(ValueError, match="action failed") as caught:
            with provider:
                provider.start_container("hf://openenv/echo_env")
                raise ValueError("action failed")

    assert sprite.destroy_calls == 1
    assert provider.diagnostics["sprite_deleted"] is False
    assert provider.diagnostics["sprite_delete_outcome"] == "failed"
    notes = getattr(caught.value, "__notes__", [])
    if hasattr(caught.value, "add_note"):
        assert any("Sprite cleanup also failed" in note for note in notes)
    else:
        assert notes == []


def test_bridge_stop_failure_does_not_prevent_sprite_deletion() -> None:
    sprite = _Sprite()
    provider = SpritesProvider(client=_Client(sprite), sprite_name="openenv-test")

    with patch(
        "openenv_sprites.provider._AuthenticatedWebSocketProxy",
        _FailingStopProxy,
    ):
        provider.start_container("hf://openenv/echo_env")
        with pytest.raises(SpritesProviderError, match="bridge stuck"):
            provider.stop_container()

    assert sprite.destroyed
    assert provider.diagnostics["sprite_deleted"] is True


def test_supplied_token_must_match_client_token() -> None:
    with pytest.raises(ValueError, match="must match"):
        SpritesProvider(
            client=_Client(_Sprite()),
            token="different-token",
            sprite_name="openenv-test",
        )


def test_diagnostics_returns_a_deep_copy() -> None:
    sprite = _Sprite()
    sprite.destroy_errors = [SpriteError("delete failed (status 500)")]
    provider = SpritesProvider(
        client=_Client(sprite),
        sprite_name="openenv-test",
        delete_retry_delay_s=0,
    )

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        provider.start_container("hf://openenv/echo_env")
        provider.stop_container()

    diagnostics = provider.diagnostics
    diagnostics["sprite_delete_transient_errors"].append("changed")
    assert "changed" not in provider.diagnostics["sprite_delete_transient_errors"]


def test_provider_context_deletes_active_sprite() -> None:
    sprite = _Sprite()
    provider = SpritesProvider(client=_Client(sprite), sprite_name="openenv-test")

    with patch("openenv_sprites.provider._AuthenticatedWebSocketProxy", _Proxy):
        with provider:
            provider.start_container("hf://openenv/echo_env")

    assert sprite.destroyed
    assert provider.sprite is None
