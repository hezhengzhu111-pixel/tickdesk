use crate::{
    auth::{Identity, Role, Roles, MAX_PAIRS},
    protocol::{Failure, Hello, Incoming, Lane, Protocol},
    queue::{Item, Queue},
    rate::Handshakes,
};
use bytes::Bytes;
use ring::rand::{SecureRandom, SystemRandom};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    net::IpAddr,
    path::PathBuf,
    sync::{Arc, Mutex, MutexGuard},
};
use tokio::{
    sync::{watch, Notify},
    time::{self, Duration, Instant},
};

const MAX_PENDING: usize = 32;
pub(crate) const PING_INTERVAL: Duration = Duration::from_secs(15);
pub(crate) const PONG_TIMEOUT: Duration = Duration::from_secs(45);

type ConnectionId = u64;
type Key = (String, Role);

pub struct Relay {
    boot_id: String,
    state: Mutex<State>,
    pub(crate) protocol: Protocol,
    stopped: watch::Sender<bool>,
    drained: Notify,
}

struct State {
    roles: Roles,
    handshakes: Handshakes,
    next_id: ConnectionId,
    sessions: HashMap<ConnectionId, Session>,
    active: HashMap<Key, ConnectionId>,
    counts: BTreeMap<&'static str, u64>,
    closes: BTreeMap<u16, u64>,
}

struct Session {
    identity: Identity,
    key: Key,
    hello: Option<Hello>,
    queue: Queue,
    wake: Arc<Notify>,
    closed: watch::Sender<Option<u16>>,
    last_pong: Option<(Instant, Duration)>,
}

#[derive(Clone, Copy)]
pub(crate) enum AdmissionError {
    Unauthorized,
    Busy,
    Unavailable,
}

pub(crate) struct Connection {
    relay: Arc<Relay>,
    pub id: ConnectionId,
    pub role: Role,
    pub wake: Arc<Notify>,
    pub closed: watch::Receiver<Option<u16>>,
}

impl Drop for Connection {
    fn drop(&mut self) {
        let mut state = self.relay.lock();
        state.close(self.id, 1006);
        state.sessions.remove(&self.id);
        if state.sessions.is_empty() {
            self.relay.drained.notify_one();
        }
    }
}

impl Relay {
    pub fn new(roles: Roles) -> anyhow::Result<Arc<Self>> {
        let mut random = [0; 16];
        random_bytes(&mut random).map_err(|_| anyhow::anyhow!("cannot generate relay boot id"))?;
        let boot_id =
            base64::Engine::encode(&base64::engine::general_purpose::URL_SAFE_NO_PAD, random);
        Ok(Arc::new(Self {
            boot_id,
            protocol: Protocol::new()?,
            state: Mutex::new(State {
                roles,
                handshakes: Handshakes::new(Instant::now()),
                next_id: 0,
                sessions: HashMap::new(),
                active: HashMap::new(),
                counts: BTreeMap::new(),
                closes: BTreeMap::new(),
            }),
            stopped: watch::channel(false).0,
            drained: Notify::new(),
        }))
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().expect("relay state lock")
    }

    pub fn is_ready(&self) -> bool {
        let state = self.lock();
        !*self.stopped.borrow() && state.roles.has_complete_pair()
    }

