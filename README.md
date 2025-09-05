# TxtMe — End‑to‑End Technical Specification (v1)

> Lightweight, text‑only, one‑to‑one messaging app built with Django + DRF + Channels + Redis.

---

## 0) Executive Summary
- **Goal:** Ship a minimal, fast, secure 1:1 text messaging app with real‑time delivery.
- **Scope:** Auth, conversations, messages, read receipts, real‑time websockets, basic presence (optional).
- **Non‑Goals (v1):** Group chats, media/attachments, E2EE, message reactions, stickers, push notifications.
- **Success Metrics:**
  - P50 message delivery < 150ms (LAN dev) / < 500ms (prod baseline).
  - 99.9% successful websocket connects (authenticated users).
  - Page/API error rate < 1%.

---

## 1) Product Requirements
### 1.1 Core Use Cases
1. Register & login.
2. Start a new 1:1 conversation.
3. Send/receive text messages in real time.
4. Browse message history (paginated).
5. See read state (read/unread) per conversation.
6. Optional: basic presence (last seen/online).

### 1.2 User Stories
- *As a user*, I can search a username and start a chat.
- *As a user*, I can send a text and the recipient sees it instantly.
- *As a user*, I can reload and still see the full history.
- *As a user*, I can see which messages have been read.

### 1.3 Constraints
- Text‑only; max message length 4,096 chars (configurable).
- One conversation per unique user pair.
- Real‑time via WebSockets; no long‑polling.

---

## 2) System Architecture
### 2.1 Components
- **Django + DRF (ASGI)** — REST APIs for auth, conversations, messages.
- **Django Channels** — WebSocket layer for real‑time messaging.
- **Redis** — Channel layer backend (routing, fan‑out, presence cache, idempotency keys, rate limits).
- **Database** — PostgreSQL (prod) / SQLite (dev) or MySQL (if preferred).
- **ASGI Server** — Daphne/Uvicorn. HTTP served via Gunicorn (ASGI worker) or Uvicorn workers.

### 2.2 High‑Level Diagram (logical)
```
[ Clients (Web/Mobile) ]
       |          \
       | REST      \
       v            v (WebSocket)
[ DRF (HTTP) ]   [ Channels Consumers ]
       |                |        
       |                | <--> [ Redis (Channel Layer) ]
       v                |
[ PostgreSQL/MySQL ] <--+
```

### 2.3 Core Flows
- **Send Message** (WS): client → Channels → DB persist → publish via Redis → push to recipient WS → ack back to sender.
- **Fetch History** (REST): client → DRF → DB (paginated) → JSON.
- **Mark Read** (WS/REST): client sends read position → update conversation state → notify other participant.

---

## 3) Data Model (Conceptual)
> Pseudotypes: `UUID`, `TEXT`, `INT`, `BOOL`, `TIMESTAMP`, `ENUM`.

### 3.1 User
- `id: UUID (pk)`
- `username: TEXT (unique, indexed)`
- `email: TEXT (unique, optional)`
- `display_name: TEXT (optional)`
- `created_at: TIMESTAMP`
- `last_seen_at: TIMESTAMP (indexed)`
- `is_active: BOOL`

**Notes/Rules**
- Reuse Django `User` or a custom user model; keep username immutable.

### 3.2 Conversation (1:1 only)
- `id: UUID (pk)`
- `user_a_id: UUID (fk User, indexed)`
- `user_b_id: UUID (fk User, indexed)`
- `created_at: TIMESTAMP`
- `last_message_id: UUID (fk Message, nullable, indexed)`

**Constraints**
- **Uniqueness:** one row per unordered pair. Enforce by storing `user_min`, `user_max` (sorted IDs) and unique index on `(user_min, user_max)`.

