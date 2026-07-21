"""Persist incomplete estimated-cover onboarding safely."""

from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CONF_ESTIMATED_COVERS, DOMAIN
from .estimated_cover import EstimatedCoverConfig, parse_estimated_cover_configs

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1


@dataclass(frozen=True, slots=True)
class EstimatedCoverDraft:
    """Represent the safe checkpoints of one incomplete cover setup."""

    config: EstimatedCoverConfig
    controls_configured: bool
    timing_configured: bool

    def __post_init__(self) -> None:
        """Reject a completed timing checkpoint without timing provenance."""
        if self.timing_configured and self.config.calibration is None:
            raise ValueError("a completed timing draft needs calibration metadata")

    def as_dict(self) -> dict[str, Any]:
        """Serialize the draft without exposing it as runtime configuration."""
        return {
            "zone_id": self.config.zone_id,
            "config": self.config.as_dict(),
            "controls_configured": self.controls_configured,
            "timing_configured": self.timing_configured,
        }


class EstimatedCoverDraftStore:
    """Store one resumable estimated-cover draft per Lutron config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize a private, atomic Home Assistant storage record."""
        self._hass = hass
        self._key = f"{DOMAIN}.estimated_cover_draft_{entry_id}"
        self._store = self._create_store()

    def _create_store(self) -> Store[dict[str, Any]]:
        """Create a clean Store handle for the fixed config-entry key."""
        return Store[dict[str, Any]](
            self._hass,
            _STORAGE_VERSION,
            self._key,
            private=True,
            atomic_writes=True,
        )

    async def async_load(self) -> EstimatedCoverDraft | None:
        """Load and validate a draft, discarding malformed storage."""
        raw = await self._store.async_load()
        if raw is None:
            return None
        try:
            draft = self._parse(raw)
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("Discarding invalid estimated-cover setup draft: %s", err)
            await self.async_remove()
            return None
        return draft

    async def async_save(self, draft: EstimatedCoverDraft) -> None:
        """Atomically save the latest safe onboarding checkpoint."""
        await self._store.async_save(draft.as_dict())

    async def async_remove(self) -> None:
        """Delete any incomplete onboarding checkpoint."""
        await self._store.async_remove()
        # Store retains its last in-memory write after deleting the file. A fresh
        # handle prevents a same-process load from resurrecting a discarded draft.
        self._store = self._create_store()

    @staticmethod
    def _parse(raw: Mapping[str, Any]) -> EstimatedCoverDraft:
        """Validate persisted data through the runtime option parser."""
        zone_id = str(raw["zone_id"])
        parsed = parse_estimated_cover_configs(
            {CONF_ESTIMATED_COVERS: {zone_id: raw["config"]}}
        )
        if config := parsed.get(zone_id):
            controls_configured = raw["controls_configured"]
            timing_configured = raw["timing_configured"]
            if not isinstance(controls_configured, bool) or not isinstance(
                timing_configured, bool
            ):
                raise TypeError("draft completion flags must be booleans")
            return EstimatedCoverDraft(config, controls_configured, timing_configured)
        raise ValueError("draft cover configuration is invalid")
