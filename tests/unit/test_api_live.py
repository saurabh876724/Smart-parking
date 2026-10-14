"""Phase 8 live layer: frames, sockets and the threads that feed them.

The constraint the whole module exists for is stated in `api/live.py`: inference
runs on its own thread per camera, a WebSocket belongs to an event loop, and
nothing on the inference side may block on - or raise into - a browser. Each test
here is one half of that sentence being true.

* ``FrameHub`` is newest-wins and encodes only when asked.
* ``Subscription`` is bounded, and a full queue is a counted drop rather than
  back-pressure on the pipeline.
* ``WebSocketHub`` builds the payload **per subscriber**, because two viewers of
  one event are allowed to see two different plates.
* The two sockets are asserted against Starlette's real close semantics: a
  `close()` before `accept()` reaches the client as a `WebSocketDisconnect` with
  the code intact, which is the only way a dashboard learns why it was refused.
* The frame seam is exercised over a real clip with **no YOLO and no OCR**
  (labelled replay, `read_plates=False`), so what is checked is the handover from
  pipeline to hub, not the detector's opinion of a car.

The throughput of these paths on the GPU is a separate, unmeasured question. In
particular the replay runs below decode a 90-frame clip in well under a second
because no model is in the loop; that number is a property of the fixture, and
nothing here offers it as a frame rate for the served stream.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import cv2
import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

from conftest import ROOT, Api, tokens_for
from smart_parking.api.live import (
    CameraManager,
    CameraRunner,
    FrameHub,
    QuietBus,
    Subscription,
    WebSocketHub,
    _payload_for,
)
from smart_parking.api.routers import cameras as cameras_router
from smart_parking.api.routers.live_ws import CLOSE_NO_CAMERA, CLOSE_ROLE, CLOSE_UNAUTHORISED
from smart_parking.core.config import (
    ApiConfig,
    AppConfig,
    CameraConfig,
    CameraRole,
    MjpegConfig,
    ServeCameraConfig,
    ServePipelineConfig,
    load_config,
)
from smart_parking.core.events import Event, RecordingEventBus
from smart_parking.core.schemas import (
    CAMERA_STATUS,
    PLATE_READ,
    SLOT_FREED,
    SLOT_OCCUPIED,
    SLOT_SNAPSHOT,
    SUMMARY,
    VEHICLE_ENTERED,
    VEHICLE_EXITED,
    VIOLATION,
    FrameView,
    Role,
    Track,
)
from smart_parking.db.repositories import Repositories
from smart_parking.services.persistence import plate_normaliser_for

FULL = "MH20AB1234"
MASKED = "MH20**1234"
CLIP = "data/fixtures/gate_visit.mp4"
TRACKS = "data/fixtures/gate_visit_tracks.json"
# Channel order is cv2's (blue, green, red), the same one `frame` fills in.
GREEN = (0, 255, 0)
BLUE = (255, 0, 0)
RED = (0, 0, 255)


# -- helpers -------------------------------------------------------------------


def frame(width: int = 480, height: int = 360, colour: tuple[int, int, int] = (0, 0, 255)) -> np.ndarray:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :] = colour
    return image


def view(camera_id: str = "lot_cam1", seq: int = 1, **kwargs) -> FrameView:
    return FrameView(
        camera_id=camera_id,
        seq=seq,
        timestamp=time.time(),
        image=kwargs.pop("image", frame()),
        **kwargs,
    )


def track(track_id: int = 7) -> Track:
    return Track(
        bbox=(10.0, 10.0, 110.0, 70.0),
        confidence=0.87,
        class_id=2,
        class_name="car",
        track_id=track_id,
    )


def event(type_: str = SLOT_OCCUPIED, payload: dict | None = None, camera_id: str = "lot_cam1") -> Event:
    return Event(type=type_, camera_id=camera_id, payload=payload or {"slot_code": "A2"})


def decoded_size(jpeg: bytes) -> tuple[int, int]:
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, "the bytes are not a JPEG cv2 can read back"
    return image.shape[1], image.shape[0]


def corner_colour(jpeg: bytes) -> tuple[int, int, int]:
    """Which flat-colour frame this JPEG came from.

    JPEG is lossy, so a pure colour comes back a few levels off; thresholding at
    the midpoint makes the comparison about *which* colour, not about the codec.
    """
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return tuple(int(value > 128) * 255 for value in image[5, 5])


def mjpeg_parts(body: bytes, camera_id: str = "lot_cam1") -> list[bytes]:
    """The JPEG payloads, read back out of the multipart framing by hand.

    A browser's `<img>` does this parse, so if the framing is wrong the stream is
    blank - and no HTTP status would ever say so. The assertion is on the bytes a
    client has to work with, not on a substring of the whole body.
    """
    marker = f"--smartparking{camera_id}\r\n".encode("ascii")
    parts: list[bytes] = []
    for chunk in body.split(marker)[1:]:
        head, sep, rest = chunk.partition(b"\r\n\r\n")
        assert sep, f"a part needs its header block: {head[:60]!r}"
        text = head.decode("ascii")
        assert "Content-Type: image/jpeg" in text, text
        length = int(text.split("Content-Length:")[1].split("\r\n")[0])
        jpeg = rest[:length]
        assert rest[length:] == b"\r\n", "a part is its own bytes plus one CRLF"
        assert len(jpeg) == length
        parts.append(jpeg)
    return parts


@pytest.fixture()
def live_logs():
    """Every record `smart_parking.api.live` emits during the test.

    Not `caplog`: `core/logging.py` puts the project's own handler on the
    `smart_parking` logger with `propagate = False`, so pytest's root handler
    never sees these records. A refusal that only prints to stdout is a refusal
    nobody can assert on, so the capture goes on the logger that emits it.
    """
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("smart_parking.api.live")
    handler = _Collect(level=logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


@pytest.fixture()
def fast_stream(make_client) -> Api:
    """An app that paces its MJPEG at 50 Hz instead of the shipped 5.

    The stream generator sleeps per part, so at the production interval a test
    that reads two frames would spend most of its wall clock asleep. The value
    under test is the framing and the newest-wins cursor, not the throttle.
    """
    client = make_client(
        update=lambda api: api.model_copy(update={"mjpeg": MjpegConfig(fps=50.0, max_width=0)})
    )
    return Api(client, tokens_for(client))


# -- FrameHub -----------------------------------------------------------------


def test_a_new_frame_replaces_the_old_one_rather_than_joining_it():
    hub = FrameHub(MjpegConfig())
    first, second = view(seq=1), view(seq=2)
    hub.put(first)
    hub.put(second)
    assert hub.view("lot_cam1") is second
    assert hub.frames_in == 2
    hub.put(view("gate_cam1"))
    assert hub.cameras() == ["gate_cam1", "lot_cam1"]


def test_nothing_is_encoded_until_someone_asks_for_a_picture():
    hub = FrameHub(MjpegConfig())
    hub.put(view())
    assert hub.encodes == 0, "a camera nobody watches costs no JPEG"
    seq, jpeg = hub.jpeg_since("lot_cam1", None)
    assert (seq, hub.encodes) == (1, 1)
    assert jpeg[:2] == b"\xff\xd8", "a JPEG starts with SOI"


def test_a_viewer_that_has_seen_the_current_frame_waits_instead_of_getting_a_copy():
    hub = FrameHub(MjpegConfig())
    hub.put(view(seq=5))
    assert hub.jpeg_since("lot_cam1", 5) is None
    assert hub.jpeg_since("lot_cam1", 4) is not None
    assert hub.jpeg_since("nobody_here", None) is None
    # An old cursor still gets the newest frame, not the one it asked past.
    assert hub.jpeg_since("lot_cam1", -10)[0] == 5


def test_the_stream_is_scaled_and_the_snapshot_is_not():
    """A stream is a picture a browser keeps; a snapshot is the calibration
    measurement the doc asks for, so it keeps the source's own pixels."""
    hub = FrameHub(MjpegConfig(max_width=240, quality=60))
    hub.put(view(image=frame(width=480, height=360)))
    assert decoded_size(hub.jpeg_since("lot_cam1", None)[1]) == (240, 180)
    assert decoded_size(hub.snapshot("lot_cam1")) == (480, 360)