### 3.3 Message
- `id: UUID (pk)` (ULID allowed for time‑ordering)
- `conversation_id: UUID (fk Conversation, indexed)`
- `sender_id: UUID (fk User, indexed)`
- `recipient_id: UUID (fk User, indexed)` (denormalized for quick filters)
- `content: TEXT` (max 4096)
- `status: ENUM('persisted','dispatched','delivered','read')` (optional)
- `client_msg_id: TEXT (optional, unique per sender for idempotency)`
- `sent_at: TIMESTAMP (default now, indexed)`
- `delivered_at: TIMESTAMP (nullable)`
- `read_at: TIMESTAMP (nullable)`
- `is_deleted: BOOL (soft delete, optional)`

**Indexes**
- `(conversation_id, sent_at desc)`
- `(recipient_id, read_at nulls first)` for unread queries.

### 3.4 ConversationState (per participant)
- `id: UUID (pk)`
- `conversation_id: UUID (fk)`
- `participant_id: UUID (fk User)`
- `last_read_message_id: UUID (fk Message, nullable)`
- `unread_count: INT (cached, optional)`
- Unique index on `(conversation_id, participant_id)`.

**Rationale**: Efficient read status without updating every message.

### 3.5 Optional (Presence Cache, Redis)
- Keys: `presence:{user_id}` → `online|offline`, `last_seen_ts`.
- TTL‑based updates from WS pings.

---

## 4) API Design (HTTP, versioned `/api/v1`)
> All endpoints require `Authorization: Bearer <JWT>` unless noted.

### 4.1 Auth
- `POST /api/v1/auth/register` → Create account.
  - Request: `{ username, email?, password }`
  - 201 Response: `{ id, username, email?, created_at }`
- `POST /api/v1/auth/login` → Obtain tokens.
  - Request: `{ username, password }`
  - 200 Response: `{ access, refresh }`
- `POST /api/v1/auth/refresh` → Refresh token.

### 4.2 Users
- `GET /api/v1/users/me` → Current profile.
- `PATCH /api/v1/users/me` → Update `display_name`.
- `GET /api/v1/users/search?username=<q>` → Basic search (rate‑limited).

### 4.3 Conversations
- `GET /api/v1/conversations` → List my conversations.
  - Query: `limit`, `cursor` (opaque), `sort=updated_desc` (default)
  - 200 Example:
    ```json
    {
      "items": [
        {
          "id": "uuid",
          "peer": {"id": "uuid", "username": "sami", "display_name": "Sami"},
          "last_message": {
            "id": "uuid",
            "preview": "Hey!",
            "sent_at": "2025-09-05T10:20:30Z",
            "is_read": false
          },
          "unread_count": 2
        }
      ],
      "next_cursor": "..."
    }
    ```
- `POST /api/v1/conversations` → Start (or fetch existing) 1:1 conversation.
  - Request: `{ recipient_id }`
  - 201/200 Response: `{ id, participants: [me, recipient], created_at }`
- `GET /api/v1/conversations/{id}` → Conversation detail.

### 4.4 Messages
- `GET /api/v1/conversations/{id}/messages` → Paginated history.
  - Cursor pagination (recommended): `limit` (<=100), `before_id` or `after_id`.
  - 200 Example:
    ```json
    {
      "items": [
        {
          "id": "m_01H...",
          "sender": {"id": "uuid", "username": "yetmgeta"},
          "content": "Hello!",
          "sent_at": "2025-09-05T10:20:30Z",
          "read_at": "2025-09-05T10:22:00Z"
        }
      ],
      "has_more": true,
      "next_cursor": "..."
    }
    ```
- `POST /api/v1/conversations/{id}/messages` → Send text message.
  - Request: `{ content, client_msg_id? }`
  - 201 Response: `{ id, content, sender, sent_at, status }`
  - **Idempotency:** If header `Idempotency-Key: <client_msg_id>` is repeated, respond 200 with original message.
- `POST /api/v1/conversations/{id}/read` → Mark read up to `message_id`.
  - Request: `{ message_id }`
  - 200 Response: `{ conversation_id, last_read_message_id }`

### 4.5 Errors (Standard Envelope)
```json
{
  "error": {
    "code": "not_authorized",
    "message": "You are not a participant of this conversation.",
    "details": null
  }
}
```
- Common codes: `validation_error`, `not_found`, `not_authorized`, `rate_limited`, `conflict`, `too_long`.

