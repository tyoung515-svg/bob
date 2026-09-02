-- Hermes-shaped interface W1A: persist per-conversation profile/locale pins.
-- `profile` pins a saved profile (HOW layer) to the conversation;
-- `locale` pins the reply locale. NULL = unpinned (session/routing default).
-- Idempotent so re-applying / fresh init.sql runs both converge.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS profile TEXT;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS locale TEXT;