    pub(crate) fn admit(
        self: &Arc<Self>,
        ip: IpAddr,
        token: Option<&str>,
    ) -> Result<Connection, AdmissionError> {
        let mut state = self.lock();
        let now = Instant::now();
        if *self.stopped.borrow() {
            return Err(AdmissionError::Unavailable);
        }
        if !state.handshakes.allow(ip, now) {
            state.count("handshake_limited");
            return Err(AdmissionError::Busy);
        }
        let Some(identity) = token.and_then(|token| state.roles.authenticate(token, now)) else {
            state.count("handshake_unauthorized");
            return Err(AdmissionError::Unauthorized);
        };
        if state.sessions.len() >= 2 * MAX_PAIRS + MAX_PENDING
            || state.sessions.len() - state.active.len() >= MAX_PENDING
        {
            state.count("handshake_limited");
            return Err(AdmissionError::Busy);
        }
        state.next_id = state
            .next_id
            .checked_add(1)
            .expect("connection id exhausted");
        let id = state.next_id;
        let role = identity.role;
        let key = (identity.pair_id.clone(), role);
        let wake = Arc::new(Notify::new());
        let (closed, receiver) = watch::channel(None);
        state.sessions.insert(
            id,
            Session {
                identity,
                key,
                hello: None,
                wake: Arc::clone(&wake),
                closed,
                queue: Queue::default(),
                last_pong: None,
            },
        );
        Ok(Connection {
            relay: Arc::clone(self),
            id,
            role,
            wake,
            closed: receiver,
        })
    }

    pub(crate) fn activate(&self, id: ConnectionId, hello: Hello) -> bool {
        let mut state = self.lock();
        if !state.open(id) {
            return false;
        }
        // Replacement, welcome, and both presence notifications share the routing lock.
        let key = state.sessions[&id].key.clone();
        let role = key.1;
        let peer = state.peer(&key).filter(|peer| state.open(*peer));
        state.sessions.get_mut(&id).expect("admitted socket").hello = Some(hello);
        if let Some(old) = state.active.insert(key.clone(), id) {
            state.close(old, 4409);
        }
        state.control(
            id,
            json!({"v":2, "type":"welcome", "pairId":key.0,
            "role":role, "relayBootId":self.boot_id, "connectionId":format!("conn_{id}"),
            "heartbeatIntervalMs":PING_INTERVAL.as_millis() as u64,
            "heartbeatTimeoutMs":PONG_TIMEOUT.as_millis() as u64}),
        );
        let presence = state.presence(peer);
        state.control(id, presence);
        if let Some(peer) = peer {
            let presence = state.presence(Some(id));
            state.control(peer, presence);
        }
        state.count(match role {
            Role::App => "app_connected",
            Role::Local => "local_connected",
        });
        true
    }

    pub(crate) fn route(&self, id: ConnectionId, incoming: Incoming, bytes: Bytes) {
        let mut state = self.lock();
        if !state.current(id) {
            return;
        }
        let source = &state.sessions[&id];
        let failure = |code| Failure::new(code, incoming.id.as_deref());
        if let Some((from, _)) = &incoming.incarnations {
            if *from != source.hello.as_ref().expect("active hello").incarnation {
                state.reject(id, failure("INCARNATION_MISMATCH"));
                return;
            }
        }
        let Some(peer) = state.peer(&source.key) else {
            // An endpoint error is advisory. Never generate an error in response to an error.
            if incoming.incarnations.is_some() {
                state.reject(id, failure("PEER_OFFLINE"));
            }
            return;
        };
        if !state.open(peer) {
            if incoming.incarnations.is_some() {
                state.reject(id, failure("PEER_OFFLINE"));
            }
            return;
        }
        if let Some((_, to)) = &incoming.incarnations {
            if *to
                != state.sessions[&peer]
                    .hello
                    .as_ref()
                    .expect("active peer")
                    .incarnation
            {
                state.reject(id, failure("INCARNATION_MISMATCH"));
                return;
            }
        }
        if !state.enqueue(
            peer,
            incoming.lane,
            Item {
                bytes,
                source: Some(id),
            },
        ) {
            if incoming.incarnations.is_some() {
                state.reject(id, failure("BUSY"));
            } else {
                state.close(id, 1013);
            }
            state.count("queue_overflow");
        } else {
            state.count(match incoming.lane {
                Lane::Business => "business_forward_queued",
                Lane::File => "file_forward_queued",
                Lane::Control => "control_forward_queued",
            });
        }
    }

