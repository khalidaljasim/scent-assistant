"""Time entities for Scent Diffuser."""
from __future__ import annotations

import logging
from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
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
    """Set up time entities."""
    device: ScentDiffuserDevice = hass.data[DOMAIN][entry.entry_id]
    if device.device_type == DeviceType.SCENTIMENT:
        return

    entities = [
        DiffuserStartTime(device, entry), DiffuserEndTime(device, entry),
    ]
    entities.extend(
        entity
        for slot in range(1, 6)
        for entity in (
            AKV3ScheduleTime(device, entry, 1, slot, True),
            AKV3ScheduleTime(device, entry, 1, slot, False),
        )
    )
    async_add_entities(entities)


class DiffuserStartTime(TimeEntity):
    """Start time for the daily spray schedule."""

    _attr_has_entity_name = True
    _attr_name = "Start Time"
    _attr_icon = "mdi:clock-start"

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_start_time"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
        }
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is None:
            return
        self.async_write_ha_state()

    @property
    def native_value(self) -> time | None:
        return time(self._device.state.start_hour, self._device.state.start_minute)

    @property
    def available(self) -> bool:
        return self._device.available and not self._device.protocol_is_v3

    async def async_set_value(self, value: time) -> None:
        """Set the start time and write schedule to device."""
        self._device.state.start_hour = value.hour
        self._device.state.start_minute = value.minute
        await self._device.set_schedule(
            weekday_mask=0x7F,  # all days
            start_hour=value.hour,
            start_minute=value.minute,
            end_hour=self._device.state.end_hour,
            end_minute=self._device.state.end_minute,
            work_seconds=self._device.state.work_seconds or 10,
            pause_seconds=self._device.state.pause_seconds or 120,
        )


class DiffuserEndTime(TimeEntity):
    """End time for the daily spray schedule."""

    _attr_has_entity_name = True
    _attr_name = "End Time"
    _attr_icon = "mdi:clock-end"

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_end_time"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
        }
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is None:
            return
        self.async_write_ha_state()

    @property
    def native_value(self) -> time | None:
        return time(self._device.state.end_hour, self._device.state.end_minute)

    @property
    def available(self) -> bool:
        return self._device.available and not self._device.protocol_is_v3

    async def async_set_value(self, value: time) -> None:
        """Set the end time and write schedule to device."""
        self._device.state.end_hour = value.hour
        self._device.state.end_minute = value.minute
        await self._device.set_schedule(
            weekday_mask=0x7F,  # all days
            start_hour=self._device.state.start_hour,
            start_minute=self._device.state.start_minute,
            end_hour=value.hour,
            end_minute=value.minute,
            work_seconds=self._device.state.work_seconds or 10,
            pause_seconds=self._device.state.pause_seconds or 120,
        )


class AKV3ScheduleTime(TimeEntity):
    """Explicit endpoint+slot start or end time for an AK V3 schedule."""

    _attr_has_entity_name = True

    def __init__(self, device, entry, endpoint: int, slot: int, is_start: bool) -> None:
        self._device, self._endpoint, self._slot, self._is_start = device, endpoint, slot, is_start
        label = "start" if is_start else "end"
        self._attr_name = f"Schedule {slot} {label} time"
        self._attr_icon = "mdi:clock-start" if is_start else "mdi:clock-end"
        self._attr_unique_id = f"{device.unique_id}_schedule_{slot}_{label}_time"
        self._attr_device_info = device.device_info
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def _schedule(self):
        return self._device.state.ak_v3_schedules.get((self._endpoint, self._slot))

    @property
    def native_value(self) -> time | None:
        if self._schedule is None:
            return None
        return time(
            self._schedule.start_hour if self._is_start else self._schedule.end_hour,
            self._schedule.start_minute if self._is_start else self._schedule.end_minute,
        )

    @property
    def available(self) -> bool:
        return self._device.ak_v3_read_available("schedules") and self._schedule is not None

    async def async_set_value(self, value: time) -> None:
        fields = (
            {"start_hour": value.hour, "start_minute": value.minute}
            if self._is_start
            else {"end_hour": value.hour, "end_minute": value.minute}
        )
        await self._device.async_update_ak_v3_slot(self._endpoint, self._slot, **fields)
