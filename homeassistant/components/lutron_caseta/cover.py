"""Support for Lutron Caseta shades."""

from enum import Enum
from functools import partial
from typing import Any, override

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    DOMAIN as COVER_DOMAIN,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEVICE_TYPE_OPEN_CLOSE_STOP
from .cover_estimator import OpenCloseStopEstimator
from .entity import LutronCasetaEntity, LutronCasetaUpdatableEntity
from .estimated_cover import EstimatedCoverConfig
from .models import LutronCasetaConfigEntry


class ShadeMovementDirection(Enum):
    """Enum for shade movement direction."""

    OPENING = "opening"
    CLOSING = "closing"
    STOPPED = "stopped"


class LutronCasetaOpenCloseStopCover(LutronCasetaEntity, CoverEntity):
    """Representation of a cover without position feedback."""

    _attr_assumed_state = True
    _attr_is_closed: bool | None = None
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    def __init__(self, device, data) -> None:
        """Initialize command routing and setup ownership checks."""
        super().__init__(device, data)
        self._manager = data.open_close_stop_manager
        self._zone_id = str(device["zone"])

    @override
    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        self._manager.ensure_commands_allowed(self._zone_id)
        await self._smartbridge.lower_cover(self.device_id)

    @override
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        self._manager.ensure_commands_allowed(self._zone_id)
        await self._smartbridge.raise_cover(self.device_id)

    @override
    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        self._manager.ensure_commands_allowed(self._zone_id)
        await self._smartbridge.stop_cover(self.device_id)


