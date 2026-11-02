"""Synthetic CloudKit shared-create routing and authorization tests."""
# pylint: disable=protected-access

from typing import cast
from unittest.mock import MagicMock

import pytest

from pyicloud.common.cloudkit import (
    CKLookupResponse,
    CKModifyResponse,
    CKZoneChangesResponse,
    CKZoneListResponse,
)
from pyicloud.services.reminders.exceptions import RemindersApiError
from pyicloud.services.reminders.models import RemindersList
from pyicloud.services.reminders.service import RemindersService

ZONE = {"zoneName": "SyntheticZone", "ownerRecordName": "synthetic-owner"}
LIST = "List/SYNTHETIC"
SHARE = "cloudKit.share/SYNTHETIC"


def _record(name: str, kind: str, **extras: object) -> dict[str, object]:
    return {"recordName": name, "recordType": kind, "fields": {}, **extras}


def _service() -> RemindersService:
    service = RemindersService("https://example.invalid", MagicMock(), {})
    service._raw = MagicMock()
    service._shared_raw = MagicMock()
    service._raw.changes.return_value = CKZoneChangesResponse(zones=[])
    service._shared_raw.zones_list.return_value = CKZoneListResponse.model_validate({
        "zones": [{"zoneID": ZONE}]
    })
    service._shared_raw.changes.return_value = CKZoneChangesResponse.model_validate({
        "zones": [
            {
                "zoneID": ZONE,
                "syncToken": "synthetic",
                "records": [
                    _record(
                        LIST,
                        "List",
                        fields={"Name": {"type": "STRING", "value": "Synthetic"}},
                    )
                ],
            }
        ]
    })
    service._shared_raw.lookup.side_effect = [
        CKLookupResponse.model_validate({
            "records": [
                _record(LIST, "List", share={"recordName": SHARE, "zoneID": ZONE})
            ]
        }),
        CKLookupResponse.model_validate({
            "records": [
                _record(
                    SHARE,
                    "cloudKit.share",
                    zoneID=ZONE,
                    currentUserParticipant={
                        "permission": "READ_WRITE",
                        "acceptanceStatus": "ACCEPTED",
                    },
                )
            ]
        }),
        CKLookupResponse.model_validate({
            "records": [
                _record(
                    "Reminder/NEW",
                    "Reminder",
                    fields={
                        "List": {"type": "REFERENCE", "value": {"recordName": LIST}}
                    },
                )
            ]
        }),
    ]
    service._shared_raw.modify.return_value = CKModifyResponse.model_validate({
        "records": [_record("Reminder/NEW", "Reminder")]
    })
    return service


def test_shared_create_accepts_lowercase_cloudkit_share_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CloudKit's lowercase share type still permits a verified shared write."""
    service = _service()
    monkeypatch.setattr("pyicloud.services.reminders._writes.uuid.uuid4", lambda: "NEW")
    lookups = list(cast(MagicMock, service._shared_raw).lookup.side_effect)
    lookups[1] = CKLookupResponse.model_validate({
        "records": [
            _record(
                SHARE,
                "cloudkit.share",
                zoneID=ZONE,
                currentUserParticipant={
                    "permission": "READ_WRITE",
                    "acceptanceStatus": "ACCEPTED",
                },
            )
        ]
    })
    cast(MagicMock, service._shared_raw).lookup.side_effect = lookups

    result = service.create(LIST, "Synthetic")

    assert result.list_id == LIST
    cast(MagicMock, service._shared_raw).modify.assert_called_once()
    cast(MagicMock, service._raw).modify.assert_not_called()


