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
    CREATE INDEX IF NOT EXISTS threads_user_id_created_at_idx
    ON threads ("userId", "createdAt" DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS threads_user_identifier_idx
    ON threads ("userIdentifier");
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
    CREATE INDEX IF NOT EXISTS steps_thread_id_created_at_idx
    ON steps ("threadId", "createdAt" ASC);
    """,
    """
    CREATE INDEX IF NOT EXISTS steps_parent_id_idx
    ON steps ("parentId");
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
    CREATE INDEX IF NOT EXISTS elements_thread_id_idx
    ON elements ("threadId");
    """,
    """
    CREATE INDEX IF NOT EXISTS elements_for_id_idx
    ON elements ("forId");
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
    CREATE INDEX IF NOT EXISTS feedbacks_for_id_idx
    ON feedbacks ("forId");
    """,
    """
    CREATE INDEX IF NOT EXISTS feedbacks_thread_id_idx
    ON feedbacks ("threadId");
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
        policy_version BIGINT NOT NULL DEFAULT 0,
        window_start TIMESTAMPTZ,
        window_end TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    ALTER TABLE quota_user_summary
    ADD COLUMN IF NOT EXISTS policy_version BIGINT NOT NULL DEFAULT 0;
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_accounts (
        username TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS managed_users (
        identifier TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deleted_at TIMESTAMPTZ,
        last_login_at TIMESTAMPTZ
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS managed_users_is_deleted_idx
    ON managed_users (is_deleted);
    """,
    """
    CREATE TABLE IF NOT EXISTS quota_policies (
        id TEXT PRIMARY KEY,
        scope_type TEXT NOT NULL,
        scope_id TEXT,
        window_start TIMESTAMPTZ,
        window_end TIMESTAMPTZ,
        total_token_limit INT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        policy_version BIGINT NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    ALTER TABLE quota_policies
    ADD COLUMN IF NOT EXISTS policy_version BIGINT NOT NULL DEFAULT 1;
    """,
    """
    CREATE INDEX IF NOT EXISTS quota_policies_scope_idx
    ON quota_policies (scope_type, scope_id, is_active);
    """,
    """
    CREATE TABLE IF NOT EXISTS team_token_auth_config (
        config_key TEXT PRIMARY KEY,
        public_key TEXT NOT NULL,
        config_version BIGINT NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    ALTER TABLE team_token_auth_config
    ADD COLUMN IF NOT EXISTS config_version BIGINT NOT NULL DEFAULT 1;
    """,
    """
    CREATE TABLE IF NOT EXISTS monitor_notification_channels (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        channel_type TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        config_public JSONB NOT NULL DEFAULT '{}'::jsonb,
        config_secret_ciphertext TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_test_at TIMESTAMPTZ
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS monitor_notification_channels_active_idx
    ON monitor_notification_channels (is_active, channel_type, created_at DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS monitor_notification_events (
        id TEXT PRIMARY KEY,
        monitor_id TEXT NOT NULL,
        monitor_name TEXT NOT NULL,
        event_type TEXT NOT NULL,
        status TEXT NOT NULL,
        message TEXT NOT NULL,
        delivery_results JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS monitor_notification_events_monitor_created_idx
    ON monitor_notification_events (monitor_id, created_at DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS user_import_jobs (
        id TEXT PRIMARY KEY,
        preview_only BOOLEAN NOT NULL DEFAULT TRUE,
        status TEXT NOT NULL,
        total_rows INT NOT NULL DEFAULT 0,
        success_rows INT NOT NULL DEFAULT 0,
        failure_rows INT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS user_import_job_rows (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        row_number INT NOT NULL,
        identifier TEXT,
        action TEXT,
        status TEXT NOT NULL,
        error_message TEXT,
        row_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS user_import_job_rows_job_id_idx
    ON user_import_job_rows (job_id, row_number);
    """,
    """
    CREATE TABLE IF NOT EXISTS caps_submissions (
        id TEXT PRIMARY KEY,
        user_identifier TEXT NOT NULL,
        game_id INT NOT NULL,
        team_id INT NOT NULL,
        player_name TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        stored_filename TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        file_extension TEXT NOT NULL,
        content_type TEXT,
        file_size_bytes BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS caps_submissions_team_id_created_at_idx
    ON caps_submissions (team_id, created_at DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS caps_submissions_user_identifier_idx
    ON caps_submissions (user_identifier);
    """,
]
