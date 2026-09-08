"""Select entities for Scent Diffuser."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DeviceType
from .device import ScentDiffuserDevice

MODE_CUSTOM = "Custom"
MODE_FIXED = "Fixed"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities."""
    device: ScentDiffuserDevice = hass.data[DOMAIN][entry.entry_id]

    entities: list[SelectEntity] = []
    # AK V3 records are physical per-slot schedules. Do not expose a global
    # mode selector that could imply a write to an unspecified physical slot.
    if device.device_type == DeviceType.SCENT_MARKETING_AK:
        entities.extend(AKV3ScheduleModeSelect(device, entry, 1, slot) for slot in range(1, 6))

    async_add_entities(entities)


class AKV3ScheduleModeSelect(SelectEntity):
    """Explicit mode selector for one physical AK V3 schedule slot."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:tune-variant"

    def __init__(self, device, entry, endpoint: int, slot: int) -> None:
        self._device, self._endpoint, self._slot = device, endpoint, slot
        self._attr_name = f"Schedule {slot} mode"
        self._attr_unique_id = f"{device.unique_id}_schedule_{slot}_mode"
        self._attr_device_info = device.device_info
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def _schedule(self):
        return self._device.state.ak_v3_schedules.get((self._endpoint, self._slot))

    @property
    def current_option(self) -> str | None:
        if self._schedule is None:
            return None
        return MODE_CUSTOM if self._schedule.mode == 1 else MODE_FIXED

    @property
    def options(self) -> list[str]:
        """Expose Custom only with confirmed device support and usable limits."""
        if self._device.supports_ak_v3_custom_mode:
            return [MODE_FIXED, MODE_CUSTOM]
        return [MODE_FIXED]

    @property
    def available(self) -> bool:
        return (
            self._device.ak_v3_read_available("schedules")
            and self._schedule is not None
            and (self._schedule.mode == 0 or self._device.supports_ak_v3_custom_mode)
        )

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ValueError(f"Unsupported AK V3 schedule mode: {option}")
        await self._device.async_update_ak_v3_slot(
            self._endpoint, self._slot, mode=1 if option == MODE_CUSTOM else 0
        )
