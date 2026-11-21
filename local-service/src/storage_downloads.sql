CREATE TABLE file_downloads (
    download_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES files(file_id),
    pair_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    file_version INTEGER NOT NULL,
    total_chunks INTEGER NOT NULL CHECK(total_chunks BETWEEN 1 AND 640),
    expires_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('sending','complete','cancelled','expired','failed'))
);
CREATE INDEX file_download_leases ON file_downloads(file_id,expires_at) WHERE state='sending';
CREATE INDEX file_download_queue ON file_downloads(expires_at,download_id) WHERE state='sending';
CREATE TABLE file_download_chunks (
    download_id TEXT NOT NULL REFERENCES file_downloads(download_id),
    chunk_index INTEGER NOT NULL CHECK(chunk_index BETWEEN 0 AND 639),
    delivery_id TEXT NOT NULL UNIQUE REFERENCES transport_outbox(delivery_id),
    PRIMARY KEY (download_id,chunk_index)
) WITHOUT ROWID;
