//! Group-chat control surface: ADDITIVE endpoints and the `group` SSE event.
//!
//! The frozen `POST /im/send` and the `message` event are untouched. Groups add:
//!
//! ```text
//! GET  /im/groups         -> {"groups":[GroupInfo,...]}
//! POST /im/group          <- {"participants":[str],"name":str|null,"text":str|null}
//!                         -> {"ok":true,"chat_guid":str,"guid":str|null}
//! POST /im/group/rename   <- {"chat_guid":str,"name":str}            -> {"ok":true,"group":GroupInfo}
//! POST /im/group/members  <- {"chat_guid":str,"add":[str],"remove":[str]} -> {"ok":true,"group":GroupInfo}
//! POST /im/group/leave    <- {"chat_guid":str}                       -> {"ok":true}
//! ```
//!
//! Errors are the same `(status, {"error":...})` shape as everywhere else. The
//! backend owns the group state (member list, name, iMessage group id); this
//! module only validates the request shape and hands a [`GroupOp`] over.

use std::sync::Arc;

use axum::extract::rejection::JsonRejection;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::api::{ApiError, AppState};
use crate::backend::BackendError;
use crate::events::strip_handle;

/// One group conversation as the backend knows it.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct GroupInfo {
    pub chat_guid: String,
    pub name: Option<String>,
    /// Bare handles (no `tel:`/`mailto:`), including this server's own handle.
    pub participants: Vec<String>,
    pub group_version: u64,
}

/// A mutation the HTTP layer asks the backend to perform.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GroupOp {
    Create {
        participants: Vec<String>,
        name: Option<String>,
        text: Option<String>,
    },
    Rename {
        chat_guid: String,
        name: String,
    },
    Members {
        chat_guid: String,
        add: Vec<String>,
        remove: Vec<String>,
    },
    Leave {
        chat_guid: String,
    },
}

/// What a [`GroupOp`] produced.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct GroupResult {
    /// The group after the change (`None` after a leave).
    pub group: Option<GroupInfo>,
    /// Guid of the first message when a create carried text.
    pub guid: Option<String>,
}

/// `Event::Group` payload (`{"type":"group","kind":...}`), published when a
/// peer renames a group, changes its members or its icon.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct GroupEvent {
    pub guid: String,
    pub chat_guid: String,
    /// `rename` | `participants` | `icon`
    pub kind: String,
    /// Bare handle of whoever made the change.
    pub from: String,
    pub name: Option<String>,
    /// Members after the change, bare handles.
    pub participants: Vec<String>,
    pub added: Vec<String>,
    pub removed: Vec<String>,
    pub timestamp: i64,
}

/// Trim, strip `tel:`/`mailto:`, drop empties and duplicates, keep order.
pub fn clean_handles(raw: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for item in raw {
        let handle = strip_handle(item.trim());
        if handle.is_empty() || out.contains(&handle) {
            continue;
        }
        out.push(handle);
    }
    out
}

/// Validate a create request: at least two *other* people (three members with
/// us), since two members is a plain 1:1 chat in iMessage.
pub fn validate_create(
    participants: &[String],
    name: &Option<String>,
    text: &Option<String>,
) -> Result<(), String> {
    if clean_handles(participants).len() < 2 {
        return Err("a group needs at least two participants besides this account".into());
    }
    let has_text = text.as_deref().map(|t| !t.trim().is_empty()).unwrap_or(false);
    let has_name = name.as_deref().map(|n| !n.trim().is_empty()).unwrap_or(false);
    if !has_text && !has_name {
        return Err("a new group needs a first message ('text') or a 'name'".into());
    }
    Ok(())
}

#[derive(Deserialize)]
struct CreateBody {
    participants: Vec<String>,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    text: Option<String>,
}

#[derive(Deserialize)]
struct RenameBody {
    chat_guid: String,
    name: String,
}

#[derive(Deserialize)]
struct MembersBody {
    chat_guid: String,
    #[serde(default)]
    add: Vec<String>,
    #[serde(default)]
    remove: Vec<String>,
}

#[derive(Deserialize)]
struct LeaveBody {
    chat_guid: String,
}

fn bad(message: impl Into<String>) -> ApiError {
    ApiError::new(StatusCode::BAD_REQUEST, message)
}

fn body<T>(body: Result<Json<T>, JsonRejection>) -> Result<T, ApiError> {
    body.map(|Json(b)| b)
        .map_err(|e| bad(format!("invalid body: {e}")))
}

fn require_registered(app: &AppState) -> Result<(), ApiError> {
    if app.store.registered() {
        Ok(())
    } else {
        Err(ApiError::from(BackendError::not_registered()))
    }
}

fn require_chat_guid(raw: &str) -> Result<String, ApiError> {
    let guid = raw.trim();
    if guid.is_empty() {
        Err(bad("'chat_guid' must not be empty"))
    } else {
        Ok(guid.to_string())
    }
}

