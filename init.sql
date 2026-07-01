-- BoBClaw — PostgreSQL Schema Initialization
-- Run automatically by docker-compose on first start

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ══════════════════════════════════════════════════════
-- Conversations & Messages
-- ══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL DEFAULT 'admin',
    title TEXT,
    face_id TEXT,
    model_preference TEXT,
    backend_preference TEXT,
    is_archived BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_face ON conversations(face_id);

-- Projects (server-side workspaces): group conversations + carry project-level context
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL DEFAULT 'admin',
    name TEXT NOT NULL,
    description TEXT,
    instructions TEXT,
    default_face_id TEXT,
    default_backend TEXT,
    is_archived BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id, is_archived, updated_at DESC);

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project_id);

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,  -- user, assistant, system, tool
    content TEXT NOT NULL,
    model_used TEXT,
    backend TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd NUMERIC(10,6),
    elapsed_ms INTEGER,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at DESC);

-- ══════════════════════════════════════════════════════
-- Builds (Claude Pipeline)
-- ══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS builds (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued, running, complete, failed, cancelled
    model TEXT,
    face_id TEXT,
    artifacts JSONB,
    error TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd NUMERIC(10,6),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_builds_status ON builds(status);
CREATE INDEX IF NOT EXISTS idx_builds_created ON builds(created_at DESC);

-- ══════════════════════════════════════════════════════
-- Faces / Roles
-- ══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS faces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    avatar TEXT,
    system_prompt TEXT,
    preferred_backend TEXT DEFAULT 'local',
    allowed_tools JSONB DEFAULT '[]'::jsonb,
    escalation_backend TEXT,
    ui_theme TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ══════════════════════════════════════════════════════
-- LangGraph Checkpoints
-- ══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    checkpoint JSONB NOT NULL,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (thread_id, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_thread ON langgraph_checkpoints(thread_id, created_at DESC);

-- ══════════════════════════════════════════════════════
-- OpenCode Instance Health (multi-process shared state)
-- ══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS opencode_instance_health (
    host          TEXT NOT NULL,
    port          INTEGER NOT NULL,
    alive         BOOLEAN NOT NULL DEFAULT TRUE,
    last_probe_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (host, port)
);

CREATE INDEX IF NOT EXISTS idx_opencode_health_alive
    ON opencode_instance_health(alive, last_probe_at DESC);

-- ══════════════════════════════════════════════════════
-- Approvals (Human-in-the-loop)
-- ══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id),
    user_id TEXT NOT NULL DEFAULT 'admin',
    action_type TEXT NOT NULL,  -- email_send, form_submit, purchase, task_approval, etc.
    details JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, approved, rejected, expired
    approved_by TEXT,  -- NULL = human-required; 'gate' = auto-cleared by the scope Gate (GR-P3)
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Idempotent migration for deployments whose volume pre-dates approved_by (GR-P3)
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approved_by TEXT;

-- Idempotent migration for deployments that pre-date user_id column
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'approvals' AND column_name = 'user_id'
    ) THEN
        ALTER TABLE approvals ADD COLUMN user_id TEXT;
        UPDATE approvals a SET user_id = COALESCE(
            (SELECT c.user_id FROM conversations c WHERE c.id = a.conversation_id),
            'admin'
        );
        ALTER TABLE approvals ALTER COLUMN user_id SET NOT NULL;
        ALTER TABLE approvals ALTER COLUMN user_id SET DEFAULT 'admin';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_approvals_user_status
    ON approvals(user_id, status, created_at DESC);

-- ══════════════════════════════════════════════════════
-- Email Metadata Cache
-- ══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS email_cache (
    message_id TEXT PRIMARY KEY,
    folder TEXT,
    sender TEXT,
    subject TEXT,
    preview TEXT,
    is_read BOOLEAN DEFAULT FALSE,
    received_at TIMESTAMPTZ,
    cached_at TIMESTAMPTZ DEFAULT NOW()
);

-- ══════════════════════════════════════════════════════
-- Ideas (ADHD parking lot — capture now, triage later)
-- ══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS ideas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL DEFAULT 'admin',
    body TEXT NOT NULL,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    state TEXT NOT NULL DEFAULT 'raw'
        CHECK (state IN ('raw', 'triaged', 'active', 'parked', 'archived')),
    promoted_to UUID REFERENCES conversations(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ideas_user_state
    ON ideas(user_id, state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ideas_user_updated
    ON ideas(user_id, updated_at DESC);

-- ══════════════════════════════════════════════════════
-- Default Face Configurations
-- ══════════════════════════════════════════════════════

INSERT INTO faces (id, name, avatar, system_prompt, preferred_backend, allowed_tools, escalation_backend, ui_theme) VALUES
('builder-bob', 'Builder Bob', '👷',
 'You are Builder Bob, a hands-on project builder. You break problems into buildable pieces, write code, create files, and test your work. You prefer doing over discussing.',
 'local', '["code", "files", "shell", "docs", "browser"]'::jsonb, 'claude_managed', 'orange'),

('researcher', 'Researcher', '🔬',
 'You are the Researcher. You find, analyze, and synthesize information. You cite sources, compare viewpoints, and produce structured findings.',
 'local', '["search", "docs", "files", "email", "browser"]'::jsonb, 'gemini_deep_research', 'blue'),

('reviewer', 'Reviewer', '🔍',
 'You are the Reviewer. You audit code, documents, and plans for quality, security, and correctness. You provide structured feedback with severity levels.',
 'local', '["code", "files", "search"]'::jsonb, 'claude_api', 'purple'),

('council', 'The Council', '⚖️',
 'You are The Council — a multi-perspective decision-making entity. Consider problems from builder, researcher, reviewer, and user perspectives.',
 'local', '["code", "files", "search", "docs", "email", "browser"]'::jsonb, 'claude_managed', 'gold'),

('assistant', 'General Assistant', '🤖',
 'You are the General Assistant. You handle everyday tasks: email triage, scheduling, web lookups, file management, and casual conversation.',
 'local', '["search", "docs", "files", "email", "browser"]'::jsonb, 'claude_api', 'green')

ON CONFLICT (id) DO NOTHING;
