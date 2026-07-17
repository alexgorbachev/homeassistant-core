"""Config flow for Lutron Caseta."""

import asyncio
from dataclasses import replace
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
    ACTION_LONG_PRESS,
    ACTION_MULTITAP,
    ACTION_PRESS,
    ACTION_RELEASE,
    ATTR_LEAP_BUTTON_NUMBER,
    BRIDGE_DEVICE_ID,
    BRIDGE_DEVICE_TYPES_WITH_LONG_HOLD,
    CALIBRATION_SOURCE_GUIDED_HA,
    CALIBRATION_SOURCE_GUIDED_PICO,
    CALIBRATION_SOURCE_MANUAL,
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
from .cover_estimator import ExternalAction, TravelTimes
from .cover_setup import (
    OpenCloseStopSetupSession,
    SetupButtonEvent,
    SetupCaptureMismatch,
    SetupCaptureTimeout,
    SetupSessionBusyError,
    ThirdSampleRequired,
    aggregate_calibration_samples,
)
from .estimated_cover import (
    MAX_GUARD_SECONDS,
    MAX_TRAVEL_SECONDS,
    MIN_TRAVEL_SECONDS,
    SUPPORTED_DEVICE_CLASSES,
    CalibrationMetadata,
    EstimatedCoverConfig,
    PicoBinding,
    parse_estimated_cover_configs,
    standard_pico_bindings,
)
from .models import (
    LUTRON_BUTTON_BUTTON_NAME,
    LUTRON_BUTTON_LEAP_BUTTON_NUMBER,
    LUTRON_KEYPAD_AREA_NAME,
    LUTRON_KEYPAD_BUTTONS,
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
    """Manage estimated OpenCloseStop covers through guided setup."""

    def __init__(self) -> None:
        """Initialize transient wizard state without changing stored options."""
        self._zone_id: str | None = None
        self._working_config: EstimatedCoverConfig | None = None
        self._session: OpenCloseStopSetupSession | None = None
        self._progress_task: asyncio.Task[Any] | None = None
        self._capture_action: ExternalAction | None = None
        self._capture_candidates: tuple[SetupButtonEvent, ...] = ()
        self._pending_bindings: tuple[PicoBinding, ...] | None = None
        self._verification_queue: list[PicoBinding] = []
        self._selected_keypad_serial: str | None = None
        self._editing_binding: int | None = None
        self._calibration_method = CALIBRATION_SOURCE_GUIDED_HA
        self._calibration_plan: list[tuple[ExternalAction, bool]] = []
        self._calibration_action: ExternalAction | None = None
        self._calibration_counted = False
        self._calibration_initial_complete = False
        self._open_samples: list[float] = []
        self._close_samples: list[float] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show available cover setup operations."""
        menu_options = ["add"]
        if parse_estimated_cover_configs(self.config_entry.options):
            menu_options.append("configure")
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
            self._working_config = EstimatedCoverConfig(
                self._zone_id,
                CoverDeviceClass.BLIND,
                TravelTimes(10, 10, 1, 1),
                (),
            )
            return await self.async_step_add_method()
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

    async def async_step_add_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose guided calibration or expert manual timing for a new cover."""
        assert self._working_config is not None
        if user_input is not None:
            self._working_config = replace(
                self._working_config,
                device_class=CoverDeviceClass(user_input[CONF_DEVICE_CLASS]),
            )
            if user_input["setup_method"] == "guided":
                return await self.async_step_calibrate()
            return await self.async_step_manual_timing()
        return self.async_show_form(
            step_id="add_method",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_CLASS, default=CoverDeviceClass.BLIND
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[item.value for item in SUPPORTED_DEVICE_CLASSES]
                        )
                    ),
                    vol.Required("setup_method", default="guided"): vol.In(
                        {"guided": "Guided calibration", "manual": "Manual timing"}
                    ),
                }
            ),
            description_placeholders={
                "zone": self._cover_label(self._working_config.zone_id)
            },
        )

    async def async_step_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a configured cover to manage."""
        configs = parse_estimated_cover_configs(self.config_entry.options)
        if not configs:
            return self.async_abort(reason="no_configured_covers")
        if user_input is not None:
            self._zone_id = user_input["zone_id"]
            self._working_config = configs[self._zone_id]
            return await self.async_step_zone()
        return self.async_show_form(
            step_id="configure",
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

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Retain the previous edit entry point for API clients."""
        return await self.async_step_configure(user_input)

    async def async_step_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show all operations for the selected cover."""
        assert self._working_config is not None
        return self.async_show_menu(
            step_id="zone",
            menu_options=(
                "overview",
                "standard_picos",
                "advanced_mappings",
                "learn_action",
                "calibrate",
                "manual_timing",
                "remove",
            ),
            description_placeholders={
                "zone": self._cover_label(self._working_config.zone_id)
            },
        )

    async def async_step_overview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Summarize timing and action bindings for one cover."""
        config = self._require_working_config()
        if user_input is not None:
            return await self.async_step_zone()
        return self.async_show_form(
            step_id="overview",
            data_schema=vol.Schema({}),
            description_placeholders={
                "zone": self._cover_label(config.zone_id),
                "open_time": str(config.travel_times.open_seconds),
                "close_time": str(config.travel_times.close_seconds),
                "bindings": str(len(config.bindings)),
                "source": (
                    config.calibration.source
                    if config.calibration is not None
                    else "existing configuration"
                ),
            },
        )

    async def async_step_standard_picos(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Associate standard raise/lower Picos after physical verification."""
        config = self._require_working_config()
        errors: dict[str, str] = {}
        current_serials = self._standard_pico_serials(config.bindings)
        if user_input is not None:
            selected = list(user_input[CONF_STANDARD_PICOS])
            standard = standard_pico_bindings(selected)
            current_standard_serials = set(current_serials)
            advanced = tuple(
                binding
                for binding in config.bindings
                if binding.keypad_serial not in current_standard_serials
                or not self._is_standard_binding(binding)
            )
            candidate = (*advanced, *standard)
            if self._bindings_conflict(candidate):
                errors[CONF_STANDARD_PICOS] = "pico_already_assigned"
            else:
                self._pending_bindings = candidate
                added = set(selected) - set(current_serials)
                self._verification_queue = [
                    binding for binding in standard if binding.keypad_serial in added
                ]
                if not self._verification_queue:
                    return await self._async_save_bindings(candidate)
                return await self.async_step_verify_binding()
        return self.async_show_form(
            step_id="standard_picos",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_STANDARD_PICOS, default=current_serials
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._pico_options(), multiple=True
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={"zone": self._cover_label(config.zone_id)},
        )

    async def async_step_verify_binding(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt before moving the cover to verify one standard Pico action."""
        binding = self._verification_queue[0]
        if user_input is not None:
            if error := await self._async_ensure_setup():
                return self.async_show_form(
                    step_id="verify_binding",
                    data_schema=self._confirmation_schema("start"),
                    errors={"base": error},
                    description_placeholders={
                        "action": binding.effect,
                        "button": binding.button_type,
                        "pico": self._keypad_label(binding.keypad_serial),
                    },
                )
            assert self._session is not None
            self._progress_task = self.hass.async_create_task(
                self._session.async_capture_action(binding.effect)
            )
            return await self.async_step_verify_binding_progress()
        return self.async_show_form(
            step_id="verify_binding",
            data_schema=self._confirmation_schema("start"),
            description_placeholders={
                "action": binding.effect,
                "button": binding.button_type,
                "pico": self._keypad_label(binding.keypad_serial),
            },
        )

    async def async_step_verify_binding_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for a standard Pico action and target-zone confirmation."""
        assert self._progress_task is not None
        if not self._progress_task.done():
            return self.async_show_progress(
                step_id="verify_binding_progress",
                progress_action="verify_binding",
                progress_task=self._progress_task,
            )
        return self.async_show_progress_done(next_step_id="verify_binding_result")

    async def async_step_verify_binding_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate the captured action and continue standard Pico verification."""
        assert self._progress_task is not None
        binding = self._verification_queue[0]
        try:
            candidates = await self._progress_task
        except SetupCaptureMismatch, SetupCaptureTimeout:
            self._progress_task = None
            return self.async_show_form(
                step_id="verify_binding",
                data_schema=self._confirmation_schema("start"),
                errors={"base": "verification_failed"},
                description_placeholders={
                    "action": binding.effect,
                    "button": binding.button_type,
                    "pico": self._keypad_label(binding.keypad_serial),
                },
            )
        except Exception:
            _LOGGER.exception("Unable to stop cover after Pico verification")
            self._progress_task = None
            return self.async_show_form(
                step_id="verify_binding",
                data_schema=self._confirmation_schema("start"),
                errors={"base": "verification_failed"},
                description_placeholders={
                    "action": binding.effect,
                    "button": binding.button_type,
                    "pico": self._keypad_label(binding.keypad_serial),
                },
            )
        self._progress_task = None
        if binding.event_key not in {candidate.event_key for candidate in candidates}:
            return self.async_show_form(
                step_id="verify_binding",
                data_schema=self._confirmation_schema("start"),
                errors={"base": "verification_mismatch"},
                description_placeholders={
                    "action": binding.effect,
                    "button": binding.button_type,
                    "pico": self._keypad_label(binding.keypad_serial),
                },
            )
        self._verification_queue.pop(0)
        if self._verification_queue:
            return await self.async_step_verify_binding()
        assert self._pending_bindings is not None
        return await self._async_save_bindings(self._pending_bindings)

    async def async_step_advanced_mappings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show individual mapping operations."""
        config = self._require_working_config()
        options = ["advanced_add"]
        if config.bindings:
            options.extend(("advanced_edit", "advanced_remove"))
        options.append("zone")
        return self.async_show_menu(step_id="advanced_mappings", menu_options=options)

    async def async_step_advanced_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a keypad for a new explicit mapping."""
        if user_input is not None:
            self._selected_keypad_serial = user_input["keypad_serial"]
            self._editing_binding = None
            return await self.async_step_advanced_binding()
        return self.async_show_form(
            step_id="advanced_add",
            data_schema=vol.Schema(
                {
                    vol.Required("keypad_serial"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._keypad_options())
                    )
                }
            ),
        )

    async def async_step_advanced_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select an existing mapping to edit."""
        config = self._require_working_config()
        if user_input is not None:
            self._editing_binding = int(user_input["binding"])
            self._selected_keypad_serial = config.bindings[
                self._editing_binding
            ].keypad_serial
            return await self.async_step_advanced_binding()
        return self.async_show_form(
            step_id="advanced_edit",
            data_schema=vol.Schema(
                {
                    vol.Required("binding"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._binding_options(config.bindings)
                        )
                    )
                }
            ),
        )

    async def async_step_advanced_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove one explicit or learned mapping."""
        config = self._require_working_config()
        if user_input is not None:
            index = int(user_input["binding"])
            bindings = tuple(
                binding
                for position, binding in enumerate(config.bindings)
                if position != index
            )
            return await self._async_save_bindings(bindings)
        return self.async_show_form(
            step_id="advanced_remove",
            data_schema=vol.Schema(
                {
                    vol.Required("binding"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._binding_options(config.bindings)
                        )
                    )
                }
            ),
        )

    async def async_step_advanced_binding(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure button, gesture, and effect for one mapping."""
        config = self._require_working_config()
        assert self._selected_keypad_serial is not None
        existing = (
            config.bindings[self._editing_binding]
            if self._editing_binding is not None
            else None
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            button_number = int(user_input[ATTR_LEAP_BUTTON_NUMBER])
            binding = PicoBinding(
                self._selected_keypad_serial,
                button_number,
                self._button_name(self._selected_keypad_serial, button_number),
                user_input["gesture"],
                ExternalAction(user_input["effect"]),
            )
            bindings = list(config.bindings)
            if self._editing_binding is None:
                bindings.append(binding)
            else:
                bindings[self._editing_binding] = binding
            if self._bindings_conflict(tuple(bindings)):
                errors["base"] = "binding_already_assigned"
            else:
                return await self._async_save_bindings(tuple(bindings))
        button_field = (
            vol.Required(ATTR_LEAP_BUTTON_NUMBER, default=existing.leap_button_number)
            if existing is not None
            else vol.Required(ATTR_LEAP_BUTTON_NUMBER)
        )
        return self.async_show_form(
            step_id="advanced_binding",
            data_schema=vol.Schema(
                {
                    button_field: selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._button_options(self._selected_keypad_serial)
                        )
                    ),
                    vol.Required(
                        "gesture",
                        default=(existing.gesture if existing else ACTION_PRESS),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._gesture_options())
                    ),
                    vol.Required(
                        "effect",
                        default=(existing.effect if existing else ExternalAction.OPEN),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[action.value for action in ExternalAction]
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "keypad": self._keypad_label(self._selected_keypad_serial)
            },
        )

    async def async_step_learn_action(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the effect to learn from a physical interaction."""
        if user_input is not None:
            self._capture_action = ExternalAction(user_input["effect"])
            return await self.async_step_learn_start()
        return self.async_show_form(
            step_id="learn_action",
            data_schema=vol.Schema(
                {
                    vol.Required("effect"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[action.value for action in ExternalAction]
                        )
                    )
                }
            ),
        )

    async def async_step_learn_start(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt before listening for a Pico action."""
        assert self._capture_action is not None
        if user_input is not None:
            if error := await self._async_ensure_setup():
                return self.async_show_form(
                    step_id="learn_start",
                    data_schema=self._confirmation_schema("start"),
                    errors={"base": error},
                    description_placeholders={"action": self._capture_action},
                )
            assert self._session is not None
            self._progress_task = self.hass.async_create_task(
                self._session.async_capture_action(self._capture_action)
            )
            return await self.async_step_learn_progress()
        return self.async_show_form(
            step_id="learn_start",
            data_schema=self._confirmation_schema("start"),
            description_placeholders={"action": self._capture_action},
        )

    async def async_step_learn_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for a physical button interaction."""
        assert self._progress_task is not None
        if not self._progress_task.done():
            return self.async_show_progress(
                step_id="learn_progress",
                progress_action="learn_action",
                progress_task=self._progress_task,
            )
        return self.async_show_progress_done(next_step_id="learn_result")

    async def async_step_learn_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Present normalized candidates after capture."""
        assert self._progress_task is not None
        assert self._capture_action is not None
        try:
            self._capture_candidates = await self._progress_task
        except SetupCaptureTimeout:
            self._progress_task = None
            return self.async_show_form(
                step_id="learn_start",
                data_schema=self._confirmation_schema("start"),
                errors={"base": "capture_timeout"},
                description_placeholders={"action": self._capture_action},
            )
        except SetupCaptureMismatch:
            self._progress_task = None
            return self.async_show_form(
                step_id="learn_start",
                data_schema=self._confirmation_schema("start"),
                errors={"base": "capture_mismatch"},
                description_placeholders={"action": self._capture_action},
            )
        except Exception:
            _LOGGER.exception("Unable to stop cover after action capture")
            self._progress_task = None
            return self.async_show_form(
                step_id="learn_start",
                data_schema=self._confirmation_schema("start"),
                errors={"base": "command_failed"},
                description_placeholders={"action": self._capture_action},
            )
        self._progress_task = None
        return await self.async_step_learn_candidate()

    async def async_step_learn_candidate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose the intended gesture from captured candidates."""
        config = self._require_working_config()
        assert self._capture_action is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            event = self._capture_candidates[int(user_input["candidate"])]
            binding = PicoBinding(
                event.serial,
                event.leap_button_number,
                event.button_type,
                event.gesture,
                self._capture_action,
            )
            bindings = (*config.bindings, binding)
            if self._bindings_conflict(bindings):
                errors["base"] = "binding_already_assigned"
            else:
                return await self._async_save_bindings(bindings)
        return self.async_show_form(
            step_id="learn_candidate",
            data_schema=vol.Schema(
                {
                    vol.Required("candidate", default="0"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=str(index),
                                    label=(
                                        f"{self._keypad_label(event.serial)} — "
                                        f"{event.button_type} — {event.gesture}"
                                    ),
                                )
                                for index, event in enumerate(self._capture_candidates)
                            ]
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_calibrate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the controls used for guided calibration."""
        config = self._require_working_config()
        methods = {CALIBRATION_SOURCE_GUIDED_HA: "Home Assistant controls"}
        if self._pico_calibration_bindings() is not None:
            methods[CALIBRATION_SOURCE_GUIDED_PICO] = "Mapped Pico actions"
        if user_input is not None:
            self._calibration_method = user_input["method"]
            if error := await self._async_ensure_setup():
                return self.async_show_form(
                    step_id="calibrate",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                "method", default=self._calibration_method
                            ): vol.In(methods)
                        }
                    ),
                    errors={"base": error},
                    description_placeholders={
                        "zone": self._cover_label(config.zone_id)
                    },
                )
            self._open_samples = []
            self._close_samples = []
            self._calibration_initial_complete = False
            self._calibration_plan = [
                (ExternalAction.CLOSE, False),
                (ExternalAction.OPEN, True),
                (ExternalAction.CLOSE, True),
                (ExternalAction.OPEN, True),
                (ExternalAction.CLOSE, True),
            ]
            return await self._async_advance_calibration()
        return self.async_show_form(
            step_id="calibrate",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "method", default=CALIBRATION_SOURCE_GUIDED_HA
                    ): vol.In(methods)
                }
            ),
            description_placeholders={"zone": self._cover_label(config.zone_id)},
        )

    async def async_step_calibration_ha_start(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start one HA-controlled calibration or repositioning leg."""
        assert self._session is not None
        assert self._calibration_action is not None
        if user_input is not None:
            try:
                await self._session.async_start_home_assistant_movement(
                    self._calibration_action
                )
            except Exception:  # noqa: BLE001 - translate bridge failures for the wizard
                return self.async_show_form(
                    step_id="calibration_ha_start",
                    data_schema=self._confirmation_schema("start"),
                    errors={"base": "command_failed"},
                )
            return await self.async_step_calibration_ha_finish()
        return self.async_show_form(
            step_id="calibration_ha_start",
            data_schema=self._confirmation_schema("start"),
            description_placeholders=self._calibration_placeholders(),
        )

    async def async_step_calibration_ha_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stop the current HA-controlled leg at the physical endpoint."""
        assert self._session is not None
        if user_input is not None:
            try:
                duration = await self._session.async_finish_home_assistant_movement()
            except Exception:  # noqa: BLE001 - keep Stop failure inside the wizard
                return self.async_show_form(
                    step_id="calibration_ha_finish",
                    data_schema=self._confirmation_schema("reached_endpoint"),
                    errors={"base": "stop_failed"},
                )
            self._record_calibration_leg(duration)
            return await self._async_advance_calibration()
        return self.async_show_form(
            step_id="calibration_ha_finish",
            data_schema=self._confirmation_schema("reached_endpoint"),
            description_placeholders=self._calibration_placeholders(),
        )

    async def async_step_calibration_pico_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for mapped Pico direction and Stop actions."""
        assert self._session is not None
        assert self._calibration_action is not None
        bindings = self._pico_calibration_bindings()
        assert bindings is not None
        if self._progress_task is None:
            direction = bindings[self._calibration_action]
            stop = bindings[ExternalAction.STOP]
            self._progress_task = self.hass.async_create_task(
                self._session.async_measure_pico_movement(
                    direction.event_key, stop.event_key
                )
            )
        if not self._progress_task.done():
            return self.async_show_progress(
                step_id="calibration_pico_progress",
                progress_action="calibrate_pico",
                progress_task=self._progress_task,
                description_placeholders=self._calibration_placeholders(),
            )
        return self.async_show_progress_done(next_step_id="calibration_pico_result")

    async def async_step_calibration_pico_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Record a completed Pico calibration leg."""
        assert self._progress_task is not None
        try:
            duration = await self._progress_task
        except SetupCaptureMismatch, SetupCaptureTimeout:
            self._progress_task = None
            return self.async_show_form(
                step_id="calibration_pico_retry",
                data_schema=self._confirmation_schema("retry"),
                errors={"base": "calibration_capture_failed"},
                description_placeholders=self._calibration_placeholders(),
            )
        self._progress_task = None
        self._record_calibration_leg(duration)
        return await self._async_advance_calibration()

    async def async_step_calibration_pico_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Retry an incomplete Pico calibration leg."""
        if user_input is not None:
            return await self.async_step_calibration_pico_progress()
        return self.async_show_form(
            step_id="calibration_pico_retry",
            data_schema=self._confirmation_schema("retry"),
            description_placeholders=self._calibration_placeholders(),
        )

    async def async_step_calibration_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm calibrated timing and independent endpoint guards."""
        config = self._require_working_config()
        open_result = aggregate_calibration_samples(self._open_samples)
        close_result = aggregate_calibration_samples(self._close_samples)
        if user_input is not None:
            calibrated = replace(
                config,
                travel_times=TravelTimes(
                    open_result.seconds,
                    close_result.seconds,
                    user_input[CONF_OPEN_GUARD_SECONDS],
                    user_input[CONF_CLOSE_GUARD_SECONDS],
                ),
                calibration=CalibrationMetadata(
                    self._calibration_method,
                    tuple(round(sample, 2) for sample in self._open_samples),
                    tuple(round(sample, 2) for sample in self._close_samples),
                ),
            )
            return await self._async_save_zone(config.zone_id, calibrated)
        return self.async_show_form(
            step_id="calibration_summary",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_OPEN_GUARD_SECONDS,
                        default=config.travel_times.open_guard_seconds,
                    ): self._guard_selector(),
                    vol.Required(
                        CONF_CLOSE_GUARD_SECONDS,
                        default=config.travel_times.close_guard_seconds,
                    ): self._guard_selector(),
                }
            ),
            description_placeholders={
                "open_samples": self._format_samples(self._open_samples),
                "close_samples": self._format_samples(self._close_samples),
                "open_time": str(open_result.seconds),
                "close_time": str(close_result.seconds),
                "open_spread": str(open_result.spread),
                "close_spread": str(close_result.spread),
            },
        )

    async def async_step_manual_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit expert timing values without moving the motor."""
        config = self._require_working_config()
        if user_input is not None:
            manual = replace(
                config,
                device_class=CoverDeviceClass(user_input[CONF_DEVICE_CLASS]),
                travel_times=TravelTimes(
                    user_input[CONF_OPEN_TRAVEL_SECONDS],
                    user_input[CONF_CLOSE_TRAVEL_SECONDS],
                    user_input[CONF_OPEN_GUARD_SECONDS],
                    user_input[CONF_CLOSE_GUARD_SECONDS],
                ),
                calibration=CalibrationMetadata(CALIBRATION_SOURCE_MANUAL),
            )
            return await self._async_save_zone(config.zone_id, manual)
        return self.async_show_form(
            step_id="manual_timing",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_CLASS, default=config.device_class
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[item.value for item in SUPPORTED_DEVICE_CLASSES]
                        )
                    ),
                    vol.Required(
                        CONF_OPEN_TRAVEL_SECONDS,
                        default=config.travel_times.open_seconds,
                    ): self._travel_selector(),
                    vol.Required(
                        CONF_CLOSE_TRAVEL_SECONDS,
                        default=config.travel_times.close_seconds,
                    ): self._travel_selector(),
                    vol.Required(
                        CONF_OPEN_GUARD_SECONDS,
                        default=config.travel_times.open_guard_seconds,
                    ): self._guard_selector(),
                    vol.Required(
                        CONF_CLOSE_GUARD_SECONDS,
                        default=config.travel_times.close_guard_seconds,
                    ): self._guard_selector(),
                }
            ),
            description_placeholders={"zone": self._cover_label(config.zone_id)},
        )

    async def async_step_cover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Retain the previous direct-timing API step."""
        return await self.async_step_manual_timing(user_input)

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove estimation while retaining the command-only cover."""
        config = self._require_working_config()
        if user_input is not None:
            return await self._async_save_zone(config.zone_id, None)
        return self.async_show_form(
            step_id="remove",
            data_schema=self._confirmation_schema("remove"),
            description_placeholders={"zone": self._cover_label(config.zone_id)},
        )

    async def _async_advance_calibration(self) -> ConfigFlowResult:
        if not self._calibration_plan:
            if not self._calibration_initial_complete:
                self._calibration_initial_complete = True
                need_open = self._needs_third_sample(self._open_samples)
                need_close = self._needs_third_sample(self._close_samples)
                if need_open:
                    self._calibration_plan.append((ExternalAction.OPEN, True))
                if need_close:
                    if not need_open:
                        self._calibration_plan.append((ExternalAction.OPEN, False))
                    self._calibration_plan.append((ExternalAction.CLOSE, True))
                if self._calibration_plan:
                    return await self._async_advance_calibration()
            return await self.async_step_calibration_summary()

        self._calibration_action, self._calibration_counted = (
            self._calibration_plan.pop(0)
        )
        if self._calibration_method == CALIBRATION_SOURCE_GUIDED_PICO:
            return await self.async_step_calibration_pico_progress()
        return await self.async_step_calibration_ha_start()

    def _record_calibration_leg(self, duration: float) -> None:
        assert self._calibration_action is not None
        if not self._calibration_counted:
            return
        samples = (
            self._open_samples
            if self._calibration_action is ExternalAction.OPEN
            else self._close_samples
        )
        samples.append(duration)

    @staticmethod
    def _needs_third_sample(samples: list[float]) -> bool:
        try:
            aggregate_calibration_samples(samples)
        except ThirdSampleRequired:
            return True
        return False

    async def _async_save_bindings(
        self, bindings: tuple[PicoBinding, ...]
    ) -> ConfigFlowResult:
        config = replace(self._require_working_config(), bindings=bindings)
        return await self._async_save_zone(config.zone_id, config)

    async def _async_save_zone(
        self, zone_id: str, config: EstimatedCoverConfig | None
    ) -> ConfigFlowResult:
        """Persist one zone atomically while preserving unrelated options."""
        await self._async_release_setup()
        options = dict(self.config_entry.options)
        raw_covers = options.get(CONF_ESTIMATED_COVERS, {})
        covers = dict(raw_covers) if isinstance(raw_covers, dict) else {}
        if config is None:
            covers.pop(zone_id, None)
        else:
            covers[zone_id] = config.as_dict()
        options[CONF_ESTIMATED_COVERS] = covers
        return self.async_create_entry(title="", data=options)

    async def _async_ensure_setup(self) -> str | None:
        if self._session is not None:
            return None
        assert self._zone_id is not None
        try:
            self._session = await self.config_entry.runtime_data.open_close_stop_manager.async_begin_setup(
                self._zone_id
            )
        except SetupSessionBusyError:
            return "setup_in_progress"
        except Exception:
            _LOGGER.exception("Unable to start OpenCloseStop cover setup")
            return "setup_failed"
        return None

    async def _async_release_setup(self) -> None:
        if self._session is None:
            return
        session = self._session
        self._session = None
        await self.config_entry.runtime_data.open_close_stop_manager.async_end_setup(
            session
        )

    def _bindings_conflict(self, bindings: tuple[PicoBinding, ...]) -> bool:
        keys = [binding.event_key for binding in bindings]
        if len(set(keys)) != len(keys):
            return True
        claimed = {
            binding.event_key
            for zone_id, config in parse_estimated_cover_configs(
                self.config_entry.options
            ).items()
            if zone_id != self._zone_id
            for binding in config.bindings
        }
        return bool(set(keys) & claimed)

    def _pico_calibration_bindings(
        self,
    ) -> dict[ExternalAction, PicoBinding] | None:
        by_effect: dict[ExternalAction, PicoBinding] = {}
        for binding in self._require_working_config().bindings:
            by_effect.setdefault(binding.effect, binding)
        if set(by_effect) == set(ExternalAction):
            return by_effect
        return None

    def _calibration_placeholders(self) -> dict[str, str]:
        assert self._calibration_action is not None
        samples = (
            self._open_samples
            if self._calibration_action is ExternalAction.OPEN
            else self._close_samples
        )
        return {
            "action": self._calibration_action,
            "purpose": "sample" if self._calibration_counted else "positioning",
            "sample": str(len(samples) + 1 if self._calibration_counted else 0),
        }

    def _require_working_config(self) -> EstimatedCoverConfig:
        assert self._working_config is not None
        return self._working_config

    @staticmethod
    def _is_standard_binding(binding: PicoBinding) -> bool:
        return binding.gesture == ACTION_PRESS and (
            binding.leap_button_number,
            binding.effect,
        ) in {
            (3, ExternalAction.OPEN),
            (4, ExternalAction.CLOSE),
            (1, ExternalAction.STOP),
        }

    @classmethod
    def _standard_pico_serials(cls, bindings: tuple[PicoBinding, ...]) -> list[str]:
        grouped: dict[str, set[tuple[int, ExternalAction]]] = {}
        for binding in bindings:
            if cls._is_standard_binding(binding):
                grouped.setdefault(binding.keypad_serial, set()).add(
                    (binding.leap_button_number, binding.effect)
                )
        required = {
            (3, ExternalAction.OPEN),
            (4, ExternalAction.CLOSE),
            (1, ExternalAction.STOP),
        }
        return [serial for serial, actions in grouped.items() if actions == required]

    def _gesture_options(self) -> list[str]:
        gestures = [ACTION_PRESS, ACTION_RELEASE, ACTION_MULTITAP]
        if (
            self.config_entry.runtime_data.bridge_device["type"]
            in BRIDGE_DEVICE_TYPES_WITH_LONG_HOLD
        ):
            gestures.append(ACTION_LONG_PRESS)
        return gestures

    def _keypad_options(self) -> list[selector.SelectOptionDict]:
        return [
            selector.SelectOptionDict(
                value=str(keypad[LUTRON_KEYPAD_SERIAL]),
                label=self._keypad_label(str(keypad[LUTRON_KEYPAD_SERIAL])),
            )
            for keypad in self.config_entry.runtime_data.keypad_data.keypads.values()
        ]

    def _button_options(self, serial: str) -> list[selector.SelectOptionDict]:
        keypad = self._keypad(serial)
        buttons = self.config_entry.runtime_data.keypad_data.buttons
        return [
            selector.SelectOptionDict(
                value=str(buttons[button_id][LUTRON_BUTTON_LEAP_BUTTON_NUMBER]),
                label=buttons[button_id][LUTRON_BUTTON_BUTTON_NAME],
            )
            for button_id in keypad[LUTRON_KEYPAD_BUTTONS]
        ]

    def _button_name(self, serial: str, leap_button_number: int) -> str:
        keypad = self._keypad(serial)
        buttons = self.config_entry.runtime_data.keypad_data.buttons
        return next(
            buttons[button_id][LUTRON_BUTTON_BUTTON_NAME]
            for button_id in keypad[LUTRON_KEYPAD_BUTTONS]
            if buttons[button_id][LUTRON_BUTTON_LEAP_BUTTON_NUMBER]
            == leap_button_number
        )

    def _keypad(self, serial: str):
        return next(
            keypad
            for keypad in self.config_entry.runtime_data.keypad_data.keypads.values()
            if str(keypad[LUTRON_KEYPAD_SERIAL]) == serial
        )

    def _keypad_label(self, serial: str) -> str:
        keypad = self._keypad(serial)
        return f"{keypad[LUTRON_KEYPAD_AREA_NAME]} {keypad[LUTRON_KEYPAD_NAME]}"

    def _binding_options(
        self, bindings: tuple[PicoBinding, ...]
    ) -> list[selector.SelectOptionDict]:
        return [
            selector.SelectOptionDict(
                value=str(index),
                label=(
                    f"{self._keypad_label(binding.keypad_serial)} — "
                    f"{binding.button_type} {binding.gesture} → {binding.effect}"
                ),
            )
            for index, binding in enumerate(bindings)
        ]

    @staticmethod
    def _confirmation_schema(field: str) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(field, default=False): selector.BooleanSelector(),
            }
        )

    @staticmethod
    def _travel_selector() -> selector.NumberSelector:
        return selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=MIN_TRAVEL_SECONDS,
                max=MAX_TRAVEL_SECONDS,
                step=0.1,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        )

    @staticmethod
    def _guard_selector() -> selector.NumberSelector:
        return selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=MAX_GUARD_SECONDS,
                step=0.1,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        )

    @staticmethod
    def _format_samples(samples: list[float]) -> str:
        return ", ".join(f"{sample:.2f}s" for sample in samples)

    @override
    def async_remove(self) -> None:
        """Cancel progress and schedule motor-safe setup cleanup."""
        if self._progress_task is not None and not self._progress_task.done():
            self._progress_task.cancel()
        if self._session is not None:
            self.hass.async_create_task(self._async_release_setup())

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