---

## 5) Real‑Time (WebSocket) Design
### 5.1 Endpoint & Auth
- URL: `wss://api.txtme.com/ws/conversations/{conversation_id}`
- Auth: `?token=<JWT>` (or Subprotocol header). Reject if not a participant.
- Heartbeats: ping/pong every 30s. Idle timeout 60s.

### 5.2 Client → Server Messages (envelope)
```json
{ "type": "send_message", "data": { "content": "Hi", "client_msg_id": "abc123" } }
{ "type": "mark_read", "data": { "message_id": "m_01H..." } }
```

### 5.3 Server → Client Messages
```json
{ "type": "message_ack", "data": { "client_msg_id": "abc123", "server_id": "m_01H...", "sent_at": "..." } }
{ "type": "new_message", "data": { "id": "m_01H...", "sender_id": "u_...", "content": "Hi", "sent_at": "..." } }
{ "type": "read_update", "data": { "conversation_id": "c_...", "last_read_message_id": "m_..." } }
{ "type": "error", "data": { "code": "rate_limited", "message": "Slow down" } }
```

### 5.4 Delivery Semantics
- **At‑least‑once** over WS with **idempotency** via `client_msg_id` to appear exactly‑once to users.
- On reconnect, client requests missed messages via REST using `after_id` (source of truth).

### 5.5 Rate Limits & Quotas
- Connect: max 20 connects/min/IP.
- Send: 20 msgs/10s per user (burst 40).
- Message size: 4KB max (reject with `too_long`).

---

## 6) Security & Privacy
- **Auth:** JWT access (short‑lived) + refresh tokens. Store access in memory on web clients.
- **Transport:** HTTPS/WSS only. HSTS.
- **CORS:** Allow only approved origins.
- **AuthZ:** A user can only access conversations where they are `user_a` or `user_b`.
- **Input Sanitization:** Strip control chars; normalize whitespace; optional profanity filter.
- **Abuse Controls:** IP/user rate limits (REST + WS). Temporary blocks via Redis keys.
- **Secrets:** All secrets via env vars; no secrets in repo.
- **Data Retention:** Configurable purge policy (e.g., delete messages older than 180 days) — optional cron.

---

## 7) Performance & Scalability
- **Indexes:**
  - `message (conversation_id, sent_at DESC)` for history.
  - `conversation (user_min, user_max)` unique for pair lookup.
  - `conversation_state (conversation_id, participant_id)`.
- **Caching:**
  - Conversation list items include denormalized last_message & unread_count (maintained transactionally or via async task).
- **Redis Usage:**
  - Channel layer for fan‑out.
  - Presence: `presence:{user_id}` set on connect; TTL refresh by heartbeats.
  - Idempotency: `idemp:{sender_id}:{client_msg_id}` with short TTL (24h).
- **Backpressure:** Drop WS messages over 1MB aggregate buffer; instruct client to fallback to REST sync.

---

## 8) Observability
- **Logging:** Structured JSON logs for requests, WS events (connect, send, ack, error), DB errors.
- **Tracing:** Optional OpenTelemetry integration.
- **Metrics:**
  - WS connect success rate, active connections.
  - Msg throughput, delivery latency (persisted→dispatched→delivered).
  - Error/Rate‑limit counts.
- **Alerts:** High error rates, Redis disconnects, DB connection pool saturation.

---

