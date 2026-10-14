"""The deployment's own files, read as configuration rather than run as one.

These are the two claims a reviewer can only check by looking: that HTTPS is not
advertised when it is not configured, and that a credential in a URL is not copied
into a log file on the way past the proxy. Both are properties of text in `deploy/`,
and both are easy to undo by accident - one uncommented `listen 443` in a file that
ships an empty `deploy/ssl/`, one `access_log` switched back to nginx's built-in
format - which is why they are asserted here and not only in a comment.

Nothing in this file starts nginx or Docker. `test_api_access_log.py` covers the
same rule on the API's own access log, where the other copy of a handshake URL is
kept; the proxy rules below are checked as text, and were also verified once by hand
against a real container (see the notes in `deploy/nginx-site.conf`).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def read(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def directives(name: str) -> str:
    """The file's text with nginx's comment lines dropped.

    The comments in these files talk about `listen 443` and `ssl_certificate` - the
    point of half of them is to say why they are absent - so a test that a file
    "contains no TLS" has to mean its directives, not its prose.
    """
    return "\n".join(
        line for line in read(name).splitlines() if not line.strip().startswith("#")
    )


def compose(name: str = "docker-compose.yml") -> dict:
    return yaml.load(read(name), Loader=ComposeLoader)


class ComposeLoader(yaml.SafeLoader):
    """`SafeLoader` with Compose's own tags accepted.

    PyYAML has never heard of `!override` and raises a ConstructorError on the
    overlay file, whose mounts are exactly what is worth checking here. The tag
    annotates a sequence and says what to do with it during a merge, not what is in
    it, so reading it as the sequence is the faithful answer for a text assertion -
    and `docker compose config` is what validates the merge itself.
    """


ComposeLoader.add_multi_constructor(
    "!",
    lambda loader, tag, node: (
        loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node)
        if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)
    ),
)


def mounts(service: dict, target: str) -> list[str]:
    """The host side of every bind mount this service puts at `target`."""
    return [
        str(entry).split(":")[0]
        for entry in service.get("volumes", [])
        if str(entry).split(":")[1] == target
    ]


# -- HTTPS is a choice, not a default ---------------------------------------
def test_the_http_entry_configuration_speaks_no_tls_at_all():
    """`nginx.conf` is what runs when no certificates exist, so it must not pretend.

    A `listen 443 ssl` whose `ssl_certificate` is missing is not a disabled server
    block: it is a proxy that does not start. The commented-out form used to sit in
    this file waiting to be uncommented, which is the same shape as a half-built
    deployment - so the TLS listener moved to a file of its own and this one now has
    no TLS vocabulary in its directives at all.
    """
    http = directives("nginx.conf")
    for phrase in ("listen 443", "ssl_certificate", "ssl_protocols", "http2 on"):
        assert phrase not in http, phrase
    assert "listen 80;" in http


def test_the_tls_entry_configuration_names_the_files_it_needs():
    tls = directives("nginx.tls.conf")
    assert "listen 443 ssl;" in tls
    assert "ssl_certificate /etc/nginx/ssl/fullchain.pem;" in tls
    assert "ssl_certificate_key /etc/nginx/ssl/privkey.pem;" in tls
    # Unencrypted traffic is turned away rather than served, and it is turned away by
    # the listener rather than an `if`, which nginx cannot evaluate against a
    # certificate that may or may not be mounted.
    assert "return 301 https://$host$request_uri;" in tls


def test_both_entry_files_route_through_the_same_two_snippets():
    """One copy of the proxy rules, whatever the transport in front of them.

    The split is by nginx context, not by tidiness: the image's own config includes
    `conf.d/*.conf` at **http** level, so a `location` placed there is a startup
    error, and `upstream` inside a `server` is one too.
    """
    for name in ("nginx.conf", "nginx.tls.conf"):
        text = directives(name)
        assert "include /etc/nginx/snippets/http.conf;" in text, name
        assert "include /etc/nginx/snippets/site.conf;" in text, name
    site = directives("nginx-site.conf")
    for directive in ("upstream", "log_format", "limit_req_zone"):
        assert directive not in site, directive
    http = directives("nginx-http.conf")
    for directive in ("upstream api_backend", "log_format sp_no_query", "limit_req_zone"):
        assert directive in http, directive


def test_the_base_compose_publishes_80_and_nothing_else():
    """Port 443 with TLS disabled is the finding this line keeps from coming back.

    It was mapped while `deploy/ssl/` was empty, so the host had a listener that
    answered nothing and the file said nothing about why.
    """
    nginx = compose()["services"]["nginx"]
    assert nginx["ports"] == ["80:80"]
    assert mounts(nginx, "/etc/nginx/conf.d/default.conf") == ["./nginx.conf"]
    assert mounts(nginx, "/etc/nginx/ssl") == []


def test_the_tls_overlay_is_the_only_thing_that_opens_443():
    tls = compose("docker-compose.tls.yml")["services"]["nginx"]
    assert tls["ports"] == ["80:80", "443:443"]
    assert mounts(tls, "/etc/nginx/conf.d/default.conf") == ["./nginx.tls.conf"]
    assert mounts(tls, "/etc/nginx/ssl") == ["./ssl"]
    # `!override` is what makes the overlay replace the base mounts rather than add a
    # second file at the same container path: two entry configurations loaded at once
    # would define `upstream api_backend` twice and the proxy would not start.
    raw = read("docker-compose.tls.yml")
    assert "volumes: !override" in raw and "ports: !override" in raw
    # Its health check proves a TLS handshake completed, not that a port answers TCP.
    assert "https://localhost/health" in str(tls["healthcheck"]["test"])


# -- a credential in a URL must not be written down --------------------------
def test_the_access_log_format_quotes_the_path_and_not_the_request_line():
    """`$request` is the request line *with* the query string; `$uri` is not.

    Both of the credentials this API can only receive in a query string arrive
    through this proxy: a socket's `?ticket=` (a browser WebSocket cannot set an
    `Authorization` header - see `api/tickets.py`) and an `<img>` stream's `?token=`
    (see `deps.viewer_user`).
    """
    text = directives("nginx-http.conf")
    start = text.index("log_format sp_no_query")
    body = text[start : text.index(";", start)]
    assert "$request_method $uri $server_protocol" in body
    assert "$request " not in body
    assert "escape=default" in body  # a percent-encoded newline cannot forge a row
    assert "access_log /var/log/nginx/access.log sp_no_query;" in directives("nginx-site.conf")


def test_the_two_locations_that_carry_a_credential_quiet_the_upstream_errors():
    """The other place nginx quotes a whole URL: an upstream failure at `error`.

    Verified against a running container - with the API down, a request to /ws/ that
    carried a ticket produced
        [error] ... request: "GET /ws/live?ticket=... HTTP/1.1",
                      upstream: "http://127.0.0.1:8000/ws/live?ticket=..."
    which no access-log format has any influence over.
    """
    site = directives("nginx-site.conf")
    for location in (
        "location /ws/ {",
        "location ~* ^/api/v1/cameras/[^/]+/stream\\.mjpg$ {",
    ):
        block = site[site.index(location) :]
        head = block[: block.index("proxy_pass")]
        assert "error_log /var/log/nginx/error.log crit;" in head, location


def test_the_proxy_still_forwards_the_query_string_it_does_not_log():
    """Silence in the log is not a rewrite: the ticket has to reach the application.

    A `proxy_pass` with no URI forwards the original path and its arguments. Appending
    a URI to any of these lines drops `$args`, and every socket would then be refused
    with `token_missing` while the logs looked beautifully clean.
    """
    for line in directives("nginx-site.conf").splitlines():
        if "proxy_pass" in line:
            assert line.strip() == "proxy_pass http://api_backend;", line


def test_the_mjpeg_location_is_a_regex_so_it_beats_the_api_prefix():
    """Ordering in nginx is by type, not by file position.

    A prefix `/api/` match is overridden by a regex match, which is the only reason
    this stream escapes `proxy_buffering on` and the rate limit - a buffered
    multipart response never reaches an `<img>` tag at all.
    """
    site = directives("nginx-site.conf")
    start = site.index("location ~* ^/api/v1/cameras/[^/]+/stream\\.mjpg$ {")
    assert "proxy_buffering off" in site[start : start + 600]
    assert "limit_req zone=api" in site  # the API limit exists, just not for the stream


def test_no_compose_file_ships_a_value_for_a_secret():
    """Secrets in these files are variables with a message attached (`:?`), so a
    missing one stops `compose up` with an instruction instead of starting a service
    with an empty signing key or a default database password."""
    for name in ("docker-compose.yml", "docker-compose.tls.yml"):
        for service in compose(name)["services"].values():
            for key, value in (service.get("environment") or {}).items():
                if key.endswith(("SECRET", "PASSWORD")):
                    assert str(value).startswith("${"), f"{name}: {key}"


# -- the file that tells an operator what to set ------------------------------
def interpolated() -> set[str]:
    """Every `${NAME…}` the compose files ask the shell or `.env` to fill in."""
    names = set()
    for name in ("docker-compose.yml", "docker-compose.tls.yml"):
        names.update(re.compile(r"\$\{([A-Z_][A-Z0-9_]*)").findall(read(name)))
    return names


def example_keys() -> tuple[set[str], set[str]]:
    """(`NAME=` assignments, `# NAME=` mentions) in `deploy/.env.example`."""
    active, mentioned = set(), set()
    for line in read(".env.example").splitlines():
        match = re.compile(r"^(\s*#\s*)?([A-Z_][A-Z0-9_]*)\s*=").match(line)
        if match:
            (mentioned if match.group(1) else active).add(match.group(2))
    return active, mentioned


def test_every_variable_compose_needs_is_documented_for_the_operator():
    """A required variable nobody told you about is a deployment that fails mysteriously.

    `${HEALTH_EMAIL:?…}` stops `docker compose up` with a one-line message that names
    the file to edit, and `.env.example` *is* that file's template. It had no
    `HEALTH_EMAIL` or `HEALTH_PASSWORD` in it, so the documented first step - "copy the
    example and fill it in" - produced an `.env` that could not start the stack.
    """
    active, mentioned = example_keys()
    documented = active | mentioned
    missing = interpolated() - documented
    assert not missing, sorted(missing)
    # Both probe variables have to be *settable*, not merely explained: a commented
    # `# HEALTH_EMAIL=` would leave the operator copying a file that still fails.
    assert {"HEALTH_EMAIL", "HEALTH_PASSWORD"} <= active


def test_the_example_file_names_no_variable_that_nothing_reads():
    """The other direction of the same mistake, and the older one.

    `API_HOST`, `API_PORT`, `LOG_LEVEL`, `ENABLE_PIPELINES`, `ENABLE_METRICS` and
    `CORS_ALLOW_ORIGINS` were all listed as if compose or the app consumed them. None
    do: host, port, log level and the pipeline switches are `scripts/serve.py`
    arguments, and the CORS allow-list is `api.cors` in `configs/app.yaml`. An operator
    who edited them was configuring a fiction, which is worse than an error message.
    """
    read_by_compose = interpolated()
    # The two the application itself looks up, by name, through the config loader.
    read_by_the_app = {"SMART_PARKING_DATABASE_URL", "SMART_PARKING_JWT_SECRET"}
    assert example_keys()[0] <= read_by_compose | read_by_the_app, sorted(
        example_keys()[0] - read_by_compose - read_by_the_app
    )
