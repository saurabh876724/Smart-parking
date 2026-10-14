"""Phase 8 configuration and seams: the API's sub-configs, the signing key, and who may talk to whom.

Three groups, and each exists because the architecture doc states the rule:

* `api:` in `configs/app.yaml` reaches `ApiConfig` and its sub-models, and a
  nonsense value fails at load time rather than at the first login. Nothing here
  invents a range: every refusal tested is a validator that exists.
* **The signing key has no path into the config file.** `ApiConfig` names an
  environment variable and a file, and `jwt_secret_for` resolves them in that
  order or raises. A default key would be an authentication system anyone who has
  read the source can bypass, so both failure paths are tested - including that
  the *reason* differs, because an operator has to know which one to fix.
* The layering the doc draws is checked from the source, not from an import
  graph nobody maintains: the AI layers may not know SQLAlchemy or FastAPI exist,
  the API may not read YAML or open a session, and the database may not know the
  API exists. `test_anpr_config.py` and `test_gate_config.py` do this for their
  own layers; this extends it to Phase 8's.
"""

from __future__ import annotations

from pathlib import Path
from re import Pattern, compile as compile_pattern

import pytest
from pydantic import ValidationError

from smart_parking.core.config import (
    ApiConfig,
    AppConfig,
    CorsConfig,
    MjpegConfig,
    PasswordConfig,
    SecretUnavailable,
    ServeCameraConfig,
    ServePipelineConfig,
    jwt_secret_for,
    load_config,
)
from smart_parking.db.base import Base
from smart_parking.db.models import MIGRATED_TABLES

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "smart_parking"

# A key long enough to pass the 32-byte floor, and distinct enough that a test can
# tell which of two sources it came from.
ENV_KEY = "environment-key-0123456789abcdef-0123456789abcdef"
FILE_KEY = "dev-file-key-0123456789abcdef-0123456789abcdef-xy"


def source(name: str) -> Path:
    return SRC / name


# -- the shape of the config --------------------------------------------------


def test_the_shipped_api_block_loads_and_matches_the_documented_defaults():
    api = load_config(ROOT / "configs" / "app.yaml", cameras_yaml=None, root=ROOT).api
    defaults = ApiConfig()
    assert api.prefix == defaults.prefix == "/api/v1"
    assert api.jwt_secret_env == "SMART_PARKING_JWT_SECRET"
    assert api.jwt_secret_file == "data/.secret"
    assert api.access_token_ttl_s == defaults.access_token_ttl_s
    assert api.refresh_token_ttl_s == defaults.refresh_token_ttl_s
    # The socket handshake's credential and the sign-in throttle are named in the
    # shipped file, and the file has to say what the model defaults to: an operator
    # who cannot find a knob in `app.yaml` is an operator who edits the code.
    assert api.ws_ticket_ttl_s == defaults.ws_ticket_ttl_s == 15
    assert api.login_throttle_window_s == defaults.login_throttle_window_s == 300
    assert api.login_throttle_max_failures == defaults.login_throttle_max_failures == 10
    assert (api.page_size, api.ws_queue_size) == (defaults.page_size, defaults.ws_queue_size)
    assert api.mjpeg.quality == defaults.mjpeg.quality
    assert api.pipeline.enabled is True
    # A served replay runs at its clip's own frame rate, and only the first pass of
    # a loop reaches the database. The shipped file has to say what the model
    # defaults to, or the two drift apart on the first deployment.
    assert api.pipeline.pace_replay is True is defaults.pipeline.pace_replay
    assert api.pipeline.first_pass_events_only is True is defaults.pipeline.first_pass_events_only
    # The doc's strict allow-list starts closed: an empty list is the shipped
    # answer, and Phase 9's dev server adds its own origin.
    assert api.cors.allow_origins == []
    assert api.cors.allow_credentials is False


def test_every_served_camera_names_a_configured_one():
    """`api.pipeline.cameras` is ids, and an id that is not in `cameras.yaml` is a
    stream that would 404 on its own first request."""
    cfg = load_config(root=ROOT)
    configured = {camera.id for camera in cfg.cameras}
    assert [entry.camera for entry in cfg.api.pipeline.cameras] and all(
        entry.camera in configured for entry in cfg.api.pipeline.cameras
    )


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ({"prefix": "api/v1"}, "must start with"),
        ({"access_token_ttl_s": 5}, "30 s"),
        ({"refresh_token_ttl_s": 29}, "30 s"),
        ({"page_size": 0}, "1-500"),
        ({"page_size": 501}, "1-500"),
        # A ticket shorter than a reconnect is a logout; one longer than a few
        # minutes is a token with a new name, in the URL the tickets exist to keep.
        ({"ws_ticket_ttl_s": 2}, "5-300"),
        ({"ws_ticket_ttl_s": 3600}, "5-300"),
        ({"login_throttle_max_failures": 0}, "positive"),
        ({"login_throttle_window_s": -1}, "positive"),
        ({"jwt_algorithm": "RS256"}, ""),
        ({"jwt_algorithm": "none"}, ""),
        # Someone pasting the key into YAML instead of the file it names.
        ({"jwt_secret_file": "x" * 200}, "is a path, not the secret"),
        ({"jwt_secret_file": "data/.secret\n0123"}, "is a path, not the secret"),
    ],
)
def test_a_nonsense_api_value_is_refused_at_load_time(bad, fragment):
    with pytest.raises(ValidationError) as caught:
        ApiConfig(**bad)
    assert fragment in str(caught.value)