def test_a_stream_at_the_source_width_is_an_option_not_a_bug():
    hub = FrameHub(MjpegConfig(max_width=0))
    hub.put(view(image=frame(width=700, height=200)))
    assert decoded_size(hub.jpeg_since("lot_cam1", None)[1]) == (700, 200)


def test_a_snapshot_of_a_camera_that_has_never_produced_a_frame_is_none():
    assert FrameHub(MjpegConfig()).snapshot("lot_cam1") is None


# -- the payload and its masking ----------------------------------------------


@pytest.mark.parametrize(
    "type_, channel",
    [
        (SLOT_OCCUPIED, "slot"),
        (SLOT_FREED, "slot"),
        (SLOT_SNAPSHOT, "slot"),
        (SUMMARY, "summary"),
        (VEHICLE_ENTERED, "session"),
        (VEHICLE_EXITED, "session"),
        (VIOLATION, "violation"),
        (PLATE_READ, "plate"),
        (CAMERA_STATUS, "camera"),
        ("something_new", "other"),
    ],
)
def test_every_bus_event_type_lands_on_a_named_channel(type_, channel):
    payload = _payload_for(event(type_), True)
    assert payload["channel"] == channel
    # The bus's own vocabulary survives the hop: a dashboard that filters on
    # `slot_occupied` is filtering on a name that exists elsewhere in the system.
    assert payload["type"] == type_
    assert payload["camera_id"] == "lot_cam1"
    assert set(payload) == {"type", "channel", "camera_id", "ts", "payload"}


def test_the_plate_in_an_event_is_masked_for_the_roles_that_may_not_read_it():
    for key in ("plate", "plate_before", "plate_after"):
        assert _payload_for(event(PLATE_READ, {key: FULL}), False)["payload"][key] == MASKED
        assert _payload_for(event(PLATE_READ, {key: FULL}), True)["payload"][key] == FULL
    # An absent plate is not a masked `None`: the field stays out of the payload.
    assert "plate" not in _payload_for(event(PLATE_READ, {}), False)["payload"]
    assert _payload_for(event(PLATE_READ, {"plate": ""}), False)["payload"]["plate"] == ""


