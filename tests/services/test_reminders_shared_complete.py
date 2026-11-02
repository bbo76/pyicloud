"""Mocked shared completion safety checks; no live iCloud requests."""
# pylint: disable=protected-access

import json
from typing import Any

import pytest

from pyicloud.common.cloudkit import CKErrorItem, CKLookupResponse, CKModifyResponse
from pyicloud.services.reminders.exceptions import RemindersApiError
from pyicloud.services.reminders.models import RemindersList
from tests.services.test_reminders_shared_delete import _record, _setup


def test_complete_shared_writes_only_completion_fields() -> None:
    """Completion uses the shared owner zone and leaves other fields untouched."""
    svc, reminder = _setup()
    reminder.priority = 5
    reminder.desc = "keep this"
    reminder.due_date = None
    rec = svc._shared_raw.lookup.replies[2].records[0]
    rec.fields["Completed"] = _record(
        "Reminder/1", "Reminder", fields={"Completed": {"type": "INT64", "value": 0}}
    ).fields["Completed"]
    rec.fields["ResolutionTokenMap"] = _record(
        "Reminder/1",
        "Reminder",
        fields={
            "ResolutionTokenMap": {
                "type": "STRING",
                "value": json.dumps({"map": {"priority": {"counter": 4}}}),
            }
        },
    ).fields["ResolutionTokenMap"]

    svc.complete(reminder)

    assert svc._shared_raw.lookup.call_count == 3
    assert [
        call.kwargs["record_names"] for call in svc._shared_raw.lookup.call_args_list
    ] == [["List/SHARED"], ["Share/1"], ["Reminder/1"]]
    assert all(
        call.kwargs["zone_id"].ownerRecordName == "owner"
        for call in svc._shared_raw.lookup.call_args_list
    )
    svc._raw.modify.assert_not_called()
    svc._raw.lookup.assert_not_called()

    svc._shared_raw.modify.assert_called_once()
    kwargs = svc._shared_raw.modify.call_args.kwargs
    assert (kwargs["zone_id"].zoneName, kwargs["zone_id"].ownerRecordName) == (
        "SharedZone",
        "owner",
    )
    (op,) = kwargs["operations"]
    assert (op.operationType, op.record.recordName, op.record.recordChangeTag) == (
        "update",
        "Reminder/1",
        "current",
    )
    fields = op.record.fields
    assert set(fields) == {
        "Completed",
        "CompletionDate",
        "LastModifiedDate",
        "ResolutionTokenMap",
    }
    assert fields["Completed"].value == 1
    assert fields["CompletionDate"].value == fields["LastModifiedDate"].value
    tokens = json.loads(fields["ResolutionTokenMap"].value)["map"]
    assert set(tokens) == {
        "completed",
        "completionDate",
        "lastModifiedDate",
        "priority",
    }
    assert tokens["priority"] == {"counter": 4}
    assert (reminder.completed, reminder.deleted, reminder.record_change_tag) == (
        True,
        False,
        "next",
    )
    assert reminder.completed_date == reminder.modified
    assert (reminder.priority, reminder.desc, reminder.due_date) == (
        5,
        "keep this",
        None,
    )


def test_update_shared_never_uses_generic_private_route() -> None:
    """The old general-purpose update cannot silently write a shared item privately."""
    svc, reminder = _setup()
    with pytest.raises(RemindersApiError):
        svc.update(reminder)
    svc._raw.lookup.assert_not_called()
    svc._raw.modify.assert_not_called()
    svc._shared_raw.modify.assert_not_called()


