"""Configure and route estimated OpenCloseStop covers."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
from typing import Any

from pylutron_caseta.smartbridge import (
    Smartbridge,
    ZoneStatusEvent,
    ZoneStatusEventOrigin,
)

from homeassistant.components.cover import CoverDeviceClass
from homeassistant.const import CONF_DEVICE_CLASS, CONF_EFFECT
from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    ACTION_PRESS,
    ATTR_ACTION,
    ATTR_BUTTON_TYPE,
    ATTR_LEAP_BUTTON_NUMBER,
    ATTR_SERIAL,
    CONF_BINDINGS,
    CONF_CLOSE_GUARD_SECONDS,
    CONF_CLOSE_TRAVEL_SECONDS,
    CONF_ESTIMATED_COVERS,
    CONF_GESTURE,
    CONF_KEYPAD_SERIAL,
    CONF_OPEN_GUARD_SECONDS,
    CONF_OPEN_TRAVEL_SECONDS,
    LUTRON_CASETA_BUTTON_EVENT,
)
from .cover_estimator import ExternalAction, OpenCloseStopEstimator, TravelTimes

_LOGGER = logging.getLogger(__name__)

MIN_TRAVEL_SECONDS = 0.1
MAX_TRAVEL_SECONDS = 600.0
MAX_GUARD_SECONDS = 10.0
SUPPORTED_DEVICE_CLASSES = (
    CoverDeviceClass.BLIND,
    CoverDeviceClass.CURTAIN,
    CoverDeviceClass.SHADE,
)


@dataclass(frozen=True, slots=True)
class PicoBinding:
    """Map one stable Pico event tuple to a cover effect."""

    keypad_serial: str
    leap_button_number: int
    button_type: str
    gesture: str
    effect: ExternalAction

    @property
    def event_key(self) -> tuple[str, int, str]:
        """Return the stable event fields used for dispatch."""
        return (self.keypad_serial, self.leap_button_number, self.gesture)

    def as_dict(self) -> dict[str, str | int]:
        """Serialize the binding into config-entry options."""
        return {
            CONF_KEYPAD_SERIAL: self.keypad_serial,
            ATTR_LEAP_BUTTON_NUMBER: self.leap_button_number,
            ATTR_BUTTON_TYPE: self.button_type,
            CONF_GESTURE: self.gesture,
            CONF_EFFECT: self.effect,
        }


@dataclass(frozen=True, slots=True)
class EstimatedCoverConfig:
    """Validated configuration for one estimated cover."""

    zone_id: str
    device_class: CoverDeviceClass
    travel_times: TravelTimes
    bindings: tuple[PicoBinding, ...]

    def as_dict(self) -> dict[str, Any]:
        """Serialize this cover into config-entry options."""
        return {
            CONF_DEVICE_CLASS: self.device_class,
            CONF_OPEN_TRAVEL_SECONDS: self.travel_times.open_seconds,
            CONF_CLOSE_TRAVEL_SECONDS: self.travel_times.close_seconds,
            CONF_OPEN_GUARD_SECONDS: self.travel_times.open_guard_seconds,
            CONF_CLOSE_GUARD_SECONDS: self.travel_times.close_guard_seconds,
            CONF_BINDINGS: [binding.as_dict() for binding in self.bindings],
        }


def standard_pico_bindings(serials: list[str]) -> tuple[PicoBinding, ...]:
    """Build conventional press bindings for three-button raise/lower Picos."""
    buttons = (
        (3, "raise", ExternalAction.OPEN),
        (4, "lower", ExternalAction.CLOSE),
        (1, "stop", ExternalAction.STOP),
    )
    return tuple(
        PicoBinding(str(serial), number, button_type, ACTION_PRESS, effect)
        for serial in serials
        for number, button_type, effect in buttons
    )


def parse_estimated_cover_configs(
    options: Mapping[str, Any],
) -> dict[str, EstimatedCoverConfig]:
    """Validate cover options independently so one bad zone cannot block setup."""
    raw_covers = options.get(CONF_ESTIMATED_COVERS, {})
    if not isinstance(raw_covers, Mapping):
        _LOGGER.error("Ignoring invalid OpenCloseStop estimated-cover options")
        return {}

    configs: dict[str, EstimatedCoverConfig] = {}
    claimed_bindings: dict[tuple[str, int, str], str] = {}
    for raw_zone_id, raw_config in raw_covers.items():
        zone_id = str(raw_zone_id)
        try:
            config = _parse_cover_config(zone_id, raw_config)
        except (TypeError, ValueError, KeyError) as err:
            _LOGGER.error(
                "Ignoring invalid estimated-cover configuration for zone %s: %s",
                zone_id,
                err,
            )
            continue

        conflict = next(
            (
                claimed_bindings[binding.event_key]
                for binding in config.bindings
                if binding.event_key in claimed_bindings
            ),
            None,
        )
        if conflict is not None:
            _LOGGER.error(
                "Ignoring estimated-cover configuration for zone %s: Pico action "
                "is already assigned to zone %s",
                zone_id,
                conflict,
            )
            continue

        configs[zone_id] = config
        for binding in config.bindings:
            claimed_bindings[binding.event_key] = zone_id

    return configs


def _parse_cover_config(zone_id: str, raw_config: Any) -> EstimatedCoverConfig:
    if not isinstance(raw_config, Mapping):
        raise TypeError("cover configuration must be a mapping")

    device_class = CoverDeviceClass(raw_config[CONF_DEVICE_CLASS])
    if device_class not in SUPPORTED_DEVICE_CLASSES:
        raise ValueError(f"unsupported device class {device_class}")

    open_seconds = _validated_number(
        raw_config[CONF_OPEN_TRAVEL_SECONDS],
        CONF_OPEN_TRAVEL_SECONDS,
        minimum=MIN_TRAVEL_SECONDS,
        maximum=MAX_TRAVEL_SECONDS,
    )
    close_seconds = _validated_number(
        raw_config[CONF_CLOSE_TRAVEL_SECONDS],
        CONF_CLOSE_TRAVEL_SECONDS,
        minimum=MIN_TRAVEL_SECONDS,
        maximum=MAX_TRAVEL_SECONDS,
    )
    open_guard = _validated_number(
        raw_config[CONF_OPEN_GUARD_SECONDS],
        CONF_OPEN_GUARD_SECONDS,
        minimum=0,
        maximum=MAX_GUARD_SECONDS,
    )
    close_guard = _validated_number(
        raw_config[CONF_CLOSE_GUARD_SECONDS],
        CONF_CLOSE_GUARD_SECONDS,
        minimum=0,
        maximum=MAX_GUARD_SECONDS,
    )

    raw_bindings = raw_config.get(CONF_BINDINGS, [])
    if not isinstance(raw_bindings, list):
        raise TypeError("bindings must be a list")
    bindings = tuple(_parse_binding(binding) for binding in raw_bindings)
    if len({binding.event_key for binding in bindings}) != len(bindings):
        raise ValueError("bindings contain duplicate Pico actions")

    return EstimatedCoverConfig(
        zone_id,
        device_class,
        TravelTimes(open_seconds, close_seconds, open_guard, close_guard),
        bindings,
    )


def _parse_binding(raw_binding: Any) -> PicoBinding:
    if not isinstance(raw_binding, Mapping):
        raise TypeError("binding must be a mapping")
    serial = str(raw_binding[CONF_KEYPAD_SERIAL])
    button_number = raw_binding[ATTR_LEAP_BUTTON_NUMBER]
    if isinstance(button_number, bool) or not isinstance(button_number, int):
        raise TypeError("LEAP button number must be an integer")
    button_type = raw_binding[ATTR_BUTTON_TYPE]
    gesture = raw_binding[CONF_GESTURE]
    if not isinstance(button_type, str) or not isinstance(gesture, str):
        raise TypeError("button type and gesture must be strings")
    return PicoBinding(
        serial,
        button_number,
        button_type,
        gesture,
        ExternalAction(raw_binding[CONF_EFFECT]),
    )


def _validated_number(
    value: Any, name: str, *, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


class OpenCloseStopManager:
    """Route controller and Pico events to configured cover estimators."""

    def __init__(
        self,
        hass: HomeAssistant,
        bridge: Smartbridge,
        configs: dict[str, EstimatedCoverConfig],
    ) -> None:
        """Subscribe once for the integration and index all configured bindings."""
        self._hass = hass
        self._configs = configs
        self._engines: dict[str, OpenCloseStopEstimator] = {}
        self._binding_routes = {
            binding.event_key: (config.zone_id, binding.effect)
            for config in configs.values()
            for binding in config.bindings
        }
        self._remove_button_listener = hass.bus.async_listen(
            LUTRON_CASETA_BUTTON_EVENT, self._async_handle_button_event
        )
        self._remove_zone_listener = bridge.add_zone_status_subscriber(
            self._handle_zone_event
        )
        self._closed = False

    def config_for_zone(self, zone_id: str | None) -> EstimatedCoverConfig | None:
        """Return validated estimator configuration for a zone."""
        return self._configs.get(str(zone_id)) if zone_id is not None else None

    def register_engine(
        self, zone_id: str, engine: OpenCloseStopEstimator
    ) -> Callable[[], None]:
        """Register an entity engine and return an idempotent removal call."""
        if zone_id in self._engines:
            raise ValueError(f"An estimator is already registered for zone {zone_id}")
        self._engines[zone_id] = engine

        def remove() -> None:
            if self._engines.get(zone_id) is engine:
                self._engines.pop(zone_id)

        return remove

    @callback
    def _handle_zone_event(self, event: ZoneStatusEvent) -> None:
        if self._closed or (engine := self._engines.get(event.zone_id)) is None:
            return
        self._hass.async_create_task(
            engine.async_handle_zone_update(
                initial=event.origin is ZoneStatusEventOrigin.INITIAL
            )
        )

    @callback
    def _async_handle_button_event(self, event: Event) -> None:
        if self._closed:
            return
        data = event.data
        button_number = data.get(ATTR_LEAP_BUTTON_NUMBER)
        if isinstance(button_number, bool) or not isinstance(button_number, int):
            return
        action = data.get(ATTR_ACTION)
        if not isinstance(action, str):
            return
        key = (str(data.get(ATTR_SERIAL)), button_number, action)
        if (route := self._binding_routes.get(key)) is None:
            return
        zone_id, effect = route
        if (engine := self._engines.get(zone_id)) is not None:
            self._hass.async_create_task(engine.async_external_action(effect))

    async def async_shutdown(self) -> None:
        """Remove listeners, stop active engines, and make shutdown idempotent."""
        if self._closed:
            return
        self._closed = True
        self._remove_button_listener()
        self._remove_zone_listener()
        await asyncio.gather(
            *(engine.async_shutdown() for engine in tuple(self._engines.values()))
        )
        self._engines.clear()