    pub(crate) fn pop(&self, id: ConnectionId) -> Option<Item> {
        let mut state = self.lock();
        if !state.current(id) {
            return None;
        }
        loop {
            let item = state.sessions.get_mut(&id)?.queue.pop()?;
            // An old connection can have frames waiting in the other socket's queue.
            if item.source.is_none_or(|source| state.current(source)) {
                return Some(item);
            }
            state.count("fenced_queued_frame");
        }
    }

    pub(crate) fn start_write<T>(
        &self,
        id: ConnectionId,
        source: Option<ConnectionId>,
        poll: impl FnOnce() -> T,
    ) -> Option<T> {
        let mut state = self.lock();
        if !state.current(id) || source.is_some_and(|source| !state.current(source)) {
            return None;
        }
        // Poll only the first nonblocking transport write under the same lock as replacement.
        // If the transport returns Pending, the writer validates both sessions again when woken.
        Some(poll())
    }

    pub(crate) fn reject(&self, id: ConnectionId, failure: Failure) {
        let mut state = self.lock();
        if state.current(id) {
            state.reject(id, failure);
        }
    }

    pub(crate) fn close(&self, id: ConnectionId, code: u16) {
        self.lock().close(id, code);
    }

    pub(crate) fn pong(&self, id: ConnectionId, now: Instant, rtt: Duration) {
        let mut state = self.lock();
        if state.current(id) {
            state
                .sessions
                .get_mut(&id)
                .expect("active socket")
                .last_pong = Some((now, rtt));
        }
    }

    pub fn reload_roles(&self, mut roles: Roles) {
        let mut state = self.lock();
        roles.preserve_rotation_deadlines(&state.roles);
        state.roles = roles;
        state.reap_revocations();
        state.count("roles_reloaded");
    }

    pub async fn watch_roles(self: Arc<Self>, path: PathBuf) {
        let mut stopped = self.stopped.subscribe();
        let mut interval = time::interval(Duration::from_secs(1));
        interval.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
        let mut last_observed = None;
        loop {
            tokio::select! {
                biased;
                _ = stopped.wait_for(|stopped| *stopped) => break,
                _ = interval.tick() => {}
            }
            self.lock().reap_revocations();
            let path = path.clone();
            let read = tokio::task::spawn_blocking(move || Roles::read(&path)).await;
            match read {
                Ok(Ok(bytes)) if last_observed.as_ref() == Some(&bytes) => {}
                Ok(Ok(bytes)) => {
                    match Roles::from_bytes(&bytes) {
                        Ok(roles) => self.reload_roles(roles),
                        Err(_) => self.reload_failed(),
                    }
                    last_observed = Some(bytes);
                }
                _ => {
                    // Only log the transition into a read failure, even across polling ticks.
                    if last_observed.as_ref().is_none_or(|bytes| !bytes.is_empty()) {
                        self.reload_failed();
                    }
                    last_observed = Some(Vec::new());
                }
            }
        }
    }

    fn reload_failed(&self) {
        self.lock().count("role_reload_rejected");
        tracing::warn!("role digest reload rejected; retaining previous configuration");
    }

    pub fn stop(&self) {
        let mut state = self.lock();
        self.stopped.send_replace(true);
        state.active.clear();
        let ids: Vec<_> = state.sessions.keys().copied().collect();
        for id in ids {
            state.close(id, 1012);
        }
    }

    pub async fn wait_drained(&self) {
        loop {
            let changed = self.drained.notified();
            if self.lock().sessions.is_empty() {
                break;
            }
            changed.await;
        }
    }

