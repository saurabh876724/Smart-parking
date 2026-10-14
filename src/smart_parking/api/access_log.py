"""A uvicorn access log that does not keep the credentials in a URL.

nginx is not the only proxy in front of this application, and it is not the only
log: uvicorn's own access line is

    INFO:  127.0.0.1:52104 - "GET /ws/live?ticket=… HTTP/1.1" 101

which is the same request line the browser sent, query string and all. Running
`scripts/serve.py` without nginx - in development, in a container whose logs go
somewhere else, or on any host where `docker compose logs api` is readable - puts a
socket ticket or a stream token in that log, kept for as long as the log is.

The tickets are already designed to be worthless by the time anybody reads them
(one use, `api.ws_ticket_ttl_s` seconds), so this is defence in depth rather than
the fix. The fix is that the dashboard authenticates with a ticket at all.

A filter and not a formatter, because uvicorn's access log builds `request_line`
from the ASGI scope and hands it to logging as an extra attribute: rewriting the
formatted string would mean re-declaring uvicorn's own format, which is a thing to
get wrong quietly.

`log_config` in `scripts/serve.py` installs this on `uvicorn.access`,
`uvicorn.error` and `uvicorn`. The middle one is the reason the set is not just the
access logger: the WebSocket handshake is logged by the websocket protocol itself,
on `uvicorn.error`, as `'%s - "WebSocket %s" [accepted]'` with
`get_path_with_query_string(scope)` as the second argument - so a filter on
`uvicorn.access` alone masks the dashboard's API calls and leaves every socket
ticket in the file. Masking depends on which logger a record is *emitted* on, not
on which handler prints it, so all three are named.
"""

from __future__ import annotations

import re
from logging import Filter, LogRecord

# `?ticket=…&x=…` and `?token=…`: the two query parameters this API accepts as a
# credential. Deliberately not "anything that looks secret" - a filter that rewrites
# `?status=ACTIVE` makes the access log useless for the traffic it is there to show.
_CREDENTIAL = re.compile(r"(?i)\b(ticket|token)=([^&\s\"']+)")


def redact_credentials(text: str) -> str:
    """The same text with any `ticket=`/`token=` value replaced by `***`."""
    return _CREDENTIAL.sub(lambda m: f"{m.group(1)}=***", text)


class CredentialMaskFilter(Filter):
    """Masks a credential in the access line, then lets the record through.

    Every place a message can carry it is rewritten, because which one it is depends
    on the uvicorn version: `record.request_line`, which the stock formatter
    interpolates, a `record.msg` that is already the whole line, and `record.args`,
    which is how a `%s`-style access line reaches `getMessage()`.

    It never returns False. A filter that drops records would hide the traffic this
    log exists to show; the value is what is removed, not the request.
    """

    def filter(self, record: LogRecord) -> bool:
        request_line = getattr(record, "request_line", None)
        if isinstance(request_line, str):
            record.request_line = redact_credentials(request_line)
        if isinstance(record.msg, str):
            record.msg = redact_credentials(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_credentials(a) if isinstance(a, str) else a for a in record.args
            )
        return True
