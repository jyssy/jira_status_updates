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
ACTIVITY_DIR = DATA_DIR / "activity"
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


def slack_issue_label(key: str, site_url: str | None) -> str:
    """Format a Jira issue as a clickable Slack link when the site URL is configured."""
    if site_url:
        return f"<{site_url}/browse/{key}|{key}>"
    return f"`{key}`"


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
    rendered = "\n".join(lines) + "\n"
    cached = _load_issue_activity(key)
    return _with_activity_section(rendered, cached) if cached else rendered


def _adf_text(value: object) -> str:
    """Extract readable text from Jira's string or Atlassian document comment body."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(part for part in (_adf_text(item) for item in value) if part)
    if isinstance(value, dict):
        own = value.get("text", "")
        child = _adf_text(value.get("content", []))
        return " ".join(part for part in (own, child) if part)
    return ""


def _activity_entries(activity: dict) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for comment in activity.get("comments", []):
        author = (comment.get("author") or {}).get("displayName", "Unknown author")
        body = _plain_text(_adf_text(comment.get("body", "")))
        if body:
            created = str(comment.get("created", ""))
            updated = str(comment.get("updated", ""))
            edited = bool(updated and updated != created)
            entries.append((updated or created,
                            f"- **{'Edited comment' if edited else 'Comment'} "
                            f"({_plain_text(author)}):** {body}"))
    for event in activity.get("changelog", []):
        author = (event.get("author") or {}).get("displayName", "Unknown author")
        created = str(event.get("created", ""))
        for item in event.get("items", []):
            field = _plain_text(item.get("field", "Field"))
            old = _plain_text(item.get("fromString", "")) or "—"
            new = _plain_text(item.get("toString", "")) or "—"
            entries.append((created,
                            f"- **Change ({_plain_text(author)}):** {field}: {old} → {new}"))
    for attachment in activity.get("attachments", []):
        author = (attachment.get("author") or {}).get("displayName", "Unknown author")
        filename = _plain_text(attachment.get("filename", "Unnamed file"))
        entries.append((str(attachment.get("created", "")),
                        f"- **File attached ({_plain_text(author)}):** {filename}"))
    entries.sort(key=lambda entry: entry[0], reverse=True)
    return entries


def _activity_lines(activity: dict) -> list[str]:
    entries = _activity_entries(activity)
    return [f"- _{created or 'Unknown time'}_ {line[2:]}" for created, line in entries[:30]]


def activity_summary(activity: dict, since: str | None = None) -> list[str]:
    """Return readable activity lines, optionally limited to events after a timestamp."""
    entries = _activity_entries(activity)
    if since:
        try:
            threshold = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if threshold.tzinfo is None:
                threshold = threshold.replace(tzinfo=timezone.utc)
            threshold = threshold.astimezone(timezone.utc)
        except ValueError:
            threshold = None
        if threshold is not None:
            filtered: list[tuple[str, str]] = []
            for created, line in entries:
                try:
                    event_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if event_time.tzinfo is None:
                        event_time = event_time.replace(tzinfo=timezone.utc)
                    if event_time.astimezone(timezone.utc) > threshold:
                        filtered.append((created, line))
                except ValueError:
                    continue
            entries = filtered
    return [f"- _{created or 'Unknown time'}_ {line[2:]}" for created, line in entries[:30]]


def activity_digest_lines(activity: dict, since: str | None = None) -> list[str]:
    """Return only newly added comments and attachment metadata for Slack."""
    if not since:
        return []
    try:
        threshold = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if threshold.tzinfo is None:
            threshold = threshold.replace(tzinfo=timezone.utc)
        threshold = threshold.astimezone(timezone.utc)
    except ValueError:
        return []

    candidates: list[tuple[str, str]] = []
    for comment in activity.get("comments", []):
        created = str(comment.get("created", ""))
        updated = str(comment.get("updated", ""))
        activity_time = updated or created
        edited = bool(updated and updated != created)
        author = _plain_text((comment.get("author") or {}).get("displayName", "Unknown author"))
        body = _plain_text(_adf_text(comment.get("body", "")))
        if body:
            marker = "🟧" if edited else "🟩"
            label = "Edited comment" if edited else "New comment"
            candidates.append((activity_time, f"{marker} {label} by *{author}*: {body}"))
    for attachment in activity.get("attachments", []):
        created = str(attachment.get("created", ""))
        author = _plain_text((attachment.get("author") or {}).get("displayName", "Unknown author"))
        filename = _plain_text(attachment.get("filename", "Unnamed file"))
        size = attachment.get("size")
        size_text = f", {size} bytes" if isinstance(size, int) else ""
        candidates.append((created, f"🟩 New file attached by *{author}*: {filename}{size_text}"))

    digest: list[str] = []
    for created, detail in sorted(candidates, key=lambda item: item[0], reverse=True):
        try:
            event_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            if event_time.astimezone(timezone.utc) <= threshold:
                continue
        except ValueError:
            continue
        if len(detail) > 1200:
            detail = detail[:1197].rstrip() + "..."
        display_time = event_time.strftime("%Y-%m-%dT%H:%M")
        digest.append(f"{display_time} — {detail}")
        if len(digest) == 10:
            break
    return digest


def _with_activity_section(content: str, activity: dict) -> str:
    start = "<!-- Jira activity start -->"
    end = "<!-- Jira activity end -->"
    lines = [start, "## Recent Jira activity", ""]
    lines.extend(_activity_lines(activity) or ["- No comments or field changes found."])
    lines.append(end)
    section = "\n".join(lines)
    if start in content and end in content:
        before = content.split(start, 1)[0].rstrip()
        after = content.split(end, 1)[1].lstrip("\n")
        content = before + "\n\n" + section + ("\n" + after if after else "\n")
    else:
        content = content.rstrip() + "\n\n" + section + "\n"
    return content


def _load_issue_activity(issue_key: str) -> dict | None:
    path = ACTIVITY_DIR / f"{issue_key}.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalDataError("Could not read local Jira activity data safely.") from exc
    if not isinstance(value, dict):
        raise LocalDataError("Local Jira activity data has an unexpected format.")
    return value


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
            digest_event = event
            if event == "Updated in Jira; no tracked fields changed":
                digest_event = "🟨 *Updated in Jira; no tracked fields changed*"
            else:
                digest_event = event
            new_updates.append(
                f"• {slack_issue_label(key, site_url)} — {current['summary'] or '(no summary)'} "
                f"({current['status'] or 'Unknown'}): {digest_event}"
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
        new_updates.append(
            f"• {slack_issue_label(key, site_url)} — no longer appears in your assigned-issues search"
        )

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


def local_ticket_keys() -> list[str]:
    """Return valid Jira keys represented by the existing local ticket baseline."""
    if not TICKETS_DIR.is_dir():
        raise LocalDataError("No local ticket baseline exists under .jira-data/tickets/.")
    return sorted(
        path.stem for path in TICKETS_DIR.glob("*.md")
        if ISSUE_KEY_PATTERN.fullmatch(path.stem)
    )


def save_issue_activity(issue_key: str, activity: dict) -> Path:
    """Atomically save comments and changelog separately from ticket snapshots."""
    path = ACTIVITY_DIR / f"{issue_key}.json"
    _atomic_write(path, json.dumps(activity, indent=2, ensure_ascii=False) + "\n")
    return path


def update_ticket_activity(issue_key: str, activity: dict) -> None:
    """Refresh the activity section in an existing ticket file without replacing its history."""
    path = TICKETS_DIR / f"{issue_key}.md"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LocalDataError("Could not read a local ticket file safely.") from exc
    _atomic_write(path, _with_activity_section(content, activity))


def digest_text(updates: list[str]) -> str:
    return "*Jira updates since the previous successful Slack post*\n" + "\n".join(updates)
