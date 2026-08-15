# Slack Multi-Agent Workbench MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a Windows-local React browser workbench where a user collaborates with the seven existing investment-research Agents through Slack-style channels, threads, tasks, artifacts, review loops, evidence, and Markdown reports.

**Architecture:** Add a typed React/Vite application at `frontend/workbench` and build it into the existing workbench static root. Extend `CollaborationStore` and `workbench_server` with stable REST/SSE contracts while preserving the current CrewAI/OpenHarness execution path, SQLite records, and report fallback behavior.

**Tech Stack:** Python 3.10+, SQLite, existing `ThreadingHTTPServer`, CrewAI/OpenHarness, React 19, TypeScript 5.8, Vite 6, Vitest, Testing Library, SSE, CSS.

## Global Constraints

- Run locally on Windows without Docker.
- Keep the data model generic; connect only the seven built-in investment-research Agents in MVP.
- Preserve existing real Agent execution, evidence, review, and fallback-report paths.
- API keys and tokens stay backend-only and never appear in REST responses, SSE payloads, browser state, or logs.
- Do not expose model hidden reasoning; expose only task, tool, source, artifact, status, and error records.
- A transient model/network error gets one retry; a malformed or empty Agent response gets one repair attempt.
- SSE reconnects from a monotonic event cursor and deduplicates by `event_id`.
- Task status is backend-owned: `queued`, `running`, `review`, `rework`, `completed`, `failed`, or `cancelled`.
- Direct messages provide chat only; they do not expose channel task or file controls.
- P1/P2 features remain out of scope: multi-user auth, custom Agent creation, external Agent Gateway, Skill creation/import, and Word export.

---

## File Structure

### Backend

- Modify `src/openharness/invest_research/collaboration_store.py`: schema migrations and persistence methods for channels, message revisions, review issues, read state, search, and artifact versions.
- Create `src/openharness/invest_research/workbench_api.py`: JSON-safe DTO builders, validation, search aggregation, and secret redaction.
- Modify `src/openharness/invest_research/workbench_server.py`: route REST commands through the API module, serve Vite assets, and preserve current run/SSE behavior.
- Modify `run-investment-research.ps1`: build/install guidance and workbench start behavior.

### React frontend

- Create `frontend/workbench/package.json`, TypeScript/Vite/Vitest configuration, and `src/main.tsx`.
- Create `frontend/workbench/src/domain.ts`: shared frontend DTO types and labels.
- Create `frontend/workbench/src/api/client.ts`: REST calls and normalized `ApiError`.
- Create `frontend/workbench/src/api/events.ts`: SSE cursor, reconnect, deduplication, and event callbacks.
- Create `frontend/workbench/src/state/workbench.tsx`: reducer/provider that owns workspace state.
- Create focused components under `frontend/workbench/src/components/` and views under `frontend/workbench/src/views/`.
- Create `frontend/workbench/src/styles/`: tokens, shell, components, and responsive behavior.
- Build output into `.openharness/plugins/investment-research/workbench/`; replace the hand-written `app.js` and `styles.css` with generated assets.

### Tests

- Modify `tests/invest_research/test_collaboration_store.py`.
- Create `tests/invest_research/test_workbench_api.py`.
- Modify `tests/invest_research/test_workbench_realtime.py`.
- Create frontend tests beside components as `*.test.ts(x)`.
- Create `frontend/workbench/src/test/fixtures.ts` for stable realistic DTOs.
- Create `tests/invest_research/test_workbench_browser_smoke.py` for built-asset/API smoke coverage.

---

### Task 1: Extend SQLite collaboration records

**Files:**
- Modify: `src/openharness/invest_research/collaboration_store.py`
- Modify: `tests/invest_research/test_collaboration_store.py`

**Interfaces:**
- Produces: `create_channel(name, project_company=None) -> dict[str, Any]`
- Produces: `update_message(message_id, *, body=None, withdrawn=None) -> dict[str, Any] | None`
- Produces: `mark_channel_read(channel_id, user_id, event_seq) -> dict[str, Any]`
- Produces: `create_review_issue(...)`, `update_review_issue(...)`, `list_review_issues(...)`
- Produces: `search(query, *, object_type=None, channel_id=None, limit=50) -> list[dict[str, Any]]`
- Consumed by: Task 2 REST API and Tasks 5-7 UI.