def test_shared_create_uses_verified_share_and_exact_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted writable share routes creation to the owning shared zone."""
    service = _service()
    monkeypatch.setattr("pyicloud.services.reminders._writes.uuid.uuid4", lambda: "NEW")

    result = service.create(LIST, "Synthetic")

    assert result.list_id == LIST
    assert cast(MagicMock, service._shared_raw).lookup.call_count == 3
    calls = cast(MagicMock, service._shared_raw).lookup.call_args_list
    assert calls[0].kwargs["record_names"] == [LIST]
    assert calls[1].kwargs["record_names"] == [SHARE]
    for call in calls:
        assert (
            call.kwargs["zone_id"].zoneName,
            call.kwargs["zone_id"].ownerRecordName,
        ) == ("SyntheticZone", "synthetic-owner")
    modify = cast(MagicMock, service._shared_raw).modify.call_args.kwargs
    assert (modify["zone_id"].zoneName, modify["zone_id"].ownerRecordName) == (
        "SyntheticZone",
        "synthetic-owner",
    )
    op = modify["operations"][0]
    assert op.operationType == "create"
    assert op.record.parent.recordName == LIST
    assert op.record.fields["List"].value.recordName == LIST
    assert op.record.fields["Completed"].value == 0
    cast(MagicMock, service._raw).modify.assert_not_called()
    cast(MagicMock, service._raw).lookup.assert_not_called()


@pytest.mark.parametrize(
    "case",
    [
        "no_list",
        "duplicate_list",
        "no_share_ref",
        "wrong_ref_zone",
        "no_share_record",
        "wrong_share_type",
        "read_only",
        "pending",
        "unknown_permission",
        "missing_share_zone",
        "wrong_share_zone",
        "wrong_parent",
        "wrong_list_zone",
        "lookup_error",
        "network_error",
        "zone_envelope_mismatch",
    ],
)
def test_shared_create_fails_closed(case: str) -> None:
    """Absent or conflicting route and permission evidence never causes a write."""
    service = _service()
    lookups = list(cast(MagicMock, service._shared_raw).lookup.side_effect)
    if case == "no_list":
        cast(
            MagicMock, service._shared_raw
        ).changes.return_value = CKZoneChangesResponse(zones=[])
    elif case == "duplicate_list":
        zone = cast(MagicMock, service._shared_raw).changes.return_value.zones[0]
        zone.records.append(zone.records[0])
    elif case == "no_share_ref":
        lookups[0] = CKLookupResponse.model_validate({
            "records": [_record(LIST, "List")]
        })
    elif case == "wrong_ref_zone":
        lookups[0] = CKLookupResponse.model_validate({
            "records": [
                _record(
                    LIST,
                    "List",
                    share={
                        "recordName": SHARE,
                        "zoneID": {
                            "zoneName": "Other",
                            "ownerRecordName": "synthetic-owner",
                        },
                    },
                )
            ]
        })
    elif case == "no_share_record":
        lookups[1] = CKLookupResponse(records=[])
    elif case == "wrong_share_type":
        lookups[1] = CKLookupResponse.model_validate({
            "records": [_record(SHARE, "Reminder", zoneID=ZONE)]
        })
    elif case in ("read_only", "pending", "unknown_permission"):
        participant = {
            "permission": "READ_ONLY"
            if case == "read_only"
            else (None if case == "unknown_permission" else "READ_WRITE"),
            "acceptanceStatus": "PENDING" if case == "pending" else "ACCEPTED",
        }
        lookups[1] = CKLookupResponse.model_validate({
            "records": [
                _record(
                    SHARE,
                    "cloudKit.share",
                    zoneID=ZONE,
                    currentUserParticipant=participant,
                )
            ]
        })
    elif case in ("missing_share_zone", "wrong_share_zone", "wrong_parent"):
        kwargs: dict[str, object] = {
            "zoneID": None
            if case == "missing_share_zone"
            else (
                {"zoneName": "Other", "ownerRecordName": "synthetic-owner"}
                if case == "wrong_share_zone"
                else ZONE
            ),
            "currentUserParticipant": {
                "permission": "READ_WRITE",
                "acceptanceStatus": "ACCEPTED",
            },
        }
        if case == "wrong_parent":
            kwargs["parent"] = {"recordName": "List/OTHER"}
        lookups[1] = CKLookupResponse.model_validate({
            "records": [_record(SHARE, "cloudKit.share", **kwargs)]
        })
    elif case == "wrong_list_zone":
        lookups[0] = CKLookupResponse.model_validate({
            "records": [
                _record(
                    LIST,
                    "List",
                    zoneID={"zoneName": "Other", "ownerRecordName": "synthetic-owner"},
                    share={"recordName": SHARE, "zoneID": ZONE},
                )
            ]
        })
    elif case == "lookup_error":
        lookups[1] = CKLookupResponse.model_validate({
            "records": [{"serverErrorCode": "PERMISSION_DENIED", "recordName": SHARE}]
        })
    elif case == "network_error":
        cast(MagicMock, service._shared_raw).lookup.side_effect = OSError(
            "synthetic offline"
        )
    elif case == "zone_envelope_mismatch":
        zone = cast(MagicMock, service._shared_raw).changes.return_value.zones[0]
        zone.zoneID.zoneName = "Other"
    if case != "network_error":
        cast(MagicMock, service._shared_raw).lookup.side_effect = lookups

    with pytest.raises((RemindersApiError, OSError)):
        service.create(LIST, "Synthetic")

    cast(MagicMock, service._raw).modify.assert_not_called()
    cast(MagicMock, service._raw).lookup.assert_not_called()
    cast(MagicMock, service._shared_raw).modify.assert_not_called()


def test_private_create_preserves_private_client_and_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discovered private list retains the existing private write route."""
    service = _service()
    monkeypatch.setattr("pyicloud.services.reminders._writes.uuid.uuid4", lambda: "NEW")
    monkeypatch.setattr(
        service._reads, "lists", lambda: iter([RemindersList(id=LIST, title="Private")])
    )
    cast(
        MagicMock, service._raw
    ).modify.return_value = CKModifyResponse.model_validate({
        "records": [_record("Reminder/NEW", "Reminder")]
    })
    cast(
        MagicMock, service._raw
    ).lookup.return_value = CKLookupResponse.model_validate({
        "records": [
            _record(
                "Reminder/NEW",
                "Reminder",
                fields={"List": {"type": "REFERENCE", "value": {"recordName": LIST}}},
            )
        ]
    })

    result = service.create(LIST, "Synthetic")

    assert result.list_id == LIST
    assert (
        cast(MagicMock, service._raw).modify.call_args.kwargs["zone_id"].zoneName
        == "Reminders"
    )
    assert (
        cast(MagicMock, service._raw).lookup.call_args.kwargs["zone_id"].zoneName
        == "Reminders"
    )
    cast(MagicMock, service._shared_raw).modify.assert_not_called()
    cast(MagicMock, service._shared_raw).lookup.assert_not_called()


