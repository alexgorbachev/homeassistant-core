"""Tests for OpenCloseStop cover position estimation."""

import asyncio
import heapq
from unittest.mock import AsyncMock, Mock

import pytest

from homeassistant.components.cover import CoverDeviceClass
from homeassistant.components.lutron_caseta.const import (
    CONF_BINDINGS,
    CONF_CALIBRATION,
    CONF_CALIBRATION_SOURCE,
    CONF_GESTURE,
)
from homeassistant.components.lutron_caseta.cover_estimator import (
    EstimatorState,
    ExternalAction,
    OpenCloseStopEstimator,
    TravelTimes,
)
from homeassistant.components.lutron_caseta.estimated_cover import (
    EstimatedCoverConfig,
    parse_estimated_cover_configs,
    standard_pico_bindings,
)


class FakeTime:
    """Provide deterministic monotonic time and scheduled sleeps."""

    def __init__(self) -> None:
        """Initialize at monotonic time zero."""
        self.now = 0.0
        self._sequence = 0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        """Return the current fake monotonic time."""
        return self.now

    async def sleep(self, delay: float) -> None:
        """Wait until advance reaches the requested deadline."""
        future = asyncio.get_running_loop().create_future()
        self._sequence += 1
        heapq.heappush(self._sleepers, (self.now + delay, self._sequence, future))
        await future

    async def advance(self, seconds: float) -> None:
        """Advance time and allow newly due sleeps to schedule successors."""
        await asyncio.sleep(0)
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


@pytest.fixture
async def estimator_setup():
    """Create an estimator with deterministic time and mocked commands."""
    fake_time = FakeTime()
    raise_cover = AsyncMock()
    lower_cover = AsyncMock()
    stop_cover = AsyncMock()
    changed = Mock()
    estimator = OpenCloseStopEstimator(
        TravelTimes(10, 10, 1, 1),
        raise_cover,
        lower_cover,
        stop_cover,
        changed,
        monotonic=fake_time.monotonic,
        sleep=fake_time.sleep,
    )
    yield estimator, fake_time, raise_cover, lower_cover, stop_cover, changed
    await estimator.async_shutdown()


async def _synchronize_closed(estimator, fake_time) -> None:
    """Run an unknown cover to the closed endpoint."""
    await estimator.async_move_to(0)
    await fake_time.advance(11)
    assert estimator.snapshot.position == 0


async def test_unknown_partial_synchronizes_via_cheapest_endpoint(
    estimator_setup,
) -> None:
    """A partial target from unknown first establishes the cheaper endpoint."""
    estimator, fake_time, raise_cover, lower_cover, stop_cover, _ = estimator_setup

    await estimator.async_move_to(25)

    stop_cover.assert_awaited_once_with()
    lower_cover.assert_awaited_once_with()
    assert estimator.snapshot.state is EstimatorState.CLOSING
    assert estimator.snapshot.position is None

    await fake_time.advance(11)
    assert raise_cover.await_count == 1
    assert estimator.snapshot.is_opening
    assert estimator.snapshot.position == 0

    await fake_time.advance(2.5)
    assert estimator.snapshot.state is EstimatorState.IDLE_KNOWN
    assert estimator.snapshot.position == 25
    assert stop_cover.await_count == 3


async def test_known_position_moves_proportionally_and_stops(estimator_setup) -> None:
    """Known movement reports interpolated position and settles at the target."""
    estimator, fake_time, raise_cover, _, stop_cover, _ = estimator_setup
    await _synchronize_closed(estimator, fake_time)
    stop_cover.reset_mock()

    await estimator.async_move_to(50)
    await fake_time.advance(2)
    assert estimator.snapshot.position == 20
    assert estimator.snapshot.is_opening

    await estimator.async_stop()
    assert estimator.snapshot.state is EstimatorState.IDLE_KNOWN
    assert estimator.snapshot.position == 20
    stop_cover.assert_awaited_once_with()
    assert raise_cover.await_count == 1


async def test_setup_suspension_invalidates_after_stop_failure(estimator_setup) -> None:
    """Remain unknown if setup cannot confirm Stop for active motion."""
    estimator, fake_time, _, _, stop_cover, _ = estimator_setup
    await _synchronize_closed(estimator, fake_time)
    stop_cover.reset_mock()
    await estimator.async_move_to(50)
    stop_cover.side_effect = OSError("stop failed")

    with pytest.raises(OSError, match="stop failed"):
        await estimator.async_suspend_for_setup()

    assert estimator.snapshot.state is EstimatorState.UNKNOWN
    assert estimator.snapshot.position is None
    assert estimator.snapshot.desynchronization_reason == "setup_started"
    stop_cover.side_effect = None


async def test_endpoint_request_always_applies_guard(estimator_setup) -> None:
    """Requesting the current endpoint reseats it for the configured guard time."""
    estimator, fake_time, _, lower_cover, stop_cover, _ = estimator_setup
    await _synchronize_closed(estimator, fake_time)
    lower_cover.reset_mock()
    stop_cover.reset_mock()

    await estimator.async_move_to(0)
    assert estimator.snapshot.state is EstimatorState.CLOSING
    await asyncio.sleep(0)
    assert estimator.snapshot.state is EstimatorState.ENDPOINT_GUARD

    await fake_time.advance(1)
    lower_cover.assert_awaited_once_with()
    stop_cover.assert_awaited_once_with()
    assert estimator.snapshot.position == 0