def test_the_rest_of_the_event_is_carried_unchanged():
    original = {"slot_code": "A2", "seconds_since": 12.5, "track_id": 11}
    payload = _payload_for(event(SLOT_OCCUPIED, dict(original)), False)
    assert payload["payload"] == original
    # The dict is a copy: a masked payload must not rewrite the event the
    # persistence worker is reading from the same bus.
    assert payload["payload"] is not original


# -- the per-connection queue --------------------------------------------------


def test_a_slow_viewer_is_dropped_rather_than_slowing_the_pipeline_down():
    async def fill() -> None:
        sub = Subscription(asyncio.get_running_loop(), maxsize=2, reveal_plates=False)
        for index in range(5):
            sub.offer({"i": index})
        assert (sub.delivered, sub.dropped) == (2, 3)
        assert [sub.queue.get_nowait()["i"] for _ in range(2)] == [0, 1], "oldest kept"

    asyncio.run(fill())


def test_the_hub_only_serialises_for_viewers_that_exist():
    bus = RecordingEventBus()
    hub = WebSocketHub(bus, ApiConfig(ws_queue_size=4))
    assert hub.clients == 0
    bus.publish(event())
    assert hub.events_in == 0, "no subscriber, no work"


def test_a_role_determines_what_its_viewers_are_handed():
    bus = RecordingEventBus()
    hub = WebSocketHub(bus, ApiConfig())

    async def connect_and_read() -> tuple[dict, dict]:
        admin = hub.connect(Role.ADMIN, loop=asyncio.get_running_loop())
        board = hub.connect(Role.VIEWER, loop=asyncio.get_running_loop())
        assert hub.clients == 2
        bus.publish(event(PLATE_READ, {"plate": FULL}))
        # `call_soon_threadsafe` from this same thread queues the callback; one
        # turn of the loop is what runs it.
        await asyncio.sleep(0)
        return admin.queue.get_nowait(), board.queue.get_nowait()

    revealed, masked = asyncio.run(connect_and_read())
    assert revealed["payload"]["plate"] == FULL
    assert masked["payload"]["plate"] == MASKED
    assert masked["payload"] is not revealed["payload"], "one event, two payloads"
    assert hub.events_in == 1


def test_a_string_role_works_as_well_as_the_enum():
    """`CurrentUser.role` reaches the hub from the token's claim, where it is a
    string; a `Role(...)` refusal there would close every socket."""
    hub = WebSocketHub(RecordingEventBus(), ApiConfig())
    assert hub.connect("OPERATOR", loop=asyncio.new_event_loop()).reveal_plates is True
    assert hub.connect("CUSTOMER", loop=asyncio.new_event_loop()).reveal_plates is False


def test_a_summary_can_be_left_out_of_the_channel_entirely():
    bus = RecordingEventBus()
    hub = WebSocketHub(bus, ApiConfig(), include_summary=False)

    async def read() -> None:
        sub = hub.connect(Role.VIEWER, loop=asyncio.get_running_loop())
        bus.publish(event(SUMMARY, {"occupied": 3}))
        bus.publish(event(SLOT_OCCUPIED))
        await asyncio.sleep(0)
        assert sub.queue.qsize() == 1
        assert sub.queue.get_nowait()["type"] == SLOT_OCCUPIED
        assert hub.events_in == 1, "a filtered summary never reaches a subscriber"

    asyncio.run(read())


def test_a_viewer_whose_loop_already_closed_is_counted_not_lost_silently():
    bus = RecordingEventBus()
    hub = WebSocketHub(bus, ApiConfig())
    closed = asyncio.new_event_loop()
    closed.close()
    sub = Subscription(closed, 8, reveal_plates=False)
    with hub._lock:
        hub._subs.add(sub)
    # Called directly, as the bus calls it: on the inference thread, where raising
    # would be swallowed by the bus and look like a dashboard that just stopped.
    hub._on_event(event())
    assert (hub.dropped, sub.dropped) == (1, 1)
    assert hub.clients == 0, "the dead viewer was removed from the set"


def test_a_disconnect_carries_the_viewers_own_drop_count_into_the_total():
    bus = RecordingEventBus()
    hub = WebSocketHub(bus, ApiConfig(ws_queue_size=4))

    async def overflow() -> None:
        sub = hub.connect(Role.VIEWER, loop=asyncio.get_running_loop())
        for index in range(10):
            sub.offer({"i": index})
        assert (sub.delivered, sub.dropped) == (4, 6)
        hub.disconnect(sub)
        assert hub.dropped == sub.dropped
        assert hub.clients == 0

    asyncio.run(overflow())


def test_the_hub_survives_an_event_it_cannot_serialise():
    """A payload that will not copy is one viewer's bug, not the gate's."""
    bus = RecordingEventBus()
    hub = WebSocketHub(bus, ApiConfig())

    class Uncooperative(dict):
        def copy(self):  # `dict(event.payload)` calls this on a dict subclass
            raise ValueError("no")

    async def run() -> None:
        hub.connect(Role.VIEWER, loop=asyncio.get_running_loop())
        bus.publish(event(SLOT_OCCUPIED, Uncooperative()))
        await asyncio.sleep(0)

    asyncio.run(run())  # must not raise
    assert hub.events_in == 0 or hub.clients == 1


