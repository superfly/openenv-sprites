"""A fork-free OpenEnv provider backed by a single Fly.io Sprite."""

from __future__ import annotations

import asyncio
import copy
import inspect
import os
import re
import secrets
import shlex
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from openenv.core.containers.runtime import ContainerProvider

if TYPE_CHECKING:
    from sprites import Sprite, SpritesClient
    from sprites.types import SpriteConfig, URLSettings


class SpritesProviderError(RuntimeError):
    """Raised when a Sprite cannot be provisioned as an OpenEnv runtime."""


@dataclass(frozen=True)
class _Source:
    clone_url: str
    revision: str | None = None


class _AuthenticatedWebSocketProxy:
    """Loopback WebSocket bridge that keeps the Sprites token client-side.

    OpenEnv's client currently has no constructor option for WebSocket headers.
    The provider therefore returns a loopback URL and this bridge adds the bearer
    token only to the upstream connection. It intentionally proxies WebSockets
    only; readiness checks go directly through the authenticated Sprites SDK.
    """

    def __init__(
        self,
        upstream_base_url: str,
        token: str,
        *,
        max_message_size_mb: float = 100.0,
        open_timeout_s: float = 10.0,
        close_timeout_s: float = 10.0,
    ) -> None:
        self._upstream_base_url = upstream_base_url.rstrip("/")
        self._token = token
        self._max_message_size = int(max_message_size_mb * 1024 * 1024)
        self._open_timeout_s = open_timeout_s
        self._close_timeout_s = close_timeout_s
        self._capability = secrets.token_urlsafe(24)
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._port: int | None = None
        self._error: BaseException | None = None
        self._connection_errors = 0

    def start(self, timeout_s: float = 10.0) -> str:
        if self._thread is not None:
            raise RuntimeError("WebSocket proxy has already been started")

        self._thread = threading.Thread(
            target=self._thread_main,
            name="openenv-sprites-ws-proxy",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout_s):
            raise SpritesProviderError("timed out starting the local WebSocket proxy")
        if self._error is not None:
            raise SpritesProviderError(
                "failed to start local WebSocket proxy"
            ) from self._error
        assert self._port is not None
        return f"http://127.0.0.1:{self._port}/{self._capability}"

    def stop(self, timeout_s: float = 10.0) -> None:
        loop = self._loop
        stop_event = self._stop_event
        thread = self._thread
        if loop is not None and stop_event is not None and loop.is_running():
            loop.call_soon_threadsafe(stop_event.set)
        if thread is not None and thread.is_alive():
            thread.join(timeout_s)
        if thread is not None and thread.is_alive():
            raise SpritesProviderError(
                "timed out stopping the local WebSocket proxy"
            )
        self._thread = None

    @property
    def diagnostics(self) -> dict[str, int]:
        """Return non-secret bridge diagnostics."""

        return {"connection_errors": self._connection_errors}

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # surfaced synchronously by start()
            self._error = exc
            self._ready.set()

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        async with serve(
            self._handle_connection,
            "127.0.0.1",
            0,
            max_size=self._max_message_size,
            close_timeout=self._close_timeout_s,
        ) as server:
            sockets = server.sockets
            if not sockets:
                raise SpritesProviderError("WebSocket proxy did not bind a socket")
            self._port = int(sockets[0].getsockname()[1])
            self._ready.set()
            await self._stop_event.wait()

    async def _handle_connection(self, incoming: Any) -> None:
        from websockets.asyncio.client import connect

        request = getattr(incoming, "request", None)
        path = getattr(request, "path", None) or ""
        expected_path = f"/{self._capability}/ws"
        if urlsplit(path).path != expected_path:
            await incoming.close(code=1008, reason="only /ws is supported")
            return

        upstream_url = _as_websocket_url(f"{self._upstream_base_url}/ws")
        header_name = (
            "additional_headers"
            if "additional_headers" in inspect.signature(connect).parameters
            else "extra_headers"
        )
        kwargs = {
            header_name: {"Authorization": f"Bearer {self._token}"},
            "max_size": self._max_message_size,
            "open_timeout": self._open_timeout_s,
            "close_timeout": self._close_timeout_s,
        }

        try:
            async with connect(upstream_url, **kwargs) as upstream:
                incoming_to_upstream = asyncio.create_task(
                    self._relay(incoming, upstream)
                )
                upstream_to_incoming = asyncio.create_task(
                    self._relay(upstream, incoming)
                )
                done, pending = await asyncio.wait(
                    {incoming_to_upstream, upstream_to_incoming},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                results = await asyncio.gather(*done, return_exceptions=True)
                from websockets.exceptions import ConnectionClosed

                unexpected = [
                    result
                    for result in results
                    if isinstance(result, BaseException)
                    and not isinstance(result, ConnectionClosed)
                ]
                if unexpected:
                    raise unexpected[0]
        except Exception:
            self._connection_errors += 1
            try:
                await incoming.close(code=1011, reason="upstream connection failed")
            except Exception:
                pass

    @staticmethod
    async def _relay(source: Any, destination: Any) -> None:
        async for message in source:
            await destination.send(message)


class SpritesProvider(ContainerProvider):
    """Run one OpenEnv server in one newly-created Sprite.

    The OpenEnv ``image`` argument is interpreted as a source checkout, not an
    OCI image. Supported forms are an HTTPS Git URL, ``git+https://...``, or
    ``hf://<owner>/<space>[@revision]``.

    This implementation deliberately has no pool or cross-Sprite checkpoint
    semantics. Every call creates a fresh Sprite and installs the source.
    ``delete_on_stop=False`` is available for debugging, but retained Sprites
    are not automatically reset before reuse.
    """

    def __init__(
        self,
        *,
        source: str | None = None,
        token: str | None = None,
        api_base_url: str = "https://api.sprites.dev",
        sprite_name: str | None = None,
        sprite_name_prefix: str = "openenv",
        service_name: str = "openenv",
        project_dir: str = "/srv/openenv",
        revision: str | None = None,
        uv_version: str = "0.12.6",
        uv_install_dir: str = "/opt/openenv/bin",
        server_command: Sequence[str] = (
            "{uv_bin}",
            "run",
            "server",
            "--host",
            "0.0.0.0",
            "--port",
            "{port}",
        ),
        dependency_command: Sequence[str] = ("{uv_bin}", "sync"),
        frozen_dependencies: bool = True,
        health_path: str = "/health",
        readiness_poll_interval_s: float = 0.5,
        readiness_request_timeout_s: float = 5.0,
        include_health_body_in_diagnostics: bool = False,
        bridge_max_message_size_mb: float = 100.0,
        bridge_open_timeout_s: float = 10.0,
        bridge_close_timeout_s: float = 10.0,
        clone_timeout_s: float = 300.0,
        uv_download_timeout_s: float = 120.0,
        uv_install_timeout_s: float = 300.0,
        dependency_sync_timeout_s: float = 900.0,
        delete_on_stop: bool = True,
        delete_attempts: int = 4,
        delete_retry_delay_s: float = 0.5,
        wait_for_capacity: bool = False,
        labels: Sequence[str] = ("openenv",),
        sprite_config: SpriteConfig | None = None,
        url_settings: URLSettings | None = None,
        sprite_runtime: str | None = None,
        client: SpritesClient | None = None,
    ) -> None:
        environment_token = os.environ.get("SPRITES_API_TOKEN") or os.environ.get(
            "SPRITE_TOKEN"
        )
        client_token = getattr(client, "token", None)
        if token and client_token and token != client_token:
            raise ValueError("token= must match the supplied client's token")
        resolved_token = (
            (token or client_token)
            if client is not None
            else (token or environment_token)
        )
        if not resolved_token:
            raise ValueError(
                "a Sprites token is required; pass token=, supply a token-bearing "
                "client, or set SPRITES_API_TOKEN"
            )
        if not server_command:
            raise ValueError("server_command must contain at least one argument")
        if not dependency_command:
            raise ValueError("dependency_command must contain at least one argument")
        if not health_path.startswith("/") or "?" in health_path or "#" in health_path:
            raise ValueError("health_path must be an absolute URL path")
        if not project_dir.startswith("/") or project_dir == "/":
            raise ValueError("project_dir must be an absolute, non-root path")
        if not uv_install_dir.startswith("/") or uv_install_dir == "/":
            raise ValueError("uv_install_dir must be an absolute, non-root path")
        for name, value in {
            "readiness_poll_interval_s": readiness_poll_interval_s,
            "readiness_request_timeout_s": readiness_request_timeout_s,
            "bridge_max_message_size_mb": bridge_max_message_size_mb,
            "bridge_open_timeout_s": bridge_open_timeout_s,
            "bridge_close_timeout_s": bridge_close_timeout_s,
            "clone_timeout_s": clone_timeout_s,
            "uv_download_timeout_s": uv_download_timeout_s,
            "uv_install_timeout_s": uv_install_timeout_s,
            "dependency_sync_timeout_s": dependency_sync_timeout_s,
        }.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if delete_attempts < 1:
            raise ValueError("delete_attempts must be at least 1")
        if delete_retry_delay_s < 0:
            raise ValueError("delete_retry_delay_s cannot be negative")
        if sprite_runtime not in {None, "default", "dev"}:
            raise ValueError("sprite_runtime must be 'default', 'dev', or None")
        invalid_labels = any(
            not isinstance(label, str) or not label for label in labels
        )
        if not labels or invalid_labels:
            raise ValueError("labels must contain at least one non-empty string")
        if sprite_name is not None:
            _validate_sprite_name(sprite_name)
        _validate_sprite_name_prefix(sprite_name_prefix)

        self.source = source
        self._token = resolved_token or getattr(client, "token", "")
        self.api_base_url = api_base_url.rstrip("/")
        self.sprite_name = sprite_name
        self.sprite_name_prefix = sprite_name_prefix
        self.service_name = service_name
        self.project_dir = project_dir.rstrip("/")
        self.revision = revision
        self.uv_version = uv_version
        self.uv_install_dir = uv_install_dir.rstrip("/")
        self.server_command = tuple(server_command)
        self.dependency_command = tuple(dependency_command)
        self.frozen_dependencies = frozen_dependencies
        self.health_path = health_path
        self.readiness_poll_interval_s = readiness_poll_interval_s
        self.readiness_request_timeout_s = readiness_request_timeout_s
        self.include_health_body_in_diagnostics = include_health_body_in_diagnostics
        self.bridge_max_message_size_mb = bridge_max_message_size_mb
        self.bridge_open_timeout_s = bridge_open_timeout_s
        self.bridge_close_timeout_s = bridge_close_timeout_s
        self.clone_timeout_s = clone_timeout_s
        self.uv_download_timeout_s = uv_download_timeout_s
        self.uv_install_timeout_s = uv_install_timeout_s
        self.dependency_sync_timeout_s = dependency_sync_timeout_s
        self.delete_on_stop = delete_on_stop
        self.delete_attempts = delete_attempts
        self.delete_retry_delay_s = delete_retry_delay_s
        self.wait_for_capacity = wait_for_capacity
        self.labels = list(labels)
        self.sprite_config = sprite_config
        self.url_settings = url_settings
        self.sprite_runtime = sprite_runtime

        self._client = client
        self._owns_client = client is None
        self._sprite: Sprite | None = None
        self._proxy: _AuthenticatedWebSocketProxy | None = None
        self._upstream_base_url: str | None = None
        self._timings: dict[str, float] = {}
        self._diagnostics: dict[str, Any] = {}

    @property
    def sprite(self) -> Sprite | None:
        """The active Sprite handle, if the provider has been started."""

        return self._sprite

    @property
    def timings(self) -> dict[str, float]:
        """Completed provisioning-stage durations in seconds."""

        return dict(self._timings)

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Non-secret details from the most recent lifecycle or failure."""

        return copy.deepcopy(self._diagnostics)

    def start_container(
        self,
        image: str | None = None,
        port: int | None = None,
        env_vars: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> str:
        """Create and provision a Sprite, returning a loopback OpenEnv URL."""

        command_override = kwargs.pop("cmd", None)
        workers = kwargs.pop("workers", 1)
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            raise ValueError(f"unsupported SpritesProvider options: {unknown}")
        if self._sprite is not None:
            raise RuntimeError("this SpritesProvider already owns a running Sprite")
        if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
            raise ValueError("workers must be a positive integer")

        source = _parse_source(image or self.source, self.revision)
        service_port = 8000 if port is None else port
        if (
            not isinstance(service_port, int)
            or isinstance(service_port, bool)
            or not 1 <= service_port <= 65535
        ):
            raise ValueError("port must be between 1 and 65535")
        service_command = (
            _normalize_command(command_override)
            if command_override is not None
            else self.server_command
        )
        if workers != 1 and not any("{workers}" in part for part in service_command):
            raise ValueError(
                "workers requires a server_command or cmd containing {workers}"
            )
        client = self._get_client()
        name = self.sprite_name or _new_sprite_name(self.sprite_name_prefix)
        provision_started = time.monotonic()
        self._timings = {}
        self._diagnostics = {
            "sprite_name": name,
            "source": source.clone_url,
            "source_revision": source.revision,
        }

        try:
            with self._stage("sprite_create"):
                sprite, needs_preparation = self._allocate_sprite(client, name)
            self._sprite = sprite
            if needs_preparation:
                self._prepare_sprite(
                    sprite,
                    source,
                    service_port,
                    env_vars or {},
                    service_command,
                    workers,
                )

            sprite_url = getattr(sprite, "url", None)
            if not sprite_url:
                raise SpritesProviderError(
                    "Sprites API did not return the Sprite's user-service URL"
                )
            # The direct Sprite URL replays with a proxy:: token, causing
            # sprite-env to route /health and /ws to the configured HTTP
            # service. The API catch-all currently replays a plain control
            # token, under which /health is an unknown sprite-env API route.
            self._upstream_base_url = sprite_url.rstrip("/")
            self._diagnostics["upstream_url"] = self._upstream_base_url
            with self._stage("websocket_bridge"):
                self._proxy = _AuthenticatedWebSocketProxy(
                    self._upstream_base_url,
                    self._token,
                    max_message_size_mb=self.bridge_max_message_size_mb,
                    open_timeout_s=self.bridge_open_timeout_s,
                    close_timeout_s=self.bridge_close_timeout_s,
                )
                return self._proxy.start(timeout_s=self.bridge_open_timeout_s)
        except BaseException:
            self._cleanup_after_failed_start()
            raise
        finally:
            self._timings["provision_total"] = round(
                time.monotonic() - provision_started, 3
            )

    def wait_for_ready(self, base_url: str, timeout_s: float = 180.0) -> None:
        """Poll the OpenEnv server's authenticated ``/health`` endpoint."""

        # OpenEnv passes the loopback bridge URL. Readiness must use the
        # authenticated direct Sprite URL because the bridge is WebSocket-only.
        del base_url
        if self._upstream_base_url is None:
            raise RuntimeError("provider has not been started")

        deadline = time.monotonic() + timeout_s
        health_url = f"{self._upstream_base_url}{self.health_path}"
        last_status: int | None = None
        last_error: Exception | None = None
        last_body = ""
        health_attempts = 0
        transient_errors = 0
        with self._stage("readiness"):
            while time.monotonic() < deadline:
                health_attempts += 1
                self._diagnostics["health_attempts"] = health_attempts
                try:
                    response = self._get_client().http_client.get(
                        health_url,
                        timeout=min(
                            self.readiness_request_timeout_s,
                            max(0.1, deadline - time.monotonic()),
                        ),
                    )
                    last_status = response.status_code
                    last_body = str(getattr(response, "text", ""))[:500]
                    self._diagnostics["last_health_status"] = last_status
                    if self.include_health_body_in_diagnostics:
                        self._diagnostics["last_health_body"] = last_body
                    if response.status_code == 200:
                        self._diagnostics.pop("last_health_error", None)
                        return
                    if response.status_code in {401, 403}:
                        raise SpritesProviderError(
                            f"Sprites rejected the health check with HTTP "
                            f"{response.status_code}; verify the token can access "
                            f"Sprite {self._diagnostics['sprite_name']!r}"
                        )
                except SpritesProviderError:
                    raise
                except Exception as exc:
                    last_error = exc
                    transient_errors += 1
                    self._diagnostics["health_transient_errors"] = transient_errors
                    self._diagnostics["last_health_error"] = str(exc)
                time.sleep(self.readiness_poll_interval_s)

            detail = (
                f"last HTTP status was {last_status}"
                if last_status
                else str(last_error)
            )
            service_diagnostic = self._service_diagnostic()
            if service_diagnostic:
                self._diagnostics["service"] = service_diagnostic
                detail += f", service={service_diagnostic}"
            if last_body and self.include_health_body_in_diagnostics:
                detail += f", response={last_body!r}"
            raise TimeoutError(
                f"OpenEnv server at {health_url} was not ready within "
                f"{timeout_s}s ({detail})"
            )

    def stop_container(self) -> None:
        """Stop the bridge and delete the Sprite when this provider owns it."""

        proxy, self._proxy = self._proxy, None
        sprite, self._sprite = self._sprite, None
        self._upstream_base_url = None
        cleanup_errors: list[str] = []
        if proxy is not None:
            try:
                proxy.stop(timeout_s=self.bridge_close_timeout_s)
            except Exception as exc:
                detail = self._redact(f"{type(exc).__name__}: {exc}")
                cleanup_errors.append(f"bridge: {detail}")
                self._diagnostics["bridge_stop_error"] = detail
            finally:
                self._diagnostics["bridge"] = proxy.diagnostics
        try:
            if sprite is not None and self.delete_on_stop:
                self._diagnostics["sprite_delete_attempted"] = True
                try:
                    self._destroy_sprite(sprite)
                except Exception as exc:
                    self._diagnostics["sprite_deleted"] = False
                    detail = self._redact(str(exc)[:2000])
                    self._diagnostics["sprite_delete_error"] = detail
                    cleanup_errors.append(f"sprite: {detail}")
                else:
                    self._diagnostics["sprite_deleted"] = True
                    self._diagnostics.pop("sprite_delete_error", None)
            elif sprite is not None:
                self._diagnostics["sprite_delete_attempted"] = False
                self._diagnostics["sprite_deleted"] = False
        finally:
            # EnvClient.close() calls stop_container(), but doesn't call the
            # provider's close() hook. Release an internally-created SDK client
            # here so normal OpenEnv context-manager use doesn't leak sockets.
            if self._owns_client and self._client is not None:
                try:
                    self._client.close()
                except Exception as exc:
                    detail = self._redact(f"{type(exc).__name__}: {exc}")
                    cleanup_errors.append(f"client: {detail}")
                    self._diagnostics["client_close_error"] = detail
                finally:
                    self._client = None

        if cleanup_errors:
            raise SpritesProviderError(
                "Sprite lifecycle cleanup failed: " + "; ".join(cleanup_errors)
            )

    def close(self) -> None:
        """Release the Sprite and any SDK resources held by the provider."""

        self.stop_container()

    def __enter__(self) -> SpritesProvider:
        """Enter a provider-managed lifecycle across OpenEnv versions."""

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        """Release the active Sprite when leaving the provider context."""

        try:
            self.close()
        except Exception as cleanup_error:
            if exc_value is None:
                raise
            # Never replace the environment/action exception with a secondary
            # cleanup failure. Python 3.11+ will display this note with the
            # original exception; diagnostics retain it on all versions.
            if hasattr(exc_value, "add_note"):
                exc_value.add_note(f"Sprite cleanup also failed: {cleanup_error}")

    def _get_client(self) -> SpritesClient:
        if self._client is None:
            from sprites import SpritesClient

            self._client = SpritesClient(self._token, base_url=self.api_base_url)
        return self._client

    def _allocate_sprite(
        self, client: SpritesClient, name: str
    ) -> tuple[Sprite, bool]:
        """Allocate a runtime and report whether it still needs preparation.

        This boundary deliberately contains today's fresh-Sprite behavior. A
        future fork-backed implementation can return ``(fork, False)`` without
        changing the OpenEnv-facing lifecycle or introducing an idle pool.
        """

        create_options: dict[str, Any] = {
            "labels": self.labels,
            "wait_for_capacity": self.wait_for_capacity,
        }
        if self.sprite_config is not None:
            create_options["config"] = self.sprite_config
        if self.url_settings is not None:
            create_options["url_settings"] = self.url_settings
        if self.sprite_runtime is not None:
            create_options["runtime"] = self.sprite_runtime
        return client.create_sprite(name, **create_options), True

    def _prepare_sprite(
        self,
        sprite: Sprite,
        source: _Source,
        service_port: int,
        env_vars: Mapping[str, str],
        service_command: Sequence[str],
        workers: int,
    ) -> None:
        """Install an environment and register its server in a fresh Sprite."""

        self._bootstrap_source(sprite, source)
        with self._stage("service_register"):
            self._create_service(
                sprite,
                service_port,
                env_vars,
                service_command,
                workers,
            )

    def _destroy_sprite(self, sprite: Sprite) -> None:
        """Delete a Sprite, retrying failures that are safe to retry."""

        from sprites.exceptions import NotFoundError

        errors: list[str] = []
        for attempt in range(1, self.delete_attempts + 1):
            self._diagnostics["sprite_delete_attempts"] = attempt
            try:
                sprite.destroy()
            except NotFoundError:
                # DELETE is idempotent from the provider's perspective.
                self._diagnostics["sprite_delete_outcome"] = "already_absent"
                self._diagnostics["sprite_delete_transient_errors"] = errors
                return
            except Exception as exc:
                detail = self._redact(f"{type(exc).__name__}: {exc}"[:2000])
                errors.append(detail)
                self._diagnostics["sprite_delete_transient_errors"] = list(errors)
                if attempt == self.delete_attempts or not _retryable_delete_error(
                    exc
                ):
                    self._diagnostics["sprite_delete_outcome"] = "failed"
                    sprite_name = getattr(sprite, "name", "<unknown>")
                    raise SpritesProviderError(
                        f"failed to delete Sprite {sprite_name!r} "
                        f"after {attempt} attempt(s): {exc}"
                    ) from exc

                delay = self.delete_retry_delay_s * (2 ** (attempt - 1))
                # Jitter prevents concurrent lifecycle cleanup from retrying
                # against the same org metadata at exactly the same instant.
                jitter = 0.75 + (secrets.randbelow(501) / 1000)
                time.sleep(delay * jitter)
            else:
                self._diagnostics["sprite_delete_outcome"] = "deleted"
                self._diagnostics["sprite_delete_transient_errors"] = errors
                return

    def _bootstrap_source(self, sprite: Sprite, source: _Source) -> None:
        with self._stage("source_clone"):
            if source.revision:
                self._run_checked(
                    sprite,
                    ["git", "init", self.project_dir],
                    timeout_s=self.clone_timeout_s,
                )
                self._run_checked(
                    sprite,
                    [
                        "git",
                        "-C",
                        self.project_dir,
                        "remote",
                        "add",
                        "origin",
                        source.clone_url,
                    ],
                    timeout_s=self.clone_timeout_s,
                )
                self._run_checked(
                    sprite,
                    [
                        "git",
                        "-C",
                        self.project_dir,
                        "fetch",
                        "--depth",
                        "1",
                        "origin",
                        source.revision,
                    ],
                    timeout_s=self.clone_timeout_s,
                )
                self._run_checked(
                    sprite,
                    [
                        "git",
                        "-C",
                        self.project_dir,
                        "checkout",
                        "--detach",
                        "FETCH_HEAD",
                    ],
                    timeout_s=self.clone_timeout_s,
                )
            else:
                self._run_checked(
                    sprite,
                    [
                        "git",
                        "clone",
                        "--depth",
                        "1",
                        source.clone_url,
                        self.project_dir,
                    ],
                    timeout_s=self.clone_timeout_s,
                )

            commit = self._run_checked(
                sprite,
                ["git", "rev-parse", "HEAD"],
                cwd=self.project_dir,
                timeout_s=30.0,
            )
            self._diagnostics["source_commit"] = _decode_output(
                getattr(commit, "stdout", b"")
            ).strip()

        installer_path = "/tmp/openenv-uv-install.sh"
        installer_url = f"https://astral.sh/uv/{self.uv_version}/install.sh"
        with self._stage("uv_install"):
            self._run_checked(
                sprite,
                ["curl", "-LsSf", installer_url, "-o", installer_path],
                timeout_s=self.uv_download_timeout_s,
            )
            self._run_checked(
                sprite,
                [
                    "env",
                    f"UV_UNMANAGED_INSTALL={self.uv_install_dir}",
                    "sh",
                    installer_path,
                ],
                timeout_s=self.uv_install_timeout_s,
            )
        uv_bin = f"{self.uv_install_dir}/uv"
        self._run_checked(sprite, [uv_bin, "--version"], timeout_s=30.0)
        dependency_command = _format_command(
            self.dependency_command,
            port=8000,
            project_dir=self.project_dir,
            uv_bin=uv_bin,
            workers=1,
        )
        lock_check = sprite.run(
            "test",
            "-f",
            f"{self.project_dir}/uv.lock",
            capture_output=True,
            timeout=30.0,
        )
        has_lockfile = lock_check.returncode == 0
        self._diagnostics["dependency_lockfile"] = has_lockfile
        if (
            self.frozen_dependencies
            and has_lockfile
            and dependency_command[:2] == [uv_bin, "sync"]
            and "--frozen" not in dependency_command
        ):
            dependency_command.append("--frozen")
            self._diagnostics["dependency_mode"] = "frozen"
        else:
            self._diagnostics["dependency_mode"] = "unfrozen"

        with self._stage("dependency_sync"):
            self._run_checked(
                sprite,
                dependency_command,
                cwd=self.project_dir,
                timeout_s=self.dependency_sync_timeout_s,
            )

    def _create_service(
        self,
        sprite: Sprite,
        port: int,
        env_vars: Mapping[str, str],
        service_command: Sequence[str],
        workers: int,
    ) -> None:
        command = _format_command(
            service_command,
            port=port,
            project_dir=self.project_dir,
            uv_bin=f"{self.uv_install_dir}/uv",
            workers=workers,
        )
        stream = sprite.create_service(
            service_name=self.service_name,
            cmd=command[0],
            args=command[1:],
            env=dict(env_vars),
            dir=self.project_dir,
            http_port=port,
            duration=2.0,
        )
        errors = []
        for event in stream:
            if getattr(event, "type", None) == "error":
                errors.append(getattr(event, "data", None) or "unknown service error")
            exit_code = getattr(event, "exit_code", None)
            if exit_code not in (None, 0):
                errors.append(f"service exited with status {exit_code}")
        if errors:
            detail = self._redact("; ".join(errors), env_vars.values())
            raise SpritesProviderError(detail)

    def _run_checked(
        self,
        sprite: Sprite,
        args: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_s: float,
    ) -> Any:
        result = sprite.run(
            *args,
            capture_output=True,
            timeout=timeout_s,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result
        output = (result.stdout or b"") + (result.stderr or b"")
        detail = output.decode("utf-8", errors="replace").strip()
        message = (
            f"Sprite command failed ({result.returncode}): {' '.join(args)}\n{detail}"
        )
        raise SpritesProviderError(self._redact(message))

    def _redact(
        self, value: str, additional_secrets: Iterable[str] = ()
    ) -> str:
        secrets_to_redact = [self._token, *list(additional_secrets)]
        redacted = value
        for secret in secrets_to_redact:
            if isinstance(secret, str) and len(secret) >= 8:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted

    def _cleanup_after_failed_start(self) -> None:
        try:
            self.stop_container()
        except Exception:
            # Preserve the provisioning failure, which is usually more actionable.
            pass

    @contextmanager
    def _stage(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        except BaseException as exc:
            self._diagnostics["failed_stage"] = name
            self._diagnostics["error_type"] = type(exc).__name__
            self._diagnostics["error"] = self._redact(str(exc)[:2000])
            raise
        finally:
            self._timings[name] = round(time.monotonic() - started, 3)

    def _service_diagnostic(self) -> dict[str, Any]:
        sprite = self._sprite
        if sprite is None or not hasattr(sprite, "get_service"):
            return {}
        try:
            service = sprite.get_service(self.service_name)
            state = getattr(service, "state", None)
            if state is None:
                return {"state": None}
            service_error = getattr(state, "error", None)
            return {
                "status": getattr(state, "status", None),
                "error": (
                    self._redact(service_error)
                    if isinstance(service_error, str)
                    else service_error
                ),
                "restart_count": getattr(state, "restart_count", None),
            }
        except Exception as exc:
            return {"lookup_error": self._redact(str(exc))}


def _parse_source(value: str | None, revision: str | None = None) -> _Source:
    if not value:
        raise ValueError(
            "an OpenEnv source is required; pass image to start_container() "
            "or source= to SpritesProvider"
        )

    if value.startswith("registry.hf.space/"):
        raise ValueError(
            "SpritesProvider expects a source checkout, not an OCI image; use "
            "hf://<owner>/<space> or the Space's Git URL"
        )

    if value.startswith("hf://"):
        repo_and_revision = value.removeprefix("hf://")
        if "@" in repo_and_revision:
            repo, inline_revision = repo_and_revision.rsplit("@", 1)
            revision = revision or inline_revision
        else:
            repo = repo_and_revision
        if repo.count("/") != 1 or any(not part for part in repo.split("/")):
            raise ValueError("HF sources must use hf://<owner>/<space>[@revision]")
        _validate_revision(revision)
        return _Source(
            clone_url=f"https://huggingface.co/spaces/{repo}",
            revision=revision,
        )

    clone_url = value.removeprefix("git+")
    parsed = urlsplit(clone_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("source must be an HTTPS Git URL or hf:// Space reference")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "source URLs must not contain credentials; configure repository "
            "access separately"
        )
    if parsed.query or parsed.fragment:
        raise ValueError("source URLs must not contain a query string or fragment")
    _validate_revision(revision)
    return _Source(clone_url=clone_url, revision=revision)


def _new_sprite_name(prefix: str) -> str:
    cleaned = prefix.strip("-") or "openenv"
    name = f"{cleaned[:50].rstrip('-')}-{secrets.token_hex(6)}"
    _validate_sprite_name(name)
    return name


def _as_websocket_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http":
        scheme = "ws"
    elif parsed.scheme in {"ws", "wss"}:
        scheme = parsed.scheme
    else:
        raise ValueError(f"unsupported upstream URL scheme: {parsed.scheme}")
    return urlunsplit(
        (scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _retryable_delete_error(exc: Exception) -> bool:
    """Return whether a Sprites deletion failure is likely transient."""

    from sprites.exceptions import NetworkError

    if isinstance(exc, NetworkError):
        return True
    match = re.search(r"\bstatus (\d{3})\b", str(exc))
    if match is None:
        return False
    status = int(match.group(1))
    return status in {408, 409, 425, 429} or status >= 500


def _normalize_command(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        command = tuple(shlex.split(value))
    else:
        command = tuple(value)
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("cmd must contain at least one non-empty argument")
    return command


def _format_command(
    command: Sequence[str],
    *,
    port: int,
    project_dir: str,
    uv_bin: str,
    workers: int,
) -> list[str]:
    values = {
        "port": port,
        "project_dir": project_dir,
        "uv_bin": uv_bin,
        "workers": workers,
    }
    try:
        return [part.format(**values) for part in command]
    except (IndexError, KeyError, ValueError) as exc:
        raise ValueError(
            "command templates may use only {port}, {project_dir}, "
            "{uv_bin}, and {workers}"
        ) from exc


def _decode_output(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _validate_revision(revision: str | None) -> None:
    if revision is None:
        return
    if not revision or revision.startswith("-") or any(
        ord(character) < 32 for character in revision
    ):
        raise ValueError(
            "revision must be a non-empty Git ref without control characters"
        )


def _validate_sprite_name(value: str) -> None:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ValueError(
            "sprite_name must be a lowercase DNS label of at most 63 characters"
        )


def _validate_sprite_name_prefix(value: str) -> None:
    candidate = value.strip("-")
    invalid = not candidate or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]*", candidate
    )
    if invalid or len(candidate) > 50:
        raise ValueError(
            "sprite_name_prefix must be at most 50 lowercase letters, digits, or '-'"
        )
