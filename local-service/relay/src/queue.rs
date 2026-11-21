use crate::protocol::Lane;
use bytes::Bytes;
use std::collections::VecDeque;

const MAX_ITEMS: usize = 32;
const MAX_BYTES: usize = 1024 * 1024;
const CONTROL_ITEMS: usize = 4;
const CONTROL_BYTES: usize = 16 * 1024;
const FILE_ITEMS: usize = 4;
const BUSINESS_WEIGHT: u8 = 8;

pub(crate) struct Item {
    pub bytes: Bytes,
    pub source: Option<u64>,
}

#[derive(Default)]
pub(crate) struct Queue {
    control: VecDeque<Item>,
    business: VecDeque<Item>,
    files: VecDeque<Item>,
    bytes: usize,
    control_bytes: usize,
    business_turns: u8,
}

impl Queue {
    pub fn push(&mut self, lane: Lane, item: Item) -> bool {
        let bytes = item.bytes.len();
        let items = self.control.len() + self.business.len() + self.files.len();
        if items >= MAX_ITEMS || bytes + self.bytes > MAX_BYTES {
            return false;
        }
        if lane == Lane::Control {
            if self.control.len() >= CONTROL_ITEMS || bytes + self.control_bytes > CONTROL_BYTES {
                return false;
            }
            self.control_bytes += bytes;
            self.control.push_back(item);
        } else {
            let data_items = self.business.len() + self.files.len();
            let data_bytes = self.bytes - self.control_bytes;
            if data_items >= MAX_ITEMS - CONTROL_ITEMS
                || data_bytes + bytes > MAX_BYTES - CONTROL_BYTES
                || (lane == Lane::File && self.files.len() >= FILE_ITEMS)
            {
                return false;
            }
            match lane {
                Lane::Business => self.business.push_back(item),
                Lane::File => self.files.push_back(item),
                Lane::Control => unreachable!(),
            }
        }
        self.bytes += bytes;
        true
    }

    pub fn pop(&mut self) -> Option<Item> {
        let item = if let Some(control) = self.control.pop_front() {
            self.control_bytes -= control.bytes.len();
            control
        } else if !self.files.is_empty()
            && (self.business.is_empty() || self.business_turns >= BUSINESS_WEIGHT)
        {
            self.business_turns = 0;
            self.files.pop_front()?
        } else {
            let item = self.business.pop_front()?;
            self.business_turns = self.business_turns.saturating_add(1);
            item
        };
        self.bytes -= item.bytes.len();
        Some(item)
    }

    pub fn occupancy(&self) -> (usize, usize) {
        (
            self.control.len() + self.business.len() + self.files.len(),
            self.bytes,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(size: usize, fill: u8) -> Item {
        Item {
            bytes: Bytes::from(vec![fill; size]),
            source: None,
        }
    }

    #[test]
    fn slow_consumer_has_reserved_control_capacity_and_weighted_data() {
        let mut queue = Queue::default();
        for _ in 0..24 {
            assert!(queue.push(Lane::Business, item(1, b'b')));
        }
        for _ in 0..4 {
            assert!(queue.push(Lane::File, item(1, b'f')));
        }
        assert!(!queue.push(Lane::File, item(1, b'f')));
        assert!(!queue.push(Lane::Business, item(1, b'b')));
        assert!(queue.push(Lane::Control, item(CONTROL_BYTES, b'c')));
        assert!(!queue.push(Lane::Control, item(1, b'c')));
        assert_eq!(queue.pop().unwrap().bytes[0], b'c');
        for _ in 0..8 {
            assert_eq!(queue.pop().unwrap().bytes[0], b'b');
        }
        assert_eq!(queue.pop().unwrap().bytes[0], b'f');

        let mut queue = Queue::default();
        assert!(queue.push(Lane::Business, item(MAX_BYTES - CONTROL_BYTES, b'b')));
        assert!(!queue.push(Lane::Business, item(1, b'b')));
        assert!(queue.push(Lane::Control, item(CONTROL_BYTES, b'c')));
        assert_eq!(queue.occupancy(), (2, MAX_BYTES));
    }
}
