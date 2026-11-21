CREATE TABLE peer (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    pair_id TEXT NOT NULL,
    incarnation TEXT NOT NULL,
    fence INTEGER NOT NULL CHECK(fence>0)
);
CREATE TABLE retired_peers (pair_id TEXT NOT NULL, incarnation TEXT NOT NULL, PRIMARY KEY(pair_id, incarnation));
CREATE TABLE transport_inbox (
    id INTEGER PRIMARY KEY,
    pair_id TEXT NOT NULL,
    peer TEXT NOT NULL,
    fence INTEGER NOT NULL,
    delivery_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    received_epoch TEXT NOT NULL,
    command_epoch TEXT NOT NULL,
    client_msg_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processed','quarantined','rejected')),
    UNIQUE(pair_id, peer, delivery_id)
);
CREATE INDEX inbox_pending ON transport_inbox(id) WHERE state='pending';
CREATE INDEX inbox_command ON transport_inbox(pair_id, command_epoch, client_msg_id, request_hash);
CREATE TABLE delivery_conflicts (
    pair_id TEXT NOT NULL,
    peer TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    original_hash TEXT NOT NULL,
    conflicting_hash TEXT NOT NULL,
    PRIMARY KEY(pair_id, peer, delivery_id, conflicting_hash)
);
CREATE TABLE command_results (
    pair_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    client_msg_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY(pair_id, epoch, client_msg_id)
);
CREATE TABLE command_rejections (
    pair_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    client_msg_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    code TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY(pair_id,epoch,client_msg_id,request_hash,code)
);
CREATE TABLE transport_outbox (
    id INTEGER PRIMARY KEY,
    delivery_id TEXT NOT NULL UNIQUE,
    pair_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    event_id TEXT NOT NULL,
    frame TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','delivered','quarantined'))
);
CREATE INDEX outbox_pending ON transport_outbox(id) WHERE state='pending';
CREATE UNIQUE INDEX outbox_pending_event ON transport_outbox(pair_id,sender,recipient,event_id) WHERE state='pending';
CREATE TABLE recovery (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    session_id TEXT NOT NULL,
    original_epoch TEXT NOT NULL,
    reviewed_seq INTEGER NOT NULL
);
INSERT INTO metadata(key,value) VALUES ('snapshot_seq','0');
