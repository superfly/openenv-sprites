"""Run a minimal OpenEnv Coding rollout in a fresh Sprite."""

import json
import os

from coding_env import CodeAction, CodingEnv

from openenv_sprites import SpritesProvider

provider = SpritesProvider(
    token=os.environ["SPRITES_API_TOKEN"],
    source="hf://openenv/coding_env",
)

try:
    with provider:
        base_url = provider.start_container()
        provider.wait_for_ready(base_url)

        with CodingEnv(base_url=base_url).sync() as env:
            env.reset()
            result = env.step(
                CodeAction(
                    code=(
                        "values = [2, 3, 5, 7]\n"
                        "print(f'sum={sum(values)} count={len(values)}')"
                    )
                )
            )
            print(result.observation)
finally:
    print("timings=" + json.dumps(provider.timings, sort_keys=True))
    print("diagnostics=" + json.dumps(provider.diagnostics, sort_keys=True))
