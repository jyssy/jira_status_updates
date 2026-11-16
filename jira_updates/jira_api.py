"""Read the authenticated user's assigned Jira issues."""

from __future__ import annotations

import base64
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class JiraRequestError(RuntimeError):
    """Raised for Jira transport or API errors without exposing response data."""


def fetch_assigned_issues(api_base_url: str, email: str, api_token: str) -> list[dict]:
    """Fetch every currently assigned issue, requesting only digest fields."""
    credentials = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
    }
    jql = (
        "assignee = currentUser() AND resolution IS EMPTY "
        "AND statusCategory != Done ORDER BY updated DESC"
    )
    issues: list[dict] = []
    next_page_token: str | None = None

    while True:
        params = {
            "jql": jql,
            "maxResults": "100",
            "fields": "summary,status,updated,priority",
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        request = Request(
            f"{api_base_url}/rest/api/3/search/jql?{urlencode(params)}",
            headers=headers,
            method="GET",
        )

        for attempt in range(4):
            try:
                with urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    retry_after = exc.headers.get("Retry-After", "1")
                    try:
                        delay = max(1, min(int(retry_after), 60))
                    except ValueError:
                        delay = 2 ** attempt
                    time.sleep(delay)
                    continue
                raise JiraRequestError(f"Jira returned HTTP {exc.code}.") from None
            except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise JiraRequestError(f"Jira request failed: {type(exc).__name__}.") from None

        issues.extend(payload.get("issues", []))
        if payload.get("isLast", True):
            break
        next_page_token = payload.get("nextPageToken")
        if not next_page_token:
            raise JiraRequestError("Jira indicated another page but returned no page token.")

    return issues
