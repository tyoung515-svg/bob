import json
from datetime import datetime, timezone
from typing import Any


class InMemoryPostgresPool:
    def __init__(self) -> None:
        self.conversations: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.ideas: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        self._conversation_seq = 0
        self._message_seq = 0
        self._idea_seq = 0
        self._approval_seq = 0
        self._project_seq = 0

    def add_project(
        self,
        *,
        name: str,
        user_id: str = "admin",
        description: str | None = None,
        instructions: str | None = None,
        default_face_id: str | None = None,
        default_backend: str | None = None,
        is_archived: bool = False,
        updated_at: datetime | None = None,
        created_at: datetime | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        self._project_seq += 1
        record_id = project_id or f"project-{self._project_seq}"
        now = datetime.now(timezone.utc)
        record = {
            "id": record_id,
            "user_id": user_id,
            "name": name,
            "description": description,
            "instructions": instructions,
            "default_face_id": default_face_id,
            "default_backend": default_backend,
            "is_archived": is_archived,
            "created_at": created_at or now,
            "updated_at": updated_at or now,
        }
        self.projects[record_id] = record
        return record

    def add_approval(
        self,
        *,
        conversation_id: str | None = None,
        user_id: str = "admin",
        action_type: str = "task_approval",
        details: dict | None = None,
        status: str = "pending",
        approved_by: str | None = None,
        decided_at: datetime | None = None,
        created_at: datetime | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        self._approval_seq += 1
        record_id = approval_id or f"approval-{self._approval_seq}"
        now = datetime.now(timezone.utc)
        record = {
            "id": record_id,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "action_type": action_type,
            "details": details or {},
            "status": status,
            "approved_by": approved_by,
            "decided_at": decided_at,
            "created_at": created_at or now,
        }
        self.approvals[record_id] = record
        return record

    def add_idea(
        self,
        *,
        body: str,
        user_id: str = "admin",
        tags: list | None = None,
        state: str = "raw",
        promoted_to: str | None = None,
        updated_at: datetime | None = None,
        created_at: datetime | None = None,
        idea_id: str | None = None,
    ) -> dict[str, Any]:
        self._idea_seq += 1
        record_id = idea_id or f"idea-{self._idea_seq}"
        now = datetime.now(timezone.utc)
        record = {
            "id": record_id,
            "user_id": user_id,
            "body": body,
            "tags": list(tags) if tags is not None else [],
            "state": state,
            "promoted_to": promoted_to,
            "created_at": created_at or now,
            "updated_at": updated_at or now,
        }
        self.ideas[record_id] = record
        return record

    def add_conversation(
        self,
        *,
        title: str,
        user_id: str = "admin",
        face_id: str | None = None,
        model_preference: str | None = None,
        backend_preference: str | None = None,
        project_id: str | None = None,
        updated_at: datetime | None = None,
        is_archived: bool = False,
        conv_id: str | None = None,
    ) -> dict[str, Any]:
        self._conversation_seq += 1
        conversation_id = conv_id or f"conv-{self._conversation_seq}"
        record = {
            "id": conversation_id,
            "user_id": user_id,
            "title": title,
            "face_id": face_id,
            "model_preference": model_preference,
            "backend_preference": backend_preference,
            "project_id": project_id,
            "updated_at": updated_at or datetime.now(timezone.utc),
            "is_archived": is_archived,
        }
        self.conversations[conversation_id] = record
        return record

    def add_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        self._message_seq += 1
        record = {
            "id": message_id or f"msg-{self._message_seq}",
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "created_at": created_at or datetime.now(timezone.utc),
            "metadata": metadata or {},
        }
        self.messages.append(record)
        return record

    async def fetch(self, query: str, *args):
        normalized = " ".join(query.lower().split())

        # Projects: list non-archived for user, newest-updated first, with count
        if "from projects p" in normalized:
            limit, offset, user_id = args
            projects = [
                project
                for project in self.projects.values()
                if not project["is_archived"] and project.get("user_id", "admin") == user_id
            ]
            projects.sort(key=lambda item: item["updated_at"], reverse=True)
            rows = []
            for project in projects[offset : offset + limit]:
                conversation_count = sum(
                    1
                    for conversation in self.conversations.values()
                    if conversation.get("project_id") == project["id"]
                    and not conversation["is_archived"]
                )
                rows.append({
                    "id": project["id"],
                    "name": project["name"],
                    "description": project["description"],
                    "default_face_id": project["default_face_id"],
                    "default_backend": project["default_backend"],
                    "updated_at": project["updated_at"],
                    "conversation_count": conversation_count,
                })
            return rows

        # Approvals digest: gate-cleared slice (approved_by = 'gate')
        if "from approvals" in normalized and "approved_by = 'gate'" in normalized:
            user_id, limit = args
            rows = [
                a for a in self.approvals.values()
                if a["user_id"] == user_id and a.get("approved_by") == "gate"
            ]
            rows.sort(key=lambda item: item["created_at"], reverse=True)
            return rows[:limit]

        # Approvals digest: flagged-pending slice (pending worker_scope_review)
        if (
            "from approvals" in normalized
            and "status = 'pending'" in normalized
            and "action_type = 'worker_scope_review'" in normalized
        ):
            user_id, limit = args
            rows = [
                a for a in self.approvals.values()
                if a["user_id"] == user_id
                and a["status"] == "pending"
                and a["action_type"] == "worker_scope_review"
            ]
            rows.sort(key=lambda item: item["created_at"], reverse=True)
            return rows[:limit]

        # Approvals: list with status filter
        if "from approvals" in normalized and "where user_id = $1 and status = $2" in normalized:
            user_id, status, limit, offset = args
            rows = [
                a for a in self.approvals.values()
                if a["user_id"] == user_id and a["status"] == status
            ]
            rows.sort(key=lambda item: item["created_at"], reverse=True)
            return rows[offset : offset + limit]

        # Approvals: list all for user
        if "from approvals" in normalized and "where user_id = $1" in normalized and "status" not in normalized.split("where")[1].split("order")[0]:
            user_id, limit, offset = args
            rows = [a for a in self.approvals.values() if a["user_id"] == user_id]
            rows.sort(key=lambda item: item["created_at"], reverse=True)
            return rows[offset : offset + limit]

        # Ideas: list with optional state filter
        if "from ideas" in normalized and "where user_id = $1 and state = $2" in normalized:
            user_id, state, limit, offset = args
            rows = [
                idea for idea in self.ideas.values()
                if idea["user_id"] == user_id and idea["state"] == state
            ]
            rows.sort(key=lambda item: item["updated_at"], reverse=True)
            return rows[offset : offset + limit]

        if "from ideas" in normalized and "state != 'archived'" in normalized and "limit $2 offset $3" in normalized:
            user_id, limit, offset = args
            rows = [
                idea for idea in self.ideas.values()
                if idea["user_id"] == user_id and idea["state"] != "archived"
            ]
            rows.sort(key=lambda item: item["updated_at"], reverse=True)
            return rows[offset : offset + limit]

        if "from ideas" in normalized and "state != 'archived'" in normalized:
            # by-state: no LIMIT, single user_id arg
            (user_id,) = args
            rows = [
                idea for idea in self.ideas.values()
                if idea["user_id"] == user_id and idea["state"] != "archived"
            ]
            rows.sort(key=lambda item: item["updated_at"], reverse=True)
            return rows

        if "from conversations c" in normalized:
            # New schema: limit, offset, user_id
            if len(args) == 3:
                limit, offset, user_id = args
            else:
                limit, offset = args
                user_id = "admin"
            conversations = [
                conversation
                for conversation in self.conversations.values()
                if not conversation["is_archived"]
                and conversation.get("user_id", "admin") == user_id
            ]
            conversations.sort(key=lambda item: item["updated_at"], reverse=True)
            rows = []
            for conversation in conversations[offset : offset + limit]:
                preview = None
                related = [
                    message
                    for message in self.messages
                    if message["conversation_id"] == conversation["id"]
                ]
                related.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
                if related:
                    preview = related[0]["content"][:120]
                rows.append({**conversation, "last_message_preview": preview})
            return rows

        if "from messages" in normalized and "limit $2" in normalized:
            conversation_id, limit = args
            rows = [
                message
                for message in self.messages
                if message["conversation_id"] == conversation_id
            ]
            rows.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
            return rows[:limit]

        if "from messages" in normalized and "limit $4" in normalized:
            conversation_id, cursor_created_at, before_id, limit = args
            rows = [
                message
                for message in self.messages
                if message["conversation_id"] == conversation_id
            ]
            rows.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
            if cursor_created_at is not None:
                rows = [
                    message
                    for message in rows
                    if message["created_at"] < cursor_created_at
                    or (
                        message["created_at"] == cursor_created_at
                        and message["id"] < before_id
                    )
                ]
            return rows[:limit]

        raise AssertionError(f"Unhandled fetch query: {query}")

    async def fetchrow(self, query: str, *args):
        normalized = " ".join(query.lower().split())

        # Approvals: SELECT by id
        if "from approvals" in normalized and "where id = $1 and user_id = $2" in normalized:
            approval_id, user_id = args
            approval_id_str = str(approval_id)
            a = self.approvals.get(approval_id_str)
            if a is None or a["user_id"] != user_id:
                return None
            return a

        # Approvals: UPDATE decide
        if normalized.startswith("update approvals set status = $2"):
            approval_id, new_status, user_id = args
            approval_id_str = str(approval_id)
            a = self.approvals.get(approval_id_str)
            if a is None or a["user_id"] != user_id or a["status"] != "pending":
                return None
            a["status"] = new_status
            a["decided_at"] = datetime.now(timezone.utc)
            return a

        # Ideas: INSERT
        if "insert into ideas" in normalized:
            user_id, body, tags = args
            return self.add_idea(user_id=user_id, body=body, tags=tags or [])

        # Ideas: SELECT by id
        if "from ideas" in normalized and "where id = $1 and user_id = $2" in normalized:
            idea_id, user_id = args
            idea = self.ideas.get(idea_id)
            if idea is None or idea["user_id"] != user_id:
                return None
            return idea

        # Ideas: PATCH (UPDATE with COALESCE)
        if normalized.startswith("update ideas set body = coalesce"):
            idea_id, body, tags, state, promoted_to, user_id = args
            idea = self.ideas.get(idea_id)
            if idea is None or idea["user_id"] != user_id:
                return None
            if body is not None:
                idea["body"] = body
            if tags is not None:
                idea["tags"] = list(tags)
            if state is not None:
                idea["state"] = state
            if promoted_to is not None:
                idea["promoted_to"] = promoted_to
            idea["updated_at"] = datetime.now(timezone.utc)
            return idea

        # Projects: INSERT
        if "insert into projects" in normalized:
            user_id, name, description, instructions, default_face_id, default_backend = args
            return self.add_project(
                user_id=user_id,
                name=name,
                description=description,
                instructions=instructions,
                default_face_id=default_face_id,
                default_backend=default_backend,
            )

        # Projects: SELECT full by id (get_project)
        if "from projects" in normalized and "is_archived, created_at, updated_at" in normalized:
            project_id, user_id = args
            project = self.projects.get(project_id)
            if project is None or project.get("user_id", "admin") != user_id:
                return None
            return project

        # Projects: inheritance lookup (create_conversation)
        if "select default_face_id, default_backend from projects" in normalized:
            project_id, user_id = args
            project = self.projects.get(project_id)
            if project is None or project["is_archived"] or project.get("user_id", "admin") != user_id:
                return None
            return project

        # Projects: ownership check (assign_conversation_project)
        if "select id from projects" in normalized:
            project_id, user_id = args
            project = self.projects.get(project_id)
            if project is None or project["is_archived"] or project.get("user_id", "admin") != user_id:
                return None
            return project

        # Projects: UPDATE (dynamic set clause, RETURNING full row)
        if normalized.startswith("update projects set"):
            project_id = args[0]
            user_id = args[-1]
            project = self.projects.get(project_id)
            if project is None or project["is_archived"] or project.get("user_id", "admin") != user_id:
                return None
            # Re-derive which columns were set from the SET clause + positional args.
            updatable = ("name", "description", "instructions", "default_face_id", "default_backend")
            arg_index = 1  # $1 is project_id, set values start at $2
            for key in updatable:
                if f"{key} = $" in normalized:
                    project[key] = args[arg_index]
                    arg_index += 1
            project["updated_at"] = datetime.now(timezone.utc)
            return project

        # INSERT with user_id (new schema)
        if "insert into conversations" in normalized and "user_id" in normalized:
            user_id, title, face_id, model_preference, backend_preference, project_id = args
            return self.add_conversation(
                user_id=user_id,
                title=title,
                face_id=face_id,
                model_preference=model_preference,
                backend_preference=backend_preference,
                project_id=project_id,
            )

        # INSERT without user_id (fallback)
        if "insert into conversations" in normalized:
            title, face_id, model_preference = args
            return self.add_conversation(
                title=title,
                face_id=face_id,
                model_preference=model_preference,
            )

        # SELECT by id with user_id filter
        if "select id, title, face_id, model_preference, backend_preference, project_id, updated_at, is_archived, user_id from conversations" in normalized:
            conversation_id, user_id = args
            conv = self.conversations.get(conversation_id)
            if conv is None or conv["is_archived"] or conv.get("user_id", "admin") != user_id:
                return None
            return conv

        # SELECT by id without user_id (fallback)
        if normalized.startswith("select id, title, face_id, model_preference, updated_at, is_archived from conversations"):
            conversation_id = args[0]
            return self.conversations.get(conversation_id)

        # chat.py: conversation pins + project instructions (LEFT JOIN projects)
        if "from conversations c left join projects p" in normalized:
            (conversation_id,) = args
            conv = self.conversations.get(conversation_id)
            if conv is None:
                return None
            project = self.projects.get(conv.get("project_id")) if conv.get("project_id") else None
            return {
                "face_id": conv.get("face_id"),
                "model_preference": conv.get("model_preference"),
                "backend_preference": conv.get("backend_preference"),
                "project_name": project["name"] if project else None,
                "project_description": project.get("description") if project else None,
                "project_instructions": project["instructions"] if project else None,
            }

        # SELECT id from conversations with is_archived filter (assign ownership check)
        if "select id from conversations" in normalized and "is_archived = false" in normalized:
            conversation_id, user_id = args
            conv = self.conversations.get(conversation_id)
            if conv is None or conv["is_archived"] or conv.get("user_id", "admin") != user_id:
                return None
            return conv

        # SELECT id from conversations (ownership check in list_messages)
        if "select id from conversations" in normalized:
            conversation_id, user_id = args
            conv = self.conversations.get(conversation_id)
            if conv is None or conv.get("user_id", "admin") != user_id:
                return None
            return conv

        # UPDATE conversations set project_id (assign/unassign)
        if normalized.startswith("update conversations set project_id = $2"):
            conversation_id, project_id, user_id = args
            conv = self.conversations.get(conversation_id)
            if conv is None or conv["is_archived"] or conv.get("user_id", "admin") != user_id:
                return None
            conv["project_id"] = project_id
            conv["updated_at"] = datetime.now(timezone.utc)
            return conv

        # UPDATE title with user_id filter
        if normalized.startswith("update conversations set title = $2") and "user_id = $3" in normalized:
            conversation_id, title, user_id = args
            conversation = self.conversations.get(conversation_id)
            if conversation is None or conversation["is_archived"] or conversation.get("user_id", "admin") != user_id:
                return None
            conversation["title"] = title
            conversation["updated_at"] = datetime.now(timezone.utc)
            return conversation

        # UPDATE title without user_id (fallback)
        if normalized.startswith("update conversations set title = $2"):
            conversation_id, title = args
            conversation = self.conversations.get(conversation_id)
            if conversation is None or conversation["is_archived"]:
                return None
            conversation["title"] = title
            conversation["updated_at"] = datetime.now(timezone.utc)
            return conversation

        # SELECT message cursor
        if normalized.startswith("select id, created_at from messages"):
            conversation_id, message_id = args
            for message in self.messages:
                if message["conversation_id"] == conversation_id and message["id"] == message_id:
                    return {"id": message["id"], "created_at": message["created_at"]}
            return None

        # INSERT message
        if "insert into messages" in normalized:
            conversation_id, role, content, metadata = args
            return self.add_message(
                conversation_id=conversation_id,
                role=role,
                content=content,
                metadata=metadata,
            )

        raise AssertionError(f"Unhandled fetchrow query: {query}")

    async def execute(self, query: str, *args):
        normalized = " ".join(query.lower().split())

        # Projects: archive (soft delete)
        if normalized.startswith("update projects set is_archived=true"):
            project_id, user_id = args
            project = self.projects.get(project_id)
            if project is None or project["is_archived"] or project.get("user_id", "admin") != user_id:
                return "UPDATE 0"
            project["is_archived"] = True
            project["updated_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"

        # Conversations: unassign all members of an archived project.
        # Literal `project_id=NULL` distinguishes this from the parameterized
        # assignment UPDATE (`project_id=$2`, handled in fetchrow via RETURNING).
        if normalized.startswith("update conversations set project_id=null"):
            project_id, user_id = args
            count = 0
            for conversation in self.conversations.values():
                if (
                    conversation.get("project_id") == project_id
                    and conversation.get("user_id", "admin") == user_id
                ):
                    conversation["project_id"] = None
                    conversation["updated_at"] = datetime.now(timezone.utc)
                    count += 1
            return f"UPDATE {count}"

        # Conversations: switch_face persistence (set face_id or clear to NULL).
        if normalized.startswith("update conversations set face_id ="):
            if "face_id = null" in normalized:
                conversation_id, user_id = args
                new_face = None
            else:
                conversation_id, new_face, user_id = args
            conv = self.conversations.get(conversation_id)
            if conv is None or conv.get("user_id", "admin") != user_id:
                return "UPDATE 0"
            conv["face_id"] = new_face
            conv["updated_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"

        # Conversations: switch_model persistence (set backend/model or clear to NULL).
        if normalized.startswith("update conversations set backend_preference ="):
            if "backend_preference = null" in normalized:
                conversation_id, user_id = args
                new_backend, new_model = None, None
            else:
                conversation_id, new_backend, new_model, user_id = args
            conv = self.conversations.get(conversation_id)
            if conv is None or conv.get("user_id", "admin") != user_id:
                return "UPDATE 0"
            conv["backend_preference"] = new_backend
            conv["model_preference"] = new_model
            conv["updated_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"

        # Approvals: INSERT (used by chat.py _persist_approval)
        if normalized.startswith("insert into approvals"):
            approval_id, conversation_id, user_id, action_type, details = args
            approval_id_str = str(approval_id)
            if approval_id_str in self.approvals:
                return "INSERT 0 0"  # ON CONFLICT DO NOTHING
            try:
                parsed_details = json.loads(details) if isinstance(details, str) else details
            except (ValueError, json.JSONDecodeError):
                parsed_details = {}
            self.add_approval(
                approval_id=approval_id_str,
                conversation_id=str(conversation_id) if conversation_id else None,
                user_id=user_id,
                action_type=action_type,
                details=parsed_details,
            )
            return "INSERT 0 1"

        # Ideas: archive (soft delete)
        if normalized.startswith("update ideas set state = 'archived'"):
            idea_id, user_id = args
            idea = self.ideas.get(idea_id)
            if idea is None or idea["user_id"] != user_id or idea["state"] == "archived":
                return "UPDATE 0"
            idea["state"] = "archived"
            idea["updated_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"

        # UPDATE archive with user_id filter
        if normalized.startswith("update conversations set is_archived = true") and "user_id = $2" in normalized:
            conversation_id, user_id = args
            conversation = self.conversations.get(conversation_id)
            if conversation is None or conversation["is_archived"] or conversation.get("user_id", "admin") != user_id:
                return "UPDATE 0"
            conversation["is_archived"] = True
            conversation["updated_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"

        # UPDATE archive without user_id (fallback)
        if normalized.startswith("update conversations set is_archived = true"):
            conversation_id = args[0]
            conversation = self.conversations.get(conversation_id)
            if conversation is None or conversation["is_archived"]:
                return "UPDATE 0"
            conversation["is_archived"] = True
            conversation["updated_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"

        raise AssertionError(f"Unhandled execute query: {query}")

    def acquire(self):
        """Return an async-context-manager connection sharing this pool's state.

        Mirrors asyncpg's ``pool.acquire()`` so router code that opens an
        explicit connection + transaction (e.g. archive_project) works against
        the in-memory pool. The fake transaction has no real rollback — tests
        exercise the happy/404 paths, not mid-transaction failure recovery.
        """
        return _FakeAcquire(self)


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self, pool: "InMemoryPostgresPool") -> None:
        self._pool = pool

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, query: str, *args):
        return await self._pool.execute(query, *args)

    async def fetchrow(self, query: str, *args):
        return await self._pool.fetchrow(query, *args)

    async def fetch(self, query: str, *args):
        return await self._pool.fetch(query, *args)


class _FakeAcquire:
    def __init__(self, pool: "InMemoryPostgresPool") -> None:
        self._pool = pool

    async def __aenter__(self):
        return _FakeConnection(self._pool)

    async def __aexit__(self, exc_type, exc, tb):
        return False
