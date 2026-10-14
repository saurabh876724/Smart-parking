"""Velocity gate tests: estimation maths, timing source and config plumbing."""

from __future__ import annotations

import time

import pytest
import yaml
from pydantic import ValidationError

from smart_parking.core.config import VelocityGatingConfig, load_config
from smart_parking.core.schemas import Track
from smart_parking.tracking.velocity import VelocityGate

T0 = 1000.0
STEP = 0.1  # 10 fps of inference, so speeds read as px per 0.1s * 10


def car(x: float, y: float = 400.0, track_id: int = 1) -> Track:
    """A 160x140 box whose ground point (bottom-centre) sits at (x, y)."""
    return Track(
        bbox=(x - 80.0, y - 140.0, x + 80.0, y),
        confidence=0.9,
        class_id=2,
        class_name="car",
        track_id=track_id,
        footprint_ratio=0.4,
    )


def gate(**over) -> VelocityGate:
    settings = {"smoothing_window": 3, "max_parking_velocity": 30.0}
    settings.update(over)
    return VelocityGate(VelocityGatingConfig(**settings))


def feed(g: VelocityGate, positions, t0=T0, step=STEP):
    for i, x in enumerate(positions):
        g.update([car(x)], t0 + i * step)


# -- estimation -------------------------------------------------------------
def test_ground_point_is_the_footprint_bottom_edge():
    assert VelocityGate.ground_point(car(250.0, 400.0)) == (250.0, 400.0)


def test_constant_velocity_recovers_the_exact_slope():
    g = gate()
    feed(g, [100.0, 110.0, 120.0, 130.0])  # 10 px per 0.1 s
    assert g.speed(1) == pytest.approx(100.0)


def test_diagonal_motion_combines_both_axes():
    g = gate()
    for i in range(4):
        g.update([Track(bbox=(i * 3.0, i * 4.0, 10 + i * 3.0, 20 + i * 4.0),
                        confidence=0.9, class_id=2, class_name="car", track_id=1)], T0 + i * STEP)
    # 30 px/s in x and 40 px/s in y.
    assert g.speed(1) == pytest.approx(50.0)


def test_window_smooths_single_frame_jitter():
    """A 20 px blip on an otherwise parked car must not read as motion."""
    g = gate(smoothing_window=5)
    feed(g, [100.0, 100.0, 120.0, 100.0, 100.0, 100.0])
    assert g.speed(1) < 30.0


def test_speed_is_unknown_until_two_samples_exist():
    g = gate()
    g.update([car(100.0)], T0)
    assert g.speed(1) is None
    # Unknown is treated as eligible: the FSM's dwell timer is what filters a
    # one-frame detection, not the gate.
    assert [t.track_id for t in g.update([car(100.0)], T0)] == [1]


# -- timing source ----------------------------------------------------------
def test_processing_speed_does_not_change_the_estimate():
    """Replay timing: the estimate comes from the timestamps, not the clock.

    The same (position, video-time) pairs fed at two different wall-clock rates
    must give the same physical velocity, which is what makes a 3x-speed replay
    behave identically to normal speed.
    """
    slow, fast = gate(), gate()
    for i in range(4):
        slow.update([car(100.0 + i * 10.0)], T0 + i * STEP)
        time.sleep(0.02)
        fast.update([car(100.0 + i * 10.0)], T0 + i * STEP)
    assert slow.speed(1) == fast.speed(1) == pytest.approx(100.0)


def test_timestamp_epoch_is_irrelevant():
    """Only the *differences* between timestamps matter.

    Replay stamps frames as ``time.time() + n / fps``, so the estimator works
    with epoch-scale magnitudes; the slope must not care. (Compared with approx
    because a 1e6 s offset costs a little float64 precision in the subtraction.)
    """
    a, b = gate(), gate()
    feed(a, [100.0, 110.0, 120.0])
    feed(b, [100.0, 110.0, 120.0], t0=T0 + 1_000_000.0)
    assert a.speed(1) == pytest.approx(100.0)
    assert b.speed(1) == pytest.approx(100.0)


def test_frame_rate_changes_the_physical_answer():
    """Same 10 px of motion per frame is twice the speed at twice the fps."""
    a, b = gate(), gate()
    feed(a, [100.0, 110.0, 120.0], step=1.0 / 30)
    feed(b, [100.0, 110.0, 120.0], step=1.0 / 15)
    assert a.speed(1) == pytest.approx(300.0)
    assert b.speed(1) == pytest.approx(150.0)


