-- Developer vs QA test cases: add a `phase` dimension to test_cases so both a
-- QA set (Ready for QA) and a developer set (Code Review) can coexist per ticket.
--
-- The Q&A workflow (app/testcase_chat_workflow.py) also self-heals this schema
-- on first write, so applying this migration by hand is optional — but preferred
-- for a controlled deploy. Idempotent: safe to run more than once.

BEGIN;

-- 1. New column; existing rows become the QA set.
ALTER TABLE test_cases
    ADD COLUMN IF NOT EXISTS phase VARCHAR(8) NOT NULL DEFAULT 'qa';

-- 2. Drop the legacy unique key on (jira_ticket_id, tc_index). It would reject a
--    dev row that shares (ticket, index) with a QA row. Handles both the case
--    where it is a named CONSTRAINT and where it is a bare unique INDEX.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'public.test_cases'::regclass
          AND contype = 'u'
          AND (SELECT array_agg(attname ORDER BY attname)
               FROM pg_attribute
               WHERE attrelid = conrelid AND attnum = ANY(conkey))
              = ARRAY['jira_ticket_id', 'tc_index']::name[]
    LOOP
        EXECUTE format('ALTER TABLE test_cases DROP CONSTRAINT %I', r.conname);
    END LOOP;

    FOR r IN
        SELECT i.relname
        FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_class t ON t.oid = x.indrelid
        WHERE t.relname = 'test_cases'
          AND x.indisunique AND NOT x.indisprimary
          AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid)
          AND (SELECT array_agg(a.attname ORDER BY a.attname)
               FROM pg_attribute a
               WHERE a.attrelid = t.oid AND a.attnum = ANY(x.indkey))
              = ARRAY['jira_ticket_id', 'tc_index']::name[]
    LOOP
        EXECUTE format('DROP INDEX IF EXISTS %I', r.relname);
    END LOOP;
END $$;

-- 3. Phase-scoped unique key — the target of the upsert's ON CONFLICT.
CREATE UNIQUE INDEX IF NOT EXISTS test_cases_jira_phase_tc_uidx
    ON test_cases (jira_ticket_id, phase, tc_index);

-- 4. Slack thread -> phase map. A ticket has one QA thread AND one dev thread;
--    `tickets` only holds one thread per ticket, so this per-thread table lets
--    the Q&A workflow (/workflow2) resolve which test-case set a reply is about.
--    Written by the n8n closing flows (WF5 = qa, WF5b = dev); read by
--    app/testcase_chat_workflow.py::resolve_ticket_from_thread.
CREATE TABLE IF NOT EXISTS testcase_threads (
    slack_channel_id TEXT NOT NULL,
    slack_thread_ts  TEXT NOT NULL,
    jira_ticket_id   TEXT NOT NULL,
    phase            VARCHAR(8) NOT NULL DEFAULT 'qa',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (slack_channel_id, slack_thread_ts)
);

COMMIT;
