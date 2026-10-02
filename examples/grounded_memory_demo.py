"""Reproducible HTTP demonstration against a disposable, migrated Loimi service.

Run: LOIMI_DEMO_OPERATOR_TOKEN=... python examples/grounded_memory_demo.py --url http://127.0.0.1:18400
Creates one isolated demo principal and scope; prints no credentials.
"""

import argparse
import ast
import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from miragen.memory import grounded
from miragen.memory.client import MemoryClient
from miragen.memory.lifecycle import MemoryLifecycle
from miragen.models import MemorySpec


async def demonstrate(url: str):
    suffix = uuid.uuid4().hex[:12]
    scope = f"group:grounding-demo-{suffix}"
    principal = f"grounding-demo-{suffix}"
    spec = MemorySpec(
        scopes={"read": [scope], "propose": [scope], "default_write": scope}
    )
    admin = MemoryClient(
        spec, base_url=url, token=os.environ["LOIMI_DEMO_OPERATOR_TOKEN"]
    )
    identity = await admin.admin_create_principal(
        principal_id=principal, kind="service"
    )
    await admin.admin_create_scope(scope_id=scope, kind="group")
    await admin.admin_grant(
        principal_id=principal, scope_id=scope, verbs=["read", "propose", "maintain"]
    )
    lifecycle = MemoryLifecycle(
        spec, "demo", MemoryClient(spec, base_url=url, token=identity["token"])
    )
    with tempfile.TemporaryDirectory(prefix="grounding-demo-") as directory:
        root = Path(directory)

        def git(*args):
            subprocess.run(
                [
                    "git",
                    "-C",
                    directory,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgsign=false",
                    *args,
                ],
                check=True,
                capture_output=True,
            )

        git("init", "-b", "main")
        git("config", "user.name", "Grounding demo")
        git("config", "user.email", "demo@example.invalid")
        source = root / "pricing.py"
        source.write_text("def price():\n    return 10\n")
        git("add", ".")
        git("commit", "-m", "fixture")
        parsed = ast.parse(source.read_text())
        assert isinstance(parsed.body[0].body[0], ast.Return)
        assert parsed.body[0].body[0].value.value == 10
        locator = {"path": "pricing.py", "symbol": "price"}
        stored = await grounded.remember(
            lifecycle,
            directory,
            locator,
            payload={
                "text": "The fixture price function contains a literal return of 10; this is not production pricing."
            },
            support={
                "result": "supports",
                "method": "demo AST literal-return check",
                "limitations": "Inspected fixture syntax; no runtime or production behavior asserted.",
            },
        )
        before = await grounded.recall(lifecycle, directory, [locator])
        source.write_text("def price():\n    return 20\n")
        after = await grounded.recall(lifecycle, directory, [locator])
        evidence = await grounded.check(lifecycle, directory, [locator])
        history = await grounded.recall(lifecycle, directory, [locator], inspect=True)
        assert len(before["items"]) == 1 and not after["items"]
        assert evidence["receipts"][0]["outcome"] == "revalidation_required"
        assert "INSPECTION ONLY" in history["text"]
        print(
            json.dumps(
                {
                    "scope": scope,
                    "record_id": stored["record"]["record_id"],
                    "before": {"emitted": before["rendering"]["emitted"]},
                    "after": {
                        "emitted": after["rendering"]["emitted"],
                        "omitted": after["rendering"]["omitted"],
                    },
                    "check": evidence["receipts"][0]["outcome"],
                    "historical_text": history["text"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    asyncio.run(demonstrate(parser.parse_args().url))
