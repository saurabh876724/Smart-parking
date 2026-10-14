"""Config loading.

Thresholds live in YAML, never in code - the doc calls this out specifically
because tuning debounce timers is the bulk of getting occupancy right.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from smart_parking.core.schemas import CameraRole


class DetectionConfig(BaseModel):
    model_path: str = "yolo11s.pt"
    confidence: float = 0.35
    imgsz: int = 640
    device: str = "cuda"  # falls back to cpu at runtime if unavailable
    classes: list[int] = Field(default_factory=lambda: [2, 3, 5, 7])
    frame_stride: int = 1  # run inference every Nth frame on a LOT camera
    half: bool = True


class OccupancyConfig(BaseModel):
    # Overlap of a vehicle footprint with a slot needed to call it occupied.
    occupy_score: float = 0.30
    # Below this, the vehicle is in the aisle rather than any slot.
    any_slot_score: float = 0.20
    # A footprint split across two slots at or above this is straddling lines.
    straddle_score: float = 0.20
    # Asymmetric on purpose: a false FREE double-books a slot, a delayed FREE
    # only costs a few seconds of availability.
    occupied_after_s: float = 0.6
    free_after_s: float = 8.0
    footprint_ratio: float = 0.4


class VelocityGatingConfig(BaseModel):
    """Eligibility filter applied to tracks before slot assignment (Phase 4).

    ``configs/app.yaml`` is authoritative; these defaults mirror the measured
    values so a missing section behaves like the documented one instead of
    silently switching gating off. An invalid value raises at load time.
    """

    enabled: bool = True
    # Pixels per second at the source resolution. Per-camera and per-view: pixel
    # velocity is not vehicle speed until the camera is calibrated.
    max_parking_velocity: float = Field(default=30.0, gt=0.0)
    # Samples (frames the track was seen in) the slope is fitted over.
    smoothing_window: int = Field(default=15, ge=2)
    # A track unseen for longer than this starts a fresh window instead of
    # averaging its displacement across the gap.
    max_sample_gap_s: float = Field(default=1.0, gt=0.0)


class AnprDetectorConfig(BaseModel):
    """Plate candidate screening inside a vehicle crop (Phase 5).

    The detector is a text-region detector, so these limits are what turns a
    text region into a *plate*: geometry first, OCR second.
    """

    # Text-region score needed to keep a candidate.
    confidence: float = Field(default=0.30, ge=0.0, le=1.0)
    # Below `min_plate_width` pixels OCR cannot resolve the characters, however
    # good the engine is - reject rather than guess. Measured per camera with
    # scripts/measure_plates.py.
    min_plate_width: float = Field(default=110.0, gt=0.0)
    max_plate_width: float = Field(default=520.0, gt=0.0)
    # Indian plates are ~2:1 (two-line plates are excluded by this).
    min_aspect_ratio: float = Field(default=1.4, gt=0.0)
    max_aspect_ratio: float = Field(default=7.0, gt=0.0)
    # Vehicles smaller than this cannot carry a legible plate at this distance.
    min_vehicle_width: float = Field(default=120.0, ge=0.0)
    # Plates sit low on a vehicle, so only the bottom slice of the box is
    # searched. Screening the whole box wastes detections on number-board-sized
    # signage and shop fronts behind the car.
    roi_bottom_fraction: float = Field(default=0.75, gt=0.0, le=1.0)
    max_candidates: int = Field(default=3, ge=1)
    # Detector input scaling, re-measurable with
    # `python scripts/bench_anpr.py --limit-type min|max` on the fixture (280
    # stills, process pinned to the P-cores):
    #
    #   'max'  located the plate 194/280 = 69%, screens kept 60 of them (21% of
    #          vehicles), median proposed region 100 px, 29.6 ms/vehicle
    #   'min'  located it more often, 215/280 = 77%, but proposed narrower
    #          regions (median 70 px) so the screens kept only 29 (10% of
    #          vehicles), at 115.1 ms/vehicle
    #
    # 'min' is slower because it stretches the short side of the ROI up to the
    # long side, and a vehicle ROI is plate-shaped - wide and low - so the
    # network runs on aspect-ratio-squared pixels for a worse answer. 'max' keeps
    # a small crop at native size and only shrinks when the long side is over the
    # limit. `det_side_len` is what `get_preprocess` is handed, so with 'max' it
    # acts as a downscale ceiling on a big close-up vehicle, never an upscale.
    det_limit_type: Literal["min", "max"] = "max"
    det_side_len: int = Field(default=640, ge=32)
    # Text detectors lose thin strokes at the edge of a crop, so the vehicle
    # region is padded before screening and the offset subtracted afterwards.
    crop_pad_px: int = Field(default=12, ge=0, le=64)

    @model_validator(mode="after")
    def _check_widths(self) -> "AnprDetectorConfig":
        if self.max_plate_width <= self.min_plate_width:
            raise ValueError("anpr.detector.max_plate_width must exceed min_plate_width")
        if self.max_aspect_ratio <= self.min_aspect_ratio:
            raise ValueError("anpr.detector.max_aspect_ratio must exceed min_aspect_ratio")
        return self


class AnprPreprocessConfig(BaseModel):
    """Plate-crop conditioning before recognition.

    Measured, not assumed. `scripts/bench_anpr.py` section 4 ran every switch
    against the unprocessed crop - 140 labelled stills, process pinned to the
    P-cores so the timings mean something:

        raw (control)        29% exact / 36% char   14.1 ms/crop
        grayscale            29% / 39%             14.2 ms
        + CLAHE              24% / 34%             14.4 ms
        + upscale            29% / 38%             14.3 ms
        upscale + CLAHE      26% / 34%             14.5 ms
        + sharpen            27% / 36%             14.4 ms
        + denoise            29% / 38%             28.7 ms
        everything on        23% / 33%             41.7 ms

    No switch improves exact-match accuracy; grayscale only buys 3 points of
    character accuracy at the same cost. CLAHE and sharpening actively lose: both
    invent local contrast on strokes that are already at the resolution limit and
    the recogniser reads that as a different character. Denoise doubles the time
    for nothing, and running every operation at once costs 3x the time and 6
    points of accuracy. So the shipped defaults are grayscale alone. Turn a
    switch on only after measuring it on your own camera's crops - the fixture
    says these are not free.
    """

    # PP-OCR's recogniser resizes to its own fixed height regardless, so
    # enlarging a thin crop first was measured as a small loss, not a gain.
    # 0 disables the step entirely.
    min_crop_height: int = Field(default=0, ge=0)
    target_height: int = Field(default=64, ge=16)
    grayscale: bool = True
    clahe: bool = False
    denoise: bool = False
    sharpen: bool = False


class AnprOcrConfig(BaseModel):
    enabled: bool = True
    engine: str = "rapidocr"
    # Recognition score floor. A low-confidence read is discarded so that a
    # garbled frame cannot outvote a clean one.
    min_text_score: float = Field(default=0.50, ge=0.0, le=1.0)
    # PP-OCR's angle classifier: off because plate crops are already upright.
    use_orientation_cls: bool = False
    preprocess: AnprPreprocessConfig = Field(default_factory=AnprPreprocessConfig)


class AnprValidationConfig(BaseModel):
    """Normalisation and rejection rules for OCR text.

    Deliberately permissive about *format* and strict about *structure*: it will
    not invent characters, and it will not reject a legitimate series.
    """

    require_region_code: bool = False
    # Substitute OCR homoglyphs (O->0, I->1, ...) only where the format demands
    # a digit and only if the result matches - length is never changed.
    repair_confusables: bool = True
    # One confused character is a repairable read; two means the read is garbage
    # and the vote will have better luck with another frame.
    max_repair_edits: int = Field(default=1, ge=0, le=4)
    # Repair only readings that already have the full canonical length. A short
    # string is evidence the OCR dropped a character, and re-reading a letter as
    # a digit to make a 9-character string "valid" produces a plausible plate
    # that belongs to nobody - the worst possible ANPR failure.
    repair_lengths: list[int] = Field(default_factory=lambda: [10])
    # Repairing a letter position is far more likely to corrupt a real plate
    # than to rescue a read, so it is opt-in.
    repair_letter_positions: bool = False
    # Extra accepted shapes, e.g. temporary or diplomatic series.
    extra_patterns: list[str] = Field(default_factory=list)
    # High-security series print the country name on the plate itself, so OCR
    # returns "INDIAMH20AB1234". These words are stripped only at the very start
    # or end of the text - never in the middle, where they could eat real
    # characters.
    strip_words: list[str] = Field(default_factory=lambda: ["INDIA", "BHARAT"])
    min_length: int = Field(default=6, ge=2)
    max_length: int = Field(default=12, ge=2)

    @model_validator(mode="after")
    def _check_lengths(self) -> "AnprValidationConfig":
        if self.max_length < self.min_length:
            raise ValueError("anpr.validation.max_length must be >= min_length")
        return self


class AnprVotingConfig(BaseModel):
    """Per-track multi-frame consensus. One OCR frame is never an answer."""

    window_size: int = Field(default=7, ge=2)
    minimum_observations: int = Field(default=3, ge=1)
    minimum_agreement: float = Field(default=0.6, gt=0.0, le=1.0)
    # A track unseen for longer than this loses its window, so a reused id
    # cannot inherit the previous vehicle's plate.
    max_track_age_s: float = Field(default=3.0, gt=0.0)

    @model_validator(mode="after")
    def _check_window(self) -> "AnprVotingConfig":
        if self.minimum_observations > self.window_size:
            raise ValueError(
                "anpr.voting.minimum_observations cannot exceed window_size - "
                "the window would never fill enough votes to decide"
            )
        return self


class AnprConfig(BaseModel):
    """Automatic number plate recognition for gate/entrance cameras.

    Independent of occupancy: it reads plates, it does not book slots.
    """

    enabled: bool = True
    detector: AnprDetectorConfig = Field(default_factory=AnprDetectorConfig)
    ocr: AnprOcrConfig = Field(default_factory=AnprOcrConfig)
    validation: AnprValidationConfig = Field(default_factory=AnprValidationConfig)
    voting: AnprVotingConfig = Field(default_factory=AnprVotingConfig)


def _normalised(points: list[list[float]], what: str) -> None:
    """Gate geometry is stored in 0-1 coordinates, exactly like the slot files.

    One rule for every calibration file means one calibration tool and one
    mental model, and it keeps a gate drawing valid when a camera changes
    resolution or a clip is resized. A pixel-space file would be silently wrong
    at another size, so it is rejected here instead.
    """
    for point in points:
        if len(point) != 2:
            raise ValueError(f"{what} needs [x, y] pairs, got {point!r}")
        x, y = point
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError(f"{what} points must be normalised 0-1, got {point!r}")


class GateLineConfig(BaseModel):
    """One virtual gate line: two endpoints, drawn in the direction they face.

    Crossing direction is the sign of the cross product of the line against the
    vehicle centroid's step, so a vehicle that wobbles 2 px around the line does
    not register twice, and one that crosses it entirely between two frames still
    does (the step segment is tested, not the point).

    In normalised image coordinates (y grows downwards), `positive` means the
    vehicle moves from the negative side of the drawn line to the positive side.
    Draw the line left-to-right and a vehicle travelling down the frame crosses
    positive.
    """

    name: str
    points: list[list[float]]
    direction: Literal["positive", "negative"] = "positive"
    # Which event a matching crossing produces. There is deliberately no
    # default: direction says nothing about whether this line is the barrier you
    # drive in at or the one you drive out of, and guessing from the line's name
    # would misclassify every vehicle on that line if the name were loose.
    role: Literal["ENTRY", "EXIT"]

    @model_validator(mode="after")
    def _check_endpoints(self) -> "GateLineConfig":
        if len(self.points) != 2:
            raise ValueError(f"gate line {self.name!r} needs exactly 2 points")
        _normalised(self.points, f"gate line {self.name!r}")
        (ax, ay), (bx, by) = self.points
        if ax == bx and ay == by:
            raise ValueError(f"gate line {self.name!r} has two identical endpoints")
        return self


class GateConfig(BaseModel):
    """Entry/exit logic for a gate camera (Phase 6).

    Geometry lives in a per-camera file next to the slot files, timers here. The
    session state machine itself is in `services/session_service.py`: this stage
    only decides that a crossing happened.
    """

    enabled: bool = True
    lines: list[GateLineConfig] = Field(default_factory=list)
    # Optional polygon that bounds the gate area. Tracks outside it are not
    # considered, which keeps a car on the road beyond the barrier from counting.
    zone: list[list[float]] = Field(default_factory=list)
    # A vehicle that stops being seen inside the exit zone for this long has
    # left the frame with the zone still its last known position: treat it as an
    # exit rather than leaving the session ACTIVE forever.
    dwell_exit_s: float = Field(default=12.0, gt=0.0)
    # A track disappears for longer than this and the crossing tracker forgets
    # which side of the line it was on - the next sighting is a fresh track.
    track_timeout_s: float = Field(default=5.0, gt=0.0)
    # Doc section C step 5: a plate the vote is not confident about is still a
    # session, but a session the operator should look at, not a silent accept.
    min_ocr_confidence: float = Field(default=0.80, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_zone(self) -> "GateConfig":
        if self.zone:
            if len(self.zone) < 3:
                raise ValueError("gate.zone needs 3 or more points (or none)")
            _normalised(self.zone, "gate.zone")
        names = [line.name for line in self.lines]
        if len(names) != len(set(names)):
            raise ValueError("gate.lines names must be unique")
        return self


class RenderConfig(BaseModel):
    show: bool = False
    save_dir: str | None = None
    max_frames: int | None = None
    playback_fps: float = 0.0  # 0 = as fast as inference allows


class DatabaseConfig(BaseModel):
    """Where the persistence layer lives (Phase 7).

    SQLite by default because Docker is not on this machine; the URL is the only
    thing that changes when Postgres arrives. Nothing in `db/` reads YAML - it is
    handed this object, exactly like every other stage.
    """

    # A relative sqlite path is resolved against the project root by
    # `database_url_for`, so the file lands in the same place however the script
    # was started.
    url: str = "sqlite:///data/smart_parking.db"
    # SQL logging is off by default: it would dominate the console during a run.
    echo: bool = False

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("database.url must not be empty")
        if not v.startswith(("sqlite", "postgresql", "mysql")):
            # A typo here otherwise surfaces as a connect error deep inside
            # SQLAlchemy, which reads like a database bug rather than a bad URL.
            raise ValueError(
                "database.url must name a dialect SQLAlchemy understands, e.g. "
                "sqlite:///data/smart_parking.db"
            )
        return v


class CameraConfig(BaseModel):
    id: str
    name: str = ""
    role: CameraRole = CameraRole.LOT
    source: str
    slots_file: str | None = None
    # Per-camera gate geometry (entry/exit lines + zone), same idea as slots_file.
    gate_file: str | None = None
    fps_limit: float = 15.0


class MjpegConfig(BaseModel):
    """How the annotated frame reaches a browser (Phase 8).

    The doc's MVP is multipart MJPEG rather than WebRTC, and the browser is
    reading a picture, not a video stream: a throttled, downscaled JPEG is what
    a laptop GPU and a home Wi-Fi link both survive.
    """

    # Frames per second handed out per client. Encoding every processed frame
    # for every viewer costs more than the detection stage does.
    fps: float = 5.0
    quality: int = 80
    # 0 keeps the source width. Downscaling is lossy for a plate crop, but a
    # stream is not evidence: the plate crop is taken in the pipeline, never here.
    max_width: int = 960

    @field_validator("quality")
    @classmethod
    def _quality_range(cls, v: int) -> int:
        if not 1 <= v <= 95:
            raise ValueError("api.mjpeg.quality must be 1-95 (JPEG has no useful 100)")
        return v


class ServeCameraConfig(BaseModel):
    """One camera the API streams, and what it is allowed to show.

    Per-camera rather than a bare id list because the three choices below are not
    global: a lot camera can loop a demo clip, a gate camera must not (see
    `loop`), and only a camera whose source actually contains readable plates
    should spend GPU time reading them.
    """

    camera: str
    # Labelled boxes replayed instead of running YOLO: what a rendered fixture
    # needs, and it is marked SYNTHETIC everywhere it appears.
    tracks_file: str | None = None
    # ANPR is off by default on a served stream: every frame a browser sees has
    # the plate drawn into the picture, so streaming a plate-reading camera is a
    # disclosure decision, not a performance one.
    read_plates: bool = False
    # Restart the run when the clip ends. A gate replay that loops re-crosses the
    # same tracks, so its sessions start again with the ids the previous pass
    # already used - `--persist` into a shared database will then refuse the
    # duplicates loudly (see services/persistence.py).
    loop: bool = True


class ServePipelineConfig(BaseModel):
    """Cameras the API starts for itself, so `/stream.mjpg` has frames to send.

    A pipeline is started by the application's lifespan, not by a request: one
    decoder per camera, shared by every viewer, exactly as the doc draws it.
    """

    enabled: bool = True
    # Empty means the API serves reads only and nothing streams, which is a
    # legitimate deployment for a DB-only box.
    cameras: list[ServeCameraConfig] = Field(default_factory=list)
    # A file source is read frame by frame (`replay`) rather than through the
    # newest-frame grabber: on a clip the grabber drops most frames, and a dropped
    # gate frame is a missed crossing.
    replay: bool = True
    frame_stride: int | None = None
    # The lot camera's per-frame SUMMARY is a counter snapshot; streaming it is
    # useful, persisting it is not (see services/persistence.py).
    send_summary_over_ws: bool = True
    # A served replay runs at its clip's own frame rate. A CLI run reads a file as
    # fast as the decoder allows, because the number that matters there is
    # throughput; the same loop behind an `<img>` is only a stream if the picture
    # changes at the rate the clip was shot at. Unpaced, a 6-second gate fixture
    # restarts ~100 times a second, which is a flickering picture, a core pinned
    # on decoding and a queue of duplicate events (see `first_pass_events_only`).
    pace_replay: bool = True
    # The first pass of a run is a set of visits worth recording. A *second* pass
    # of the same looped file is the same vehicles doing the same thing again, so
    # its events are dropped instead of published: the picture keeps moving, the
    # dashboard keeps getting frames, and the database is not asked to hold a
    # re-run of a clip as new history (it would refuse it - `source_session_id` is
    # the idempotency key - so every pass would be a page of errors).
    first_pass_events_only: bool = True


class CorsConfig(BaseModel):
    """Which browser origins may call this API (doc: "strict CORS allow-list").

    A default-deny empty list, because the safe mistake is the same one a missing
    feature makes: a dashboard that cannot reach the API says so in its console
    within a second, and a server that answers any origin says nothing until the
    day someone else's page asks it to. The Vite dev server's port is named as the
    one non-empty value a development config needs.
    """

    allow_origins: list[str] = Field(default_factory=list)
    allow_credentials: bool = False

    @field_validator("allow_origins")
    @classmethod
    def _no_wildcard(cls, v: list[str]) -> list[str]:
        if "*" in v:
            # With allow_credentials true this is a browser-enforced pair any page
            # on the internet can aim at this operator's token store.
            raise ValueError(
                "api.cors.allow_origins must name its origins: '*' is an allow-list "
                "with the list left out (use http://localhost:5173 for the dev server)"
            )
        return [origin.rstrip("/") for origin in v]


class PasswordConfig(BaseModel):
    """Argon2id cost parameters and the length policy a password must clear first.

    Defaults are argon2-cffi's own (64 MiB, 3 passes, 4 lanes), which is the
    recommendation of the people who maintain the binding rather than a number
    invented here. They are configuration because the cost belongs to the
    hardware: ~55 ms per login on this laptop is the point of a KDF, and a test
    suite that hashes a password forty times asks for the cheap end instead.
    """

    memory_kib: int = 65536
    time_cost: int = 3
    parallelism: int = 4
    # Checked before any hashing happens: a KDF is the wrong place to discover
    # that a caller sent 4 MB of text.
    min_length: int = 10
    max_length: int = 128

    @field_validator("memory_kib")
    @classmethod
    def _memory(cls, v: int) -> int:
        if v < 8192:
            # 8 MiB is Argon2's own floor; below it the name is not the algorithm.
            raise ValueError("api.password.memory_kib must be at least 8192 (8 MiB)")
        return v

    @field_validator("time_cost", "parallelism")
    @classmethod
    def _passes(cls, v: int) -> int:
        if v < 1:
            raise ValueError("api.password time_cost and parallelism must be positive")
        return v

    @field_validator("min_length")
    @classmethod
    def _min(cls, v: int) -> int:
        if v < 8:
            raise ValueError("api.password.min_length must be at least 8")
        return v


class ApiConfig(BaseModel):
    """The HTTP surface (Phase 8): paths, tokens, throttles.

    Deliberately absent: **no secret value**. The signing key is named here by
    the environment variable and the dev file that may hold it, and resolved by
    `jwt_secret_for` - a password or key in `app.yaml` would be a file that gets
    committed, and the doc's own table puts `password_hash` in the database.
    """

    prefix: str = "/api/v1"
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_secret_env: str = "SMART_PARKING_JWT_SECRET"
    # Relative paths go through `AppConfig.resolve`, like every other path here.
    jwt_secret_file: str = "data/.secret"
    # 15 minutes: short enough that a leaked access token is a brief problem,
    # long enough that a dashboard does not re-login while being watched.
    access_token_ttl_s: int = 900
    # 12 hours, so an operator's shift is not interrupted by a token expiry.
    refresh_token_ttl_s: int = 43200
    # A WebSocket handshake cannot set a header, so the socket is opened with a
    # one-time ticket that lives just long enough to be presented once. It is not
    # the access token: a URL is written into proxy logs, browser history and a
    # server's debug output, and a credential that appears there must already be
    # worthless by the time anyone reads it.
    ws_ticket_ttl_s: int = 15
    page_size: int = 50
    ws_queue_size: int = 200
    # Failed sign-ins per email+address inside the window, after which the KDF is
    # not run at all and the caller is told to wait. This is an in-process counter
    # (see `api/throttle.py`): its job is to make a brute-force attempt expensive
    # and to leave an audit trail, not to survive a restart or a second worker.
    login_throttle_window_s: int = 300
    login_throttle_max_failures: int = 10
    password: PasswordConfig = Field(default_factory=PasswordConfig)
    mjpeg: MjpegConfig = Field(default_factory=MjpegConfig)
    pipeline: ServePipelineConfig = Field(default_factory=ServePipelineConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("api.prefix must start with '/', e.g. /api/v1")
        return v.rstrip("/") or "/api/v1"

    @field_validator("access_token_ttl_s", "refresh_token_ttl_s")
    @classmethod
    def _positive_ttl(cls, v: int) -> int:
        if v < 30:
            raise ValueError("token lifetimes below 30 s are a logout loop, not security")
        return v

    @field_validator("jwt_secret_file")
    @classmethod
    def _not_a_secret_value(cls, v: str) -> str:
        if len(v.strip()) > 120 or "\n" in v:
            # A path cannot be that long, so a value that size is someone having
            # pasted the key into YAML - which is the failure this config refuses.
            raise ValueError(
                "api.jwt_secret_file is a path, not the secret itself; "
                "put the key in the environment or in the file it points at"
            )
        return v

    @field_validator("page_size")
    @classmethod
    def _page(cls, v: int) -> int:
        if not 1 <= v <= 500:
            raise ValueError("api.page_size must be 1-500")
        return v

    @field_validator("ws_ticket_ttl_s")
    @classmethod
    def _ticket_ttl(cls, v: int) -> int:
        if not 5 <= v <= 300:
            # Below five seconds a reconnect on a slow link cannot re-handshake in
            # time; above five minutes the ticket is a token with a new name, and
            # the point of it is that it is not worth stealing.
            raise ValueError("api.ws_ticket_ttl_s must be 5-300 seconds")
        return v

    @field_validator("login_throttle_window_s", "login_throttle_max_failures")
    @classmethod
    def _throttle_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("api.login_throttle window and maximum must be positive")
        return v


class AppConfig(BaseModel):
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    occupancy: OccupancyConfig = Field(default_factory=OccupancyConfig)
    velocity_gating: VelocityGatingConfig = Field(default_factory=VelocityGatingConfig)
    anpr: AnprConfig = Field(default_factory=AnprConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)
    project_root: Path = Field(default=Path("."), exclude=True)

    def resolve(self, relative: str | Path) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.project_root / p

    def detection_model_path(self) -> Path:
        """The detection weights as an absolute path.

        ``detection.model_path`` is relative to the project root like every other
        path here. Ultralytics would otherwise resolve a bare name against the
        *working directory* - and, failing that, silently download the weights
        into it - so a server started from anywhere but the root would either
        miss the file or fetch a second copy of it.
        """
        return self.resolve(self.detection.model_path)


def load_config(
    app_yaml: str | Path = "configs/app.yaml",
    cameras_yaml: str | Path | None = "configs/cameras.yaml",
    root: Path | None = None,
) -> AppConfig:
    root = Path(root or Path(__file__).resolve().parents[3])
    app_yaml = Path(app_yaml)
    if not app_yaml.is_absolute():
        app_yaml = root / app_yaml

    data = {}
    if app_yaml.exists():
        data = yaml.safe_load(app_yaml.read_text(encoding="utf-8")) or {}

    cfg = AppConfig(**data, project_root=root)  # type: ignore[arg-type]

    if cameras_yaml:
        cameras_yaml = Path(cameras_yaml)
        if not cameras_yaml.is_absolute():
            cameras_yaml = root / cameras_yaml
        if cameras_yaml.exists():
            raw = yaml.safe_load(cameras_yaml.read_text(encoding="utf-8")) or {}
            cfg.cameras = [CameraConfig(**c) for c in raw.get("cameras", [])]

    return cfg


def load_slots(path: str | Path, root: Path) -> list[dict]:
    """Read a slot config file: [{code, polygon: [[x,y],...], type}, ...]."""
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("slots", [])


def load_gate_lines(path: str | Path, root: Path) -> dict:
    """Read a gate geometry file: {lines: [{name, points, direction}], zone: [...]}.

    Stored as JSON because the slot files are, and JSON parses as YAML, so this
    uses the same reader rather than a second config format.
    """
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {"lines": data.get("lines", []), "zone": data.get("zone", [])}


def gate_config_for(cfg: AppConfig, camera: CameraConfig) -> GateConfig:
    """This camera's gate config: geometry from its gate file, timers from YAML.

    A camera with no gate file gets the configured timers and no lines, which
    means the stage stays inert instead of guessing where the barrier is.
    """
    merged = cfg.gate.model_dump()
    if camera.gate_file:
        merged.update(load_gate_lines(camera.gate_file, cfg.project_root))
    return GateConfig(**merged)


def database_url_for(cfg: AppConfig, database: DatabaseConfig | None = None) -> str:
    """The effective URL, with a relative sqlite path anchored to the project.

    SQLAlchemy resolves a relative sqlite path against the *working directory*,
    so starting a script from elsewhere silently opens a different - empty -
    database. Every other path in this project goes through `AppConfig.resolve`,
    and the database file is no exception.

    Precedence: an explicit ``database`` argument (a caller naming its own
    database, e.g. a test's scratch file), then the ``SMART_PARKING_DATABASE_URL``
    environment variable (what a deployment and `scripts/migrate.py --url` set),
    then ``database.url`` in `configs/app.yaml`. One variable name for the
    running app and the migrations means the two cannot point at different
    files - `db/migrations/env.py` reads the same variable.
    """
    if database is None:
        override = os.environ.get("SMART_PARKING_DATABASE_URL", "").strip()
        if override:
            database = DatabaseConfig(url=override)
    url = (database or cfg.database).url
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return url  # a server URL is already absolute by definition
    target, sep, query = url[len(prefix):].partition("?")
    if not target or target == ":memory:":
        return url
    if PurePosixPath(target).is_absolute() or PureWindowsPath(target).is_absolute():
        return url
    path = cfg.resolve(target.replace("\\", "/"))
    return prefix + path.as_posix() + sep + query


class SecretUnavailable(RuntimeError):
    """No usable JWT signing key, with the reason the message already states.

    A distinct type because the API layer has to tell "this deployment is
    misconfigured" apart from "this token is bad", and a bare RuntimeError would
    invite a caller to catch both.
    """


def jwt_secret_for(cfg: AppConfig, api: ApiConfig | None = None) -> str:
    """The signing key, from the environment or the dev file it names.

    Resolution order matters: an environment variable is what a real deployment
    sets, and a file is what `scripts/dev_secret.py` writes for a laptop run so
    tokens survive a restart. Neither is `app.yaml`, which is committed - the
    whole point of the ordering is that a secret has no path into version control.

    Fails loudly rather than falling back to a hard-coded key. A default secret
    is not a missing feature, it is an authentication system that anyone who has
    read the source can bypass, and this project has already refused that trade
    once (see the plaintext-plate decision in Phase 7).
    """
    api = api or cfg.api
    from_env = os.environ.get(api.jwt_secret_env, "").strip()
    if from_env:
        origin = f"${api.jwt_secret_env}"
        secret = from_env
    else:
        path = cfg.resolve(api.jwt_secret_file)
        if not path.exists():
            raise SecretUnavailable(
                f"no JWT signing key: set {api.jwt_secret_env}, or run "
                f"scripts/dev_secret.py to create {path}"
            )
        secret = path.read_text(encoding="utf-8").strip()
        origin = str(path)
    if len(secret.encode()) < 32:
        # python-jose will happily sign with a 4-character "secret"; the result
        # is a token that can be forged offline, which is worse than not starting.
        raise SecretUnavailable(
            f"JWT key from {origin} is {len(secret.encode())} bytes; use at least 32 "
            "(scripts/dev_secret.py generates one)"
        )
    return secret
