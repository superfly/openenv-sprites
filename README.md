# OpenEnv on Sprites

`openenv-sprites` is an experimental OpenEnv `ContainerProvider` backed by
[Fly.io Sprites](https://sprites.dev/). It provides a fresh, private Sprite for
each environment lifecycle; it does not maintain an idle pool and does not
depend on Sprite forking or cross-Sprite checkpoint restore.

The provider has been exercised with the OpenEnv Echo and Coding environments,
including two concurrent Coding lifecycles. In that run both environments
preserved their own session state, remained isolated, and were deleted after
use. The slower of the two cold environments reached readiness in 34.6 seconds;
Sprite creation itself took about 0.25 seconds, while source/dependency setup
dominated startup.

## Lifecycle

Each provider instance owns at most one Sprite:

1. Create a fresh authenticated Sprite.
2. Clone an OpenEnv Space or HTTPS Git repository.
3. Resolve and record the exact Git commit.
4. Install pinned `uv` and run dependency synchronization. A present `uv.lock`
   is honored with `uv sync --frozen` by default.
5. Register the environment's `server` entry point as a Sprite HTTP service.
6. Poll the authenticated health endpoint to keep cold-start traffic active.
7. Relay OpenEnv WebSockets through a capability-protected loopback bridge.
8. Delete the Sprite with bounded retries when the client closes.

Allocation and preparation are separate internal boundaries. Today allocation
returns a blank Sprite that requires preparation. A future implementation can
return a prepared fork without changing the OpenEnv-facing lifecycle.

## Install and run

From this checkout, install this package and its development dependencies:

```console
uv sync --extra dev
```

Then add or inject the client package for the desired environment, for example:

```console
uv add "openenv-echo-env @ git+https://huggingface.co/spaces/openenv/echo_env"
```

Published environment packages currently span several OpenEnv core versions.
The explicit lifecycle works across those versions:

```python
from echo_env import EchoEnv
from openenv_sprites import SpritesProvider

provider = SpritesProvider(source="hf://openenv/echo_env")

with provider:
    base_url = provider.start_container()
    provider.wait_for_ready(base_url)

    with EchoEnv(base_url=base_url).sync() as env:
        result = env.reset()
```

With a current OpenEnv client, the native provider-owned async lifecycle is:

```python
from echo_env import EchoEnv
from openenv_sprites import SpritesProvider

provider = SpritesProvider()
env = await EchoEnv.from_docker_image(
    "hf://openenv/echo_env",
    provider=provider,
)

async with env:
    result = await env.reset()
```

The `image` parameter is provider-specific here. Accepted source identifiers
are:

- `hf://<owner>/<space>[@revision]`
- `https://...` Git URLs
- `git+https://...` Git URLs

Credential-bearing Git URLs, query strings, and fragments are rejected to keep
secrets out of commands and diagnostics. Arbitrary OCI images are not supported.

## Authentication

Set `SPRITES_API_TOKEN` or pass `token=`. The legacy `SPRITE_TOKEN` environment
variable is accepted as a fallback. For deployed use, prefer a restricted
Sprites token whose policy constrains all of the following:

- name prefix `openenv-`;
- required label `openenv`;
- an appropriate total Sprite limit; and
- an expiration time.

The token remains in the client process. It is never copied into the Sprite or
returned in diagnostics. The local WebSocket URL includes a random, per-provider
capability path, binds only to `127.0.0.1`, and injects the bearer token only on
the upstream connection. The Python SDK does not yet expose mint/revoke methods
for restricted tokens, so token provisioning remains the caller's responsibility.

## Configuration

The common constructor options are:

| Option | Default | Purpose |
| --- | --- | --- |
| `source` | none | Source used when `start_container(image=...)` omits `image`. |
| `revision` | none | Git branch, tag, or commit to fetch. |
| `sprite_name_prefix` | `openenv` | Prefix for generated names and restricted-token policies. |
| `labels` | `("openenv",)` | Labels applied at Sprite creation. |
| `sprite_config` | API default | Sprites CPU, RAM, region, and storage configuration. |
| `sprite_runtime` | API default | `default` or `dev` Sprite runtime. |
| `url_settings` | API default | Sprite URL authentication settings. |
| `server_command` | `uv run server ...` | Service command with template placeholders. |
| `dependency_command` | `uv sync` | Dependency setup command. |
| `frozen_dependencies` | `True` | Add `--frozen` when the default sync finds `uv.lock`. |
| `health_path` | `/health` | Authenticated readiness endpoint. |
| `bridge_max_message_size_mb` | `100` | Maximum WebSocket message size in both directions. |
| `delete_attempts` | `4` | Maximum idempotent cleanup attempts. |
| `delete_on_stop` | `True` | Set false only when retaining a Sprite for debugging. |

Commands are executed directly as argument arrays, not through a shell. Command
templates may use `{uv_bin}`, `{project_dir}`, `{port}`, and `{workers}`. A
per-start `cmd=` override accepts either a shell-like string parsed with
`shlex.split` or a sequence of arguments. `workers` greater than one requires an
explicit `{workers}` placeholder.

Clone, installer, dependency, bridge, and readiness timeouts are separately
configurable; cleanup has bounded retry and backoff controls. `provider.timings`
and `provider.diagnostics`
return defensive copies and never intentionally contain supplied credentials or
environment-variable values. Health response bodies are excluded unless
`include_health_body_in_diagnostics=True` is explicitly selected.

## Examples

```console
uv run --with "openenv-echo-env @ git+https://huggingface.co/spaces/openenv/echo_env" \
python examples/run_echo.py
```

```console
uv run --with "openenv-coding-env @ git+https://huggingface.co/spaces/openenv/coding_env" \
python examples/run_coding.py
```

The concurrent fresh-lifecycle harness creates and deletes exactly two new
Sprites per round:

```console
uv run --with "openenv-coding-env @ git+https://huggingface.co/spaces/openenv/coding_env" \
python examples/run_fresh_lifecycle.py --rounds 1
```

All commands require `SPRITES_API_TOKEN` in the environment.

## Current boundaries

- Startup installs each environment from public source and is intentionally not
  optimized with retained Sprites or a pool.
- `delete_on_stop=False` is debugging-only; retained Sprites are not reset or
  reused.
- The bridge proxies only OpenEnv's WebSocket endpoint. Health checks go directly
  to the authenticated Sprite URL.
- The default bootstrap requires `git`, `curl`, and `sh` in the Sprite and
  installs `uv` 0.12.6 under `/opt/openenv/bin`.
- Private Git authentication, OCI/Compose semantics, GPUs, attached devices,
  egress policy, and workload-specific setup are not inferred automatically.

See [Hardening and upstream work](docs/hardening.md) for the production threat
model, operational behavior, and the remaining `sprites-api`, `sprites-py`, and
future-fork integration work.
