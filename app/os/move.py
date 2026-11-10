"""POST /api/move: move an item between Personal and a workspace, either way (spec
2026-11-09-workspaces-design, "Moving items").

- A project brings its tasks (with subtasks), notes and events; a task brings its subtasks;
  every moved item brings its files.
- Who may: from Personal, its owner; from a workspace, whoever added it or an admin; into a
  workspace, only a member. Anything else answers 404.
- Into a workspace, personal links (category, tags, goal, course, trip, person, household)
  are cleared. Back to Personal, the item becomes the mover's and is unassigned.
- Assignees who aren't members of the destination are cleared, and no link is left pointing
  into another space (a task's project or parent, an event's task or project, a note's project).
"""

from datetime import timedelta
from typing import Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, StrictStr

from app.db import db
from app.os.common.deps import plus_user
from app.os.common.http import nest_error, nest_router
from app.os.common.ownership import LABELS
from app.os.common.space import ADMIN_ROLES, Space, member_role, space_for

router = nest_router("/move")

_LINKS_CLEARED = {
    "task": {"categoryId": None, "goalId": None, "courseId": None, "tripId": None},
    "event": {"categoryId": None, "goalId": None, "courseId": None, "tripId": None, "householdId": None},
    "project": {"categoryId": None, "goalId": None},
    "note": {"categoryId": None, "courseId": None, "tripId": None, "personId": None},
}
_HAS_TAGS = ("task", "note")
_ENTITY = {"task": "TASK", "event": "EVENT", "project": "PROJECT", "note": "NOTE"}


class MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["task", "event", "project", "note"]
    id: StrictStr
    to: StrictStr | None = None


def _not_found(kind: str):
    return nest_error(404, f"{LABELS[kind]} not found")


async def _may_move_from(user, row, kind: str) -> Space:
    """The item's current space, if this person may move it out of there."""
    if row.workspaceId is None:
        if row.userId != user.id:
            raise _not_found(kind)
        return Space(user=user)
    role = await member_role(row.workspaceId, user.id)
    added_it = (row.createdById or row.userId) == user.id
    if role is None or not (added_it or role in ADMIN_ROLES):
        raise _not_found(kind)
    return Space(user=user, workspace_id=row.workspaceId, role=role)


async def _collect(kind: str, row) -> dict[str, list[str]]:
    """Every id that moves with this item, by model."""
    same = {"workspaceId": row.workspaceId}
    ids: dict[str, list[str]] = {"project": [], "task": [], "event": [], "note": []}
    if kind == "project":
        ids["project"] = [row.id]
        tops = await db.task.find_many(where={**same, "projectId": row.id})
        ids["task"] = [t.id for t in tops]
        ids["event"] = [e.id for e in await db.event.find_many(where={**same, "projectId": row.id})]
        ids["note"] = [n.id for n in await db.note.find_many(where={**same, "projectId": row.id})]
    else:
        ids[kind] = [row.id]
    if ids["task"]:
        subs = await db.task.find_many(where={**same, "parentId": {"in": ids["task"]}})
        ids["task"] = list(dict.fromkeys(ids["task"] + [s.id for s in subs]))
    return ids


async def _ok_link(model: str, target_id: str | None, moving: set[str], dest: Space) -> bool:
    if target_id is None or target_id in moving:
        return True
    return await getattr(db, model).count(where={"id": target_id, **dest.where}) > 0


@router.post("")
async def move(body: MoveBody, user=Depends(plus_user)):
    kind = body.type
    row = await getattr(db, kind).find_unique(where={"id": body.id})
    if row is None:
        raise _not_found(kind)
    await _may_move_from(user, row, kind)
    dest = await space_for(user, body.to)
    zero = {"projects": 0, "tasks": 0, "events": 0, "notes": 0, "attachments": 0}
    if dest.workspace_id == row.workspaceId and (dest.workspace_id or row.userId == user.id):
        return {"moved": zero}

    ids = await _collect(kind, row)
    members = None
    if not dest.personal:
        members = {m.userId for m in await db.workspacemember.find_many(where={"workspaceId": dest.workspace_id})}
    common = {"workspaceId": dest.workspace_id}
    if dest.personal:
        common.update(userId=user.id, createdById=user.id)

    moving_projects, moving_tasks = set(ids["project"]), set(ids["task"])
    attachment_ids: list[str] = []
    async with db.tx(timeout=timedelta(seconds=60)) as tx:
        for model, model_ids in ids.items():
            if not model_ids:
                continue
            data = dict(common)
            if not dest.personal:
                data.update(_LINKS_CLEARED[model])
            await getattr(tx, model).update_many(where={"id": {"in": model_ids}}, data=data)
            if not dest.personal and model in _HAS_TAGS:
                for item_id in model_ids:
                    await getattr(tx, model).update(where={"id": item_id}, data={"tags": {"set": []}})
        # who it's assigned to: nobody in Personal; only members in a workspace
        if moving_tasks:
            if dest.personal:
                await tx.task.update_many(where={"id": {"in": list(moving_tasks)}}, data={"assigneeId": None})
            else:
                await tx.task.update_many(
                    where={"id": {"in": list(moving_tasks)}, "assigneeId": {"not_in": list(members)}},
                    data={"assigneeId": None},
                )
        # files go with their item
        for model, model_ids in ids.items():
            if model_ids:
                files = await tx.attachment.find_many(where={"entityType": _ENTITY[model], "entityId": {"in": model_ids}})
                attachment_ids += [f.id for f in files]
        if attachment_ids:
            await tx.attachment.update_many(where={"id": {"in": attachment_ids}}, data=dict(common))

    # links that would now point into another space are dropped
    for t in await db.task.find_many(where={"id": {"in": list(moving_tasks)}}):
        fix = {}
        if not await _ok_link("project", t.projectId, moving_projects, dest):
            fix["projectId"] = None
        if t.parentId and t.parentId not in moving_tasks:
            fix["parentId"] = None
        if fix:
            await db.task.update(where={"id": t.id}, data=fix)
    for e in await db.event.find_many(where={"id": {"in": ids["event"]}}):
        fix = {}
        if not await _ok_link("project", e.projectId, moving_projects, dest):
            fix["projectId"] = None
        if not await _ok_link("task", e.taskId, moving_tasks, dest):
            fix["taskId"] = None
        if fix:
            await db.event.update(where={"id": e.id}, data=fix)
    for n in await db.note.find_many(where={"id": {"in": ids["note"]}}):
        if not await _ok_link("project", n.projectId, moving_projects, dest):
            await db.note.update(where={"id": n.id}, data={"projectId": None})
    # what stayed behind and pointed at what left
    if moving_tasks:
        await db.event.update_many(where={"taskId": {"in": list(moving_tasks)}, "id": {"not_in": ids["event"]}}, data={"taskId": None})
        await db.task.update_many(where={"parentId": {"in": list(moving_tasks)}, "id": {"not_in": list(moving_tasks)}}, data={"parentId": None})
    return {
        "moved": {
            "projects": len(ids["project"]),
            "tasks": len(ids["task"]),
            "events": len(ids["event"]),
            "notes": len(ids["note"]),
            "attachments": len(attachment_ids),
        }
    }
