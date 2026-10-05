"""
Prepare multi-domain task index JSONL for dynamic tau-bench training.

Merges retail (train split) and airline (test split) into a single JSONL
where each row's ``task_id`` encodes domain and local index as
``"retail:42"`` or ``"airline:7"``.  Slime is launched with
``--input-key task_id``; ``generate_with_tau_dynamic.py`` parses the key
to reconstruct the right environment and task index.

Usage:
    python prepare_multidomain_tasks.py --local_dir /root/tau-bench/
"""

import argparse
import json
import os

from tau_bench.envs import get_env
from tau_bench.types import RunConfig


DOMAIN_SPLITS = {
    "retail": "train",
    "airline": "test",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local_dir", required=True,
                        help="Directory to write multidomain_train_tasks.jsonl")
    args = parser.parse_args()

    os.makedirs(args.local_dir, exist_ok=True)

    config = RunConfig(
        model_provider="mock",
        user_model_provider="mock",
        user_strategy="human",
        model="mock",
    )

    rows = []
    for domain, split in DOMAIN_SPLITS.items():
        config.env = domain
        config.task_split = split
        env_instance = get_env(
            env_name=domain,
            user_strategy=config.user_strategy,
            user_model=config.user_model,
            task_split=split,
        )
        for local_idx, task in enumerate(env_instance.tasks):
            rows.append({
                "task_id": f"{domain}:{local_idx}",
                "domain": domain,
                "task_split": split,
                "local_index": local_idx,
                "metadata": task.model_dump(),
            })
        print(f"  {domain}/{split}: {len(env_instance.tasks)} tasks")

    out_path = os.path.join(args.local_dir, "multidomain_train_tasks.jsonl")
    with open(out_path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(f"Wrote {len(rows)} tasks → {out_path}")


if __name__ == "__main__":
    main()
