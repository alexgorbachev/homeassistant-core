"""Config flow for Lutron Caseta."""

import asyncio
import logging
import os
import ssl
from typing import Any, override

from pylutron_caseta.pairing import PAIR_CA, PAIR_CERT, PAIR_KEY, async_pair
from pylutron_caseta.smartbridge import Smartbridge
import voluptuous as vol

from homeassistant.components.cover import CoverDeviceClass
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_DEVICE_CLASS, CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    ABORT_REASON_CANNOT_CONNECT,
    BRIDGE_DEVICE_ID,
    CONF_CA_CERTS,
    CONF_CERTFILE,
    CONF_CLOSE_GUARD_SECONDS,
    CONF_CLOSE_TRAVEL_SECONDS,
    CONF_ESTIMATED_COVERS,
    CONF_KEYFILE,
    CONF_OPEN_GUARD_SECONDS,
    CONF_OPEN_TRAVEL_SECONDS,
    CONF_STANDARD_PICOS,
    CONFIGURE_TIMEOUT,
    CONNECT_TIMEOUT,
    DEVICE_TYPE_OPEN_CLOSE_STOP,
    DOMAIN,
    ERROR_CANNOT_CONNECT,
    STEP_IMPORT_FAILED,
)
from .cover_estimator import TravelTimes
from .estimated_cover import (
    MAX_GUARD_SECONDS,
    MAX_TRAVEL_SECONDS,
    MIN_TRAVEL_SECONDS,
    SUPPORTED_DEVICE_CLASSES,
    EstimatedCoverConfig,
    parse_estimated_cover_configs,
    standard_pico_bindings,
)
from .models import (
    LUTRON_KEYPAD_AREA_NAME,
    LUTRON_KEYPAD_NAME,
    LUTRON_KEYPAD_SERIAL,
    LUTRON_KEYPAD_TYPE,
    LutronCasetaConfigEntry,
)

HOSTNAME = "hostname"


FILE_MAPPING = {
    PAIR_KEY: CONF_KEYFILE,
    PAIR_CERT: CONF_CERTFILE,
    PAIR_CA: CONF_CA_CERTS,
}

_LOGGER = logging.getLogger(__name__)

ENTRY_DEFAULT_TITLE = "Caséta bridge"

DATA_SCHEMA_USER = vol.Schema({vol.Required(CONF_HOST): str})
TLS_ASSET_TEMPLATE = "lutron_caseta-{}-{}.pem"


class LutronCasetaFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle Lutron Caseta config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize a Lutron Caseta flow."""
        self.data: dict[str, Any] = {}
        self.lutron_id: str | None = None
        self.tls_assets_validated = False
        self.attempted_tls_validation = False

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: LutronCasetaConfigEntry,
    ) -> LutronCasetaOptionsFlow:
        """Return the estimated-cover options flow."""
        return LutronCasetaOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        if user_input is not None:
            self.data[CONF_HOST] = user_input[CONF_HOST]
            return await self.async_step_link()

        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA_USER)

    @override
    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a flow initialized by zeroconf discovery."""
        hostname = discovery_info.hostname
        if hostname is None or not hostname.lower().startswith("lutron-"):
            return self.async_abort(reason="not_lutron_device")

        self.lutron_id = hostname.split("-")[1].replace(".local.", "")

        await self.async_set_unique_id(self.lutron_id)
        host = discovery_info.host
        self._abort_if_unique_id_configured({CONF_HOST: host})

        self.data[CONF_HOST] = host
        self.context["title_placeholders"] = {
            CONF_NAME: self.bridge_id,
            CONF_HOST: host,
        }
        return await self.async_step_link()

    @override
    async def async_step_homekit(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a flow initialized by homekit discovery."""
        return await self.async_step_zeroconf(discovery_info)

    async def async_step_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle pairing with the hub."""
        errors = {}
        # Abort if existing entry with matching host exists.
        self._async_abort_entries_match({CONF_HOST: self.data[CONF_HOST]})

        self._configure_tls_assets()

        if (
            not self.attempted_tls_validation
            and await self.hass.async_add_executor_job(self._tls_assets_exist)
            and await self.async_get_lutron_id()
        ):
            self.tls_assets_validated = True
        self.attempted_tls_validation = True

        if user_input is not None:
            if self.tls_assets_validated:
                # If we previous paired and the tls assets already exist,
                # we do not need to go though pairing again.
                return self.async_create_entry(title=self.bridge_id, data=self.data)

            assets = None
            try:
                assets = await async_pair(self.data[CONF_HOST])
            except (TimeoutError, OSError) as exc:
                _LOGGER.debug("Pairing failed", exc_info=exc)
                errors["base"] = "cannot_connect"

            if not errors:
                await self.hass.async_add_executor_job(self._write_tls_assets, assets)
                return self.async_create_entry(title=self.bridge_id, data=self.data)

        return self.async_show_form(
            step_id="link",
            errors=errors,
            description_placeholders={
                CONF_NAME: self.bridge_id,
                CONF_HOST: self.data[CONF_HOST],
            },
        )

    @property
    def bridge_id(self):
        """Return the best identifier for the bridge.

        If the bridge was not discovered via zeroconf,
        we fallback to using the host.
        """
        return self.lutron_id or self.data[CONF_HOST]

    def _write_tls_assets(self, assets):
        """Write the tls assets to disk."""
        for asset_key, conf_key in FILE_MAPPING.items():
            with open(
                self.hass.config.path(self.data[conf_key]), "w", encoding="utf8"
            ) as file_handle:
                file_handle.write(assets[asset_key])

    def _tls_assets_exist(self):
        """Check to see if tls assets are already on disk."""
        for conf_key in FILE_MAPPING.values():
            if not os.path.exists(self.hass.config.path(self.data[conf_key])):
                return False
        return True

    @callback
    def _configure_tls_assets(self):
        """Fill the tls asset locations in self.data."""
        for asset_key, conf_key in FILE_MAPPING.items():
            self.data[conf_key] = TLS_ASSET_TEMPLATE.format(self.bridge_id, asset_key)

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Import a new Caseta bridge as a config entry.

        This flow is triggered by `async_setup`.
        """
        host = import_data[CONF_HOST]
        # Store the imported config for other steps in this flow to access.
        self.data[CONF_HOST] = host

        # Abort if existing entry with matching host exists.
        self._async_abort_entries_match({CONF_HOST: self.data[CONF_HOST]})

        self.data[CONF_KEYFILE] = import_data[CONF_KEYFILE]
        self.data[CONF_CERTFILE] = import_data[CONF_CERTFILE]
        self.data[CONF_CA_CERTS] = import_data[CONF_CA_CERTS]

        if not (lutron_id := await self.async_get_lutron_id()):
            # Ultimately we won't have a dedicated step for import failure, but
            # in order to keep configuration.yaml-based configs transparently
            # working without requiring further actions from the user, we don't
            # display a form at all before creating a config entry in the
            # default case, so we're only going to show a form in case the
            # import fails.
            # This will change in an upcoming release where UI-based config flow
            # will become the default for the Lutron Caseta integration (which
            # will require users to go through a confirmation flow for imports).
            return await self.async_step_import_failed()

        await self.async_set_unique_id(lutron_id, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=ENTRY_DEFAULT_TITLE, data=self.data)

    async def async_step_import_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Make failed import surfaced to user."""
        self.context["title_placeholders"] = {CONF_NAME: self.data[CONF_HOST]}

        if user_input is None:
            return self.async_show_form(
                step_id=STEP_IMPORT_FAILED,
                description_placeholders={"host": self.data[CONF_HOST]},
                errors={"base": ERROR_CANNOT_CONNECT},
            )

        return self.async_abort(reason=ABORT_REASON_CANNOT_CONNECT)

    async def async_get_lutron_id(self) -> str | None:
        """Check if we can connect to the bridge with the current config."""
        try:
            bridge = Smartbridge.create_tls(
                hostname=self.data[CONF_HOST],
                keyfile=self.hass.config.path(self.data[CONF_KEYFILE]),
                certfile=self.hass.config.path(self.data[CONF_CERTFILE]),
                ca_certs=self.hass.config.path(self.data[CONF_CA_CERTS]),
            )
        except ssl.SSLError:
            _LOGGER.error(
                "Invalid certificate used to connect to bridge at %s",
                self.data[CONF_HOST],
            )
            return None

        try:
            async with asyncio.timeout(CONNECT_TIMEOUT + CONFIGURE_TIMEOUT):
                await bridge.connect()
        except TimeoutError:
            _LOGGER.error(
                "Timeout while trying to connect to bridge at %s",
                self.data[CONF_HOST],
            )
        else:
            if not bridge.is_connected():
                return None
            devices = bridge.get_devices()
            bridge_device = devices[BRIDGE_DEVICE_ID]
            return hex(bridge_device["serial"])[2:].zfill(8)
        finally:
            await bridge.close()

        return None


class LutronCasetaOptionsFlow(OptionsFlow):
    """Manage calibrated OpenCloseStop covers."""

    def __init__(self) -> None:
        """Initialize transient flow state."""
        self._zone_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show available estimated-cover operations."""
        configs = parse_estimated_cover_configs(self.config_entry.options)
        menu_options = ["add"]
        if configs:
            menu_options.extend(("edit", "remove"))
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select an unconfigured OpenCloseStop zone."""
        configured = parse_estimated_cover_configs(self.config_entry.options)
        options = self._cover_options(exclude=set(configured))
        if not options:
            return self.async_abort(reason="no_available_covers")
        if user_input is not None:
            self._zone_id = user_input["zone_id"]
            return await self.async_step_cover()
        return self.async_show_form(
            step_id="add",
            data_schema=vol.Schema(
                {
                    vol.Required("zone_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select an estimated cover to edit."""
        configs = parse_estimated_cover_configs(self.config_entry.options)
        if not configs:
            return self.async_abort(reason="no_configured_covers")
        if user_input is not None:
            self._zone_id = user_input["zone_id"]
            return await self.async_step_cover()
        return self.async_show_form(
            step_id="edit",
            data_schema=vol.Schema(
                {
                    vol.Required("zone_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._cover_options(include=set(configs))
                        )
                    )
                }
            ),
        )

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove estimation while leaving the command-only cover available."""
        configs = parse_estimated_cover_configs(self.config_entry.options)
        if not configs:
            return self.async_abort(reason="no_configured_covers")
        if user_input is not None:
            return self._save_zone(user_input["zone_id"], None)
        return self.async_show_form(
            step_id="remove",
            data_schema=vol.Schema(
                {
                    vol.Required("zone_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._cover_options(include=set(configs))
                        )
                    )
                }
            ),
        )

    async def async_step_cover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure timing and standard Pico associations for one cover."""
        assert self._zone_id is not None
        configs = parse_estimated_cover_configs(self.config_entry.options)
        existing = configs.get(self._zone_id)
        errors: dict[str, str] = {}

        if user_input is not None:
            candidate = EstimatedCoverConfig(
                self._zone_id,
                CoverDeviceClass(user_input[CONF_DEVICE_CLASS]),
                TravelTimes(
                    user_input[CONF_OPEN_TRAVEL_SECONDS],
                    user_input[CONF_CLOSE_TRAVEL_SECONDS],
                    user_input[CONF_OPEN_GUARD_SECONDS],
                    user_input[CONF_CLOSE_GUARD_SECONDS],
                ),
                standard_pico_bindings(user_input[CONF_STANDARD_PICOS]),
            )
            claimed = {
                binding.event_key
                for zone_id, config in configs.items()
                if zone_id != self._zone_id
                for binding in config.bindings
            }
            if any(binding.event_key in claimed for binding in candidate.bindings):
                errors[CONF_STANDARD_PICOS] = "pico_already_assigned"
            else:
                return self._save_zone(self._zone_id, candidate)

        default_times = existing.travel_times if existing else TravelTimes(10, 10, 1, 1)
        selected_picos = (
            list(dict.fromkeys(binding.keypad_serial for binding in existing.bindings))
            if existing
            else []
        )
        return self.async_show_form(
            step_id="cover",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_CLASS,
                        default=(
                            existing.device_class
                            if existing
                            else CoverDeviceClass.BLIND
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                device_class.value
                                for device_class in SUPPORTED_DEVICE_CLASSES
                            ]
                        )
                    ),
                    vol.Required(
                        CONF_OPEN_TRAVEL_SECONDS,
                        default=default_times.open_seconds,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_TRAVEL_SECONDS,
                            max=MAX_TRAVEL_SECONDS,
                            step=0.1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Required(
                        CONF_CLOSE_TRAVEL_SECONDS,
                        default=default_times.close_seconds,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_TRAVEL_SECONDS,
                            max=MAX_TRAVEL_SECONDS,
                            step=0.1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Required(
                        CONF_OPEN_GUARD_SECONDS,
                        default=default_times.open_guard_seconds,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=MAX_GUARD_SECONDS,
                            step=0.1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Required(
                        CONF_CLOSE_GUARD_SECONDS,
                        default=default_times.close_guard_seconds,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=MAX_GUARD_SECONDS,
                            step=0.1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Required(
                        CONF_STANDARD_PICOS, default=selected_picos
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._pico_options(), multiple=True
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"zone": self._cover_label(self._zone_id)},
        )

    def _save_zone(
        self, zone_id: str, config: EstimatedCoverConfig | None
    ) -> ConfigFlowResult:
        """Persist one zone while preserving unrelated integration options."""
        options = dict(self.config_entry.options)
        raw_covers = options.get(CONF_ESTIMATED_COVERS, {})
        covers = dict(raw_covers) if isinstance(raw_covers, dict) else {}
        if config is None:
            covers.pop(zone_id, None)
        else:
            covers[zone_id] = config.as_dict()
        options[CONF_ESTIMATED_COVERS] = covers
        return self.async_create_entry(title="", data=options)

    def _cover_options(
        self,
        *,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
    ) -> list[selector.SelectOptionDict]:
        """Build selectors from live OpenCloseStop discovery."""
        return [
            selector.SelectOptionDict(value=zone_id, label=self._cover_label(zone_id))
            for device in self.config_entry.runtime_data.bridge.get_devices_by_type(
                DEVICE_TYPE_OPEN_CLOSE_STOP
            )
            if (zone_id := str(device["zone"]))
            and (include is None or zone_id in include)
            and (exclude is None or zone_id not in exclude)
        ]

    def _cover_label(self, zone_id: str) -> str:
        """Return a useful live name for an OpenCloseStop zone."""
        bridge = self.config_entry.runtime_data.bridge
        device = bridge.get_device_by_zone_id(zone_id)
        return f"{device['name']} (zone {zone_id})"

    def _pico_options(self) -> list[selector.SelectOptionDict]:
        """List compatible standard raise/lower Picos."""
        return [
            selector.SelectOptionDict(
                value=str(keypad[LUTRON_KEYPAD_SERIAL]),
                label=(
                    f"{keypad[LUTRON_KEYPAD_AREA_NAME]} {keypad[LUTRON_KEYPAD_NAME]}"
                ),
            )
            for keypad in self.config_entry.runtime_data.keypad_data.keypads.values()
            if keypad[LUTRON_KEYPAD_TYPE] == "Pico3ButtonRaiseLower"
        ]
