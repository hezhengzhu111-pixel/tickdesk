CREATE TABLE chat_jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    peer_fence INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','running','completed','quarantined')),
    accepted_at INTEGER NOT NULL,
    deadline INTEGER NOT NULL,
    text TEXT NOT NULL,
    evidence TEXT NOT NULL,
    raw_output TEXT,
    model TEXT,
    prompt_version TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    reply TEXT,
    UNIQUE(pair_id,epoch,request_id)
);
CREATE INDEX pending_chats ON chat_jobs(state,accepted_at);
CREATE TABLE provider_health (
    name TEXT PRIMARY KEY CHECK(name IN ('deepseek','vision')),
    status TEXT NOT NULL CHECK(status IN ('unknown','online','degraded','offline')),
    checked_at INTEGER NOT NULL,
    error_code TEXT
);
