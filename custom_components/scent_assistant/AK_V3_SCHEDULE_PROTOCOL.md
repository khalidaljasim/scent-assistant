# Ultra Max Tower AK V3 Protocol Evidence

## Scope

These findings apply only to the SA Ultra Max Tower at MiCasa, PCB `DV1.0
V1.1`, diffuser firmware `A316`, and official app `3.0.9 (3092)`.

## Continuation Status (2026-08-29)

- One physical diffuser is in scope. Home Assistant displays `Ultra Max Tower`
  while preserving learned write prefix `SA_`; label is `MiCasa`; fragrance is
  `Mountain Mist`. Label and fragrance are distinct fields.
- Full read mapping: `C1/42` name, `C2/43` label, `C3/44` firmware, `C4/45`
  model, `C5/46` limits, `C6/47` grade table, `C7/48` fragrance table,
   `C8/4B` oil amounts, and `CE/50` flow. Schedule records are `4A`; normal
   schedule writes are `2A`. V3 collects the direct `4A` push stream armed
   before `21`; `CA` acknowledgements are optional. `83`, `89`, and `86` are
   V2-only and are not sent in V3 production paths.
- The loaded source gates Core-start initialization until HA is RUNNING and
  platform forwarding has completed. It then uses one initialization task and
  serializes login/time sync, metadata, schedule read, grade, oil, and flow.
  This is an HA readiness correction, not a speculative protocol pause.
- Latest runtime accepted fresh `C1/42`, `C2/43`, `C7/48`, `C8/4B`, and `CE/50`
  values, including `699 mL` and `5.25 mL/h`. `C6/47` timed out, so the grade
  table and derived remaining-days value are unavailable. Do not substitute
  stale grade data.
- The remaining-days source mirrors the APK's ordered overlap processing,
  midnight handling, integer duty truncation, and reversed weekday index
  (Sunday 6 through Saturday 0). It needs five fresh schedules, grade table,
  oil, and flow; no fixed days result is valid across input sets.
- AK login and password changes are separate. AK password changing remains
  unsupported; `password_required` belongs to GW state. No additional reload,
  restart, write, calibration, time sync, or dashboard change is authorized.

## Proven Schedule Flow

- Metadata and the direct-push table collector are both armed before dynamic
  `21`, then the collector accepts five ordered `4A` records for endpoint 1.
- The reader may send a bounded per-record `CA` acknowledgement, including
  `CA 01 05`. Acknowledgement transport never changes table completion.
- A normal schedule update clones a fresh physical `4A` record, sends one `2A`
  frame, and obtains a fresh complete table read for comparison.
- Slot 1 persisted after the normal update and was restored to `05:00`:
  `4a0102030001070500051f7f000a000f012c`.
- Slots 2 through 5 matched their established baseline frames after that update.

## Supported Controls

- Schedule 1 through 5 use physical endpoint 1 records.
- Fixed mode is represented by mode byte `00` and is exposed as `Fixed`.
- Custom mode is not exposed until capability data confirms custom support and
  usable limits for this device.
- Manual AK V3 clock sync uses `21 03 YY MM DD HH mm ss`. It is never sent
  automatically as part of an entity repair or verification action.

## Unsupported Paths Removed

- Raw-baseline recovery and commit behavior are removed.
- Direct slot probing and reconciliation are removed.
- Endpoint 2 is not a physical Ultra Max Tower target.
- Public slot creation and deletion remain unsupported. The protected lifecycle
  verification service may baseline an empty slot, create/read/delete/read it,
  and rolls back on failure; it must not be invoked without explicit approval.

## Confirmed Metadata And Oil Writes

- Device name is `22 <append-prefix><UTF-8 editable name>`; the editable name is
  limited to 16 UTF-8 bytes and a known append prefix must be retained exactly.
- Device label is `23 <UTF-8 label>`.
- Fragrance names are `28` followed by ordered, zero-padded 16-byte UTF-8
  records. The record position, not an embedded identifier, selects an aroma.
- Oil amount is `2B <preserved status> <total:u16-be> <remaining:u16-be>` per
  aroma. The status byte is read from the `4B` response and is never invented.
- Oil calculation is `30` followed by seven-byte ordered records:
  `00 <flow*100:u16-be> <estimated-days:u16-be> 00 00`.
- A normal oil save and calibration send `2B`, wait 200 ms, then send `30`.
  Fresh `C8` and `CE` reads follow. GATT completion alone is not success.
- Calibration records a measured remaining amount. It updates flow only when
  the APK's prior-calibration prerequisites are met; otherwise it preserves the
  existing flow.
- `83`, `89`, and `86` belong only to the isolated V2 polling flow. AK V3
  schedule completion requires five validated direct `4A` records; capability-approved
  `CA <aroma> <slot>` acknowledgements are sent only where approved. AK V3 production
  paths do not transmit `83`, `89`, or `86`. The existing derived-days calculation
  remains authoritative; the `0x50` stored-days field is diagnostic only.
- Password encoder `0F <four UTF-8 characters> OK01` is staged and tested only.
  It is separate from the existing fixed AK `0x8F` login handshake; no AK
  credential storage, reauthentication, or password-change service is staged.