# -- /ws/live ------------------------------------------------------------------


def test_the_event_socket_greets_the_viewer_with_what_it_may_see(api):
    with api.client.websocket_connect(api.ws("/ws/live", role="OPERATOR")) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["payload"] == {"role": "OPERATOR", "reveal_plates": True, "queue_size": 200}
        api.ctx.bus.publish(event(CAMERA_STATUS, {"state": "running"}))
        message = ws.receive_json()
        assert message["channel"] == "camera"
        assert message["payload"] == {"state": "running"}


@pytest.mark.parametrize(
    "path, ticket, reason",
    [
        ("/ws/live", None, "token_missing"),
        ("/ws/live", "not-a-ticket", "token_invalid"),
    ],
)
def test_a_socket_that_cannot_present_a_ticket_is_refused_before_it_opens(api, path, ticket, reason):
    """The handshake is refused rather than accepted-then-closed, so a browser
    sees `onclose` with a code and never a half-open channel."""
    with pytest.raises(WebSocketDisconnect) as caught:
        with api.client.websocket_connect(api.ws(path, ticket=ticket)):
            pass
    assert caught.value.code == CLOSE_UNAUTHORISED
    assert caught.value.reason == reason


@pytest.mark.parametrize("which", ["access_token", "refresh_token"])
def test_no_bearer_token_of_any_kind_opens_a_socket_anymore(api, which):
    """`?token=` was the old handshake, and it is refused **by name**.

    Two reasons this is worth a close code of its own rather than the generic
    "no credential": a dashboard cached from before tickets would otherwise be
    told its operator is logged out when the real finding is that the URL is
    wrong, and the short-lived credential the sockets used to carry is exactly
    what must not appear in a logged query string again - so a valid access token
    here is the case to refuse, not an accident.
    """
    pair = api.post(
        "/auth/login",
        role=None,
        json={"email": "desk@lot.example", "password": "Barrier-Ticket-2026"},
    ).json()
    with pytest.raises(WebSocketDisconnect) as caught:
        with api.client.websocket_connect(api.ws("/ws/live", token=pair[which])):
            pass
    assert caught.value.code == CLOSE_UNAUTHORISED
    assert caught.value.reason == "ticket_required"


def test_a_ticket_is_spent_by_the_connection_that_used_it(api):
    """Single use, so a ticket that leaks from a log cannot open a second socket."""
    ticket = api.ws_ticket(role="ADMIN")
    with api.client.websocket_connect(api.ws("/ws/live", ticket=ticket)) as ws:
        assert ws.receive_json()["type"] == "hello"
    with pytest.raises(WebSocketDisconnect) as caught:
        with api.client.websocket_connect(api.ws("/ws/live", ticket=ticket)):
            pass
    assert caught.value.code == CLOSE_UNAUTHORISED
    assert caught.value.reason == "token_invalid"


def test_a_ticket_outlives_the_signout_it_was_minted_from(api, seeded):
    """The handshake re-reads the account, so a ticket cannot smuggle a logout.

    A ticket is not a credential, but it names an account, and the window between
    `POST /auth/ws-ticket` and the connect is real: the dashboard that signed out
    in one tab must not still be streaming in another.
    """
    ticket = api.ws_ticket(role="OPERATOR")
    assert api.post("/auth/logout", role="OPERATOR").status_code == 200
    with pytest.raises(WebSocketDisconnect) as caught:
        with api.client.websocket_connect(api.ws("/ws/live", ticket=ticket)):
            pass
    assert caught.value.code == CLOSE_UNAUTHORISED
    # `token_revoked` rather than `user_inactive`: the account is fine, and the
    # answer the operator needs is "sign in again", not "you have been cut off".
    assert caught.value.reason == "token_revoked"


def test_a_deactivated_account_cannot_open_a_socket(api, seeded):
    ticket = api.ws_ticket(role="VIEWER")
    with api.ctx.database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        repos.users.deactivate(repos.users.by_email("board@lot.example").id)
    with pytest.raises(WebSocketDisconnect) as caught:
        with api.client.websocket_connect(api.ws("/ws/live", ticket=ticket)):
            pass
    assert caught.value.code == CLOSE_UNAUTHORISED
    assert caught.value.reason == "user_inactive"


def test_a_token_whose_role_no_longer_matches_the_account_gets_the_403_close(api, seeded):
    """4403 is the permission close, and a stale role claim is the one case the
    WebSocket layer can reach: the account is live, the ticket is valid, and the
    identity it names disagrees with the row. No HTTP endpoint changes a role, so
    the disagreement is written to the database the way it would be after an
    out-of-band edit."""
    ticket = api.ws_ticket(role="ADMIN")
    with api.ctx.database.scope() as session:
        repos = Repositories(session, normalise=plate_normaliser_for(api.ctx.cfg))
        repos.users.get(seeded.user_ids["ADMIN"]).role = Role.VIEWER
    with pytest.raises(WebSocketDisconnect) as caught:
        with api.client.websocket_connect(api.ws("/ws/live", ticket=ticket)):
            pass
    assert caught.value.code == CLOSE_ROLE
    assert caught.value.reason == "role_changed"


