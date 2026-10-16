import pytest

import miragen.app as app_module


@pytest.fixture(autouse=True)
def _reset_admission_state():
    """Admission state is module-global on purpose (one long-lived loop in
    production), but tests each run their own short-lived event loop — a
    background task killed with its loop never runs its release(), so the
    counter would leak across tests. Reset BEFORE each test."""
    app_module._active_runs = 0
    app_module._busy_instances.clear()
    yield


@pytest.fixture(autouse=True)
def _reset_model_rates():
    """The model rates source is process-global (the lifespan configures
    it); no test inherits another's, and the log-once memory starts empty."""
    from miragen import model_rates

    model_rates.configure(None)
    model_rates._warned.clear()
    yield
    model_rates.configure(None)
