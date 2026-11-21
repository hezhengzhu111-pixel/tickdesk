use serde_json::{json, Value};
use tickdesk_local::{contract::digest, storage::Storage};

pub const LOCAL_PAIR: &str = "test-pair";
pub const PEER: &str = "test-app";

pub fn command(storage: &Storage, id: &str, key: &str, name: &str, mut fields: Value) -> Value {
    fields["cmd"] = json!(name);
    fields["clientMsgId"] = json!(key);
    fields["serverEpoch"] = json!(storage.identity.server_epoch);
    json!({"v":2,"kind":"command","id":id,"dedupeKey":format!("cmd:{key}"),"ts":1,
        "serverEpoch":storage.identity.server_epoch,"data":fields})
}

pub fn deliver(storage: &Storage, peer: &str, id: &str, payload: &Value) -> Value {
    json!({"v":2, "type":"deliver", "deliveryId":id, "fromIncarnation":peer,
        "toIncarnation":storage.identity.incarnation, "category":"business",
        "payloadHash":digest(payload).unwrap(), "payload":payload})
}

#[allow(dead_code)]
pub fn status_query(
    storage: &mut Storage,
    peer: &str,
    id: &str,
    epoch: &str,
    target: &Value,
    hash: Option<String>,
) -> Value {
    let payload = command(
        storage,
        id,
        id,
        "get_command_status",
        json!({"originalEpoch":epoch, "targetClientMsgId":target["data"]["clientMsgId"],
            "targetRequestHash":hash.unwrap_or_else(|| digest(&target["data"]).unwrap())}),
    );
    let receipt = storage
        .receive(&deliver(storage, peer, id, &payload))
        .unwrap();
    let work = storage
        .job(receipt["deliveryId"].as_str().unwrap())
        .unwrap()
        .unwrap();
    storage.process(work, 100).unwrap().unwrap()
}