def test_two_viewers_of_one_event_see_two_different_plates(api):
    admin = api.client.websocket_connect(api.ws("/ws/live", role="ADMIN"))
    board = api.client.websocket_connect(api.ws("/ws/live", role="VIEWER"))
    admin.__enter__()
    board.__enter__()
    try:
        admin.receive_json() and board.receive_json()  # the two hellos
        api.ctx.bus.publish(event(PLATE_READ, {"plate": FULL}))
        assert admin.receive_json()["payload"]["plate"] == FULL
        assert board.receive_json()["payload"]["plate"] == MASKED
        assert api.ctx.hub.events_in == 1, "one event, both viewers"
        assert api.ctx.hub.dropped == 0
    finally:
        admin.__exit__(None, None, None)
        board.__exit__(None, None, None)


def test_a_socket_only_untouched_by_events_still_holds_its_own_queue(api):
    """The queue is per connection: a viewer that receives nothing cannot be the
    reason another one's events get dropped."""
    with api.client.websocket_connect(api.ws("/ws/live", role="VIEWER")) as ws:
        ws.receive_json()
        for _ in range(3):
            api.ctx.bus.publish(event(SLOT_OCCUPIED, {"slot_code": "A1"}))
        assert [ws.receive_json()["payload"]["slot_code"] for _ in range(3)] == ["A1"] * 3
    assert api.ctx.hub.clients == 0, "the hub let go of the socket"


def test_the_sockets_are_mounted_at_the_root_and_the_rest_is_under_the_prefix(api):
    """`routers/__init__.py` says the doc names its sockets at the root
    (`WS /ws/live?ticket=`), with no prefix, while every REST route sits under
    `api.prefix`. A client that assumed the two match gets a router-level close
    (1000, no reason), which is what makes this worth pinning: the failure looks
    like authentication, not like a URL.
    """
    assert api.prefix == "/api/v1"
    for query in (f"ticket={api.ws_ticket(role='ADMIN')}", f"token={api.tokens['ADMIN']}"):
        with pytest.raises(WebSocketDisconnect) as caught:
            with api.client.websocket_connect(api.prefix + "/ws/live?" + query):
                pass
        assert caught.value.code == 1000, query
    with api.client.websocket_connect(api.ws("/ws/live", role="ADMIN")) as ws:
        assert ws.receive_json()["type"] == "hello"


# -- the overlay socket and endpoint ------------------------------------------


def test_a_camera_with_no_frames_closes_the_overlay_socket_by_name(api):
    """The refusal is a message *and* a close: the picture says what is wrong in
    the body, the code says it to anything that only reads the status."""
    with pytest.raises(WebSocketDisconnect) as caught:
        with api.client.websocket_connect(api.ws("/ws/cameras/lot_cam1/overlay", role="VIEWER")) as ws:
            assert ws.receive_json() == {"type": "error", "code": "no_frame", "camera_id": "lot_cam1"}
            # Reading again is what surfaces the close: a client that stopped at
            # the message would never see why its socket went away.
            ws.receive_json()
    assert caught.value.code == CLOSE_NO_CAMERA
    assert caught.value.reason == "no_frame"


def test_the_overlay_describes_the_picture_the_stream_is_sending(api):
    api.ctx.frames.put(
        view(seq=3, tracks=[track(7)], labels={7: f"{FULL} 4f2a19"}, summary={"occupied": 1})
    )
    body = api.get("/cameras/lot_cam1/overlay", role="VIEWER").json()
    assert body["camera_id"] == "lot_cam1" and body["seq"] == 3
    assert body["summary"] == {"occupied": 1}
    assert body["tracks"][0]["label"] == f"{MASKED} 4f2a19"
    assert body["tracks"][0]["bbox"] == [10.0, 10.0, 110.0, 70.0]
    assert body["tracks"][0]["confidence"] == 0.87
    assert api.get("/cameras/lot_cam1/overlay", role="OPERATOR").json()["tracks"][0]["label"] == (
        f"{FULL} 4f2a19"
    )


def test_a_class_name_is_not_redacted_like_a_plate(api):
    """`car 0.81` has no plate in it. Masking it would show an operator a redacted
    word where there is nothing to redact, and hide nothing."""
    api.ctx.frames.put(view(seq=4, tracks=[track(8)], labels={8: "motorcycle 0.81"}))
    for role in ("VIEWER", "CUSTOMER"):
        assert api.get("/cameras/lot_cam1/overlay", role=role).json()["tracks"][0]["label"] == (
            "motorcycle 0.81"
        )


def test_the_overlay_socket_streams_frames_until_the_viewer_leaves(api):
    api.ctx.frames.put(view(seq=1, tracks=[track(2)], labels={2: FULL}))
    with api.client.websocket_connect(api.ws("/ws/cameras/lot_cam1/overlay", role="CUSTOMER")) as ws:
        first = ws.receive_json()
        assert first["seq"] == 1
        assert first["tracks"][0]["label"] == MASKED
        api.ctx.frames.put(view(seq=2, tracks=[track(2)], labels={2: FULL}))
        second = ws.receive_json()
        assert second["seq"] == 2
        # A frame nobody asked for is not a copy of the last one: the sequence
        # number is the cursor, and it moved.
        assert api.ctx.frames.view("lot_cam1").seq == second["seq"]


