CHAINLIT_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        "id" UUID PRIMARY KEY,
        "identifier" TEXT NOT NULL UNIQUE,
        "metadata" JSONB NOT NULL DEFAULT '{}'::jsonb,
        "createdAt" TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS threads (
        "id" UUID PRIMARY KEY,
        "createdAt" TEXT,
        "name" TEXT,
        "userId" UUID,
        "userIdentifier" TEXT,
        "tags" TEXT[],
        "metadata" JSONB
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS steps (
        "id" UUID PRIMARY KEY,
        "name" TEXT NOT NULL,
        "type" TEXT NOT NULL,
        "threadId" UUID NOT NULL,
        "parentId" UUID,
        "disableFeedback" BOOLEAN,
        "streaming" BOOLEAN NOT NULL DEFAULT FALSE,
        "waitForAnswer" BOOLEAN,
        "isError" BOOLEAN,
        "metadata" JSONB,
        "tags" TEXT[],
        "input" TEXT,
        "output" TEXT,
        "createdAt" TEXT,
        "start" TEXT,
        "end" TEXT,
        "generation" JSONB,
        "defaultOpen" BOOLEAN,
        "autoCollapse" BOOLEAN,
        "showInput" TEXT,
        "language" TEXT,
        "indent" INT
    );
    """,
    """
    ALTER TABLE steps ADD COLUMN IF NOT EXISTS "defaultOpen" BOOLEAN;
    """,
    """
    ALTER TABLE steps ADD COLUMN IF NOT EXISTS "autoCollapse" BOOLEAN;
    """,
    """
    CREATE TABLE IF NOT EXISTS elements (
        "id" UUID PRIMARY KEY,
        "threadId" UUID,
        "type" TEXT,
        "url" TEXT,
        "chainlitKey" TEXT,
        "name" TEXT NOT NULL,
        "display" TEXT,
        "objectKey" TEXT,
        "size" TEXT,
        "page" INT,
        "language" TEXT,
        "forId" UUID,
        "mime" TEXT,
        "props" JSONB,
        "autoPlay" BOOLEAN,
        "playerConfig" JSONB
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS feedbacks (
        "id" UUID PRIMARY KEY,
        "forId" UUID NOT NULL,
        "threadId" UUID,
        "value" INT NOT NULL,
        "comment" TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS quota_usage_ledger (
        request_id TEXT PRIMARY KEY,
        provider_request_id TEXT,
        thread_id TEXT NOT NULL,
        user_identifier TEXT NOT NULL,
        step_id TEXT NOT NULL,
        prompt_tokens INT NOT NULL DEFAULT 0,
        completion_tokens INT NOT NULL DEFAULT 0,
        total_tokens INT NOT NULL DEFAULT 0,
        reserved_tokens INT NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        usage_source TEXT NOT NULL DEFAULT 'estimated',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        settled_at TIMESTAMPTZ
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS quota_usage_ledger_thread_id_idx
    ON quota_usage_ledger (thread_id);
    """,
    """
    CREATE INDEX IF NOT EXISTS quota_usage_ledger_user_identifier_idx
    ON quota_usage_ledger (user_identifier);
    """,
    """
    CREATE INDEX IF NOT EXISTS quota_usage_ledger_status_idx
    ON quota_usage_ledger (status);
    """,
    """
    CREATE TABLE IF NOT EXISTS quota_thread_summary (
        thread_id TEXT PRIMARY KEY,
        used_tokens INT NOT NULL DEFAULT 0,
        reserved_tokens INT NOT NULL DEFAULT 0,
        limit_tokens INT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS quota_user_summary (
        user_identifier TEXT PRIMARY KEY,
        used_tokens INT NOT NULL DEFAULT 0,
        reserved_tokens INT NOT NULL DEFAULT 0,
        limit_tokens INT NOT NULL,
        window_start TIMESTAMPTZ,
        window_end TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
]