async fn list_groups(State(app): State<Arc<AppState>>) -> Result<Json<Value>, ApiError> {
    let groups = app.backend.list_groups().await?;
    Ok(Json(json!({ "groups": groups })))
}

async fn create_group(
    State(app): State<Arc<AppState>>,
    req: Result<Json<CreateBody>, JsonRejection>,
) -> Result<Json<Value>, ApiError> {
    let req = body(req)?;
    validate_create(&req.participants, &req.name, &req.text).map_err(bad)?;
    require_registered(&app)?;
    let result = app
        .backend
        .group_op(GroupOp::Create {
            participants: clean_handles(&req.participants),
            name: req.name.map(|n| n.trim().to_string()).filter(|n| !n.is_empty()),
            text: req.text.filter(|t| !t.trim().is_empty()),
        })
        .await?;
    let group = result
        .group
        .ok_or_else(|| ApiError::from(BackendError::internal("group was not created")))?;
    Ok(Json(json!({
        "ok": true,
        "chat_guid": group.chat_guid,
        "guid": result.guid,
        "group": group,
    })))
}

async fn rename_group(
    State(app): State<Arc<AppState>>,
    req: Result<Json<RenameBody>, JsonRejection>,
) -> Result<Json<Value>, ApiError> {
    let req = body(req)?;
    let chat_guid = require_chat_guid(&req.chat_guid)?;
    if req.name.trim().is_empty() {
        return Err(bad("'name' must not be empty"));
    }
    require_registered(&app)?;
    let result = app
        .backend
        .group_op(GroupOp::Rename {
            chat_guid,
            name: req.name.trim().to_string(),
        })
        .await?;
    Ok(Json(json!({ "ok": true, "group": result.group })))
}

async fn change_members(
    State(app): State<Arc<AppState>>,
    req: Result<Json<MembersBody>, JsonRejection>,
) -> Result<Json<Value>, ApiError> {
    let req = body(req)?;
    let chat_guid = require_chat_guid(&req.chat_guid)?;
    let add = clean_handles(&req.add);
    let remove = clean_handles(&req.remove);
    if add.is_empty() && remove.is_empty() {
        return Err(bad("'add' and 'remove' are both empty"));
    }
    require_registered(&app)?;
    let result = app
        .backend
        .group_op(GroupOp::Members {
            chat_guid,
            add,
            remove,
        })
        .await?;
    Ok(Json(json!({ "ok": true, "group": result.group })))
}

async fn leave_group(
    State(app): State<Arc<AppState>>,
    req: Result<Json<LeaveBody>, JsonRejection>,
) -> Result<Json<Value>, ApiError> {
    let req = body(req)?;
    let chat_guid = require_chat_guid(&req.chat_guid)?;
    require_registered(&app)?;
    app.backend.group_op(GroupOp::Leave { chat_guid }).await?;
    Ok(Json(json!({ "ok": true })))
}

/// Routes to `merge` into the main router (before `with_state`).
pub fn routes() -> Router<Arc<AppState>> {
    Router::new()
        .route("/im/groups", get(list_groups))
        .route("/im/group", post(create_group))
        .route("/im/group/rename", post(rename_group))
        .route("/im/group/members", post(change_members))
        .route("/im/group/leave", post(leave_group))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn clean_handles_strips_prefixes_and_dedups() {
        assert_eq!(
            clean_handles(&v(&["tel:+1555", " +1555 ", "mailto:a@b", "", "a@b", "+1777"])),
            v(&["+1555", "a@b", "+1777"])
        );
    }

    #[test]
    fn create_needs_two_others_and_something_to_send() {
        let name = Some("Crew".to_string());
        assert!(validate_create(&v(&["+1"]), &name, &None).is_err());
        assert!(validate_create(&v(&["+1", "tel:+1"]), &name, &None).is_err());
        assert!(validate_create(&v(&["+1", "+2"]), &None, &None).is_err());
        assert!(validate_create(&v(&["+1", "+2"]), &Some("  ".into()), &Some(" ".into())).is_err());
        assert!(validate_create(&v(&["+1", "+2"]), &name, &None).is_ok());
        assert!(validate_create(&v(&["+1", "+2"]), &None, &Some("hi".into())).is_ok());
    }

    #[test]
    fn group_event_shape() {
        let ev = GroupEvent {
            guid: "G".into(),
            chat_guid: "C".into(),
            kind: "rename".into(),
            from: "+1".into(),
            name: Some("N".into()),
            participants: v(&["+1", "+2"]),
            added: vec![],
            removed: vec![],
            timestamp: 5,
        };
        let value = serde_json::to_value(crate::events::Event::Group(ev)).unwrap();
        assert_eq!(value["type"], "group");
        assert_eq!(value["kind"], "rename");
        assert_eq!(value["name"], "N");
        assert_eq!(value["participants"][1], "+2");
    }
}
