"""Number entities for Scent Diffuser."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
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
    """Set up number entities."""
    device: ScentDiffuserDevice = hass.data[DOMAIN][entry.entry_id]

    if device.device_type == DeviceType.SCENTIMENT:
        async_add_entities([ScentimentLevelNumber(device, entry)])
        return

    entities: list[NumberEntity] = [WorkDurationNumber(device, entry), PauseDurationNumber(device, entry)]
    if device.device_type == DeviceType.SCENT_MARKETING_AK:
        entities.append(ScentMarketingIntensityNumber(device, entry))
        entities.extend([
            AKV3OilCapacityNumber(device, entry),
            AKV3OilRemainingNumber(device, entry),
            AKV3OilFlowNumber(device, entry),
        ])
        entities.extend(AKV3ScheduleIntensityNumber(device, entry, 1, slot) for slot in range(1, 6))
        entities.extend(AKV3ScheduleDurationNumber(device, entry, 1, slot, "work") for slot in range(1, 6))
        entities.extend(AKV3ScheduleDurationNumber(device, entry, 1, slot, "pause") for slot in range(1, 6))
    if device.device_type == DeviceType.AROMA_LINK:
        entities.append(MomentaryDurationNumber(device, entry))
    async_add_entities(entities)


class WorkDurationNumber(NumberEntity):
    """Spray work duration in seconds."""

    _attr_has_entity_name = True
    _attr_name = "Work Duration"
    _attr_icon = "mdi:timer"
    _attr_native_unit_of_measurement = "s"
    _attr_native_min_value = 5
    _attr_native_max_value = 600
    _attr_native_step = 5
    _attr_mode = NumberMode.BOX

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_work_duration"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
        }
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is None:
            return
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return self._device.state.work_seconds

    @property
    def available(self) -> bool:
        if self._device.device_type == DeviceType.SCENT_MARKETING_AK and self._device.protocol_is_v3:
            return self._device.ak_v3_read_available("schedules") and self._device.state.intensity is not None
        return self._device.available and (
            not self._device.protocol_is_v3 or self._device.state.intensity is not None
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._device.set_work_duration(int(value))


class PauseDurationNumber(NumberEntity):
    """Pause duration between sprays in seconds."""

    _attr_has_entity_name = True
    _attr_name = "Pause Duration"
    _attr_icon = "mdi:timer-pause"
    _attr_native_unit_of_measurement = "s"
    _attr_native_min_value = 15
    _attr_native_max_value = 3600
    _attr_native_step = 5
    _attr_mode = NumberMode.BOX

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_pause_duration"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
        }
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is None:
            return
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return self._device.state.pause_seconds

    @property
    def available(self) -> bool:
        if self._device.device_type == DeviceType.SCENT_MARKETING_AK and self._device.protocol_is_v3:
            return self._device.ak_v3_read_available("schedules") and self._device.state.intensity is not None
        return self._device.available and (
            not self._device.protocol_is_v3 or self._device.state.intensity is not None
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._device.set_pause_duration(int(value))


class MomentaryDurationNumber(NumberEntity):
    """Run time for the Diffuse Now button (Aroma-Link).

    Held on the device manager only — resets to the default after an HA
    restart. Configuration entity, so it lands in the device's
    "Configuration" section rather than next to the live controls.
    """

    _attr_has_entity_name = True
    _attr_name = "Momentary Duration"
    _attr_icon = "mdi:timer-cog"
    _attr_native_unit_of_measurement = "s"
    _attr_native_min_value = 5
    _attr_native_max_value = 600
    _attr_native_step = 5
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_momentary_duration"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
        }

    @property
    def native_value(self) -> float:
        return self._device.momentary_seconds

    @property
    def available(self) -> bool:
        return self._device.available and not self._device.protocol_is_v3

    async def async_set_native_value(self, value: float) -> None:
        self._device.momentary_seconds = int(value)
        if self.hass is not None:
            self.async_write_ha_state()


class _AKV3OilNumber(NumberEntity):
    """Base for one-aroma AK V3 oil-table editors."""

    _attr_has_entity_name = True

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry, suffix: str) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_{suffix}"
        self._attr_device_info = device.device_info
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        state = self._device.state
        return (
            self._device.ak_v3_read_available(
                "oil_status", "oil_max_ml", "oil_current_ml", "oil_consumption_mlh"
            )
            and state.oil_status_byte is not None
            and state.oil_max_ml is not None
            and state.oil_current_ml is not None
            and state.oil_consumption_mlh is not None
        )


class AKV3OilCapacityNumber(_AKV3OilNumber):
    _attr_name = "Oil capacity setpoint"
    _attr_icon = "mdi:cup"
    _attr_native_unit_of_measurement = "mL"
    _attr_native_min_value = 0
    _attr_native_max_value = 65535
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        super().__init__(device, entry, "oil_capacity_setpoint")

    @property
    def native_value(self) -> float | None:
        return self._device.state.oil_max_ml

    async def async_set_native_value(self, value: float) -> None:
        await self._device.async_set_ak_v3_oil(total_ml=int(value))


class AKV3OilRemainingNumber(_AKV3OilNumber):
    _attr_name = "Oil remaining setpoint"
    _attr_icon = "mdi:cup-water"
    _attr_native_unit_of_measurement = "mL"
    _attr_native_min_value = 0
    _attr_native_max_value = 65535
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        super().__init__(device, entry, "oil_remaining_setpoint")

    @property
    def native_value(self) -> float | None:
        return self._device.state.oil_current_ml

    async def async_set_native_value(self, value: float) -> None:
        await self._device.async_set_ak_v3_oil(remaining_ml=int(value))


class AKV3OilFlowNumber(_AKV3OilNumber):
    _attr_name = "Oil flow rate"
    _attr_icon = "mdi:speedometer"
    _attr_native_unit_of_measurement = "mL/h"
    _attr_native_min_value = 0
    _attr_native_max_value = 655.35
    _attr_native_step = 0.01
    _attr_mode = NumberMode.BOX

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        super().__init__(device, entry, "oil_flow_rate")

    @property
    def native_value(self) -> float | None:
        return self._device.state.oil_consumption_mlh

    async def async_set_native_value(self, value: float) -> None:
        await self._device.async_set_ak_v3_oil(flow_mlh=value)


class ScentMarketingIntensityNumber(NumberEntity):
    """Spray intensity for Scent Marketing AK devices.

    Range goes up to 20 (V3 firmware ceiling); V2 devices accept 0-10 and
    the device manager clamps on send. Intensity isn't a standalone BLE
    write — it's the LL field in the AK schedule frame — so changing this
    re-applies the current schedule with the new level.
    """

    _attr_has_entity_name = True
    _attr_name = "Intensity"
    _attr_icon = "mdi:speedometer"
    _attr_native_min_value = 0
    _attr_native_max_value = 20
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_intensity"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
        }
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is None:
            return
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._device.state.intensity

    @property
    def available(self) -> bool:
        if self._device.device_type == DeviceType.SCENT_MARKETING_AK and self._device.protocol_is_v3:
            return self._device.ak_v3_read_available("schedules") and self._device.state.intensity is not None
        return self._device.available and self._device.state.intensity is not None

    async def async_set_native_value(self, value: float) -> None:
        await self._device.set_intensity(int(value))


class AKV3ScheduleIntensityNumber(NumberEntity):
    """Explicit read-before-write intensity control for one AK V3 slot."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:speedometer"
    _attr_native_min_value = 0
    _attr_native_max_value = 20
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry, endpoint: int, slot: int) -> None:
        self._device, self._endpoint, self._slot = device, endpoint, slot
        self._attr_name = f"Schedule {slot} intensity"
        self._attr_unique_id = f"{device.unique_id}_schedule_{slot}_intensity"
        self._attr_device_info = {"identifiers": {(DOMAIN, device.unique_id)}}
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        schedule = self._device.state.ak_v3_schedules.get((self._endpoint, self._slot))
        return schedule.intensity if schedule else None

    @property
    def available(self) -> bool:
        return self._device.ak_v3_read_available("schedules") and (self._endpoint, self._slot) in self._device.state.ak_v3_schedules

    async def async_set_native_value(self, value: float) -> None:
        await self._device.async_update_ak_v3_slot(self._endpoint, self._slot, intensity=int(value))