# -- the multipart stream ------------------------------------------------------


def test_each_part_is_a_whole_jpeg_and_a_running_camera_replaces_it(fast_stream, monkeypatch):
    """The framing is the contract: a browser's `<img>` parses these bytes itself,
    so a wrong Content-Length is a picture that never appears and no HTTP status
    ever complains about.
    """
    monkeypatch.setattr(cameras_router, "NO_FRAME_TIMEOUT_S", 0.25)
    frames = fast_stream.ctx.frames
    frames.put(view(seq=1, image=frame(colour=GREEN)))

    def late() -> None:
        # Two frames land while the viewer is already reading, which is the only
        # way to see the cursor move rather than merely being set.
        time.sleep(0.2)
        frames.put(view(seq=2, image=frame(colour=BLUE)))
        time.sleep(0.2)
        frames.put(view(seq=3, image=frame(colour=RED)))

    feeder = threading.Thread(target=late, name="mjpeg-feeder")
    feeder.start()
    try:
        response = fast_stream.client.get(
            fast_stream.prefix + "/cameras/lot_cam1/stream.mjpg",
            headers={"Authorization": f"Bearer {fast_stream.tokens['VIEWER']}"},
        )
    finally:
        feeder.join()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "multipart/x-mixed-replace; boundary=smartparkinglot_cam1"
    )
    assert response.headers["cache-control"] == "no-store"
    parts = mjpeg_parts(response.content)
    assert [corner_colour(jpeg) for jpeg in parts] == [GREEN, BLUE, RED]
    for jpeg in parts:
        assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9", "SOI ... EOI"
    assert decoded_size(parts[0]) == (480, 360)


def test_a_stream_ends_when_the_camera_stops_reporting(fast_stream, monkeypatch):
    """An endless 200 OK that carries nothing is how a dead camera looks alive."""
    monkeypatch.setattr(cameras_router, "NO_FRAME_TIMEOUT_S", 0.1)
    fast_stream.ctx.frames.put(view(seq=1))
    started = time.monotonic()
    response = fast_stream.client.get(
        fast_stream.prefix + "/cameras/lot_cam1/stream.mjpg",
        headers={"Authorization": f"Bearer {fast_stream.tokens['VIEWER']}"},
    )
    assert time.monotonic() - started < 5.0
    assert len(mjpeg_parts(response.content)) == 1


def test_a_viewer_never_gets_a_backlog_of_frames(fast_stream, monkeypatch):
    """Three frames landed while nobody was watching; the next viewer still gets
    one part, because the hub only ever holds the newest."""
    monkeypatch.setattr(cameras_router, "NO_FRAME_TIMEOUT_S", 0.1)
    for seq in (1, 2, 3):
        fast_stream.ctx.frames.put(view(seq=seq, image=frame(colour=BLUE if seq == 3 else GREEN)))
    response = fast_stream.client.get(
        fast_stream.prefix + "/cameras/lot_cam1/stream.mjpg",
        headers={"Authorization": f"Bearer {fast_stream.tokens['VIEWER']}"},
    )
    parts = mjpeg_parts(response.content)
    assert [corner_colour(jpeg) for jpeg in parts] == [BLUE]
    assert fast_stream.ctx.frames.encodes == 1, "the two superseded frames were never encoded"


# -- the pipeline seam ---------------------------------------------------------


def gate_fixture() -> tuple[CameraConfig, ServeCameraConfig]:
    """The Phase 6 rendered clip, replayed through its labelled boxes.

    `read_plates=False` keeps OCR out of the process and `tracks_file` keeps YOLO
    out of it, so what is exercised is the handover from a pipeline thread to the
    hub. The source is absolute because the clip is opened by a decoder, which
    resolves against the working directory rather than the project root.
    """
    cfg = load_config(root=ROOT)
    camera = CameraConfig(
        id="gate_visit",
        name="Synthetic gate visit clip (rendered fixture)",
        role=CameraRole.GATE_ENTRY,
        source=str(cfg.resolve(CLIP)),
        gate_file="configs/gates/gate_visit_synth.json",
    )
    entry = ServeCameraConfig(camera="gate_visit", tracks_file=TRACKS, loop=False, read_plates=False)
    return camera, entry


def served_cfg(
    camera: CameraConfig | None = None,
    entries: list | None = None,
    **pipeline,
) -> AppConfig:
    """The shipped config with one served camera in it.

    `cameras` replaces the whole `cameras.yaml` list, so a manager test says which
    camera it means instead of inheriting whichever ids the file happens to name.
    Any other keyword goes to the pipeline block, which is how a test asks for an
    unpaced pass without waiting six seconds for a 15 fps clip.
    """
    cfg = load_config(root=ROOT)
    fixture_camera, entry = gate_fixture()
    return cfg.model_copy(
        update={
            "cameras": [camera or fixture_camera],
            "api": cfg.api.model_copy(
                update={
                    "pipeline": ServePipelineConfig(cameras=entries or [entry], **pipeline)
                }
            ),
        }
    )


