"""Estimate OpenCloseStop cover position from calibrated travel time."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
import logging
import time

_LOGGER = logging.getLogger(__name__)

POSITION_UPDATE_INTERVAL = 0.5
EXPECTED_ZONE_EVENT_SECONDS = 2.0
ZONE_EVENT_GRACE_SECONDS = 0.25

type AsyncCommand = Callable[[], Awaitable[None]]
type Sleep = Callable[[float], Awaitable[None]]


class EstimatorState(StrEnum):
    """State of an estimated cover."""

    UNKNOWN = "unknown"
    IDLE_KNOWN = "idle_known"
    OPENING = "opening"
    CLOSING = "closing"
    ENDPOINT_GUARD = "endpoint_guard"


class ExternalAction(StrEnum):
    """An action performed outside Home Assistant."""

    OPEN = "open"
    CLOSE = "close"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class TravelTimes:
    """Calibrated full-travel and endpoint guard durations."""

    open_seconds: float
    close_seconds: float
    open_guard_seconds: float
    close_guard_seconds: float


@dataclass(frozen=True, slots=True)
class EstimatorSnapshot:
    """Expose the caller-visible estimated state."""

    state: EstimatorState
    position: int | None
    target: int | None
    generation: int
    last_source: str | None
    desynchronization_reason: str | None

    @property
    def is_opening(self) -> bool:
        """Return whether the cover is moving open."""
        return self.state is EstimatorState.OPENING

    @property
    def is_closing(self) -> bool:
        """Return whether the cover is moving closed."""
        return self.state is EstimatorState.CLOSING


@dataclass(frozen=True, slots=True, eq=False)
class _ExpectedZoneEvent:
    expires_at: float


class OpenCloseStopEstimator:
    """Own timing, interruption, and synchronization for one cover."""

    def __init__(
        self,
        travel_times: TravelTimes,
        raise_cover: AsyncCommand,
        lower_cover: AsyncCommand,
        stop_cover: AsyncCommand,
        state_changed: Callable[[], None],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Initialize an estimator with commands and testable time primitives."""
        self._travel_times = travel_times
        self._raise_cover = raise_cover
        self._lower_cover = lower_cover
        self._stop_cover = stop_cover
        self._state_changed = state_changed
        self._monotonic = monotonic
        self._sleep = sleep

        self._state = EstimatorState.UNKNOWN
        self._position: float | None = None
        self._target: int | None = None
        self._motion_started_at: float | None = None
        self._motion_start_position: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._generation = 0
        self._pending_zone_event: asyncio.Task[None] | None = None
        self._expected_zone_events: list[_ExpectedZoneEvent] = []
        self._last_source: str | None = None
        self._desynchronization_reason: str | None = "startup"

    @property
    def snapshot(self) -> EstimatorSnapshot:
        """Return the latest state written by the estimator."""
        position = None
        if self._position is not None:
            position = round(min(100.0, max(0.0, self._position)))
        return EstimatorSnapshot(
            self._state,
            position,
            self._target,
            self._generation,
            self._last_source,
            self._desynchronization_reason,
        )

    async def async_move_to(self, target: int) -> None:
        """Start a Home Assistant movement and return after its first command."""
        if not 0 <= target <= 100:
            raise ValueError("target must be between 0 and 100")
        self._last_source = "home_assistant"

        try:
            interrupted = await self._async_cancel_motion(send_stop=True)
            if self._position is None:
                await self._async_start_unknown_move(
                    target, already_stopped=interrupted
                )
                return

            if target not in (0, 100) and abs(self._position - target) < 0.5:
                self._set_idle_known(target)
                return

            await self._async_start_known_move(target)
        except asyncio.CancelledError:
            # A bridge request may reach the processor before its caller is canceled.
            await self._async_cancel_motion(send_stop=False)
            try:
                await self._stop_cover()
            except Exception:
                _LOGGER.exception("Failed to stop canceled OpenCloseStop movement")
            self._set_unknown("canceled_command")
            raise

    async def async_stop(self) -> None:
        """Stop motion and retain position only when its starting point was known."""
        self._last_source = "home_assistant"
        await self._async_cancel_motion(send_stop=False)
        await self._stop_cover()
        if self._position is None:
            self._set_unknown("stopped_while_unknown")
        else:
            self._set_idle_known(self._position)

    async def async_external_action(self, action: ExternalAction) -> None:
        """Track a mapped Pico action without sending a duplicate command."""
        self._last_source = "pico"
        zone_event_arrived_first = False
        if action is not ExternalAction.STOP:
            zone_event_arrived_first = await self._async_cancel_pending_zone_event()
        await self._async_cancel_motion(send_stop=False)

        if action is ExternalAction.STOP:
            if self._position is None:
                self._set_unknown("pico_stop_while_unknown")
            else:
                self._set_idle_known(self._position)
            return

        if not zone_event_arrived_first:
            self._expect_zone_event()

        target = 100 if action is ExternalAction.OPEN else 0
        self._start_external_move(target)

    async def async_handle_zone_update(self, *, initial: bool) -> None:
        """Correlate a raw zone event or invalidate on an unexplained update."""
        if initial:
            await self.async_invalidate("initial_snapshot")
            return

        now = self._monotonic()
        self._expected_zone_events = [
            event for event in self._expected_zone_events if event.expires_at >= now
        ]
        if self._expected_zone_events:
            self._expected_zone_events.pop(0)
            return

        await self._async_cancel_pending_zone_event()
        self._pending_zone_event = asyncio.create_task(self._async_expire_zone_event())

    async def async_invalidate(self, reason: str = "unexpected_zone_event") -> None:
        """Forget position after reconnect or an unexplained controller action."""
        await self._async_cancel_pending_zone_event()
        await self._async_cancel_task()
        self._expected_zone_events.clear()
        self._set_unknown(reason)

    async def async_suspend_for_setup(self) -> None:
        """Stop active movement and invalidate position before guided setup."""
        self._last_source = "setup"
        await self._async_cancel_pending_zone_event()
        was_active = self._task is not None or self._state in (
            EstimatorState.OPENING,
            EstimatorState.CLOSING,
            EstimatorState.ENDPOINT_GUARD,
        )
        self._settle_motion()
        await self._async_cancel_task()
        self._expected_zone_events.clear()
        try:
            if was_active:
                await self._stop_cover()
        finally:
            self._set_unknown("setup_started")

    async def async_shutdown(self) -> None:
        """Cancel timers and stop a cover that may still be moving."""
        await self._async_cancel_pending_zone_event()
        was_active = self._task is not None or self._state in (
            EstimatorState.OPENING,
            EstimatorState.CLOSING,
            EstimatorState.ENDPOINT_GUARD,
        )
        self._settle_motion()
        await self._async_cancel_task()
        if was_active:
            try:
                await self._stop_cover()
            except Exception:
                _LOGGER.exception("Failed to stop OpenCloseStop cover during shutdown")
        self._set_unknown("shutdown")

    async def _async_start_unknown_move(
        self, target: int, *, already_stopped: bool
    ) -> None:
        if not already_stopped:
            await self._stop_cover()

        sync_target = target if target in (0, 100) else self._cheapest_endpoint(target)
        await self._send_direction(sync_target)
        self._target = target
        self._position = None
        self._state = self._movement_state(sync_target)
        self._notify()
        self._start_task(
            lambda generation: self._async_run_unknown_move(
                generation, sync_target, target
            )
        )

    async def _async_run_unknown_move(
        self, generation: int, endpoint: int, target: int
    ) -> None:
        duration = self._full_travel_seconds(endpoint)
        await self._sleep_without_position(duration)
        if generation != self._generation:
            return
        await self._async_endpoint_guard(endpoint)
        if generation != self._generation:
            return
        await self._stop_cover()
        if generation != self._generation:
            return
        self._set_idle_known(endpoint)

        if target == endpoint:
            return

        await self._send_direction(target)
        if generation != self._generation:
            return
        self._begin_motion(target)
        await self._async_run_known_move(generation, target)

    async def _async_start_known_move(self, target: int) -> None:
        await self._send_direction(target)
        self._begin_motion(target)
        self._start_task(
            lambda generation: self._async_run_known_move(generation, target)
        )

    def _start_external_move(self, target: int) -> None:
        if self._position is None:
            self._target = target
            self._state = self._movement_state(target)
            self._notify()
            duration = self._full_travel_seconds(target)
        else:
            self._begin_motion(target)
            duration = self._remaining_travel_seconds(target)
        self._start_task(
            lambda generation: self._async_run_external_move(
                generation, target, duration
            )
        )

    async def _async_run_external_move(
        self, generation: int, target: int, duration: float
    ) -> None:
        await self._sleep_motion(generation, duration)
        if generation != self._generation:
            return
        await self._async_endpoint_guard(target)
        if generation != self._generation:
            return
        self._set_idle_known(target)

    async def _async_run_known_move(self, generation: int, target: int) -> None:
        duration = self._remaining_travel_seconds(target)
        await self._sleep_motion(generation, duration)
        if generation != self._generation:
            return
        self._position = float(target)
        self._clear_motion()
        if target in (0, 100):
            await self._async_endpoint_guard(target)
            if generation != self._generation:
                return
        await self._stop_cover()
        if generation != self._generation:
            return
        self._set_idle_known(target)

    async def _async_endpoint_guard(self, endpoint: int) -> None:
        guard = (
            self._travel_times.open_guard_seconds
            if endpoint == 100
            else self._travel_times.close_guard_seconds
        )
        if guard <= 0:
            return
        self._position = float(endpoint) if self._position is not None else None
        self._state = EstimatorState.ENDPOINT_GUARD
        self._notify()
        await self._sleep(guard)

    async def _sleep_motion(self, generation: int, duration: float) -> None:
        elapsed = 0.0
        while elapsed < duration:
            interval = min(POSITION_UPDATE_INTERVAL, duration - elapsed)
            await self._sleep(interval)
            if generation != self._generation:
                return
            elapsed += interval
            self._update_motion_position()
            self._notify()

    async def _sleep_without_position(self, duration: float) -> None:
        elapsed = 0.0
        while elapsed < duration:
            interval = min(POSITION_UPDATE_INTERVAL, duration - elapsed)
            await self._sleep(interval)
            elapsed += interval

    async def _send_direction(self, target: int) -> None:
        token = self._expect_zone_event()
        try:
            if self._is_opening_toward(target):
                await self._raise_cover()
            else:
                await self._lower_cover()
        except Exception:
            if token in self._expected_zone_events:
                self._expected_zone_events.remove(token)
            self._set_unknown("command_failure")
            raise

    def _begin_motion(self, target: int) -> None:
        self._target = target
        self._motion_started_at = self._monotonic()
        self._motion_start_position = self._position
        self._state = self._movement_state(target)
        self._notify()

    def _update_motion_position(self) -> None:
        if self._motion_started_at is None or self._motion_start_position is None:
            return
        elapsed = self._monotonic() - self._motion_started_at
        opening = self._state is EstimatorState.OPENING
        travel = (
            self._travel_times.open_seconds
            if opening
            else self._travel_times.close_seconds
        )
        change = elapsed / travel * 100
        if not opening:
            change = -change
        position = self._motion_start_position + change
        target = self._target if self._target is not None else position
        self._position = min(position, target) if opening else max(position, target)

    def _settle_motion(self) -> None:
        self._update_motion_position()
        self._clear_motion()

    def _clear_motion(self) -> None:
        self._motion_started_at = None
        self._motion_start_position = None

    async def _async_cancel_motion(self, *, send_stop: bool) -> bool:
        active = self._task is not None
        self._settle_motion()
        await self._async_cancel_task()
        if active and send_stop:
            try:
                await self._stop_cover()
            except Exception:
                self._set_unknown("stop_failure")
                raise
        if active:
            if self._position is None:
                self._set_unknown("interrupted_while_unknown")
            else:
                self._set_idle_known(self._position)
        return active and send_stop

    async def _async_cancel_task(self) -> None:
        task = self._task
        self._task = None
        if task is None or task is asyncio.current_task():
            return
        self._generation += 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _start_task(self, operation: Callable[[int], Awaitable[None]]) -> None:
        self._generation += 1
        generation = self._generation
        self._task = asyncio.create_task(
            self._async_run_background(generation, operation)
        )

    async def _async_run_background(
        self, generation: int, operation: Callable[[int], Awaitable[None]]
    ) -> None:
        task = asyncio.current_task()
        try:
            await operation(generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("OpenCloseStop cover movement failed")
            self._set_unknown("background_command_failure")
        finally:
            if self._task is task and generation == self._generation:
                self._task = None

    async def _async_expire_zone_event(self) -> None:
        task = asyncio.current_task()
        try:
            await self._sleep(ZONE_EVENT_GRACE_SECONDS)
            await self._async_cancel_task()
            self._expected_zone_events.clear()
            self._set_unknown("unexpected_zone_event")
        finally:
            if self._pending_zone_event is task:
                self._pending_zone_event = None

    async def _async_cancel_pending_zone_event(self) -> bool:
        task = self._pending_zone_event
        self._pending_zone_event = None
        if task is None or task is asyncio.current_task():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    def _expect_zone_event(self) -> _ExpectedZoneEvent:
        token = _ExpectedZoneEvent(self._monotonic() + EXPECTED_ZONE_EVENT_SECONDS)
        self._expected_zone_events.append(token)
        return token

    def _remaining_travel_seconds(self, target: int) -> float:
        if self._position is None:
            return self._full_travel_seconds(target)
        distance = abs(target - self._position) / 100
        travel = (
            self._travel_times.open_seconds
            if self._is_opening_toward(target)
            else self._travel_times.close_seconds
        )
        return distance * travel

    def _full_travel_seconds(self, target: int) -> float:
        return (
            self._travel_times.open_seconds
            if target == 100
            else self._travel_times.close_seconds
        )

    def _cheapest_endpoint(self, target: int) -> int:
        via_closed = (
            self._travel_times.close_seconds
            + self._travel_times.close_guard_seconds
            + target / 100 * self._travel_times.open_seconds
        )
        via_open = (
            self._travel_times.open_seconds
            + self._travel_times.open_guard_seconds
            + (100 - target) / 100 * self._travel_times.close_seconds
        )
        return 0 if via_closed <= via_open else 100

    def _movement_state(self, target: int) -> EstimatorState:
        return (
            EstimatorState.OPENING
            if self._is_opening_toward(target)
            else EstimatorState.CLOSING
        )

    def _is_opening_toward(self, target: int) -> bool:
        if self._position is not None:
            return target > self._position
        return target == 100

    def _set_idle_known(self, position: float) -> None:
        self._position = min(100.0, max(0.0, float(position)))
        self._target = None
        self._clear_motion()
        self._state = EstimatorState.IDLE_KNOWN
        self._desynchronization_reason = None
        self._notify()

    def _set_unknown(self, reason: str | None = None) -> None:
        self._position = None
        self._target = None
        self._clear_motion()
        self._state = EstimatorState.UNKNOWN
        if reason is not None:
            self._desynchronization_reason = reason
        self._notify()

    def _notify(self) -> None:
        self._state_changed()