class AKV3ScheduleDurationNumber(NumberEntity):
    """Explicit endpoint+slot work or pause duration for an AK V3 schedule."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "s"
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, device, entry, endpoint: int, slot: int, field: str) -> None:
        self._device, self._endpoint, self._slot, self._field = device, endpoint, slot, field
        self._attr_name = f"Schedule {slot} {field} duration"
        self._attr_icon = "mdi:timer" if field == "work" else "mdi:timer-pause"
        self._attr_native_min_value = 0
        self._attr_native_max_value = 65535
        self._attr_unique_id = f"{device.unique_id}_schedule_{slot}_{field}_duration"
        self._attr_device_info = device.device_info
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def _schedule(self):
        return self._device.state.ak_v3_schedules.get((self._endpoint, self._slot))

    @property
    def native_value(self) -> float | None:
        return getattr(self._schedule, f"{self._field}_seconds") if self._schedule else None

    @property
    def available(self) -> bool:
        return self._device.ak_v3_read_available("schedules") and self._schedule is not None

    async def async_set_native_value(self, value: float) -> None:
        await self._device.async_update_ak_v3_slot(
            self._endpoint, self._slot, **{f"{self._field}_seconds": int(value)}
        )


class ScentimentLevelNumber(NumberEntity):
    """Spray intensity level (Scentiment, 1-3)."""

    _attr_has_entity_name = True
    _attr_name = "Level"
    _attr_icon = "mdi:speedometer"
    _attr_native_min_value = 1
    _attr_native_max_value = 3
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, device: ScentDiffuserDevice, entry: ConfigEntry) -> None:
        self._device = device
        self._attr_unique_id = f"{device.unique_id}_level"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
        }
        device.register_state_callback(self._on_state_update)

    def _on_state_update(self) -> None:
        if self.hass is None:
            return
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._device.state.level

    @property
    def available(self) -> bool:
        return self._device.available

    async def async_set_native_value(self, value: float) -> None:
        await self._device.set_level(int(value))
