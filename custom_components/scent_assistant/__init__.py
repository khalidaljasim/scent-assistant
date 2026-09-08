"""The Scent Diffuser integration."""
from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    CONF_BLE_ADDRESS,
    CONF_BLE_NAME,
    CONF_DEVICE_TYPE,
    CONF_CLOUD_USERNAME,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_DEVICE_ID,
    CONF_CONNECTION_MODE,
    CONF_AK_PASSWORD,
    DEFAULT_AK_PASSWORD,
    CLOUD_POLL_INTERVAL_SECONDS,
    WEEKDAY_MON, WEEKDAY_TUE, WEEKDAY_WED, WEEKDAY_THU,
    WEEKDAY_FRI, WEEKDAY_SAT, WEEKDAY_SUN,
    DeviceType,
)
from .device import ScentDiffuserDevice
from .protocol_ble import ScheduleSlot, ScheduleSetup
from .protocol_cloud import AromaLinkCloudClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "sensor", "number", "time", "button", "light", "select", "text"]

SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_AK_V3_UPDATE_SLOT_INTENSITY = "update_ak_v3_slot_intensity"
SERVICE_AK_V3_UPDATE_SLOT = "update_ak_v3_slot"
SERVICE_AK_V3_DIAGNOSE_SCHEDULE_TABLE = "diagnose_ak_v3_schedule_table"
SERVICE_AK_V3_VERIFY_SLOT_LIFECYCLE = "verify_ak_v3_slot_lifecycle"
SERVICE_AK_V3_PRESERVE_LOGICAL_SLOT = "preserve_ak_v3_logical_slot"
SERVICE_AK_V3_SET_DEVICE_NAME = "set_ak_v3_device_name"
SERVICE_AK_V3_SET_DEVICE_LABEL = "set_ak_v3_device_label"
SERVICE_AK_V3_SET_OIL_NAME = "set_ak_v3_oil_name"
SERVICE_AK_V3_SET_OIL = "set_ak_v3_oil"
SERVICE_AK_V3_CALIBRATE_OIL = "calibrate_ak_v3_oil"

DAY_NAME_TO_BIT = {
    "mon": WEEKDAY_MON,
    "tue": WEEKDAY_TUE,
    "wed": WEEKDAY_WED,
    "thu": WEEKDAY_THU,
    "fri": WEEKDAY_FRI,
    "sat": WEEKDAY_SAT,
    "sun": WEEKDAY_SUN,
}

SET_SCHEDULE_SCHEMA = vol.Schema({
    vol.Required("days"): vol.All(
        cv.ensure_list,
        [vol.In(["mon", "tue", "wed", "thu", "fri", "sat", "sun", "all"])],
    ),
    vol.Optional("start_time", default="00:00"): cv.string,
    vol.Optional("end_time", default="23:59"): cv.string,
    vol.Optional("work_seconds", default=10): vol.All(
        vol.Coerce(int), vol.Range(min=5, max=600),
    ),
    vol.Optional("pause_seconds", default=120): vol.All(
        vol.Coerce(int), vol.Range(min=5, max=3600),
    ),
    vol.Optional("enabled", default=True): cv.boolean,
    vol.Optional("entity_id"): cv.string,
})