def test_the_prefix_is_normalised_rather_than_rejected():
    assert ApiConfig(prefix="/api/v1/").prefix == "/api/v1"
    # A bare "/" would mount every route at "//health"; that is a mistake, not a
    # choice, so the default is what comes back.
    assert ApiConfig(prefix="/").prefix == "/api/v1"


def test_no_field_of_api_config_can_hold_a_secret_value():
    holding = [name for name in ApiConfig.model_fields if "secret" in name]
    assert holding == ["jwt_secret_env", "jwt_secret_file"]


@pytest.mark.parametrize("quality", [0, 96, 100])
def test_jpeg_quality_has_a_useful_range(quality):
    with pytest.raises(ValidationError):
        MjpegConfig(quality=quality)
    assert MjpegConfig(quality=95).quality == 95


def test_the_password_costs_are_configuration_because_they_are_hardware():
    cheap = PasswordConfig(memory_kib=8192, time_cost=1, parallelism=1)
    assert (cheap.memory_kib, cheap.time_cost, cheap.parallelism) == (8192, 1, 1)
    assert PasswordConfig().min_length == 10 and PasswordConfig().max_length == 128


@pytest.mark.parametrize(
    "bad",
    [
        {"memory_kib": 8191},  # below Argon2's own floor
        {"time_cost": 0},
        {"parallelism": 0},
        {"min_length": 7},
    ],
)
def test_a_password_parameter_below_its_floor_is_refused(bad):
    with pytest.raises(ValidationError):
        PasswordConfig(**bad)


def test_cors_will_not_take_a_wildcard():
    with pytest.raises(ValidationError) as caught:
        CorsConfig(allow_origins=["*"])
    assert "5173" in str(caught.value)
    assert CorsConfig(allow_origins=["http://localhost:5173/"]).allow_origins == [
        "http://localhost:5173"
    ]


def test_a_served_stream_defaults_to_the_conservative_choices():
    entry = ServeCameraConfig(camera="gate_cam1")
    # Both of these are disclosure/performance decisions, and the quiet answer to
    # an unspecified one is the safe one.
    assert entry.read_plates is False
    assert entry.loop is True
    assert entry.tracks_file is None
    with pytest.raises(ValidationError):
        ServeCameraConfig()


def test_the_pipeline_block_defaults_to_streaming_nothing_rather_than_guessing():
    pipeline = ServePipelineConfig()
    assert pipeline.enabled is True
    assert pipeline.cameras == []
    assert pipeline.replay is True
    assert pipeline.frame_stride is None
    assert pipeline.send_summary_over_ws is True
    # A stream is watched at the clip's own rate, and a loop's second pass is not
    # new history: both are the answers a deployment gets when it says nothing.
    assert pipeline.pace_replay is True
    assert pipeline.first_pass_events_only is True
    assert pipeline.model_validate({"cameras": [{"camera": "a"}]}).cameras[0].camera == "a"


# -- the signing key ----------------------------------------------------------


def configured(tmp_path: Path, **api) -> AppConfig:
    """A project root of its own, so no developer's `data/.secret` is ever read."""
    return AppConfig(project_root=tmp_path, api=ApiConfig(**api))


def test_the_environment_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SMART_PARKING_JWT_SECRET", raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".secret").write_text(FILE_KEY, encoding="utf-8")
    monkeypatch.setenv("SMART_PARKING_JWT_SECRET", ENV_KEY)
    assert jwt_secret_for(configured(tmp_path)) == ENV_KEY


def test_the_dev_file_is_the_fallback_and_is_read_verbatim(tmp_path, monkeypatch):
    monkeypatch.delenv("SMART_PARKING_JWT_SECRET", raising=False)
    (tmp_path / "data").mkdir()
    # A trailing newline is what `echo` and every editor produce; a key with one
    # in it verifies nothing, so it is stripped rather than signed with.
    (tmp_path / "data" / ".secret").write_text(FILE_KEY + "\n", encoding="utf-8")
    assert jwt_secret_for(configured(tmp_path)) == FILE_KEY


