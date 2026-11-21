CREATE TABLE file_deletions (
    file_id TEXT PRIMARY KEY REFERENCES files(file_id),
    pair_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    client_msg_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    deleting_version INTEGER NOT NULL
);
