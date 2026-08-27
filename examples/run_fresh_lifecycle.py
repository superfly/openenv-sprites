"""Exercise concurrent, isolated, fresh-Sprite OpenEnv lifecycles.

Every lane creates and deletes its own Sprite. There is no pool, checkpoint
restore, or runtime reuse between lanes or rounds.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from coding_env import CodeAction, CodingEnv

from openenv_sprites import SpritesProvider

MARKER_VARIABLE = "openenv_lifecycle_marker"


@dataclass(frozen=True)
class LaneResult:
    round: int
    lane: str
    marker: str
    sprite_name: str
    first_stdout: str
    second_stdout: str
    timings: dict[str, float]
    diagnostics: dict[str, object]


def checked_stdout(result: object, *, lane: str, phase: str, expected: str) -> str:
    observation = result.observation  # type: ignore[attr-defined]
    stdout = observation.stdout.strip()
    if stdout != expected:
        raise AssertionError(
            f"{lane} {phase} returned unexpected stdout: {stdout!r}; "
            f"stderr={observation.stderr!r}; "
            f"exit_code={observation.exit_code!r}; "
            f"observation={observation!r}"
        )
    return stdout


def run_lane(
    *,
    token: str,
    round_number: int,
    lane: str,
    written: threading.Barrier,
) -> LaneResult:
    marker = f"round-{round_number}-{lane}"
    provider = SpritesProvider(
        token=token,
        source="hf://openenv/coding_env",
        labels=("openenv", "fresh-lifecycle"),
    )

    try:
        with provider:
            base_url = provider.start_container()
            provider.wait_for_ready(base_url)

            with CodingEnv(base_url=base_url).sync() as env:
                env.reset()
                first = env.step(
                    CodeAction(
                        code=(
                            f"{MARKER_VARIABLE} = {marker!r}\n"
                            f"print({MARKER_VARIABLE})"
                        )
                    )
                )
                first_stdout = checked_stdout(
                    first,
                    lane=lane,
                    phase="initial write",
                    expected=marker,
                )

                # Both environments now contain the same variable with
                # different values. Read only after both writes finish.
                written.wait(timeout=600.0)
                second = env.step(
                    CodeAction(code=f"print({MARKER_VARIABLE})")
                )
                second_stdout = checked_stdout(
                    second,
                    lane=lane,
                    phase="persistence read",
                    expected=marker,
                )
    except BaseException:
        written.abort()
        raise

    diagnostics = provider.diagnostics
    if diagnostics.get("sprite_deleted") is not True:
        raise AssertionError(
            f"{lane} did not confirm Sprite deletion: {diagnostics!r}"
        )

    return LaneResult(
        round=round_number,
        lane=lane,
        marker=marker,
        sprite_name=str(diagnostics["sprite_name"]),
        first_stdout=first_stdout,
        second_stdout=second_stdout,
        timings=provider.timings,
        diagnostics=diagnostics,
    )


def run_round(token: str, round_number: int) -> list[LaneResult]:
    written = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                run_lane,
                token=token,
                round_number=round_number,
                lane=lane,
                written=written,
            )
            for lane in ("a", "b")
        ]
        return [future.result() for future in futures]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="number of fresh two-Sprite rounds to run (default: 1)",
    )
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")

    token = os.environ["SPRITES_API_TOKEN"]
    started = time.monotonic()
    results: list[LaneResult] = []
    for round_number in range(1, args.rounds + 1):
        results.extend(run_round(token, round_number))

    print(
        json.dumps(
            {
                "elapsed": round(time.monotonic() - started, 3),
                "lanes": [asdict(result) for result in results],
                "rounds": args.rounds,
                "status": "passed",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