def test_an_empty_environment_variable_falls_through_to_the_file(tmp_path, monkeypatch):
    """A set-but-blank variable is a deployment that forgot the value, and the
    message has to name the variable to fix."""
    monkeypatch.setenv("SMART_PARKING_JWT_SECRET", "   ")
    with pytest.raises(SecretUnavailable) as caught:
        jwt_secret_for(configured(tmp_path))
    assert "SMART_PARKING_JWT_SECRET" in str(caught.value)
    assert "dev_secret.py" in str(caught.value)


def test_a_missing_key_names_both_ways_to_supply_one(tmp_path, monkeypatch):
    monkeypatch.delenv("SMART_PARKING_JWT_SECRET", raising=False)
    with pytest.raises(SecretUnavailable) as caught:
        jwt_secret_for(configured(tmp_path))
    message = str(caught.value)
    assert "no JWT signing key" in message
    assert "set SMART_PARKING_JWT_SECRET" in message
    # The path it looked at, separator and all: "create the file" is only
    # actionable if the message says which file.
    assert str(tmp_path / "data" / ".secret") in message


def test_a_short_key_is_refused_and_says_what_it_found(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_PARKING_JWT_SECRET", "hunter2")
    with pytest.raises(SecretUnavailable) as caught:
        jwt_secret_for(configured(tmp_path))
    assert "7 bytes" in str(caught.value)
    assert "$SMART_PARKING_JWT_SECRET" in str(caught.value)

    monkeypatch.delenv("SMART_PARKING_JWT_SECRET")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".secret").write_text("too-short", encoding="utf-8")
    with pytest.raises(SecretUnavailable) as caught:
        jwt_secret_for(configured(tmp_path))
    # The file's path, not the environment's name: these are different mistakes.
    assert "data" in str(caught.value) and "SMART_PARKING_JWT_SECRET" not in str(caught.value)


def test_the_config_supplied_api_block_is_the_one_used(tmp_path, monkeypatch):
    """A caller may pass an `ApiConfig` that is not the one on `AppConfig` (the
    app builds its issuer from either), so the resolution follows the argument."""
    monkeypatch.delenv("SMART_PARKING_JWT_SECRET", raising=False)
    monkeypatch.setenv("OTHER_SECRET", ENV_KEY)
    cfg = configured(tmp_path)
    assert jwt_secret_for(cfg, ApiConfig(jwt_secret_env="OTHER_SECRET")) == ENV_KEY
    with pytest.raises(SecretUnavailable):
        jwt_secret_for(cfg)


# -- the tables the migration owes ---------------------------------------------


def test_every_orm_table_is_in_the_list_health_checks():
    assert MIGRATED_TABLES == tuple(sorted(Base.metadata.tables))
    # Named here as well as in `db/models.py`, so a model that vanishes from the
    # metadata fails this test rather than quietly shrinking the check.
    assert {
        "cameras",
        "parking_slots",
        "license_plates",
        "vehicles",
        "parking_sessions",
        "slot_occupancy_history",
        "detection_logs",
        "users",
        "audit_logs",
    } == set(MIGRATED_TABLES)


# -- the layering -------------------------------------------------------------

YAML_READING = compile_pattern(r"\bimport yaml\b|yaml\.(?:safe_)?load|load_config\(")
# `load_slots`/`load_track_file` are excluded on purpose: geometry and labelled
# replay files are read by the composition root, which is exactly what
# `scripts/run_occupancy.py` and `services/persistence.py` do. `app.yaml` is not.
ORM_IN_API = compile_pattern(
    # `smart_parking.db.session` in an import is the layer being *named*, not
    # used; what is refused is a router holding a session and writing through it.
    r"\bfrom sqlalchemy import\b|\.session\.\w|\.session\)|\.(?:flush|commit)\("
)
AI_LAYERS = ("anpr", "detection", "parking", "pipeline", "tracking", "video")
KNOWS_THE_DATABASE = compile_pattern(r"\bsqlalchemy\b|smart_parking\.db|\bfastapi\b")
KNOWS_THE_API = compile_pattern(r"smart_parking\.api|\bfastapi\b")


def offenders(root: Path, pattern: Pattern) -> list[str]:
    return [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]


def test_the_api_layer_never_reads_the_configuration_file():
    assert offenders(SRC / "api", YAML_READING) == []


def test_the_api_layer_never_opens_a_database_session():
    """Every write goes through a repository method, so the one place that
    commits - `Database.scope` - stays the only owner of the unit of work."""
    assert offenders(SRC / "api", ORM_IN_API) == []


@pytest.mark.parametrize("layer", AI_LAYERS)
def test_the_ai_layers_do_not_know_about_storage_or_http(layer):
    assert offenders(SRC / layer, KNOWS_THE_DATABASE) == []


def test_the_database_layer_does_not_know_the_api_exists():
    """Direction of dependency: the API reads the database, and a row that can
    call a router is a migration that has become a web framework."""
    assert offenders(SRC / "db", KNOWS_THE_API) == []
