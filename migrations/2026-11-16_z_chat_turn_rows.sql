-- Correlation only; question ownership and signed answer authority are unchanged.
ALTER TABLE chat_questions ADD COLUMN IF NOT EXISTS request_id TEXT;