## 9) Environments & Config
- **Config Vars:**
  - `DJANGO_SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `JWT_ACCESS_TTL`, `JWT_REFRESH_TTL`, `ENV`.
- **Dev:** SQLite + local Redis; debug ON.
- **Staging/Prod:** Postgres/MySQL + managed Redis (Render/Upstash/Redis Cloud); debug OFF.

---

## 10) Deployment Plan
- **Process model:**
  - `web` (ASGI, HTTP + WS), `worker` (optional Celery for housekeeping), `scheduler` (optional cron for retention).
- **ASGI Server:** Daphne/Uvicorn.
- **Static/Media:** Not applicable (text‑only) — minimal.
- **Render/VPS:** Ensure WebSocket support; health checks on `/healthz`.

---

## 11) Testing Strategy
- **Unit:** serializers, permissions, pagination, validators.
- **Integration:** DRF views (auth, conversations, messages), WS consumer with Channels test client.
- **E2E (manual/automated):** two browser sessions exchanging messages; reconnection & resume; read markers.
- **Load:** basic Locust scenario: 500 concurrent users, 1 msg/sec each for 60s.

---

## 12) Acceptance Criteria (v1)
1. Two authenticated users can create/find a conversation and exchange messages in real time.
2. History loads with cursor pagination; ordering by `sent_at` desc.
3. Read markers update and reflect correctly for both participants.
4. Unauthorized access to other users’ conversations is rejected.
5. WS reconnect + history catch‑up works (no missing or duplicated messages).

---

## 13) Project Plan & Milestones (suggested)
- **M1 (Days 1–3):** Models, migrations, DRF endpoints for auth/conversations/messages.
- **M2 (Days 4–7):** Channels + Redis, message send/receive, acks, read updates.
- **M3 (Days 8–10):** Pagination, unread counts, search, rate limits.
- **M4 (Days 11–14):** Observability, polish, staging deploy, acceptance tests.

---

## 14) API & WS Schemas (Detailed Examples)

### 14.1 Create/Get Conversation
**POST** `/api/v1/conversations`
```json
{ "recipient_id": "u_9b3..." }
```
**201**
```json
{ "id": "c_01H...", "participants": [{"id": "u_me", "username": "me"}, {"id": "u_9b3", "username": "sami"}], "created_at": "2025-09-05T10:15:00Z" }
```

### 14.2 Send Message (HTTP)
**POST** `/api/v1/conversations/{id}/messages`
Headers: `Idempotency-Key: 0f1-abc`
```json
{ "content": "What’s up?", "client_msg_id": "0f1-abc" }
```
**201**
```json
{ "id": "m_01H...", "content": "What’s up?", "sender": {"id": "u_me", "username": "me"}, "sent_at": "...", "status": "persisted" }
```

### 14.3 WebSocket Frames
**Client → Server**
```json
{ "type": "send_message", "data": { "content": "Hello", "client_msg_id": "k1" } }
```
**Server → Sender (ack)**
```json
{ "type": "message_ack", "data": { "client_msg_id": "k1", "server_id": "m_01H...", "sent_at": "..." } }
```
**Server → Recipient**
```json
{ "type": "new_message", "data": { "id": "m_01H...", "sender_id": "u_me", "content": "Hello", "sent_at": "..." } }
```

### 14.4 Mark Read
**POST** `/api/v1/conversations/{id}/read`
```json
{ "message_id": "m_01H..." }
```
**WS broadcast**
```json
{ "type": "read_update", "data": { "conversation_id": "c_...", "last_read_message_id": "m_..." } }
```

---

## 15) Directory Layout (reference)
```
backend/
  app/
    users/
    conversations/
    messages/
    realtime/ (channels consumers)
    common/
  config/ (settings, urls, asgi.py)
  tests/
  manage.py
```

---

## 16) Risks & Mitigations
- **WS scaling**: Use Redis channel layer; horizontal ASGI workers. Health checks for Redis outages.
- **Duplicate messages**: Enforce `client_msg_id` idempotency and DB unique constraint per sender.
- **Hot partitions**: Very active conversations — ensure proper composite indexes; consider ULIDs for locality.
- **Abuse/Spam**: Implement per‑user rate limits and temporary bans.

---

## 17) Backlog (Post‑v1)
- Typing indicators (WS `typing_start/stop`).
- Push notifications (FCM/APNs) when offline.
- Soft delete / delete for me.
- Message search (full‑text index).
- End‑to‑end encryption (protocol & key mgmt) — major scope.

---

**End of Spec v1**

