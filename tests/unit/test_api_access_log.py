"""The API's own access log must not keep a credential it was handed in a URL.

`deploy/nginx-http.conf`'s `sp_no_query` log format covers the proxy. Nothing covers
uvicorn, which quotes the request line - query string included - in its access log,
and `scripts/serve.py` runs without nginx in front of it on the local-development
path and inside the API container, whose log `docker compose logs api` keeps.

So the same two query parameters are masked twice: `?ticket=`, which is what a
browser WebSocket can carry (see `api/tickets.py`), and `?token=`, which is what an
`<img>` stream tag can carry (see `deps.viewer_user`). Everything else in the line -
the path, the status, the other parameters a page is paged with - stays readable,
because an access log that redacts the traffic is not an access log.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import logging.config
from pathlib import Path

import pytest

from smart_parking.api.access_log import CredentialMaskFilter, redact_credentials

ROOT = Path(__file__).resolve().parents[2]


def line(text: str) -> str:
    """The access line as uvicorn hands it to logging, run through the filter.

    Both shapes at once: the stock formatter interpolates a `request_line` extra,
    and the message itself is built from `%s` arguments. Which one a version uses is
    not this project's decision, so the filter covers either route.
    """
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        None,
        None,
        '%s - "%s" %s',
        ("127.0.0.1:52104", text, 101),
        None,
    )
    record.request_line = text  # type: ignore[attr-defined]
    assert CredentialMaskFilter().filter(record) is True
    return record.getMessage()


def test_a_socket_ticket_is_masked_and_the_path_stays_readable():
    out = line("GET /ws/live?ticket=Ab3dEf__gHi7jKl&role=ADMIN HTTP/1.1")
    assert "ticket=***" in out
    assert "Ab3dEf__gHi7jKl" not in out
    # The rest of the line is the point of the log: what was asked for, how it went.
    assert "/ws/live" in out
    assert "role=ADMIN" in out
    assert '" 101' in out


def test_the_request_line_extra_is_masked_too():
    # A handler using uvicorn's format reads `%(request_line)s` rather than the
    # message, so the attribute has to be scrubbed where it is stored and not only
    # where it happens to be printed.
    record = logging.LogRecord("uvicorn.access", logging.INFO, None, None, "", (), None)
    record.request_line = "GET /ws/live?ticket=Ab3dEf__gHi7jKl HTTP/1.1"  # type: ignore[attr-defined]
    CredentialMaskFilter().filter(record)
    assert record.request_line == "GET /ws/live?ticket=*** HTTP/1.1"  # type: ignore[attr-defined]


def test_a_stream_token_is_masked_because_an_img_tag_cannot_send_a_header():
    out = line("GET /api/v1/cameras/gate_cam1/stream.mjpg?token=eyJhbGciOi HTTP/1.1")
    assert "token=***" in out
    assert "eyJhbGciOi" not in out


def test_a_request_with_nothing_to_hide_is_left_exactly_alone():
    for text in (
        "GET /api/v1/sessions?limit=10&offset=20&status=ACTIVE HTTP/1.1",
        "GET /health HTTP/1.1",
        "POST /api/v1/auth/login HTTP/1.1",
    ):
        assert line(text) == f'127.0.0.1:52104 - "{text}" 101'


def test_the_mask_leaves_no_substring_that_can_be_read_back():
    # `TICKET=` in a hand-built URL, and a value that itself looks like a second
    # parameter: neither may leave part of the secret standing.
    assert redact_credentials("/ws/live?TICKET=secret1") == "/ws/live?TICKET=***"
    assert redact_credentials("/ws/live?ticket=abc&ticket=def") == "/ws/live?ticket=***&ticket=***"


def test_a_name_that_merely_ends_in_ticket_is_not_a_credential():
    # The boundary is a word boundary before the key, so a column called
    # `parking_ticket` in a filter stays legible in the log.
    assert redact_credentials("/sessions?parking_ticket_id=77") == "/sessions?parking_ticket_id=77"


@pytest.fixture(scope="module")
def serve():
    """`scripts/serve.py` as a module, without a port being opened.

    The script is not on the import path the suite runs from, so it is loaded by
    file. Its `main()` is guarded, so importing it starts nothing.
    """
    spec = importlib.util.spec_from_file_location("serve", ROOT / "scripts" / "serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_serve_hangs_the_mask_on_uvicorns_access_logger(serve):
    config = serve.uvicorn_log_config()
    assert config["loggers"]["uvicorn.access"]["filters"] == ["credentials"]
    assert config["filters"]["credentials"]["()"] is CredentialMaskFilter


def test_serve_masks_the_websocket_handshake_line_from_its_own_logger(serve):
    """The handshake is not on `uvicorn.access`, and this is the line that was leaking.

    `uvicorn/protocols/websockets/*` logs the accept itself, on `uvicorn.error`:

        '%s - "WebSocket %s" [accepted]' % (client, get_path_with_query_string(scope))

    A filter on the access logger alone therefore masked every page request the
    dashboard makes and left each socket's one-time ticket in the log - found by
    running a server, connecting, and grepping the file it wrote. Which logger's
    filters run is decided by where the record is *emitted*, not by which handler
    prints it, so `uvicorn.error` has to be named.
    """
    config = serve.uvicorn_log_config()
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        assert "credentials" in config["loggers"][name]["filters"], name


def test_the_handshake_record_uvicorn_actually_builds_is_masked():
    """The record shape copied from uvicorn's source, not a plausible invention.

    Two `%s` arguments, the credential in the *second* one, and an empty message
    pattern of its own - which is why the filter walks `record.args` rather than
    only the message.
    """
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, None, None,
        '%s - "WebSocket %s" [accepted]',
        ("127.0.0.1:50853", "/ws/live?ticket=Ab3dEf__gHi7jKl3oN5pQ7rS9tU1vW3xY5zA7bC9d"),
        None,
    )
    CredentialMaskFilter().filter(record)
    written = record.getMessage()
    assert "ticket=***" in written
    assert "Ab3dEf__gHi7jKl" not in written
    # The address and the path stay, because they are the log's whole subject.
    assert "127.0.0.1:50853" in written and "/ws/live" in written


def test_a_rejected_handshake_is_masked_the_same_way():
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, None, None,
        '%s - "WebSocket %s" %d',
        ("127.0.0.1:50856", "/ws/live?ticket=Ab3dEf__gHi7jKl3oN5pQ7r", 403),
        None,
    )
    CredentialMaskFilter().filter(record)
    out = record.getMessage()
    assert "ticket=***" in out and "Ab3dEf__gHi7jKl" not in out
    assert out.endswith("403")  # a non-string argument survives


def test_an_integer_argument_survives_the_walk_through_args():
    # `%d`-style arguments must not be turned into strings by the masking pass:
    # logging formats `record.args` itself, and a str where a number is expected is
    # a TypeError inside the handler, on a line that had nothing to hide.
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, None, None,
        "%s closed with code %d", ("/ws/live?ticket=abc", 1000), None,
    )
    CredentialMaskFilter().filter(record)
    assert record.args[1] == 1000 and isinstance(record.args[1], int)
    assert record.getMessage() == "/ws/live?ticket=*** closed with code 1000"


def test_serve_keeps_uvicorns_own_format_and_handlers(serve):
    # The config is uvicorn's, copied and extended: re-declaring the formatter here
    # would be a second place the access line's shape is decided, and the two
    # versions would disagree quietly.
    import uvicorn.config

    default = uvicorn.config.LOGGING_CONFIG
    built = serve.uvicorn_log_config()
    assert built["formatters"]["default"]["fmt"] == default["formatters"]["default"]["fmt"]
    assert built["loggers"]["uvicorn.access"]["handlers"] == default["loggers"]["uvicorn.access"]["handlers"]
    assert built["loggers"]["uvicorn"]["handlers"] == default["loggers"]["uvicorn"]["handlers"]
    assert built["handlers"]["default"] == default["handlers"]["default"]


def test_dictconfig_builds_the_filter_the_way_serve_configures_it(serve):
    """The `()` factory form is what `logging.config.dictConfig` has to accept."""
    spec = serve.uvicorn_log_config()["filters"]["credentials"]
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"credentials": spec},
            "loggers": {
                "test_api_access_log.sandbox": {
                    "level": "INFO",
                    "filters": ["credentials"],
                    "handlers": [],
                }
            },
        }
    )
    logger = logging.getLogger("test_api_access_log.sandbox")
    assert any(isinstance(f, CredentialMaskFilter) for f in logger.filters)


def test_a_logger_using_the_filter_writes_the_masked_line(serve):
    """End to end inside the logging module, without touching uvicorn's handlers."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter('"%(request_line)s" %(status_code)s'))
    handler.addFilter(CredentialMaskFilter())
    logger = logging.getLogger("test_api_access_log.writes")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            "unused",
            extra={"request_line": "GET /ws/live?ticket=Ab3dEf__gHi7jKl HTTP/1.1", "status_code": 101},
        )
    finally:
        logger.handlers = []
    written = stream.getvalue()
    assert "ticket=***" in written
    assert "Ab3dEf__gHi7jKl" not in written
