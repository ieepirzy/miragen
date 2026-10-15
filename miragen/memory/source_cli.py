"""Explicit grounding workflow: python -m miragen.memory.source_cli --help."""

import argparse
import asyncio
import json
from pathlib import Path

from miragen.memory.client import MemoryClient
from miragen.memory.lifecycle import MemoryLifecycle
from miragen.memory.resources import inspect_resources
from miragen.models import MemorySpec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=["inspect", "ground", "recall", "history", "check", "queue", "apply"],
    )
    parser.add_argument("--checkout", default=".")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--path")
    parser.add_argument("--symbol")
    parser.add_argument(
        "--input",
        type=Path,
        help="JSON payload/support for ground; bounded proposal/candidates for apply/queue",
    )
    parser.add_argument("--budget", type=int, default=2400)
    args = parser.parse_args()
    spec = MemorySpec.model_validate(
        {
            "backend": "loimi",
            "endpoint_env": "LOIMI_MEMORY_URL",
            "credential_env": "LOIMI_MEMORY_TOKEN",
            "scopes": {
                "read": [args.scope],
                "propose": [args.scope],
                "default_write": args.scope,
            },
            "recall": {"max_optional_chars": args.budget},
        }
    )
    lifecycle = MemoryLifecycle(spec, "source-cli", MemoryClient(spec))
    locators = [{"path": args.path, "symbol": args.symbol}] if args.path else []
    body = json.loads(args.input.read_text()) if args.input else {}

    async def execute():
        from miragen.memory import grounded

        if args.operation == "inspect":
            return inspect_resources(args.checkout, locators)
        if args.operation == "ground":
            return await grounded.remember(
                lifecycle,
                args.checkout,
                locators[0],
                payload=body["payload"],
                support=body["support"],
                record_type=body.get("type", "observation"),
            )
        if args.operation in ("recall", "history"):
            return await grounded.recall(
                lifecycle, args.checkout, locators, inspect=args.operation == "history"
            )
        if args.operation == "check":
            return await grounded.check(lifecycle, args.checkout, locators)
        if args.operation == "queue":
            return await lifecycle.client.queue_consolidation(
                body | {"scope_id": args.scope}
            )
        return await lifecycle.client.consolidate(body)

    print(json.dumps(asyncio.run(execute()), indent=2))


if __name__ == "__main__":
    main()