def test_a_long_gap_restarts_the_window():
    """A track lost and re-found must not average its displacement over the gap."""
    g = gate()
    feed(g, [100.0, 100.0, 100.0])
    assert g.speed(1) == pytest.approx(0.0)
    # Gone for longer than max_sample_gap_s, then back 500 px away.
    g.update([car(600.0)], T0 + 5.0)
    assert g.speed(1) is None


# -- gating decision --------------------------------------------------------
def test_stationary_track_stays_eligible():
    g = gate()
    feed(g, [100.0] * 6)
    assert [t.track_id for t in g.update([car(100.0)], T0 + 0.6)] == [1]


def test_fast_track_is_gated_out():
    g = gate()
    feed(g, [100.0, 150.0, 200.0, 250.0])  # 500 px/s
    assert g.update([car(300.0)], T0 + 0.4) == []


def test_track_slowing_below_the_threshold_becomes_eligible_again():
    g = gate(smoothing_window=3)
    feed(g, [100.0, 200.0, 300.0])  # 1000 px/s
    assert g.update([car(300.0)], T0 + 0.3) == []
    for i in range(4):
        g.update([car(300.0)], T0 + 0.4 + i * STEP)
    assert [t.track_id for t in g.update([car(300.0)], T0 + 0.9)] == [1]


def test_disabled_gate_passes_everything_through():
    g = gate(enabled=False)
    feed(g, [100.0, 500.0, 900.0])  # absurd speed
    assert [t.track_id for t in g.update([car(1300.0)], T0 + 0.3)] == [1]


def test_each_track_is_gated_independently():
    g = gate()
    for i in range(4):
        g.update([car(100.0, track_id=1), car(100.0 + i * 60.0, track_id=2)], T0 + i * STEP)
    eligible = g.update([car(100.0, track_id=1), car(280.0, track_id=2)], T0 + 0.4)
    assert [t.track_id for t in eligible] == [1]


# -- configuration ----------------------------------------------------------
def _write_config(tmp_path, threshold: float, window: int = 3):
    (tmp_path / "configs").mkdir(exist_ok=True)
    (tmp_path / "configs" / "app.yaml").write_text(
        yaml.safe_dump({"velocity_gating": {"enabled": True,
                                            "max_parking_velocity": threshold,
                                            "smoothing_window": window}}),
        encoding="utf-8",
    )
    return load_config(root=tmp_path, cameras_yaml=None)


def test_threshold_comes_from_the_yaml_file(tmp_path):
    cfg = _write_config(tmp_path, 42.5, window=7)
    assert cfg.velocity_gating.max_parking_velocity == 42.5
    assert cfg.velocity_gating.smoothing_window == 7


def test_changing_the_configured_threshold_changes_gating(tmp_path):
    """Loader -> config object -> gate -> actual decision, end to end."""
    positions = [100.0, 140.0, 180.0, 220.0]  # 400 px/s

    strict = VelocityGate(_write_config(tmp_path, 30.0).velocity_gating)
    feed(strict, positions)
    assert strict.update([car(260.0)], T0 + 0.4) == []

    lenient = VelocityGate(_write_config(tmp_path, 500.0).velocity_gating)
    feed(lenient, positions)
    assert [t.track_id for t in lenient.update([car(260.0)], T0 + 0.4)] == [1]


def test_missing_section_keeps_gating_enabled(tmp_path):
    """An absent velocity_gating block must not silently switch the filter off."""
    (tmp_path / "configs").mkdir(exist_ok=True)
    (tmp_path / "configs" / "app.yaml").write_text("detection:\n  confidence: 0.4\n", encoding="utf-8")
    cfg = load_config(root=tmp_path, cameras_yaml=None)
    assert cfg.velocity_gating.enabled is True
    assert cfg.velocity_gating.max_parking_velocity > 0


def test_invalid_threshold_is_rejected_at_load_time(tmp_path):
    (tmp_path / "configs").mkdir(exist_ok=True)
    (tmp_path / "configs" / "app.yaml").write_text(
        yaml.safe_dump({"velocity_gating": {"max_parking_velocity": 0}}), encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        load_config(root=tmp_path, cameras_yaml=None)


def test_window_below_two_samples_is_rejected():
    with pytest.raises(ValidationError):
        VelocityGatingConfig(smoothing_window=1)
