"""Run the OpenEnv Echo environment in a fresh Sprite."""

import json
import os

from echo_env import CallToolAction, EchoEnv

from openenv_sprites import SpritesProvider

provider = SpritesProvider(
    token=os.environ["SPRITES_API_TOKEN"],
    source="hf://openenv/echo_env",
)

try:
    with provider:
        base_url = provider.start_container()
        provider.wait_for_ready(base_url)

        with EchoEnv(base_url=base_url).sync() as env:
            env.reset()
            result = env.step(
                CallToolAction(
                    tool_name="echo_message",
                    arguments={"message": "Hello from a Sprite"},
                )
            )
            print(result.observation)
finally:
    print("timings=" + json.dumps(provider.timings, sort_keys=True))
    print("diagnostics=" + json.dumps(provider.diagnostics, sort_keys=True))