def test_complete_accepts_omitted_reminder_zone_with_exact_lookup() -> None:
    """Missing response zone is acceptable only after verified shared routing."""
    svc, reminder = _setup()
    svc._shared_raw.lookup.replies[2].records[0].zoneID = None
    svc._list_zones[reminder.list_id] = ("private", "Reminders", None)
    svc.complete(reminder)
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
def test_complete_rejects_unaccepted_or_read_only_share(
    permission: str, status: str
) -> None:
    """Accepted read-write participation is mandatory for shared completion."""
    svc, reminder = _setup()
    share = svc._shared_raw.lookup.replies[1].records[0]
    share.currentUserParticipant.permission = permission
    share.currentUserParticipant.acceptanceStatus = status
    _reject(svc, reminder)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_list",
        "ambiguous_list",
        "missing_share",
        "missing_share_record",
        "missing_membership",
        "wrong_membership",
        "stale_tag",
        "missing_tag",
        "already_completed_local",
        "already_completed_remote",
        "already_deleted_local",
        "already_deleted_remote",
        "tombstone",
        "wrong_zone",
        "wrong_owner",
        "wrong_envelope_zone",
        "missing_share_proof",
        "missing_owner",
        "missing_reminder",
        "ambiguous_reminder",
        "wrong_record_type",
    ],
)
def test_complete_fails_closed(fault: str) -> None:
    """Any missing or conflicting route, version or state prevents modification."""
    svc, reminder = _setup()
    replies = svc._shared_raw.lookup.replies
    list_rec, share, rec = (reply.records[0] for reply in replies)
    if fault == "missing_list":
        svc._reads.lists.return_value = iter([])
    elif fault == "ambiguous_list":
        svc._reads.lists.return_value = iter([
            RemindersList(id=reminder.list_id, title="a"),
            RemindersList(id=reminder.list_id, title="b"),
        ])
    elif fault == "missing_share":
        list_rec.share = None
    elif fault == "missing_share_record":
        replies[1] = CKLookupResponse(records=[])
    elif fault == "missing_membership":
        del rec.fields["List"]
    elif fault == "wrong_membership":
        rec.fields["List"].value.recordName = "List/OTHER"
    elif fault == "stale_tag":
        rec.recordChangeTag = "newer"
    elif fault == "missing_tag":
        reminder.record_change_tag = None
    elif fault == "already_completed_local":
        reminder.completed = True
    elif fault == "already_completed_remote":
        rec.fields["Completed"] = _record(
            "Reminder/1",
            "Reminder",
            fields={"Completed": {"type": "INT64", "value": 1}},
        ).fields["Completed"]
    elif fault == "already_deleted_local":
        reminder.deleted = True
    elif fault == "already_deleted_remote":
        rec.fields["Deleted"] = _record(
            "Reminder/1", "Reminder", fields={"Deleted": {"type": "INT64", "value": 1}}
        ).fields["Deleted"]
    elif fault == "tombstone":
        rec.deleted = True
    elif fault in {"wrong_zone", "wrong_owner"}:
        setattr(
            rec.zoneID,
            "zoneName" if fault == "wrong_zone" else "ownerRecordName",
            "other",
        )
    elif fault == "wrong_envelope_zone":
        rec.zoneID = None
        replies[2] = CKLookupResponse.model_validate({
            "records": [rec.model_dump(mode="json", exclude_none=True)],
            "zoneID": {"zoneName": "other", "ownerRecordName": "owner"},
        })
    elif fault == "missing_share_proof":
        rec.zoneID = None
        share.zoneID = None
    elif fault == "missing_owner":
        svc._reads.lists.return_value = iter([
            RemindersList(
                id=reminder.list_id, title="a", scope="shared", zone_name="SharedZone"
            )
        ])
    elif fault == "missing_reminder":
        replies[2] = CKLookupResponse(records=[])
    elif fault == "ambiguous_reminder":
        replies[2] = CKLookupResponse(records=[rec, rec])
    else:
        rec.recordType = "Other"
    _reject(svc, reminder)


def _reject(svc: Any, reminder: Any) -> None:
    before = reminder.model_dump()
    with pytest.raises(RemindersApiError):
        svc.complete(reminder)
    assert reminder.model_dump() == before
    svc._shared_raw.modify.assert_not_called()
    svc._raw.modify.assert_not_called()


def test_private_complete_keeps_private_route_and_narrow_payload() -> None:
    """Private completion remains in the private zone without shared writes."""
    svc, reminder = _setup()
    reminder.list_id = "List/PRIVATE"
    svc._reads.lists.return_value = iter([
        RemindersList(id="List/PRIVATE", title="Private")
    ])
    svc._raw.lookup.return_value = CKLookupResponse(
        records=[
            _record(
                "Reminder/1",
                "Reminder",
                fields={"Completed": {"type": "INT64", "value": 0}},
            )
        ]
    )
    svc._raw.modify.return_value = svc._shared_raw.modify.return_value
    svc.complete(reminder)
    fields = svc._raw.modify.call_args.kwargs["operations"][0].record.fields
    assert set(fields) == {
        "Completed",
        "CompletionDate",
        "LastModifiedDate",
        "ResolutionTokenMap",
    }
    assert svc._raw.modify.call_args.kwargs["zone_id"].zoneName == "Reminders"
    svc._shared_raw.lookup.assert_not_called()
    svc._shared_raw.modify.assert_not_called()


def test_complete_failed_modify_keeps_local_model() -> None:
    """A rejected server write cannot claim local completion."""
    svc, reminder = _setup()
    svc._shared_raw.modify.side_effect = RemindersApiError("server rejected")
    before = reminder.model_dump()
    with pytest.raises(RemindersApiError, match="server rejected"):
        svc.complete(reminder)
    assert reminder.model_dump() == before
    svc._shared_raw.modify.assert_called_once()
    svc._raw.modify.assert_not_called()


def test_complete_rejects_malformed_existing_resolution_tokens() -> None:
    """Never replace unknown device conflict tokens with a fresh map."""
    svc, reminder = _setup()
    rec = svc._shared_raw.lookup.replies[2].records[0]
    rec.fields["ResolutionTokenMap"] = _record(
        "Reminder/1",
        "Reminder",
        fields={"ResolutionTokenMap": {"type": "STRING", "value": "{not-json"}},
    ).fields["ResolutionTokenMap"]
    _reject(svc, reminder)


def test_complete_rejects_stale_server_modify_error() -> None:
    """An update conflict after lookup leaves the local object unchanged."""
    svc, reminder = _setup()
    svc._shared_raw.modify.return_value = CKModifyResponse(
        records=[CKErrorItem(recordName="Reminder/1", serverErrorCode="CONFLICT")]
    )
    before = reminder.model_dump()
    with pytest.raises(RemindersApiError, match="CONFLICT"):
        svc.complete(reminder)
    assert reminder.model_dump() == before
    svc._shared_raw.modify.assert_called_once()
    svc._raw.modify.assert_not_called()
