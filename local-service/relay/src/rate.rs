use std::{collections::HashMap, net::IpAddr};
use tokio::time::{Duration, Instant};

const MAX_IPS: usize = 1024;
const IDLE: Duration = Duration::from_secs(60);

pub(crate) struct Bucket {
    capacity: f64,
    per_second: f64,
    available: f64,
    updated: Instant,
}

impl Bucket {
    pub fn new(capacity: u32, per_second: u32, now: Instant) -> Self {
        Self {
            capacity: capacity.into(),
            available: capacity.into(),
            per_second: per_second.into(),
            updated: now,
        }
    }

    pub fn allow(&mut self, cost: usize, now: Instant) -> bool {
        self.available = (self.available
            + now.duration_since(self.updated).as_secs_f64() * self.per_second)
            .min(self.capacity);
        self.updated = now;
        if self.available < cost as f64 {
            return false;
        }
        self.available -= cost as f64;
        true
    }
}

pub(crate) struct Handshakes {
    global: Bucket,
    ips: HashMap<IpAddr, Bucket>,
}

impl Handshakes {
    pub fn new(now: Instant) -> Self {
        Self {
            global: Bucket::new(64, 32, now),
            ips: HashMap::new(),
        }
    }

    pub fn allow(&mut self, ip: IpAddr, now: Instant) -> bool {
        let ip = ip.to_canonical();
        if self.ips.len() >= MAX_IPS && !self.ips.contains_key(&ip) {
            self.ips
                .retain(|_, bucket| now.duration_since(bucket.updated) < IDLE);
            if self.ips.len() >= MAX_IPS {
                return false;
            }
        }
        self.ips
            .entry(ip)
            .or_insert_with(|| Bucket::new(16, 4, now))
            .allow(1, now)
            && self.global.allow(1, now)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn already_limited_ip_cannot_consume_global_handshakes() {
        let now = Instant::now();
        let mut rate = Handshakes::new(now);
        let one = "192.0.2.1".parse().unwrap();
        let another = "192.0.2.2".parse().unwrap();
        for _ in 0..16 {
            assert!(rate.allow(one, now));
        }
        for _ in 0..64 {
            assert!(!rate.allow(one, now));
        }
        assert!(rate.allow(another, now));
    }
}
