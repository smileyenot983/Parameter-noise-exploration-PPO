# pylint:disable=missing-module-docstring
from gym.envs.registration import register

register(
    id="CartPoleSwingUp-v0",
    entry_point="cartpole_swingup.envs.cartpole_swingup:CartPoleSwingUpV0",
    max_episode_steps=500,
)

register(
    id="CartPoleSwingUp-v1",
    entry_point="cartpole_swingup.envs.cartpole_swingup:CartPoleSwingUpV1",
    max_episode_steps=500,
)

register(
    id="CartPoleSwingUp-v2",
    entry_point="cartpole_swingup.envs.cartpole_swingup:CartPoleSwingUpV2",
    max_episode_steps=500,
)


