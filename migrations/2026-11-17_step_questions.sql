-- Platform step-limit questions share the existing durable question/answer path.
ALTER TABLE chat_questions ADD COLUMN IF NOT EXISTS origin TEXT;
