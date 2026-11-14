//! Scrape-time node state for the metrics listener.
use super::Node;
use crate::metrics::NodeSample;
use std::sync::atomic::Ordering;

impl Node {
    pub fn observe(&self) -> NodeSample {
        NodeSample {
            healthy: self.healthy.load(Ordering::Acquire),
            revision: self.index.read().unwrap().as_ref().map(|i| i.revision),
            capacity_limit: self.capacity_limit,
            capacity_available: self.capacity.available_permits(),
            websocket_sessions: self.sessions.in_use(),
            websocket_turns: self.turns.in_use(),
        }
    }
}