async def test_pico_actions_track_without_duplicate_commands(estimator_setup) -> None:
    """Mapped Pico direction and Stop actions update timing without bridge commands."""
    estimator, fake_time, raise_cover, lower_cover, stop_cover, _ = estimator_setup
    await _synchronize_closed(estimator, fake_time)
    raise_cover.reset_mock()
    lower_cover.reset_mock()
    stop_cover.reset_mock()

    await estimator.async_external_action(ExternalAction.OPEN)
    await fake_time.advance(3)
    await estimator.async_external_action(ExternalAction.STOP)

    assert estimator.snapshot.position == 30
    assert estimator.snapshot.state is EstimatorState.IDLE_KNOWN
    raise_cover.assert_not_awaited()
    lower_cover.assert_not_awaited()
    stop_cover.assert_not_awaited()


async def test_pico_reversal_and_repeated_stop_settle_known_position(
    estimator_setup,
) -> None:
    """Replace Pico direction in flight and make repeated Stop idempotent."""
    estimator, fake_time, raise_cover, lower_cover, stop_cover, _ = estimator_setup
    await _synchronize_closed(estimator, fake_time)
    raise_cover.reset_mock()
    lower_cover.reset_mock()
    stop_cover.reset_mock()

    await estimator.async_external_action(ExternalAction.OPEN)
    await fake_time.advance(3)
    await estimator.async_external_action(ExternalAction.CLOSE)
    await fake_time.advance(1)
    await estimator.async_external_action(ExternalAction.STOP)
    await estimator.async_external_action(ExternalAction.STOP)

    assert estimator.snapshot.state is EstimatorState.IDLE_KNOWN
    assert estimator.snapshot.position == 20
    assert estimator.snapshot.last_source == "pico"
    raise_cover.assert_not_awaited()
    lower_cover.assert_not_awaited()
    stop_cover.assert_not_awaited()


@pytest.mark.parametrize(
    ("open_seconds", "close_seconds"),
    [(12.76, 12.23), (12.45, 12.21)],
    ids=("sofia", "giulia"),
)
async def test_site_timings_interpolate_pico_stop_in_both_directions(
    open_seconds: float, close_seconds: float
) -> None:
    """Apply each onsite calibration to Pico reversal and Stop estimation."""
    fake_time = FakeTime()
    raise_cover = AsyncMock()
    lower_cover = AsyncMock()
    stop_cover = AsyncMock()
    estimator = OpenCloseStopEstimator(
        TravelTimes(open_seconds, close_seconds, 1, 1),
        raise_cover,
        lower_cover,
        stop_cover,
        Mock(),
        monotonic=fake_time.monotonic,
        sleep=fake_time.sleep,
    )
    try:
        await estimator.async_move_to(0)
        await fake_time.advance(close_seconds + 1)
        raise_cover.reset_mock()
        lower_cover.reset_mock()
        stop_cover.reset_mock()

        await estimator.async_external_action(ExternalAction.OPEN)
        await fake_time.advance(open_seconds / 2)
        await estimator.async_external_action(ExternalAction.STOP)
        assert estimator.snapshot.position == 50

        await estimator.async_external_action(ExternalAction.CLOSE)
        await fake_time.advance(close_seconds / 4)
        await estimator.async_external_action(ExternalAction.STOP)
        assert estimator.snapshot.position == 25

        raise_cover.assert_not_awaited()
        lower_cover.assert_not_awaited()
        stop_cover.assert_not_awaited()
    finally:
        await estimator.async_shutdown()


async def test_zone_event_before_pico_is_correlated(estimator_setup) -> None:
    """A zone event immediately before its Pico event does not desynchronize."""
    estimator, fake_time, *_ = estimator_setup
    await _synchronize_closed(estimator, fake_time)

    await estimator.async_handle_zone_update(initial=False)
    await estimator.async_external_action(ExternalAction.OPEN)
    await fake_time.advance(0.25)

    assert estimator.snapshot.position is not None
    assert estimator.snapshot.is_opening


async def test_zone_event_before_pico_stop_still_invalidates(estimator_setup) -> None:
    """A Pico Stop cannot explain a preceding zone event because Stop emits none."""
    estimator, fake_time, *_ = estimator_setup
    await _synchronize_closed(estimator, fake_time)

    await estimator.async_handle_zone_update(initial=False)
    await estimator.async_external_action(ExternalAction.STOP)
    await fake_time.advance(0.25)

    assert estimator.snapshot.state is EstimatorState.UNKNOWN


async def test_unexpected_and_initial_zone_events_invalidate(estimator_setup) -> None:
    """Unmatched app activity and reconnect snapshots discard the estimate."""
    estimator, fake_time, *_ = estimator_setup
    await _synchronize_closed(estimator, fake_time)

    await estimator.async_handle_zone_update(initial=False)
    await fake_time.advance(0.25)
    assert estimator.snapshot.state is EstimatorState.UNKNOWN

    await estimator.async_move_to(100)
    await fake_time.advance(11)
    assert estimator.snapshot.position == 100
    await estimator.async_handle_zone_update(initial=True)
    assert estimator.snapshot.state is EstimatorState.UNKNOWN


