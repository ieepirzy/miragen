"""`miragend-spoke`: run miragend in pull mode inside a cluster.

Off unless `MIRAGEND_SPOKE_CONFIG` names the spoke's config file. The hub
token is read from the file the config names, never from the environment,
and is never logged.
"""

from __future__ import annotations

import logging
import os
import signal

from miragen.daemon.spawn.kubernetes_workloads import KubernetesWorkloadDriver
from miragen.daemon.spoke.agents import AgentClient
from miragen.daemon.spoke.config import SpokeConfigInvalid, load_config
from miragen.daemon.spoke.hub import HubClient
from miragen.daemon.spoke.loop import Spoke

logger = logging.getLogger(__name__)


def main() -> None:  # pragma: no cover - exercised only in a real deployment
    logging.basicConfig(level=logging.INFO)
    path = os.getenv("MIRAGEND_SPOKE_CONFIG", "")
    if not path:
        raise SystemExit("pull mode is off: MIRAGEND_SPOKE_CONFIG is not set")
    try:
        config = load_config(path)
        token = config.token()
    except SpokeConfigInvalid as exc:
        raise SystemExit(f"spoke config: {exc}") from exc

    stopping = {"now": False}

    def _stop(*_: object) -> None:
        stopping["now"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    hub = HubClient(config.hub_url, token)
    spoke = Spoke(
        config=config,
        hub=hub,
        driver=KubernetesWorkloadDriver.from_incluster_config(),
        agents=AgentClient(timeout=config.agent_timeout_seconds),
    )
    logger.info("spoke pulling from the hub; %d namespace(s) served", len(config.namespaces))
    try:
        spoke.run_forever(should_stop=lambda: stopping["now"])
    finally:
        hub.close()


if __name__ == "__main__":  # pragma: no cover
    main()
