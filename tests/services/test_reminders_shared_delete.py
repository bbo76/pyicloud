"""Fail-closed shared Reminders soft-delete tests; no authenticated iCloud calls."""
# pylint: disable=protected-access

from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from pyicloud.common.cloudkit import (
    CKLookupResponse,
    CKModifyResponse,
    CKRecord,
    CKZoneIDReq,
)
from pyicloud.services.reminders.client import CloudKitRemindersClient
from pyicloud.services.reminders.exceptions import RemindersApiError
from pyicloud.services.reminders.models import Reminder, RemindersList
from pyicloud.services.reminders.service import RemindersService

ZONE = {"zoneName": "SharedZone", "ownerRecordName": "owner"}


def test_lookup_sends_exact_zone_and_preserves_missing_record_zone() -> None:
    """Parsed omission is real: the shared request still specifies zone and owner."""
    client = CloudKitRemindersClient(
        "https://example.invalid/database/1/com.apple.reminders/production/shared",
        MagicMock(),
        {},
    )
    with patch.object(
        client._client._http,
        "post",
        return_value={
            "records": [
                {"recordName": "Reminder/1", "recordType": "Reminder", "fields": {}}
            ]
        },
    ) as post:
        response = client.lookup(["Reminder/1"], CKZoneIDReq(**ZONE))
    path, payload = post.call_args.args
    assert path == "/records/lookup"
    assert payload["zoneID"] == ZONE
    assert payload["records"] == [{"recordName": "Reminder/1"}]
    assert isinstance(response.records[0], CKRecord)
    assert response.records[0].zoneID is None


def _record(name: str, record_type: str, **kwargs: object) -> CKRecord:
    """Construct a parsed synthetic CloudKit record."""
    return CKRecord.model_validate({
        "recordName": name,
        "recordType": record_type,
        "fields": {},
        **kwargs,
    })


def _setup(share_type: str = "cloudKit.share") -> tuple[Any, Reminder]:
    """Mock discovery, three shared lookups and one successful modify."""
    svc = RemindersService("https://example.invalid", MagicMock(), {})
    svc._raw = MagicMock()
    svc._shared_raw = MagicMock()
    cast(Any, svc._reads).lists = MagicMock(
        return_value=iter([
            RemindersList(
                id="List/SHARED",
                title="Synthetic",
                scope="shared",
                zone_name="SharedZone",
                zone_owner="owner",
            )
        ])
    )
    list_record = _record(
        "List/SHARED", "List", share={"recordName": "Share/1", "zoneID": ZONE}
    )
    share_record = _record(
        "Share/1",
        share_type,
        zoneID=ZONE,
        parent={"recordName": "List/SHARED"},
        currentUserParticipant={
            "permission": "READ_WRITE",
            "acceptanceStatus": "ACCEPTED",
        },
    )
    reminder_record = _record(
        "Reminder/1",
        "Reminder",
        zoneID=ZONE,
        recordChangeTag="current",
        fields={
            "List": {
                "type": "REFERENCE",
                "value": {"recordName": "List/SHARED", "action": "VALIDATE"},
            }
        },
    )
    replies = [
        CKLookupResponse(records=[list_record]),
        CKLookupResponse(records=[share_record]),
        CKLookupResponse(records=[reminder_record]),
    ]
    svc._shared_raw.lookup.replies = replies
    svc._shared_raw.lookup.side_effect = lambda **_: replies[
        len(svc._shared_raw.lookup.call_args_list) - 1
    ]
    svc._shared_raw.modify.return_value = CKModifyResponse(
        records=[_record("Reminder/1", "Reminder", recordChangeTag="next")]
    )
    reminder = Reminder(
        id="Reminder/1",
        list_id="List/SHARED",
        title="Synthetic",
        completed=False,
        record_change_tag="current",
    )
    return cast(Any, svc), reminder


@pytest.mark.parametrize("share_type", ["cloudKit.share", "cloudkit.share"])
def test_shared_delete_writes_only_shared_zone(share_type: str) -> None:
    """Both share record spellings route a soft delete to the owner zone."""
    svc, reminder = _setup(share_type)
    svc.delete(reminder)
    assert svc._shared_raw.lookup.call_count == 3
    assert all(
        call.kwargs["zone_id"].ownerRecordName == "owner"
        for call in svc._shared_raw.lookup.call_args_list
    )
    op = svc._shared_raw.modify.call_args.kwargs["operations"][0]
    zone = svc._shared_raw.modify.call_args.kwargs["zone_id"]
    assert (zone.zoneName, zone.ownerRecordName) == ("SharedZone", "owner")
    assert op.operationType == "update"
    assert op.record.recordChangeTag == "current"
    assert op.record.fields["Deleted"].value == 1
    assert "Completed" not in op.record.fields
    assert (reminder.deleted, reminder.completed) == (True, False)
    svc._raw.modify.assert_not_called()
    svc._raw.lookup.assert_not_called()