- [ ] **Step 1: Write failing schema and behavior tests**

```python
def test_channel_message_revision_review_and_search(workspace_tmp: Path) -> None:
    store = CollaborationStore(workspace_tmp)
    channel = store.create_channel("宁德时代研究", project_company="宁德时代")
    message = store.add_message(
        channel_id=channel["channel_id"], author_id="owner", author_type="human",
        message_kind="user_message", body="请检查现金流", mentions=["fundamental"],
    )
    edited = store.update_message(message["message_id"], body="请检查经营现金流")
    issue = store.create_review_issue(
        channel_id=channel["channel_id"], task_id="TASK-1", artifact_id="ART-1",
        created_by="reviewer_arbiter", assignee_id="fundamental",
        severity="warning", body="现金流来源需要补充", status="open",
    )
    assert edited and edited["edited_at"]
    assert edited["revisions"][0]["body"] == "请检查现金流"
    assert store.list_review_issues(channel["channel_id"]) == [issue]
    assert {item["object_type"] for item in store.search("现金流")} == {"message", "review_issue"}
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `uv run pytest tests/invest_research/test_collaboration_store.py -q`

Expected: FAIL because the new methods and tables do not exist.

- [ ] **Step 3: Add idempotent migrations and decoding helpers**

Add columns/tables with migration checks based on `PRAGMA table_info`, rather than assuming a fresh database:

```python
CREATE TABLE IF NOT EXISTS workbench_message_revisions (
    revision_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workbench_review_issues (
    issue_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, task_id TEXT,
    artifact_id TEXT, created_by TEXT NOT NULL, assignee_id TEXT NOT NULL,
    severity TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
    metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workbench_read_state (
    channel_id TEXT NOT NULL, user_id TEXT NOT NULL, event_seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY(channel_id, user_id)
);
```

Add `edited_at` and `withdrawn_at` to messages, and `version` to artifacts. Validate task status against the global status set before writing.

- [ ] **Step 4: Implement store methods and deterministic search results**

Search only user-visible text columns. Return `{object_type, object_id, channel_id, title, excerpt, created_at}` and never raw `metadata_json`.

- [ ] **Step 5: Run store tests**

Run: `uv run pytest tests/invest_research/test_collaboration_store.py -q`

Expected: all tests PASS, including opening an existing pre-migration database twice.

- [ ] **Step 6: Commit**

```bash
git add src/openharness/invest_research/collaboration_store.py tests/invest_research/test_collaboration_store.py
git commit -m "feat(workbench): extend collaboration records"
```

---

### Task 2: Define safe REST and SSE contracts

**Files:**
- Create: `src/openharness/invest_research/workbench_api.py`
- Create: `tests/invest_research/test_workbench_api.py`
- Modify: `src/openharness/invest_research/workbench_server.py`
- Modify: `tests/invest_research/test_workbench_realtime.py`

**Interfaces:**
- Produces: `WorkbenchAPI` with `workspace`, `create_channel`, `messages`, `edit_message`, `withdraw_message`, `mark_channel_read`, `thread`, `agents`, `tasks`, `artifacts`, `review_issues`, `search`, `report` methods.
- Produces HTTP routes:
  - `GET/POST /api/channels`
  - `PATCH /api/channels/{channel_id}/read`
  - `GET/POST /api/channels/{channel_id}/messages`
  - `PATCH/DELETE /api/messages/{message_id}`
  - `GET /api/threads/{message_id}`
  - `GET /api/agents`, `/api/tasks`, `/api/artifacts`, `/api/review-issues`
  - `POST /api/review-issues`, `PATCH /api/review-issues/{issue_id}`
  - `GET /api/search?q=&type=&channel_id=`
  - existing run, pause, resume, report, and SSE routes.
- Consumed by: all frontend tasks.

- [ ] **Step 1: Write failing API contract tests**

```python
def test_workspace_payload_is_generic_and_secret_safe(api: WorkbenchAPI) -> None:
    payload = api.workspace(channel_id="research-room", user_id="owner")
    assert payload["workspace"]["workspace_id"] == "default"
    assert payload["selected_channel_id"] == "research-room"
    assert payload["event_cursor"] >= 0
    serialized = json.dumps(payload).lower()
    assert "api_key" not in serialized
    assert "hidden_reasoning" not in serialized
    assert all("unread_count" in channel for channel in payload["channels"])

def test_search_requires_non_blank_query(api: WorkbenchAPI) -> None:
    with pytest.raises(WorkbenchAPIError, match="query is required"):
        api.search(query="   ")
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `uv run pytest tests/invest_research/test_workbench_api.py tests/invest_research/test_workbench_realtime.py -q`

Expected: FAIL because `workbench_api` and routes are absent.

- [ ] **Step 3: Implement DTO builders and redaction**

Use an allowlist serializer:

```python
def public_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    return {
        "event_seq": int(event["event_seq"]),
        "event_id": str(event["event_id"]),
        "channel_id": str(event["channel_id"]),
        "event_type": str(event["event_type"]),
        "created_at": str(event["created_at"]),
        "payload": {key: payload[key] for key in PUBLIC_EVENT_KEYS if key in payload},
    }
```

Errors use `{error: {code, message, details}}`; never return Python exception reprs or file paths to the browser.

- [ ] **Step 4: Route GET/POST/PATCH/DELETE through `WorkbenchAPI`**

Add `do_PATCH` and `do_DELETE`, validate path IDs, return `201` for creates, `200` for updates, `204` for withdrawal, `400` for validation, and `404` for missing records.

- [ ] **Step 5: Preserve and harden SSE cursor behavior**

Emit only `public_event(event)`, keep default SSE message events, send `id: event_seq`, heartbeat every five seconds, and replay rows where `event_seq > after`.

- [ ] **Step 6: Run backend contract tests**

Run: `uv run pytest tests/invest_research/test_workbench_api.py tests/invest_research/test_workbench_realtime.py tests/invest_research/test_workbench_agent_tasks.py -q`

Expected: all tests PASS and existing direct-Agent behavior remains unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/openharness/invest_research/workbench_api.py src/openharness/invest_research/workbench_server.py tests/invest_research/test_workbench_api.py tests/invest_research/test_workbench_realtime.py
git commit -m "feat(workbench): add safe collaboration API"
```

---

### Task 3: Scaffold typed React app and live state layer

**Files:**
- Create: `frontend/workbench/package.json`
- Create: `frontend/workbench/package-lock.json`
- Create: `frontend/workbench/index.html`
- Create: `frontend/workbench/tsconfig.json`
- Create: `frontend/workbench/tsconfig.app.json`
- Create: `frontend/workbench/vite.config.ts`
- Create: `frontend/workbench/vitest.config.ts`
- Create: `frontend/workbench/src/main.tsx`
- Create: `frontend/workbench/src/domain.ts`
- Create: `frontend/workbench/src/api/client.ts`
- Create: `frontend/workbench/src/api/client.test.ts`
- Create: `frontend/workbench/src/api/events.ts`
- Create: `frontend/workbench/src/api/events.test.ts`
- Create: `frontend/workbench/src/state/workbench.tsx`
- Create: `frontend/workbench/src/state/workbench.test.tsx`
- Create: `frontend/workbench/src/test/setup.ts`
- Create: `frontend/workbench/src/test/fixtures.ts`

**Interfaces:**
- Produces: `WorkbenchClient` methods matching Task 2 endpoints.
- Produces: `connectWorkbenchEvents({channelId, after, onEvent, onState}) => () => void`.
- Produces: `WorkbenchProvider`, `useWorkbench()`, and reducer actions `snapshotLoaded`, `eventReceived`, `channelSelected`, `drawerOpened`, `drawerClosed`.
- Consumed by: Tasks 4-7 components.

- [ ] **Step 1: Create package configuration and install locked dependencies**

Use scripts:

```json
{
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "test": "vitest run",
    "test:watch": "vitest"
  }
}
```

Dependencies: `react`, `react-dom`; dev dependencies: `@vitejs/plugin-react`, `vite`, `typescript`, `vitest`, `jsdom`, `@testing-library/react`, `@testing-library/user-event`, `@testing-library/jest-dom`, React type packages.

- [ ] **Step 2: Write failing API and SSE tests**

```ts
it("deduplicates replayed SSE events and advances the cursor", () => {
  const state = reduceEvents(emptyState, [event(3, "A"), event(3, "A"), event(4, "B")]);
  expect(state.eventCursor).toBe(4);
  expect(state.seenEventIds).toEqual(new Set(["A", "B"]));
});
```

- [ ] **Step 3: Run tests and confirm failure**

Run: `npm.cmd test -- --run` from `frontend/workbench`.

Expected: FAIL because clients and reducer are missing.

- [ ] **Step 4: Implement domain DTOs, API errors, SSE reconnect, and reducer**

`ApiError` exposes `status`, `code`, and safe `message`. SSE uses exponential retry capped at 10 seconds, recreates `EventSource` with the latest cursor, and ignores seen `event_id` values.

- [ ] **Step 5: Run tests and production build**

Run: `npm.cmd test` and `npm.cmd run build` from `frontend/workbench`.

Expected: tests PASS; Vite writes hashed assets and `index.html` to the configured workbench output directory.

- [ ] **Step 6: Commit**

```bash
git add frontend/workbench
git commit -m "feat(workbench): scaffold React client and live state"
```

---

### Task 4: Build Slack-style shell and channel navigation

**Files:**
- Create: `frontend/workbench/src/App.tsx`
- Create: `frontend/workbench/src/App.test.tsx`
- Create: `frontend/workbench/src/components/AppShell.tsx`
- Create: `frontend/workbench/src/components/WorkspaceRail.tsx`
- Create: `frontend/workbench/src/components/ConversationSidebar.tsx`
- Create: `frontend/workbench/src/components/ChannelHeader.tsx`
- Create: `frontend/workbench/src/components/ConnectionBanner.tsx`
- Create: `frontend/workbench/src/styles/tokens.css`
- Create: `frontend/workbench/src/styles/shell.css`
- Create: `frontend/workbench/src/styles/responsive.css`

**Interfaces:**
- Consumes: `useWorkbench`, `client.createChannel`, run pause/resume methods.
- Produces: routes/views `collaboration`, `members`, `tasks`, `search`; selected channel and direct-message state.
- Produces CSS tokens for color, spacing, typography, borders, status, and focus rings.

- [ ] **Step 1: Write failing shell interaction tests**

```tsx
it("switches channels and hides task controls in a direct message", async () => {
  render(<TestApp snapshot={workspaceFixture} />);
  await user.click(screen.getByRole("button", {name: /宁德时代研究/}));
  expect(screen.getByRole("heading", {name: /宁德时代研究室/})).toBeVisible();
  await user.click(screen.getByRole("button", {name: /Planner 私信/}));
  expect(screen.queryByRole("button", {name: /暂停项目/})).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run the test and confirm failure**

Run: `npm.cmd test -- App.test.tsx` from `frontend/workbench`.

Expected: FAIL because the shell is absent.

- [ ] **Step 3: Implement the four-column desktop shell**

Use the approved layout: dark 54px rail, light 190px conversation sidebar, flexible conversation main, and conditional 320px context drawer. Use yellow only for active navigation and important task accents, not as a full-page background.

- [ ] **Step 4: Add responsive behavior and accessible controls**

At widths below 900px, overlay the sidebar and drawer rather than shrinking the message column below 360px. Add visible keyboard focus, `aria-current`, `aria-expanded`, and status text in addition to colored dots.

- [ ] **Step 5: Run component tests and build**

Run: `npm.cmd test` and `npm.cmd run build`.

Expected: PASS; no TypeScript errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/workbench/src
git commit -m "feat(workbench): add Slack-style application shell"
```

---

### Task 5: Implement messages, mentions, attachments, and threads

**Files:**
- Create: `frontend/workbench/src/views/ConversationView.tsx`
- Create: `frontend/workbench/src/views/ConversationView.test.tsx`
- Create: `frontend/workbench/src/components/MessageTimeline.tsx`
- Create: `frontend/workbench/src/components/MessageRow.tsx`
- Create: `frontend/workbench/src/components/Composer.tsx`
- Create: `frontend/workbench/src/components/MentionMenu.tsx`
- Create: `frontend/workbench/src/components/ThreadPanel.tsx`
- Create: `frontend/workbench/src/components/AttachmentList.tsx`
- Modify: `src/openharness/invest_research/workbench_api.py`
- Modify: `src/openharness/invest_research/workbench_server.py`
- Modify: `tests/invest_research/test_workbench_api.py`

**Interfaces:**
- Consumes: Task 2 messages/thread API and Task 3 state/client.
- Produces: ordinary message send, multi-Agent mention payload, message edit/withdraw, thread replies, optimistic send states, and attachment metadata.
- Adds: `POST /api/uploads` using multipart content, maximum 20 MiB per file, safe filename normalization, and storage below `.openharness/data/uploads/{channel_id}/`.

- [ ] **Step 1: Write failing composer and thread tests**

```tsx
it("sends two selected Agent mentions without starting every Agent", async () => {
  render(<ConversationHarness />);
  await user.type(screen.getByRole("textbox"), "@Fundamental @Risk 补充分析");
  await user.click(screen.getByRole("button", {name: "发送"}));
  expect(api.sendMessage).toHaveBeenCalledWith("research-room", expect.objectContaining({
    mentions: ["fundamental", "risk"],
  }));
});
```

- [ ] **Step 2: Write failing upload safety tests**

Assert traversal filenames are normalized, over-limit files return `413`, disallowed channel IDs return `404`, and responses contain metadata but not host absolute paths.

- [ ] **Step 3: Run focused frontend/backend tests and confirm failure**

Run: `npm.cmd test -- ConversationView.test.tsx` and `uv run pytest tests/invest_research/test_workbench_api.py -q`.

- [ ] **Step 4: Implement timeline, composer, mention selection, send states, and thread drawer**

Message state is `sending`, `sent`, `agent_processing`, `completed`, or `failed`. Thread replies reuse the same composer but include `thread_id`; mentioning Planner inside a thread must not restart the full research flow.

- [ ] **Step 5: Implement safe attachment upload and metadata rendering**

Allow PDF, Markdown, text, CSV, XLSX, DOCX, PNG, JPEG, and WebP by MIME/extension allowlist. Store a generated ID and sanitized name. Agent file access remains controlled by existing task/tool permissions.

- [ ] **Step 6: Run tests and build**

Run: `npm.cmd test`, `npm.cmd run build`, and `uv run pytest tests/invest_research/test_workbench_api.py tests/invest_research/test_workbench_realtime.py -q`.

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/workbench/src src/openharness/invest_research/workbench_api.py src/openharness/invest_research/workbench_server.py tests/invest_research/test_workbench_api.py
git commit -m "feat(workbench): add conversations threads and uploads"
```

---

### Task 6: Render tasks, artifacts, reviews, evidence, and reports

**Files:**
- Create: `frontend/workbench/src/components/TaskCard.tsx`
- Create: `frontend/workbench/src/components/ArtifactCard.tsx`
- Create: `frontend/workbench/src/components/ReviewCard.tsx`
- Create: `frontend/workbench/src/components/RunEventRow.tsx`
- Create: `frontend/workbench/src/components/ContextDrawer.tsx`
- Create: `frontend/workbench/src/panels/TaskPanel.tsx`
- Create: `frontend/workbench/src/panels/ArtifactPanel.tsx`
- Create: `frontend/workbench/src/panels/EvidencePanel.tsx`
- Create: `frontend/workbench/src/panels/ReportPanel.tsx`
- Create: `frontend/workbench/src/components/StructuredCards.test.tsx`
- Modify: `src/openharness/invest_research/workbench_api.py`
- Modify: `src/openharness/invest_research/workbench_server.py`
- Modify: `tests/invest_research/test_workbench_api.py`

**Interfaces:**
- Consumes: tasks, artifacts, review issues, evidence store, report endpoint, and run events.
- Produces: linked drawer navigation `openDrawer({kind, id})` and targeted rework command.
- Adds: `GET /api/evidence/{ref_id}` returning only current-run lineage, and `POST /api/review-issues/{issue_id}/rework` creating a directed task.

- [ ] **Step 1: Write failing structured-card and lineage tests**

```tsx
it("opens evidence lineage from an artifact without showing hidden reasoning", async () => {
  render(<ArtifactCard artifact={artifactFixture} />);
  await user.click(screen.getByRole("button", {name: /查看证据/}));
  expect(screen.getByText("S-001")).toBeVisible();
  expect(screen.queryByText(/chain of thought|hidden reasoning/i)).not.toBeInTheDocument();
});
```

Backend tests must reject evidence from another run/channel and verify that rework creates exactly one task for the issue assignee.

- [ ] **Step 2: Run focused tests and confirm failure**

Run frontend structured-card tests and `uv run pytest tests/invest_research/test_workbench_api.py -q`.

- [ ] **Step 3: Implement typed message renderers and drawer panels**

Map message kinds to components without conditional HTML string construction. Unknown kinds fall back to a safe plain-message renderer.

- [ ] **Step 4: Implement evidence lineage and rework API**

Return `{reference, source, fact, logic, review, report_sections}` with absent nodes as `null`. Check run, task, channel, and file permissions before returning data.

- [ ] **Step 5: Implement Markdown report preview and download**

Render Markdown as sanitized React elements; do not use unsanitized `dangerouslySetInnerHTML`. Label fallback reports prominently from `fallback_used`.

- [ ] **Step 6: Run tests and build**

Run all frontend tests, build, and focused backend tests.

Expected: PASS; formal and fallback reports are visually distinguishable.

- [ ] **Step 7: Commit**

```bash
git add frontend/workbench/src src/openharness/invest_research/workbench_api.py src/openharness/invest_research/workbench_server.py tests/invest_research/test_workbench_api.py
git commit -m "feat(workbench): add traceable delivery and review UI"
```

---

### Task 7: Add members graph, tasks view, and unified search

**Files:**
- Create: `frontend/workbench/src/views/MembersView.tsx`
- Create: `frontend/workbench/src/views/MembersView.test.tsx`
- Create: `frontend/workbench/src/components/AgentGraph.tsx`
- Create: `frontend/workbench/src/panels/AgentPanel.tsx`
- Create: `frontend/workbench/src/views/TasksView.tsx`
- Create: `frontend/workbench/src/views/TasksView.test.tsx`
- Create: `frontend/workbench/src/views/SearchView.tsx`
- Create: `frontend/workbench/src/views/SearchView.test.tsx`
- Modify: `src/openharness/invest_research/workbench_api.py`
- Modify: `tests/invest_research/test_workbench_api.py`

**Interfaces:**
- Consumes: Agent roster, tasks, search records, message mentions, and handoff events.
- Produces: Agent relationship edges `{source, target, weight, last_interaction_at}` from `GET /api/relationships?channel_id=`.
- Produces: task filters and search result navigation `{view, channelId, threadId?, drawer?}`.

- [ ] **Step 1: Write failing members/tasks/search tests**

```tsx
it("uses actual mentions and handoffs to build relationship edges", () => {
  render(<MembersView agents={agents} relationships={[{source:"planner", target:"risk", weight:3}]} />);
  expect(screen.getByLabelText("Planner 到 Risk，3 次协作")).toBeVisible();
});
```

Test Agent tabs `资料`, `动态`, `聊天`, `Skills`; ensure Skills is read-only. Test task status filters and a search result jump back to its channel/thread.

- [ ] **Step 2: Run tests and confirm failure**

Run: `npm.cmd test -- MembersView.test.tsx TasksView.test.tsx SearchView.test.tsx`.

- [ ] **Step 3: Add relationship projection to the API**

Aggregate message mentions plus `task_handoff` events. Do not create edges from static workflow definitions alone.

- [ ] **Step 4: Implement accessible SVG graph and Agent detail tabs**

Provide a textual edge summary below the SVG, keyboard-selectable Agent nodes, Agent count, edge count, and most-connected Agent.

- [ ] **Step 5: Implement tasks and search views**

Use URL-independent internal navigation state so the local server does not require SPA fallback routing. Debounce search by 250ms and abort stale requests.

- [ ] **Step 6: Run tests and build**

Run all frontend tests/build and API tests.

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/workbench/src src/openharness/invest_research/workbench_api.py tests/invest_research/test_workbench_api.py
git commit -m "feat(workbench): add members tasks and search views"
```

---

### Task 8: Integrate production assets and verify the complete MVP

**Files:**
- Modify: `frontend/workbench/vite.config.ts`
- Modify: `.openharness/plugins/investment-research/workbench/index.html`
- Delete: `.openharness/plugins/investment-research/workbench/app.js`
- Delete: `.openharness/plugins/investment-research/workbench/styles.css`
- Create/Update: `.openharness/plugins/investment-research/workbench/assets/*`
- Modify: `src/openharness/invest_research/workbench_server.py`
- Modify: `run-investment-research.ps1`
- Modify: `.openharness/plugins/investment-research/README.md`
- Create: `tests/invest_research/test_workbench_browser_smoke.py`

**Interfaces:**
- Produces: one command, `.\run-investment-research.ps1 -Workbench`, serving the React build at `http://127.0.0.1:8787/`.
- Preserves: all existing PlannerWeb, smoke, validation, direct-Agent, full-chain, and report commands.

- [ ] **Step 1: Write failing built-asset smoke tests**

```python
def test_workbench_index_references_hashed_react_assets() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="root"' in html
    assert re.search(r'/assets/index-[^" ]+\.js', html)
    assert "app.js" not in html
```

Add an HTTP smoke test that starts `build_server(0)`, requests `/`, a JS asset, `/api/workspace`, and a replayed SSE event, then closes the server.

- [ ] **Step 2: Run smoke tests and confirm failure**

Run: `uv run pytest tests/invest_research/test_workbench_browser_smoke.py -q`.

Expected: FAIL while the legacy static page is present.

- [ ] **Step 3: Configure deterministic production output**

Vite uses `base: "/"`, outputs to the existing `WEB_ROOT`, clears only generated web assets, and preserves no runtime data. Add `frontend/workbench/node_modules/` to `.gitignore`.

- [ ] **Step 4: Build assets and update startup documentation**

Run `npm.cmd ci`, `npm.cmd test`, and `npm.cmd run build` in `frontend/workbench`. Document first-time install, rebuild, start, test, keys, local database, and report paths.

- [ ] **Step 5: Run complete automated verification**

Run:

```powershell
uv run pytest tests/invest_research -q
uv run pytest tests/test_api tests/test_tools -q
Set-Location frontend/workbench
npm.cmd test
npm.cmd run build
```

Expected: zero failures; TypeScript and Vite exit `0`.

- [ ] **Step 6: Run browser acceptance against the local server**

Start `.\run-investment-research.ps1 -Workbench -Port 8787` and verify:

1. create/switch channel and send an ordinary message;
2. `@Fundamental` creates one direct task;
3. `@Fundamental @Risk` creates two tasks without starting unrelated Agents;
4. `@Planner` starts the full real flow;
5. SSE updates task/Agent/tool state without manual refresh;
6. thread reply and one directed rework succeed;
7. task, artifact, evidence, and report links navigate correctly;
8. Markdown report previews and downloads, with fallback labeling when applicable;
9. refreshing restores recorded state;
10. responses/events/logs contain no API key or hidden reasoning.

- [ ] **Step 7: Inspect repository state and commit**

```bash
git status --short
git add frontend/workbench .openharness/plugins/investment-research/workbench src/openharness/invest_research/workbench_server.py run-investment-research.ps1 .openharness/plugins/investment-research/README.md tests/invest_research/test_workbench_browser_smoke.py .gitignore
git commit -m "feat(workbench): deliver React multi-agent MVP"
```

---

## Plan Self-Review Checklist

- Every MVP requirement in the approved design is assigned to Tasks 1-8.
- Backend contracts precede frontend consumers.
- Every task begins with a failing test and ends with focused verification and a commit.
- The existing Agent runtime and orchestration remain in place; no task rewrites them.
- P1/P2 capabilities are not exposed as nonfunctional controls.
- Security checks cover REST, SSE, uploads, evidence authorization, logs, and Markdown rendering.
- The final acceptance explicitly covers all 10 PRD MVP criteria.
