# Jira ticket snapshots and four-hour Slack updates

## Goal

Read unresolved issues outside the Done status category assigned to the
authenticated Jira user, keep a readable local
snapshot and field-change history per issue, and send a short Slack digest on
the same four-hour cadence. Jira access is read-only. The one-shot starter
script is implemented; no schedule or credentials are configured here.

## Recommended first version

Run a small Python command locally every four hours. Use Jira Cloud's REST API
to search with `assignee = currentUser() AND resolution IS EMPTY AND
statusCategory != Done ORDER BY updated DESC`, request only
the fields the snapshots and digest need, and follow the API's pagination
token until all matching issues have been fetched. Keep one current file per
issue, named by its Jira key (for example `PROJ-123.md`); put the summary
inside the file rather than using an API-provided title in a path. Store
snapshots and sync state under `.jira-data/`, which is ignored by Git. Each
successful run sends one Slack digest covering changes since the previous
successful post. Include only the date, issue key, status, and a short change
indicator or link. Do not send descriptions, comments, or full snapshots to
Slack.

Suggested eventual layout:

```text
.jira-data/
  tickets/
    PROJ-123.md
    PROJ-456.md
  activity/
    PROJ-123.json             # opt-in comments and changelog cache
  state.json                 # last seen issue fields and pending Slack digest
jira_updates/
  __main__.py
  config.py
  jira_api.py
  local_store.py
  main.py
  slack.py
.jira-status.env.example
```

The files remain on this machine and outside version control. Git history is
not needed for the first version; if a durable shared archive is later wanted,
decide its access, retention, and review rules before tracking ticket content.

## Access and delivery

- Confirm the site is Jira Cloud and use the account's exact site/cloud ID.
- Use a dedicated, revocable Atlassian API token restricted to the minimum
  Jira read scopes the selected API requires. Token scopes do not grant access
  beyond the user's Jira permissions. For a scoped API token, Atlassian
  documents the `api.atlassian.com/ex/jira/{cloudId}` API base; the direct
  site URL is used for unscoped API tokens. Use the account email plus token
  for the documented basic-auth flow, or choose OAuth 2.0 if this grows into a
  distributable integration.
- Keep Jira credentials and the Slack webhook in the OS credential store or
  injected environment variables. Never put them in this repository, logs, or
  ticket files. A Slack incoming webhook is channel-bound; install/configure
  it for the new private channel and keep its URL secret.
- Schedule locally with `launchd` on macOS (or the host's scheduler). Avoid a
  hosted workflow for the first version: it would move full ticket snapshots
  off this machine and would require a write-back strategy to preserve them.
- The script should only make Jira GET/search requests and one Slack POST.
  It must not create, edit, transition, comment on, or assign issues.

## Privacy and reliability choices

- Start with key, summary, status, priority, updated timestamp, and Jira URL.
  Add descriptions or comments only if there is a clear need; those fields can
  contain sensitive material. Never download attachments for this use case.
- Keep the digest short and useful. Retain pending digest entries until Slack
  confirms delivery so a failed post is retried on the next run. Send no Slack
  message if there are no changes.
- Write each snapshot atomically after a complete successful fetch. Use stable
  Jira keys for filenames, normalize content to plain text/Markdown, and store
  the last successful sync marker locally. Handle pagination and rate limits;
  retry transient failures with bounded backoff. Do not advance the sync marker
  when a fetch fails.
- Keep unsent digest entries in the local state file. After a successful Slack
  response, clear the queue. If delivery fails, retain it for the next run and
  report the failure without printing credentials.
- Treat local snapshots as sensitive even though they are Git-ignored. They
  still exist on disk and may be included by machine backups or indexing tools.

## Slack app choice

Use a small Slack app with an Incoming Webhook for the first version. It is a
simple one-way post to the selected channel and fits the requirement that only
the digest goes to Slack. Use a bot token with `chat:write` instead if the
integration later needs to choose among channels, update/delete messages, or
use richer API behavior. The ChatGPT app for Slack is for people to ask
ChatGPT questions, summarize Slack content, and draft posts; it is not the
delivery mechanism for a scheduled Jira script.

## Suggested implementation sequence

1. Confirm Jira Cloud versus Data Center, the site/cloud ID, the desired
   schedule/time zone, and whether issue descriptions or comments belong in
   local snapshots.
2. Create the least-privileged Jira credential and a channel-specific Slack
   webhook; store both outside the repository.
3. Run `python3 -m jira_updates --dry-run` to preview the issue set and changes.
4. Use `--local-only` to write snapshots without posting, then
   `--test-slack` to send a labeled test message.
5. Add a local four-hour schedule only after successful manual runs. The same
   one-shot command refreshes ticket files and posts any pending Slack digest.

## Documentation

When a later sync sees an issue's Jira `updated` timestamp change, it also reads
that issue's comments and changelog and adds a recent activity section to its
ticket file and includes only comments added or edited and attachment names/metadata newer
than the previous snapshot in the Slack digest. It does not download attachment
contents. Set `JIRA_SITE_URL` to make issue keys clickable in Slack. The opt-in
`python3 -m jira_updates --fetch-activity` command refreshes
activity for every locally cached ticket; it does not post to Slack. Raw Jira
activity is also saved under `.jira-data/activity/`. Treat it as sensitive local
ticket content.

- [Jira Cloud issue search API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)
- [Atlassian API tokens](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/)
- [Jira REST API basic authentication](https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/)
- [Slack Incoming Webhooks](https://api.slack.com/messaging/webhooks)
- [Using ChatGPT in Slack](https://help.openai.com/en/articles/12462158-chatgpt-app-for-slack)
