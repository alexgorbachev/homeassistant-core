"""Capture Pico actions and calibrate OpenCloseStop covers safely."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from statistics import mean, median
import time
from typing import Any

from .cover_estimator import ExternalAction

EXPECTED_ZONE_EVENT_SECONDS = 2.0
ZONE_EVENT_GRACE_SECONDS = 0.25
CAPTURE_QUIET_SECONDS = 0.5
CAPTURE_TIMEOUT_SECONDS = 10.0
CALIBRATION_TIMEOUT_SECONDS = 120.0
CALIBRATION_SPREAD_SECONDS = 0.75

type AsyncCommand = Callable[[], Awaitable[None]]
type PicoEventKey = tuple[str, int, str]
type Sleep = Callable[[float], Awaitable[None]]


class SetupSessionError(RuntimeError):
    """Base error for an OpenCloseStop setup operation."""


class SetupSessionBusyError(SetupSessionError):
    """The requested cover already has an active setup session."""


class SetupCaptureTimeout(SetupSessionError):
    """No complete setup interaction arrived before the deadline."""


class SetupCaptureMismatch(SetupSessionError):
    """Controller activity could not be correlated to a Pico interaction."""


class ThirdSampleRequired(SetupSessionError):
    """Two calibration samples differ enough to require a third."""


@dataclass(frozen=True, slots=True)
class SetupButtonEvent:
    """Stable button identity captured from the Home Assistant event bus."""

    serial: str
    leap_button_number: int
    button_type: str
    gesture: str
    timestamp: float

    @property
    def event_key(self) -> PicoEventKey:
        """Return the stable fields used for binding and dispatch."""
        return (self.serial, self.leap_button_number, self.gesture)


@dataclass(frozen=True, slots=True)
class _SetupZoneEvent:
    initial: bool
    timestamp: float


type _SetupEvent = SetupButtonEvent | _SetupZoneEvent


@dataclass(frozen=True, slots=True)
class CalibrationAggregate:
    """Proposed travel time and the observed sample spread."""

    seconds: float
    spread: float


def aggregate_calibration_samples(
    samples: Sequence[float],
) -> CalibrationAggregate:
    """Aggregate two consistent samples or the median of three samples."""
    if len(samples) not in (2, 3) or any(sample <= 0 for sample in samples):
        raise ValueError("calibration requires two or three positive samples")
    spread = max(samples) - min(samples)
    if len(samples) == 2:
        if spread > CALIBRATION_SPREAD_SECONDS:
            raise ThirdSampleRequired
        seconds = mean(samples)
    else:
        seconds = median(samples)
    return CalibrationAggregate(round(seconds, 2), round(spread, 2))


class OpenCloseStopSetupSession:
    """Own one cover's temporary capture and calibration lifecycle."""

    def __init__(
        self,
        zone_id: str,
        raise_cover: AsyncCommand,
        lower_cover: AsyncCommand,
        stop_cover: AsyncCommand,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Initialize commands, event transport, and testable time primitives."""
        self.zone_id = zone_id
        self._raise_cover = raise_cover
        self._lower_cover = lower_cover
        self._stop_cover = stop_cover
        self._monotonic = monotonic
        self._sleep = sleep
        self._events: asyncio.Queue[_SetupEvent] = asyncio.Queue()
        self._operation_active = False
        self._operation_task: asyncio.Task[Any] | None = None
        self._may_be_moving = False
        self._closed = False
        self._measurement_started_at: float | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._last_operation: str | None = None

    @property
    def diagnostics(self) -> dict[str, str | bool | None]:
        """Return non-sensitive setup state for diagnostics."""
        return {
            "zone_id": self.zone_id,
            "operation_active": self._operation_active,
            "may_be_moving": self._may_be_moving,
            "last_operation": self._last_operation,
        }

    def receive_button(self, event: SetupButtonEvent) -> None:
        """Queue one normalized Pico or keypad event."""
        if not self._closed:
            if self._operation_active and self._last_operation in {
                "learn_open",
                "learn_close",
                "calibrate_pico",
            }:
                self._may_be_moving = True
            self._events.put_nowait(event)

    def receive_zone_update(self, *, initial: bool) -> None:
        """Queue one target-zone status event."""
        if not self._closed:
            self._events.put_nowait(_SetupZoneEvent(initial, self._monotonic()))

    async def async_capture_action(
        self,
        action: ExternalAction,
        *,
        timeout: float = CAPTURE_TIMEOUT_SECONDS,
    ) -> tuple[SetupButtonEvent, ...]:
        """Capture and correlate one user-initiated Pico interaction."""
        self._begin_operation(f"learn_{action}")
        self._drain_events()
        try:
            if action is ExternalAction.STOP:
                return await self._async_capture_stop(timeout)
            return await self._async_capture_direction(timeout)
        finally:
            self._end_operation()

    async def async_start_home_assistant_movement(
        self,
        action: ExternalAction,
        *,
        timeout: float = CALIBRATION_TIMEOUT_SECONDS,
    ) -> None:
        """Start a user-timed movement and arm its safety watchdog."""
        if action not in (ExternalAction.OPEN, ExternalAction.CLOSE):
            raise ValueError("calibration movement must be open or close")
        self._begin_operation(f"calibrate_{action}")
        self._drain_events()
        try:
            await self._direction_command(action)()
        except Exception:
            self._may_be_moving = True
            await self._async_stop_safely()
            self._end_operation()
            raise
        self._measurement_started_at = self._monotonic()
        self._may_be_moving = True
        self._watchdog = asyncio.create_task(self._async_watchdog(timeout))

    async def async_finish_home_assistant_movement(self) -> float:
        """Stop a user-timed movement and return its elapsed duration."""
        if self._measurement_started_at is None:
            raise SetupSessionError("no Home Assistant calibration movement is active")
        duration = self._monotonic() - self._measurement_started_at
        await self._async_stop()
        self._end_operation()
        return duration

    async def async_confirm_stationary_endpoint(self) -> None:
        """Safety-Stop after the user confirms an unmeasured endpoint visually."""
        self._begin_operation("confirm_stationary_endpoint")
        self._drain_events()
        try:
            await self._async_stop()
        finally:
            self._end_operation()

    async def async_measure_pico_movement(
        self,
        direction: PicoEventKey,
        stop: PicoEventKey,
        *,
        timeout: float = CALIBRATION_TIMEOUT_SECONDS,
    ) -> float:
        """Measure a mapped Pico direction-to-Stop interaction without commands."""
        self._begin_operation("calibrate_pico")
        self._drain_events()
        deadline = self._monotonic() + timeout
        direction_event: SetupButtonEvent | None = None
        zone_event: _SetupZoneEvent | None = None
        correlated = False
        try:
            while True:
                now = self._monotonic()
                if now >= deadline:
                    raise SetupCaptureTimeout
                event_deadline = deadline
                if direction_event is not None and zone_event is None:
                    event_deadline = min(
                        event_deadline,
                        direction_event.timestamp + EXPECTED_ZONE_EVENT_SECONDS,
                    )
                elif zone_event is not None and direction_event is None:
                    event_deadline = min(
                        event_deadline,
                        zone_event.timestamp + ZONE_EVENT_GRACE_SECONDS,
                    )
                event = await self._async_next_event(event_deadline - now)
                if event is None:
                    now = self._monotonic()
                    if now >= deadline:
                        raise SetupCaptureTimeout
                if isinstance(event, _SetupZoneEvent):
                    if event.initial:
                        raise SetupCaptureMismatch(
                            "controller reconnected during calibration"
                        )
                    if correlated:
                        raise SetupCaptureMismatch("unexpected target-zone activity")
                    zone_event = event
                    self._may_be_moving = True
                elif (
                    isinstance(event, SetupButtonEvent)
                    and direction_event is None
                    and event.event_key == direction
                ):
                    direction_event = event
                    self._may_be_moving = True
                elif (
                    isinstance(event, SetupButtonEvent)
                    and correlated
                    and event.event_key == stop
                ):
                    assert direction_event is not None
                    self._may_be_moving = False
                    return event.timestamp - direction_event.timestamp

                if direction_event is not None and zone_event is not None:
                    if self._events_correlate(direction_event, zone_event):
                        correlated = True
                    elif zone_event.timestamp < direction_event.timestamp:
                        raise SetupCaptureMismatch(
                            "zone event arrived without its Pico action"
                        )

                now = self._monotonic()
                if (
                    direction_event is not None
                    and zone_event is None
                    and now >= direction_event.timestamp + EXPECTED_ZONE_EVENT_SECONDS
                ):
                    raise SetupCaptureMismatch("Pico direction produced no zone event")
                if (
                    zone_event is not None
                    and direction_event is None
                    and now >= zone_event.timestamp + ZONE_EVENT_GRACE_SECONDS
                ):
                    raise SetupCaptureMismatch(
                        "zone event arrived without its Pico action"
                    )
        finally:
            if self._may_be_moving:
                await self._async_stop_safely()
            self._end_operation()

    async def async_cancel(self) -> None:
        """Cancel setup and stop a cover that may still be moving."""
        if self._closed:
            return
        self._closed = True
        operation_task = self._operation_task
        if (
            operation_task is not None
            and operation_task is not asyncio.current_task()
            and not operation_task.done()
        ):
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
        if self._may_be_moving:
            await self._async_stop_safely()
        await self._async_cancel_watchdog()
        self._end_operation()
        self._drain_events()

    async def _async_capture_direction(
        self, timeout: float
    ) -> tuple[SetupButtonEvent, ...]:
        deadline = self._monotonic() + timeout
        buttons: list[SetupButtonEvent] = []
        zones: list[_SetupZoneEvent] = []
        while True:
            match = next(
                (
                    (button, zone)
                    for zone in zones
                    for button in buttons
                    if self._events_correlate(button, zone)
                ),
                None,
            )
            if match is not None:
                _, zone = match
                await self._async_stop()
                await self._async_collect_quiet(buttons, CAPTURE_QUIET_SECONDS)
                return self._rank_candidates(buttons, zone.timestamp)

            now = self._monotonic()
            unmatched_zone_deadline = min(
                (
                    zone.timestamp + ZONE_EVENT_GRACE_SECONDS
                    for zone in zones
                    if not any(
                        self._events_correlate(button, zone) for button in buttons
                    )
                ),
                default=deadline,
            )
            unmatched_button_deadline = min(
                (
                    button.timestamp + EXPECTED_ZONE_EVENT_SECONDS
                    for button in buttons
                    if not any(self._events_correlate(button, zone) for zone in zones)
                ),
                default=deadline,
            )
            next_deadline = min(
                deadline, unmatched_zone_deadline, unmatched_button_deadline
            )
            if now >= next_deadline:
                if zones:
                    await self._async_stop_safely()
                    raise SetupCaptureMismatch(
                        "zone event arrived without a correlated Pico action"
                    )
                if buttons:
                    await self._async_stop_safely()
                    raise SetupCaptureMismatch(
                        "Pico action produced no target-zone update"
                    )
                raise SetupCaptureTimeout

            event = await self._async_next_event(next_deadline - now)
            if event is None:
                continue
            if isinstance(event, SetupButtonEvent):
                self._may_be_moving = True
                buttons.append(event)
                continue
            if event.initial:
                await self._async_stop_safely()
                raise SetupCaptureMismatch("controller reconnected during capture")
            self._may_be_moving = True
            zones.append(event)

    async def _async_capture_stop(self, timeout: float) -> tuple[SetupButtonEvent, ...]:
        deadline = self._monotonic() + timeout
        buttons: list[SetupButtonEvent] = []
        while True:
            now = self._monotonic()
            if now >= deadline:
                raise SetupCaptureTimeout
            wait = CAPTURE_QUIET_SECONDS if buttons else deadline - now
            event = await self._async_next_event(min(wait, deadline - now))
            if event is None:
                if buttons:
                    return self._rank_candidates(buttons, buttons[0].timestamp)
                continue
            if isinstance(event, SetupButtonEvent):
                buttons.append(event)
                continue
            if not event.initial:
                self._may_be_moving = True
                await self._async_stop_safely()
                raise SetupCaptureMismatch(
                    "unexpected target-zone activity while learning Stop"
                )

    async def _async_collect_quiet(
        self, buttons: list[SetupButtonEvent], quiet_seconds: float
    ) -> None:
        while (event := await self._async_next_event(quiet_seconds)) is not None:
            if isinstance(event, SetupButtonEvent):
                buttons.append(event)

    async def _async_next_event(self, timeout: float) -> _SetupEvent | None:
        get_event = asyncio.create_task(self._events.get())
        timer = asyncio.ensure_future(self._sleep(max(0.0, timeout)))
        tasks = (get_event, timer)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if get_event in done:
                return get_event.result()
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _async_watchdog(self, timeout: float) -> None:
        await self._sleep(timeout)
        if self._may_be_moving:
            await self._async_stop_safely()
            self._end_operation()

    async def _async_stop(self) -> None:
        duration_task = self._watchdog
        self._watchdog = None
        if duration_task is not None and duration_task is not asyncio.current_task():
            duration_task.cancel()
            with suppress(asyncio.CancelledError):
                await duration_task
        await self._stop_cover()
        self._measurement_started_at = None
        self._may_be_moving = False

    async def _async_stop_safely(self) -> None:
        try:
            await self._async_stop()
        except Exception:  # noqa: BLE001 - cleanup must not mask the original failure
            self._may_be_moving = False

    async def _async_cancel_watchdog(self) -> None:
        if self._watchdog is None:
            return
        watchdog = self._watchdog
        self._watchdog = None
        watchdog.cancel()
        with suppress(asyncio.CancelledError):
            await watchdog

    def _begin_operation(self, name: str) -> None:
        if self._closed:
            raise SetupSessionError("setup session is closed")
        if self._operation_active:
            raise SetupSessionBusyError("another setup operation is active")
        self._operation_active = True
        self._operation_task = asyncio.current_task()
        self._last_operation = name

    def _end_operation(self) -> None:
        self._operation_active = False
        self._operation_task = None

    def _drain_events(self) -> None:
        while not self._events.empty():
            self._events.get_nowait()

    def _direction_command(self, action: ExternalAction) -> AsyncCommand:
        return self._raise_cover if action is ExternalAction.OPEN else self._lower_cover

    @staticmethod
    def _events_correlate(button: SetupButtonEvent, zone: _SetupZoneEvent) -> bool:
        delta = zone.timestamp - button.timestamp
        return (
            0 <= delta <= EXPECTED_ZONE_EVENT_SECONDS
            or -ZONE_EVENT_GRACE_SECONDS <= delta < 0
        )

    @staticmethod
    def _rank_candidates(
        buttons: Sequence[SetupButtonEvent], reference: float
    ) -> tuple[SetupButtonEvent, ...]:
        unique: dict[PicoEventKey, SetupButtonEvent] = {}
        for button in buttons:
            current = unique.get(button.event_key)
            if current is None or abs(button.timestamp - reference) < abs(
                current.timestamp - reference
            ):
                unique[button.event_key] = button
        gesture_rank = {"long_press": 0, "multi_tap": 0, "press": 1, "release": 2}
        return tuple(
            sorted(
                unique.values(),
                key=lambda button: (
                    gesture_rank.get(button.gesture, 3),
                    abs(button.timestamp - reference),
                    button.serial,
                    button.leap_button_number,
                ),
            )
        )
