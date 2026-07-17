"""Tests for the Lutron Caseta integration."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from pylutron_caseta.smartbridge import ZoneStatusEvent, ZoneStatusEventOrigin
import pytest

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_SET_COVER_POSITION,
    SERVICE_STOP_COVER,
    CoverEntityFeature,
    CoverState,
)
from homeassistant.components.lutron_caseta.const import (
    ACTION_PRESS,
    ATTR_ACTION,
    ATTR_LEAP_BUTTON_NUMBER,
    ATTR_SERIAL,
    CONF_BINDINGS,
    CONF_CLOSE_GUARD_SECONDS,
    CONF_CLOSE_TRAVEL_SECONDS,
    CONF_ESTIMATED_COVERS,
    CONF_OPEN_GUARD_SECONDS,
    CONF_OPEN_TRAVEL_SECONDS,
    DOMAIN,
    LUTRON_CASETA_BUTTON_EVENT,
)
from homeassistant.components.lutron_caseta.estimated_cover import (
    standard_pico_bindings,
)
from homeassistant.const import (
    ATTR_ASSUMED_STATE,
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    CONF_DEVICE_CLASS,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.state import async_reproduce_state

from . import MockBridge, async_setup_integration

OPEN_CLOSE_STOP_ENTITY_ID = (
    "cover.basement_bedroom_basement_bedroom_motorized_window_treatment"
)

ESTIMATED_OPTIONS = {
    CONF_ESTIMATED_COVERS: {
        "805": {
            CONF_DEVICE_CLASS: "blind",
            CONF_OPEN_TRAVEL_SECONDS: 10.0,
            CONF_CLOSE_TRAVEL_SECONDS: 10.0,
            CONF_OPEN_GUARD_SECONDS: 1.0,
            CONF_CLOSE_GUARD_SECONDS: 1.0,
            CONF_BINDINGS: [
                binding.as_dict() for binding in standard_pico_bindings(["68551522"])
            ],
        }
    }
}


@pytest.fixture
async def mock_bridge_with_cover_mocks(hass: HomeAssistant) -> MockBridge:
    """Set up mock bridge with all cover methods mocked for testing."""
    instance = MockBridge()

    def factory(*args: Any, **kwargs: Any) -> MockBridge:
        """Return the mock bridge instance."""
        return instance

    # Patch all cover methods on the instance with AsyncMocks
    instance.set_value = AsyncMock()
    instance.raise_cover = AsyncMock()
    instance.lower_cover = AsyncMock()
    instance.stop_cover = AsyncMock()

    await async_setup_integration(hass, factory)
    await hass.async_block_till_done()

    return instance


async def test_cover_unique_id(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """Test a cover unique ID."""
    await async_setup_integration(hass, MockBridge)

    cover_entity_id = "cover.basement_bedroom_basement_bedroom_left_shade"

    # Assert that Caseta covers will have the bridge serial hash
    # and the zone id as the uniqueID
    assert entity_registry.async_get(cover_entity_id).unique_id == "000004d2_802"


async def test_open_close_stop_cover_has_unknown_state(
    hass: HomeAssistant, mock_bridge_with_cover_mocks: MockBridge
) -> None:
    """Test an OpenCloseStop cover exposes commands without a position."""
    state = hass.states.get(OPEN_CLOSE_STOP_ENTITY_ID)

    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_ASSUMED_STATE] is True
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    assert ATTR_CURRENT_POSITION not in state.attributes
    assert ATTR_DEVICE_CLASS not in state.attributes


@pytest.mark.parametrize(
    ("service", "bridge_method", "unexpected_methods"),
    [
        pytest.param(
            SERVICE_OPEN_COVER,
            "raise_cover",
            ("lower_cover", "stop_cover"),
            id="open",
        ),
        pytest.param(
            SERVICE_CLOSE_COVER,
            "lower_cover",
            ("raise_cover", "stop_cover"),
            id="close",
        ),
        pytest.param(
            SERVICE_STOP_COVER,
            "stop_cover",
            ("raise_cover", "lower_cover"),
            id="stop",
        ),
    ],
)
async def test_open_close_stop_cover_commands(
    hass: HomeAssistant,
    mock_bridge_with_cover_mocks: MockBridge,
    service: str,
    bridge_method: str,
    unexpected_methods: tuple[str, str],
) -> None:
    """Test OpenCloseStop commands call only their matching bridge method."""
    bridge = mock_bridge_with_cover_mocks

    await hass.services.async_call(
        COVER_DOMAIN,
        service,
        {ATTR_ENTITY_ID: OPEN_CLOSE_STOP_ENTITY_ID},
        blocking=True,
    )

    getattr(bridge, bridge_method).assert_awaited_once_with("805")
    for unexpected_method in unexpected_methods:
        getattr(bridge, unexpected_method).assert_not_awaited()
    bridge.set_value.assert_not_awaited()


async def test_command_only_cover_rejects_commands_during_setup(
    hass: HomeAssistant, mock_bridge_with_cover_mocks: MockBridge
) -> None:
    """Reserve an unconfigured cover exclusively for its setup wizard."""
    manager = next(
        entry.runtime_data.open_close_stop_manager
        for entry in hass.config_entries.async_entries(DOMAIN)
    )
    session = await manager.async_begin_setup("805")

    with pytest.raises(HomeAssistantError, match="setup is in progress"):
        await hass.services.async_call(
            COVER_DOMAIN,
            SERVICE_OPEN_COVER,
            {ATTR_ENTITY_ID: OPEN_CLOSE_STOP_ENTITY_ID},
            blocking=True,
        )
    mock_bridge_with_cover_mocks.raise_cover.assert_not_awaited()

    await manager.async_end_setup(session)


async def test_estimated_open_close_stop_cover_features_and_commands(
    hass: HomeAssistant,
) -> None:
    """Test a configured cover exposes position control and synchronizes first."""
    bridge = MockBridge()
    bridge.raise_cover = AsyncMock()
    bridge.lower_cover = AsyncMock()
    bridge.stop_cover = AsyncMock()
    bridge.set_value = AsyncMock()
    await async_setup_integration(
        hass, lambda **kwargs: bridge, options=ESTIMATED_OPTIONS
    )

    state = hass.states.get(OPEN_CLOSE_STOP_ENTITY_ID)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_ASSUMED_STATE] is True
    assert state.attributes[ATTR_DEVICE_CLASS] == "blind"
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_SET_COVER_POSITION,
        {ATTR_ENTITY_ID: OPEN_CLOSE_STOP_ENTITY_ID, "position": 25},
        blocking=True,
    )

    bridge.stop_cover.assert_awaited_once_with("805")
    bridge.lower_cover.assert_awaited_once_with("805")
    bridge.raise_cover.assert_not_awaited()
    bridge.set_value.assert_not_awaited()
    state = hass.states.get(OPEN_CLOSE_STOP_ENTITY_ID)
    assert state is not None
    assert state.state == "closing"
    assert ATTR_CURRENT_POSITION not in state.attributes

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: OPEN_CLOSE_STOP_ENTITY_ID},
        blocking=True,
    )
    assert bridge.stop_cover.await_count == 2
    assert hass.states.get(OPEN_CLOSE_STOP_ENTITY_ID).state == STATE_UNKNOWN


async def test_estimated_cover_accepts_scene_percentage(hass: HomeAssistant) -> None:
    """Test scene reproduction routes a target percentage to the estimator."""
    bridge = MockBridge()
    bridge.raise_cover = AsyncMock()
    bridge.lower_cover = AsyncMock()
    bridge.stop_cover = AsyncMock()
    await async_setup_integration(
        hass, lambda **kwargs: bridge, options=ESTIMATED_OPTIONS
    )

    await async_reproduce_state(
        hass,
        [
            State(
                OPEN_CLOSE_STOP_ENTITY_ID,
                CoverState.OPEN,
                {ATTR_CURRENT_POSITION: 50},
            )
        ],
    )

    bridge.stop_cover.assert_awaited_once_with("805")
    bridge.lower_cover.assert_awaited_once_with("805")
    bridge.raise_cover.assert_not_awaited()


async def test_invalid_estimator_options_fall_back_to_command_only(
    hass: HomeAssistant,
) -> None:
    """Test one invalid external option cannot prevent integration setup."""
    invalid_options = {
        CONF_ESTIMATED_COVERS: {
            "805": ESTIMATED_OPTIONS[CONF_ESTIMATED_COVERS]["805"]
            | {CONF_OPEN_TRAVEL_SECONDS: -1}
        }
    }
    await async_setup_integration(hass, MockBridge, options=invalid_options)

    state = hass.states.get(OPEN_CLOSE_STOP_ENTITY_ID)
    assert state is not None
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    assert ATTR_CURRENT_POSITION not in state.attributes


async def test_estimated_cover_tracks_standard_pico_without_commands(
    hass: HomeAssistant,
) -> None:
    """Test Pico presses update timing without duplicate bridge commands."""
    bridge = MockBridge()
    bridge.raise_cover = AsyncMock()
    bridge.lower_cover = AsyncMock()
    bridge.stop_cover = AsyncMock()
    await async_setup_integration(
        hass, lambda **kwargs: bridge, options=ESTIMATED_OPTIONS
    )

    hass.bus.async_fire(
        LUTRON_CASETA_BUTTON_EVENT,
        {
            ATTR_SERIAL: "68551522",
            ATTR_LEAP_BUTTON_NUMBER: 3,
            ATTR_ACTION: ACTION_PRESS,
        },
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    state = hass.states.get(OPEN_CLOSE_STOP_ENTITY_ID)
    assert state is not None
    assert state.state == "opening"
    assert ATTR_CURRENT_POSITION not in state.attributes
    bridge.raise_cover.assert_not_awaited()
    bridge.lower_cover.assert_not_awaited()
    bridge.stop_cover.assert_not_awaited()

    hass.bus.async_fire(
        LUTRON_CASETA_BUTTON_EVENT,
        {
            ATTR_SERIAL: "68551522",
            ATTR_LEAP_BUTTON_NUMBER: 1,
            ATTR_ACTION: ACTION_PRESS,
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get(OPEN_CLOSE_STOP_ENTITY_ID).state == STATE_UNKNOWN


async def test_estimated_cover_invalidates_on_reconnect_snapshot(
    hass: HomeAssistant,
) -> None:
    """Test a reconnect snapshot leaves the volatile estimate unknown."""
    bridge = MockBridge()
    await async_setup_integration(
        hass, lambda **kwargs: bridge, options=ESTIMATED_OPTIONS
    )

    bridge.call_zone_status_subscribers(
        ZoneStatusEvent(
            "805",
            "805",
            {"Zone": {"href": "/zone/805"}},
            ZoneStatusEventOrigin.INITIAL,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert hass.states.get(OPEN_CLOSE_STOP_ENTITY_ID).state == STATE_UNKNOWN


async def test_estimated_cover_stops_before_bridge_unload(
    hass: HomeAssistant,
) -> None:
    """Test integration shutdown stops active motion before closing the bridge."""
    bridge = MockBridge()
    bridge.stop_cover = AsyncMock()
    entry = await async_setup_integration(
        hass, lambda **kwargs: bridge, options=ESTIMATED_OPTIONS
    )
    hass.bus.async_fire(
        LUTRON_CASETA_BUTTON_EVENT,
        {
            ATTR_SERIAL: "68551522",
            ATTR_LEAP_BUTTON_NUMBER: 3,
            ATTR_ACTION: ACTION_PRESS,
        },
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert await hass.config_entries.async_unload(entry.entry_id)

    bridge.stop_cover.assert_awaited_once_with("805")
    assert not bridge.is_connected()


async def test_cover_open_close_using_set_value(
    hass: HomeAssistant, mock_bridge_with_cover_mocks: MockBridge
) -> None:
    """Test that open/close commands use set_value to avoid stuttering."""
    mock_instance = mock_bridge_with_cover_mocks
    cover_entity_id = "cover.basement_bedroom_basement_bedroom_left_shade"

    # Test opening the cover
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_OPEN_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # Should use set_value(100) instead of raise_cover
    mock_instance.set_value.assert_called_with("802", 100)
    mock_instance.raise_cover.assert_not_called()

    mock_instance.set_value.reset_mock()
    mock_instance.lower_cover.reset_mock()

    # Test closing the cover
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_CLOSE_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # Should use set_value(0) instead of lower_cover
    mock_instance.set_value.assert_called_with("802", 0)
    mock_instance.lower_cover.assert_not_called()


async def test_cover_stop_with_direction_tracking(
    hass: HomeAssistant, mock_bridge_with_cover_mocks: MockBridge
) -> None:
    """Test that stop command sends appropriate directional command first."""
    mock_instance = mock_bridge_with_cover_mocks
    cover_entity_id = "cover.basement_bedroom_basement_bedroom_left_shade"

    # Simulate shade moving up (opening)
    mock_instance.devices["802"]["current_state"] = 30
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    mock_instance.devices["802"]["current_state"] = 60
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    # Now stop while opening
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # Should send raise_cover before stop_cover when opening
    mock_instance.raise_cover.assert_called_with("802")
    mock_instance.stop_cover.assert_called_with("802")
    mock_instance.lower_cover.assert_not_called()

    mock_instance.raise_cover.reset_mock()
    mock_instance.lower_cover.reset_mock()
    mock_instance.stop_cover.reset_mock()

    # Simulate shade moving down (closing)
    mock_instance.devices["802"]["current_state"] = 40
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    mock_instance.devices["802"]["current_state"] = 20
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    # Now stop while closing
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # Should send lower_cover before stop_cover when closing
    mock_instance.lower_cover.assert_called_with("802")
    mock_instance.stop_cover.assert_called_with("802")
    mock_instance.raise_cover.assert_not_called()


async def test_cover_stop_at_endpoints(
    hass: HomeAssistant, mock_bridge_with_cover_mocks: MockBridge
) -> None:
    """Test stop command behavior when shade is at fully open or closed."""
    mock_instance = mock_bridge_with_cover_mocks
    cover_entity_id = "cover.basement_bedroom_basement_bedroom_left_shade"

    # Test stop at fully open (100) - should infer it was opening
    mock_instance.devices["802"]["current_state"] = 100
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # At fully open, should send raise_cover before stop
    mock_instance.raise_cover.assert_called_with("802")
    mock_instance.stop_cover.assert_called_with("802")

    mock_instance.raise_cover.reset_mock()
    mock_instance.lower_cover.reset_mock()
    mock_instance.stop_cover.reset_mock()

    # Test stop at fully closed (0) - should infer it was closing
    mock_instance.devices["802"]["current_state"] = 0
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # At fully closed, should send lower_cover before stop
    mock_instance.lower_cover.assert_called_with("802")
    mock_instance.stop_cover.assert_called_with("802")


async def test_cover_position_heuristic_fallback(
    hass: HomeAssistant, mock_bridge_with_cover_mocks: MockBridge
) -> None:
    """Test stop command uses position heuristic when movement direction is unknown."""
    mock_instance = mock_bridge_with_cover_mocks
    cover_entity_id = "cover.basement_bedroom_basement_bedroom_left_shade"

    # Test stop at position < 50 with no movement
    # Update the device data directly in the bridge's devices dict
    mock_instance.devices["802"]["current_state"] = 30
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # Position < 50, should send lower_cover
    mock_instance.lower_cover.assert_called_with("802")
    mock_instance.stop_cover.assert_called_with("802")

    mock_instance.raise_cover.reset_mock()
    mock_instance.lower_cover.reset_mock()
    mock_instance.stop_cover.reset_mock()

    # Test stop at position >= 50 with no movement
    mock_instance.devices["802"]["current_state"] = 70
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # Position >= 50, should send raise_cover
    mock_instance.raise_cover.assert_called_with("802")
    mock_instance.stop_cover.assert_called_with("802")


async def test_cover_stopped_movement_detection(
    hass: HomeAssistant, mock_bridge_with_cover_mocks: MockBridge
) -> None:
    """Test that movement direction is set to STOPPED when position doesn't change."""
    mock_instance = mock_bridge_with_cover_mocks
    cover_entity_id = "cover.basement_bedroom_basement_bedroom_left_shade"

    # Set initial position
    mock_instance.devices["802"]["current_state"] = 50
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    # Send same position again - should detect as stopped
    mock_instance.devices["802"]["current_state"] = 50
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    # Now stop command should use position heuristic (>= 50)
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # Position >= 50 with STOPPED direction, should send raise_cover
    mock_instance.raise_cover.assert_called_with("802")
    mock_instance.stop_cover.assert_called_with("802")


