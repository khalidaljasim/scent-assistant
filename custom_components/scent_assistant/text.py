"""Text entities for writable Scent Marketing AK V3 metadata."""
from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DeviceType
from .device import ScentDiffuserDevice


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    """Register AK V3 text controls only for AK devices."""
    device: ScentDiffuserDevice = hass.data[DOMAIN][entry.entry_id]
    if device.device_type == DeviceType.SCENT_MARKETING_AK:
        async_add_entities([
            AKV3DeviceNameText(device), AKV3DeviceLabelText(device), AKV3OilNameText(device),
        ])


class _AKV3TextEntity(TextEntity):
    _attr_has_entity_name = True

    def __init__(self, device: ScentDiffuserDevice, suffix: str, state_field: str) -> None:
        self._device = device
        self._state_field = state_field
        self._attr_unique_id = f"{device.unique_id}_{suffix}"
        self._attr_device_info = device.device_info
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return (
            self._device.ak_v3_read_available(self._state_field)
            and self._state_field in self._device.state.ak_v3_metadata_available
        )


class AKV3DeviceNameText(_AKV3TextEntity):
    _attr_name = "Device name"
    _attr_icon = "mdi:rename-box"

    def __init__(self, device: ScentDiffuserDevice) -> None:
        super().__init__(device, "device_name", "device_name")

    @property
    def native_value(self) -> str | None:
        name = self._device.state.device_name
        prefix = self._device.state.device_name_append_prefix.decode("utf-8", errors="replace")
        return name.removeprefix(prefix) if name and prefix else name

    async def async_set_value(self, value: str) -> None:
        await self._device.async_set_ak_v3_device_name(value)


class AKV3DeviceLabelText(_AKV3TextEntity):
    _attr_name = "Device label"
    _attr_icon = "mdi:label"

    def __init__(self, device: ScentDiffuserDevice) -> None:
        super().__init__(device, "device_label", "device_label")

    @property
    def native_value(self) -> str | None:
        return self._device.state.device_label

    async def async_set_value(self, value: str) -> None:
        await self._device.async_set_ak_v3_device_label(value)


class AKV3OilNameText(_AKV3TextEntity):
    _attr_name = "Fragrance name"
    _attr_icon = "mdi:bottle-tonic-plus"

    def __init__(self, device: ScentDiffuserDevice) -> None:
        super().__init__(device, "oil_name", "oil_names")

    @property
    def native_value(self) -> str | None:
        return self._device.state.oil_names[0] or None if self._device.state.oil_names else None

    async def async_set_value(self, value: str) -> None:
        await self._device.async_set_ak_v3_oil_name(value)