def test_shared_subtask_requires_verified_parent() -> None:
    """An unchecked subtask parent never reaches either modify endpoint."""
    service = _service()
    with pytest.raises(RemindersApiError):
        service.create(LIST, "Synthetic", parent_reminder_id="Reminder/OTHER")
    cast(MagicMock, service._raw).modify.assert_not_called()
    cast(MagicMock, service._shared_raw).modify.assert_not_called()


def test_shared_hydration_rejects_wrong_list_without_private_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-create hydration rejects a different list without private lookup."""
    service = _service()
    monkeypatch.setattr("pyicloud.services.reminders._writes.uuid.uuid4", lambda: "NEW")
    responses = list(cast(MagicMock, service._shared_raw).lookup.side_effect)
    responses[2] = CKLookupResponse.model_validate({
        "records": [
            _record(
                "Reminder/NEW",
                "Reminder",
                fields={
                    "List": {"type": "REFERENCE", "value": {"recordName": "List/OTHER"}}
                },
            )
        ]
    })
    cast(MagicMock, service._shared_raw).lookup.side_effect = responses
    with pytest.raises(RemindersApiError):
        service.create(LIST, "Synthetic")
    cast(MagicMock, service._raw).modify.assert_not_called()


def test_shared_modify_error_never_retries_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared API refusal never triggers a second write in private."""
    service = _service()
    monkeypatch.setattr("pyicloud.services.reminders._writes.uuid.uuid4", lambda: "NEW")
    cast(
        MagicMock, service._shared_raw
    ).modify.return_value = CKModifyResponse.model_validate({
        "records": [
            {"serverErrorCode": "PERMISSION_DENIED", "recordName": "Reminder/NEW"}
        ]
    })
    with pytest.raises(RemindersApiError):
        service.create(LIST, "Synthetic")
    cast(MagicMock, service._shared_raw).modify.assert_called_once()
    cast(MagicMock, service._raw).modify.assert_not_called()
    cast(MagicMock, service._raw).lookup.assert_not_called()
