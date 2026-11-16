"""Load non-secret settings and retrieve secrets from macOS Keychain."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / ".jira-status.env"
JIRA_TOKEN_ITEM = "Jira Updates API Cred"
SLACK_WEBHOOK_ITEM = "Jira Updates Webhook"


class ConfigurationError(RuntimeError):
    """Raised when required local configuration is missing."""


def _read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=value settings. This file must not contain secrets."""
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


def setting(name: str) -> str | None:
    """Environment variables override non-secret values in .jira-status.env."""
    return os.environ.get(name) or _read_env_file(CONFIG_FILE).get(name)


def required_setting(name: str) -> str:
    value = setting(name)
    if not value:
        raise ConfigurationError(
            f"Set {name} in .jira-status.env (copy the example file first)."
        )
    return value


def keychain_secret(item_name: str, account_name: str | None = None) -> str:
    """Read one named generic-password item without printing its value."""
    command = ["/usr/bin/security", "find-generic-password", "-s", item_name]
    if account_name:
        command.extend(["-a", account_name])
    command.append("-w")
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigurationError(
            f"Timed out reading '{item_name}' from Keychain. Unlock the login keychain "
            "or approve the macOS access prompt, then retry."
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            f"Could not start the macOS Keychain command for '{item_name}'."
        ) from exc

    if result.returncode != 0 or not result.stdout.strip():
        raise ConfigurationError(
            f"Could not find or access the Keychain item '{item_name}'."
        )
    return result.stdout.rstrip("\r\n")


@dataclass(frozen=True)
class JiraConfig:
    api_base_url: str
    email: str
    site_url: str | None


def jira_config() -> JiraConfig:
    return JiraConfig(
        api_base_url=required_setting("JIRA_API_BASE_URL").rstrip("/"),
        email=required_setting("JIRA_EMAIL"),
        site_url=(setting("JIRA_SITE_URL") or "").rstrip("/") or None,
    )
