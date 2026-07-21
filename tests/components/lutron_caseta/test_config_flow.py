"""Test the Lutron Caseta config flow."""

from ipaddress import ip_address
from pathlib import Path
import ssl
from typing import Any
from unittest.mock import AsyncMock, patch

from pylutron_caseta.pairing import PAIR_CA, PAIR_CERT, PAIR_KEY
from pylutron_caseta.smartbridge import (
    Smartbridge,
    ZoneStatusEvent,
    ZoneStatusEventOrigin,
)
import pytest

from homeassistant import config_entries
from homeassistant.components.lutron_caseta import (
    DOMAIN,
    config_flow as CasetaConfigFlow,
)
from homeassistant.components.lutron_caseta.const import (
    ACTION_LONG_PRESS,
    ACTION_MULTITAP,
    ACTION_PRESS,
    ACTION_RELEASE,
    ATTR_ACTION,
    ATTR_BUTTON_TYPE,
    ATTR_LEAP_BUTTON_NUMBER,
    ATTR_SERIAL,
    CALIBRATION_SOURCE_GUIDED_HA,
    CALIBRATION_SOURCE_GUIDED_PICO,
    CONF_BINDINGS,
    CONF_CA_CERTS,
    CONF_CERTFILE,
    CONF_CLOSE_GUARD_SECONDS,
    CONF_CLOSE_TRAVEL_SECONDS,
    CONF_ESTIMATED_COVERS,
    CONF_KEYFILE,
    CONF_OPEN_GUARD_SECONDS,
    CONF_OPEN_TRAVEL_SECONDS,
    CONF_STANDARD_PICOS,
    DEVICE_TYPE_OPEN_CLOSE_STOP,
    ERROR_CANNOT_CONNECT,
    LUTRON_CASETA_BUTTON_EVENT,
    STEP_IMPORT_FAILED,
)
from homeassistant.components.lutron_caseta.cover_estimator import ExternalAction
from homeassistant.components.lutron_caseta.cover_setup import (
    SetupButtonEvent,
    SetupCaptureFailureReason,
    SetupCaptureTimeout,
)
from homeassistant.components.lutron_caseta.estimated_cover import (
    standard_pico_bindings,
)
from homeassistant.components.lutron_caseta.estimated_cover_draft import (
    EstimatedCoverDraft,
    EstimatedCoverDraftStore,
)
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_DEVICE_CLASS, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from . import ENTRY_MOCK_DATA, MockBridge, async_setup_integration

from tests.common import MockConfigEntry

ATTR_HOSTNAME = "hostname"

EMPTY_MOCK_CONFIG_ENTRY = {
    CONF_HOST: "",
    CONF_KEYFILE: "",
    CONF_CERTFILE: "",
    CONF_CA_CERTS: "",
}


MOCK_ASYNC_PAIR_SUCCESS = {
    PAIR_KEY: "mock_key",
    PAIR_CERT: "mock_cert",
    PAIR_CA: "mock_ca",
}