    pub(crate) fn metrics(&self) -> String {
        use std::fmt::Write as _;
        let state = self.lock();
        let mut text = String::new();
        let now = Instant::now();
        for role in [Role::App, Role::Local] {
            let name = if role == Role::App { "app" } else { "local" };
            let mut online = 0;
            let mut items = 0;
            let mut bytes = 0;
            let mut max_age = 0;
            let mut max_rtt = 0;
            let mut pongs = 0;
            for (key, id) in &state.active {
                if key.1 == role {
                    let session = &state.sessions[id];
                    online += 1;
                    let (count, size) = session.queue.occupancy();
                    items += count;
                    bytes += size;
                    if let Some((pong, rtt)) = session.last_pong {
                        pongs += 1;
                        max_age = max_age.max(now.duration_since(pong).as_millis());
                        max_rtt = max_rtt.max(rtt.as_millis());
                    }
                }
            }
            for (stat, value) in [
                ("connected", online),
                ("queued_messages", items),
                ("queued_bytes", bytes),
                ("heartbeat_sampled_connections", pongs),
            ] {
                let _ = writeln!(text, "tickdesk_relay_{stat}{{role=\"{name}\"}} {value}");
            }
            for (stat, value) in [("pong_age_max_ms", max_age), ("ping_rtt_max_ms", max_rtt)] {
                let _ = writeln!(text, "tickdesk_relay_{stat}{{role=\"{name}\"}} {value}");
            }
        }
        for (event, count) in &state.counts {
            let _ = writeln!(
                text,
                "tickdesk_relay_events_total{{event=\"{event}\"}} {count}"
            );
        }
        for (code, count) in &state.closes {
            let _ = writeln!(
                text,
                "tickdesk_relay_closes_total{{code=\"{code}\"}} {count}"
            );
        }
        text
    }
}

impl State {
    fn open(&mut self, id: ConnectionId) -> bool {
        let Some(session) = self.sessions.get(&id) else {
            return false;
        };
        if session.closed.borrow().is_some() {
            return false;
        }
        if !self.roles.permits(&session.identity, Instant::now()) {
            self.close(id, 4401);
            return false;
        }
        true
    }

    fn current(&mut self, id: ConnectionId) -> bool {
        self.open(id) && self.active.get(&self.sessions[&id].key) == Some(&id)
    }

    fn peer(&self, key: &Key) -> Option<ConnectionId> {
        self.active.get(&(key.0.clone(), key.1.opposite())).copied()
    }

    fn presence(&self, id: Option<ConnectionId>) -> Value {
        let hello = id.and_then(|id| self.sessions[&id].hello.as_ref());
        json!({"v":2,"type":"presence","peer":{
            "online":hello.is_some(),
            "incarnation":hello.map(|hello| &hello.incarnation),
            "serverEpoch":hello.and_then(|hello| hello.epoch.as_ref())
        }})
    }

    fn enqueue(&mut self, id: ConnectionId, lane: Lane, item: Item) -> bool {
        let session = self.sessions.get_mut(&id).expect("queued socket");
        if session.queue.push(lane, item) {
            session.wake.notify_one();
            return true;
        }
        false
    }

    fn control(&mut self, id: ConnectionId, value: Value) {
        let bytes = Bytes::from(serde_json::to_vec(&value).expect("serialize control frame"));
        if !self.enqueue(
            id,
            Lane::Control,
            Item {
                bytes,
                source: None,
            },
        ) {
            self.close(id, 1013);
        }
    }

    fn reject(&mut self, id: ConnectionId, failure: Failure) {
        self.count(failure.code());
        self.control(id, failure.frame());
    }

    fn count(&mut self, event: &'static str) {
        *self.counts.entry(event).or_default() += 1;
    }

    fn close(&mut self, id: ConnectionId, code: u16) {
        let Some(session) = self.sessions.get_mut(&id) else {
            return;
        };
        if session.closed.borrow().is_some() {
            return;
        }
        session.closed.send_replace(Some(code));
        session.queue = Queue::default();
        *self.closes.entry(code).or_default() += 1;
        let key = session.key.clone();
        if self.active.get(&key) == Some(&id) {
            self.active.remove(&key);
            if let Some(peer) = self.peer(&key) {
                self.control(peer, self.presence(None));
            }
        }
    }