def test_shared_delete_accepts_missing_record_zone_only_with_verified_route() -> None:
    """An exact shared-zone lookup can establish zone when record omits zoneID."""
    svc, reminder = _setup()
    svc._shared_raw.lookup.replies[2].records[0].zoneID = None
    svc.delete(reminder)
    assert svc._shared_raw.lookup.call_count == 3
    assert all(
        call.kwargs["record_names"] == [name]
        and (call.kwargs["zone_id"].zoneName, call.kwargs["zone_id"].ownerRecordName)
        == ("SharedZone", "owner")
        for call, name in zip(
            svc._shared_raw.lookup.call_args_list,
            ("List/SHARED", "Share/1", "Reminder/1"),
            strict=True,
        )
    )
    svc._shared_raw.modify.assert_called_once()
    svc._raw.modify.assert_not_called()


@pytest.mark.parametrize(
    "permission,status",
    [
        ("READ_ONLY", "ACCEPTED"),
        ("READ_WRITE", "PENDING"),
        ("READ_WRITE", "INVITED"),
    ],
)
def test_shared_delete_requires_accepted_write(permission: str, status: str) -> None:
    """Neither read-only access nor an unaccepted invitation permits writing."""
    svc, reminder = _setup()
    share = svc._shared_raw.lookup.replies[1].records[0]
    share.currentUserParticipant.permission = permission
    share.currentUserParticipant.acceptanceStatus = status
    _reject(svc, reminder)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_shared_delete_rejects_missing_lookup(index: int) -> None:
    """Every required list, share and reminder lookup must resolve uniquely."""
    svc, reminder = _setup()
    svc._shared_raw.lookup.replies[index] = CKLookupResponse(records=[])
    _reject(svc, reminder)