def _estimated_cover_options(
    *,
    open_time: float = 9.8,
    close_time: float = 9.3,
    bindings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one valid estimated-cover options payload for flow tests."""
    return {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: open_time,
                CONF_CLOSE_TRAVEL_SECONDS: close_time,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: bindings or [],
            }
        }
    }


class TwoOpenCloseStopMockBridge(MockBridge):
    """Expose two generic OpenCloseStop zones for concurrent editor tests."""

    def load_devices(self) -> dict[str, dict]:
        """Add a second generic motor zone."""
        devices = super().load_devices()
        devices["806"] = {
            **devices["805"],
            "device_id": "806",
            "zone": "806",
            "name": "Dining Room_Motorized Window Treatment",
            "type": DEVICE_TYPE_OPEN_CLOSE_STOP,
        }
        return devices


class HomeWorksQSXMockBridge(MockBridge):
    """Identify the mock processor as HomeWorks QSX."""

    def load_devices(self) -> dict[str, dict]:
        """Expose native long-hold capability on the processor."""
        devices = super().load_devices()
        devices["1"]["type"] = "HWQSProcessor"
        return devices


async def test_bridge_import_flow(hass: HomeAssistant) -> None:
    """Test a bridge entry gets created and set up during the import flow."""

    entry_mock_data = {
        CONF_HOST: "1.1.1.1",
        CONF_KEYFILE: "",
        CONF_CERTFILE: "",
        CONF_CA_CERTS: "",
    }

    with (
        patch(
            "homeassistant.components.lutron_caseta.async_setup_entry",
            return_value=True,
        ) as mock_setup_entry,
        patch("homeassistant.components.lutron_caseta.async_setup", return_value=True),
        patch.object(
            Smartbridge,
            "create_tls",
        ) as create_tls,
    ):
        create_tls.return_value = MockBridge(can_connect=True)

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=entry_mock_data,
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == CasetaConfigFlow.ENTRY_DEFAULT_TITLE
    assert result["data"] == entry_mock_data
    assert result["result"].unique_id == "000004d2"

    assert len(mock_setup_entry.mock_calls) == 1


async def test_bridge_cannot_connect(hass: HomeAssistant) -> None:
    """Test checking for connection and cannot_connect error."""

    entry_mock_data = {
        CONF_HOST: "not.a.valid.host",
        CONF_KEYFILE: "",
        CONF_CERTFILE: "",
        CONF_CA_CERTS: "",
    }

    with patch.object(Smartbridge, "create_tls") as create_tls:
        create_tls.return_value = MockBridge(can_connect=False)

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=entry_mock_data,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == STEP_IMPORT_FAILED
    assert result["errors"] == {"base": ERROR_CANNOT_CONNECT}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == CasetaConfigFlow.ABORT_REASON_CANNOT_CONNECT


async def test_bridge_cannot_connect_unknown_error(hass: HomeAssistant) -> None:
    """Test checking for connection and encountering an unknown error."""

    with patch.object(Smartbridge, "create_tls") as create_tls:
        mock_bridge = MockBridge()
        mock_bridge.connect = AsyncMock(side_effect=TimeoutError)
        create_tls.return_value = mock_bridge
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=EMPTY_MOCK_CONFIG_ENTRY,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == STEP_IMPORT_FAILED
    assert result["errors"] == {"base": ERROR_CANNOT_CONNECT}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == CasetaConfigFlow.ABORT_REASON_CANNOT_CONNECT


async def test_bridge_invalid_ssl_error(hass: HomeAssistant) -> None:
    """Test checking for connection and encountering invalid ssl certs."""

    with patch.object(Smartbridge, "create_tls", side_effect=ssl.SSLError):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=EMPTY_MOCK_CONFIG_ENTRY,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == STEP_IMPORT_FAILED
    assert result["errors"] == {"base": ERROR_CANNOT_CONNECT}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == CasetaConfigFlow.ABORT_REASON_CANNOT_CONNECT


async def test_duplicate_bridge_import(hass: HomeAssistant) -> None:
    """Test that creating a bridge entry with a duplicate host errors."""

    mock_entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_MOCK_DATA)
    mock_entry.add_to_hass(hass)

    with patch(
        "homeassistant.components.lutron_caseta.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        # Mock entry added, try initializing flow with duplicate host
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=ENTRY_MOCK_DATA,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(mock_setup_entry.mock_calls) == 0


async def test_already_configured_with_ignored(hass: HomeAssistant) -> None:
    """Test ignored entries do not break checking for existing entries."""

    config_entry = MockConfigEntry(
        domain=DOMAIN, data={}, source=config_entries.SOURCE_IGNORE
    )
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={
            CONF_HOST: "1.1.1.1",
            CONF_KEYFILE: "",
            CONF_CERTFILE: "",
            CONF_CA_CERTS: "",
        },
    )
    assert result["type"] is FlowResultType.FORM


async def test_form_user(hass: HomeAssistant, tmp_path: Path) -> None:
    """Test we get the form and can pair."""
    config_dir = tmp_path / "tls_assets"
    await hass.async_add_executor_job(config_dir.mkdir)
    hass.config.config_dir = str(config_dir)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] is None
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "1.1.1.1",
        },
    )
    await hass.async_block_till_done()
    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "link"

    with (
        patch(
            "homeassistant.components.lutron_caseta.config_flow.async_pair",
            return_value=MOCK_ASYNC_PAIR_SUCCESS,
        ),
        patch(
            "homeassistant.components.lutron_caseta.async_setup", return_value=True
        ) as mock_setup,
        patch(
            "homeassistant.components.lutron_caseta.async_setup_entry",
            return_value=True,
        ) as mock_setup_entry,
    ):
        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {},
        )
        await hass.async_block_till_done()

    assert result3["type"] is FlowResultType.CREATE_ENTRY
    assert result3["title"] == "1.1.1.1"
    assert result3["data"] == {
        CONF_HOST: "1.1.1.1",
        CONF_KEYFILE: "lutron_caseta-1.1.1.1-key.pem",
        CONF_CERTFILE: "lutron_caseta-1.1.1.1-cert.pem",
        CONF_CA_CERTS: "lutron_caseta-1.1.1.1-ca.pem",
    }
    assert len(mock_setup.mock_calls) == 1
    assert len(mock_setup_entry.mock_calls) == 1


async def test_form_user_pairing_fails(hass: HomeAssistant, tmp_path: Path) -> None:
    """Test we get the form and we handle pairing failure."""
    config_dir = tmp_path / "tls_assets"
    await hass.async_add_executor_job(config_dir.mkdir)
    hass.config.config_dir = str(config_dir)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] is None
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "1.1.1.1",
        },
    )
    await hass.async_block_till_done()
    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "link"

    with (
        patch(
            "homeassistant.components.lutron_caseta.config_flow.async_pair",
            side_effect=TimeoutError,
        ),
        patch(
            "homeassistant.components.lutron_caseta.async_setup", return_value=True
        ) as mock_setup,
        patch(
            "homeassistant.components.lutron_caseta.async_setup_entry",
            return_value=True,
        ) as mock_setup_entry,
    ):
        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {},
        )
        await hass.async_block_till_done()

    assert result3["type"] is FlowResultType.FORM
    assert result3["errors"] == {"base": "cannot_connect"}
    assert len(mock_setup.mock_calls) == 0
    assert len(mock_setup_entry.mock_calls) == 0


async def test_form_user_reuses_existing_assets_when_pairing_again(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Test the tls assets saved on disk are reused when pairing again."""
    config_dir = tmp_path / "tls_assets"
    await hass.async_add_executor_job(config_dir.mkdir)
    hass.config.config_dir = str(config_dir)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] is None
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "1.1.1.1",
        },
    )
    await hass.async_block_till_done()
    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "link"

    with (
        patch(
            "homeassistant.components.lutron_caseta.config_flow.async_pair",
            return_value=MOCK_ASYNC_PAIR_SUCCESS,
        ),
        patch(
            "homeassistant.components.lutron_caseta.async_setup", return_value=True
        ) as mock_setup,
        patch(
            "homeassistant.components.lutron_caseta.async_setup_entry",
            return_value=True,
        ) as mock_setup_entry,
    ):
        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {},
        )
        await hass.async_block_till_done()

    assert result3["type"] is FlowResultType.CREATE_ENTRY
    assert result3["title"] == "1.1.1.1"
    assert result3["data"] == {
        CONF_HOST: "1.1.1.1",
        CONF_KEYFILE: "lutron_caseta-1.1.1.1-key.pem",
        CONF_CERTFILE: "lutron_caseta-1.1.1.1-cert.pem",
        CONF_CA_CERTS: "lutron_caseta-1.1.1.1-ca.pem",
    }
    assert len(mock_setup.mock_calls) == 1
    assert len(mock_setup_entry.mock_calls) == 1

    with patch(
        "homeassistant.components.lutron_caseta.async_unload_entry", return_value=True
    ) as mock_unload:
        await hass.config_entries.async_remove(result3["result"].entry_id)
        await hass.async_block_till_done()

    assert len(mock_unload.mock_calls) == 1

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] is None
    assert result["step_id"] == "user"

    with patch.object(Smartbridge, "create_tls") as create_tls:
        create_tls.return_value = MockBridge(can_connect=True)
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "1.1.1.1",
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "link"

    with (
        patch("homeassistant.components.lutron_caseta.async_setup", return_value=True),
        patch(
            "homeassistant.components.lutron_caseta.async_setup_entry",
            return_value=True,
        ),
    ):
        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {},
        )
        await hass.async_block_till_done()

    assert result3["type"] is FlowResultType.CREATE_ENTRY
    assert result3["title"] == "1.1.1.1"
    assert result3["data"] == {
        CONF_HOST: "1.1.1.1",
        CONF_KEYFILE: "lutron_caseta-1.1.1.1-key.pem",
        CONF_CERTFILE: "lutron_caseta-1.1.1.1-cert.pem",
        CONF_CA_CERTS: "lutron_caseta-1.1.1.1-ca.pem",
    }


