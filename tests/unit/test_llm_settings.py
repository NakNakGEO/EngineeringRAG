"""The local model chosen at runtime: only this machine / private networks, sane values."""

from __future__ import annotations

import pytest

from eios_core.settings import Settings
from eios_runtime.llm_settings import InvalidLocalLlm, providers_from, validate


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:1234/v1",
        "http://host.docker.internal:11434/v1",
        "http://192.168.1.20:8000/v1",
        "http://ollama:11434/v1",
        "http://[::1]:11434/v1",
    ],
)
def test_local_and_private_servers_are_accepted(url: str) -> None:
    cfg = validate(url + "/", "gemma4:e2b", 240)
    assert cfg.base_url == url  # a trailing slash is dropped
    assert cfg.model == "gemma4:e2b"


@pytest.mark.parametrize(
    "url", ["https://api.openai.com/v1", "http://example.com/v1", "ftp://localhost/v1", "localhost:11434", "http://8.8.8.8/v1"]
)
def test_public_or_malformed_addresses_are_refused(url: str) -> None:
    with pytest.raises(InvalidLocalLlm):
        validate(url, "m", 60)


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254/v1", "http://169.254.1.1:8000/v1", "http://[fd00:ec2::254]/v1", "http://0.0.0.0:11434/v1", "http://224.0.0.1/v1"],
)
def test_metadata_link_local_and_unspecified_addresses_are_refused(url: str) -> None:
    with pytest.raises(InvalidLocalLlm):
        validate(url, "m", 60)


def test_model_name_and_timeout_are_checked() -> None:
    with pytest.raises(InvalidLocalLlm):
        validate("http://localhost:11434/v1", "   ", 60)
    with pytest.raises(InvalidLocalLlm):
        validate("http://localhost:11434/v1", "m", 1)
    with pytest.raises(InvalidLocalLlm):
        validate("http://localhost:11434/v1", "m", 99999)


def test_a_chosen_model_replaces_the_one_from_env_and_remote_keys_stay_in_env() -> None:
    env = Settings(llm_local_base_url="http://localhost:1/v1", llm_local_model="from-env")  # type: ignore[call-arg]
    assert providers_from(env, None)["local"].default_model == "from-env"
    chosen = validate("http://localhost:11434/v1", "gemma4:e2b", 120)
    got = providers_from(env, chosen)
    assert got["local"].default_model == "gemma4:e2b"
    assert set(got) == {"local"}  # no remote provider appears from nowhere
