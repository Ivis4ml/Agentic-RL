"""
Dynamic, multi-domain tau-bench generate function for slime training.

Differences from ``generate_with_tau.py``:
  - task_id encodes domain+index: ``"retail:42"`` or ``"airline:7"``
    (produced by ``prepare_multidomain_tasks.py``, loaded via
    ``--input-key task_id``).
  - User strategy is rotated deterministically per task_id across
    ["llm", "react", "verify", "reflection"] so each domain/index pair
    always gets the same strategy, avoiding run-to-run variance while still
    diversifying the dialog distribution across the dataset.
  - ``--rollout-max-response-len`` is expected to be 4096 (multi-turn).

Noise sources that increase reward standard deviation within a group:
  1. User strategy diversity across tasks.
  2. Higher n_samples_per_prompt (set to 16 in the shell script) → more
     draws from the same stochastic Gemini user simulator.
  3. Combined retail+airline → heterogeneous task difficulty per batch.

Compatible with ``--rollout-function-path
slime.rollout.fully_async_rollout.generate_rollout_fully_async``;
``args.partial_rollout`` must be False (asserted below).
"""

import logging
import os
from typing import Any

from tau_bench.envs import get_env
from tau_bench.types import RunConfig
from trainable_agents import InteractionResult, Status, agent_factory

from slime.utils.types import Sample

logger = logging.getLogger(__name__)

# Configurable via env var so the deploy-time API key never lives in code.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "NONE")
os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY

# User strategies rotated per (domain, local_index) for diversity-as-noise.
# "llm" is the default; the others differ in how the user simulator reasons,
# creating varied dialog length and turn structure for the same underlying task.
_USER_STRATEGIES = ["llm", "react", "verify", "reflection"]

# task_split is fixed per domain: retail supports "train"; airline only "test".
_DOMAIN_SPLIT = {
    "retail": "train",
    "airline": "test",
}

_BASE_CONFIG = {
    "agent_strategy": "tool-calling",
    "user_model": "gemini-2.5-flash-lite",
    "user_model_provider": "gemini",
    "model_provider": "auto_router",
    "model": "qwen3-4b",
}


def _parse_task_id(task_id: str) -> tuple[str, int, str]:
    """Parse ``"domain:local_idx"`` → (domain, local_idx, task_split)."""
    parts = task_id.split(":", 1)
    if len(parts) != 2:
        raise ValueError(
            f"task_id must be 'domain:local_idx', got {task_id!r}"
        )
    domain, idx_str = parts
    if domain not in _DOMAIN_SPLIT:
        raise ValueError(f"Unknown domain {domain!r}; expected one of {list(_DOMAIN_SPLIT)}")
    return domain, int(idx_str), _DOMAIN_SPLIT[domain]


def _pick_user_strategy(domain: str, local_idx: int) -> str:
    """Deterministic strategy selection — same (domain, idx) always maps to the same strategy."""
    key = hash(f"{domain}:{local_idx}") % len(_USER_STRATEGIES)
    return _USER_STRATEGIES[key]


def _res_to_sample(res: InteractionResult, task_id: str) -> Sample:
    status_mapping = {
        Status.COMPLETED: "completed",
        Status.TRUNCATED: "truncated",
        Status.ABORTED: "aborted",
    }
    sample = Sample(
        index=task_id,
        prompt=res.prompt,
        tokens=res.tokens,
        response=res.response,
        reward=res.reward,
        loss_mask=res.loss_mask,
        status=status_mapping.get(res.status),
        metadata=res.info,
    )
    if hasattr(res, "response_length"):
        sample.response_length = res.response_length
    elif res.loss_mask:
        sample.response_length = len(res.loss_mask)
    elif res.tokens:
        sample.response_length = len(res.tokens)
    else:
        sample.response_length = 0
    return sample


async def generate(args: dict[str, Any], sample: Sample, sampling_params: dict) -> Sample:
    """
    Entry point wired through ``--custom-generate-function-path
    generate_with_tau_dynamic.generate``.

    ``sample.prompt`` is the raw string value of ``task_id`` from the JSONL,
    e.g. ``"retail:42"`` or ``"airline:7"``.
    """
    assert not args.partial_rollout, (
        "Partial rollout is not supported for tau-bench interactions."
    )

    task_id = sample.prompt
    domain, local_idx, task_split = _parse_task_id(task_id)
    user_strategy = _pick_user_strategy(domain, local_idx)

    logger.info(
        f"[tau-dynamic] task_id={task_id} domain={domain} local_idx={local_idx} "
        f"split={task_split} user_strategy={user_strategy}"
    )

    tau_config = RunConfig(
        **_BASE_CONFIG,
        env=domain,
        task_split=task_split,
        user_strategy=user_strategy,
    )

    env = get_env(
        env_name=domain,
        user_strategy=user_strategy,
        user_model=tau_config.user_model,
        user_provider=tau_config.user_model_provider,
        task_split=task_split,
        task_index=local_idx,
    )

    agent = agent_factory(
        tools_info=env.tools_info,
        wiki=env.wiki,
        config=tau_config,
        rollout_args=args,
        sampling_params=sampling_params,
    )

    interaction_result = await agent.asolve(
        env, agent.rollout_args, agent.sampling_params, local_idx
    )

    result_sample = _res_to_sample(interaction_result, task_id)
    logger.info(f"[tau-dynamic] task_id={task_id} reward={result_sample.reward:.3f}")
    return result_sample
