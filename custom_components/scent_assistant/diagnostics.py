"""Diagnostics support for the Scent Diffuser integration.

A reporter can click "Download Diagnostics" on the device page in HA and
attach the resulting JSON to a bug report; everything we need to validate
or debug detection + protocol behaviour is contained here.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_BLE_ADDRESS,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_USERNAME,
    CONF_GW_PASSWORD,
)
from .device import ScentDiffuserDevice

TO_REDACT = {
    CONF_BLE_ADDRESS, CONF_CLOUD_USERNAME, CONF_CLOUD_PASSWORD,
    CONF_GW_PASSWORD, "mac_from_adv",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    device: ScentDiffuserDevice | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    state_snapshot: dict[str, Any] = {}
    if device is not None:
        s = device.state
        state_snapshot = {
            "power": s.power,
            "phase": s.phase,
            "fan": s.fan,
            "level": s.level,
            "battery": s.battery,
            "oil_remaining": s.oil_remaining,
            "oil_current_ml": s.oil_current_ml,
            "oil_max_ml": s.oil_max_ml,
            "oil_consumption_mlh": s.oil_consumption_mlh,
            "oil_days_remaining": s.oil_days_remaining,
            "schedule_custom_mode": s.schedule_custom_mode,
            "grade_table": s.grade_table,
            "work_remaining": s.work_remaining,
            "pause_remaining": s.pause_remaining,
            "lock": s.lock,
            "light_on": s.light_on,
            "device_name": s.device_name,
            "password_required": s.password_required,
            "firmware_version": s.firmware_version,
            "work_seconds": s.work_seconds,
            "pause_seconds": s.pause_seconds,
            # Scent Marketing AK read-back fields let a reporter confirm the
            # stored schedule state seen by the response-driven reader.
            "intensity": s.intensity,
            "weekday_mask": s.weekday_mask,
            "schedule_slot": s.schedule_slot,
            "start_hour": s.start_hour,
            "start_minute": s.start_minute,
            "end_hour": s.end_hour,
            "end_minute": s.end_minute,
            "device_label": s.device_label,
            "model_code": s.model_code,
            "schedule_enabled": s.schedule_enabled,
            "ak_v3_schedules": {
                f"{endpoint}:{slot}": {
                    "enabled": schedule.enabled,
                    "start_hour": schedule.start_hour,
                    "start_minute": schedule.start_minute,
                    "end_hour": schedule.end_hour,
                    "end_minute": schedule.end_minute,
                    "days_mask": schedule.days_mask,
                    "mode": schedule.mode,
                    "intensity": schedule.intensity,
                    "work_seconds": schedule.work_seconds,
                    "pause_seconds": schedule.pause_seconds,
                    "raw_frame": schedule.raw_frame.hex(),
                }
                for (endpoint, slot), schedule in s.ak_v3_schedules.items()
            },
        }

    payload: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "device": {
            "device_type": device.device_type.value if device else None,
            "model": device.model_name if device else None,
            "available": device.available if device else None,
            "connection_mode": device.connection_mode if device else None,
            "supports_fan": device.supports_fan if device else None,
            "supports_cloud": device.supports_cloud if device else None,
            "ble_connection": device.ble_connection_diagnostics if device else {},
        },
        "ak_v3_protocol_scope": device.ak_v3_protocol_scope if device else {},
        "ak_v3_runtime_readback": {
            "model_code": device.state.model_code if device else None,
            "firmware_version": device.state.firmware_version if device else None,
            "note": (
                "These are parsed from read-only D0/CB responses when received; "
                "they are not asserted to match the official-app-reported scope."
            ),
        },
        "sm_metadata": async_redact_data(
            dict(device.sm_metadata) if device and device.sm_metadata else {},
            TO_REDACT,
        ),
        "state": state_snapshot,
        "recent_notifications_hex": device.recent_notifications if device else [],
        "recent_commands_hex": device.recent_commands if device else [],
    }
    return payload