async def _async_start_cover_options(
    hass: HomeAssistant, entry: MockConfigEntry, zone_id: str = "805"
) -> ConfigFlowResult:
    """Open the transactional editor for one configured cover."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "configure"}
    )
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"zone_id": zone_id}
    )


async def _async_finish_cover_options(
    hass: HomeAssistant, result: ConfigFlowResult
) -> ConfigFlowResult:
    """Save staged cover changes and wait for the options reload callback."""
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
        await hass.async_block_till_done()
    return result


async def _async_stage_advanced_binding(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    zone_id: str,
) -> ConfigFlowResult:
    """Stage the same generic advanced mapping for one cover editor."""
    result = await _async_start_cover_options(hass, entry, zone_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_mappings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_add"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"keypad_serial": "66286451"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            ATTR_LEAP_BUTTON_NUMBER: "3",
            "gesture": ACTION_MULTITAP,
            "effect": ExternalAction.OPEN,
        },
    )
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )


async def _async_resolve_progress(
    hass: HomeAssistant, result: ConfigFlowResult
) -> ConfigFlowResult:
    """Poll an immediately completing setup task through its result step."""
    await hass.async_block_till_done()
    while result["type"] in (
        FlowResultType.SHOW_PROGRESS,
        FlowResultType.SHOW_PROGRESS_DONE,
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"])
        await hass.async_block_till_done()
    return result


async def _async_start_new_cover(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ConfigFlowResult:
    """Start onboarding the mock OpenCloseStop zone through cover type."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"zone_id": "805"}
    )
    assert "_" not in result["description_placeholders"]["zone"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DEVICE_CLASS: "blind"}
    )


@pytest.mark.parametrize(
    "entry_state",
    [
        config_entries.ConfigEntryState.UNLOAD_IN_PROGRESS,
        config_entries.ConfigEntryState.SETUP_IN_PROGRESS,
    ],
)
async def test_options_flow_aborts_cleanly_while_entry_reloads(
    hass: HomeAssistant, entry_state: config_entries.ConfigEntryState
) -> None:
    """Avoid a server error when Configure races the options reload."""
    entry = MockConfigEntry(domain=DOMAIN, state=entry_state)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "config_entry_not_loaded"


async def test_estimated_cover_options_flow_add(hass: HomeAssistant) -> None:
    """Test adding timing and standard Pico bindings through native options."""
    entry = await async_setup_integration(hass, MockBridge)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["add"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"zone_id": "805"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_method"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_DEVICE_CLASS: "blind"},
    )
    assert result["step_id"] == "add_controls"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_without_pico"}
    )
    assert result["step_id"] == "add_timing"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manual_timing"}
    )
    assert result["step_id"] == "manual_timing"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_OPEN_TRAVEL_SECONDS: 9.8,
            CONF_CLOSE_TRAVEL_SECONDS: 9.3,
            CONF_OPEN_GUARD_SECONDS: 0.5,
            CONF_CLOSE_GUARD_SECONDS: 0.7,
        },
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "add_review"
    assert result["menu_options"][0] == "finish"
    assert entry.options == {}

    result = await _async_finish_cover_options(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    cover = result["data"][CONF_ESTIMATED_COVERS]["805"]
    assert cover[CONF_DEVICE_CLASS] == "blind"
    assert cover[CONF_OPEN_TRAVEL_SECONDS] == 9.8
    assert cover[CONF_CLOSE_TRAVEL_SECONDS] == 9.3
    assert cover[CONF_OPEN_GUARD_SECONDS] == 0.5
    assert cover[CONF_CLOSE_GUARD_SECONDS] == 0.7
    assert cover[CONF_BINDINGS] == []
    assert cover["calibration"]["source"] == "manual"


async def test_new_cover_abort_stops_motion_and_resumes_before_timing(
    hass: HomeAssistant,
) -> None:
    """Safety-Stop an interrupted setup and resume its last safe checkpoint."""
    entry = await async_setup_integration(hass, MockBridge)
    bridge = entry.runtime_data.bridge
    bridge.lower_cover = AsyncMock()
    bridge.stop_cover = AsyncMock()

    result = await _async_start_new_cover(hass, entry)
    assert result["step_id"] == "add_controls"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_without_pico"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "calibrate_ha"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "calibration_ha_begin"}
    )
    assert result["step_id"] == "calibration_ha_finish"

    hass.config_entries.options.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    bridge.lower_cover.assert_awaited_once()
    bridge.stop_cover.assert_awaited_once()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["resume_add", "discard_add_draft"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "resume_add"}
    )
    assert result["step_id"] == "add_timing"
    assert "finish" not in result["menu_options"]

    hass.config_entries.options.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    draft = await EstimatedCoverDraftStore(hass, entry.entry_id).async_load()
    assert draft is not None
    with pytest.raises(ValueError, match="needs calibration metadata"):
        EstimatedCoverDraft(draft.config, True, True)
    bridge.devices.pop("805")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "draft_unavailable"
    assert result["description_placeholders"] == {"zone_id": "805"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "discard_add_draft"}
    )
    assert result["menu_options"] == ["add"]


async def test_completed_new_cover_draft_resumes_at_review(
    hass: HomeAssistant,
) -> None:
    """Preserve completed manual timing across an accidental flow close."""
    entry = await async_setup_integration(hass, MockBridge)
    result = await _async_start_new_cover(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_without_pico"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manual_timing"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_OPEN_TRAVEL_SECONDS: 12.8,
            CONF_CLOSE_TRAVEL_SECONDS: 12.3,
            CONF_OPEN_GUARD_SECONDS: 1.0,
            CONF_CLOSE_GUARD_SECONDS: 1.0,
        },
    )
    assert result["step_id"] == "add_review"

    hass.config_entries.options.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    persisted = await EstimatedCoverDraftStore(hass, entry.entry_id).async_load()
    assert persisted is not None
    assert persisted.timing_configured is True
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "resume_add"}
    )
    assert result["step_id"] == "add_review"
    result = await _async_finish_cover_options(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    cover = result["data"][CONF_ESTIMATED_COVERS]["805"]
    assert cover[CONF_OPEN_TRAVEL_SECONDS] == 12.8
    assert cover[CONF_CLOSE_TRAVEL_SECONDS] == 12.3
    assert await entry.runtime_data.estimated_cover_draft_store.async_load() is None


async def test_estimated_cover_options_flow_remove(hass: HomeAssistant) -> None:
    """Test removing estimation returns the zone to command-only behavior."""
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 9.8,
                CONF_CLOSE_TRAVEL_SECONDS: 9.3,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [],
            }
        },
        "unrelated": True,
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove"}
    )
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ESTIMATED_COVERS: {}, "unrelated": True}


