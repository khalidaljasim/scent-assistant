"""Unified device manager for scent diffusers."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime
from enum import StrEnum

from bleak import BleakClient, BleakScanner, BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import (
    AK_V3_BOOT_SETTLE_SECONDS,
    DeviceType,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_WORK_DURATION,
    DEFAULT_PAUSE_DURATION,
    SM_AK_RESP_SCHEDULE_V3,
    SM_AK_RESP_DEVICE_NAME_V3,
    SM_AK_RESP_LABEL_V3,
    SM_AK_RESP_FRAGRANCES_V3,
    SM_GW_DP_PASSWORD,
)
from .protocol_ble import (
    BleProtocol,
    DiffuserState,
    ScheduleSlot,
    ScheduleSetup,
    AromaLinkBleProtocol,
    ScentimentProtocol,
    ScentMarketingAkProtocol,
    ScentMarketingGwProtocol,
    ScentMarketingGwXorProtocol,
    AromelyAroMaxProtocol,
    get_protocol,
    detect_device_type,
)
from .protocol_cloud import AromaLinkCloudClient

_LOGGER = logging.getLogger(__name__)

# Cooldown after a failed connect / write before we try again, so a
# stuck device gets a chance to recover instead of being hammered.
BLE_FAILURE_COOLDOWN_SECONDS = 3.0
# bleak_retry_connector max-attempts. HA's bluetooth stack already
# layers its own retries on top of ours, so keeping this low avoids
# 6-8 rapid connect attempts that can wedge some firmwares.
BLE_CONNECT_MAX_ATTEMPTS = 2
BLE_IDLE_DISCONNECT_SECONDS = 10.0
AK_V3_LOGIN_TIMEOUT_SECONDS = 0.5
# Keep two seconds of caller/API margin outside this internal deadline.
AK_V3_DIAGNOSTIC_DEADLINE_SECONDS = 20.0
# A mutation has three response-driven reads (baseline, verification, and
# possible rollback verification). Keep each phase bounded so a caller can
# receive a definitive rollback result before its service request expires.
AK_V3_TRANSACTION_READ_SECONDS = 10.0
# Trailing CA/86 writes close the device exchange but do not redefine whether
# five owned 4A records were received. Bound them independently so a stuck
# GATT acknowledgement cannot erase an already complete table.
AK_V3_ACK_FINALIZE_SECONDS = 1.0
# AK V3 metadata replies contain no request identifier. Retrying a timed-out
# opcode could accept a late response from the previous attempt.
AK_V3_METADATA_RESPONSE_SECONDS = 1.5
# Official-app metadata scopes the AK V3 schedule investigation. These values
# are not BLE read-back and must not be generalized to other AK hardware.
AK_V3_PROTOCOL_SCOPE = {
    "source": "official_app_reported",
    "device_model": "SA_Ultra Max Tower",
    "device_location": "MiCasa",
    "pcb_version": "DV1.0 V1.1",
    "diffuser_firmware_version": "A316",
    "official_app_version": "3.0.9 (3092)",
    "scope": "Only this hardware, PCB, firmware, and official-app combination.",
    "generalization": "Unverified for other AK models, PCB revisions, firmware versions, or app versions.",
}
# Default run time for the momentary "Diffuse Now" button. Adjustable
# per device via the Momentary Duration number entity (not persisted
# across HA restarts).
DEFAULT_MOMENTARY_SECONDS = 30


@dataclass
class AKV3ModernRead:
    """One response-driven A316 schedule-table read transaction."""

    transaction_id: int
    expected_endpoint: int | None = None
    expected_slot: int = 1
    endpoint: int | None = None
    phase: str = "slot"
    records: dict[int, object] | None = None
    diagnostic: bool = False
    events: list[dict] | None = None
    transitions: list[dict] | None = None
    completion_event: asyncio.Event | None = None
    records_complete_event: asyncio.Event | None = None
    last_activity_at: float | None = None
    completion_result: str = "pending"
    send_tasks: set[asyncio.Task] | None = None
    send_lock: asyncio.Lock | None = None
    aggregate_flags: tuple[bool, bool] | None = None

    def __post_init__(self) -> None:
        if self.records is None:
            self.records = {}
        if self.events is None:
            self.events = []
        if self.transitions is None:
            self.transitions = []
        if self.completion_event is None:
            self.completion_event = asyncio.Event()
        if self.records_complete_event is None:
            self.records_complete_event = asyncio.Event()
        if self.send_tasks is None:
            self.send_tasks = set()
        if self.send_lock is None:
            self.send_lock = asyncio.Lock()


@dataclass
class AKV3MetadataRead:
    """One isolated, response-validated AK V3 metadata read."""

    command: bytes
    response_opcode: int
    state_field: str
    minimum_response_length: int = 1
    response_event: asyncio.Event = field(default_factory=asyncio.Event)
    accepted: bool = False
    generation: int | None = None
    expected_response_length: int | None = None
    response: bytes | None = None


@dataclass
class AKV3StartupChain:
    """One generation-owned, reply-driven V3 metadata exchange."""

    generation: int
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    failed: bool = False
    post_21_sent: bool = False
    accepted_frames: dict[int, bytes] = field(default_factory=dict)
    continuations_sent: set[int] = field(default_factory=set)
    rejected_opcodes: set[int] = field(default_factory=set)
    grade_record_count: int | None = None
    grade_max: int | None = None


@dataclass
class AKV3StartupBarrier:
    """One generation-owned barrier for V3 startup reads."""

    generation: int
    armed: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)
    chain: AKV3StartupChain | None = None
    failed: bool = False


@dataclass
class AKV3LoginState:
    """One generation-owned primary/fallback AK V3 login exchange."""

    generation: int
    phase: str = "primary"
    response_event: asyncio.Event = field(default_factory=asyncio.Event)
    fallback_sent: bool = False
    fallback_required: bool = False
    accepted: bool = False


class AKV3SlotLifecycle(StrEnum):
    """Transient lifecycle of one authoritative AK V3 schedule slot."""

    PRESENT_ENABLED = "present_enabled"
    PRESENT_DISABLED = "present_disabled"
    INTENTIONALLY_DISABLED_ABSENT = "intentionally_disabled_absent"
    UNEXPECTEDLY_ABSENT = "unexpectedly_absent"
    CREATING = "creating"
    UPDATING = "updating"
    DISABLING = "disabling"
    VERIFYING = "verifying"
    RESTORING = "restoring"
    STALE = "stale"
    ERROR = "error"


class ScentDiffuserDevice:
    """Manages a single scent diffuser via BLE and/or cloud."""

    def __init__(
        self,
        hass: HomeAssistant | None = None,
        ble_address: str | None = None,
        ble_name: str | None = None,
        device_type: DeviceType | None = None,
        cloud_client: AromaLinkCloudClient | None = None,
        cloud_device_id: str | None = None,
        sm_metadata: dict | None = None,
        gw_password: str | None = None,
        ak_password: str = "8888",
        persistence_key: str | None = None,
    ) -> None:
        # HomeAssistant reference, used to fetch a cached BLEDevice via
        # the core bluetooth integration before opening a connection.
        # Optional so the manager can still be unit-tested without HA.
        self._hass = hass
        # Detection metadata from the config flow — populated only for
        # Scent Marketing family devices.
        self._sm_metadata = sm_metadata or {}
        # Optional 4-char ASCII password for Scent Marketing GW devices.
        # Sent proactively after every BLE connect.
        self._gw_password = gw_password or None
        # Trace ring-buffer for the diagnostics download.
        self._recent_notifications: list[str | dict] = []
        self._recent_commands: list[str | dict] = []
        # BLE
        self._ble_address = ble_address
        self._ble_name = ble_name or ""
        self._ble_client: BleakClient | None = None
        self._ble_connected = False
        self._ble_notify_subscribed = False
        self._ble_connection_stage = "idle"
        self._ble_connection_error: str | None = None
        self._ble_lock = asyncio.Lock()
        self._ble_reconnect_task: asyncio.Task | None = None
        self._ble_disconnect_task: asyncio.Task | None = None
        self._ble_disconnect_expected = False
        # A slot transaction clears the transient read-back cache while it
        # collects replies. Do not let concurrent editor changes interleave.
        self._ak_v3_transaction_lock = asyncio.Lock()
        # Competing user actions fail at their call boundary rather than queue.
        self._ak_v3_action_owner: tuple[str, int, int] | None = None
        self._ak_v3_action_id = 0
        self._ak_v3_read_transaction_id = 0
        self._ak_v3_modern_read: AKV3ModernRead | None = None
        self._ak_v3_metadata_read: AKV3MetadataRead | None = None
        self._ak_v3_startup_chain: AKV3StartupChain | None = None
        self._ak_v3_startup_barrier: AKV3StartupBarrier | None = None
        self._ak_v3_login: AKV3LoginState | None = None
        self._ak_v3_startup_generation = 0
        # Availability is owned by the current authenticated generation, not
        # by whatever values happened to remain in DiffuserState.
        self._ak_v3_current_fields: set[str] = set()
        self._ak_v3_retained_generation: int | None = None
        self._ak_v3_retained_fields: set[str] = set()
        self._ak_v3_power_generation: int | None = None
        self._ak_v3_startup_trace: list[dict] = []
        self._ak_v3_startup_trace_active = False
        self._ak_v3_calculation_read_generation = 0
        self._ak_v3_startup_read_task: asyncio.Task | None = None
        self._ak_v3_startup_listener_unsub: callable | None = None
        self._ak_v3_startup_delay_unsub: callable | None = None
        self._ak_v3_entity_platforms_ready = False
        self._ak_v3_initialization_scheduled = False
        self._ak_v3_manual_refresh_active = False
        self._ak_v3_manual_refresh_generation: int | None = None
        self._ak_v3_manual_refresh_result: str | None = None
        self._ak_v3_manual_refresh_at: str | None = None
        self._ak_v3_modern_diagnostic_trace: AKV3ModernRead | None = None
        self._ak_v3_slot_lifecycle: dict[tuple[int, int], AKV3SlotLifecycle] = {}
        self._ak_v3_restored_identities: set[tuple[int, int]] = set()
        self._ak_v3_confirmation_metadata: dict[tuple[int, int], dict] = {}
        # This is set only by the protected create/read/delete/read verification
        # transaction. It deliberately does not expose public create/delete APIs.
        self._ak_v3_slot_lifecycle_verified = False
        self._ak_v3_store = (
            Store(hass, 1, f"scent_assistant.ak_v3_schedules.{persistence_key}")
            if hass is not None and persistence_key else None
        )
        self._ak_v3_store_lock = asyncio.Lock()
        self._ble_has_synced_time = False
        # Monotonic timestamp of the last failed BLE connect/write —
        # used to back off after errors instead of hammering a stuck
        # device (which can wedge a V3 diffuser's GATT stack badly
        # enough that even the official app can't reconnect until a
        # power cycle, per @Mins95's 2026-06-01 report).
        self._ble_last_failure_ts: float = 0.0

        # Device type
        if device_type:
            self._device_type = device_type
        elif ble_name:
            # No advertisement object available at this point — fall back to
            # name-only detection (config flow performs the richer
            # advertisement-aware detection up front and persists the result).
            self._device_type = detect_device_type(ble_name) or DeviceType.AROMA_LINK
        else:
            self._device_type = DeviceType.AROMA_LINK

        # Protocol handler. GW-XOR needs the MAC for its keystream; GW
        # devices with PID 98 use the Tuya-DP hex parser.
        mac = (ble_address or "").replace(":", "")
        pid = self._sm_metadata.get("pid") if self._sm_metadata else None
        self._protocol: BleProtocol = get_protocol(
            self._device_type, mac=mac, pid=pid, ak_password=ak_password,
        )

        # Cloud
        self._cloud: AromaLinkCloudClient | None = cloud_client
        self._cloud_device_id = cloud_device_id

        # State
        self._state = DiffuserState()
        self._state_callbacks: list[callable] = []

        # Momentary diffusion ("Diffuse Now" button): power on, then
        # auto-off after this many seconds via a background task.
        self.momentary_seconds: int = DEFAULT_MOMENTARY_SECONDS
        self._momentary_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._ble_name or f"Diffuser {self._ble_address or self._cloud_device_id}"

    @property
    def unique_id(self) -> str:
        return self._ble_address or self._cloud_device_id or "unknown"

    @property
    def device_type(self) -> DeviceType:
        return self._device_type

    @property
    def sm_metadata(self) -> dict:
        """Scent Marketing detection metadata (empty for other families)."""
        return self._sm_metadata

    @property
    def recent_notifications(self) -> list[str | dict]:
        return list(self._recent_notifications)

    @property
    def recent_commands(self) -> list[str | dict]:
        return list(self._recent_commands)

    @property
    def ak_v3_protocol_scope(self) -> dict:
        """Return the fixed official-app scope for AK V3 investigation output."""
        return dict(AK_V3_PROTOCOL_SCOPE)

    @property
    def supports_ak_v3_custom_mode(self) -> bool:
        """Return whether the device confirmed custom mode and usable limits."""
        limits = self._state.grade_limits
        return bool(self._state.ak_v3_has_custom_mode and limits and all(limits[1:]))

    @property
    def ble_connection_diagnostics(self) -> dict:
        """Expose the most recent BLE connection stage without retrying it."""
        return {
            "stage": self._ble_connection_stage,
            "error": self._ble_connection_error,
            "connected": self._ble_connected,
            "notifications_subscribed": self._ble_notify_subscribed,
        }

    @property
    def model_name(self) -> str:
        """Human-readable model name for HA's device-info "model" field.

        For Scent Marketing devices this surfaces the detected family
        directly on the device page, so a reporter can verify our
        detection at a glance without digging through logs.
        """
        mapping = {
            DeviceType.TUYA_BLE: "ShinePick / Tuya BLE",
            DeviceType.AROMA_LINK: "Aroma-Link",
            DeviceType.SCENTIMENT: "Scentiment Air 2",
            DeviceType.SCENT_MARKETING_AK: "Scent Marketing (AK)",
            DeviceType.SCENT_MARKETING_GW: "Scent Marketing (GW)",
            DeviceType.SCENT_MARKETING_GW_XOR: "Scent Marketing (GW, encrypted)",
            DeviceType.AROMELY_ARO_MAX: "Aromely Aro Max",
        }
        base = mapping.get(self._device_type, self._device_type.value)
        # Append the PID when known — different OEMs share the same family
        # but have distinct PIDs, useful for triage.
        pid = self._sm_metadata.get("pid")
        if pid is not None:
            return f"{base} — PID {pid}"
        return base

    @property
    def device_info(self) -> dict:
        """Shared HA device_info block, consumed by every entity."""
        return {
            "identifiers": {("scent_assistant", self.unique_id)},
            "name": self.name,
            "manufacturer": "Scent Diffuser",
            "model": self.model_name,
        }

    @property
    def state(self) -> DiffuserState:
        return self._state

    @property
    def supports_fan(self) -> bool:
        return self._protocol.supports_fan()

    @property
    def protocol_is_v3(self) -> bool:
        """True when the AK protocol has identified the device as V3.

        Stays False both for non-AK protocols and for AK devices whose
        login response hasn't been parsed yet (i.e. before the first
        successful BLE connect).
        """
        proto = self._protocol
        if isinstance(proto, ScentMarketingAkProtocol):
            return proto.is_v3
        return False

    @property
    def is_ak_protocol(self) -> bool:
        """Whether this entry uses the Scent Marketing AK protocol family."""
        return isinstance(self._protocol, ScentMarketingAkProtocol)

    @property
    def ak_v3_manual_refresh_available(self) -> bool:
        """Whether an isolated AK state refresh can start now."""
        return (
            self.is_ak_protocol
            and bool(self._ble_address)
            and getattr(self, "_ak_v3_action_owner", None) is None
            and not self._ble_lock.locked()
            and not self._ak_v3_manual_refresh_active
            and self._ak_v3_startup_chain is None
            and self._ak_v3_modern_read is None
            and self._ak_v3_metadata_read is None
        )

    def _claim_ak_v3_action(self, action: str) -> tuple[str, int, int] | None:
        """Synchronously claim the one generation-owned AK V3 user action."""
        if (
            getattr(self, "_ak_v3_action_owner", None) is not None
            or (
                action == "slot_update"
                and (
                    getattr(self, "_ak_v3_startup_chain", None) is not None
                    or getattr(self, "_ak_v3_startup_barrier", None) is not None
                    or getattr(self, "_ak_v3_modern_read", None) is not None
                    or getattr(self, "_ak_v3_metadata_read", None) is not None
                )
            )
        ):
            return None
        self._ak_v3_action_id = getattr(self, "_ak_v3_action_id", 0) + 1
        owner = (action, getattr(self, "_ak_v3_startup_generation", 0), self._ak_v3_action_id)
        self._ak_v3_action_owner = owner
        return owner

    def _advance_ak_v3_action_owner(
        self, owner: tuple[str, int, int]
    ) -> tuple[str, int, int] | None:
        """Carry an owner into its newly authenticated generation only if current."""
        if getattr(self, "_ak_v3_action_owner", None) != owner:
            return None
        current = (owner[0], getattr(self, "_ak_v3_startup_generation", 0), owner[2])
        self._ak_v3_action_owner = current
        return current

    def _ak_v3_action_is_owner(self, owner: tuple[str, int, int]) -> bool:
        """Require both action identity and generation to match before a write."""
        return (
            getattr(self, "_ak_v3_action_owner", None) == owner
            and owner[1] == getattr(self, "_ak_v3_startup_generation", 0)
        )

    def _release_ak_v3_action(self, owner: tuple[str, int, int]) -> None:
        """Stale cleanup cannot release a newer action's owner token."""
        if getattr(self, "_ak_v3_action_owner", None) == owner:
            self._ak_v3_action_owner = None

    def _current_ak_v3_action_owner(
        self, owner: tuple[str, int, int]
    ) -> tuple[str, int, int]:
        """Return this action's advanced generation token, never a successor's."""
        current = getattr(self, "_ak_v3_action_owner", None)
        if current is not None and current[0] == owner[0] and current[2] == owner[2]:
            return current
        return owner

    @property
    def supports_cloud(self) -> bool:
        return self._cloud is not None and self._cloud_device_id is not None

    @property
    def connection_mode(self) -> str:
        if self._ble_address:
            return "ble"
        if self.supports_cloud and self._cloud and self._cloud.authenticated:
            return "cloud"
        return "offline"

    @property
    def available(self) -> bool:
        if self._ble_address:
            return self._ble_connected and self._ble_notify_subscribed
        return self.connection_mode != "offline"

    def ak_v3_read_available(self, *fields: str) -> bool:
        """Return whether AK V3 read fields belong to the live generation."""
        if not (self.device_type == DeviceType.SCENT_MARKETING_AK and self.protocol_is_v3):
            return self.available
        generation = getattr(self, "_ak_v3_startup_generation", 0)
        current = getattr(self, "_ak_v3_current_fields", set())
        retained = (
            getattr(self, "_ak_v3_retained_fields", set())
            if getattr(self, "_ak_v3_retained_generation", None) == generation
            else set()
        )
        if self._ble_connected and self._ble_notify_subscribed:
            return all(field in current for field in fields)
        return all(field in retained for field in fields)

    def _begin_ak_v3_generation(self) -> None:
        """Hide every prior generation before accepting this session's replies."""
        self._ak_v3_current_fields = set()
        self._ak_v3_retained_generation = None
        self._ak_v3_retained_fields = set()

    def _retain_ak_v3_fields(self, *fields: str) -> None:
        """Commit accepted fields for this generation without exposing stale data."""
        generation = getattr(self, "_ak_v3_startup_generation", 0)
        if getattr(self, "_ak_v3_retained_generation", None) != generation:
            self._ak_v3_retained_generation = generation
            self._ak_v3_retained_fields = set()
        current = getattr(self, "_ak_v3_current_fields", None)
        if current is None:
            current = self._ak_v3_current_fields = set()
        current.update(fields)
        self._ak_v3_retained_fields.update(fields)

    def register_state_callback(self, callback: callable) -> None:
        self._state_callbacks.append(callback)

    def unregister_state_callback(self, callback: callable) -> None:
        """Remove an entity callback during its Home Assistant teardown."""
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    def _notify_state_changed(self) -> None:
        for cb in self._state_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Error in state callback")

    def _clear_device_derived_state(self) -> None:
        """Do not present cached BLE data as live after an unexpected disconnect."""
        state = self._state
        for field_name in (
            "power", "fan", "diffusion_enabled", "fan_active", "level", "battery",
            "rgb_on", "rgb_color", "lock", "oil_remaining", "oil_current_ml",
            "oil_max_ml", "oil_consumption_mlh", "oil_days_remaining", "oil_status_byte",
            "oil_old_calibration_ml", "schedule_custom_mode", "grade_table", "grade_limits",
            "light_on", "device_name", "device_label", "model_code", "schedule_enabled",
            "ak_v3_lamp_type", "ak_v3_protocol_identity",
            "firmware_version", "intensity", "weekday_mask", "schedule_slot",
            "password_required", "work_remaining", "pause_remaining",
        ):
            setattr(state, field_name, None)
        state.phase = "unknown"
        state.ak_v3_schedules.clear()
        state.ak_v3_empty_schedules.clear()
        state.ak_v3_metadata_available.clear()

    def _clear_ak_v3_session_state(self) -> None:
        """Start each AK V3 generation with no stale expected-response fields."""
        self._clear_device_derived_state()
        self._state.oil_names.clear()
        self._state.oil_calculation_records.clear()
        self._state.ak_v3_capabilities = None
        self._state.ak_v3_has_oil = False
        self._state.ak_v3_has_battery = False
        self._state.ak_v3_has_custom_mode = False
        self._state.ak_v3_has_aromas = False
        self._state.ak_v3_has_fan = False
        self._state.ak_v3_has_round_battery = False
        self._state.ak_v3_has_lamp = False
        self._state.ak_v3_has_global_control = False
        self._state.ak_v3_reply_chaining = False
        self._state.ak_v3_lamp_type = None
        self._state.ak_v3_protocol_identity = None
        self._ak_v3_power_generation = None
        self._ak_v3_restored_identities.clear()
        self._ak_v3_confirmation_metadata.clear()

    def _activate_ak_v3_startup_trace(self) -> None:
        """Start one bounded, payload-free trace before an AK connection."""
        if getattr(self, "_ak_v3_startup_trace_active", False):
            return
        self._ak_v3_startup_generation = getattr(self, "_ak_v3_startup_generation", 0) + 1
        self._begin_ak_v3_generation()
        self._ak_v3_startup_trace = []
        self._ak_v3_startup_trace_active = True
        self._record_ak_v3_startup_trace("generation_started")

    def _cancel_ak_v3_startup_barrier(self) -> None:
        """Release a superseded barrier without letting it start reads."""
        barrier = getattr(self, "_ak_v3_startup_barrier", None)
        if barrier is not None:
            barrier.failed = True
            barrier.released.set()
        self._ak_v3_startup_barrier = None
        self._ak_v3_startup_chain = None

    async def _async_wait_for_ak_v3_startup_barrier(self) -> bool:
        """Keep independent V3 reads behind the current chain terminal state."""
        barrier = getattr(self, "_ak_v3_startup_barrier", None)
        if barrier is None:
            return True
        if barrier.generation != self._ak_v3_startup_generation:
            return False
        await barrier.released.wait()
        return (
            self._ak_v3_startup_barrier is barrier
            and barrier.generation == self._ak_v3_startup_generation
        )

    def _invalidate_ak_v3_chain_metadata(self, chain: AKV3StartupChain) -> None:
        """Clear only metadata not validated by this generation's ledger."""
        state = self._state
        stages = {
            0x4D: ("power",),
            0x42: ("device_name", "device_name_append_prefix"),
            0x43: ("device_label",),
            0x44: ("firmware_version",),
            0x45: ("model_code",),
            0x46: ("grade_limits",),
            0x47: ("grade_table",),
            0x48: ("oil_names",),
            0x4B: ("oil_current_ml", "oil_max_ml", "oil_remaining", "oil_status_byte"),
            0x50: ("oil_consumption_mlh",),
            0x51: ("light_on",),
        }
        for opcode, fields in stages.items():
            if opcode in chain.accepted_frames:
                continue
            for field_name in fields:
                if field_name == "oil_names":
                    state.oil_names.clear()
                else:
                    setattr(state, field_name, None)
            state.ak_v3_metadata_available.discard({
                0x4D: "power",
                0x42: "device_name", 0x43: "device_label", 0x44: "firmware_version",
                0x45: "model_code", 0x46: "grade_limits", 0x47: "grade_table",
                0x48: "oil_names", 0x4B: "oil_current_ml", 0x50: "oil_consumption_mlh",
                0x51: "light_on",
            }[opcode])
        self._recompute_oil_days()
        self._notify_state_changed()

    def _schedule_disconnect(self) -> None:
        """Keep AK V3 subscribed; retain idle teardown for other protocols."""
        if isinstance(self._protocol, ScentMarketingAkProtocol) and self._protocol.is_v3:
            return
        if self._ble_disconnect_task and not self._ble_disconnect_task.done():
            self._ble_disconnect_task.cancel()
        self._ble_disconnect_task = asyncio.create_task(self._delayed_disconnect())

    async def _delayed_disconnect(self) -> None:
        """Release non-AK-V3 BLE sessions after their established idle timeout."""
        await asyncio.sleep(BLE_IDLE_DISCONNECT_SECONDS)
        async with self._ble_lock:
            await self._teardown_ble_client(reason="idle")

    async def _async_complete_ak_v3_login(
        self, generation: int, *, arm_startup_chain: bool = True
    ) -> bool:
        """Complete one V3 login without allowing replies across generations."""
        login = self._ak_v3_login
        if (
            login is None
            or login.generation != generation
            or generation != self._ak_v3_startup_generation
        ):
            return False
        try:
            await asyncio.wait_for(login.response_event.wait(), AK_V3_LOGIN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            login.fallback_required = True
            self._record_ak_v3_startup_trace("login_primary_timeout")

        if (
            self._ak_v3_login is not login
            or generation != self._ak_v3_startup_generation
        ):
            return False
        if login.accepted and not self._protocol.is_v3:
            ak_time = self._protocol.build_time_sync()
            if ak_time:
                await self._ble_send(ak_time)
                await asyncio.sleep(0.1)
                self._ble_has_synced_time = True
            return True
        if login.fallback_required:
            login.phase = "fallback"
            login.response_event.clear()
            login.fallback_sent = True
            self._record_ak_v3_startup_trace("login_fallback_sent")
            await self._ble_send(self._protocol.build_login_secondary_v3())
            try:
                await asyncio.wait_for(login.response_event.wait(), AK_V3_LOGIN_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                self._record_ak_v3_startup_trace("login_fallback_timeout")
                return False

        if (
            self._ak_v3_login is not login
            or generation != self._ak_v3_startup_generation
            or not login.accepted
        ):
            return False
        if not arm_startup_chain:
            return self._protocol.is_v3
        # A claimed user action owns its authentication generation and starts
        # its own direct-push collector. Startup must not join that session.
        if (getattr(self, "_ak_v3_action_owner", None) or (None,))[0] == "slot_update":
            return False
        chain = AKV3StartupChain(generation)
        barrier = self._ak_v3_startup_barrier
        if barrier is None or barrier.generation != generation:
            return False
        # The V3 clock sync starts both response streams. Arm the metadata
        # chain and passive 4A collector before its dynamic payload is sent.
        self._arm_ak_v3_modern_collector()
        self._ak_v3_startup_chain = chain
        barrier.chain = chain
        barrier.armed.set()
        self._record_ak_v3_startup_trace("chain_armed")
        chain.post_21_sent = True
        if not await self._ble_send(self._protocol.build_time_sync()):
            chain.post_21_sent = False
            chain.failed = True
            chain.completed.set()
            return False
        await asyncio.sleep(0.1)
        self._ble_has_synced_time = True
        return True

    def async_schedule_initialization(self) -> None:
        """Start one device initialization only after HA and platforms are ready."""
        self._ak_v3_entity_platforms_ready = True
        if self._ak_v3_initialization_scheduled:
            return
        self._ak_v3_initialization_scheduled = True
        if self._hass is None:
            self._ak_v3_startup_read_task = asyncio.create_task(self.async_setup())
            return
        if self._hass.state is CoreState.running:
            self._ak_v3_startup_delay_unsub = async_call_later(
                self._hass, AK_V3_BOOT_SETTLE_SECONDS, self._async_after_startup_delay
            )
            return
        self._ak_v3_startup_listener_unsub = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, self._async_on_hass_started
        )

    @callback
    def _async_on_hass_started(self, _event) -> None:
        """Allow the startup event loop to settle before the first BLE request."""
        self._ak_v3_startup_listener_unsub = None
        self._ak_v3_startup_delay_unsub = async_call_later(
            self._hass, AK_V3_BOOT_SETTLE_SECONDS, self._async_after_startup_delay
        )

    @callback
    def _async_after_startup_delay(self, _now) -> None:
        self._ak_v3_startup_delay_unsub = None
        self._async_create_initialization_task()

    def _async_create_initialization_task(self) -> None:
        task = self._ak_v3_startup_read_task
        if task is None or task.done():
            _LOGGER.info(
                "Scent Assistant initialization starting for %s after HA is RUNNING; platforms ready=%s",
                self._ble_name, self._ak_v3_entity_platforms_ready,
            )
            self._ak_v3_startup_read_task = asyncio.create_task(self.async_setup())

    async def async_cancel_initialization(self) -> None:
        """Cancel startup callbacks and the sole initialization task on unload."""
        for attribute in ("_ak_v3_startup_listener_unsub", "_ak_v3_startup_delay_unsub"):
            unsub = getattr(self, attribute, None)
            if unsub is not None:
                unsub()
                setattr(self, attribute, None)
        task = self._ak_v3_startup_read_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._ak_v3_initialization_scheduled = False

    # ------------------------------------------------------------------
    # BLE connect-on-demand
    # ------------------------------------------------------------------

    async def _ble_connect(
        self,
        *,
        read_ak_state: bool = True,
        keep_connected: bool = False,
        manual_ak_v3_session: bool = False,
        startup_ak_v3_session: bool = False,
    ) -> bool:
        """Connect to BLE and retain a subscribed session while HA is running."""
        if not self._ble_address:
            self._ble_connection_stage = "address_unavailable"
            self._ble_connection_error = "No BLE address is configured"
            return False
        if (
            startup_ak_v3_session
            and (getattr(self, "_ak_v3_action_owner", None) or (None,))[0] == "slot_update"
        ):
            return False

        if self._ble_connected and self._ble_client and self._ble_client.is_connected:
            return True

        # Cooldown after a recent failure: skip the connect attempt
        # entirely and let the next user action retry. Prevents back-
        # to-back retries from wedging a V3 firmware whose GATT stack
        # is already in a bad state.
        loop = asyncio.get_event_loop()
        since_failure = loop.time() - self._ble_last_failure_ts
        if 0 < since_failure < BLE_FAILURE_COOLDOWN_SECONDS:
            self._ble_connection_stage = "cooldown"
            self._ble_connection_error = "Previous BLE connection failure is cooling down"
            _LOGGER.debug(
                "BLE connect to %s skipped — within failure cooldown (%.1fs left)",
                self._ble_name, BLE_FAILURE_COOLDOWN_SECONDS - since_failure,
            )
            return False

        async with self._ble_lock:
            if isinstance(self._protocol, ScentMarketingAkProtocol):
                self._activate_ak_v3_startup_trace()
                self._record_ak_v3_startup_trace("lock_acquired_connect")
            # Double-check after acquiring lock
            if self._ble_connected and self._ble_client and self._ble_client.is_connected:
                return True

            try:
                self._ble_connection_error = None
                _LOGGER.debug("BLE connecting to %s", self._ble_name)
                # Prefer the BLEDevice cached by HA's bluetooth integration:
                # it carries the adapter / proxy details required by the retry
                # connector. A cache miss retains the established direct-address
                # connection path without passing a string to that connector.
                target = self._ble_address
                cached = None
                if self._hass is not None:
                    try:
                        cached = bluetooth.async_ble_device_from_address(
                            self._hass, self._ble_address, connectable=True,
                        )
                    except Exception as err:
                        # HA's cache lookup is optional. Its implementation
                        # can reject stale proxy/discovery data; the address
                        # remains a valid Bleak connection target.
                        self._ble_connection_stage = "discovery_fallback"
                        self._ble_connection_error = f"Cached BLE lookup failed: {err}"
                        _LOGGER.warning(
                            "Cached BLE lookup failed for %s; using its address: %s",
                            self._ble_name, err,
                        )
                    else:
                        if cached is not None:
                            target = cached
                self._ble_connection_stage = "gatt_connecting"
                try:
                    if cached is not None:
                        # Never give establish_connection an address string:
                        # it calls get_connected_devices(), which requires a
                        # real BLEDevice and its .details routing metadata.
                        self._ble_client = await establish_connection(
                            BleakClient,
                            cached,
                            self._ble_name or self._ble_address,
                            max_attempts=BLE_CONNECT_MAX_ATTEMPTS,
                            disconnected_callback=self._on_ble_disconnected,
                        )
                    else:
                        self._ble_client = BleakClient(
                            target, disconnected_callback=self._on_ble_disconnected
                        )
                        await self._ble_client.connect()
                except Exception as err:
                    self._ble_connection_stage = "connection_failed"
                    self._ble_connection_error = str(err)
                    self._record_ak_v3_startup_trace("connection_failed")
                    _LOGGER.warning("BLE connect failed for %s: %s", self._ble_name, err)
                    await self._teardown_ble_client()
                    self._ble_last_failure_ts = loop.time()
                    return False
                self._ble_connected = True
                self._ble_disconnect_expected = False
                self._ble_connection_stage = "gatt_connected"
                self._record_ak_v3_startup_trace("connection_ready")

                # Subscribe to notifications for responses. Without these
                # the AK family can't sync state back to HA, so a silent
                # failure here is worth surfacing — bump it from debug to
                # warning so it shows up in logs and diagnostics. Track
                # whether the subscription took, so the disconnect path
                # can call stop_notify before tearing the link down.
                try:
                    await self._ble_client.start_notify(
                        self._protocol.notify_char_uuid, self._on_ble_notification
                    )
                    self._ble_notify_subscribed = True
                    self._ble_connection_stage = "notifications_subscribed"
                    self._record_ak_v3_startup_trace("notifications_ready")
                except Exception as err:
                    self._ble_notify_subscribed = False
                    self._ble_connection_stage = "notification_failed"
                    self._ble_connection_error = str(err)
                    _LOGGER.warning(
                        "BLE start_notify failed on %s (%s): %s",
                        self._ble_name, self._protocol.notify_char_uuid, err,
                    )

                # Scent Marketing AK family — PIN 8888 login must precede
                # every other write, otherwise the device drops them
                # silently. The response also tells us whether to use the
                # V2 or V3 command set; we wait briefly for it before
                # sending follow-ups so `_v3_mode` is set in time. Once
                # login completes, mirror the official app and read back
                # schedule / power / firmware state so HA entities
                # reflect what the device actually has stored rather
                # than starting from a blank optimistic guess.
                if isinstance(self._protocol, ScentMarketingAkProtocol):
                    if read_ak_state:
                        self._clear_ak_v3_session_state()
                        self._record_ak_v3_startup_trace("login_start")
                    self._protocol.reset_login_state()
                    try:
                        generation = self._ak_v3_startup_generation
                        self._cancel_ak_v3_startup_barrier()
                        self._ak_v3_startup_barrier = AKV3StartupBarrier(generation)
                        self._ak_v3_login = AKV3LoginState(generation)
                        await self._ble_send(self._protocol.build_login_primary())
                        if not await self._async_complete_ak_v3_login(
                            generation,
                            arm_startup_chain=(
                                read_ak_state or manual_ak_v3_session or startup_ak_v3_session
                            ),
                        ):
                            raise asyncio.TimeoutError("AK V3 login did not complete")
                        if read_ak_state:
                            # State read-back: fire queries; responses are
                            # parsed asynchronously by parse_notification.
                            if self._protocol.is_v3:
                                # Startup must not wait for optional metadata
                                # responses. The task reacquires this BLE lock
                                # before emitting C1/C2/C7 then starts the
                                # existing schedule-table reader exactly once.
                                # The config-entry lifecycle owns the single
                                # startup task and runs these reads after every
                                # platform has registered its callback.
                                pass
                            else:
                                for frame in self._protocol.build_read_schedule_queries():
                                    await self._ble_send(frame)
                                    await asyncio.sleep(0.15)
                            if not self._protocol.is_v3:
                                # V2's independent queries are outside the
                                # A316 response-driven V3 schedule sequence.
                                grade_query = self._protocol.build_grade_table_query()
                                if grade_query:
                                    await asyncio.sleep(0.5)
                                    await self._ble_send(grade_query)
                                    await asyncio.sleep(0.4)
                                for frame in self._protocol.build_read_state_queries():
                                    await self._ble_send(frame)
                                    await asyncio.sleep(0.15)
                    except (BleakError, asyncio.TimeoutError, OSError) as err:
                        # Mid-handshake BLE failure leaves the link in
                        # an unknown state — tear down rather than
                        # keeping a half-broken connection scheduled
                        # for idle disconnect, since reusing it tends
                        # to make the V3 firmware's GATT stack wedge.
                        _LOGGER.warning(
                            "Scent Marketing AK handshake failed on %s: %s",
                            self._ble_name, err,
                        )
                        await self._teardown_ble_client()
                        self._ble_last_failure_ts = loop.time()
                        return False

                # Aromely Aro Max — the app opens every session with a
                # session-start frame, then a time sync, then reads back
                # the name / label / schedule. We mirror that so HA starts
                # from the device's real stored state.
                if isinstance(self._protocol, AromelyAroMaxProtocol):
                    try:
                        await self._ble_send(self._protocol.build_session_start())
                        await asyncio.sleep(0.2)
                        ar_time = self._protocol.build_time_sync()
                        if ar_time:
                            await self._ble_send(ar_time)
                            await asyncio.sleep(0.2)
                            self._ble_has_synced_time = True
                        for frame in self._protocol.build_read_queries():
                            await self._ble_send(frame)
                            await asyncio.sleep(0.15)
                    except (BleakError, asyncio.TimeoutError, OSError) as err:
                        _LOGGER.warning(
                            "Aromely Aro Max handshake failed on %s: %s",
                            self._ble_name, err,
                        )
                        await self._teardown_ble_client()
                        self._ble_last_failure_ts = loop.time()
                        return False

                # Time sync on first connection of this session (skipped for
                # protocols that don't support it).
                if not self._ble_has_synced_time:
                    time_sync = self._protocol.build_time_sync()
                    if time_sync:
                        await self._ble_send(time_sync)
                        _LOGGER.info("BLE connected + time synced: %s", self._ble_name)
                    else:
                        _LOGGER.info("BLE connected: %s", self._ble_name)
                    self._ble_has_synced_time = True

                # GW family password handshake. We send unconditionally —
                # the firmware ignores a password write on unprotected
                # devices, and there's no reliable pre-connect way to tell
                # which mode the device is in.
                if self._gw_password and isinstance(
                    self._protocol, ScentMarketingGwProtocol
                ):
                    try:
                        await self._ble_send(self._protocol.build_password(self._gw_password))
                    except Exception as err:
                        _LOGGER.debug("Scent Marketing GW: password send failed: %s", err)

                if self._ble_notify_subscribed:
                    self._ble_connection_stage = "ready"
                return True

            except (BleakError, asyncio.TimeoutError, OSError) as err:
                self._ble_connection_stage = "connection_failed"
                self._ble_connection_error = str(err)
                self._record_ak_v3_startup_trace("connection_failed")
                _LOGGER.warning("BLE connect failed for %s: %s", self._ble_name, err)
                await self._teardown_ble_client()
                self._ble_last_failure_ts = loop.time()
                return False

    def _on_ble_disconnected(self, _client: BleakClient) -> None:
        """Mark device-derived state unavailable and serialize recovery."""
        if self._ble_disconnect_expected:
            return
        self._record_ak_v3_manual_refresh_trace(
            "disconnect_unexpected",
            matcher_armed=self._ak_v3_metadata_read is not None,
            transaction_owner=self._ak_v3_modern_read is not None,
        )
        self._ble_connected = False
        self._ble_notify_subscribed = False
        self._ble_connection_stage = "unexpected_disconnect"
        self._record_ak_v3_startup_trace("disconnect_unexpected")
        self._ak_v3_login = None
        self._cancel_ak_v3_startup_barrier()
        self._ak_v3_current_fields = set()
        self._ak_v3_retained_generation = None
        self._ak_v3_retained_fields = set()
        self._ak_v3_startup_generation += 1
        self._clear_device_derived_state()
        transaction = self._ak_v3_modern_read
        if transaction is None:
            self._notify_state_changed()
        else:
            async def cleanup() -> None:
                await self._async_stop_ak_v3_modern_read(transaction)
                self._notify_state_changed()
            asyncio.create_task(cleanup())
        task = self._ble_reconnect_task
        if task is None or task.done():
            self._ble_reconnect_task = asyncio.create_task(self._async_reconnect_after_disconnect())

    async def _async_reconnect_after_disconnect(self) -> None:
        """Reconnect once at a time; every AK V3 recovery starts a full session."""
        await asyncio.sleep(BLE_FAILURE_COOLDOWN_SECONDS)
        if await self._ble_connect(read_ak_state=True, keep_connected=True):
            if isinstance(self._protocol, ScentMarketingAkProtocol) and self._protocol.is_v3:
                await self._async_start_ak_v3_startup_reads()

    async def _teardown_ble_client(self, *, reason: str = "error") -> None:
        """Cleanly release the BLE client.

        Always stop notifications before disconnecting (some firmwares —
        notably the Scent Marketing V3 ESP32 — get into a stuck GATT
        state if a client disconnects without unsubscribing first), then
        disconnect, then drop the client reference so the next connect
        attempt starts fresh. Safe to call when the client is already
        gone; logs at debug.

        Caller must hold `_ble_lock` if called from anywhere other than
        the connect / delayed-disconnect paths (which already do).
        """
        client = self._ble_client
        transaction = self._ak_v3_modern_read
        self._record_ak_v3_startup_trace("disconnect_teardown")
        self._ak_v3_login = None
        self._cancel_ak_v3_startup_barrier()
        self._ble_disconnect_expected = True
        if client is not None and self._ble_notify_subscribed:
            try:
                await client.stop_notify(self._protocol.notify_char_uuid)
            except Exception as err:
                _LOGGER.debug(
                    "BLE stop_notify on %s failed during teardown (%s): %s",
                    self._ble_name, reason, err,
                )
        self._ble_notify_subscribed = False
        if client is not None:
            try:
                if client.is_connected:
                    await client.disconnect()
                    _LOGGER.debug("BLE disconnected (%s): %s", reason, self._ble_name)
            except Exception as err:
                _LOGGER.debug(
                    "BLE disconnect on %s failed during teardown (%s): %s",
                    self._ble_name, reason, err,
                )
        self._ble_client = None
        self._ble_connected = False
        if transaction is not None:
            await self._async_stop_ak_v3_modern_read(transaction)

    async def _ble_send(self, data: bytes) -> bool:
        """Send a command via BLE.

        Protocols may return more than one on-wire chunk per command — the
        Scent Marketing GW family for instance needs (nonce, seq)-prefixed
        18-byte chunks. We delegate the split decision to the protocol and
        write each chunk sequentially.

        Raises `BleakError`/`asyncio.TimeoutError`/`OSError` on write
        failure (GATT-133 on Android surfaces here too). Callers wrap
        this so they can decide whether to tear the BLE client down —
        leaving a half-broken handle around made V3 firmwares unable
        to be reached by *any* client until a power cycle.
        """
        if not self._ble_client or not self._ble_client.is_connected:
            return False
        diagnostic_data = data
        is_ak_password_change = (
            isinstance(self._protocol, ScentMarketingAkProtocol) and data[:1] == b"\x0F"
        )
        is_gw_password_check = (
            isinstance(self._protocol, ScentMarketingGwProtocol)
            and SM_GW_DP_PASSWORD.to_bytes(2, "big") in data
        )
        if is_ak_password_change or is_gw_password_check:
            # Password commands must never enter diagnostics or the command
            # ring buffer. The original bytes still go to the BLE transport.
            diagnostic_data = b"\x0F" + b"\x00" * (len(data) - 1)
        self._record_ak_v3_startup_trace("TX", data)
        self._record_ak_v3_modern_diagnostic_event("TX", diagnostic_data)
        chunks = self._protocol.wire_chunks(data) if data else []
        if chunks:
            self._recent_commands.append(self._safe_ble_history_record("TX", diagnostic_data))
            if len(self._recent_commands) > 10:
                del self._recent_commands[0]
        for chunk in chunks:
            await self._ble_client.write_gatt_char(
                self._protocol.write_char_uuid, chunk, response=True
            )
        self._record_ak_v3_startup_trace("write_complete", data)
        return True

    async def _ble_execute(self, data: bytes) -> bool:
        """Connect, send command, schedule disconnect."""
        if not await self._ble_connect():
            return False
        try:
            success = await self._ble_send(data)
        except (BleakError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.warning("BLE write failed on %s: %s", self._ble_name, err)
            self._ble_last_failure_ts = asyncio.get_event_loop().time()
            async with self._ble_lock:
                await self._teardown_ble_client(reason="write-failure")
            return False
        # Wait briefly for notification response.
        await asyncio.sleep(1.0)
        return success

    async def async_update_ak_v3_slot(
        self, endpoint: int, slot: int, *, rollback_on_failure: bool = True, **changes: int | bool,
    ) -> dict:
        """Perform the one proven A316 schedule write/read comparison flow."""
        if changes.get("delete"):
            return self._ak_v3_transaction_error("AK V3 schedule deletion is unsupported")
        if not isinstance(rollback_on_failure, bool):
            return self._ak_v3_transaction_error("Invalid AK V3 rollback option")
        return await self._async_update_ak_v3_slot(
            endpoint, slot, allow_create=False, rollback_on_failure=rollback_on_failure, **changes
        )

    async def _async_update_ak_v3_slot(
        self, endpoint: int, slot: int, *, allow_create: bool, allow_delete: bool = False,
        rollback_on_failure: bool = True, **changes: int | bool,
    ) -> dict:
        """Private transactional encoder path used by lifecycle verification only."""
        owner = self._claim_ak_v3_action("slot_update")
        if owner is None:
            return self._ak_v3_transaction_error("AK V3 action is busy")
        try:
            return await self._async_update_ak_v3_slot_owned(
                owner, endpoint, slot, allow_create=allow_create, allow_delete=allow_delete,
                rollback_on_failure=rollback_on_failure, **changes
            )
        finally:
            self._release_ak_v3_action(self._current_ak_v3_action_owner(owner))

    def _ak_v3_slot_write_ready(self, owner: tuple[str, int, int]) -> bool:
        """Allow 2A only from this action's live accepted V3 login."""
        login = getattr(self, "_ak_v3_login", None)
        client = getattr(self, "_ble_client", None)
        return (
            self._ak_v3_action_is_owner(owner)
            and isinstance(self._protocol, ScentMarketingAkProtocol)
            and self._protocol.is_v3
            and self._ble_connected
            and self._ble_notify_subscribed
            and client is not None
            and client.is_connected
            and login is not None
            and login.generation == self._ak_v3_startup_generation
            and login.accepted
        )

    async def _async_update_ak_v3_slot_owned(
        self, owner: tuple[str, int, int], endpoint: int, slot: int,
        *, allow_create: bool, allow_delete: bool = False, rollback_on_failure: bool = True,
        **changes: int | bool,
    ) -> dict:
        """Run a claimed mutation without allowing another action to queue."""
        if not isinstance(self._protocol, ScentMarketingAkProtocol) or not self._protocol.is_v3:
            return self._ak_v3_transaction_error("AK V3 session is not established")
        if endpoint != 1 or not 1 <= slot <= 5:
            return self._ak_v3_transaction_error("Ultra Max Tower supports endpoint 1 only")
        allowed = {
            "enabled", "start_hour", "start_minute", "end_hour", "end_minute",
            "days_mask", "mode", "intensity", "work_seconds", "pause_seconds", "delete",
        }
        if not changes or not set(changes).issubset(allowed):
            return self._ak_v3_transaction_error("Invalid AK V3 schedule update")

        identity = (1, slot)
        if identity in getattr(self, "_ak_v3_restored_identities", set()):
            return self._ak_v3_transaction_error(
                "AK V3 schedule requires fresh physical confirmation before writing"
            )
        if not hasattr(self, "_ak_v3_transaction_lock"):
            self._ak_v3_transaction_lock = asyncio.Lock()
        async with self._ak_v3_transaction_lock:
            try:
                if not self._ak_v3_slot_write_ready(owner):
                    if self._ble_connected or getattr(self, "_ble_client", None) is not None:
                        return self._ak_v3_transaction_error("AK V3 session is not established")
                    if not await self._ble_connect(read_ak_state=False, keep_connected=True):
                        return self._ak_v3_transaction_error("BLE connection failed before baseline read")
                    owner = self._advance_ak_v3_action_owner(owner)
                    if owner is None:
                        return self._ak_v3_transaction_error("AK V3 action ownership was lost")
                if not self._ak_v3_slot_write_ready(owner):
                    return self._ak_v3_transaction_error("AK V3 session is not established")
                baseline = await self._async_read_ak_v3_modern_table(1, owner=owner)
                before = baseline.get(identity)
                creating = before is not None and before.is_empty
                if creating and not allow_create:
                    return self._ak_v3_transaction_error("AK V3 schedule creation is unsupported")
                if not self._ak_v3_schedule_complete(before, identity) and not creating:
                    return self._ak_v3_transaction_error("Baseline table did not contain the requested slot")
                if creating and not {
                    "enabled", "start_hour", "start_minute", "end_hour", "end_minute",
                    "days_mask", "mode", "intensity", "work_seconds", "pause_seconds",
                }.issubset(changes):
                    return self._ak_v3_transaction_error("AK V3 slot creation requires a complete schedule")

                if changes.get("mode") == 1 and not self.supports_ak_v3_custom_mode:
                    return self._ak_v3_transaction_error(
                        "AK V3 custom mode is not supported by device capabilities"
                    )
                deleting = bool(changes.pop("delete", False))
                if deleting and not allow_delete:
                    return self._ak_v3_transaction_error("AK V3 schedule deletion is unsupported")
                if deleting:
                    write = self._protocol.build_v3_schedule_delete(before)
                    verification_changes = {}
                else:
                    write = self._protocol.build_v3_schedule_update(before, **changes)
                    verification_changes = changes
                if not self._ak_v3_slot_write_ready(owner):
                    return self._ak_v3_transaction_error("AK V3 session is not established")
                if not await self._ble_send(write):
                    return self._ak_v3_transaction_error("AK V3 schedule write was not sent")
                after = await self._async_read_ak_v3_modern_table(1, owner=owner)
            except (BleakError, asyncio.TimeoutError, OSError, ValueError) as err:
                return self._ak_v3_transaction_error(
                    f"AK V3 schedule result is inconclusive: {err}", status="inconclusive"
                )

            observed = after.get(identity)
            if deleting and observed is not None and observed.is_empty and not observed.present and not observed.enabled:
                status = "success"
            elif not deleting and self._ak_v3_target_matches(before, observed, verification_changes):
                status = "success"
            elif observed is not None and observed.raw_frame == before.raw_frame:
                status = "unchanged"
            else:
                status = "inconclusive"

            if status != "success" and rollback_on_failure:
                # `before` is frozen and its raw frame is immutable. Restore
                # it, then perform a full normal table read before reporting.
                try:
                    if self._ak_v3_slot_write_ready(owner):
                        await self._ble_send(self._protocol.build_v3_schedule_update(before))
                        await self._async_read_ak_v3_modern_table(1, owner=owner)
                except (BleakError, asyncio.TimeoutError, OSError, ValueError):
                    pass

            result = {
                "success": status == "success",
                "status": status,
                "endpoint": 1,
                "slot": slot,
                "write_frame": write.hex(),
                "baseline_frame": before.raw_frame.hex(),
                "rollback_on_failure": rollback_on_failure,
            }
            return result

    async def async_create_ak_v3_slot(self, **schedule: int | bool) -> dict:
        """Public creation remains unsupported outside verification."""
        return self._ak_v3_transaction_error("AK V3 schedule creation is unsupported")

    async def async_delete_ak_v3_slot(self, endpoint: int, slot: int) -> dict:
        """Public deletion remains unsupported outside verification."""
        return self._ak_v3_transaction_error("AK V3 schedule deletion is unsupported")

    async def async_verify_ak_v3_slot_lifecycle(self, **schedule: int | bool) -> dict:
        """Run the protected empty-slot create/read/delete/read verification flow."""
        required = {
            "enabled", "start_hour", "start_minute", "end_hour", "end_minute",
            "days_mask", "mode", "intensity", "work_seconds", "pause_seconds",
        }
        if set(schedule) != required:
            return self._ak_v3_transaction_error("A complete AK V3 verification schedule is required")
        if not await self._ble_connect(read_ak_state=False, keep_connected=True):
            return self._ak_v3_transaction_error("BLE connection failed before lifecycle verification")
        baseline = await self._async_read_ak_v3_modern_table(1)
        empty = next((item for item in baseline.values() if item.is_empty), None)
        if empty is None:
            return self._ak_v3_transaction_error("No physical empty AK V3 slot is available")
        identity = (1, empty.slot_id)
        created = await self._async_update_ak_v3_slot(1, empty.slot_id, allow_create=True, **schedule)
        if not created.get("success"):
            return created
        deleted = await self._async_update_ak_v3_slot(
            1, empty.slot_id, allow_create=False, allow_delete=True, delete=True
        )
        if deleted.get("success"):
            self._ak_v3_slot_lifecycle_verified = True
            return {"success": True, "endpoint": 1, "slot": empty.slot_id, "verified": True}
        # Restore the original physical empty record and verify it before reporting failure.
        try:
            await self._ble_send(self._protocol.build_v3_schedule_update(empty))
            restored = await self._async_read_ak_v3_modern_table(1)
            if restored.get(identity) != empty:
                return self._ak_v3_transaction_error("AK V3 lifecycle verification failed and rollback was inconclusive")
        except (BleakError, asyncio.TimeoutError, OSError, ValueError):
            return self._ak_v3_transaction_error("AK V3 lifecycle verification failed and rollback was inconclusive")
        return self._ak_v3_transaction_error("AK V3 lifecycle verification failed; baseline was restored")

    def _ak_v3_transaction_error(
        self, error: str, *, identity: tuple[int, int] | None = None, **details: object
    ) -> dict:
        """Return an actionable error and refresh entities/cards with it."""
        self._state.ak_v3_transaction_error = error
        if identity is not None:
            self._set_ak_v3_slot_lifecycle(identity, AKV3SlotLifecycle.ERROR, notify=False)
        self._notify_state_changed()
        return {"success": False, "error": error, **details}

    def ak_v3_slot_lifecycle(self, endpoint: int, slot: int) -> AKV3SlotLifecycle:
        """Return the transient lifecycle for one exact slot identity."""
        identity = (endpoint, slot)
        lifecycle = getattr(self, "_ak_v3_slot_lifecycle", {}).get(identity)
        if lifecycle is not None:
            return lifecycle
        if self.protocol_is_v3:
            return AKV3SlotLifecycle.VERIFYING
        return self._ak_v3_slot_lifecycle_from_schedule(
            self._state.ak_v3_schedules.get(identity), identity
        )

    def _set_ak_v3_slot_lifecycle(
        self, identity: tuple[int, int], lifecycle: AKV3SlotLifecycle, *, notify: bool = True
    ) -> None:
        if not hasattr(self, "_ak_v3_slot_lifecycle"):
            self._ak_v3_slot_lifecycle = {}
        self._ak_v3_slot_lifecycle[identity] = lifecycle
        if notify:
            self._notify_state_changed()

    def _ak_v3_slot_lifecycle_from_schedule(
        self, schedule: object, identity: tuple[int, int]
    ) -> AKV3SlotLifecycle:
        if identity in self._ak_v3_restored_identities:
            return AKV3SlotLifecycle.STALE
        if not self._ak_v3_schedule_complete(schedule, identity):
            return AKV3SlotLifecycle.STALE
        return AKV3SlotLifecycle.PRESENT_ENABLED if schedule.enabled else AKV3SlotLifecycle.PRESENT_DISABLED

    def _ak_v3_schedule_complete(self, schedule: object, identity: tuple[int, int]) -> bool:
        """Accept only a fully decoded record for its requested identity."""
        if (
            schedule is None
            or schedule.is_empty
            or (schedule.endpoint_id, schedule.slot_id) != identity
        ):
            return False
        try:
            self._protocol.build_v3_schedule_update(schedule)
        except ValueError:
            return False
        return True

    async def _async_restore_ak_v3_schedules(self) -> None:
        """Restore only complete, identity-matched records as stale evidence."""
        if getattr(self, "_ak_v3_store", None) is None:
            return
        try:
            payload = await self._ak_v3_store.async_load()
        except Exception as err:
            _LOGGER.warning("AK V3 schedule cache could not be loaded: %s", err)
            return
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return
        restored: dict[tuple[int, int], object] = {}
        for item in payload.get("slots", []):
            if not isinstance(item, dict):
                continue
            try:
                endpoint, slot = int(item["endpoint"]), int(item["slot"])
                raw = bytes.fromhex(item["raw_frame"])
                schedule = self._protocol._parse_v3_schedule(raw)
            except (KeyError, TypeError, ValueError):
                continue
            identity = (endpoint, slot)
            if not (1 <= endpoint <= 0xFF and 1 <= slot <= 5) or not self._ak_v3_schedule_complete(schedule, identity):
                continue
            restored[identity] = schedule
            self._ak_v3_confirmation_metadata[identity] = {
                "confirmed_at": item.get("confirmed_at"),
                "source": item.get("source", "fresh_physical"),
                "physical_status": "restored_stale",
            }
        self._state.ak_v3_schedules.update(restored)
        self._ak_v3_restored_identities.update(restored)
        for identity in restored:
            self._set_ak_v3_slot_lifecycle(identity, AKV3SlotLifecycle.STALE, notify=False)

    def _persist_ak_v3_schedules(self) -> None:
        """Persist validated records only; never session or credential data."""
        if getattr(self, "_ak_v3_store", None) is None:
            return
        self._hass.async_create_task(self._async_persist_ak_v3_schedules())

    async def _async_persist_ak_v3_schedules(self) -> None:
        """Serialize Store writes so a completed logical save cannot be overwritten stale."""
        if getattr(self, "_ak_v3_store", None) is None:
            return
        if not hasattr(self, "_ak_v3_store_lock"):
            self._ak_v3_store_lock = asyncio.Lock()
        async with self._ak_v3_store_lock:
            slots = []
            for identity, schedule in self._state.ak_v3_schedules.items():
                if self._ak_v3_schedule_complete(schedule, identity):
                    slots.append({"endpoint": identity[0], "slot": identity[1],
                                  "raw_frame": schedule.raw_frame.hex(),
                                  **self._ak_v3_confirmation_metadata.get(identity, {})})
            await self._ak_v3_store.async_save({"version": 1, "slots": slots})

    async def async_preserve_ak_v3_logical_slot(self, endpoint: int, slot: int, **fields: object) -> dict:
        """Persist a user-supplied logical slot without any BLE activity."""
        if not isinstance(self._protocol, ScentMarketingAkProtocol) or not self._protocol.is_v3:
            return {"success": False, "error": "AK V3 session is not established"}
        identity = (endpoint, slot)
        try:
            work, pause = int(fields["work_seconds"]), int(fields["pause_seconds"])
            raw = bytes([0x4A, endpoint, 0x02, 0x03 if fields["enabled"] else 0x01, slot, slot,
                         0x03 if fields["enabled"] else 0x01, int(fields["start_hour"]), int(fields["start_minute"]),
                         int(fields["end_hour"]), int(fields["end_minute"]), int(fields["days_mask"]),
                         int(fields["mode"]), int(fields["intensity"]), work >> 8, work & 0xFF,
                         pause >> 8, pause & 0xFF])
            schedule = self._protocol._parse_v3_schedule(raw)
        except (KeyError, TypeError, ValueError):
            return {"success": False, "error": "Invalid logical schedule"}
        if not self._ak_v3_schedule_complete(schedule, identity):
            return {"success": False, "error": "Invalid logical schedule"}
        self._state.ak_v3_schedules[identity] = schedule
        self._ak_v3_restored_identities.add(identity)
        self._ak_v3_confirmation_metadata[identity] = {
            "confirmed_at": datetime.now().astimezone().isoformat(), "source": "explicit_user_logical",
        }
        self._set_ak_v3_slot_lifecycle(identity, AKV3SlotLifecycle.STALE, notify=False)
        await self._async_persist_ak_v3_schedules()
        self._notify_state_changed()
        return {"success": True, "endpoint": endpoint, "slot": slot,
                "provenance": "explicit_user_logical", "write_eligible": False,
                "record": self._ak_v3_schedule_record(schedule)}

    def _ak_v3_record_valid(self, schedule: object) -> bool:
        """Validate a received record without treating any zero value as absent."""
        if schedule is None or len(schedule.raw_frame) != 18:
            return False
        return (
            schedule.protocol_byte_2 == 0x02
            and schedule.fan_state is not None
            and 0 <= schedule.enable_state_byte <= 0x07
            and 0 <= schedule.start_hour <= 23
            and 0 <= schedule.end_hour <= 23
            and 0 <= schedule.start_minute <= 59
            and 0 <= schedule.end_minute <= 59
            and 0 <= schedule.days_mask <= 0x7F
            and schedule.mode in (0, 1)
            and 0 <= schedule.intensity <= 20
            and schedule.work_seconds is not None
            and schedule.pause_seconds is not None
            and 0 <= schedule.work_seconds <= 0xFFFF
            and 0 <= schedule.pause_seconds <= 0xFFFF
        )

    def _arm_ak_v3_modern_collector(self) -> AKV3ModernRead:
        """Arm the V3 direct-push 4A collector before its triggering 21 write."""
        self._ak_v3_read_transaction_id = getattr(self, "_ak_v3_read_transaction_id", 0) + 1
        transaction = getattr(self, "_ak_v3_modern_diagnostic_trace", None)
        if transaction is None:
            transaction = AKV3ModernRead(self._ak_v3_read_transaction_id)
        else:
            transaction.transaction_id = self._ak_v3_read_transaction_id
            transaction.expected_slot = 1
            transaction.endpoint = None
            transaction.records.clear()
            transaction.completion_event.clear()
            transaction.records_complete_event.clear()
            transaction.completion_event.clear()
            transaction.aggregate_flags = None
            self._transition_ak_v3_modern_read(transaction, "slot", "armed direct 4A collector")
        self._ak_v3_modern_read = transaction
        return transaction

    async def _async_start_ak_v3_modern_read(self) -> None:
        """Compatibility entry point for arming a direct-push V3 collector."""
        self._arm_ak_v3_modern_collector()

    async def _async_start_ak_v3_modern_read_locked(self) -> None:
        """Compatibility entry point for callers already holding the BLE lock."""
        self._arm_ak_v3_modern_collector()

    async def _async_start_ak_v3_startup_reads(self) -> None:
        """Run the V3 metadata chain, then the separate schedule transaction."""
        if (getattr(self, "_ak_v3_action_owner", None) or (None,))[0] == "slot_update":
            return
        chain_failed = False
        transaction: AKV3ModernRead | None = None
        try:
            barrier = getattr(self, "_ak_v3_startup_barrier", None)
            if barrier is not None:
                await barrier.armed.wait()
                if (
                    self._ak_v3_startup_barrier is not barrier
                    or barrier.generation != self._ak_v3_startup_generation
                    or barrier.chain is None
                ):
                    return
            async with self._ble_lock:
                self._record_ak_v3_startup_trace("lock_acquired_startup")
                if (getattr(self, "_ak_v3_action_owner", None) or (None,))[0] == "slot_update":
                    return
                if not self._ble_connected or not self._ble_client or not self._ble_client.is_connected:
                    return
                if not self._ble_notify_subscribed or not getattr(self, "_ak_v3_entity_platforms_ready", False):
                    _LOGGER.warning("AK V3 startup prerequisites were not ready for %s", self._ble_name)
                    return
                chain = barrier.chain if barrier is not None else self._ak_v3_startup_chain
            if chain is not None:
                try:
                    await asyncio.wait_for(chain.completed.wait(), AK_V3_TRANSACTION_READ_SECONDS)
                except asyncio.TimeoutError:
                    self._record_ak_v3_startup_trace("chain_timeout")
                    _LOGGER.info("AK V3 startup metadata chain completed without optional terminal 52")
                finally:
                    if self._ak_v3_startup_chain is chain:
                        self._ak_v3_startup_chain = None
                    if barrier is not None and self._ak_v3_startup_barrier is barrier:
                        if not barrier.released.is_set():
                            barrier.failed = chain.failed
                            barrier.released.set()
                            self._record_ak_v3_startup_trace("startup_barrier_released")
                if chain.failed:
                    chain_failed = True
                    self._invalidate_ak_v3_chain_metadata(chain)
            async with self._ble_lock:
                self._record_ak_v3_startup_trace("lock_acquired_schedule")
                if (getattr(self, "_ak_v3_action_owner", None) or (None,))[0] == "slot_update":
                    return
                transaction = self._ak_v3_modern_read
                if transaction is None:
                    transaction = self._arm_ak_v3_modern_collector()
            if transaction is not None:
                await asyncio.wait_for(
                    transaction.records_complete_event.wait(), AK_V3_TRANSACTION_READ_SECONDS
                )
                try:
                    await asyncio.wait_for(
                        transaction.completion_event.wait(), AK_V3_ACK_FINALIZE_SECONDS
                    )
                except asyncio.TimeoutError:
                    pass
            if chain_failed:
                missing_oil = {opcode for opcode in (0x4B, 0x50) if opcode not in chain.accepted_frames}
                if missing_oil:
                    await self._async_refresh_ak_v3_oil_calculation_state(missing_oil)
        except (BleakError, asyncio.TimeoutError, OSError) as err:
            self._record_ak_v3_startup_trace("startup_failed")
            _LOGGER.warning("AK V3 startup reads failed on %s: %s", self._ble_name, err)
        finally:
            if transaction is not None:
                await self._async_stop_ak_v3_modern_read(transaction)
            self._ak_v3_startup_trace_active = False
            self._notify_state_changed()

    def _ak_v3_required_startup_opcodes(self) -> tuple[tuple[int, str], ...]:
        """Return the response stages required by the authenticated capabilities."""
        required = []
        if self._state.ak_v3_has_global_control:
            required.append((0x4D, "control_4d"))
        if self._state.ak_v3_has_oil:
            required.extend(((0x4B, "oil_4b"), (0x50, "oil_50")))
        if self._state.ak_v3_has_custom_mode:
            required.extend(((0x46, "metadata_46"), (0x47, "metadata_47")))
        if self._state.ak_v3_has_lamp:
            required.extend(((0x51, "metadata_51"), (0x52, "metadata_52")))
        return tuple(required)

    def _ak_v3_manual_refresh_failure(self, chain: AKV3StartupChain) -> str:
        """Name the first genuinely required metadata stage not accepted."""
        for opcode, stage in self._ak_v3_required_startup_opcodes():
            if opcode not in chain.accepted_frames:
                return stage
        return "metadata"

    def _ak_v3_startup_chain_complete(self, chain: AKV3StartupChain) -> bool:
        """Return whether accepted frames satisfy this device's metadata graph."""
        required = self._ak_v3_required_startup_opcodes()
        return bool(required) and all(opcode in chain.accepted_frames for opcode, _ in required)

    def _advance_ak_v3_startup_chain(self, raw: bytes, updates: dict) -> bool:
        """Dispatch valid reply-mode frames once within the active generation."""
        continuations = {
            0x41: b"\xC1", 0x42: b"\xC2", 0x43: b"\xC3", 0x44: b"\xC4",
            0x45: b"\xC5", 0x46: b"\xC6", 0x47: b"\xC7", 0x48: b"\xC8",
            0x4B: b"\xCB", 0x4C: b"\xCC", 0x4D: b"\xCD", 0x4E: b"\xCE",
            0x50: b"\xD0", 0x51: b"\xD1",
        }
        freshness = {
            0x42: "device_name", 0x43: "device_label", 0x44: "firmware_version",
            0x45: "model_code", 0x46: "grade_limits", 0x48: "oil_names",
            0x4B: "oil_current_ml", 0x50: "oil_consumption_mlh", 0x51: "light_on",
            0x4D: "power",
        }
        chain = getattr(self, "_ak_v3_startup_chain", None)
        opcode = raw[0] if raw else None
        barrier = getattr(self, "_ak_v3_startup_barrier", None)
        if (
            opcode not in (*continuations, 0x52)
            or chain is None
            or barrier is None
            or chain.generation != self._ak_v3_startup_generation
            or barrier.chain is not chain
            or barrier.generation != chain.generation
            or not chain.post_21_sent
            or barrier.released.is_set()
        ):
            return False

        if not self._ak_v3_startup_frame_valid(opcode, raw):
            chain.rejected_opcodes.add(opcode)
            self._record_ak_v3_startup_trace("malformed_response", raw)
            return False
        previous = chain.accepted_frames.get(opcode)
        if previous is not None:
            if previous != raw:
                chain.rejected_opcodes.add(opcode)
            self._record_ak_v3_startup_trace(
                "duplicate_response" if previous == raw else "conflicting_response", raw
            )
            return previous == raw
        chain.accepted_frames[opcode] = raw
        if opcode == 0x4D:
            self._ak_v3_power_generation = chain.generation
        self._record_ak_v3_manual_refresh_trace(
            "metadata_accepted", opcode=f"{opcode:02X}", length=len(raw)
        )
        if opcode == 0x46:
            chain.grade_max = raw[1]
        elif opcode == 0x47:
            chain.grade_record_count = (len(raw) - 1) // 4
        self._reconcile_ak_v3_startup_grade_freshness(chain)
        field = freshness.get(opcode)
        if field is not None:
            self._state.ak_v3_metadata_available.add(field)
            self._retain_ak_v3_fields(field)
        if opcode == 0x4B:
            self._retain_ak_v3_fields("oil_status", "oil_current_ml", "oil_max_ml", "oil_remaining")
        elif opcode == 0x50:
            self._retain_ak_v3_fields("oil_consumption_mlh")
        if opcode == 0x52:
            chain.completed.set()
            self._record_ak_v3_startup_trace("chain_terminal", raw)
            return True

        if self._ak_v3_startup_chain_complete(chain):
            chain.completed.set()
            self._record_ak_v3_startup_trace("chain_complete", raw)
            return True

        frame = continuations[opcode]
        if frame == b"\xD0" and not self._state.ak_v3_has_lamp:
            # D0 queries lamp state. A non-lamp A316 ends at 50, with no
            # 51/52 tail to wait for.
            chain.completed.set()
            self._record_ak_v3_startup_trace("chain_complete", raw)
            return True
        chain.continuations_sent.add(opcode)
        self._record_ak_v3_startup_trace("chain_transition", raw)

        async def send() -> None:
            async with self._ble_lock:
                self._record_ak_v3_startup_trace("lock_acquired_chain")
                if (
                    self._ak_v3_startup_chain is chain
                    and barrier.chain is chain
                    and not chain.failed
                    and not barrier.released.is_set()
                ):
                    self._record_ak_v3_manual_refresh_trace(
                        "continuation_attempted", opcode=f"{frame[0]:02X}", expected=f"{opcode:02X}"
                    )
                    if not await self._ble_send(frame):
                        chain.failed = True
                        chain.completed.set()
                    else:
                        self._record_ak_v3_manual_refresh_trace(
                            "continuation_completed", opcode=f"{frame[0]:02X}"
                        )

        asyncio.create_task(send())
        return True

    @staticmethod
    def _ak_v3_startup_frame_valid(opcode: int, raw: bytes) -> bool:
        """Validate the APK's independently dispatched chain reply shapes."""
        if opcode in (0x41, 0x52):
            return len(raw) == 1
        if opcode in (0x4C, 0x4E):
            return len(raw) >= 1
        if opcode == 0x46:
            return len(raw) >= 10 and raw[1] > 0
        if opcode == 0x47:
            return len(raw) > 1 and (len(raw) - 1) % 4 == 0
        if opcode == 0x48:
            return len(raw) >= 17 and (len(raw) - 1) % 16 == 0
        if opcode == 0x4B:
            return len(raw) == 6
        if opcode == 0x50:
            return len(raw) >= 8 and (len(raw) - 1) % 7 == 0
        if opcode == 0x51:
            return len(raw) >= 4
        if opcode in (0x42, 0x43, 0x45):
            return len(raw) >= 2
        if opcode == 0x44:
            return len(raw) >= 17
        if opcode == 0x4D:
            return len(raw) >= 2
        return False

    def _reconcile_ak_v3_startup_grade_freshness(self, chain: AKV3StartupChain) -> None:
        """Expose grade data only after independently received frames agree."""
        if chain.grade_max is None or chain.grade_record_count is None:
            return
        if chain.grade_max == chain.grade_record_count:
            self._state.ak_v3_metadata_available.add("grade_table")
            self._retain_ak_v3_fields("grade_limits", "grade_table")
            return
        self._state.grade_table = None
        self._state.ak_v3_metadata_available.discard("grade_table")

    async def _async_refresh_ak_v3_metadata(self) -> None:
        """Read V3 metadata in serialized, response-validated transactions.

        A response lacks a request ID, so a timeout is terminal for that field:
        reissuing the same opcode could accept a late prior response. The next
        field and the normal schedule reader still proceed.
        """
        if not isinstance(self._protocol, ScentMarketingAkProtocol) or not self._protocol.is_v3:
            return
        if not await self._async_wait_for_ak_v3_startup_barrier():
            return
        requests = (
            (b"\xC1", SM_AK_RESP_DEVICE_NAME_V3, "device_name"),
            (b"\xC2", SM_AK_RESP_LABEL_V3, "device_label"),
        )
        for command, response_opcode, state_field in requests:
            transaction = AKV3MetadataRead(command, response_opcode, state_field)
            self._ak_v3_metadata_read = transaction
            self._state.ak_v3_metadata_available.discard(state_field)
            try:
                if not await self._ble_send(command):
                    raise BleakError(f"AK V3 metadata read was not sent: {command.hex()}")
                await asyncio.wait_for(
                    transaction.response_event.wait(), AK_V3_METADATA_RESPONSE_SECONDS
                )
            except (BleakError, asyncio.TimeoutError, OSError) as err:
                _LOGGER.warning(
                    "AK V3 metadata read %s/%02X failed: %s",
                    command.hex().upper(), response_opcode, err,
                )
            finally:
                if self._ak_v3_metadata_read is transaction:
                    self._ak_v3_metadata_read = None
            if transaction.accepted:
                self._state.ak_v3_metadata_available.add(state_field)
                _LOGGER.info(
                    "AK V3 metadata read completed: %s/%02X",
                    command.hex().upper(), response_opcode,
                )
            self._notify_state_changed()

    async def _async_refresh_ak_v3_oil_calculation_state(
        self, missing_opcodes: set[int] | None = None,
    ) -> None:
        """Read C8/CE one at a time after the completed grade transaction."""
        if not await self._async_wait_for_ak_v3_startup_barrier():
            return
        try:
            async with self._ble_lock:
                if not self._ble_connected or not self._ble_client or not self._ble_client.is_connected:
                    return
                self._ak_v3_calculation_read_generation = (
                    getattr(self, "_ak_v3_calculation_read_generation", 0) + 1
                )
                generation = self._ak_v3_calculation_read_generation
                for command, opcode, field, minimum in (
                    (b"\xC8", 0x4B, "oil_current_ml", 6),
                    (b"\xCE", 0x50, "oil_consumption_mlh", 8),
                ):
                    if missing_opcodes is not None and opcode not in missing_opcodes:
                        continue
                    self._invalidate_ak_v3_calculation_field(field)
                    transaction = AKV3MetadataRead(
                        command, opcode, field, minimum, generation=generation
                    )
                    self._ak_v3_metadata_read = transaction
                    try:
                        if not await self._ble_send(command):
                            return
                        await asyncio.wait_for(
                            transaction.response_event.wait(), AK_V3_METADATA_RESPONSE_SECONDS
                        )
                    except (BleakError, asyncio.TimeoutError, OSError) as err:
                        _LOGGER.warning("AK V3 calculation read %s/%02X failed: %s", command.hex().upper(), opcode, err)
                    finally:
                        if self._ak_v3_metadata_read is transaction:
                            self._ak_v3_metadata_read = None
                    if transaction.accepted:
                        _LOGGER.info("AK V3 startup read validated: %s/%02X", command.hex().upper(), opcode)
        except (BleakError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.warning("AK V3 oil calculation state read failed on %s: %s", self._ble_name, err)

    async def _async_refresh_ak_v3_grade_table(self) -> None:
        """Read C5/46 then immediately C6/47 with exact response lengths."""
        if not await self._async_wait_for_ak_v3_startup_barrier():
            return
        try:
            async with self._ble_lock:
                if not self._ble_connected or not self._ble_client or not self._ble_client.is_connected:
                    return
                self._invalidate_ak_v3_calculation_field("grade_table")
                limits = AKV3MetadataRead(
                    b"\xC5", 0x46, "grade_limits", expected_response_length=10
                )
                self._ak_v3_metadata_read = limits
                try:
                    if not await self._ble_send(limits.command):
                        return
                    await asyncio.wait_for(
                        limits.response_event.wait(), AK_V3_METADATA_RESPONSE_SECONDS
                    )
                except (BleakError, asyncio.TimeoutError, OSError) as err:
                    _LOGGER.warning("AK V3 grade limits C5/46 failed: %s", err)
                    return
                finally:
                    if self._ak_v3_metadata_read is limits:
                        self._ak_v3_metadata_read = None
                if not limits.accepted or self._state.grade_limits is None:
                    return
                max_grade = self._state.grade_limits[0]
                if max_grade <= 0:
                    _LOGGER.warning("AK V3 grade limits C5/46 returned invalid maxGrade %s", max_grade)
                    self._state.grade_limits = None
                    return
                grade = AKV3MetadataRead(
                    b"\xC6", 0x47, "grade_table",
                    expected_response_length=1 + 4 * max_grade,
                )
                self._ak_v3_metadata_read = grade
                try:
                    if not await self._ble_send(grade.command):
                        return
                    await asyncio.wait_for(
                        grade.response_event.wait(), AK_V3_METADATA_RESPONSE_SECONDS
                    )
                except (BleakError, asyncio.TimeoutError, OSError) as err:
                    _LOGGER.warning("AK V3 grade table C6/47 failed: %s", err)
                finally:
                    if self._ak_v3_metadata_read is grade:
                        self._ak_v3_metadata_read = None
        except (BleakError, asyncio.TimeoutError, OSError) as err:
            _LOGGER.warning("AK V3 grade-table transaction failed on %s: %s", self._ble_name, err)

    def _invalidate_ak_v3_calculation_field(self, field: str) -> None:
        """Clear a calculation input before its fresh, response-owned read."""
        if field == "grade_table":
            self._state.grade_table = None
            self._state.grade_limits = None
        elif field == "oil_current_ml":
            self._state.oil_current_ml = None
            self._state.oil_max_ml = None
            self._state.oil_remaining = None
            self._state.oil_status_byte = None
        elif field == "oil_consumption_mlh":
            self._state.oil_consumption_mlh = None
        if self._recompute_oil_days():
            self._notify_state_changed()

    def _handle_ak_v3_metadata_notification(self, raw: bytes, updates: dict) -> bool:
        """Accept only this transaction's expected decoded response."""
        transaction = getattr(self, "_ak_v3_metadata_read", None)
        if transaction is None or raw[:1] != bytes([transaction.response_opcode]):
            return False
        self._record_ak_v3_manual_refresh_trace(
            "metadata_rx", opcode=f"{raw[0]:02X}", length=len(raw), matcher_armed=True
        )
        rejection_reason = None
        if (
            transaction.generation is not None
            and transaction.generation != self._ak_v3_calculation_read_generation
        ):
            rejection_reason = "generation_mismatch"
        elif len(raw) < transaction.minimum_response_length:
            rejection_reason = "short_response"
        elif (
            transaction.expected_response_length is not None
            and len(raw) != transaction.expected_response_length
        ):
            rejection_reason = "unexpected_length"
        elif transaction.state_field not in updates and transaction.response_opcode != 0x50:
            rejection_reason = "missing_decoded_field"
        if rejection_reason is not None:
            self._record_ak_v3_manual_refresh_trace(
                "metadata_rejected", opcode=f"{raw[0]:02X}", length=len(raw), reason=rejection_reason
            )
            return False
        transaction.accepted = True
        transaction.response = bytes(raw)
        self._state.ak_v3_metadata_available.add(transaction.state_field)
        if transaction.response_opcode == 0x4B:
            self._retain_ak_v3_fields("oil_status", "oil_current_ml", "oil_max_ml", "oil_remaining")
        elif transaction.response_opcode == 0x50:
            self._retain_ak_v3_fields("oil_consumption_mlh")
        else:
            self._retain_ak_v3_fields(transaction.state_field)
        transaction.response_event.set()
        self._record_ak_v3_manual_refresh_trace(
            "metadata_accepted", opcode=f"{raw[0]:02X}", length=len(raw), matcher_armed=True
        )
        return True

    def _accept_ak_v3_aggregate_flags(self, schedule: object) -> bool:
        """Keep one consistent aroma-level aggregate pair for a table read."""
        transaction = self._ak_v3_modern_read
        if transaction is None:
            return True
        flags = (schedule.total_fan, schedule.total_fog)
        if transaction.aggregate_flags is None:
            transaction.aggregate_flags = flags
            return True
        if transaction.aggregate_flags != flags:
            _LOGGER.warning(
                "AK V3 table read ignored inconsistent aggregate flags on slot %s: %s != %s",
                schedule.slot_id, flags, transaction.aggregate_flags,
            )
            return False
        return True

    def _record_ak_v3_modern_diagnostic_event(
        self, direction: str, raw: bytes, decoded: dict | None = None
    ) -> None:
        """Append raw, timestamped evidence to the active diagnostic only."""
        transaction = getattr(self, "_ak_v3_modern_diagnostic_trace", None)
        if transaction is None:
            return
        transaction.last_activity_at = asyncio.get_running_loop().time()
        if direction == "TX" and raw[:1] == b"\x8F":
            transaction.events.append(self._safe_ble_history_record(direction, raw))
            return
        event = {
            "direction": direction,
            "timestamp": datetime.now().astimezone().isoformat(),
            "raw_hex": raw.hex(),
            "stage": transaction.phase,
        }
        if decoded:
            event["decoded"] = decoded
        transaction.events.append(event)

    @staticmethod
    def _safe_ble_history_record(direction: str, raw: bytes) -> str | dict:
        """Do not retain AK login frames, which include the configured PIN."""
        if raw[:1] == b"\x8F":
            return {"direction": direction, "opcode": "8F", "length": len(raw)}
        return raw.hex()

    def _record_ak_v3_startup_trace(
        self, event: str, raw: bytes | None = None, expected_opcode: int | None = None,
    ) -> None:
        """Record one bounded, payload-free startup diagnostic event."""
        if not getattr(self, "_ak_v3_startup_trace_active", False):
            return
        entry = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "generation": getattr(self, "_ak_v3_startup_generation", 0),
            "event": event,
            "direction": event if event in {"TX", "RX"} else None,
            "stage": getattr(self, "_ble_connection_stage", "unknown"),
            "opcode": f"{raw[0]:02X}" if raw else None,
            "length": len(raw) if raw else None,
            "expected": f"{expected_opcode:02X}" if expected_opcode is not None else None,
        }
        trace = getattr(self, "_ak_v3_startup_trace", None)
        if trace is None:
            trace = self._ak_v3_startup_trace = []
        trace.append(entry)
        if len(trace) > 40:
            del trace[0]
        _LOGGER.warning("AK V3 startup trace %s", entry)

    def _record_ak_v3_manual_refresh_trace(self, event: str, **details: object) -> None:
        """Record one payload-free event for the active manual refresh only."""
        if not getattr(self, "_ak_v3_manual_refresh_trace_active", False):
            return
        entry = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "transaction_id": getattr(self, "_ak_v3_manual_refresh_transaction_id", 0),
            "generation": getattr(self, "_ak_v3_manual_refresh_generation", None),
            "event": event,
            **details,
        }
        trace = getattr(self, "_ak_v3_manual_refresh_trace", None)
        if trace is None:
            trace = self._ak_v3_manual_refresh_trace = []
        trace.append(entry)
        if len(trace) > 40:
            del trace[0]
        _LOGGER.warning("AK V3 manual refresh trace %s", entry)

    def _transition_ak_v3_modern_read(
        self, transaction: AKV3ModernRead, phase: str, reason: str
    ) -> None:
        """Record an observed state transition before scheduling its response."""
        previous = transaction.phase
        transaction.phase = phase
        transaction.transitions.append({
            "timestamp": datetime.now().astimezone().isoformat(),
            "from": previous,
            "to": phase,
            "reason": reason,
        })

    def _queue_ak_v3_modern_acknowledgement(self, transaction_id: int, frame: bytes) -> None:
        """Optionally acknowledge a collected 4A record without owning completion."""
        transaction = self._ak_v3_modern_read
        if transaction is None or transaction.transaction_id != transaction_id:
            return

        async def send() -> None:
            if self._ak_v3_modern_read is not transaction:
                return
            try:
                async with transaction.send_lock:
                    if not await self._ble_send(frame):
                        _LOGGER.debug("AK V3 optional 4A acknowledgement was not sent: %s", frame.hex())
            except (BleakError, asyncio.TimeoutError, OSError) as err:
                _LOGGER.debug("AK V3 optional 4A acknowledgement failed: %s", err)
            finally:
                transaction.send_tasks.discard(task)

        task = asyncio.create_task(send())
        transaction.send_tasks.add(task)

    async def _async_stop_ak_v3_modern_read(self, transaction: AKV3ModernRead) -> bool:
        """Finish or cancel every response task before another read can start."""
        tasks = tuple(task for task in transaction.send_tasks if task is not asyncio.current_task())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._ak_v3_modern_read is transaction:
            self._ak_v3_modern_read = None
            return True
        return False

    def _complete_ak_v3_modern_read(self, transaction: AKV3ModernRead) -> None:
        """Publish only the five exact physical records from this transaction."""
        if transaction.endpoint is None or set(transaction.records) != set(range(1, 6)):
            return
        endpoint = transaction.endpoint
        for slot, schedule in transaction.records.items():
            identity = (endpoint, slot)
            if schedule.is_empty:
                self._state.ak_v3_schedules.pop(identity, None)
                self._state.ak_v3_empty_schedules[identity] = schedule
            else:
                self._state.ak_v3_schedules[identity] = schedule
                self._state.ak_v3_empty_schedules.pop(identity, None)
                self._ak_v3_restored_identities.discard(identity)
            self._ak_v3_confirmation_metadata[identity] = {
                "confirmed_at": datetime.now().astimezone().isoformat(), "source": "fresh_physical",
            }
            self._set_ak_v3_slot_lifecycle(
                identity, self._ak_v3_slot_lifecycle_from_schedule(schedule, identity), notify=False
            )
        self._persist_ak_v3_schedules()
        # Individual 4A records are provisional. Publish the schedule family
        # only after this transaction commits every physical slot.
        if transaction.aggregate_flags is not None:
            self._state.fan, self._state.diffusion_enabled = transaction.aggregate_flags
        self._retain_ak_v3_fields("schedules", "fan_aggregate")
        self._notify_state_changed()

    def _handle_ak_v3_modern_read_notification(self, raw: bytes, schedule: object | None) -> bool:
        """Advance only an active transaction with its exact expected response."""
        transaction = getattr(self, "_ak_v3_modern_read", None)
        if transaction is None:
            return False
        if (
            raw[:1] != bytes([SM_AK_RESP_SCHEDULE_V3])
            or len(raw) != 18
            or transaction.phase != "slot"
            or not self._ak_v3_record_valid(schedule)
            or schedule.slot_id != transaction.expected_slot
            or (
                transaction.expected_endpoint is not None
                and schedule.endpoint_id != transaction.expected_endpoint
            )
            or (
                transaction.endpoint is not None
                and schedule.endpoint_id != transaction.endpoint
            )
            or not self._accept_ak_v3_aggregate_flags(schedule)
        ):
            return False

        transaction.endpoint = schedule.endpoint_id
        transaction.records[schedule.slot_id] = schedule
        acknowledgement = bytes([0xCA, transaction.endpoint, schedule.slot_id])
        if schedule.slot_id == 1:
            transaction.expected_slot = 2
            self._transition_ak_v3_modern_read(
                transaction, "slot", "received valid direct-push 4A for slot 1"
            )
        elif schedule.slot_id == 5:
            self._transition_ak_v3_modern_read(
                transaction, "complete", "received valid 4A for final slot 5"
            )
            self._complete_ak_v3_modern_read(transaction)
            transaction.records_complete_event.set()
            transaction.completion_result = "complete"
            transaction.completion_event.set()
        else:
            transaction.expected_slot += 1
            self._transition_ak_v3_modern_read(
                transaction, "slot", f"received valid 4A for slot {schedule.slot_id}"
            )
        self._queue_ak_v3_modern_acknowledgement(transaction.transaction_id, acknowledgement)
        return True

    async def _async_read_ak_v3_modern_table(
        self, endpoint: int, *, owner: tuple[str, int, int] | None = None
    ) -> dict[tuple[int, int], object]:
        """Collect one complete A316 table from the direct 4A push stream."""
        active = self._ak_v3_modern_read
        transaction = active or AKV3ModernRead(
            self._ak_v3_read_transaction_id + 1, expected_endpoint=endpoint
        )
        transaction.expected_endpoint = endpoint
        if active is None:
            self._ak_v3_modern_diagnostic_trace = transaction
            if owner is None:
                # Every unowned dynamic 21 owns a pair of response collectors.
                generation = self._ak_v3_startup_generation
                chain = AKV3StartupChain(generation, post_21_sent=True)
                barrier = AKV3StartupBarrier(generation, chain=chain)
                barrier.armed.set()
                self._ak_v3_startup_chain = chain
                self._ak_v3_startup_barrier = barrier
            elif not self._ak_v3_action_is_owner(owner):
                raise BleakError("AK V3 slot action ownership was lost before table read")
            self._arm_ak_v3_modern_collector()
            # Direct 4A records are pushed by the V3 time sync; no V2 schedule
            # polling command is valid on this path.
            if not await self._ble_send(self._protocol.build_time_sync()):
                raise BleakError("AK V3 direct-push trigger was not sent")
        try:
            if self._ak_v3_modern_read is not transaction:
                raise BleakError("AK V3 direct-push collector did not arm")
            await asyncio.wait_for(
                transaction.records_complete_event.wait(), AK_V3_TRANSACTION_READ_SECONDS
            )
            records = {
                (endpoint, slot): schedule
                for slot, schedule in transaction.records.items()
                if schedule.endpoint_id == endpoint
            }
            if set(records) != {(endpoint, slot) for slot in range(1, 6)}:
                raise asyncio.TimeoutError("AK V3 response-driven table was incomplete")
            try:
                await asyncio.wait_for(
                    transaction.completion_event.wait(), AK_V3_ACK_FINALIZE_SECONDS
                )
            except asyncio.TimeoutError:
                transaction.completion_result = "complete_ack_timeout"
            return records
        finally:
            if active is None:
                self._ak_v3_modern_diagnostic_trace = None
            await self._async_stop_ak_v3_modern_read(transaction)

    @staticmethod
    def _ak_v3_target_matches(before: object, after: object, changes: dict) -> bool:
        """Verify every decoded persistent target field, not just the edit."""
        if after is None:
            return False
        fields = (
            "endpoint_id", "slot_id", "enabled", "start_hour", "start_minute",
            "end_hour", "end_minute", "days_mask", "mode", "intensity",
            "work_seconds", "pause_seconds", "fan_state", "active_slot_indicator",
            "protocol_byte_2", "enable_state_byte", "present",
        )
        expected = {field: getattr(before, field) for field in fields}
        expected.update(changes)
        expected["enable_state_byte"] = (
            before.enable_state_byte & 0x05
        ) | (0x02 if expected["enabled"] else 0x00)
        return all(getattr(after, field) == value for field, value in expected.items())

    async def async_update_ak_v3_slot_intensity(self, endpoint: int, slot: int, intensity: int) -> dict:
        """Backward-compatible intensity-only wrapper."""
        return await self.async_update_ak_v3_slot(endpoint, slot, intensity=intensity)

    def _ak_v3_committed_schedule_table(self, endpoint: int) -> dict:
        """Return a complete, physically confirmed in-memory table without BLE."""
        if not isinstance(self._protocol, ScentMarketingAkProtocol) or not self._protocol.is_v3:
            return {"success": False, "source": "committed", "error": "AK V3 identity is not confirmed"}
        if not self.ak_v3_read_available("schedules"):
            return {"success": False, "source": "committed", "error": "AK V3 schedule table is not current"}

        generation = getattr(self, "_ak_v3_startup_generation", 0)
        records = []
        for slot in range(1, 6):
            identity = (endpoint, slot)
            schedule = self._state.ak_v3_schedules.get(identity)
            if schedule is None:
                schedule = self._state.ak_v3_empty_schedules.get(identity)
            if (
                schedule is None
                or self._ak_v3_confirmation_metadata.get(identity, {}).get("source") != "fresh_physical"
                or schedule.raw_frame[:1] != bytes([SM_AK_RESP_SCHEDULE_V3])
                or len(schedule.raw_frame) != 18
                or schedule.endpoint_id != endpoint
                or schedule.slot_id != slot
                or not self._ak_v3_record_valid(schedule)
            ):
                return {"success": False, "source": "committed", "error": "AK V3 committed table is incomplete or invalid"}
            parsed = self._protocol._parse_v3_schedule(schedule.raw_frame)
            if parsed != schedule or not self._ak_v3_record_valid(parsed):
                return {"success": False, "source": "committed", "error": "AK V3 committed record does not match its raw frame"}
            records.append(self._ak_v3_schedule_record(parsed))

        return {
            "success": True,
            "source": "committed",
            "generation": generation,
            "endpoint": endpoint,
            "protocol_complete": True,
            "records_observed": True,
            "missing_physical_slots": [],
            "decoded_4a_records": records,
            "partial": False,
            "errors": [],
        }

    async def async_diagnose_ak_v3_schedule_table(
        self, endpoint: int, cycles: int = 1, source: str = "physical"
    ) -> dict:
        """Trace one fresh, response-driven A316 physical table read."""
        if not 1 <= endpoint <= 0xFF or not 1 <= cycles <= 3:
            return {"success": False, "error": "Invalid AK V3 diagnostic request"}
        if source == "committed":
            return self._ak_v3_committed_schedule_table(endpoint)
        if not isinstance(self._protocol, ScentMarketingAkProtocol) or not self._protocol.is_v3:
            return {"success": False, "error": "AK V3 session is not established"}

        send_errors: list[str] = []
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + AK_V3_DIAGNOSTIC_DEADLINE_SECONDS
        deadline_reached = False
        deadline_reason = "complete_observation"
        transaction: AKV3ModernRead | None = None

        async with self._ak_v3_transaction_lock:
            try:
                async with asyncio.timeout(AK_V3_DIAGNOSTIC_DEADLINE_SECONDS):
                    async with self._ble_lock:
                        await self._teardown_ble_client(reason="table-diagnostic-fresh-session")
                    self._ak_v3_modern_read = None
                    transaction = AKV3ModernRead(
                        self._ak_v3_read_transaction_id + 1,
                        expected_endpoint=endpoint,
                        diagnostic=True,
                        last_activity_at=loop.time(),
                    )
                    self._ak_v3_modern_diagnostic_trace = transaction
                    if not await self._ble_connect(read_ak_state=True, keep_connected=True):
                        send_errors.append("BLE connection failed for table diagnostic")
                    elif self._ak_v3_modern_read is not transaction:
                        send_errors.append("Modern AK V3 direct-push collector did not arm")
                    else:
                        try:
                            await asyncio.wait_for(
                                transaction.records_complete_event.wait(),
                                timeout=AK_V3_TRANSACTION_READ_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            transaction.completion_result = "incomplete"
                        else:
                            try:
                                await asyncio.wait_for(
                                    transaction.completion_event.wait(),
                                    timeout=AK_V3_ACK_FINALIZE_SECONDS,
                                )
                            except asyncio.TimeoutError:
                                transaction.completion_result = "complete_ack_timeout"
            except (BleakError, asyncio.TimeoutError, OSError) as err:
                deadline_reached = isinstance(err, asyncio.TimeoutError)
                deadline_reason = "hard_deadline" if deadline_reached else "transport_failure"
                if not deadline_reached:
                    send_errors.append(str(err))
            finally:
                if transaction is not None:
                    if transaction.completion_result == "pending":
                        transaction.completion_result = "deadline" if deadline_reached else "incomplete"
                    self._ak_v3_modern_diagnostic_trace = None
                    await self._async_stop_ak_v3_modern_read(transaction)
                async with self._ble_lock:
                    remaining = deadline - loop.time()
                    if remaining > 0:
                        try:
                            await asyncio.wait_for(
                                self._teardown_ble_client(reason="table-diagnostic-complete"), timeout=remaining
                            )
                        except asyncio.TimeoutError:
                            deadline_reached = True
                            deadline_reason = "cleanup_deadline"
                    else:
                        deadline_reached = True
                        deadline_reason = "cleanup_deadline"

        transaction = transaction or AKV3ModernRead(0, diagnostic=True)
        decoded_records = [
            self._ak_v3_schedule_record(transaction.records[slot])
            for slot in sorted(transaction.records)
        ]
        missing_slots = [slot for slot in range(1, 6) if slot not in transaction.records]
        return {
            "success": not send_errors and transaction.records_complete_event.is_set(),
            "transaction_id": transaction.transaction_id,
            "endpoint": endpoint,
            "observed_endpoint": transaction.endpoint,
            "events": transaction.events,
            "state_machine": {"stage": transaction.phase, "transitions": transaction.transitions},
            "decoded_4a_records": decoded_records,
            "missing_physical_slots": missing_slots,
            "records_observed": bool(decoded_records),
            "collection_settled": not deadline_reached,
            "protocol_scope": self.ak_v3_protocol_scope,
            "protocol_complete": transaction.records_complete_event.is_set(),
            "completion_result": transaction.completion_result,
            "deadline_seconds": AK_V3_DIAGNOSTIC_DEADLINE_SECONDS,
            "deadline_reached": deadline_reached,
            "deadline_reason": deadline_reason,
            "elapsed_seconds": round(loop.time() - started_at, 3),
            "partial": bool(send_errors or deadline_reached or not transaction.records_complete_event.is_set()),
            "errors": send_errors,
        }

    @staticmethod
    def _ak_v3_schedule_record(schedule: object) -> dict:
        """Make an AKSchedule response safe for service-response serialization."""
        return {
            "endpoint": schedule.endpoint_id,
            "slot": schedule.slot_id,
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
            "fan_state": schedule.fan_state,
            "total_fan": schedule.total_fan,
            "total_fog": schedule.total_fog,
            "present": schedule.present,
            "active_slot_indicator": schedule.active_slot_indicator,
            "protocol_byte_2": schedule.protocol_byte_2,
            "enable_state_byte": schedule.enable_state_byte,
            "raw_frame": schedule.raw_frame.hex(),
        }

    def _on_ble_notification(self, sender: int, data: bytearray) -> None:
        """Handle incoming BLE notification."""
        raw = bytes(data)
        # Login replies can echo credential bytes; retain their shape only.
        self._recent_notifications.append(self._safe_ble_history_record("RX", raw))
        if len(self._recent_notifications) > 20:
            del self._recent_notifications[0]
        modern_ak_v3_read = getattr(self, "_ak_v3_modern_read", None) is not None
        updates = self._protocol.parse_notification(raw)
        self._record_ak_v3_startup_trace("RX", raw)
        if raw[:1] == b"\x8F":
            login = getattr(self, "_ak_v3_login", None)
            if login is not None and login.generation == self._ak_v3_startup_generation:
                is_valid = self._protocol.is_v3 and self._protocol.v3_reply_chaining
                needs_fallback = self._protocol.login_check_password == 3
                if login.phase == "primary" and needs_fallback:
                    login.fallback_required = True
                    login.response_event.set()
                    self._record_ak_v3_startup_trace("login_fallback_check_password_3")
                elif login.phase == "primary" and not self._protocol.is_v3:
                    login.accepted = True
                    login.response_event.set()
                    self._record_ak_v3_startup_trace("login_primary_v2")
                elif is_valid and (login.phase != "fallback" or not needs_fallback):
                    login.accepted = True
                    login.response_event.set()
                    self._record_ak_v3_startup_trace(
                        "login_fallback_success" if login.phase == "fallback" else "login_primary_success"
                    )
            self._record_ak_v3_startup_trace(
                "login_reply_chain_enabled" if self._protocol.v3_reply_chaining else "login_reply_chain_disabled",
                expected_opcode=0x41 if self._protocol.v3_reply_chaining else None,
            )
        chain_reply = self._advance_ak_v3_startup_chain(raw, updates)
        active_chain = getattr(self, "_ak_v3_startup_chain", None)
        if (
            active_chain is not None
            and raw[:1] in {bytes([opcode]) for opcode in (*range(0x41, 0x49), 0x4B, 0x4C, 0x4D, 0x4E, 0x50, 0x51, 0x52)}
            and not chain_reply
        ):
            # A generation-owned chain must not publish malformed, stale, or
            # conflicting metadata replies.
            return
        metadata_transaction = getattr(self, "_ak_v3_metadata_read", None)
        metadata_accepted = self._handle_ak_v3_metadata_notification(raw, updates)
        if (
            metadata_transaction is not None
            and raw[:1] == bytes([metadata_transaction.response_opcode])
            and not metadata_accepted
        ):
            # The owned request rejects malformed, late, and conflicting replies.
            return
        decoded = {}
        schedule = updates.get("ak_v3_schedule")
        if schedule is not None:
            decoded["4a"] = self._ak_v3_schedule_record(schedule)
        if raw[:1] == b"\x83" and len(raw) >= 2:
            decoded["ss"] = raw[1]
        self._record_ak_v3_modern_diagnostic_event("RX", raw, decoded)
        accepted_schedule = False
        if modern_ak_v3_read:
            # The response-driven sequence owns its table until all five
            # authoritative physical records have arrived.
            accepted_schedule = self._handle_ak_v3_modern_read_notification(raw, updates.get("ak_v3_schedule"))
        if raw[:1] == bytes([SM_AK_RESP_SCHEDULE_V3]):
            schedule = updates.get("ak_v3_schedule")
            if modern_ak_v3_read:
                # Collector evidence cannot partially update legacy aggregates.
                updates.pop("total_fan", None)
                updates.pop("diffusion_enabled", None)
        if not updates:
            return

        changed = False
        if "power" in updates:
            # AK V3 Power is owned exclusively by a current-generation 4D
            # response or an isolated owned manual 4D refresh.
            if not isinstance(self._protocol, ScentMarketingAkProtocol) or not self._protocol.is_v3 or (
                "power" in self._state.ak_v3_metadata_available
                and getattr(self, "_ak_v3_power_generation", None) == self._ak_v3_startup_generation
            ):
                self._state.power = updates["power"]
                changed = True
        if "fan" in updates:
            self._state.fan = updates["fan"]
            changed = True
        if "total_fan" in updates:
            self._state.fan = updates["total_fan"]
            changed = True
        if "diffusion_enabled" in updates:
            self._state.diffusion_enabled = updates["diffusion_enabled"]
            changed = True
        if "fan_active" in updates:
            self._state.fan_active = updates["fan_active"]
            changed = True
        if "phase" in updates:
            self._state.phase = updates["phase"]
            changed = True
        if "work_seconds" in updates:
            self._state.work_seconds = updates["work_seconds"]
            changed = True
        if "pause_seconds" in updates:
            self._state.pause_seconds = updates["pause_seconds"]
            changed = True
        if "start_hour" in updates:
            self._state.start_hour = updates["start_hour"]
            self._state.start_minute = updates.get("start_minute", 0)
            changed = True
        if "end_hour" in updates:
            self._state.end_hour = updates["end_hour"]
            self._state.end_minute = updates.get("end_minute", 59)
            changed = True
        if "level" in updates:
            self._state.level = updates["level"]
            changed = True
        if "battery" in updates:
            self._state.battery = updates["battery"]
            changed = True
        if "rgb_on" in updates:
            self._state.rgb_on = updates["rgb_on"]
            changed = True
        if "rgb_color" in updates:
            self._state.rgb_color = updates["rgb_color"]
            changed = True
        # Scent Marketing GW family — new state fields
        if "lock" in updates:
            self._state.lock = updates["lock"]
            changed = True
        if "oil_remaining" in updates:
            self._state.oil_remaining = updates["oil_remaining"]
            changed = True
        if "work_remaining" in updates:
            self._state.work_remaining = updates["work_remaining"]
            changed = True
        if "pause_remaining" in updates:
            self._state.pause_remaining = updates["pause_remaining"]
            changed = True
        for _oil_field in (
            "oil_current_ml", "oil_max_ml",
            "oil_consumption_mlh", "oil_old_calibration_ml", "oil_calculation_records",
            "oil_status_byte",
            "schedule_custom_mode", "grade_table", "grade_limits",
            "ak_v3_capabilities", "ak_v3_has_oil", "ak_v3_has_battery",
            "ak_v3_has_custom_mode", "ak_v3_has_aromas", "ak_v3_has_fan",
            "ak_v3_has_round_battery", "ak_v3_has_lamp", "ak_v3_has_global_control",
            "ak_v3_reply_chaining", "ak_v3_lamp_type", "ak_v3_protocol_identity",
        ):
            if _oil_field in updates:
                setattr(self._state, _oil_field, updates[_oil_field])
                changed = True
        if "oil_names" in updates:
            self._state.oil_names = updates["oil_names"]
            changed = True
        if "light_on" in updates:
            self._state.light_on = updates["light_on"]
            changed = True
        if "device_name" in updates:
            self._state.device_name = updates["device_name"]
            changed = True
        if "device_name_append_prefix" in updates:
            self._state.device_name_append_prefix = updates["device_name_append_prefix"]
            changed = True
        if "password_required" in updates:
            self._state.password_required = updates["password_required"]
            changed = True
        if "firmware_version" in updates:
            self._state.firmware_version = updates["firmware_version"]
            changed = True
        # Scent Marketing AK — read-back fields
        if "intensity" in updates:
            self._state.intensity = updates["intensity"]
            changed = True
        if "weekday_mask" in updates:
            self._state.weekday_mask = updates["weekday_mask"]
            changed = True
        if "schedule_slot" in updates:
            self._state.schedule_slot = updates["schedule_slot"]
            changed = True
        if "device_label" in updates:
            self._state.device_label = updates["device_label"]
            changed = True
        if "model_code" in updates:
            self._state.model_code = updates["model_code"]
            changed = True
        if "schedule_enabled" in updates:
            self._state.schedule_enabled = updates["schedule_enabled"]
            changed = True
        if "ak_v3_schedule" in updates:
            schedule = updates["ak_v3_schedule"]
            identity = (schedule.endpoint_id, schedule.slot_id)
            if not modern_ak_v3_read:
                if schedule.is_empty:
                    self._state.ak_v3_schedules.pop(identity, None)
                    self._state.ak_v3_empty_schedules[identity] = schedule
                else:
                    self._state.ak_v3_schedules[identity] = schedule
                    self._state.ak_v3_empty_schedules.pop(identity, None)
                    self._ak_v3_restored_identities.discard(identity)
                    self._ak_v3_confirmation_metadata[identity] = {
                        "confirmed_at": datetime.now().astimezone().isoformat(), "source": "fresh_physical",
                    }
                    self._persist_ak_v3_schedules()
                self._set_ak_v3_slot_lifecycle(
                    identity,
                    self._ak_v3_slot_lifecycle_from_schedule(schedule, identity),
                    notify=False,
                )
            changed = True

        # Derive oil days-remaining from the latest oil + schedule state.
        # The 0x50 frame's raw value doesn't match the official app, which
        # computes it, so we mirror that math here (see _recompute_oil_days).
        if self._recompute_oil_days():
            changed = True

        if changed:
            self._notify_state_changed()

    @property
    def ak_v3_manual_refresh_diagnostics(self) -> dict[str, str | None]:
        """Return the last isolated manual refresh outcome."""
        return {"last_refresh": self._ak_v3_manual_refresh_at, "result": self._ak_v3_manual_refresh_result}

    def _ak_v3_manual_refresh_session_ready(self) -> bool:
        """Return whether this generation owns a reusable authenticated session."""
        login = self._ak_v3_login
        return (
            isinstance(self._protocol, ScentMarketingAkProtocol)
            and self._protocol.is_v3
            and self._ble_connected
            and self._ble_notify_subscribed
            and self._ble_client is not None
            and self._ble_client.is_connected
            and login is not None
            and login.generation == self._ak_v3_startup_generation
            and login.accepted
        )

    async def _async_close_manual_refresh_session(self) -> None:
        """Close a manual-only link without invalidating accepted device state."""
        await self._teardown_ble_client(reason="manual_refresh")
        self._ble_connected = False
        self._ble_notify_subscribed = False
        self._ble_connection_stage = "idle"
        self._ble_disconnect_expected = False
        self._ak_v3_manual_refresh_generation = None

    async def async_refresh_ak_v3_state(self) -> bool:
        """Run one manual, reply-driven AK V3 metadata and schedule refresh."""
        if not self.ak_v3_manual_refresh_available:
            self._ak_v3_manual_refresh_result = "unavailable_or_busy"
            return False
        owner = self._claim_ak_v3_action("manual_refresh")
        if owner is None:
            self._ak_v3_manual_refresh_result = "unavailable_or_busy"
            return False
        self._ak_v3_manual_refresh_active = True
        self._ak_v3_manual_refresh_transaction_id = (
            getattr(self, "_ak_v3_manual_refresh_transaction_id", 0) + 1
        )
        self._ak_v3_manual_refresh_trace = []
        self._ak_v3_manual_refresh_trace_active = True
        self._ak_v3_manual_refresh_generation = self._ak_v3_startup_generation
        self._record_ak_v3_manual_refresh_trace(
            "transaction_started",
            ble_connected=self._ble_connected,
            notifications_subscribed=self._ble_notify_subscribed,
            authenticated=self._ak_v3_manual_refresh_session_ready(),
        )
        manual_session_opened = False
        schedule_transaction: AKV3ModernRead | None = None
        chain: AKV3StartupChain | None = None
        barrier: AKV3StartupBarrier | None = None
        try:
            # A manual refresh is deliberately isolated from the subscribed
            # startup session: tear it down, then authenticate a fresh link.
            if self._ble_connected or self._ble_client is not None:
                async with self._ble_lock:
                    await self._async_close_manual_refresh_session()
                self._record_ak_v3_manual_refresh_trace("existing_session_closed")
            if not self._ak_v3_manual_refresh_session_ready():
                self._record_ak_v3_manual_refresh_trace("connection_attempted")
                try:
                    connected = await self._ble_connect(
                        read_ak_state=False,
                        keep_connected=True,
                        manual_ak_v3_session=True,
                    )
                except Exception as err:
                    self._record_ak_v3_manual_refresh_trace(
                        "connection_exception", error=type(err).__name__
                    )
                    self._ak_v3_manual_refresh_result = "failed:connect"
                    return False
                self._record_ak_v3_manual_refresh_trace(
                    "connection_result",
                    connected=connected,
                    ble_connected=self._ble_connected,
                    notifications_subscribed=self._ble_notify_subscribed,
                    authenticated=self._ak_v3_manual_refresh_session_ready(),
                )
                if not connected:
                    self._ak_v3_manual_refresh_result = "failed:connect"
                    return False
                manual_session_opened = True
                self._ak_v3_manual_refresh_generation = self._ak_v3_startup_generation
            if not self._ak_v3_manual_refresh_session_ready():
                self._ak_v3_manual_refresh_result = "failed:authentication"
                return False
            transaction_lock = getattr(self, "_ak_v3_transaction_lock", None)
            if transaction_lock is None:
                transaction_lock = self._ak_v3_transaction_lock = asyncio.Lock()
            async with transaction_lock:
                if not self._ak_v3_manual_refresh_session_ready():
                    self._ak_v3_manual_refresh_result = "failed:session"
                    return False
                chain = self._ak_v3_startup_chain
                barrier = self._ak_v3_startup_barrier
                schedule_transaction = self._ak_v3_modern_read
                if schedule_transaction is None:
                    self._ak_v3_manual_refresh_result = "failed:schedule_1"
                    return False
                if chain is None or barrier is None:
                    self._ak_v3_manual_refresh_result = "failed:metadata_41"
                    return False

                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            chain.completed.wait(), schedule_transaction.records_complete_event.wait()
                        ),
                        AK_V3_TRANSACTION_READ_SECONDS,
                    )
                except asyncio.TimeoutError:
                    if not chain.completed.is_set() or chain.failed:
                        self._ak_v3_manual_refresh_result = f"failed:{self._ak_v3_manual_refresh_failure(chain)}"
                    else:
                        self._ak_v3_manual_refresh_result = f"failed:schedule_{schedule_transaction.expected_slot}"
                    return False
                if chain.failed:
                    self._ak_v3_manual_refresh_result = f"failed:{self._ak_v3_manual_refresh_failure(chain)}"
                    return False
                try:
                    await asyncio.wait_for(schedule_transaction.completion_event.wait(), AK_V3_ACK_FINALIZE_SECONDS)
                except asyncio.TimeoutError:
                    self._ak_v3_manual_refresh_result = "failed:schedule_5"
                    return False
                if schedule_transaction.completion_result != "complete":
                    self._ak_v3_manual_refresh_result = "failed:schedule_5"
                    return False
                self._ak_v3_manual_refresh_result = "success"
                return True
        finally:
            self._ak_v3_metadata_read = None
            if chain is not None and self._ak_v3_startup_chain is chain:
                self._ak_v3_startup_chain = None
            if barrier is not None and self._ak_v3_startup_barrier is barrier:
                barrier.released.set()
                self._ak_v3_startup_barrier = None
            if schedule_transaction is not None:
                await self._async_stop_ak_v3_modern_read(schedule_transaction)
            if manual_session_opened:
                await self._async_close_manual_refresh_session()
            self._ak_v3_manual_refresh_active = False
            self._ak_v3_manual_refresh_at = datetime.now().astimezone().isoformat()
            self._record_ak_v3_manual_refresh_trace(
                "cleanup_complete",
                matcher_released=self._ak_v3_metadata_read is None,
                transaction_owner_released=self._ak_v3_modern_read is None,
                ble_lock_released=not self._ble_lock.locked(),
                temporary_connection_released=(not manual_session_opened or self._ble_client is None),
            )
            self._record_ak_v3_manual_refresh_trace(
                "final_notification",
                result=self._ak_v3_manual_refresh_result,
                refresh_at=self._ak_v3_manual_refresh_at,
            )
            self._ak_v3_manual_refresh_trace_active = False
            self._release_ak_v3_action(owner)
            self._notify_state_changed()

    def _recompute_oil_days(self) -> bool:
        """Derive app-equivalent days from all complete AK V3 schedules."""
        s = self._state
        prev = s.oil_days_remaining
        new = self._estimate_oil_days(s.oil_current_ml, s.oil_consumption_mlh)
        changed = new != prev
        if changed:
            s.oil_days_remaining = new
        fresh = new is not None and self._ak_v3_oil_days_inputs_current()
        if fresh:
            current = getattr(self, "_ak_v3_current_fields", set())
            fresh = "oil_days_remaining" not in current
            if fresh:
                self._retain_ak_v3_fields("oil_days_remaining")
        return changed or fresh

    def _ak_v3_oil_days_inputs_current(self) -> bool:
        """Require every input to an APK-style days estimate from this generation."""
        if not self.protocol_is_v3:
            return False
        current = getattr(self, "_ak_v3_current_fields", set())
        if not {"oil_current_ml", "oil_consumption_mlh", "schedules"} <= current:
            return False
        schedules = self._ak_v3_complete_schedules()
        if schedules is None:
            return False
        return (
            "grade_table" in current
            or not any(
                schedule.present and schedule.enabled and not schedule.is_empty and schedule.mode == 0x00
                for schedule in schedules
            )
        )

    def _ak_v3_complete_schedules(self) -> list[object] | None:
        """Return all five endpoint-1 records, including confirmed empty slots."""
        state = self._state
        schedules = []
        for slot in range(1, 6):
            schedule = state.ak_v3_schedules.get((1, slot))
            if schedule is None:
                schedule = state.ak_v3_empty_schedules.get((1, slot))
            if schedule is None:
                return None
            schedules.append(schedule)
        return schedules

    def _ak_v3_schedule_work_pause(self, schedule: object) -> tuple[int, int] | None:
        """Return the APK's work/pause pair for one physical schedule record."""
        if schedule.mode == 0x01:
            work, pause = schedule.work_seconds, schedule.pause_seconds
        elif schedule.mode == 0x00:
            table = self._state.grade_table
            index = schedule.intensity - 1
            if not table or not 0 <= index < len(table) or not table[index]:
                return None
            work, pause = table[index]
        else:
            return None
        if not work or not pause or work < 0 or pause < 0:
            return None

        return work, pause

    @staticmethod
    def _ak_v3_apk_segments(schedule: object) -> list[list[int]]:
        """Split a schedule exactly as CalculateModel.dayCalculate() does."""
        start = schedule.start_hour * 3600 + schedule.start_minute * 60
        end = schedule.end_hour * 3600 + schedule.end_minute * 60
        if start < end:
            return [[start, end], [0, 0]]
        return [[start, 86400], [0, end]]

    def _ak_v3_apk_weekly_consumption(self, flow_mlh: float) -> list[float] | None:
        """Return CalculateModel.dayCalculate()'s Saturday-through-Sunday buckets."""
        schedules = self._ak_v3_complete_schedules()
        if schedules is None:
            return None
        daily: list[float] = []
        for bucket in range(7):
            # The app indexes its seven-character repeat string directly. Its
            # bucket 0 is Saturday and bucket 6 is Sunday.
            weekday_mask_bit = 6 - bucket
            segments: list[tuple[list[list[int]], tuple[int, int]]] = []
            for schedule in schedules:
                if schedule.is_empty or not schedule.enabled or not schedule.present:
                    continue
                if not schedule.days_mask & (1 << weekday_mask_bit):
                    continue
                work_pause = self._ak_v3_schedule_work_pause(schedule)
                if work_pause is None:
                    return None
                segments.append((self._ak_v3_apk_segments(schedule), work_pause))

            total_seconds = 0
            for current_index, (current, work_pause) in enumerate(segments):
                for earlier, _earlier_work_pause in segments[:current_index]:
                    # Preserve the APK's ordered, in-place overlap mutation.
                    # It compares only matching before-/after-midnight segments.
                    for segment_index in range(2):
                        left = current[segment_index]
                        right = earlier[segment_index]
                        if left[0] >= right[0]:
                            if left[0] <= right[0] or left[0] >= right[1]:
                                continue
                            if left[1] < right[1]:
                                left[0] = left[1]
                            else:
                                left[0] = right[1]
                        elif left[1] <= right[0] or left[1] > right[1]:
                            if left[1] > right[1]:
                                left[1] -= right[1] - right[0]
                        else:
                            left[1] = right[0]

                work, pause = work_pause
                for start, end in current:
                    total_seconds += (end - start) * work // (work + pause)
            daily.append((total_seconds / 3600.0) * flow_mlh)
        return daily

    def _estimate_oil_days(self, remaining_ml: int | None, flow_mlh: float | None) -> int | None:
        """Mirror CalculateModel.dayCalculate() oil-depletion iteration."""
        if remaining_ml is None or flow_mlh is None or remaining_ml < 0 or flow_mlh <= 0:
            return None
        if remaining_ml == 0:
            return 0
        daily = self._ak_v3_apk_weekly_consumption(flow_mlh)
        if daily is None or not any(value > 0 for value in daily):
            return None
        remaining = float(remaining_ml)
        # Java Calendar.DAY_OF_WEEK is Sunday=1 through Saturday=7.
        current_index = 6 - (datetime.now().isoweekday() % 7)
        days = 0
        while remaining > 0:
            days += 1
            remaining -= daily[current_index]
            current_index = (current_index + 1) % 7
        return days

    def _ak_v3_oil_write_values(
        self, *, total_ml: int | None = None, remaining_ml: int | None = None,
        flow_mlh: float | None = None,
    ) -> tuple[int, int, float, int]:
        """Resolve a one-aroma AK V3 write from fresh parsed device state."""
        if not isinstance(self._protocol, ScentMarketingAkProtocol) or not self._protocol.is_v3:
            raise ValueError("AK V3 session is not established")
        state = self._state
        if state.oil_status_byte is None or state.oil_max_ml is None or state.oil_current_ml is None:
            raise ValueError("Fresh AK V3 oil status is required before writing")
        total = state.oil_max_ml if total_ml is None else int(total_ml)
        remaining = state.oil_current_ml if remaining_ml is None else int(remaining_ml)
        flow = state.oil_consumption_mlh if flow_mlh is None else float(flow_mlh)
        if flow is None:
            raise ValueError("Fresh AK V3 oil flow is required before writing")
        days = self._estimate_oil_days(remaining, flow)
        if days is None:
            days = state.oil_days_remaining
        if days is None:
            raise ValueError("Cannot calculate AK V3 estimated oil days from current schedule state")
        return total, remaining, flow, days

    async def _async_write_ak_v3_oil(self, total: int, remaining: int, flow: float, days: int) -> bool:
        """Send the app's ordered oil-table transaction then request fresh state."""
        status = self._state.oil_status_byte
        if status is None:
            return False
        amount = self._protocol.build_v3_oil_amounts(status, [(total, remaining)])
        calculation = self._protocol.build_v3_oil_calculations([(flow, days)])
        if not await self._ble_connect(read_ak_state=False, keep_connected=True):
            return False
        try:
            if not await self._ble_send(amount):
                return False
            await asyncio.sleep(0.2)
            if not await self._ble_send(calculation):
                return False
            # C8 and CE are the supported fresh reads for oil amount and flow.
            for frame in self._protocol.build_read_state_queries()[-2:]:
                await self._ble_send(frame)
                await asyncio.sleep(0.15)
            return True
        except (BleakError, asyncio.TimeoutError, OSError):
            return False
        finally:
            self._schedule_disconnect()

    async def _async_read_ak_v3_oil_baseline(self) -> tuple[int, int, float, int, int] | None:
        """Return fresh, response-owned AK V3 oil values without trusting cache."""
        if not await self._async_wait_for_ak_v3_startup_barrier():
            return None
        self._ak_v3_calculation_read_generation = (
            getattr(self, "_ak_v3_calculation_read_generation", 0) + 1
        )
        generation = self._ak_v3_calculation_read_generation
        responses: list[bytes] = []
        for command, opcode, field, minimum in (
            (b"\xC8", 0x4B, "oil_current_ml", 6),
            (b"\xCE", 0x50, "oil_consumption_mlh", 8),
        ):
            transaction = AKV3MetadataRead(command, opcode, field, minimum, generation=generation)
            self._ak_v3_metadata_read = transaction
            try:
                if not await self._ble_send(command):
                    return None
                await asyncio.wait_for(
                    transaction.response_event.wait(), AK_V3_METADATA_RESPONSE_SECONDS
                )
            except (BleakError, asyncio.TimeoutError, OSError):
                return None
            finally:
                if self._ak_v3_metadata_read is transaction:
                    self._ak_v3_metadata_read = None
            if not transaction.accepted or transaction.response is None:
                return None
            responses.append(transaction.response)

        amount, calculation = responses
        total = (amount[2] << 8) | amount[3]
        remaining = (amount[4] << 8) | amount[5]
        records = self._protocol.parse_notification(calculation).get("oil_calculation_records", [])
        if total <= 0 or not records:
            return None
        _enabled, flow, days, old = next((record for record in records if record[0]), records[0])
        return total, remaining, flow, days, old

    async def async_set_ak_v3_oil(
        self, *, total_ml: int | None = None, remaining_ml: int | None = None,
        flow_mlh: float | None = None,
    ) -> bool:
        """Save normal AK V3 oil edits without optimistic state mutation."""
        try:
            values = self._ak_v3_oil_write_values(
                total_ml=total_ml, remaining_ml=remaining_ml, flow_mlh=flow_mlh,
            )
            return await self._async_write_ak_v3_oil(*values)
        except ValueError as err:
            _LOGGER.warning("AK V3 oil edit rejected: %s", err)
            return False

    async def async_calibrate_ak_v3_oil(self, actual_remaining_ml: int) -> bool:
        """Calibrate from fresh records and commit only after matching read-back."""
        try:
            if not await self._ble_connect(read_ak_state=False, keep_connected=True):
                return False
            baseline = await self._async_read_ak_v3_oil_baseline()
            if baseline is None:
                _LOGGER.warning("AK V3 oil calibration is inconclusive: fresh baseline unavailable")
                return False
            total, previous_remaining, previous_flow, _stored_days, old = baseline
            actual = int(actual_remaining_ml)
            if not 0 <= actual <= total:
                raise ValueError("Calibration amount must be within the container capacity")
            flow = previous_flow
            consumed_since_previous = (old - previous_remaining) if old is not None else 0
            if old and old > actual and consumed_since_previous > 0 and previous_flow > 0:
                flow = (old - actual) / (consumed_since_previous / previous_flow)
            # The 0x30 encoder stores flow in hundredths of mL/h. Freeze the
            # calibration calculation to that same on-wire precision before
            # writing and comparing the fresh read-back.
            flow = float(Decimal(str(flow)).quantize(Decimal("0.01")))
            days = self._estimate_oil_days(actual, flow)
            if days is None:
                raise ValueError("Cannot calculate calibration days from current schedule state")
            amount = self._protocol.build_v3_oil_amounts(self._state.oil_status_byte, [(total, actual)])
            calculation = self._protocol.build_v3_oil_calculations([(flow, days)])
            if not await self._ble_send(amount):
                return False
            await asyncio.sleep(0.2)
            if not await self._ble_send(calculation):
                return False
            verified = await self._async_read_ak_v3_oil_baseline()
            if verified is None:
                _LOGGER.warning("AK V3 oil calibration is inconclusive: verification unavailable")
                return False
            observed_total, observed_remaining, observed_flow, observed_days, _observed_old = verified
            expected_flow = self._protocol.parse_notification(
                b"\x50" + calculation[1:]
            )["oil_calculation_records"][0][1]
            if (observed_total, observed_remaining, observed_flow, observed_days) == (
                total, actual, expected_flow, days,
            ):
                self._state.oil_old_calibration_ml = actual
                return True
            _LOGGER.warning("AK V3 oil calibration is inconclusive: read-back did not match write")
            return False
        except ValueError as err:
            _LOGGER.warning("AK V3 oil calibration rejected: %s", err)
            return False
        finally:
            self._schedule_disconnect()

    async def async_set_ak_v3_device_name(self, name: str) -> bool:
        """Write AK V3 name, then ask the device for its authoritative value."""
        if not isinstance(self._protocol, ScentMarketingAkProtocol):
            return False
        prefix = self._state.device_name_append_prefix
        prefix_text = prefix.decode("utf-8")
        # TextEntity displays the full device read-back. Strip only the exact
        # learned prefix before applying the 16-byte editable-payload limit.
        editable_name = name
        while prefix_text and editable_name.startswith(prefix_text):
            editable_name = editable_name.removeprefix(prefix_text)
        frame = self._protocol.build_v3_device_name(editable_name, prefix)
        if not await self._ble_execute(frame):
            return False
        if self._ble_connected:
            await self._ble_send(bytes([0xC6]))
        return True

    async def async_set_ak_v3_device_label(self, label: str) -> bool:
        """Write AK V3 label, then ask the device for its authoritative value."""
        if not isinstance(self._protocol, ScentMarketingAkProtocol):
            return False
        try:
            frame = self._protocol.build_v3_device_label(label)
        except ValueError:
            return False
        if not await self._ble_execute(frame):
            return False
        if self._ble_connected:
            await self._ble_send(bytes([0xC7]))
        return True

    async def async_set_ak_v3_oil_name(self, name: str) -> bool:
        """Write the one-aroma name table; this firmware has no name read-back."""
        if not isinstance(self._protocol, ScentMarketingAkProtocol) or not self._protocol.is_v3:
            return False
        names = list(self._state.oil_names) or [name]
        names[0] = name
        frame = self._protocol.build_v3_oil_names(names)
        if not await self._ble_execute(frame):
            return False
        # No confirmed fragrance-name read exists; retain no optimistic value.
        return True

    # ------------------------------------------------------------------
    # Commands (BLE first, cloud fallback)
    # ------------------------------------------------------------------

    async def set_power(self, on: bool) -> bool:
        """Turn device on or off."""
        # Try BLE
        if self._ble_address:
            cmd = self._protocol.build_power(on)
            if await self._ble_execute(cmd):
                if isinstance(self._protocol, ScentMarketingAkProtocol) and self._protocol.is_v3:
                    # V3 power is not optimistic: only a validated 4D read can
                    # publish the new value.
                    return True
                self._state.power = on
                self._state.phase = "idle" if on else "off"
                self._notify_state_changed()
                return True

        # Cloud fallback
        if self.supports_cloud and self._cloud:
            success = await self._cloud.set_power(self._cloud_device_id, on)
            if success:
                self._state.power = on
                self._state.phase = "idle" if on else "off"
                self._notify_state_changed()
            return success

        return False

    async def momentary_diffuse(self) -> bool:
        """Run the diffuser for `momentary_seconds`, then switch it off.

        There is no native one-shot command in the Aroma-Link protocol
        (verified against the decompiled official app), so this is
        power-on followed by a delayed power-off task. Pressing again
        while a run is active restarts the countdown.
        """
        if self._momentary_task and not self._momentary_task.done():
            self._momentary_task.cancel()
        if not await self.set_power(True):
            return False
        self._momentary_task = asyncio.ensure_future(
            self._momentary_off_later(self.momentary_seconds)
        )
        return True

    async def _momentary_off_later(self, delay: int) -> None:
        await asyncio.sleep(delay)
        if not await self.set_power(False):
            _LOGGER.warning(
                "Momentary diffusion on %s: auto power-off failed — "
                "the diffuser may still be running", self.name,
            )

    async def set_fan(self, on: bool) -> bool:
        """Turn fan on or off (Aroma-Link + Scent Marketing AK)."""
        if not self._ble_address:
            return False
        proto = self._protocol
        if isinstance(proto, (AromaLinkBleProtocol, ScentMarketingAkProtocol, AromelyAroMaxProtocol)):
            cmd = proto.build_fan(on)
            if await self._ble_execute(cmd):
                self._state.fan = on
                self._notify_state_changed()
                return True
        return False

    async def set_lock(self, on: bool) -> bool:
        """Toggle child-lock (Scent Marketing AK + GW + GW-XOR)."""
        if not self._ble_address:
            return False
        proto = self._protocol
        if isinstance(proto, ScentMarketingAkProtocol) and proto.is_v3:
            return False
        if isinstance(proto, (ScentMarketingAkProtocol, ScentMarketingGwProtocol)):
            cmd = proto.build_lock(on)
            if await self._ble_execute(cmd):
                self._state.lock = on
                self._notify_state_changed()
                return True
        return False

    async def set_lamp(self, on: bool) -> bool:
        """Toggle auxiliary lamp (Scent Marketing AK lamp-bit, GW DP-11 light)."""
        if not self._ble_address:
            return False
        proto = self._protocol
        if isinstance(proto, ScentMarketingAkProtocol):
            if proto.is_v3 and not self._state.ak_v3_has_lamp:
                return False
            cmd = proto.build_lamp(on)
        elif isinstance(proto, ScentMarketingGwProtocol):
            cmd = proto.build_light(on)
        else:
            return False
        if await self._ble_execute(cmd):
            self._state.light_on = on
            self._notify_state_changed()
            return True
        return False

    async def set_level(self, level: int) -> bool:
        """Set Scentiment spray intensity (1-3)."""
        if not isinstance(self._protocol, ScentimentProtocol) or not self._ble_address:
            return False
        cmd = self._protocol.build_set_level(level)
        if await self._ble_execute(cmd):
            self._state.level = level
            self._notify_state_changed()
            return True
        return False

    async def set_schedule_enabled(self, enabled: bool) -> bool:
        """Enable or disable the active V3 schedule/program.

        On V3 devices the program-enabled flag is a separate control
        from Power: a V3 diffuser can be powered on with the program
        disabled, in which case it won't spray. We re-apply the
        currently cached schedule with the enabled bit flipped — same
        approach as `set_intensity`, since there's no standalone
        program-enable opcode in @Mins95's captures.

        On V2 this method delegates to `set_power`, because V2's
        firmware treats the schedule-enabled bit and the power
        concept as the same toggle.
        """
        if not isinstance(self._protocol, ScentMarketingAkProtocol):
            return False
        if not self._protocol.is_v3:
            return await self.set_power(enabled)
        if self._ak_v3_schedule_writes_disabled():
            self._ak_v3_transaction_error(
                "AK V3 enable/disable unavailable until protocol verified"
            )
            return False

    async def set_intensity(self, intensity: int) -> bool:
        """Set Scent Marketing AK spray intensity.

        The AK protocol bundles intensity into schedule frames (no
        dedicated opcode is known), so this stores the value locally and
        the next schedule write will pick it up. The Number entity also
        triggers a schedule re-write so the change takes effect
        immediately even when the user doesn't separately touch a Start
        Time / End Time entity.
        """
        if not isinstance(self._protocol, ScentMarketingAkProtocol):
            return False
        if self._ak_v3_schedule_writes_disabled():
            return False
        # Clamp to the firmware-accepted range. V2 caps at 10, V3 at 20.
        max_value = 20 if self._protocol.is_v3 else 10
        clamped = max(0, min(max_value, int(intensity)))
        self._state.intensity = clamped
        self._notify_state_changed()
        # Push the current schedule with the new intensity so the change
        # is observable on the device immediately. Intensity is the
        # grade in the device's *Level* mode, so a deliberate intensity
        # change selects Level mode (work/pause come from the device's
        # grade table). Adjusting Work/Pause Duration switches back to
        # Custom mode — see `_write_schedule_to_device`.
        if self._ble_address:
            return await self._write_schedule_to_device(custom_mode=False)
        return True

    async def set_rgb_color(self, r: int, g: int, b: int) -> bool:
        """Set Scentiment RGB LED color."""
        if not isinstance(self._protocol, ScentimentProtocol) or not self._ble_address:
            return False
        cmd = self._protocol.build_set_rgb_color(r, g, b)
        if await self._ble_execute(cmd):
            self._state.rgb_color = (r, g, b)
            self._notify_state_changed()
            return True
        return False

    async def set_rgb_led(self, on: bool) -> bool:
        """Turn Scentiment RGB LED on or off."""
        if not isinstance(self._protocol, ScentimentProtocol) or not self._ble_address:
            return False
        cmd = self._protocol.build_set_rgb_led(on)
        if await self._ble_execute(cmd):
            self._state.rgb_on = on
            self._notify_state_changed()
            return True
        return False

    async def set_schedule_mode(self, custom: bool) -> bool:
        """Explicitly select the AK V3 schedule mode (Custom vs Level).

        Mirrors the official app's mode toggle. Custom honours the Work/Pause
        Duration; Level uses the device's grade table (Intensity selects the
        grade). The implicit mode-follows-control behaviour stays — this just
        lets the user pin the mode directly without nudging a duration or the
        intensity (@Mins95's UX request, #8).
        """
        if not isinstance(self._protocol, ScentMarketingAkProtocol):
            return False
        if not self._protocol.is_v3:
            return False
        if self._ak_v3_schedule_writes_disabled():
            return False
        self._state.schedule_custom_mode = custom
        self._notify_state_changed()
        if self._ble_address:
            return await self._write_schedule_to_device(custom_mode=custom)
        return True

    async def set_work_duration(self, seconds: int) -> bool:
        """Set the spray work duration and write to device."""
        if self._ak_v3_schedule_writes_disabled():
            return False
        self._state.work_seconds = seconds
        # Setting an explicit duration means the user wants Custom mode.
        return await self._write_schedule_to_device(custom_mode=True)

    async def set_pause_duration(self, seconds: int) -> bool:
        """Set the pause duration and write to device."""
        if self._ak_v3_schedule_writes_disabled():
            return False
        self._state.pause_seconds = seconds
        return await self._write_schedule_to_device(custom_mode=True)

    async def set_schedule(
        self,
        weekday_mask: int,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
        work_seconds: int,
        pause_seconds: int,
        enabled: bool = True,
    ) -> bool:
        """Set a full schedule on the device."""
        if self._ak_v3_schedule_writes_disabled():
            return False
        self._state.work_seconds = work_seconds
        self._state.pause_seconds = pause_seconds
        self._state.start_hour = start_hour
        self._state.start_minute = start_minute
        self._state.end_hour = end_hour
        self._state.end_minute = end_minute

        # An explicit work/pause schedule means Custom mode.
        return await self._write_schedule_to_device(
            weekday_mask=weekday_mask, enabled=enabled, custom_mode=True,
        )

    async def _write_schedule_to_device(
        self,
        weekday_mask: int | None = None,
        enabled: bool = True,
        custom_mode: bool | None = None,
    ) -> bool:
        """Write the current schedule state to the device.

        `weekday_mask=None` (the default for callers like `set_intensity`
        or `set_schedule_enabled` that aren't changing the day pattern)
        preserves whatever mask was last read back from the device, so a
        one-axis tweak doesn't silently flatten an M-F schedule into
        every-day.

        `custom_mode=None` preserves the device's current V3 schedule mode
        (Custom vs Level); callers that change Work/Pause pass True, and
        `set_intensity` passes False. Ignored on non-AK / V2 devices.
        """
        if self._ak_v3_schedule_writes_disabled():
            return False
        if weekday_mask is None:
            weekday_mask = self._state.weekday_mask if self._state.weekday_mask is not None else 0x7F
        work = self._state.work_seconds or DEFAULT_WORK_DURATION
        pause = self._state.pause_seconds or DEFAULT_PAUSE_DURATION
        s_h = self._state.start_hour
        s_m = self._state.start_minute
        e_h = self._state.end_hour
        e_m = self._state.end_minute

        # Try BLE
        if self._ble_address:
            cmd = None
            if self._device_type == DeviceType.TUYA_BLE:
                setup = ScheduleSetup(
                    index=0, weekday_mask=weekday_mask, enabled=enabled,
                    start_hour=s_h, start_minute=s_m, end_hour=e_h, end_minute=e_m,
                    work_seconds=work, pause_seconds=pause,
                )
                cmd = self._protocol.build_schedule([setup])
            elif isinstance(self._protocol, AromaLinkBleProtocol):
                slot = ScheduleSlot(
                    start_hour=s_h, start_minute=s_m, end_hour=e_h, end_minute=e_m,
                    enabled=enabled, work_seconds=work, pause_seconds=pause,
                )
                cmd = self._protocol.build_schedule(weekday_mask, [slot])
            elif isinstance(self._protocol, ScentMarketingGwProtocol):
                slot = ScheduleSlot(
                    start_hour=s_h, start_minute=s_m, end_hour=e_h, end_minute=e_m,
                    enabled=enabled, work_seconds=work, pause_seconds=pause,
                )
                cmd = self._protocol.build_schedule([slot], weekday_mask=weekday_mask)
            elif isinstance(self._protocol, ScentMarketingAkProtocol):
                slot = ScheduleSlot(
                    start_hour=s_h, start_minute=s_m, end_hour=e_h, end_minute=e_m,
                    enabled=enabled, work_seconds=work, pause_seconds=pause,
                )
                # Pull intensity from state. Default to mid-range for the
                # detected protocol version (V2 caps at 10, V3 at 20).
                if self._state.intensity is not None:
                    level = self._state.intensity
                elif self._protocol.is_v3:
                    level = 10
                else:
                    level = 6
                # Resolve the V3 schedule mode: preserve the device's
                # current mode when the caller didn't specify one.
                if custom_mode is None:
                    resolved_mode = (
                        self._state.schedule_custom_mode
                        if self._state.schedule_custom_mode is not None
                        else True
                    )
                else:
                    resolved_mode = custom_mode
                cmd = self._protocol.build_schedule(
                    slot, weekday_mask=weekday_mask, intensity=level,
                    custom_mode=resolved_mode,
                )
                self._state.schedule_custom_mode = resolved_mode
            elif isinstance(self._protocol, AromelyAroMaxProtocol):
                slot = ScheduleSlot(
                    start_hour=s_h, start_minute=s_m, end_hour=e_h, end_minute=e_m,
                    enabled=enabled, work_seconds=work, pause_seconds=pause,
                )
                cmd = self._protocol.build_schedule(slot, weekday_mask=weekday_mask)

            if cmd and await self._ble_execute(cmd):
                self._notify_state_changed()
                return True

        # Cloud fallback
        if self.supports_cloud and self._cloud:
            day_indices = [i + 1 for i in range(7) if weekday_mask & (1 << i)]
            success = await self._cloud.set_schedule(
                self._cloud_device_id,
                work_seconds=work, pause_seconds=pause,
                weekdays=day_indices,
                start_time=f"{s_h:02d}:{s_m:02d}",
                end_time=f"{e_h:02d}:{e_m:02d}",
            )
            if success:
                self._notify_state_changed()
            return success

        return False

    def _ak_v3_schedule_writes_disabled(self) -> bool:
        """Keep V3 schedules read-only until slot-safe writes are proven."""
        return (
            isinstance(self._protocol, ScentMarketingAkProtocol)
            and self._protocol.is_v3
        )

    async def refresh_state(self) -> None:
        """Refresh device state."""
        if self._ble_address:
            if await self._ble_connect():
                try:
                    await self._ble_send(self._protocol.build_query())
                    await asyncio.sleep(1.0)
                    # Some protocols expose extra read-registers that the
                    # device only reports on demand (e.g. Aroma-Link's oil
                    # level). Query them too when the protocol offers one.
                    oil_query = getattr(self._protocol, "build_oil_query", None)
                    if oil_query is not None:
                        await self._ble_send(oil_query())
                        await asyncio.sleep(0.3)
                    work_query = getattr(self._protocol, "build_all_work_query", None)
                    if work_query is not None:
                        await self._ble_send(work_query())
                        await asyncio.sleep(0.3)
                except (BleakError, asyncio.TimeoutError, OSError) as err:
                    _LOGGER.debug("BLE refresh query failed on %s: %s", self._ble_name, err)
                    self._ble_last_failure_ts = asyncio.get_event_loop().time()
                    async with self._ble_lock:
                        await self._teardown_ble_client(reason="refresh-failure")
            return

        if self.supports_cloud and self._cloud:
            status = await self._cloud.get_status(self._cloud_device_id)
            if status:
                if "power" in status and status["power"] is not None:
                    self._state.power = status["power"]
                if "phase" in status:
                    self._state.phase = status["phase"]
                # The cloud work-status payload carries the same live
                # countdown the BLE 52 0A frame does (plus oil/battery
                # on devices that report them) — feed it into the same
                # state fields so the sensors work in cloud mode too.
                if status.get("work_remain") is not None:
                    self._state.work_remaining = int(status["work_remain"])
                if status.get("pause_remain") is not None:
                    self._state.pause_remaining = int(status["pause_remain"])
                if status.get("oil_remaining") is not None:
                    self._state.oil_remaining = status["oil_remaining"]
                if status.get("battery") is not None:
                    self._state.battery = status["battery"]
                self._notify_state_changed()

    async def sync_time(self) -> bool:
        """Sync device clock to current local time (BLE only)."""
        if not self._ble_address:
            return False
        self._ble_has_synced_time = False
        # The manual control must not start a schedule-table read.
        return await self._ble_connect(read_ak_state=False)

    # ------------------------------------------------------------------
    # Startup / Shutdown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Initial setup - query state once."""
        await self._async_restore_ak_v3_schedules()
        if getattr(self, "_state", None) is not None and self._state.ak_v3_schedules:
            self._notify_state_changed()
        try:
            if isinstance(self._protocol, ScentMarketingAkProtocol) and self._ble_address:
                if await self._ble_connect(
                    read_ak_state=False, keep_connected=True, startup_ak_v3_session=True
                ) and self._protocol.is_v3:
                    await self._async_start_ak_v3_startup_reads()
                elif not self._protocol.is_v3:
                    await self.refresh_state()
            else:
                await self.refresh_state()
        except Exception as err:
            # Entity setup must not discard a connected client because its
            # optional initial read failed. Keep the error available for the
            # next diagnostic or on-demand operation.
            self._ble_connection_stage = "initial_query_failed"
            self._ble_connection_error = str(err)
            _LOGGER.warning("Initial state query failed for %s: %s", self._ble_name, err)

    async def async_shutdown(self) -> None:
        """Clean up resources."""
        await self.async_cancel_initialization()
        if self._momentary_task and not self._momentary_task.done():
            self._momentary_task.cancel()
        reconnect = self._ble_reconnect_task
        if reconnect and not reconnect.done():
            reconnect.cancel()
        disconnect = self._ble_disconnect_task
        if disconnect and not disconnect.done():
            disconnect.cancel()
        async with self._ble_lock:
            await self._teardown_ble_client(reason="shutdown")
        if self._cloud and hasattr(self._cloud, "close"):
            await self._cloud.close()