    fn reap_revocations(&mut self) {
        let now = Instant::now();
        let revoked: Vec<_> = self
            .sessions
            .iter()
            .filter_map(|(&id, session)| {
                (session.closed.borrow().is_none() && !self.roles.permits(&session.identity, now))
                    .then_some(id)
            })
            .collect();
        let mut removed = Vec::new();
        for &id in &revoked {
            let key = &self.sessions[&id].key;
            if self.active.get(key) == Some(&id) {
                self.active.remove(key);
                removed.push(key.clone());
            }
        }
        for id in revoked {
            self.close(id, 4401);
        }
        for key in removed {
            if let Some(peer) = self.peer(&key) {
                self.control(peer, self.presence(None));
            }
        }
    }
}

pub(crate) fn random_bytes(bytes: &mut [u8]) -> Result<(), ring::error::Unspecified> {
    SystemRandom::new().fill(bytes)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};

    pub(crate) fn token(byte: u8) -> String {
        URL_SAFE_NO_PAD.encode([byte; 32])
    }

    pub(crate) fn relay() -> Arc<Relay> {
        let app = crate::protocol::sha256_hex(token(1).as_bytes());
        let local = crate::protocol::sha256_hex(token(2).as_bytes());
        let config =
            format!("[pairs.test.app]\nsha256='{app}'\n[pairs.test.local]\nsha256='{local}'");
        Relay::new(Roles::parse(&config, Duration::ZERO, Instant::now()).unwrap()).unwrap()
    }

    pub(crate) fn open(relay: &Arc<Relay>, byte: u8, incarnation: &str) -> Connection {
        let conn = relay
            .admit("127.0.0.1".parse().unwrap(), Some(&token(byte)))
            .ok()
            .unwrap();
        assert!(relay.activate(
            conn.id,
            Hello {
                incarnation: incarnation.to_owned(),
                epoch: (conn.role == Role::Local).then(|| "epoch1".to_owned()),
            }
        ));
        conn
    }

    fn delivery() -> Incoming {
        Incoming {
            lane: Lane::File,
            id: Some("delivery_2".to_owned()),
            incarnations: Some(("a".to_owned(), "l".to_owned())),
        }
    }

    #[test]
    fn busy_keeps_control_budget_and_replacement_discards_old_queued_data_and_late_callbacks() {
        let relay = relay();
        let old = open(&relay, 1, "a");
        let local = open(&relay, 2, "l");
        while relay.pop(old.id).is_some() {}
        while relay.pop(local.id).is_some() {}

        for _ in 0..5 {
            relay.route(old.id, delivery(), Bytes::from_static(b"original file"));
        }
        let response: Value = serde_json::from_slice(&relay.pop(old.id).unwrap().bytes).unwrap();
        assert_eq!(
            response,
            json!({"v":2,"type":"error","refDeliveryId":"delivery_2",
            "code":"BUSY","retryable":true,"retryAfterMs":1000})
        );

        let dequeued = relay.pop(local.id).unwrap();
        let new = open(&relay, 1, "a");
        assert_eq!(*old.closed.borrow(), Some(4409));
        assert!(relay
            .start_write(local.id, dequeued.source, || panic!(
                "fenced frame was written"
            ))
            .is_none());
        relay.route(old.id, delivery(), Bytes::from_static(b"late old frame"));
        drop(old);
        let presence: Value = serde_json::from_slice(&relay.pop(local.id).unwrap().bytes).unwrap();
        assert_eq!(presence["peer"]["online"], true);
        assert!(relay.pop(local.id).is_none());
        relay.route(new.id, delivery(), Bytes::from_static(b"new frame"));
        assert_eq!(relay.pop(local.id).unwrap().bytes, "new frame");
        assert_eq!(*new.closed.borrow(), None);
    }
}