@pytest.mark.parametrize(
    "index,part",
    [
        (1, "zoneName"),
        (1, "ownerRecordName"),
        (2, "zoneName"),
        (2, "ownerRecordName"),
    ],
)
def test_shared_delete_rejects_wrong_zone(index: int, part: str) -> None:
    """Reject any conflicting zone or owner on share and reminder records."""
    svc, reminder = _setup()
    rec = svc._shared_raw.lookup.replies[index].records[0]
    setattr(rec.zoneID, part, "wrong")
    _reject(svc, reminder)


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("part", ["zoneName", "ownerRecordName"])
def test_shared_delete_rejects_conflicting_lookup_envelope(
    index: int, part: str
) -> None:
    """Any explicit lookup zone contradicting the request blocks modification."""
    svc, reminder = _setup()
    svc._shared_raw.lookup.replies[2].records[0].zoneID = None
    conflicting = dict(ZONE)
    conflicting[part] = "other"
    records = svc._shared_raw.lookup.replies[index].records
    svc._shared_raw.lookup.replies[index] = CKLookupResponse.model_validate({
        "records": [rec.model_dump(mode="json", exclude_none=True) for rec in records],
        "zoneID": conflicting,
    })
    _reject(svc, reminder)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_share",
        "wrong_share_zone",
        "wrong_share_parent",
        "missing_rights",
        "wrong_membership",
        "stale_tag",
        "missing_tag",
        "already_deleted",
        "duplicate_list",
        "missing_list",
        "duplicate_reminder",
        "missing_zone_and_share_proof",
        "conflicting_response_zone",
        "missing_route_owner",
        "wrong_record_name",
        "wrong_record_type",
        "deleted_record",
        "missing_list_reference",
        "wrong_share_type",
        "unsupported_route",
    ],
)
@pytest.mark.parametrize("reminder_zone_missing", [False, True])
def test_shared_delete_fails_closed(fault: str, reminder_zone_missing: bool) -> None:
    """Reject missing links, stale versions, duplicates and unsupported routes."""
    svc, reminder = _setup()
    replies = svc._shared_raw.lookup.replies
    list_rec, share_rec, reminder_rec = (reply.records[0] for reply in replies)
    if reminder_zone_missing:
        reminder_rec.zoneID = None
    if fault == "missing_share":
        list_rec.share = None
    elif fault == "wrong_share_zone":
        list_rec.share.zoneID.ownerRecordName = "other"
    elif fault == "wrong_share_parent":
        share_rec.parent.recordName = "List/OTHER"
    elif fault == "missing_rights":
        share_rec.currentUserParticipant = None
    elif fault == "wrong_membership":
        reminder_rec.fields["List"].value.recordName = "List/OTHER"
    elif fault == "stale_tag":
        reminder_rec.recordChangeTag = "newer"
    elif fault == "missing_tag":
        reminder.record_change_tag = None
    elif fault == "already_deleted":
        reminder_rec.fields["Deleted"] = _record(
            "Reminder/1", "Reminder", fields={"Deleted": {"type": "INT64", "value": 1}}
        ).fields["Deleted"]
    elif fault == "duplicate_list":
        svc._reads.lists.return_value = iter([
            RemindersList(id="List/SHARED", title="a"),
            RemindersList(id="List/SHARED", title="b"),
        ])
    elif fault == "missing_list":
        svc._reads.lists.return_value = iter([])
    elif fault == "duplicate_reminder":
        replies[2] = CKLookupResponse(records=[reminder_rec, reminder_rec])
    elif fault == "missing_zone_and_share_proof":
        reminder_rec.zoneID = None
        share_rec.zoneID = None
    elif fault == "conflicting_response_zone":
        reminder_rec.zoneID = None
        replies[2] = CKLookupResponse.model_validate({
            "records": [reminder_rec.model_dump(mode="json", exclude_none=True)],
            "zoneID": {"zoneName": "Other", "ownerRecordName": "owner"},
        })
    elif fault == "missing_route_owner":
        svc._reads.lists.return_value = iter([
            RemindersList(
                id="List/SHARED", title="a", scope="shared", zone_name="SharedZone"
            )
        ])
    elif fault == "wrong_record_name":
        reminder_rec.recordName = "Reminder/OTHER"
    elif fault == "wrong_record_type":
        reminder_rec.recordType = "Other"
    elif fault == "deleted_record":
        reminder_rec.deleted = True
    elif fault == "missing_list_reference":
        del reminder_rec.fields["List"]
    elif fault == "wrong_share_type":
        share_rec.recordType = "Other"
    else:
        svc._reads.lists.return_value = iter([
            RemindersList(id="List/SHARED", title="a", scope="shared")
        ])
    _reject(svc, reminder)


def _reject(svc: Any, reminder: Reminder) -> None:
    """Assert rejection leaves both databases and the local model unchanged."""
    with pytest.raises(RemindersApiError):
        svc.delete(reminder)
    svc._shared_raw.modify.assert_not_called()
    svc._raw.modify.assert_not_called()
    assert reminder.deleted is False


def test_stale_private_cache_never_redirects_shared_delete() -> None:
    """A stale private cache entry cannot override fresh shared discovery."""
    svc, reminder = _setup()
    svc._list_zones["List/SHARED"] = ("private", "Reminders", None)
    svc.delete(reminder)
    svc._shared_raw.modify.assert_called_once()
    svc._raw.modify.assert_not_called()


def test_shared_lookup_transport_failure_never_modifies() -> None:
    """A shared read failure cannot fall through to either writer."""
    svc, reminder = _setup()
    svc._shared_raw.lookup.side_effect = RemindersApiError("Lookup failed")
    _reject(svc, reminder)


def test_private_delete_keeps_private_payload() -> None:
    """Private deletion retains the original zone and soft-delete fields."""
    svc, reminder = _setup()
    reminder.list_id = "List/PRIVATE"
    svc._reads.lists.return_value = iter([
        RemindersList(id="List/PRIVATE", title="Private")
    ])
    svc._raw.modify.return_value = CKModifyResponse(
        records=[_record("Reminder/1", "Reminder", recordChangeTag="next")]
    )
    svc.delete(reminder)
    op = svc._raw.modify.call_args.kwargs["operations"][0]
    assert op.record.fields["Deleted"].value == 1
    assert "Completed" not in op.record.fields
    assert svc._raw.modify.call_args.kwargs["zone_id"].zoneName == "Reminders"
    svc._shared_raw.modify.assert_not_called()