async def test_cover_startup_with_shade_in_motion(
    hass: HomeAssistant, mock_bridge_with_cover_mocks: MockBridge
) -> None:
    """Test stop command when HA starts with shade already in motion."""
    mock_instance = mock_bridge_with_cover_mocks
    cover_entity_id = "cover.basement_bedroom_basement_bedroom_left_shade"

    # Shade starts at position 50 (simulating HA startup with shade in motion)
    # First stop without seeing movement should use position heuristic
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # Should have used position heuristic since we haven't seen movement yet
    # Initial position is 100 from MockBridge, so >= 50, should send raise_cover
    mock_instance.raise_cover.assert_called_with("802")
    mock_instance.stop_cover.assert_called_with("802")

    mock_instance.raise_cover.reset_mock()
    mock_instance.stop_cover.reset_mock()

    # Now simulate shade moving down (shade was actually in motion)
    mock_instance.devices["802"]["current_state"] = 45
    mock_instance.call_subscribers("802")
    await hass.async_block_till_done()

    # Now we've detected downward movement
    await hass.services.async_call(
        COVER_DOMAIN,
        SERVICE_STOP_COVER,
        {ATTR_ENTITY_ID: cover_entity_id},
        blocking=True,
    )

    # Should now correctly send lower_cover since we detected downward movement
    mock_instance.lower_cover.assert_called_with("802")
    mock_instance.stop_cover.assert_called_with("802")
