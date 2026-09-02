-- Hermes-shaped interface W2: bot roster bindings.
-- agent_bindings: a named teammate = face + optional profile + canonical conversation.
-- The binding row is the identity; the conversation title ("Bot: <slug>") is display-only.
-- channel_bindings: external channel (Telegram etc.) -> agent binding; consumed by
-- Phase 3, created now so the schema lands once.
-- Idempotent so re-applying / fresh init.sql runs both converge.
CREATE TABLE IF NOT EXISTS agent_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL DEFAULT 'admin',
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    face_id TEXT NOT NULL,
    profile_name TEXT,
    conversation_id UUID NOT NULL UNIQUE REFERENCES conversations(id),
    ui_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_archived BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_agent_bindings_user ON agent_bindings(user_id, is_archived, created_at);

CREATE TABLE IF NOT EXISTS channel_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL DEFAULT 'admin',
    agent_binding_id UUID NOT NULL REFERENCES agent_bindings(id),
    platform TEXT NOT NULL,
    external_chat_id TEXT NOT NULL,
    external_thread_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, platform, external_chat_id, external_thread_id)
);