def test_a_replayed_clip_hands_over_one_view_per_processed_frame_in_order():
    cfg = load_config(root=ROOT)
    camera, entry = gate_fixture()
    frames = FrameHub(MjpegConfig(max_width=0))
    pipeline = CameraRunner(cfg, camera, entry, RecordingEventBus(), frames)._build()
    result = pipeline.run(replay=True, on_frame=frames.put)
    # `run` reports its own count under `frames`; the hub's counter is the number
    # of sinks that actually landed. If they ever disagree, a stream is showing
    # fewer pictures than the pipeline drew.
    assert result["frames"] == pipeline.frames_processed == frames.frames_in > 0
    newest = frames.view(camera.id)
    assert newest.camera_id == camera.id
    assert newest.seq == result["frames"] - 1, "seq is the 0-based frame index"
    assert newest.image.shape == (360, 480, 3)
    assert newest.summary["sessions"] == result["sessions"]
    assert "SYNTHETIC REPLAY" in newest.summary["hud"], "a rendered clip says so on screen"


def test_the_served_labels_come_from_the_replay_not_from_a_model():
    cfg = load_config(root=ROOT)
    camera, entry = gate_fixture()
    frames = FrameHub(MjpegConfig())
    bus = RecordingEventBus()
    pipeline = CameraRunner(cfg, camera, entry, bus, frames)._build()
    assert pipeline.tracker is None, "labelled boxes, so ByteTrack would invent ids over them"
    assert pipeline.anpr is None, "a served stream reads plates only when it is told to"
    pipeline.run(replay=True, on_frame=frames.put)
    types = {event.type for event in bus.recorded}
    assert "vehicle_entered" in types, "the gate still makes sessions with no detector in it"


def test_a_stop_request_ends_the_run_and_keeps_the_frames_already_handed_over():
    cfg = load_config(root=ROOT)
    camera, entry = gate_fixture()
    frames = FrameHub(MjpegConfig())
    pipeline = CameraRunner(cfg, camera, entry, RecordingEventBus(), frames)._build()
    cap = cv2.VideoCapture(str(cfg.resolve(CLIP)))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    assert total > 6, "the fixture must be longer than the run being cut short"
    after = 6

    def sink(view_: FrameView) -> None:
        frames.put(view_)
        if frames.frames_in >= after:
            pipeline.request_stop()

    pipeline.run(replay=True, on_frame=sink)
    assert frames.frames_in == after < total
    assert pipeline.frames_processed == after
    # The hub kept exactly what was handed over - stopping is not discarding.
    assert frames.view(camera.id).seq == after - 1


def test_a_runner_replays_the_clip_once_on_its_own_thread_and_reports_the_pass():
    cfg = load_config(root=ROOT)
    camera, entry = gate_fixture()
    frames = FrameHub(MjpegConfig(max_width=320))
    bus = RecordingEventBus()
    runner = CameraRunner(cfg, camera, entry, bus, frames)
    assert runner.status()["running"] is False
    runner.start()
    assert runner._thread is not threading.current_thread()
    deadline = time.monotonic() + 60.0
    while runner.status()["running"] and time.monotonic() < deadline:
        time.sleep(0.05)
    status = runner.status()
    assert status["error"] is None
    assert status["running"] is False
    assert status["passes"] == 1
    assert status["loop"] is False, "a gate replay that loops re-crosses the same tracks"
    assert status["synthetic_replay"] is True
    assert status["read_plates"] is False
    assert status["role"] == CameraRole.GATE_ENTRY.value
    # `frames_processed` belongs to the live pipeline object, and there is none
    # once the pass has finished: the count a caller wants after the fact is the
    # hub's. Asserting None here is what keeps that honest if it ever changes.
    assert status["frames_processed"] is None
    assert frames.frames_in > 1
    assert frames.view(camera.id) is not None
    assert runner.stop() is True
    assert runner._thread is None


def test_a_looping_camera_runs_pass_after_pass_until_it_is_stopped():
    """The other half of `loop`: without it the thread exits at the end of the
    clip, with it a `stop()` has to interrupt a run that would never end."""
    camera, entry = gate_fixture()
    looping = entry.model_copy(update={"loop": True})
    frames = FrameHub(MjpegConfig(max_width=320))
    runner = CameraRunner(served_cfg(), camera, looping, RecordingEventBus(), frames)
    runner.start()
    deadline = time.monotonic() + 60.0
    while runner.passes < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runner.passes >= 2
    assert runner.status()["running"] is True
    assert runner.stop(timeout=30.0) is True
    assert runner.passes >= 2, "stopping reported success on a thread that is gone"