class LutronCasetaEstimatedOpenCloseStopCover(LutronCasetaEntity, CoverEntity):
    """Representation of a calibrated cover with an estimated position."""

    _attr_assumed_state = True
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, device, data, config: EstimatedCoverConfig) -> None:
        """Initialize the entity and its timing engine."""
        super().__init__(device, data)
        self._attr_device_class = config.device_class
        self._manager = data.open_close_stop_manager
        self._zone_id = config.zone_id
        self._remove_engine = None
        self._engine = OpenCloseStopEstimator(
            config.travel_times,
            partial(self._smartbridge.raise_cover, self.device_id),
            partial(self._smartbridge.lower_cover, self.device_id),
            partial(self._smartbridge.stop_cover, self.device_id),
            self._handle_estimator_update,
        )

    @property
    @override
    def current_cover_position(self) -> int | None:
        """Return the estimated position when synchronized."""
        return self._engine.snapshot.position

    @property
    @override
    def is_closed(self) -> bool | None:
        """Return whether the estimated position is fully closed."""
        if (position := self.current_cover_position) is None:
            return None
        return position == 0

    @property
    @override
    def is_opening(self) -> bool:
        """Return whether the estimator is moving open."""
        return self._engine.snapshot.is_opening

    @property
    @override
    def is_closing(self) -> bool:
        """Return whether the estimator is moving closed."""
        return self._engine.snapshot.is_closing

    @override
    # pylint: disable-next=home-assistant-missing-super-call
    async def async_added_to_hass(self) -> None:
        """Register this engine with the integration event router."""
        self._remove_engine = self._manager.register_engine(self._zone_id, self._engine)

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Stop active timing and unregister event routing."""
        if self._remove_engine is not None:
            self._remove_engine()
            self._remove_engine = None
        await self._engine.async_shutdown()

    @override
    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close and synchronize at the lower endpoint."""
        self._manager.ensure_commands_allowed(self._zone_id)
        await self._engine.async_move_to(0)

    @override
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open and synchronize at the upper endpoint."""
        self._manager.ensure_commands_allowed(self._zone_id)
        await self._engine.async_move_to(100)

    @override
    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop and retain a position only when motion was synchronized."""
        self._manager.ensure_commands_allowed(self._zone_id)
        await self._engine.async_stop()

    @override
    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move to an estimated percentage, synchronizing first if needed."""
        self._manager.ensure_commands_allowed(self._zone_id)
        await self._engine.async_move_to(kwargs[ATTR_POSITION])

    def _handle_estimator_update(self) -> None:
        """Publish a timing-engine state change."""
        self.async_write_ha_state()


class LutronCasetaShade(LutronCasetaUpdatableEntity, CoverEntity):
    """Representation of a Lutron shade with open/close functionality."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )
    _attr_device_class = CoverDeviceClass.SHADE
    _previous_position: int | None = None
    _movement_direction: ShadeMovementDirection | None = None

    @property
    @override
    def is_closed(self) -> bool:
        """Return if the cover is closed."""
        return self._device["current_state"] < 1

    @property
    @override
    def current_cover_position(self) -> int:
        """Return the current position of cover."""
        return self._device["current_state"]

    @override
    def _handle_bridge_update(self) -> None:
        """Handle updated data from the bridge and track movement direction."""
        current_position = self.current_cover_position

        # Track movement direction based on position changes or endpoint status
        if self._previous_position is not None:
            if current_position > self._previous_position or current_position >= 100:
                # Moving up or at fully open
                self._movement_direction = ShadeMovementDirection.OPENING
            elif current_position < self._previous_position or current_position <= 0:
                # Moving down or at fully closed
                self._movement_direction = ShadeMovementDirection.CLOSING
            else:
                # Stopped
                self._movement_direction = ShadeMovementDirection.STOPPED

        self._previous_position = current_position
        super()._handle_bridge_update()

    @override
    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        # Use set_value to avoid the stuttering issue
        await self._smartbridge.set_value(self.device_id, 0)
        await self.async_update()
        self.async_write_ha_state()

    @override
    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        # Send appropriate directional command before stop to ensure it works correctly
        # Use tracked direction if moving, otherwise use position-based heuristic
        if self._movement_direction is ShadeMovementDirection.OPENING or (
            self._movement_direction in (ShadeMovementDirection.STOPPED, None)
            and self.current_cover_position >= 50
        ):
            await self._smartbridge.raise_cover(self.device_id)
        else:
            await self._smartbridge.lower_cover(self.device_id)

        await self._smartbridge.stop_cover(self.device_id)

    @override
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        # Use set_value to avoid the stuttering issue
        await self._smartbridge.set_value(self.device_id, 100)
        await self.async_update()
        self.async_write_ha_state()

    @override
    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the shade to a specific position."""
        await self._smartbridge.set_value(self.device_id, kwargs[ATTR_POSITION])


class LutronCasetaTiltOnlyBlind(LutronCasetaUpdatableEntity, CoverEntity):
    """Representation of a Lutron tilt only blind."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN_TILT
        | CoverEntityFeature.CLOSE_TILT
        | CoverEntityFeature.SET_TILT_POSITION
        | CoverEntityFeature.OPEN_TILT
    )
    _attr_device_class = CoverDeviceClass.BLIND

    @property
    @override
    def is_closed(self) -> bool:
        """Return if the blind is closed, either at position 0 or 100."""
        return self._device["tilt"] == 0 or self._device["tilt"] == 100

    @property
    @override
    def current_cover_tilt_position(self) -> int:
        """Return the current tilt position of blind."""
        return self._device["tilt"]

    @override
    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the blind."""
        await self._smartbridge.set_tilt(self.device_id, 0)
        await self.async_update()
        self.async_write_ha_state()

    @override
    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the blind."""
        await self._smartbridge.set_tilt(self.device_id, 50)
        await self.async_update()
        self.async_write_ha_state()

    @override
    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the blind to a specific tilt."""
        await self._smartbridge.set_tilt(self.device_id, kwargs[ATTR_TILT_POSITION])


PYLUTRON_TYPE_TO_CLASSES = {
    DEVICE_TYPE_OPEN_CLOSE_STOP: LutronCasetaOpenCloseStopCover,
    "SerenaTiltOnlyWoodBlind": LutronCasetaTiltOnlyBlind,
    "Tilt": LutronCasetaTiltOnlyBlind,
    "SerenaHoneycombShade": LutronCasetaShade,
    "SerenaRollerShade": LutronCasetaShade,
    "TriathlonHoneycombShade": LutronCasetaShade,
    "TriathlonRollerShade": LutronCasetaShade,
    "QsWirelessShade": LutronCasetaShade,
    "QsWirelessHorizontalSheerBlind": LutronCasetaShade,
    "Shade": LutronCasetaShade,
    "PalladiomWireFreeShade": LutronCasetaShade,
    "SerenaEssentialsRollerShade": LutronCasetaShade,
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LutronCasetaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Lutron Caseta cover platform.

    Adds shades from the Caseta bridge associated with the config_entry as
    cover entities.
    """
    data = config_entry.runtime_data
    bridge = data.bridge
    cover_devices = bridge.get_devices_by_domain(COVER_DOMAIN)
    entities: list[LutronCasetaEntity] = []
    for cover_device in cover_devices:
        if cover_device["type"] == DEVICE_TYPE_OPEN_CLOSE_STOP and (
            config := data.open_close_stop_manager.config_for_zone(
                cover_device.get("zone")
            )
        ):
            entities.append(
                LutronCasetaEstimatedOpenCloseStopCover(cover_device, data, config)
            )
            continue

        # Default to the standard shade entity when pylutron adds a new type.
        entity_class = PYLUTRON_TYPE_TO_CLASSES.get(
            cover_device["type"], LutronCasetaShade
        )
        entities.append(entity_class(cover_device, data))

    async_add_entities(entities)
