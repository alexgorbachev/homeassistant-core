"""Tests for OpenCloseStop action capture and calibration."""

import asyncio
import heapq
from unittest.mock import AsyncMock

from pylutron_caseta.smartbridge import ZoneStatusEvent, ZoneStatusEventOrigin
import pytest

from homeassistant.components.cover import CoverDeviceClass
from homeassistant.components.lutron_caseta.const import (
    ATTR_ACTION,
    ATTR_BUTTON_TYPE,
    ATTR_LEAP_BUTTON_NUMBER,
    ATTR_SERIAL,
    LUTRON_CASETA_BUTTON_EVENT,
)
from homeassistant.components.lutron_caseta.cover_estimator import (
    ExternalAction,
    TravelTimes,
)
from homeassistant.components.lutron_caseta.cover_setup import (
    OpenCloseStopSetupSession,
    SetupButtonEvent,
    SetupCaptureFailureReason,
    SetupCaptureMismatch,
    SetupCaptureTimeout,
    SetupSessionBusyError,
    ThirdSampleRequired,
    aggregate_calibration_samples,
)
from homeassistant.components.lutron_caseta.estimated_cover import (
    EstimatedCoverConfig,
    OpenCloseStopManager,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import MockBridge


class FakeTime:
    """Provide deterministic monotonic time and scheduled sleeps."""

    def __init__(self) -> None:
        """Initialize at monotonic time zero."""
        self.now = 0.0
        self._sequence = 0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        """Return current fake monotonic time."""
        return self.now

    async def sleep(self, delay: float) -> None:
        """Wait until fake time reaches the requested deadline."""
        future = asyncio.get_running_loop().create_future()
        self._sequence += 1
        heapq.heappush(self._sleepers, (self.now + delay, self._sequence, future))
        await future

    async def advance(self, seconds: float) -> None:
        """Advance time and run sleepers in deadline order."""
        await asyncio.sleep(0)
        target = self.now + seconds
        while self._sleepers and self._sleepers[0][0] <= target:
            deadline, _, future = heapq.heappop(self._sleepers)
            self.now = deadline
            if not future.done():
                future.set_result(None)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.now = target
        await asyncio.sleep(0)
        await asyncio.sleep(0)


async def _advance_until_done(
    fake_time: FakeTime, task: asyncio.Task, *, step: float = 0.5, limit: int = 20
) -> None:
    """Advance fake time until an event-driven operation settles."""
    for _ in range(limit):
        if task.done():
            return
        await fake_time.advance(step)
    assert task.done(), (
        [(frame.f_code.co_name, frame.f_lineno) for frame in task.get_stack()],
        fake_time.now,
        [(deadline, future.done()) for deadline, _, future in fake_time._sleepers],
    )


@pytest.fixture
async def setup_session():
    """Create a setup session with deterministic time and commands."""
    fake_time = FakeTime()
    raise_cover = AsyncMock()
    lower_cover = AsyncMock()
    stop_cover = AsyncMock()
    session = OpenCloseStopSetupSession(
        "1805",
        raise_cover,
        lower_cover,
        stop_cover,
        monotonic=fake_time.monotonic,
        sleep=fake_time.sleep,
    )
    yield session, fake_time, raise_cover, lower_cover, stop_cover
    await session.async_cancel()


def test_aggregate_consistent_samples_and_third_sample() -> None:
    """Use a mean for consistent pairs and a median for three samples."""
    result = aggregate_calibration_samples([9.7, 9.9])
    assert result.seconds == 9.8
    assert result.spread == 0.2

    with pytest.raises(ThirdSampleRequired):
        aggregate_calibration_samples([9.0, 10.0])

    result = aggregate_calibration_samples([9.0, 10.0, 9.4])
    assert result.seconds == 9.4
    assert result.spread == 1.0


async def test_direction_capture_correlates_and_stops(setup_session) -> None:
    """Correlate a direction zone update and rank native gestures first."""
    session, fake_time, _, _, stop_cover = setup_session
    capture = asyncio.create_task(session.async_capture_action(ExternalAction.OPEN))
    await asyncio.sleep(0)
    session.receive_button(SetupButtonEvent("69990001", 3, "raise", "press", 0.0))
    session.receive_button(SetupButtonEvent("69990001", 3, "raise", "multi_tap", 0.01))
    await fake_time.advance(0.1)
    session.receive_zone_update(initial=False)
    await _advance_until_done(fake_time, capture, step=0.1)

    candidates = await capture
    assert [candidate.gesture for candidate in candidates] == ["multi_tap", "press"]
    stop_cover.assert_awaited_once_with()


async def test_zone_before_button_uses_grace_period(setup_session) -> None:
    """Accept a Pico event shortly after its target-zone update."""
    session, fake_time, _, _, stop_cover = setup_session
    capture = asyncio.create_task(session.async_capture_action(ExternalAction.CLOSE))
    await asyncio.sleep(0)
    session.receive_zone_update(initial=False)
    await fake_time.advance(0.1)
    session.receive_button(
        SetupButtonEvent("69990001", 4, "lower", "press", fake_time.now)
    )
    await _advance_until_done(fake_time, capture, step=0.1)

    candidates = await capture
    assert candidates[0].event_key == ("69990001", 4, "press")
    stop_cover.assert_awaited_once_with()


async def test_unmatched_zone_event_stops_for_safety(setup_session) -> None:
    """Abort and Stop when motor activity has no matching button event."""
    session, fake_time, _, _, stop_cover = setup_session
    capture = asyncio.create_task(session.async_capture_action(ExternalAction.OPEN))
    await asyncio.sleep(0)
    session.receive_zone_update(initial=False)
    await _advance_until_done(fake_time, capture, step=0.25)

    with pytest.raises(SetupCaptureMismatch):
        await capture
    stop_cover.assert_awaited_once_with()


async def test_unmatched_button_uses_forward_correlation_deadline(
    setup_session,
) -> None:
    """Reject a direction action two seconds after no zone update follows it."""
    session, fake_time, _, _, stop_cover = setup_session
    capture = asyncio.create_task(session.async_capture_action(ExternalAction.OPEN))
    await asyncio.sleep(0)
    session.receive_button(SetupButtonEvent("69990001", 3, "raise", "press", 0.0))
    await fake_time.advance(1.99)
    assert not capture.done()
    await fake_time.advance(0.01)

    with pytest.raises(SetupCaptureMismatch, match="no target-zone update") as err:
        await capture
    assert err.value.reason is SetupCaptureFailureReason.ZONE_NOT_DETECTED
    assert session.diagnostics["last_failure_reason"] == "zone_not_detected"
    stop_cover.assert_awaited_once_with()


async def test_cancel_after_direction_button_stops_during_correlation(
    setup_session,
) -> None:
    """Stop when setup closes after a direction press but before its zone event."""
    session, _, _, _, stop_cover = setup_session
    capture = asyncio.create_task(session.async_capture_action(ExternalAction.OPEN))
    await asyncio.sleep(0)
    session.receive_button(SetupButtonEvent("69990001", 3, "raise", "press", 0.0))
    await asyncio.sleep(0)

    await session.async_cancel()

    with pytest.raises(asyncio.CancelledError):
        await capture
    stop_cover.assert_awaited_once_with()


async def test_stop_capture_needs_no_zone_confirmation(setup_session) -> None:
    """Return Stop gesture candidates without issuing a duplicate command."""
    session, fake_time, _, _, stop_cover = setup_session
    capture = asyncio.create_task(session.async_capture_action(ExternalAction.STOP))
    await asyncio.sleep(0)
    session.receive_button(SetupButtonEvent("69990001", 1, "stop", "press", 0.0))
    await _advance_until_done(fake_time, capture)

    candidates = await capture
    assert candidates[0].event_key == ("69990001", 1, "press")
    stop_cover.assert_not_awaited()


async def test_home_assistant_measurement_and_watchdog(setup_session) -> None:
    """Measure command-to-endpoint time and Stop a forgotten movement."""
    session, fake_time, raise_cover, lower_cover, stop_cover = setup_session
    await session.async_start_home_assistant_movement(ExternalAction.OPEN)
    await fake_time.advance(9.8)
    assert await session.async_finish_home_assistant_movement() == 9.8
    raise_cover.assert_awaited_once_with()
    stop_cover.assert_awaited_once_with()

    await session.async_start_home_assistant_movement(ExternalAction.CLOSE, timeout=3)
    await fake_time.advance(3)
    lower_cover.assert_awaited_once_with()
    assert stop_cover.await_count == 2


async def test_confirm_stationary_endpoint_sends_only_safety_stop(
    setup_session,
) -> None:
    """Accept a visibly reached positioning endpoint without requiring movement."""
    session, _, raise_cover, lower_cover, stop_cover = setup_session

    await session.async_confirm_stationary_endpoint()

    raise_cover.assert_not_awaited()
    lower_cover.assert_not_awaited()
    stop_cover.assert_awaited_once_with()
    assert session.diagnostics["operation_active"] is False
    assert session.diagnostics["last_operation"] == "confirm_stationary_endpoint"


async def test_cancel_during_home_assistant_movement_stops_for_safety(
    setup_session,
) -> None:
    """Stop a setup-owned movement when its options flow is canceled."""
    session, _, raise_cover, _, stop_cover = setup_session
    await session.async_start_home_assistant_movement(ExternalAction.OPEN)

    await session.async_cancel()

    raise_cover.assert_awaited_once_with()
    stop_cover.assert_awaited_once_with()
    assert session.diagnostics["may_be_moving"] is False


async def test_failed_home_assistant_start_attempts_stop(setup_session) -> None:
    """Attempt Stop because a failed command may still have reached Lutron."""
    session, _, raise_cover, _, stop_cover = setup_session
    raise_cover.side_effect = OSError("connection lost after write")

    with pytest.raises(OSError, match="connection lost"):
        await session.async_start_home_assistant_movement(ExternalAction.OPEN)

    stop_cover.assert_awaited_once_with()
    assert session.diagnostics["operation_active"] is False


async def test_pico_calibration_measures_without_duplicate_commands(
    setup_session,
) -> None:
    """Measure mapped direction-to-Stop events without bridge direction calls."""
    session, fake_time, raise_cover, lower_cover, stop_cover = setup_session
    measurement = asyncio.create_task(
        session.async_measure_pico_movement(
            {
                ("68551522", 3, "press"),
                ("69990001", 3, "press"),
            },
            {
                ("68551522", 1, "press"),
                ("69990001", 1, "press"),
            },
        )
    )
    await asyncio.sleep(0)
    session.receive_button(SetupButtonEvent("69990001", 3, "raise", "press", 0.0))
    await fake_time.advance(0.1)
    session.receive_zone_update(initial=False)
    await fake_time.advance(0.1)
    await fake_time.advance(9.6)
    session.receive_button(
        SetupButtonEvent("69990001", 1, "stop", "press", fake_time.now)
    )

    assert await measurement == pytest.approx(9.8)
    raise_cover.assert_not_awaited()
    lower_cover.assert_not_awaited()
    stop_cover.assert_not_awaited()


async def test_pico_calibration_reports_missing_direction(setup_session) -> None:
    """Classify a calibration timeout before any mapped direction arrives."""
    session, fake_time, _, _, stop_cover = setup_session
    measurement = asyncio.create_task(
        session.async_measure_pico_movement(
            {("69990001", 3, "press")},
            {("69990001", 1, "press")},
            timeout=1,
        )
    )
    await _advance_until_done(fake_time, measurement, step=0.5)

    with pytest.raises(SetupCaptureTimeout) as err:
        await measurement
    assert err.value.reason is SetupCaptureFailureReason.DIRECTION_NOT_DETECTED
    assert session.diagnostics["last_failure_reason"] == "direction_not_detected"
    stop_cover.assert_not_awaited()


async def test_pico_calibration_reports_missing_zone(setup_session) -> None:
    """Classify a mapped direction that has no target-zone notification."""
    session, fake_time, _, _, stop_cover = setup_session
    measurement = asyncio.create_task(
        session.async_measure_pico_movement(
            {("69990001", 3, "press")},
            {("69990001", 1, "press")},
        )
    )
    await asyncio.sleep(0)
    session.receive_button(SetupButtonEvent("69990001", 3, "raise", "press", 0.0))
    await _advance_until_done(fake_time, measurement, step=0.5)

    with pytest.raises(SetupCaptureMismatch) as err:
        await measurement
    assert err.value.reason is SetupCaptureFailureReason.ZONE_NOT_DETECTED
    assert session.diagnostics["last_failure_reason"] == "zone_not_detected"
    stop_cover.assert_awaited_once_with()


async def test_pico_calibration_reports_missing_stop(setup_session) -> None:
    """Classify a correlated movement that has no mapped Stop action."""
    session, fake_time, _, _, stop_cover = setup_session
    measurement = asyncio.create_task(
        session.async_measure_pico_movement(
            {("69990001", 3, "press")},
            {("69990001", 1, "press")},
            timeout=1,
        )
    )
    await asyncio.sleep(0)
    session.receive_button(SetupButtonEvent("69990001", 3, "raise", "press", 0.0))
    await fake_time.advance(0.1)
    session.receive_zone_update(initial=False)
    await _advance_until_done(fake_time, measurement, step=0.5)

    with pytest.raises(SetupCaptureTimeout) as err:
        await measurement
    assert err.value.reason is SetupCaptureFailureReason.STOP_NOT_DETECTED
    assert session.diagnostics["last_failure_reason"] == "stop_not_detected"
    stop_cover.assert_awaited_once_with()


async def test_manager_exclusively_routes_setup_events(hass: HomeAssistant) -> None:
    """Route setup events once, suppress estimation, and release ownership safely."""
    bridge = MockBridge()
    bridge.stop_cover = AsyncMock()
    config = EstimatedCoverConfig(
        "805", CoverDeviceClass.BLIND, TravelTimes(9.8, 9.3, 1, 1), ()
    )
    manager = OpenCloseStopManager(hass, bridge, {"805": config})
    engine = AsyncMock()
    remove_engine = manager.register_engine("805", engine)
    session = await manager.async_begin_setup("805")

    engine.async_suspend_for_setup.assert_awaited_once_with()
    with pytest.raises(HomeAssistantError, match="setup is in progress"):
        manager.ensure_commands_allowed("805")
    with pytest.raises(SetupSessionBusyError):
        await manager.async_begin_setup("805")

    capture = asyncio.create_task(session.async_capture_action(ExternalAction.OPEN))
    await asyncio.sleep(0)
    hass.bus.async_fire(
        LUTRON_CASETA_BUTTON_EVENT,
        {
            ATTR_SERIAL: "69990001",
            ATTR_LEAP_BUTTON_NUMBER: 3,
            ATTR_BUTTON_TYPE: "raise",
            ATTR_ACTION: "press",
        },
    )
    await hass.async_block_till_done()
    bridge.call_zone_status_subscribers(
        ZoneStatusEvent(
            "805",
            "805",
            {"Zone": {"href": "/zone/805"}},
            ZoneStatusEventOrigin.UPDATE,
        )
    )
    candidates = await capture

    assert candidates[0].event_key == ("69990001", 3, "press")
    bridge.stop_cover.assert_awaited_once_with("805")
    engine.async_external_action.assert_not_awaited()
    await manager.async_end_setup(session)
    manager.ensure_commands_allowed("805")

    remove_engine()
    await manager.async_shutdown()