def test_a_looping_camera_publishes_the_clip_once_and_the_picture_repeatedly():
    """A second pass of a rendered clip is the same four vehicles crossing the same
    line under the same `source_session_id`, which is exactly what the session
    repository refuses (an idempotency key that accepted it would be a duplicate
    visit). So a looping camera gets a :class:`QuietBus` after its first pass.

    Unpaced here on purpose: the events a pass publishes do not depend on how long
    the pass takes, and this file has no reason to spend twelve wall-clock seconds
    proving it twice.
    """
    camera, entry = gate_fixture()
    once = RecordingEventBus()
    first = CameraRunner(
        served_cfg(pace_replay=False), camera, entry, once, FrameHub(MjpegConfig(max_width=320))
    )
    first.start()
    deadline = time.monotonic() + 60.0
    while first.passes < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert first.passes == 1 and first.status()["running"] is False
    one_pass = len(once.recorded)
    assert one_pass > 0, "a pass with no events in it proves nothing"

    looping = entry.model_copy(update={"loop": True})
    cap = cv2.VideoCapture(camera.source)
    clip_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    frames = FrameHub(MjpegConfig(max_width=320))
    bus = RecordingEventBus()
    runner = CameraRunner(
        served_cfg(entries=[looping], pace_replay=False), camera, looping, bus, frames
    )
    runner.start()
    deadline = time.monotonic() + 60.0
    while runner.passes < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert runner.passes >= 2
    assert len(bus.recorded) == one_pass, (
        "the second pass of a clip is not a second set of visits"
    )
    assert frames.frames_in >= 2 * clip_frames, (
        "the stream keeps moving; only the events stop"
    )
    assert runner.stop() is True


def test_a_quiet_bus_cannot_be_listened_to_or_drained():
    """The shape of a bus with none of its capacity, so a pipeline that publishes to
    it runs unchanged - and nothing downstream can be waiting for events that were
    never written."""
    quiet = QuietBus()
    seen: list[Event] = []
    quiet.subscribe(seen.append)
    quiet.publish(event(VEHICLE_ENTERED))
    quiet.publish(event(SUMMARY, {"occupied": 1}))
    assert len(quiet) == 0
    assert quiet.drain(0.0) == []
    assert seen == [], "a quiet bus has no subscribers to call"


def test_a_paced_pass_runs_at_the_clips_own_speed():
    """`pace=True` is the served case: the clip's 15 fps sets the wall clock, so a
    pass cut at 20 frames cannot finish before twenty of its own periods have gone.

    The comparator is the same pipeline unpaced, which is what the Phase 6 CLI runs
    for throughput. The ratio below is a property of this 480x360 fixture on this
    machine and is not offered as a frame rate for anything a camera would see.
    """
    cfg = load_config(root=ROOT)
    camera, entry = gate_fixture()
    cap = cv2.VideoCapture(str(cfg.resolve(CLIP)))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    assert fps > 0
    seconds_per_frame = 1.0 / fps

    def runner() -> CameraRunner:
        return CameraRunner(cfg, camera, entry, RecordingEventBus(), FrameHub(MjpegConfig()))

    paced = runner()._build().run(max_frames=20, replay=True, pace=True)
    unpaced = runner()._build().run(max_frames=20, replay=True)

    assert (paced["frames"], unpaced["frames"]) == (20, 20)
    # Sleep wakes a hair early or a hair late; one frame's period of slack keeps
    # that from being a flaky assertion without making it a weak one.
    assert paced["seconds"] >= 19 * seconds_per_frame
    assert unpaced["seconds"] < paced["seconds"] / 2
    # A CLI's number is throughput; a served pass reports the clip's, and the two
    # are different measurements of different things.
    assert unpaced["fps"] > paced["fps"]


def test_the_manager_refuses_a_camera_whose_source_it_cannot_read(live_logs):
    camera, _entry = gate_fixture()
    missing = camera.model_copy(update={"source": "data/fixtures/nope.mp4"})
    manager = CameraManager(served_cfg(camera=missing), RecordingEventBus(), FrameHub(MjpegConfig()))
    assert manager.start() == [], "an unreadable clip is not a started camera"
    assert manager.statuses() == []
    refusals = [r.getMessage() for r in live_logs if "not readable" in r.getMessage()]
    assert len(refusals) == 1, refusals


def test_the_manager_names_a_served_camera_that_is_not_in_the_config(live_logs):
    _camera, entry = gate_fixture()
    orphan = entry.model_copy(update={"camera": "ghost_cam"})
    manager = CameraManager(
        served_cfg(entries=[orphan]), RecordingEventBus(), FrameHub(MjpegConfig())
    )
    assert manager.start() == []
    assert any("ghost_cam" in r.getMessage() for r in live_logs), (
        "a typo in api.pipeline.cameras has to be said out loud, not skipped quietly"
    )


def test_the_manager_starts_the_fixture_and_stops_it_cleanly():
    frames = FrameHub(MjpegConfig())
    bus = RecordingEventBus()
    manager = CameraManager(served_cfg(), bus, frames)
    assert manager.start() == ["gate_visit"]
    started = time.monotonic()
    while frames.view("gate_visit") is None and time.monotonic() - started < 30.0:
        time.sleep(0.05)
    assert frames.view("gate_visit") is not None
    runner = manager.get("gate_visit")
    assert runner is not None and runner.status()["running"] is True
    manager.stop_all(timeout=30.0)
    assert all(r.status()["running"] is False for r in manager.runners())
    assert runner.error is None
