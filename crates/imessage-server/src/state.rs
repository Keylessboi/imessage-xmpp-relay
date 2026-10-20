//! Persisted server state (`<data_dir>/state.plist`).
//!
//! This file is the one durable artefact the server owns. It is deliberately a
//! plist, because the real backend stores the rustpush `APSState` / `IDSUser`
//! set here as an opaque blob (see [`ServerState::backend_state`]).
//!
//! NEVER print the contents: it holds account material.

use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::events::EventBus;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct ServerState {
    /// Apple ID the server is configured for (exposed on `/im/status`).
    #[serde(default)]
    pub apple_id: String,
    /// True once the Apple ID IDS user (and it alone) is registered.
    #[serde(default)]
    pub registered: bool,
    /// Active IDS handles, e.g. `["tel:+15551234567", "mailto:me@example.com"]`.
    #[serde(default)]
    pub handles: Vec<String>,
    /// APNs push token, hex, once the APS connection has one.
    #[serde(default)]
    pub apns_token: Option<String>,
    /// Opaque backend blob (rustpush push state, IDS users, NGM identity).
    #[serde(default)]
    pub backend_state: Option<plist::Data>,
}

/// Cheap-to-clone handle to the shared state plus its on-disk copy.
#[derive(Clone)]
pub struct StateStore {
    path: PathBuf,
    inner: Arc<RwLock<ServerState>>,
    bus: EventBus,
}

impl StateStore {
    /// Load `state.plist` (creating it on first boot). `apple_id` is only used
    /// to seed an empty state file.
    pub fn load(path: PathBuf, apple_id: String, bus: EventBus) -> Result<Self> {
        let mut state = if path.exists() {
            let raw = std::fs::read(&path)
                .with_context(|| format!("reading state file {}", path.display()))?;
            plist::from_bytes::<ServerState>(&raw)
                .with_context(|| format!("parsing state file {}", path.display()))?
        } else {
            ServerState::default()
        };
        if state.apple_id.is_empty() {
            state.apple_id = apple_id;
        }
        let store = Self {
            path,
            inner: Arc::new(RwLock::new(state)),
            bus,
        };
        // Materialise the file so nothing downstream has to care about first boot.
        store.save()?;
        Ok(store)
    }

    pub fn snapshot(&self) -> ServerState {
        self.inner.read().expect("state lock poisoned").clone()
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn registered(&self) -> bool {
        self.inner.read().expect("state lock poisoned").registered
    }

    pub fn handles(&self) -> Vec<String> {
        self.inner
            .read()
            .expect("state lock poisoned")
            .handles
            .clone()
    }

    pub fn apns_token(&self) -> Option<String> {
        self.inner
            .read()
            .expect("state lock poisoned")
            .apns_token
            .clone()
    }

    pub fn backend_state(&self) -> Option<plist::Data> {
        self.inner
            .read()
            .expect("state lock poisoned")
            .backend_state
            .clone()
    }

    /// Persist the current snapshot atomically. Safe to call from a sync context.
    pub fn save(&self) -> Result<()> {
        let state = self.snapshot();
        self.save_locked(&state)
    }

    /// Write one snapshot to disk (temp file + rename) without taking the state
    /// lock. The caller is responsible for whatever lock it needs; in
    /// particular update_backend_state calls this with the write lock held, so
    /// its read-modify-write cannot interleave with another writer.
    fn save_locked(&self, state: &ServerState) -> Result<()> {
        let mut buf = Vec::new();
        plist::to_writer_xml(&mut buf, state).context("serialising state")?;

        let tmp = self.path.with_extension("plist.tmp");
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir)
                .with_context(|| format!("creating data dir {}", dir.display()))?;
        }
        std::fs::write(&tmp, &buf).with_context(|| format!("writing {}", tmp.display()))?;
        restrict_permissions(&tmp);
        std::fs::rename(&tmp, &self.path)
            .with_context(|| format!("renaming into {}", self.path.display()))?;
        Ok(())
    }

    /// Mark registration. Publishes `{"type":"ready"}` on a false -> true edge,
    /// which is the only place the ready event can come from.
    pub fn set_registered(&self, registered: bool, handles: Vec<String>) {
        let transition = {
            let mut guard = self.inner.write().expect("state lock poisoned");
            let was = guard.registered;
            guard.registered = registered;
            guard.handles = handles;
            !was && registered
        };
        if let Err(e) = self.save() {
            tracing::error!("failed to persist state: {e:#}");
        }
        if transition {
            self.bus.ready();
        }
    }

    pub fn set_handles(&self, handles: Vec<String>) {
        self.inner.write().expect("state lock poisoned").handles = handles;
        if let Err(e) = self.save() {
            tracing::error!("failed to persist state: {e:#}");
        }
    }

    pub fn set_apns_token(&self, token: Option<String>) {
        self.inner.write().expect("state lock poisoned").apns_token = token;
        if let Err(e) = self.save() {
            tracing::error!("failed to persist state: {e:#}");
        }
    }

    pub fn set_backend_state(&self, blob: Option<plist::Data>) {
        self.inner
            .write()
            .expect("state lock poisoned")
            .backend_state = blob;
        if let Err(e) = self.save() {
            tracing::error!("failed to persist state: {e:#}");
        }
    }

    /// Read-modify-write the opaque backend blob in one step.
    ///
    /// backend_state has two independent writers in the real backend: the APS
    /// path saves the rustpush push state (APNs keypair + token) as soon as the
    /// connection has one - on a degraded boot too, before any registration -
    /// while an IDS registration saves the user set. A plain backend_state()
    /// followed by set_backend_state() in either one would drop the other's
    /// fields, so the whole read-modify-write, including the save, happens
    /// under the state write lock.
    pub fn update_backend_state<F>(&self, update: F) -> Result<()>
    where
        F: FnOnce(Option<plist::Data>) -> Option<plist::Data>,
    {
        let mut guard = self.inner.write().expect("state lock poisoned");
        guard.backend_state = update(guard.backend_state.take());
        self.save_locked(&*guard)
    }
}

