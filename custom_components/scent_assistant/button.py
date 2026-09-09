"""Button entities for Scent Diffuser."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DeviceType
from .device import ScentDiffuserDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities."""
    device: ScentDiffuserDevice = hass.data[DOMAIN][entry.entry_id]
    if device.device_type == DeviceType.SCENTIMENT:
        return

    entities: list[ButtonEntity] = [
        TimeSyncButton(device, entry),
        RefreshDiffuserStateButton(device, entry),
    ]
    # Momentary diffusion is power-on + delayed power-off, which only
    # makes sense on families where power is a plain on/off (Aroma-Link).
    if device.device_type == DeviceType.AROMA_LINK:
        entities.append(MomentaryDiffuseButton(device, entry))
    async_add_entities(entities)


class MomentaryDiffuseButton(ButtonEntity):
    """One-shot diffusion: power on, auto-off after a set duration.

    The Aroma-Link protocol has no native momentary command (checked
    against the decompiled official app), so the device manager emulates
    it. The run time comes from the Momentary Duration number entity.
    """

    _attr_has_entity_name = True
    _attr_name = "Diffuse Now"
    _attr_icon = "mdi:spray"

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_momentary"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
        }

    @property
    def available(self) -> bool:
        return self._device.available

    async def async_press(self) -> None:
        """Start a momentary diffusion run."""
        if not await self._device.momentary_diffuse():
            _LOGGER.warning(
                "Momentary diffusion failed to start on %s", self._device.name
            )


class TimeSyncButton(ButtonEntity):
    """Button to manually sync the device clock."""

    _attr_has_entity_name = True
    _attr_name = "Sync Time"
    _attr_icon = "mdi:clock-check"

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_time_sync"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
        }

    @property
    def available(self) -> bool:
        return self._device.connection_mode == "ble"

    async def async_press(self) -> None:
        """Sync the device clock to current local time."""
        success = await self._device.sync_time()
        if success:
            _LOGGER.info("Time synced to %s", self._device.name)
        else:
            _LOGGER.warning("Time sync failed for %s", self._device.name)


class RefreshDiffuserStateButton(ButtonEntity):
    """Run the isolated, read-only AK V3 state refresh."""

    _attr_has_entity_name = True
    _attr_name = "Refresh Diffuser State"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_refresh_state"
        self._attr_device_info = {"identifiers": {(DOMAIN, device.unique_id)}}

    async def async_added_to_hass(self) -> None:
        """Subscribe only after Home Assistant assigned the entity identity."""
        await super().async_added_to_hass()
        self._device.register_state_callback(self._on_state_update)

    async def async_will_remove_from_hass(self) -> None:
        """Leave no callback behind when the entity or entry is removed."""
        self._device.unregister_state_callback(self._on_state_update)
        await super().async_will_remove_from_hass()

    def _on_state_update(self) -> None:
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # Keep this diagnostic button stable while another AK V3 action owns
        # the BLE session. async_press retains the runtime busy guard.
        return (
            self._device.is_ak_protocol
            and self._device.protocol_is_v3
            and bool(self._device._ble_address)
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return self._device.ak_v3_manual_refresh_diagnostics

    async def async_press(self) -> None:
        await self._device.async_refresh_ak_v3_state()