async def test_estimated_cover_options_flow_edit(hass: HomeAssistant) -> None:
    """Test editing timing preserves the configured zone and unrelated options."""
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 10.0,
                CONF_CLOSE_TRAVEL_SECONDS: 10.0,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [],
            }
        },
        "unrelated": True,
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)

    result = await _async_start_cover_options(hass, entry)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "zone"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manual_timing"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual_timing"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DEVICE_CLASS: "curtain",
            CONF_OPEN_TRAVEL_SECONDS: 9.8,
            CONF_CLOSE_TRAVEL_SECONDS: 9.3,
            CONF_OPEN_GUARD_SECONDS: 0.4,
            CONF_CLOSE_GUARD_SECONDS: 0.6,
        },
    )
    assert result["step_id"] == "zone"
    assert entry.options == options
    result = await _async_finish_cover_options(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["unrelated"] is True
    cover = result["data"][CONF_ESTIMATED_COVERS]["805"]
    assert cover[CONF_DEVICE_CLASS] == "curtain"
    assert cover[CONF_OPEN_TRAVEL_SECONDS] == 9.8
    assert cover[CONF_OPEN_GUARD_SECONDS] == 0.4
    assert cover[CONF_CLOSE_GUARD_SECONDS] == 0.6


async def test_dirty_edit_back_requires_explicit_discard(
    hass: HomeAssistant,
) -> None:
    """Keep pending edits when going back until the user confirms discard."""
    options = _estimated_cover_options(open_time=12.76, close_time=12.23)
    entry = await async_setup_integration(hass, MockBridge, options=options)
    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manual_timing"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DEVICE_CLASS: "blind",
            CONF_OPEN_TRAVEL_SECONDS: 12.5,
            CONF_CLOSE_TRAVEL_SECONDS: 12.0,
            CONF_OPEN_GUARD_SECONDS: 0.8,
            CONF_CLOSE_GUARD_SECONDS: 0.9,
        },
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "back_to_covers"}
    )
    assert result["step_id"] == "discard_changes"
    assert entry.options == options
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    assert result["description_placeholders"]["status"] == "pending changes"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "back_to_covers"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "discard_changes"}
    )

    assert result["step_id"] == "configure"
    assert entry.options == options


async def test_advanced_mapping_options_flow(hass: HomeAssistant) -> None:
    """Add an explicit keypad button, gesture, and effect mapping."""
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 9.8,
                CONF_CLOSE_TRAVEL_SECONDS: 9.3,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [],
            }
        }
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_mappings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_add"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"keypad_serial": "66286451"}
    )
    assert result["step_id"] == "advanced_binding"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            ATTR_LEAP_BUTTON_NUMBER: "3",
            "gesture": ACTION_MULTITAP,
            "effect": ExternalAction.OPEN,
        },
    )
    assert result["step_id"] == "advanced_mappings"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    result = await _async_finish_cover_options(hass, result)

    binding = result["data"][CONF_ESTIMATED_COVERS]["805"][CONF_BINDINGS][0]
    assert binding == {
        "keypad_serial": "66286451",
        "leap_button_number": 3,
        "button_type": "Kitchen Pendants",
        "gesture": "multi_tap",
        "effect": "open",
    }


@pytest.mark.parametrize(
    ("bridge_class", "expected_gestures"),
    [
        (MockBridge, [ACTION_PRESS, ACTION_RELEASE, ACTION_MULTITAP]),
        (
            HomeWorksQSXMockBridge,
            [ACTION_PRESS, ACTION_RELEASE, ACTION_MULTITAP, ACTION_LONG_PRESS],
        ),
    ],
    ids=("generic-processor", "homeworks-qsx"),
)
async def test_advanced_gesture_choices_match_processor_capability(
    hass: HomeAssistant,
    bridge_class: type[MockBridge],
    expected_gestures: list[str],
) -> None:
    """Offer long-press only where the processor exposes native LongHold."""
    entry = await async_setup_integration(
        hass, bridge_class, options=_estimated_cover_options()
    )
    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_mappings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_add"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"keypad_serial": "66286451"}
    )

    assert result["data_schema"].schema["gesture"].config["options"] == (
        expected_gestures
    )


async def test_advanced_mapping_edit_remove_and_duplicate_rejection(
    hass: HomeAssistant,
) -> None:
    """Edit and remove a mapping while rejecting a duplicate event key."""
    binding = {
        "keypad_serial": "66286451",
        "leap_button_number": 3,
        "button_type": "Kitchen Pendants",
        "gesture": ACTION_MULTITAP,
        "effect": ExternalAction.OPEN,
    }
    options = _estimated_cover_options(bindings=[binding])
    entry = await async_setup_integration(hass, MockBridge, options=options)

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_mappings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_add"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"keypad_serial": "66286451"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            ATTR_LEAP_BUTTON_NUMBER: "3",
            "gesture": ACTION_MULTITAP,
            "effect": ExternalAction.CLOSE,
        },
    )
    assert result["step_id"] == "advanced_binding"
    assert result["errors"] == {"base": "binding_already_assigned"}
    assert entry.options == options
    hass.config_entries.options.async_abort(result["flow_id"])

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_mappings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_edit"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"binding": "0"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            ATTR_LEAP_BUTTON_NUMBER: "3",
            "gesture": ACTION_MULTITAP,
            "effect": ExternalAction.CLOSE,
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    assert "close" in result["description_placeholders"]["bindings"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_mappings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "advanced_remove"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"binding": "0"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    result = await _async_finish_cover_options(hass, result)

    assert result["data"][CONF_ESTIMATED_COVERS]["805"][CONF_BINDINGS] == []


async def test_standard_pico_requires_physical_verification(
    hass: HomeAssistant,
) -> None:
    """Verify every proposed standard action before saving a new Pico."""
    options = _estimated_cover_options()
    entry = await async_setup_integration(hass, MockBridge, options=options)
    session = AsyncMock()
    events = {
        ExternalAction.OPEN: SetupButtonEvent("68551522", 3, "raise", "press", 1.0),
        ExternalAction.CLOSE: SetupButtonEvent("68551522", 4, "lower", "press", 2.0),
        ExternalAction.STOP: SetupButtonEvent("68551522", 1, "stop", "press", 3.0),
    }
    session.async_capture_action.side_effect = lambda action: (events[action],)

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "standard_picos"}
    )
    assert result["step_id"] == "standard_picos"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "standard_picos_edit"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STANDARD_PICOS: ["68551522"]}
    )
    assert result["step_id"] == "verify_binding"

    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        for index in range(3):
            note = result["description_placeholders"]["verification_note"]
            assert ("does not move" in note) is (index == 2)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"next_step_id": "verify_binding_begin"}
            )
            result = await _async_resolve_progress(hass, result)

    assert result["step_id"] == "standard_picos"
    assert result["description_placeholders"]["standard_picos"] != "None"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    result = await _async_finish_cover_options(hass, result)

    bindings = result["data"][CONF_ESTIMATED_COVERS]["805"][CONF_BINDINGS]
    assert [(item["leap_button_number"], item["effect"]) for item in bindings] == [
        (3, "open"),
        (4, "close"),
        (1, "stop"),
    ]
    assert session.async_capture_action.await_count == 3