fn restrict_permissions(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))]
    let _ = path;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_path(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "imessage-server-test-{}-{name}.plist",
            uuid::Uuid::new_v4()
        ))
    }

    #[test]
    fn creates_and_reloads() {
        let path = temp_path("reload");
        let bus = EventBus::new();
        let store = StateStore::load(path.clone(), "me@example.com".into(), bus.clone()).unwrap();
        assert!(!store.registered());
        assert!(path.exists());

        store.set_apns_token(Some("aabb".into()));
        store.set_registered(true, vec!["mailto:me@example.com".into()]);

        let mut rx = bus.subscribe();
        // The ready published above happened before we subscribed; reload instead.
        let reloaded =
            StateStore::load(path.clone(), "other@example.com".into(), bus.clone()).unwrap();
        assert!(reloaded.registered());
        assert_eq!(reloaded.snapshot().apple_id, "me@example.com");
        assert_eq!(reloaded.apns_token().as_deref(), Some("aabb"));
        assert!(rx.try_recv().is_err());

        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn ready_is_published_on_transition_only() {
        let path = temp_path("ready");
        let bus = EventBus::new();
        let store = StateStore::load(path.clone(), "me@example.com".into(), bus.clone()).unwrap();
        let mut rx = bus.subscribe();

        store.set_registered(true, vec![]);
        assert!(matches!(
            rx.try_recv().unwrap(),
            crate::events::Event::Ready
        ));

        store.set_registered(true, vec![]);
        assert!(
            rx.try_recv().is_err(),
            "no second ready on a no-op transition"
        );

        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn update_backend_state_reads_modifies_writes() {
        let path = temp_path("backend-rmw");
        let bus = EventBus::new();
        let store = StateStore::load(path.clone(), "me@example.com".into(), bus).unwrap();

        store
            .update_backend_state(|_| Some(plist::Data::new(b"one".to_vec())))
            .unwrap();
        // The second update must see the first one's blob, i.e. this is a real
        // read-modify-write rather than two blind writes.
        store
            .update_backend_state(|blob| {
                let seen: Option<Vec<u8>> = blob.map(|data| data.as_ref().to_vec());
                assert_eq!(seen.as_deref(), Some(b"one".as_slice()));
                Some(plist::Data::new(b"two".to_vec()))
            })
            .unwrap();

        let reloaded =
            StateStore::load(path.clone(), "other@example.com".into(), EventBus::new()).unwrap();
        let stored: Option<Vec<u8>> = reloaded.backend_state().map(|data| data.as_ref().to_vec());
        assert_eq!(stored.as_deref(), Some(b"two".as_slice()));

        let _ = std::fs::remove_file(path);
    }
}
