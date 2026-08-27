# Hardening and upstream work

## Supported model

`SpritesProvider` implements OpenEnv's `ContainerProvider` contract while
interpreting `image` as an environment source rather than an OCI image. One
provider owns one fresh Sprite and one local WebSocket bridge. Provider instances
are not thread-safe; concurrent workloads use independent provider instances.

The provider is an execution adapter, not a scheduler. It does not pool,
pre-provision, reset, lease, or reconcile Sprites. That keeps the current design
compatible with a future allocation primitive based on Sprite forking.

## Security model

The trust boundaries are:

- The caller holds a Sprites token and environment-specific credentials.
- The Sprite runs environment source and receives only `env_vars` explicitly
  supplied for that service.
- The Sprites token stays in the caller and authenticates SDK, health, and
  upstream WebSocket operations.
- OpenEnv clients connect to `127.0.0.1` through a random capability path. The
  bridge accepts only that path and forwards only `/ws` upstream.
- Source URLs cannot contain userinfo, queries, or fragments. Diagnostics omit
  health bodies by default and redact the provider token plus service environment
  values from surfaced service errors.

For multi-user or hosted control planes, the caller should supply a restricted,
expiring Sprites token bound to the `openenv-` name prefix and `openenv` label.
Automatic token minting is deliberately absent until `sprites-py` exposes the
restricted-token lifecycle as a supported SDK operation.

## Failure behavior

Provisioning stages are timed independently. Any failure after Sprite creation
attempts cleanup before returning the original error. Readiness retries cold-start
network errors and non-success responses, but fails immediately on HTTP 401/403.
On timeout it attaches current service status when the SDK supports it.

Cleanup performs three independent operations: stop the loopback bridge, delete
the Sprite, and close an owned SDK client. A failure in one does not prevent the
others. Sprite deletion treats 404 as success and retries network failures plus
HTTP 408, 409, 425, 429, and 5xx responses with exponential jitter. If an action
and cleanup both fail, the action remains the primary exception and Python 3.11+
adds the cleanup error as an exception note.

Important diagnostics include:

- generated Sprite name and authenticated upstream URL;
- requested and resolved source revision;
- lockfile/frozen dependency mode;
- health attempts, transient errors, and last status;
- service state on readiness timeout;
- bridge connection errors; and
- deletion attempts, transient errors, and final outcome.

## Compatibility

OpenEnv stable 0.4.1 defines the required `start_container`, `wait_for_ready`,
`stop_container`, and `close` lifecycle. Current clients can use
`EnvClient.from_docker_image(..., provider=provider)`. Older published environment
packages may require an explicit `base_url`; the provider therefore owns its own
context-manager methods and supports both patterns.

The local bridge defaults to OpenEnv's 100 MB message ceiling rather than the
WebSocket library's much smaller default. It detects the installed `websockets`
header parameter name for compatibility across supported releases.

## Remaining upstream work

No `sprite-env` change is required for the proven fresh-allocation path. The
following upstream changes would remove workarounds or improve operations:

1. **Scoped user-service access.** A short-lived, per-Sprite signed URL or
   user-service credential would remove the need for the client-side WebSocket
   bridge and avoid using an organization token for data-plane connections.
2. **API catch-all routing.** The API catch-all currently reaches `sprite-env`
   as a control request, where `/health` is not a control endpoint. It should
   preserve user-service routing semantics or expose a documented proxy route.
3. **Restricted-token SDK methods.** `sprites-py` should expose create, inspect,
   and revoke operations for restricted tokens so a controller can implement
   least-privilege lifecycle credentials without raw HTTP calls.
4. **Durable deletion.** The DELETE endpoint can return a generic 500 while its
   multi-stage cleanup fails. It should expose an idempotent durable deletion
   operation, a structured error/request identifier, and reconciliation status.
5. **Fork allocation.** When Sprite forking ships, `_allocate_sprite` can return
   an already-prepared fork with `needs_preparation=False`. Baseline identity,
   invalidation, and source-commit matching should live in the allocation layer,
   not in OpenEnv clients.

## Release gate

Before calling this production-ready, add a live opt-in integration job with
restricted credentials, fork-era allocation tests, and load tests covering
concurrent cold starts, network interruption, and cleanup reconciliation. CI
now runs lint, unit and local bridge tests, and package builds across supported
Python versions. The test suite mocks billable Sprite lifecycle operations; its
WebSocket relay test uses only local sockets, and live examples remain explicit
operator actions.
