"""Non-secret host admission for the runner-owned Copilot providers."""

from collections.abc import Mapping


def browser_account_bindings(root, selected_profiles):
    """Read explicit non-secret identities for exact selected routine profiles."""
    import json
    import re
    from pathlib import Path
    root = Path(root)
    path = root / "config" / "copilot-sign-in.json"
    if not path.exists():
        return {}  # Unconfigured callers retain sign-in for manual attention.
    value = json.loads(path.read_text())
    routine = json.loads((root / "config/profiles.json").read_text())
    directories = {row["id"]: row["chrome_directory"] for row in routine}
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("account_emails"), dict)):
        raise ValueError("Copilot sign-in configuration is invalid")
    accounts = value["account_emails"]
    if any(key not in directories or not isinstance(email, str)
            or re.fullmatch(r'[^\s@<>"\\]+@[^\s@<>"\\]+\.[^\s@<>"\\]+', email) is None
            for key, email in accounts.items()):
        raise ValueError("Copilot sign-in account binding is invalid")
    result = {}
    for row in selected_profiles:
        if directories.get(row["id"]) != row["chrome_directory"]:
            raise ValueError("Copilot sign-in profile binding changed")
        if row["id"] in accounts:
            result[row["id"]] = accounts[row["id"]].casefold()
    return result


def source_settings(value: object) -> dict:
    """Normalize without changing the policy of historical frozen selections.

    Missing policy versions retain the runner's SDK-default behavior. New UI
    selections opt in to the optimized policy explicitly before reaching here.
    """
    if not isinstance(value, Mapping):
        raise ValueError("GitHub Copilot requires explicit account and source settings")
    from visibility_runner import GitHubCopilotConfig

    return GitHubCopilotConfig.from_mapping(value).to_mapping()


def runtime_settings(value: object, source: Mapping) -> dict:
    """A credential locator is infrastructure; it is never part of a source prompt."""
    if not isinstance(value, Mapping):
        raise ValueError("GitHub Copilot requires an explicit Keychain credential reference")
    from visibility_runner import GitHubCopilotRuntimeConfig

    result = GitHubCopilotRuntimeConfig.from_mapping(value).to_mapping()
    if any(result.get(key) != source.get(key) for key in ("account_ref", "github_account_id")):
        raise ValueError("GitHub Copilot runtime and measurement account identities must match")
    return result
