"""Post one message to the channel-bound Slack incoming webhook."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SlackPostError(RuntimeError):
    """Raised for webhook failures without exposing the webhook URL."""


def post_message(webhook_url: str, text: str) -> None:
    request = Request(
        webhook_url,
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            if response.status != 200 or body.strip() != "ok":
                raise SlackPostError(f"Slack webhook returned HTTP {response.status}.")
    except HTTPError as exc:
        raise SlackPostError(f"Slack webhook returned HTTP {exc.code}.") from None
    except (URLError, TimeoutError) as exc:
        raise SlackPostError(f"Slack webhook request failed: {type(exc).__name__}.") from None
