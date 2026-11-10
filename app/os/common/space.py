"""The space a request works in: the person's Personal space, or one of their workspaces
(spec 2026-11-09-workspaces-design).

The dashboard sends `X-Vournal-Space: <workspaceId>` (nothing for Personal). Every read and
write of the five shared models (Task, Event, Project, Note, Attachment) in a space-aware
section goes through `Space`:

- Personal:  `userId = me AND workspaceId IS NULL`; new items are mine.
- Workspace: `workspaceId = w`; members see, create and edit everything, delete what they
  added (admins and the owner delete anything). `userId` stays the creator until their
  account goes (then the workspace owner); `createdById` keeps who added it.

A workspace the person isn't in answers 404, as does an item outside the space: existence
is never leaked.
"""

from dataclasses import dataclass

from fastapi import Depends, Request

from app.db import db
from app.os.common.deps import plus_user
from app.os.common.http import nest_error
from app.os.common.ownership import LABELS

HEADER = "x-vournal-space"
WORKSPACE_MODELS = ("task", "event", "project", "note", "attachment")
ADMIN_ROLES = ("OWNER", "ADMIN")
# Personal links: refused inside a workspace, cleared when an item moves into one.
PERSONAL_LINKS = ("categoryId", "tagIds", "goalId", "courseId", "tripId", "personId")
PERSONAL_LINKS_REFUSED = "Personal links aren't available in a workspace"


@dataclass(frozen=True)
class Space:
    user: object
    workspace_id: str | None = None
    role: str | None = None

    @property
    def personal(self) -> bool:
        return self.workspace_id is None

    @property
    def is_admin(self) -> bool:
        return self.personal or self.role in ADMIN_ROLES

    @property
    def where(self) -> dict:
        """The list filter for the five shared models."""
        if self.personal:
            return {"userId": self.user.id, "workspaceId": None}
        return {"workspaceId": self.workspace_id}

    def create_fields(self) -> dict:
        return {"userId": self.user.id, "createdById": self.user.id, "workspaceId": self.workspace_id}

    def can_delete(self, row) -> bool:
        if self.personal or self.role in ADMIN_ROLES:
            return True
        return (getattr(row, "createdById", None) or getattr(row, "userId", None)) == self.user.id


def personal(user) -> Space:
    return Space(user=user)


async def member_role(workspace_id: str, user_id: str) -> str | None:
    member = await db.workspacemember.find_unique(
        where={"workspaceId_userId": {"workspaceId": workspace_id, "userId": user_id}}
    )
    return str(getattr(member.role, "value", member.role)) if member else None


async def space_for(user, workspace_id: str | None) -> Space:
    """The space for this person and workspace id (None or "" = Personal); 404 when they
    aren't a member."""
    if not workspace_id:
        return Space(user=user)
    role = await member_role(workspace_id, user.id)
    if role is None:
        raise nest_error(404, "Workspace not found")
    return Space(user=user, workspace_id=workspace_id, role=role)


async def current_space(request: Request, user=Depends(plus_user)) -> Space:
    """The dependency: Plus (members need it, as households do), then membership."""
    return await space_for(user, (request.headers.get(HEADER) or "").strip() or None)


async def assert_in_space(model: str, id: str | None, space: Space) -> None:
    """For ids the client sends (links, parents): the row must be in this space."""
    if id is None:
        return
    if not isinstance(id, str) or not id or await getattr(db, model).count(where={"id": id, **space.where}) == 0:
        raise nest_error(404, f"{LABELS[model]} not found")


def assert_personal_links_allowed(space: Space, body) -> None:
    """Categories, tags, goals, courses, trips and people are personal."""
    if space.personal:
        return
    for field in PERSONAL_LINKS:
        value = getattr(body, field, None)
        if value not in (None, [], ""):
            raise nest_error(400, PERSONAL_LINKS_REFUSED)