async def test_standard_pico_cancel_after_partial_verification(
    hass: HomeAssistant,
) -> None:
    """Discard an entire proposed standard Pico after one verified action."""
    options = _estimated_cover_options()
    entry = await async_setup_integration(hass, MockBridge, options=options)
    bridge = entry.runtime_data.bridge
    bridge.stop_cover = AsyncMock()
    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "standard_picos"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "standard_picos_edit"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STANDARD_PICOS: ["68551522"]}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "verify_binding_begin"}
    )
    hass.bus.async_fire(
        LUTRON_CASETA_BUTTON_EVENT,
        {
            ATTR_SERIAL: "68551522",
            ATTR_LEAP_BUTTON_NUMBER: 3,
            ATTR_BUTTON_TYPE: "raise",
            ATTR_ACTION: ACTION_PRESS,
        },
    )
    bridge.call_zone_status_subscribers(
        ZoneStatusEvent(
            "805",
            "805",
            {"Zone": {"href": "/zone/805"}},
            ZoneStatusEventOrigin.UPDATE,
        )
    )
    result = await _async_resolve_progress(hass, result)
    assert result["step_id"] == "verify_binding"
    assert result["description_placeholders"]["action"] is ExternalAction.CLOSE

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "standard_picos_cancel"}
    )

    assert result["step_id"] == "standard_picos"
    assert entry.options == options
    assert (
        entry.runtime_data.open_close_stop_manager.diagnostics()["setup_session"]
        is None
    )
    bridge.stop_cover.assert_awaited_once_with("805")


async def test_new_cover_verifies_standard_pico_before_offering_calibration(
    hass: HomeAssistant,
) -> None:
    """Make mapped-Pico calibration available directly in new-cover onboarding."""
    entry = await async_setup_integration(hass, MockBridge)
    session = AsyncMock()
    events = {
        ExternalAction.OPEN: SetupButtonEvent("68551522", 3, "raise", "press", 1.0),
        ExternalAction.CLOSE: SetupButtonEvent("68551522", 4, "lower", "press", 2.0),
        ExternalAction.STOP: SetupButtonEvent("68551522", 1, "stop", "press", 3.0),
    }
    session.async_capture_action.side_effect = lambda action: (events[action],)

    result = await _async_start_new_cover(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_standard_picos"}
    )
    assert result["step_id"] == "standard_picos_edit"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STANDARD_PICOS: ["68551522"]}
    )
    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        for _ in range(3):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"next_step_id": "verify_binding_begin"}
            )
            result = await _async_resolve_progress(hass, result)

    assert result["step_id"] == "add_timing"
    assert result["menu_options"][0] == "calibrate_pico"
    draft = await entry.runtime_data.estimated_cover_draft_store.async_load()
    assert draft is not None
    assert draft.controls_configured is True
    assert draft.timing_configured is False
    assert len(draft.config.bindings) == 3


async def test_standard_pico_form_preserves_partial_advanced_mapping(
    hass: HomeAssistant,
) -> None:
    """Do not discard a conventional-looking action that is not a full standard Pico."""
    partial_binding = standard_pico_bindings(["68551522"])[0]
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 9.8,
                CONF_CLOSE_TRAVEL_SECONDS: 9.3,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [partial_binding.as_dict()],
            }
        }
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "standard_picos"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "standard_picos_edit"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STANDARD_PICOS: []}
    )
    assert result["step_id"] == "standard_picos"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    assert "finish" not in result["menu_options"]
    assert entry.options[CONF_ESTIMATED_COVERS]["805"][CONF_BINDINGS] == [
        partial_binding.as_dict()
    ]


async def test_standard_pico_merges_matching_learned_action(
    hass: HomeAssistant,
) -> None:
    """Complete a standard triplet without duplicating a learned canonical action."""
    partial_binding = standard_pico_bindings(["68551522"])[0]
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 9.8,
                CONF_CLOSE_TRAVEL_SECONDS: 9.3,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [partial_binding.as_dict()],
            }
        }
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)
    expected = {
        binding.effect: SetupButtonEvent(
            binding.keypad_serial,
            binding.leap_button_number,
            binding.button_type,
            binding.gesture,
            1.0,
        )
        for binding in standard_pico_bindings(["68551522"])
    }
    session = AsyncMock()
    session.async_capture_action.side_effect = lambda action: (expected[action],)

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "standard_picos"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "standard_picos_edit"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STANDARD_PICOS: ["68551522"]}
    )
    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        for _ in range(3):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"next_step_id": "verify_binding_begin"}
            )
            result = await _async_resolve_progress(hass, result)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    result = await _async_finish_cover_options(hass, result)
    bindings = result["data"][CONF_ESTIMATED_COVERS]["805"][CONF_BINDINGS]
    assert len(bindings) == 3
    assert (
        len(
            {
                (
                    binding["keypad_serial"],
                    binding["leap_button_number"],
                    binding["gesture"],
                )
                for binding in bindings
            }
        )
        == 3
    )


async def test_learn_action_options_flow(hass: HomeAssistant) -> None:
    """Save only the user-confirmed normalized candidate from learn mode."""
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 9.8,
                CONF_CLOSE_TRAVEL_SECONDS: 9.3,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [],
            }
        }
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)
    session = AsyncMock()
    session.async_capture_action.return_value = (
        SetupButtonEvent("66286451", 3, "Kitchen Pendants", "multi_tap", 1.0),
        SetupButtonEvent("66286451", 3, "Kitchen Pendants", "press", 0.9),
    )

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "learn_action"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "learn_open"}
    )

    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "learn_begin"}
        )
        result = await _async_resolve_progress(hass, result)
        assert result["step_id"] == "learn_candidate"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"candidate": "0"}
        )

    assert result["step_id"] == "learn_action"
    assert "multi_tap" in result["description_placeholders"]["bindings"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    result = await _async_finish_cover_options(hass, result)

    binding = result["data"][CONF_ESTIMATED_COVERS]["805"][CONF_BINDINGS][0]
    assert binding["gesture"] == "multi_tap"
    assert binding["effect"] == "open"


@pytest.mark.parametrize(
    ("learn_step", "expected_step"),
    [
        ("learn_open", "learn_already_mapped"),
        ("learn_close", "learn_conflict"),
    ],
)
async def test_relearn_detects_existing_action_before_save(
    hass: HomeAssistant, learn_step: str, expected_step: str
) -> None:
    """Recognize canonical mappings before offering a duplicate Save action."""
    open_binding = standard_pico_bindings(["68551522"])[0]
    options = _estimated_cover_options(bindings=[open_binding.as_dict()])
    entry = await async_setup_integration(hass, MockBridge, options=options)
    session = AsyncMock()
    session.async_capture_action.return_value = (
        SetupButtonEvent("68551522", 3, "raise", "press", 1.0),
    )

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "learn_action"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": learn_step}
    )
    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "learn_begin"}
        )
        result = await _async_resolve_progress(hass, result)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == expected_step
    assert entry.options == options


