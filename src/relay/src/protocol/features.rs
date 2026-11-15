//! Provider features that keep tenant content in, or send it out through, the shared
//! upstream account. Each deployment enables them explicitly; all are off by default.
use serde::Deserialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Feature {
    StoredResponses,
    Background,
    Conversations,
    Files,
    VectorStores,
    Batches,
    CodeExecution,
    RemoteMcp,
    WebFetch,
    WebSearch,
}

impl Feature {
    pub const ALL: [Self; 10] = [
        Self::StoredResponses,
        Self::Background,
        Self::Conversations,
        Self::Files,
        Self::VectorStores,
        Self::Batches,
        Self::CodeExecution,
        Self::RemoteMcp,
        Self::WebFetch,
        Self::WebSearch,
    ];
    /// The node capability a snapshot must require before it enables the feature.
    pub fn capability(self) -> &'static str {
        match self {
            Self::StoredResponses => "relay.stored-responses.v1",
            Self::Background => "relay.background-responses.v1",
            Self::Conversations => "relay.conversations.v1",
            Self::Files => "relay.files.v1",
            Self::VectorStores => "relay.vector-stores.v1",
            Self::Batches => "relay.message-batches.v1",
            Self::CodeExecution => "relay.code-execution.v1",
            Self::RemoteMcp => "relay.remote-mcp.v1",
            Self::WebFetch => "relay.web-fetch.v1",
            Self::WebSearch => "relay.web-search.v1",
        }
    }
    fn bit(self) -> u16 {
        1 << self as u16
    }
}

/// The set of features one request or deployment uses.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Features(u16);

impl Features {
    pub fn insert(&mut self, feature: Feature) {
        self.0 |= feature.bit();
    }
    pub fn with(mut self, feature: Feature) -> Self {
        self.insert(feature);
        self
    }
    pub fn contains(self, feature: Feature) -> bool {
        self.0 & feature.bit() != 0
    }
    pub fn covers(self, needed: Self) -> bool {
        needed.0 & !self.0 == 0
    }
    pub fn is_empty(self) -> bool {
        self.0 == 0
    }
    pub fn iter(self) -> impl Iterator<Item = Feature> {
        Feature::ALL.into_iter().filter(move |f| self.contains(*f))
    }
}

impl FromIterator<Feature> for Features {
    fn from_iter<I: IntoIterator<Item = Feature>>(iter: I) -> Self {
        let mut set = Self::default();
        for f in iter {
            set.insert(f);
        }
        set
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_deployment_covers_only_what_it_enables() {
        let enabled: Features = [Feature::StoredResponses, Feature::Files]
            .into_iter()
            .collect();
        assert!(enabled.covers(Features::default()));
        assert!(enabled.covers(Features::default().with(Feature::Files)));
        assert!(!enabled.covers(Features::default().with(Feature::Background)));
        assert_eq!(enabled.iter().count(), 2);
        assert_eq!(
            Feature::ALL
                .iter()
                .map(|f| f.capability())
                .collect::<std::collections::HashSet<_>>()
                .len(),
            Feature::ALL.len()
        );
    }
}
