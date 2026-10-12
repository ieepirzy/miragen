"""The `*_FILE` secrets convention, shared by the agent runtime and the daemon.

For every variable FOO_FILE whose value is a readable path, FOO is set to the
file's contents (stripped) and FOO_FILE is removed, so a Docker or systemd
secret reaches the process as a plain variable and the path does not linger.

Two kinds of `*_FILE` variable are left alone, because resolving them breaks
the process instead of configuring it:

- Standard tool variables that are *paths by contract* (``SSL_CERT_FILE`` and
  friends). Resolving ``SSL_CERT_FILE`` deletes it, so httpx and OpenSSL stop
  trusting a proxy's CA, and leaves the PEM bundle in a stray ``SSL_CERT``.
- Files larger than :data:`MAX_SECRET_BYTES`. A secret is a token or a key,
  never hundreds of kilobytes, and Linux refuses to start any subprocess whose
  environment holds a single string over 128 KiB (E2BIG), so one oversized
  value would make every ``git`` or harness spawn fail.

Kept free of heavy imports so the daemon can use it without importing the
agent runtime.
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping
from pathlib import Path

logger = logging.getLogger(__name__)

# Variables other tools read as a path. Never secrets.
PATH_VARIABLES = frozenset({
    "SSL_CERT_FILE",
    "NIX_SSL_CERT_FILE",
    "PIP_CONFIG_FILE",
    "CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE",
})

# Half of Linux's per-string limit for argv/envp (MAX_ARG_STRLEN, 128 KiB).
MAX_SECRET_BYTES = 64 * 1024


def load_file_secrets(environ: MutableMapping[str, str] | None = None) -> None:
    """Resolve `*_FILE` variables in `environ` (default: ``os.environ``)."""
    env = os.environ if environ is None else environ
    for file_var, path in list(env.items()):
        if not file_var.endswith("_FILE") or file_var in PATH_VARIABLES:
            continue
        secret_path = Path(path)
        if not secret_path.exists():
            logger.warning(f"Secret file referenced by {file_var} not found: {path}")
            continue
        target = file_var[: -len("_FILE")]
        try:
            size = secret_path.stat().st_size
            if size > MAX_SECRET_BYTES:
                logger.warning(
                    f"Not loading {file_var}: {path} is {size} bytes, larger than "
                    f"any secret ({MAX_SECRET_BYTES}); leaving it as a path")
                continue
            env[target] = secret_path.read_text().strip()
            del env[file_var]
            logger.info(f"Loaded secret {target} from {file_var}")
        except OSError as exc:
            logger.error(f"Failed to read secret file {path} for {file_var}: {exc}")