async def test_expected_home_assistant_zone_event_is_consumed(estimator_setup) -> None:
    """A direction notification caused by Home Assistant keeps the estimate valid."""
    estimator, fake_time, *_ = estimator_setup
    await _synchronize_closed(estimator, fake_time)

    await estimator.async_move_to(100)
    await estimator.async_handle_zone_update(initial=False)
    await fake_time.advance(0.25)

    assert estimator.snapshot.is_opening
    assert estimator.snapshot.position is not None


async def test_initial_command_failure_surfaces_and_remains_unknown(
    estimator_setup,
) -> None:
    """Failure of the first direction command is returned to the service caller."""
    estimator, _, raise_cover, *_ = estimator_setup
    raise_cover.side_effect = RuntimeError("processor rejected command")

    with pytest.raises(RuntimeError, match="processor rejected command"):
        await estimator.async_move_to(100)

    assert estimator.snapshot.state is EstimatorState.UNKNOWN


async def test_delayed_command_failure_invalidates_estimate(estimator_setup) -> None:
    """A delayed Stop failure is contained by the background movement task."""
    estimator, fake_time, _, _, stop_cover, _ = estimator_setup
    await _synchronize_closed(estimator, fake_time)
    stop_cover.side_effect = RuntimeError("processor rejected delayed Stop")

    await estimator.async_move_to(50)
    await fake_time.advance(5)

    assert estimator.snapshot.state is EstimatorState.UNKNOWN


async def test_cancelled_initial_command_attempts_stop(estimator_setup) -> None:
    """Cancellation cannot leave a possibly delivered direction command running."""
    estimator, _, raise_cover, _, stop_cover, _ = estimator_setup
    command_started = asyncio.Event()

    async def wait_forever() -> None:
        command_started.set()
        await asyncio.Event().wait()

    raise_cover.side_effect = wait_forever
    movement = asyncio.create_task(estimator.async_move_to(100))
    await command_started.wait()
    movement.cancel()

    with pytest.raises(asyncio.CancelledError):
        await movement

    stop_cover.assert_awaited()
    assert estimator.snapshot.state is EstimatorState.UNKNOWN


async def test_retarget_cancels_stale_timer(estimator_setup) -> None:
    """A newer command settles and replaces an active movement timer."""
    estimator, fake_time, _, lower_cover, stop_cover, _ = estimator_setup
    await _synchronize_closed(estimator, fake_time)
    lower_cover.reset_mock()
    stop_cover.reset_mock()

    await estimator.async_move_to(100)
    await fake_time.advance(2)
    await estimator.async_move_to(0)
    assert estimator.snapshot.is_closing
    assert estimator.snapshot.position == 20
    stop_cover.assert_awaited_once_with()
    lower_cover.assert_awaited_once_with()

    await fake_time.advance(3)
    assert estimator.snapshot.position == 0
    await fake_time.advance(10)
    assert estimator.snapshot.position == 0


def test_invalid_zone_options_do_not_block_valid_covers() -> None:
    """Invalid timing and duplicate Pico actions disable only their own zones."""
    valid = EstimatedCoverConfig(
        "1",
        CoverDeviceClass.BLIND,
        TravelTimes(9.8, 9.3, 1, 1),
        standard_pico_bindings(["1234"]),
    ).as_dict()
    duplicate = dict(valid)
    invalid = dict(valid)
    invalid["open_travel_seconds"] = -1

    configs = parse_estimated_cover_configs(
        {
            "estimated_open_close_stop_covers": {
                "1": valid,
                "2": duplicate,
                "3": invalid,
            }
        }
    )

    assert list(configs) == ["1"]


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {
            CONF_CALIBRATION: {
                CONF_CALIBRATION_SOURCE: "future_source",
                "open_samples": [],
                "close_samples": [],
            }
        },
        {
            CONF_CALIBRATION: {
                CONF_CALIBRATION_SOURCE: "guided_home_assistant",
                "open_samples": [9.8],
                "close_samples": [9.3, 9.4],
            }
        },
        {
            CONF_BINDINGS: [
                {
                    "keypad_serial": "1234",
                    "leap_button_number": 3,
                    "button_type": "raise",
                    CONF_GESTURE: "triple_tap",
                    "effect": "open",
                }
            ]
        },
    ],
)
def test_invalid_calibration_or_gesture_disables_only_zone(
    invalid_fields: dict,
) -> None:
    """Reject unknown provenance and gestures at the options boundary."""
    raw_config = EstimatedCoverConfig(
        "1", CoverDeviceClass.BLIND, TravelTimes(9.8, 9.3, 1, 1), ()
    ).as_dict()
    raw_config.update(invalid_fields)

    assert (
        parse_estimated_cover_configs(
            {"estimated_open_close_stop_covers": {"1": raw_config}}
        )
        == {}
    )