async def test_learn_stop_replaces_conflicting_action_after_confirmation(
    hass: HomeAssistant,
) -> None:
    """Replace an existing effect only after the explicit Learn confirmation."""
    open_binding = standard_pico_bindings(["68551522"])[0]
    options = _estimated_cover_options(bindings=[open_binding.as_dict()])
    entry = await async_setup_integration(hass, MockBridge, options=options)
    session = AsyncMock()
    session.async_capture_action.return_value = (
        SetupButtonEvent("68551522", 3, "raise", ACTION_PRESS, 1.0),
    )
    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "learn_action"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "learn_stop"}
    )
    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "learn_begin"}
        )
        result = await _async_resolve_progress(hass, result)

    assert result["step_id"] == "learn_conflict"
    assert entry.options == options
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "learn_replace"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "zone"}
    )
    result = await _async_finish_cover_options(hass, result)

    binding = result["data"][CONF_ESTIMATED_COVERS]["805"][CONF_BINDINGS][0]
    assert binding["effect"] == ExternalAction.STOP


async def test_guided_home_assistant_calibration_options_flow(
    hass: HomeAssistant,
) -> None:
    """Store aggregate samples and provenance after the guided HA sequence."""
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 10.0,
                CONF_CLOSE_TRAVEL_SECONDS: 10.0,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [],
            }
        }
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)
    session = AsyncMock()
    session.async_finish_home_assistant_movement.side_effect = [
        9.8,
        9.3,
        9.9,
        9.2,
    ]

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "calibrate"}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["calibrate_ha", "zone"]
    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "calibrate_ha"}
        )
        assert result["step_id"] == "calibration_ha_start"
        assert (
            result["description_placeholders"]["step"] == "Establish the close endpoint"
        )
        assert (
            result["description_placeholders"]["timing_guidance"]
            == "This only establishes the starting endpoint; no duration will be "
            "recorded."
        )
        assert result["menu_options"] == [
            "calibration_ha_begin",
            "calibration_ha_at_endpoint",
            "calibration_cancel",
        ]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "calibration_ha_at_endpoint"}
        )
        assert result["step_id"] == "calibration_ha_start"
        assert result["description_placeholders"]["previous_result"] == (
            "Starting position confirmed fully closed. No timing was recorded."
        )

        steps = (
            "Open sample 1",
            "Close sample 1",
            "Open sample 2",
            "Close sample 2",
        )
        for index, step in enumerate(steps):
            assert result["step_id"] == "calibration_ha_start"
            assert result["description_placeholders"]["step"] == step
            assert result["menu_options"] == [
                "calibration_ha_begin",
                "calibration_cancel",
            ]
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"next_step_id": "calibration_ha_begin"}
            )
            assert result["step_id"] == "calibration_ha_finish"
            assert result["description_placeholders"]["step"] == step
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {}
            )
            if index < len(steps) - 1:
                assert result["step_id"] == "calibration_ha_start"
                assert result["description_placeholders"]["previous_result"].startswith(
                    f"{step} recorded:"
                )

    assert result["step_id"] == "calibration_summary"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_OPEN_TRAVEL_SECONDS: 10.0,
            CONF_CLOSE_TRAVEL_SECONDS: 9.4,
            CONF_OPEN_GUARD_SECONDS: 0.5,
            CONF_CLOSE_GUARD_SECONDS: 0.8,
        },
    )
    assert result["step_id"] == "zone"
    result = await _async_finish_cover_options(hass, result)

    cover = result["data"][CONF_ESTIMATED_COVERS]["805"]
    assert cover[CONF_OPEN_TRAVEL_SECONDS] == 10.0
    assert cover[CONF_CLOSE_TRAVEL_SECONDS] == 9.4
    assert cover[CONF_OPEN_GUARD_SECONDS] == 0.5
    assert cover[CONF_CLOSE_GUARD_SECONDS] == 0.8
    session.async_confirm_stationary_endpoint.assert_awaited_once_with()
    assert cover["calibration"] == {
        "source": CALIBRATION_SOURCE_GUIDED_HA,
        "open_samples": [9.8, 9.9],
        "close_samples": [9.3, 9.2],
    }


@pytest.mark.parametrize(
    ("open_time", "close_time"),
    [(12.76, 12.23), (12.45, 12.21)],
    ids=("sofia", "giulia"),
)
async def test_existing_cover_abort_during_calibration_stops_and_discards(
    hass: HomeAssistant, open_time: float, close_time: float
) -> None:
    """Safety-Stop an interrupted edit without changing saved cover options."""
    options = _estimated_cover_options(open_time=open_time, close_time=close_time)
    entry = await async_setup_integration(hass, MockBridge, options=options)
    bridge = entry.runtime_data.bridge
    bridge.lower_cover = AsyncMock()
    bridge.stop_cover = AsyncMock()
    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "calibrate"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "calibrate_ha"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "calibration_ha_begin"}
    )
    assert result["step_id"] == "calibration_ha_finish"

    hass.config_entries.options.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    bridge.lower_cover.assert_awaited_once_with("805")
    bridge.stop_cover.assert_awaited_once_with("805")
    assert entry.options == options
    assert (
        entry.runtime_data.open_close_stop_manager.diagnostics()["setup_session"]
        is None
    )


