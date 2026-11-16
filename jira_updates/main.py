"""One-shot Jira sync with preview, local-only, and Slack test modes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys

from jira_updates.config import (
    JIRA_TOKEN_ITEM,
    SLACK_WEBHOOK_ITEM,
    ConfigurationError,
    jira_config,
    keychain_secret,
)
from jira_updates.jira_api import JiraRequestError, fetch_assigned_issues, fetch_issue_activity
from jira_updates.local_store import (
    LocalDataError,
    activity_digest_lines,
    activity_summary,
    clear_pending_digest,
    digest_text,
    load_state,
    local_ticket_keys,
    prepare_sync,
    save_issue_activity,
    save_sync,
    update_ticket_activity,
)
from jira_updates.slack import SlackPostError, post_message


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m jira_updates",
        description="Refresh your Jira ticket files and optionally post a Slack digest.",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch Jira and preview changes; do not write files or post to Slack.",
    )
    modes.add_argument(
        "--local-only",
        action="store_true",
        help="Refresh local ticket files and queue the digest; do not post to Slack.",
    )
    modes.add_argument(
        "--test-slack",
        action="store_true",
        help="Post one clearly labeled test message; does not contact Jira.",
    )
    modes.add_argument(
        "--fetch-activity",
        action="store_true",
        help="Fetch comments, changelog, and attachment metadata for cached tickets.",
    )
    return parser.parse_args()


def _jira_run(dry_run: bool, local_only: bool) -> int:
    config = jira_config()
    token = keychain_secret(JIRA_TOKEN_ITEM, config.email)
    print("Fetching issues assigned to the authenticated Jira user…")
    issues = fetch_assigned_issues(config.api_base_url, config.email, token)
    checked_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")
    state = load_state()
    new_state, file_contents, new_updates = prepare_sync(issues, state, checked_at, config.site_url)
    changed_keys = [
        str(issue.get("key", "")) for issue in issues
        if issue.get("key") in state.get("issues", {})
        and str((issue.get("fields") or {}).get("updated", ""))
        != str(state["issues"][issue["key"]].get("updated", ""))
    ]
    activity_updates = {
        key: fetch_issue_activity(config.api_base_url, config.email, token, key)
        for key in changed_keys
    }
    if activity_updates and not dry_run:
        for key, activity in activity_updates.items():
            save_issue_activity(key, activity)
        new_state, file_contents, new_updates = prepare_sync(
            issues, state, checked_at, config.site_url
        )
    for key, activity in activity_updates.items():
        prior_updated = state["issues"][key].get("updated")
        details = activity_digest_lines(activity, prior_updated)
        if details:
            new_state["pending_digest"].append(
                f"• `{key}` — recent Jira activity:\n" + "\n".join(f"  {line}" for line in details)
            )
    combined_updates = new_state["pending_digest"]

    if dry_run:
        print(f"Found {len(issues)} currently assigned issue(s). No local files were changed.")
        for key, activity in activity_updates.items():
            print(f"\nRecent Jira activity for {key}:")
            for line in activity_summary(activity, state["issues"][key].get("updated")) or [
                "- No new comments or field changes found."
            ]:
                print(line)
        if combined_updates:
            print("\n" + digest_text(combined_updates))
        else:
            print("No changes since the previous local sync.")
        return 0

    save_sync(new_state, file_contents, set(state.get("issues", {})))
    print(f"Updated {len(file_contents)} local ticket file(s) under .jira-data/tickets/.")
    if local_only:
        print("Slack was not contacted; pending digest updates are saved for the next normal run.")
        return 0

    if not combined_updates:
        print("No ticket changes to send; Slack was not contacted.")
        return 0

    webhook = keychain_secret(SLACK_WEBHOOK_ITEM)
    post_message(webhook, digest_text(combined_updates))
    clear_pending_digest(new_state)
    print("Slack digest posted successfully.")
    return 0


def _activity_run() -> int:
    config = jira_config()
    token = keychain_secret(JIRA_TOKEN_ITEM, config.email)
    issue_keys = local_ticket_keys()
    print(f"Fetching comments, changelog, and attachment metadata for {len(issue_keys)} local ticket(s)…")
    for issue_key in issue_keys:
        activity = fetch_issue_activity(config.api_base_url, config.email, token, issue_key)
        save_issue_activity(issue_key, activity)
        update_ticket_activity(issue_key, activity)
    print(f"Saved issue activity under .jira-data/activity/ for {len(issue_keys)} ticket(s).")
    return 0


def main() -> None:
    args = _arguments()
    try:
        if args.test_slack:
            webhook = keychain_secret(SLACK_WEBHOOK_ITEM)
            post_message(webhook, "[TEST] Jira Updates webhook is connected.")
            print("Test message posted to Slack.")
            return

        if args.fetch_activity:
            _activity_run()
            return

        _jira_run(dry_run=args.dry_run, local_only=args.local_only)
    except (ConfigurationError, JiraRequestError, LocalDataError, SlackPostError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
