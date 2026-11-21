CREATE TABLE file_uploads (
    upload_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL UNIQUE,
    pair_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 20971520),
    sha256 TEXT NOT NULL,
    total_chunks INTEGER NOT NULL CHECK(total_chunks BETWEEN 1 AND 640),
    expires_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('receiving','complete','expired','cancelled'))
);
CREATE INDEX receiving_file_uploads ON file_uploads(pair_id, epoch, expires_at) WHERE state='receiving';
CREATE TABLE file_upload_chunks (
    upload_id TEXT NOT NULL REFERENCES file_uploads(upload_id),
    chunk_index INTEGER NOT NULL CHECK(chunk_index BETWEEN 0 AND 639),
    chunk_hash TEXT NOT NULL,
    bytes BLOB NOT NULL CHECK(length(bytes) BETWEEN 1 AND 32768),
    PRIMARY KEY (upload_id, chunk_index)
) WITHOUT ROWID;
CREATE TABLE file_chunk_receipts (
    pair_id TEXT NOT NULL,
    peer TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    upload_id TEXT NOT NULL REFERENCES file_uploads(upload_id),
    PRIMARY KEY (pair_id, peer, delivery_id)
) WITHOUT ROWID;
CREATE TABLE files (
    file_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('available','deleting','deleted','missing')),
    created_at INTEGER NOT NULL,
    deleted_at INTEGER
);
CREATE INDEX files_order ON files(pair_id, created_at DESC, file_id);
CREATE TABLE file_versions (
    revision INTEGER PRIMARY KEY,
    file_id TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    deleted_at INTEGER
);
CREATE INDEX file_version_history ON file_versions(file_id, revision);
CREATE INDEX file_version_order ON file_versions(pair_id, created_at DESC, file_id);
CREATE TRIGGER save_new_file_version AFTER INSERT ON files BEGIN
    INSERT INTO file_versions VALUES (
        NULL, NEW.file_id, NEW.pair_id, NEW.version, NEW.name, NEW.mime, NEW.size_bytes,
        NEW.sha256, NEW.state, NEW.created_at, NEW.deleted_at
    );
END;
CREATE TRIGGER save_changed_file_version AFTER UPDATE ON files BEGIN
    INSERT INTO file_versions VALUES (
        NULL, NEW.file_id, NEW.pair_id, NEW.version, NEW.name, NEW.mime, NEW.size_bytes,
        NEW.sha256, NEW.state, NEW.created_at, NEW.deleted_at
    );
END;
CREATE TABLE file_list_cursors (
    cursor TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    page_limit INTEGER NOT NULL,
    file_cutoff INTEGER NOT NULL,
    last_created_at INTEGER NOT NULL,
    last_file_id TEXT NOT NULL
);
