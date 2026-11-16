"""Maintain ignored local snapshots and a retryable Slack digest queue."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from jira_updates.config import ROOT


DATA_DIR = ROOT / ".jira-data"
TICKETS_DIR = DATA_DIR / "tickets"
STATE_FILE = DATA_DIR / "state.json"
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
TRACKED_FIELDS = ("summary", "status", "priority")


class LocalDataError(RuntimeError):
    """Raised when existing local sync data cannot be safely used."""


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"issues": {}, "pending_digest": []}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalDataError("Could not read .jira-data/state.json safely.") from exc
    state.setdefault("issues", {})
    state.setdefault("pending_digest", [])
    return state


def _plain_text(value: object) -> str:
    text = str(value or "")
    return " ".join(text.replace("|", "\\|").split())


def _issue_snapshot(issue: dict) -> dict[str, str]:
    fields = issue.get("fields", {})
    return {
        "summary": _plain_text(fields.get("summary")),
        "status": _plain_text((fields.get("status") or {}).get("name")),
        "priority": _plain_text((fields.get("priority") or {}).get("name")),
        "updated": _plain_text(fields.get("updated")),
    }


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".jira-update-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _render_ticket(key: str, snapshot: dict, history: list[str], site_url: str | None) -> str:
    lines = [
        f"# {key} — {snapshot['summary']}",
        "",
        f"- **Status:** {snapshot['status'] or 'Unknown'}",
        f"- **Priority:** {snapshot['priority'] or 'Not set'}",
        f"- **Last updated in Jira:** {snapshot['updated'] or 'Unknown'}",
        "",
        "## Local update history",
        "",
    ]
    if site_url:
        lines[6:6] = [f"- **Jira:** [{key}]({site_url}/browse/{key})", ""]
    lines.extend(history or ["- First local snapshot."])
    return "\n".join(lines) + "\n"


def prepare_sync(
    issues: list[dict], state: dict, checked_at: str, site_url: str | None
) -> tuple[dict, dict[Path, str], list[str]]:
    """Build new state and file contents without writing, for dry-run support."""
    previous_issues = state.get("issues", {})
    initial_sync = not state.get("last_sync_at")
    next_issues: dict = {}
    file_contents: dict[Path, str] = {}
    new_updates: list[str] = []

    for issue in issues:
        key = str(issue.get("key", ""))
        if not ISSUE_KEY_PATTERN.fullmatch(key):
            raise LocalDataError("Jira returned an unexpected issue key; no files were written.")

        current = _issue_snapshot(issue)
        previous = previous_issues.get(key)
        history = list(previous.get("history", [])) if previous else []
        deltas: list[str] = []

        if previous is None:
            if initial_sync:
                event = "Initial local baseline captured."
            else:
                event = "Newly tracked"
                deltas.append(event)
        else:
            for field in TRACKED_FIELDS:
                old_value = previous.get(field, "")
                new_value = current.get(field, "")
                if old_value != new_value:
                    label = field.capitalize()
                    deltas.append(f"{label}: {old_value or '—'} → {new_value or '—'}")
            if previous.get("updated") != current.get("updated") and not deltas:
                deltas.append("Updated in Jira; no tracked fields changed")
            event = "; ".join(deltas) if deltas else ""

        if event:
            history.append(f"- {checked_at} — {event}")
        if deltas:
            new_updates.append(
                f"• `{key}` — {current['summary'] or '(no summary)'} "
                f"({current['status'] or 'Unknown'}): {event}"
            )

        next_issues[key] = {**current, "history": history, "assigned": True}
        file_contents[TICKETS_DIR / f"{key}.md"] = _render_ticket(key, current, history, site_url)

    # Preserve snapshots for issues that no longer appear in the current user's search.
    for key, previous in previous_issues.items():
        if key in next_issues or not previous.get("assigned", True):
            continue
        history = list(previous.get("history", []))
        event = f"No longer appears in your assigned-issues search as of {checked_at}."
        history.append(f"- {checked_at} — {event}")
        snapshot = {field: previous.get(field, "") for field in (*TRACKED_FIELDS, "updated")}
        next_issues[key] = {**snapshot, "history": history, "assigned": False}
        file_contents[TICKETS_DIR / f"{key}.md"] = _render_ticket(key, snapshot, history, site_url)
        new_updates.append(f"• `{key}` — no longer appears in your assigned-issues search")

    pending = list(state.get("pending_digest", [])) + new_updates
    new_state = {
        "last_sync_at": checked_at,
        "issues": next_issues,
        "pending_digest": pending,
    }
    return new_state, file_contents, new_updates


def save_sync(state: dict, file_contents: dict[Path, str], known_issue_keys: set[str]) -> None:
    """Write snapshots and state atomically; refuse to overwrite untracked files."""
    collisions = [
        path for path in file_contents
        if path.exists() and path.stem not in known_issue_keys
    ]
    if collisions:
        raise LocalDataError(
            "A generated ticket path already exists without matching sync state; "
            "move or review the existing .jira-data/tickets files before syncing."
        )

    # Persist state first. If a file write is interrupted, the next run rebuilds
    # all ticket files from this state and completes the pending digest.
    _atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    for path, content in file_contents.items():
        _atomic_write(path, content)


def clear_pending_digest(state: dict) -> None:
    state["pending_digest"] = []
    _atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def digest_text(updates: list[str]) -> str:
    return "*Jira updates since the previous successful Slack post*\n" + "\n".join(updates)