async def test_new_cover_guided_calibration_advances_to_final_review(
    hass: HomeAssistant,
) -> None:
    """Complete calibration inside onboarding without entering the edit menu."""
    entry = await async_setup_integration(hass, MockBridge)
    session = AsyncMock()
    session.async_finish_home_assistant_movement.side_effect = [
        9.8,
        9.3,
        9.9,
        9.2,
    ]
    result = await _async_start_new_cover(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_without_pico"}
    )
    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "calibrate_ha"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "calibration_ha_at_endpoint"}
        )
        for _ in range(4):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"next_step_id": "calibration_ha_begin"}
            )
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {}
            )

    assert result["step_id"] == "calibration_summary"
    hass.config_entries.options.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "resume_add"}
    )
    assert result["step_id"] == "calibration_summary"
    assert result["description_placeholders"]["open_samples"] == "9.80s, 9.90s"
    assert result["description_placeholders"]["close_samples"] == "9.30s, 9.20s"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_OPEN_TRAVEL_SECONDS: 9.85,
            CONF_CLOSE_TRAVEL_SECONDS: 9.25,
            CONF_OPEN_GUARD_SECONDS: 0.5,
            CONF_CLOSE_GUARD_SECONDS: 0.5,
        },
    )
    assert result["step_id"] == "add_review"
    assert result["menu_options"][0] == "finish"


async def test_inconsistent_calibration_requests_third_sample(
    hass: HomeAssistant,
) -> None:
    """Measure a third pass and use its median after an inconsistent pair."""
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 10.0,
                CONF_CLOSE_TRAVEL_SECONDS: 10.0,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [],
            }
        }
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)
    session = AsyncMock()
    session.async_finish_home_assistant_movement.side_effect = [
        1.0,
        9.0,
        9.2,
        10.0,
        9.3,
        9.4,
    ]

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "calibrate"}
    )
    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "calibrate_ha"}
        )
        for _ in range(6):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"next_step_id": "calibration_ha_begin"}
            )
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {}
            )

    assert result["step_id"] == "calibration_summary"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_OPEN_TRAVEL_SECONDS: 9.4,
            CONF_CLOSE_TRAVEL_SECONDS: 9.25,
            CONF_OPEN_GUARD_SECONDS: 1.0,
            CONF_CLOSE_GUARD_SECONDS: 1.0,
        },
    )
    result = await _async_finish_cover_options(hass, result)

    cover = result["data"][CONF_ESTIMATED_COVERS]["805"]
    assert cover[CONF_OPEN_TRAVEL_SECONDS] == 9.4
    assert cover["calibration"]["open_samples"] == [9.0, 10.0, 9.4]
    assert cover["calibration"]["close_samples"] == [9.2, 9.3]


async def test_guided_pico_calibration_does_not_duplicate_commands(
    hass: HomeAssistant,
) -> None:
    """Store mapped-Pico timing while leaving movement commands to Lutron."""
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 10.0,
                CONF_CLOSE_TRAVEL_SECONDS: 10.0,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [
                    binding.as_dict()
                    for binding in standard_pico_bindings(["68551522", "66286451"])
                ],
            }
        }
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)
    session = AsyncMock()
    session.async_measure_pico_movement.side_effect = [
        9.8,
        9.3,
        9.9,
        9.2,
    ]

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "calibrate"}
    )
    assert result["menu_options"] == ["calibrate_ha", "calibrate_pico", "zone"]
    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "calibrate_pico"}
        )
        assert result["type"] is FlowResultType.MENU
        assert result["menu_options"] == ("calibrate_pico_begin", "calibrate")
        controls = result["description_placeholders"]["controls"]
        assert "Dining Room Pico" in controls
        assert "Hallway Main Stairs Position 1 Keypad" in controls
        assert "Dining Room Pico" in result["description_placeholders"]["open_binding"]
        assert "Dining Room Pico" in result["description_placeholders"]["stop_binding"]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "calibrate_pico_begin"}
        )
        assert result["step_id"] == "calibration_pico_positioning"
        assert "68551522" not in str(result["description_placeholders"])
        assert (
            result["description_placeholders"]["direction_binding"]
            == result["description_placeholders"]["direction_bindings"]
        )
        assert (
            result["description_placeholders"]["stop_binding"]
            == result["description_placeholders"]["stop_bindings"]
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"next_step_id": "calibration_pico_at_endpoint"},
        )
        assert result["step_id"] == "calibration_pico_sample"
        assert result["description_placeholders"]["previous_result"] == (
            "Starting position confirmed fully closed. No timing was recorded."
        )
        steps = (
            "Open sample 1",
            "Close sample 1",
            "Open sample 2",
            "Close sample 2",
        )
        for index, step in enumerate(steps):
            assert result["step_id"] == "calibration_pico_sample"
            assert result["description_placeholders"]["step"] == step
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"next_step_id": "calibration_pico_listen"},
            )
            result = await _async_resolve_progress(hass, result)
            if index < len(steps) - 1:
                assert result["step_id"] == "calibration_pico_sample"
                assert result["description_placeholders"]["previous_result"].startswith(
                    f"{step} recorded:"
                )

    assert result["step_id"] == "calibration_summary"
    assert session.async_measure_pico_movement.await_count == 4
    for measurement in session.async_measure_pico_movement.await_args_list:
        assert len(measurement.args[0]) == 2
        assert len(measurement.args[1]) == 2
    session.async_confirm_stationary_endpoint.assert_awaited_once_with()
    session.async_start_home_assistant_movement.assert_not_awaited()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_OPEN_TRAVEL_SECONDS: 9.85,
            CONF_CLOSE_TRAVEL_SECONDS: 9.25,
            CONF_OPEN_GUARD_SECONDS: 0.5,
            CONF_CLOSE_GUARD_SECONDS: 0.5,
        },
    )
    result = await _async_finish_cover_options(hass, result)
    assert result["data"][CONF_ESTIMATED_COVERS]["805"]["calibration"] == {
        "source": CALIBRATION_SOURCE_GUIDED_PICO,
        "open_samples": [9.8, 9.9],
        "close_samples": [9.3, 9.2],
    }


async def test_pico_calibration_retry_identifies_missing_stop(
    hass: HomeAssistant,
) -> None:
    """Tell the operator which correlated Pico calibration event was missing."""
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 10.0,
                CONF_CLOSE_TRAVEL_SECONDS: 10.0,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [
                    binding.as_dict()
                    for binding in standard_pico_bindings(["68551522"])
                ],
            }
        }
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)
    session = AsyncMock()
    session.async_measure_pico_movement.side_effect = SetupCaptureTimeout(
        SetupCaptureFailureReason.STOP_NOT_DETECTED,
        "mapped Stop action was not detected",
    )

    result = await _async_start_cover_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "calibrate"}
    )
    manager = entry.runtime_data.open_close_stop_manager
    with patch.object(manager, "async_begin_setup", AsyncMock(return_value=session)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "calibrate_pico"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "calibrate_pico_begin"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"next_step_id": "calibration_pico_at_endpoint"},
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"next_step_id": "calibration_pico_listen"},
        )
        result = await _async_resolve_progress(hass, result)

    assert result["step_id"] == "calibration_pico_retry"
    assert result["errors"] == {"base": "setup_stop_not_detected"}