AK_V3_DIAGNOSE_SCHEDULE_TABLE_SCHEMA = vol.Schema({
    vol.Required("config_entry_id"): cv.string,
    vol.Required("endpoint"): vol.All(vol.Coerce(int), vol.Range(min=1, max=0xFF)),
    vol.Optional("cycles", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
    vol.Optional("source", default="physical"): vol.In(("physical", "committed")),
})

AK_V3_UPDATE_SLOT_SCHEMA = vol.All(vol.Schema({
    vol.Required("config_entry_id"): cv.string,
    vol.Required("endpoint"): vol.All(vol.Coerce(int), vol.Range(min=1, max=0xFF)),
    vol.Required("slot"): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
    vol.Optional("enabled"): cv.boolean,
    vol.Optional("start_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Optional("start_minute"): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
    vol.Optional("end_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Optional("end_minute"): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
    vol.Optional("days_mask"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0x7F)),
    vol.Optional("mode"): vol.All(vol.Coerce(int), vol.In((0, 1))),
    vol.Optional("intensity"): vol.All(vol.Coerce(int), vol.Range(min=0, max=20)),
    vol.Optional("work_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    vol.Optional("pause_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    vol.Optional("rollback_on_failure", default=True): cv.boolean,
}), lambda data: data if any(key in data for key in {
    "enabled", "start_hour", "start_minute", "end_hour", "end_minute", "days_mask",
    "mode", "intensity", "work_seconds", "pause_seconds",
}) else (_ for _ in ()).throw(vol.Invalid("At least one schedule field is required")))

AK_V3_VERIFY_SLOT_LIFECYCLE_SCHEMA = vol.Schema({
    vol.Required("config_entry_id"): cv.string,
    vol.Required("confirmation"): vol.Equal("VERIFY AK V3 SLOT LIFECYCLE"),
    vol.Required("enabled"): cv.boolean,
    vol.Required("start_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Required("start_minute"): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
    vol.Required("end_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Required("end_minute"): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
    vol.Required("days_mask"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0x7F)),
    vol.Required("mode"): vol.All(vol.Coerce(int), vol.In((0, 1))),
    vol.Required("intensity"): vol.All(vol.Coerce(int), vol.Range(min=0, max=20)),
    vol.Required("work_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    vol.Required("pause_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
})

AK_V3_PRESERVE_LOGICAL_SLOT_SCHEMA = vol.Schema({
    vol.Required("config_entry_id"): cv.string,
    vol.Required("endpoint"): vol.All(vol.Coerce(int), vol.Range(min=1, max=0xFF)),
    vol.Required("slot"): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
    vol.Required("enabled"): cv.boolean,
    vol.Required("start_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Required("start_minute"): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
    vol.Required("end_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Required("end_minute"): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
    vol.Required("days_mask"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0x7F)),
    vol.Required("mode"): vol.All(vol.Coerce(int), vol.In((0, 1))),
    vol.Required("intensity"): vol.All(vol.Coerce(int), vol.Range(min=0, max=20)),
    vol.Required("work_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    vol.Required("pause_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
})

AK_V3_TARGET_SCHEMA = vol.Schema({
    vol.Required("config_entry_id"): cv.string,
})

AK_V3_TEXT_SCHEMA = vol.Schema({
    vol.Required("config_entry_id"): cv.string,
    vol.Required("value"): cv.string,
})

AK_V3_SET_OIL_SCHEMA = vol.All(vol.Schema({
    vol.Required("config_entry_id"): cv.string,
    vol.Optional("total_ml"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    vol.Optional("remaining_ml"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    vol.Optional("flow_mlh"): vol.All(vol.Coerce(float), vol.Range(min=0, max=655.35)),
}), lambda data: data if any(key in data for key in ("total_ml", "remaining_ml", "flow_mlh")) else (_ for _ in ()).throw(vol.Invalid("At least one oil value is required")))

AK_V3_CALIBRATE_OIL_SCHEMA = vol.Schema({
    vol.Required("config_entry_id"): cv.string,
    vol.Required("actual_remaining_ml"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    vol.Required("confirmation"): vol.Equal("CALIBRATE OIL"),
})

async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Persist the default AK login PIN for entries created before V2."""
    if entry.version >= 2:
        return True
    data = dict(entry.data)
    if data.get(CONF_DEVICE_TYPE) == DeviceType.SCENT_MARKETING_AK.value:
        data.setdefault(CONF_AK_PASSWORD, DEFAULT_AK_PASSWORD)
    hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Scent Diffuser from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    ble_address = entry.data.get(CONF_BLE_ADDRESS)
    ble_name = entry.data.get(CONF_BLE_NAME, "")
    device_type = DeviceType(entry.data.get(CONF_DEVICE_TYPE, "aroma_link"))

    connection_mode = entry.data.get(CONF_CONNECTION_MODE, "ble")

    # Set up cloud client if configured for cloud mode
    cloud_client = None
    cloud_device_id = entry.data.get(CONF_CLOUD_DEVICE_ID)
    username = entry.data.get(CONF_CLOUD_USERNAME)
    password = entry.data.get(CONF_CLOUD_PASSWORD)

    if connection_mode == "cloud" and username and password:
        session = async_get_clientsession(hass)
        cloud_client = AromaLinkCloudClient(session=session)
        if not await cloud_client.login(username, password):
            _LOGGER.error("Cloud login failed for %s", ble_name or cloud_device_id)
            return False

    # Create device manager
    device = ScentDiffuserDevice(
        hass=hass,
        ble_address=ble_address if connection_mode == "ble" else None,
        ble_name=ble_name,
        device_type=device_type,
        cloud_client=cloud_client,
        cloud_device_id=cloud_device_id,
        sm_metadata=entry.data.get("sm_metadata"),
        gw_password=entry.data.get("gw_password"),
        ak_password=entry.data.get(CONF_AK_PASSWORD, DEFAULT_AK_PASSWORD),
        persistence_key=entry.entry_id,
    )

    hass.data[DOMAIN][entry.entry_id] = device

    if not hass.services.has_service(DOMAIN, SERVICE_AK_V3_UPDATE_SLOT_INTENSITY):
        async def handle_ak_v3_update_slot_intensity(call: ServiceCall) -> dict:
            target = hass.data[DOMAIN].get(call.data["config_entry_id"])
            if not isinstance(target, ScentDiffuserDevice):
                raise HomeAssistantError("Scent Assistant device is not loaded")
            result = await target.async_update_ak_v3_slot_intensity(
                call.data["endpoint"], call.data["slot"], call.data["intensity"]
            )
            _LOGGER.warning("AK V3 schedule update result: %s", result)
            return result

        hass.services.async_register(
            DOMAIN,
            SERVICE_AK_V3_UPDATE_SLOT_INTENSITY,
            handle_ak_v3_update_slot_intensity,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_AK_V3_UPDATE_SLOT):
        async def handle_ak_v3_update_slot(call: ServiceCall) -> dict:
            target = hass.data[DOMAIN].get(call.data["config_entry_id"])
            if not isinstance(target, ScentDiffuserDevice):
                raise HomeAssistantError("Scent Assistant device is not loaded")
            changes = {
                key: value for key, value in call.data.items()
                if key in {
                    "enabled", "start_hour", "start_minute", "end_hour", "end_minute",
                    "days_mask", "mode", "intensity", "work_seconds", "pause_seconds",
                }
            }
            return await target.async_update_ak_v3_slot(
                call.data["endpoint"], call.data["slot"],
                rollback_on_failure=call.data["rollback_on_failure"], **changes
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_AK_V3_UPDATE_SLOT,
            handle_ak_v3_update_slot,
            schema=AK_V3_UPDATE_SLOT_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_AK_V3_DIAGNOSE_SCHEDULE_TABLE):
        async def handle_ak_v3_diagnose_schedule_table(call: ServiceCall) -> dict:
            target = hass.data[DOMAIN].get(call.data["config_entry_id"])
            if not isinstance(target, ScentDiffuserDevice):
                raise HomeAssistantError("Scent Assistant device is not loaded")
            return await target.async_diagnose_ak_v3_schedule_table(
                call.data["endpoint"], call.data["cycles"], call.data["source"]
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_AK_V3_DIAGNOSE_SCHEDULE_TABLE,
            handle_ak_v3_diagnose_schedule_table,
            schema=AK_V3_DIAGNOSE_SCHEDULE_TABLE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_AK_V3_VERIFY_SLOT_LIFECYCLE):
        async def handle_ak_v3_verify_slot_lifecycle(call: ServiceCall) -> dict:
            target = hass.data[DOMAIN].get(call.data["config_entry_id"])
            if not isinstance(target, ScentDiffuserDevice):
                raise HomeAssistantError("Scent Assistant device is not loaded")
            schedule = {
                key: value for key, value in call.data.items()
                if key not in {"config_entry_id", "confirmation"}
            }
            return await target.async_verify_ak_v3_slot_lifecycle(**schedule)

        hass.services.async_register(
            DOMAIN,
            SERVICE_AK_V3_VERIFY_SLOT_LIFECYCLE,
            handle_ak_v3_verify_slot_lifecycle,
            schema=AK_V3_VERIFY_SLOT_LIFECYCLE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_AK_V3_PRESERVE_LOGICAL_SLOT):
        async def handle_ak_v3_preserve_logical_slot(call: ServiceCall) -> dict:
            target = hass.data[DOMAIN].get(call.data["config_entry_id"])
            if not isinstance(target, ScentDiffuserDevice):
                raise HomeAssistantError("Scent Assistant device is not loaded")
            fields = {key: value for key, value in call.data.items() if key not in {"config_entry_id", "endpoint", "slot"}}
            return await target.async_preserve_ak_v3_logical_slot(
                call.data["endpoint"], call.data["slot"], **fields
            )

        hass.services.async_register(
            DOMAIN, SERVICE_AK_V3_PRESERVE_LOGICAL_SLOT, handle_ak_v3_preserve_logical_slot,
            schema=AK_V3_PRESERVE_LOGICAL_SLOT_SCHEMA, supports_response=SupportsResponse.ONLY,
        )

    def ak_v3_target(call: ServiceCall) -> ScentDiffuserDevice:
        target = hass.data[DOMAIN].get(call.data["config_entry_id"])
        if not isinstance(target, ScentDiffuserDevice):
            raise HomeAssistantError("Scent Assistant device is not loaded")
        return target

    if not hass.services.has_service(DOMAIN, SERVICE_AK_V3_SET_DEVICE_NAME):
        async def handle_ak_v3_set_device_name(call: ServiceCall) -> dict:
            return {"success": await ak_v3_target(call).async_set_ak_v3_device_name(call.data["value"])}

        hass.services.async_register(
            DOMAIN, SERVICE_AK_V3_SET_DEVICE_NAME, handle_ak_v3_set_device_name,
            schema=AK_V3_TEXT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_AK_V3_SET_DEVICE_LABEL):
        async def handle_ak_v3_set_device_label(call: ServiceCall) -> dict:
            return {"success": await ak_v3_target(call).async_set_ak_v3_device_label(call.data["value"])}

        hass.services.async_register(
            DOMAIN, SERVICE_AK_V3_SET_DEVICE_LABEL, handle_ak_v3_set_device_label,
            schema=AK_V3_TEXT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_AK_V3_SET_OIL_NAME):
        async def handle_ak_v3_set_oil_name(call: ServiceCall) -> dict:
            return {"success": await ak_v3_target(call).async_set_ak_v3_oil_name(call.data["value"])}

        hass.services.async_register(
            DOMAIN, SERVICE_AK_V3_SET_OIL_NAME, handle_ak_v3_set_oil_name,
            schema=AK_V3_TEXT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_AK_V3_SET_OIL):
        async def handle_ak_v3_set_oil(call: ServiceCall) -> dict:
            return {
                "success": await ak_v3_target(call).async_set_ak_v3_oil(
                    total_ml=call.data.get("total_ml"),
                    remaining_ml=call.data.get("remaining_ml"),
                    flow_mlh=call.data.get("flow_mlh"),
                )
            }

        hass.services.async_register(
            DOMAIN, SERVICE_AK_V3_SET_OIL, handle_ak_v3_set_oil,
            schema=AK_V3_SET_OIL_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_AK_V3_CALIBRATE_OIL):
        async def handle_ak_v3_calibrate_oil(call: ServiceCall) -> dict:
            return {
                "success": await ak_v3_target(call).async_calibrate_ak_v3_oil(
                    call.data["actual_remaining_ml"]
                )
            }

        hass.services.async_register(
            DOMAIN, SERVICE_AK_V3_CALIBRATE_OIL, handle_ak_v3_calibrate_oil,
            schema=AK_V3_CALIBRATE_OIL_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
        )

    # Cloud-mode devices have no push channel for autonomous state changes
    # (BLE devices push notifications when connected). Poll the cloud
    # periodically so HA reflects the device's real state, not just the
    # last command we sent. See CLOUD_POLL_INTERVAL_SECONDS in const.py.
    if connection_mode == "cloud" and cloud_client is not None:
        async def _periodic_cloud_poll(now=None) -> None:
            try:
                await device.refresh_state()
            except Exception as err:
                _LOGGER.debug("Cloud state poll failed (will retry): %s", err)

        device._unsub_cloud_poll = async_track_time_interval(
            hass,
            _periodic_cloud_poll,
            timedelta(seconds=CLOUD_POLL_INTERVAL_SECONDS),
        )

    # Register services (once for all entries)
    if not hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        async def handle_set_schedule(call: ServiceCall) -> None:
            """Handle the set_schedule service call."""
            days_list = call.data["days"]
            start_time = call.data["start_time"]
            end_time = call.data["end_time"]
            work_seconds = call.data["work_seconds"]
            pause_seconds = call.data["pause_seconds"]
            enabled = call.data["enabled"]
            entity_id = call.data.get("entity_id")

            # Build weekday mask
            weekday_mask = 0
            for day in days_list:
                if day == "all":
                    weekday_mask = 0x7F
                    break
                weekday_mask |= DAY_NAME_TO_BIT.get(day, 0)

            # Parse times
            start_h, start_m = (int(x) for x in start_time.split(":"))
            end_h, end_m = (int(x) for x in end_time.split(":"))

            # Find target device(s)
            targets = []
            for eid, dev in hass.data[DOMAIN].items():
                if isinstance(dev, ScentDiffuserDevice):
                    if entity_id is None or eid == entity_id:
                        targets.append(dev)

            if not targets:
                _LOGGER.error("No devices found for set_schedule service")
                return

            for dev in targets:
                await dev.set_schedule(
                    weekday_mask=weekday_mask,
                    start_hour=start_h,
                    start_minute=start_m,
                    end_hour=end_h,
                    end_minute=end_m,
                    work_seconds=work_seconds,
                    pause_seconds=pause_seconds,
                    enabled=enabled,
                )

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_SCHEDULE,
            handle_set_schedule,
            schema=SET_SCHEDULE_SCHEMA,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Entity callbacks, the response dispatcher, and the device registry entry
    # now exist. The device itself defers BLE until HA has reached RUNNING.
    device.async_schedule_initialization()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        device: ScentDiffuserDevice = hass.data[DOMAIN].pop(entry.entry_id)
        unsub = getattr(device, "_unsub_cloud_poll", None)
        if unsub is not None:
            unsub()
        await device.async_cancel_initialization()
        await device.async_shutdown()

    return unload_ok
