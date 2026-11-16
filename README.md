# jira_status_updates
MCP reading, analysis, and summarizing upates to any Jira ticket assigned to me in ACCESS-CI Atlassian env

See [the Jira snapshots and four-hour Slack update design](docs/jira-digest-design.md)
for the read-only Jira workflow and local storage approach.

## Local Jira updater

This is a one-shot local command. Each run reads unresolved, non-Done issues
assigned to the authenticated Jira user, refreshes one Markdown file per issue under the
Git-ignored `.jira-data/tickets/` folder, and posts a compact digest to Slack
when there are changes. Jira access is read-only. The same command can be run
every four hours later with a local scheduler; it does not schedule itself.

### Setup

1. Copy `.jira-status.env.example` to `.jira-status.env` and set
   `JIRA_API_BASE_URL` and `JIRA_EMAIL`. Set `JIRA_SITE_URL` if you want
   clickable Jira links in the ticket files.
2. In macOS Keychain, add the Jira token as a generic password named
   `Jira Updates API Cred` (the account is your Atlassian email) and the
   Slack URL as `Jira Updates Webhook` (account can be blank).
3. Open Terminal in this repository and run the commands below.

For a scoped Jira API token, the API base URL uses
`https://api.atlassian.com/ex/jira/YOUR_CLOUD_ID`. For an unscoped token, use
your Jira Cloud site URL. The token and webhook are read from Keychain at
runtime; never put their values in `.jira-status.env` or source files.

### Easy checks

```sh
# Check Jira access and preview the changes; writes no files and sends no Slack message.
python3 -m jira_updates --dry-run

# First local setup: write snapshots and queue changes; sends nothing to Slack.
python3 -m jira_updates --local-only

# Read comments, changelog, and attachment metadata for cached tickets into ticket files
# and .jira-data/activity/.
python3 -m jira_updates --fetch-activity

# Test only the Slack webhook with a clearly labeled message; does not call Jira.
python3 -m jira_updates --test-slack

# Normal run: refresh local files and post any pending digest.
python3 -m jira_updates
```

The initial local-only sync creates a baseline and does not send all existing
tickets as a Slack digest. Later changes are recorded in each ticket file and
queued until a successful Slack post. No automated schedule is configured.
