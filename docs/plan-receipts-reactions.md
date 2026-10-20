# Plan: delivery/read receipts, reactions and stickers

Status 2026-10-20: **design only, no code written.** Findings are from reading rustpush and this repo.
The server (`message_event()` in `crates/imessage-server-rustpush/src/backend.rs`) currently surfaces only
text; receipts, typing, profile updates and errors are dropped. Stickers/attachments arrive as a U+FFFC
placeholder in the text.

## What rustpush gives us (`src/imessage/messages.rs`)

- `Message::Delivered` / `Message::Read`: unit variants. `inst.id` is the guid of the receipted message,
  `inst.sender` is the peer. To SEND a read receipt: build a `MessageInst` with `Message::Read` and set
  `inst.id` to the inbound guid.
- `Message::React(ReactMessage { to_uuid, to_part, reaction, to_text })`, with
  `Reaction::{Heart, Like, Dislike, Laugh, Emphasize, Question, Emoji, Sticker}`.
- Attachments: `MessagePart::Attachment` with `name`, `mime`, `uti_type`; a sticker has
  `ext: Some(PartExtension::Sticker)`.
- `Message::Error(ErrorMessage { for_uuid, status, status_str })` (IDS command 120): see
  `docs/doctor-log.md` in the cluster repo; being handled on `feat/ids-errors`.

## Server changes (additive only; the API shape is frozen for existing events)

1. New SSE event types in `crates/imessage-server/src/events.rs`: `delivered`, `read`, `reaction`,
   `attachment`. `message` stays byte-identical.
2. `Backend::send_read`, `Backend::send_react` (default "unavailable").
3. `POST /im/read`, `POST /im/react`.
4. Pure mapping functions as NEW functions in the rustpush backend (keeps merge conflicts small).

## Bridge changes

1. `relay/xep.py` (xml.etree): XEP-0184 (delivered), XEP-0333 (displayed marker), XEP-0444 (reactions),
   plus a fallback body and XEP-0428 fallback indication for clients without XEP-0444.
2. A bounded guid <-> xmpp-id map; use the iMessage guid as the stanza id for inbound messages.
3. Additive fields on `IncomingXmpp`.
4. The component transport must stop dropping body-less stanzas (receipts and reactions have no body).
5. Config `RELAY_SEND_READ_RECEIPTS` (default on).

## Known gaps

- Attachment/sticker download needs a file host; the bridge has no web server. Inbound: forward a
  readable fallback (`[sticker: name]`). Outbound stickers/attachments (MMCS upload from a phone-only
  identity) were not investigated.
- Receipts only for messages the owner's client marked `markable` / requested a receipt for.
