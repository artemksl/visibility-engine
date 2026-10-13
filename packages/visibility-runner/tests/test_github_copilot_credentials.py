"""Provider policy stays independent of the OS credential store."""
from __future__ import annotations

import asyncio

import pytest

from platform_runtime.credentials import CredentialLookupError
from visibility_runner.config import GitHubCopilotRuntimeConfig
from visibility_runner.errors import ConfigurationError
from visibility_runner.providers import github_copilot


@pytest.mark.parametrize("lookup_code,provider_code", [
    ("reference_invalid", "copilot_credential_ref_invalid"),
    ("unavailable", "copilot_credential_unavailable"),
    ("invalid", "copilot_credential_invalid"),
])
def test_provider_preserves_redacted_lookup_codes(monkeypatch, lookup_code, provider_code):
    async def lookup(reference):
        assert reference == "credential-manager:explicit-target"
        raise CredentialLookupError(lookup_code)
    monkeypatch.setattr(github_copilot, "resolve_reference", lookup)
    with pytest.raises(github_copilot.CopilotSourceError) as refused:
        asyncio.run(github_copilot._credential("credential-manager:explicit-target"))
    assert refused.value.code == provider_code and refused.value.submitted is False


@pytest.mark.parametrize("token,expected", [
    ("", "copilot_credential_unavailable"),
    ("a" * 16385, "copilot_credential_invalid"),
    ("fixture\ntoken", "copilot_credential_invalid"),
])
def test_provider_retains_token_validation(monkeypatch, token, expected):
    async def lookup(_reference):
        return token
    monkeypatch.setattr(github_copilot, "resolve_reference", lookup)
    with pytest.raises(github_copilot.CopilotSourceError) as refused:
        asyncio.run(github_copilot._credential("env:EXPLICIT_TOKEN"))
    assert refused.value.code == expected


def test_provider_returns_valid_explicit_token(monkeypatch):
    async def lookup(_reference):
        return "fixture-token"
    monkeypatch.setattr(github_copilot, "resolve_reference", lookup)
    assert asyncio.run(github_copilot._credential("env:EXPLICIT_TOKEN")) == "fixture-token"


@pytest.mark.parametrize("reference", ["keychain:exact-service:exact:account", "credential-manager: exact:target  "])
def test_runtime_configuration_accepts_explicit_store_without_reading_it(reference):
    config = GitHubCopilotRuntimeConfig("account", "identity", reference)
    assert config.credential_ref == reference


@pytest.mark.parametrize("reference", ["env:EXPLICIT_TOKEN", "credential-manager:",
                                        "credential-manager:target\nother", "credential-manager:target\x00other"])
def test_runtime_configuration_rejects_ambient_or_malformed_store_reference(reference):
    with pytest.raises(ConfigurationError):
        GitHubCopilotRuntimeConfig("account", "identity", reference)
