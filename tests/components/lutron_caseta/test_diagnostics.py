"""Test the Lutron Caseta diagnostics."""

from unittest.mock import ANY

from homeassistant.components.lutron_caseta import DOMAIN
from homeassistant.components.lutron_caseta.const import (
    CONF_BINDINGS,
    CONF_CA_CERTS,
    CONF_CALIBRATION,
    CONF_CALIBRATION_SOURCE,
    CONF_CERTFILE,
    CONF_CLOSE_GUARD_SECONDS,
    CONF_CLOSE_SAMPLES,
    CONF_CLOSE_TRAVEL_SECONDS,
    CONF_ESTIMATED_COVERS,
    CONF_KEYFILE,
    CONF_OPEN_GUARD_SECONDS,
    CONF_OPEN_SAMPLES,
    CONF_OPEN_TRAVEL_SECONDS,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_HOST
from homeassistant.core import HomeAssistant

from . import MockBridge, async_setup_integration

from tests.common import MockConfigEntry
from tests.components.diagnostics import get_diagnostics_for_config_entry
from tests.typing import ClientSessionGenerator


async def test_diagnostics(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """Test generating diagnostics for lutron_caseta."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "1.1.1.1",
            CONF_KEYFILE: "",
            CONF_CERTFILE: "",
            CONF_CA_CERTS: "",
        },
        unique_id="abc",
    )
    config_entry.add_to_hass(hass)

    await async_setup_integration(hass, MockBridge, config_entry.entry_id)

    diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)
    assert diag == {
        "bridge_data": {
            "areas": {
                "3": {"id": "3", "name": "House", "parent_id": None},
                "898": {"id": "898", "name": "Basement", "parent_id": "3"},
                "822": {"id": "822", "name": "Bedroom", "parent_id": "898"},
                "910": {"id": "910", "name": "Bathroom", "parent_id": "898"},
                "1024": {"id": "1024", "name": "Master Bedroom", "parent_id": "3"},
                "1025": {"id": "1025", "name": "Kitchen", "parent_id": "3"},
                "1026": {"id": "1026", "name": "Dining Room", "parent_id": "3"},
                "1205": {"id": "1205", "name": "Hallway", "parent_id": "3"},
            },
            "buttons": {
                "111": {
                    "device_id": "111",
                    "current_state": "Release",
                    "button_number": 1,
                    "name": "Dining Room_Pico",
                    "type": "Pico3ButtonRaiseLower",
                    "model": "PJ2-3BRL-GXX-X01",
                    "serial": 68551522,
                    "parent_device": "9",
                },
                "1372": {
                    "device_id": "1372",
                    "current_state": "Release",
                    "button_number": 3,
                    "button_group": "1363",
                    "name": "Hallway_Main Stairs Position 1 Keypad",
                    "type": "SunnataKeypad",
                    "model": "RRST-W3RL-XX",
                    "serial": 66286451,
                    "button_name": "Kitchen Pendants",
                    "button_led": "1362",
                    "device_name": "Kitchen Pendants",
                    "parent_device": "1355",
                },
            },
            "devices": {
                "1": {
                    "model": "model",
                    "name": "bridge",
                    "serial": 1234,
                    "type": "type",
                    "area": "1205",
                },
                "801": {
                    "device_id": "801",
                    "current_state": 100,
                    "fan_speed": None,
                    "zone": "801",
                    "name": "Basement Bedroom_Main Lights",
                    "button_groups": None,
                    "type": "Dimmed",
                    "model": None,
                    "serial": None,
                    "tilt": None,
                    "area": "822",
                },
                "802": {
                    "device_id": "802",
                    "current_state": 100,
                    "fan_speed": None,
                    "zone": "802",
                    "name": "Basement Bedroom_Left Shade",
                    "button_groups": None,
                    "type": "SerenaRollerShade",
                    "model": None,
                    "serial": None,
                    "tilt": None,
                    "area": "822",
                },
                "805": {
                    "device_id": "805",
                    "current_state": -1,
                    "fan_speed": None,
                    "zone": "805",
                    "name": "Basement Bedroom_Motorized Window Treatment",
                    "button_groups": None,
                    "type": "OpenCloseStop",
                    "model": None,
                    "serial": None,
                    "tilt": None,
                    "area": "822",
                },
                "803": {
                    "device_id": "803",
                    "current_state": 100,
                    "fan_speed": None,
                    "zone": "803",
                    "name": "Basement Bathroom_Exhaust Fan",
                    "button_groups": None,
                    "type": "Switched",
                    "model": None,
                    "serial": None,
                    "tilt": None,
                    "area": "910",
                },
                "804": {
                    "device_id": "804",
                    "current_state": 100,
                    "fan_speed": None,
                    "zone": "804",
                    "name": "Master Bedroom_Ceiling Fan",
                    "button_groups": None,
                    "type": "FanSpeed",
                    "model": None,
                    "serial": None,
                    "tilt": None,
                    "area": "1024",
                },
                "901": {
                    "device_id": "901",
                    "current_state": 100,
                    "fan_speed": None,
                    "zone": "901",
                    "name": "Kitchen_Main Lights",
                    "button_groups": None,
                    "type": "WallDimmer",
                    "model": None,
                    "serial": 5442321,
                    "tilt": None,
                    "area": "1025",
                },
                "902": {
                    "device_id": "902",
                    "current_state": 0,
                    "fan_speed": None,
                    "zone": "901",
                    "name": "Kitchen_Other Lights",
                    "button_groups": None,
                    "type": "WallDimmer",
                    "model": None,
                    "serial": 5442322,
                    "tilt": None,
                    "area": "1025",
                },
                "9": {
                    "device_id": "9",
                    "current_state": -1,
                    "fan_speed": None,
                    "tilt": None,
                    "zone": None,
                    "name": "Dining Room_Pico",
                    "button_groups": ["4"],
                    "occupancy_sensors": None,
                    "type": "Pico3ButtonRaiseLower",
                    "model": "PJ2-3BRL-GXX-X01",
                    "serial": 68551522,
                    "device_name": "Pico",
                    "area": "1026",
                },
                "1355": {
                    "device_id": "1355",
                    "current_state": -1,
                    "fan_speed": None,
                    "zone": None,
                    "name": "Hallway_Main Stairs Position 1 Keypad",
                    "button_groups": ["1363"],
                    "type": "SunnataKeypad",
                    "model": "RRST-W3RL-XX",
                    "serial": 66286451,
                    "control_station_name": "Main Stairs",
                    "device_name": "Position 1",
                    "area": "1205",
                },
            },
            "occupancy_groups": {},
            "scenes": {},
            "smart_away_state": "",
        },
        "entry": {
            "data": {"ca_certs": "", "certfile": "", "host": "1.1.1.1", "keyfile": ""},
            "title": "Mock Title",
        },
        "integration_data": {
            "keypad_button_names_to_leap": {
                "1355": {"Kitchen Pendants": 3},
                "9": {"Stop": 1},
            },
            "keypad_buttons": {
                "111": {
                    "button_name": "Stop",
                    "leap_button_number": 1,
                    "led_device_id": None,
                    "lutron_device_id": "111",
                    "parent_keypad": "9",
                },
                "1372": {
                    "button_name": "Kitchen Pendants",
                    "leap_button_number": 3,
                    "led_device_id": "1362",
                    "lutron_device_id": "1372",
                    "parent_keypad": "1355",
                },
            },
            "keypads": {
                "1355": {
                    "area_id": "1205",
                    "area_name": "Hallway",
                    "buttons": ["1372"],
                    "device_info": {
                        "identifiers": [["lutron_caseta", 66286451]],
                        "manufacturer": "Lutron Electronics Co., Inc",
                        "model": "RRST-W3RL-XX (SunnataKeypad)",
                        "name": "Hallway Main Stairs Position 1 Keypad",
                        "suggested_area": "Hallway",
                        "via_device": ["lutron_caseta", 1234],
                    },
                    "dr_device_id": ANY,
                    "lutron_device_id": "1355",
                    "model": "RRST-W3RL-XX",
                    "name": "Main Stairs Position 1 Keypad",
                    "serial": 66286451,
                    "type": "SunnataKeypad",
                },
                "9": {
                    "area_id": "1026",
                    "area_name": "Dining Room",
                    "buttons": ["111"],
                    "device_info": {
                        "identifiers": [["lutron_caseta", 68551522]],
                        "manufacturer": "Lutron Electronics Co., Inc",
                        "model": "PJ2-3BRL-GXX-X01 (Pico3ButtonRaiseLower)",
                        "name": "Dining Room Pico",
                        "suggested_area": "Dining Room",
                        "via_device": ["lutron_caseta", 1234],
                    },
                    "dr_device_id": ANY,
                    "lutron_device_id": "9",
                    "model": "PJ2-3BRL-GXX-X01",
                    "name": "Pico",
                    "serial": 68551522,
                    "type": "Pico3ButtonRaiseLower",
                },
            },
        },
        "open_close_stop": {"covers": {}, "setup_session": None},
    }


async def test_estimated_cover_diagnostics_are_useful_and_redacted(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """Expose estimator provenance and state without certificate paths or names."""
    certificate_paths = {
        CONF_KEYFILE: "/private/client-key.pem",
        CONF_CERTFILE: "/private/client-cert.pem",
        CONF_CA_CERTS: "/private/ca.pem",
    }
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 9.8,
                CONF_CLOSE_TRAVEL_SECONDS: 9.3,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [
                    {
                        "keypad_serial": "68551522",
                        "leap_button_number": 3,
                        "button_type": "raise",
                        "gesture": "press",
                        "effect": "open",
                    }
                ],
                CONF_CALIBRATION: {
                    CONF_CALIBRATION_SOURCE: "guided_home_assistant",
                    CONF_OPEN_SAMPLES: [9.7, 9.9],
                    CONF_CLOSE_SAMPLES: [9.2, 9.4],
                },
            }
        }
    }
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.1.1.1", **certificate_paths},
        options=options,
        unique_id="abc",
    )
    config_entry.add_to_hass(hass)
    await async_setup_integration(hass, MockBridge, config_entry.entry_id)

    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )

    assert diagnostics["entry"]["data"] == {
        CONF_HOST: "1.1.1.1",
        CONF_KEYFILE: "**REDACTED**",
        CONF_CERTFILE: "**REDACTED**",
        CONF_CA_CERTS: "**REDACTED**",
    }
    cover = diagnostics["open_close_stop"]["covers"]["805"]
    assert (
        cover["calibration"] == options[CONF_ESTIMATED_COVERS]["805"][CONF_CALIBRATION]
    )
    assert cover["estimator"] == {
        "state": "unknown",
        "position": None,
        "target": None,
        "generation": 0,
        "last_source": None,
        "desynchronization_reason": "startup",
    }
    serialized = str(diagnostics["open_close_stop"])
    assert "Motorized Window Treatment" not in serialized
    assert all(path not in serialized for path in certificate_paths.values())
