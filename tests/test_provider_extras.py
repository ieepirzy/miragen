"""The model providers miragen profiles name are installed.

`anthropic:*` once failed at load with an ImportError because the
pydantic-ai `anthropic` extra wasn't a dependency. Building a model object
needs the provider package but makes no request.
"""

import pytest
from pydantic_ai.models import infer_model


@pytest.mark.parametrize(
    ("model", "key_env"),
    [
        ("anthropic:claude-haiku-5-5", "ANTHROPIC_API_KEY"),
        ("openai:gpt-6-luna", "OPENAI_API_KEY"),
    ],
)
def test_provider_packages_are_installed(model, key_env, monkeypatch):
    monkeypatch.setenv(key_env, "test-key-not-used")
    assert infer_model(model) is not None
