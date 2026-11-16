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
from jira_updates.jira_api import JiraRequestError, fetch_assigned_issues
from jira_updates.local_store import (
    LocalDataError,
    clear_pending_digest,
    digest_text,
    load_state,
    prepare_sync,
    save_sync,
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
    return parser.parse_args()


def _jira_run(dry_run: bool, local_only: bool) -> int:
    config = jira_config()
    token = keychain_secret(JIRA_TOKEN_ITEM, config.email)
    print("Fetching issues assigned to the authenticated Jira user…")
    issues = fetch_assigned_issues(config.api_base_url, config.email, token)
    checked_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")
    state = load_state()
    new_state, file_contents, new_updates = prepare_sync(issues, state, checked_at, config.site_url)
    combined_updates = new_state["pending_digest"]

    if dry_run:
        print(f"Found {len(issues)} currently assigned issue(s). No local files were changed.")
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


def main() -> None:
    args = _arguments()
    try:
        if args.test_slack:
            webhook = keychain_secret(SLACK_WEBHOOK_ITEM)
            post_message(webhook, "[TEST] Jira Updates webhook is connected.")
            print("Test message posted to Slack.")
            return

        _jira_run(dry_run=args.dry_run, local_only=args.local_only)
    except (ConfigurationError, JiraRequestError, LocalDataError, SlackPostError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