async def test_simultaneous_setup_flows_are_rejected(hass: HomeAssistant) -> None:
    """Allow only one options flow to own live setup listeners at a time."""
    options = {
        CONF_ESTIMATED_COVERS: {
            "805": {
                CONF_DEVICE_CLASS: "blind",
                CONF_OPEN_TRAVEL_SECONDS: 9.8,
                CONF_CLOSE_TRAVEL_SECONDS: 9.3,
                CONF_OPEN_GUARD_SECONDS: 1.0,
                CONF_CLOSE_GUARD_SECONDS: 1.0,
                CONF_BINDINGS: [],
            }
        }
    }
    entry = await async_setup_integration(hass, MockBridge, options=options)

    async def async_prepare_learn_flow() -> ConfigFlowResult:
        result = await _async_start_cover_options(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "learn_action"}
        )
        return await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "learn_open"}
        )

    first = await async_prepare_learn_flow()
    first = await hass.config_entries.options.async_configure(
        first["flow_id"], {"next_step_id": "learn_begin"}
    )
    assert first["type"] is FlowResultType.SHOW_PROGRESS

    second = await async_prepare_learn_flow()
    second = await hass.config_entries.options.async_configure(
        second["flow_id"], {"next_step_id": "learn_begin"}
    )
    assert second["type"] is FlowResultType.FORM
    assert second["errors"] == {"base": "setup_in_progress"}

    hass.config_entries.options.async_abort(first["flow_id"])
    hass.config_entries.options.async_abort(second["flow_id"])
    await hass.async_block_till_done()
    assert (
        entry.runtime_data.open_close_stop_manager.diagnostics()["setup_session"]
        is None
    )


async def test_concurrent_editors_revalidate_bindings_when_saving(
    hass: HomeAssistant,
) -> None:
    """Reject a Pico action claimed after another editor staged its changes."""
    cover = {
        CONF_DEVICE_CLASS: "blind",
        CONF_OPEN_TRAVEL_SECONDS: 9.8,
        CONF_CLOSE_TRAVEL_SECONDS: 9.3,
        CONF_OPEN_GUARD_SECONDS: 1.0,
        CONF_CLOSE_GUARD_SECONDS: 1.0,
        CONF_BINDINGS: [],
    }
    options = {CONF_ESTIMATED_COVERS: {"805": cover, "806": dict(cover)}}
    entry = await async_setup_integration(
        hass, TwoOpenCloseStopMockBridge, options=options
    )

    first = await _async_stage_advanced_binding(hass, entry, "805")
    second = await _async_stage_advanced_binding(hass, entry, "806")
    first = await _async_finish_cover_options(hass, first)
    second = await _async_finish_cover_options(hass, second)

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert second["type"] is FlowResultType.MENU
    assert second["step_id"] == "save_conflict"
    assert entry.options[CONF_ESTIMATED_COVERS]["806"][CONF_BINDINGS] == []

    second = await hass.config_entries.options.async_configure(
        second["flow_id"], {"next_step_id": "zone"}
    )
    assert second["step_id"] == "zone"
    assert "pending changes" in second["description_placeholders"]["status"]

    hass.config_entries.options.async_abort(second["flow_id"])


async def test_zeroconf_host_already_configured(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Test starting a flow from discovery when the host is already configured."""
    config_dir = tmp_path / "tls_assets"
    await hass.async_add_executor_job(config_dir.mkdir)
    hass.config.config_dir = str(config_dir)

    config_entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "1.1.1.1"})

    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=ZeroconfServiceInfo(
            ip_address=ip_address("1.1.1.1"),
            ip_addresses=[ip_address("1.1.1.1")],
            hostname="LuTrOn-abc.local.",
            name="mock_name",
            port=None,
            properties={},
            type="mock_type",
        ),
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_lutron_id_already_configured(hass: HomeAssistant) -> None:
    """Test starting a flow from discovery when lutron id already configured."""

    config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: "4.5.6.7"}, unique_id="abc"
    )

    config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=ZeroconfServiceInfo(
            ip_address=ip_address("1.1.1.1"),
            ip_addresses=[ip_address("1.1.1.1")],
            hostname="LuTrOn-abc.local.",
            name="mock_name",
            port=None,
            properties={},
            type="mock_type",
        ),
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_HOST] == "1.1.1.1"


async def test_zeroconf_not_lutron_device(hass: HomeAssistant) -> None:
    """Test starting a flow from discovery when it is not a lutron device."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=ZeroconfServiceInfo(
            ip_address=ip_address("1.1.1.1"),
            ip_addresses=[ip_address("1.1.1.1")],
            hostname="notlutron-abc.local.",
            name="mock_name",
            port=None,
            properties={},
            type="mock_type",
        ),
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_lutron_device"


@pytest.mark.parametrize(
    "source", [config_entries.SOURCE_ZEROCONF, config_entries.SOURCE_HOMEKIT]
)
async def test_zeroconf(hass: HomeAssistant, source, tmp_path: Path) -> None:
    """Test starting a flow from discovery."""
    config_dir = tmp_path / "tls_assets"
    await hass.async_add_executor_job(config_dir.mkdir)
    hass.config.config_dir = str(config_dir)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": source},
        data=ZeroconfServiceInfo(
            ip_address=ip_address("1.1.1.1"),
            ip_addresses=[ip_address("1.1.1.1")],
            hostname="LuTrOn-abc.local.",
            name="mock_name",
            port=None,
            properties={},
            type="mock_type",
        ),
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "link"

    with (
        patch(
            "homeassistant.components.lutron_caseta.config_flow.async_pair",
            return_value=MOCK_ASYNC_PAIR_SUCCESS,
        ),
        patch(
            "homeassistant.components.lutron_caseta.async_setup", return_value=True
        ) as mock_setup,
        patch(
            "homeassistant.components.lutron_caseta.async_setup_entry",
            return_value=True,
        ) as mock_setup_entry,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )
        await hass.async_block_till_done()

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == "abc"
    assert result2["data"] == {
        CONF_HOST: "1.1.1.1",
        CONF_KEYFILE: "lutron_caseta-abc-key.pem",
        CONF_CERTFILE: "lutron_caseta-abc-cert.pem",
        CONF_CA_CERTS: "lutron_caseta-abc-ca.pem",
    }
    assert len(mock_setup.mock_calls) == 1
    assert len(mock_setup_entry.mock_calls) == 1
