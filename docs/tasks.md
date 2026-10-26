# Session task ledger for Pi

A new implementation, not a fork or adaptation of pi-goal-x. The old fork was inspected for failure modes and the working background-jobs extension for session restoration, attributed notifications, and menu conventions. No goal extension source is reused. There is no goal scheduler, workspace-wide storage, migration, parent-goal completion, or automatic installation.

## Load and use

Requires **Pi 0.99.1's extension API** (`agent_before_settle`, `agent_settled`, and `ctx.modelRegistry.complete`), Node 22+, and the adjacent TypeBox dependency. Other Pi releases have not been verified.

```sh
npm ci --prefix extensions --ignore-scripts
pi --extension ./extensions/tasks.ts
```

Do not enable the old goal extension alongside this one for the same work. Merging this source does not change any running agent or remove an installed extension.

Run **`/tasks`** to open the interactive menu. It always opens a menu; there is no argument-based command interface. The menu supports:

- View task summary, acceptance, blocker or review checkpoint, submitted evidence, verdict, and completion owner.
- Add a task with optional acceptance criteria and insertion position.
- **Edit task**: change its summary and acceptance criteria in two prefilled editors. Both changes save together after the second editor; cancelling either editor discards the whole edit. IDs and list order stay unchanged. **Edit acceptance criteria** remains available for criteria-only edits.
- Remove a task, with confirmation. **Clear completed tasks** confirms the exact completed IDs, then removes only those IDs in one validated delta; open tasks and the monotonic next ID remain intact. There is no undo in `/tasks`.
- Mark complete with a confirmed, explicitly recorded **user override**. This does not fabricate a judge approval.
- Resume a blocked task.
- Mark a pending task in progress, or return it to pending. These bookkeeping actions do not wake inference or resume paused reminders.
- Pause or resume automatic reminders. Explicit **Resume reminders** resets both reminder limits; task completion and other menu edits do not reset the hard continuation cap.

TUI status labels retain their text and use distinct theme colors: pending = `accent`, progress = `mdLink` (blue in the built-in light/dark themes), blocked = `warning`, complete = `success`. A blocked review displays `[blocked]`, then `[blocked due]` on the next widget refresh after its check time. The widget, menu choices, and detail heading share this mapping; model-visible task data and Markdown snapshots contain no terminal color escapes.

Run **`/tasks-prune`** to remove completed tasks directly, without opening the menu. It uses the same exact-ID confirmation and atomic removal as **Clear completed tasks**, preserves all open tasks and the monotonic next ID, and does not start inference or reset reminder limits. It requires TUI confirmation; cancellation, changed completed IDs or session changes leave the ledger untouched. If there are no completed tasks, it reports that without prompting.

Run **`/tasks-add`** to prompt for summary, optional acceptance criteria and list position directly, without opening the menu. It requires TUI; cancellation or invalid input leaves the ledger unchanged. A successful add uses the same bounded, extension-attributed follow-up as menu add, unless paused or no pending work remains.

Task edits validate the existing summary/criteria length limits, reject stale task/session snapshots and persist one atomic task delta. Changed text clears obsolete verdict/completion ownership while preserving submitted evidence. Editing a completed task requires confirmation to reopen it as pending and respects the open-task limit; pending/progress tasks keep their status, and manual blockers remain. Existing review/job/subagent tracking detaches when its brief changes, without stopping external work; linked queued questions remain owned by the question queue. Edits neither start inference nor reset reminder limits or resume a pause. Only the user can edit through the menu; no agent edit tool is added.

Cancelling input or a confirmation leaves the task unchanged. Dialogs detect intervening task/session changes rather than overwriting them. Adding/resuming work or overriding completion queues an extension-attributed follow-up after closing the menu, unless paused or no pending work remains. Merely inspecting tasks does not start inference. The menu requires TUI; tools, context injection, judging, and reminders also work headlessly.

## Agent tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `tasks_list` | none | Ordered full ledger including evidence, verdicts, blockers and `waiting_on`, review checkpoints, due review IDs, and each task's base `status`, derived `effective_status`/`effective_waiting_on`, open `question_ids`, and `waiting_question_ids`. |
| `tasks_assign` | `task_id`, `action` (`launch`, `link`, `refresh`, `detach`), optional `agent`, `run_id` | Optional pi-subagents integration; see assignment and recovery semantics below. |
| `tasks_add` | `summary`, optional `order`, `acceptance` | Stable integer ID. `order` is a 1-based insertion position in the full list (including completed tasks); defaults to append. Acceptance defaults to summary. |
| `tasks_complete` | `task_id`, `evidence`, optional `evidence_files`, `context_messages` | Assess one claim; optionally read up to three declared workspace text files and up to three public URLs cited in the evidence before judging. Complete only on accepted verdict. Return verdict, reason, missing evidence, and remaining actionable (pending/progress) IDs. |
| `tasks_progress` | `task_id`, optional `active` (default true) | Mark pending work `progress`; `active=false` returns it to pending. Reject blocked/complete/missing tasks. Changed state cancels an in-flight assessment, preserves evidence/verdicts and does not reset reminder limits or override stops/pauses. |
| `tasks_unblock` | `task_id` | Return a blocked task to pending after its prerequisite or review is resolved. Clear the blocker, `waiting_on`, review marker and any background-job dependency; preserve evidence/verdicts and cancel an in-flight assessment for that task. Reject missing, pending or complete tasks. Does not start inference or override stop/pause/reminder limits. |
| `tasks_block` | `task_id`, `reason`, optional `waiting_on`, `background_job_id` | Use when a missing prerequisite or needed decision prevents the next authorized step; name the exact need and, when known, who or what can resolve it. Suppresses reminders for that task; does not complete/remove it. Replaces an earlier review marker. |
| `tasks_review` | `task_id`, `reviewer`, `artifact`, `check_at` | Mark an external review as the next dependency. Stores a blocked task with `waiting_on` = reviewer, artifact and ISO date-time with timezone; shows when the check is due. Refuses a different existing blocker rather than overwriting it. No model call, contact, or automatic wake. |

A linked `queue_question(task_id)` checks current open IDs through the versioned `tasks:lookup-v1` Pi event-bus channel. Its task-side listener must remain **synchronous**: the question tool reads the lookup result immediately after `emit` returns. The queue owns unanswered links and answers `questions:lookup-v1` synchronously; `questions:changed-v1` refreshes the task widget without passing another extension's context. Every linked open question appears in `question_ids` but stays **non-blocking by default**. Set `wait_for_answer: true` only when the task cannot proceed without the answer; the queue then supplies `waiting_question_ids`, and the ledger derives `effective_status: "question"` while retaining the persisted base `status`. Only this explicit waiting state suppresses the task's automatic reminders, including after branch restoration. It derives `effective_waiting_on: "user"`; an optional linked question leaves the task unblocked and does not assign an owner. Answering or clearing removes the overlay; it does not complete, unblock, or reopen the task. An answer's removal is persisted **before** Pi's fire-and-forget steering call, so an idle answer turn reads the cleared state; a synchronous send refusal restores the question. Asynchronous delivery failures cannot be observed by this queue and may not surface through Pi's fire-and-forget extension API; a crash after removal is persisted but before steering can lose the answer. A linked `progress` task still holds the one progress slot; return it to pending explicitly before progressing another task. Neither queueing nor clearing starts a provider call.

There is **no agent removal or override tool**. `tasks_summary(prompt?)` is advisory only; see the sharing commands below. Ordinary CI, review or optional quick-question waits with independent work need no block: leave the task pending or in progress and use the appropriate wait/read channel. When an external review is the next dependency, `tasks_review` marks it blocked **with** `waiting_on` = reviewer, artifact and next-check time rather than suppressing it as an untracked generic blocker. `waiting_on` identifies the person or system whose action can resolve a blocker; it is not an approval requirement or permission to contact them. It appears in injected task context and can reach the selected model, so apply the usual disclosure rules before recording private identities. Block only when a genuine missing prerequisite or needed decision prevents the next authorized step; say what must happen to resume. A failed CI check may become a defect to fix, not a reason to mark the task blocked by default. `progress` is an explicit bookkeeping state, not an automatic activity detector or completion claim. Pending and progress tasks without an explicit waiting question are eligible for the same bounded reminders; old pending/blocked/complete snapshots restore unchanged. A version without progress support will refuse a newer progress snapshot rather than silently discard it. A blocked task can be submitted for assessment once resolved; the agent can return it to pending with `tasks_unblock`, or a user can resume it from the menu. Blocking all open tasks stops automatic reminders while retaining the work and reasons. Review checkpoints appear in `tasks_list`, model context and the TUI widget; due reviews are highlighted on the **next** turn. This does **not** schedule a provider call or guarantee an unattended wake if no user/CI/Matrix event arrives. Use an appropriate Forgejo/Matrix wait for an active review. A blocked task's contextual assistance request is visible in the widget (first 160 characters), menu, `tasks_list`, injected task context, and summary-aside input. `tasks_summary` can request LLM help without silently unblocking/completing it; resolving the prerequisite remains a separate action. Tasks convey requested work, not additional spending, access, or destructive-operation authority.

Limits: **one progress task per session ledger**, 100 retained tasks, 20 open tasks, 500-character summaries, 1,000-character criteria/blockers and review artifacts, 120-character `waiting_on`/reviewer names, and 12,000-character submitted evidence. At capacity, the user can clear completed tasks in `/tasks`; removing other stale tasks still requires a user decision. IDs are not reused within a branch, including after removal. Both the tool and menu enforce the progress cap before mutation. Complete, block, or return the existing progress task to pending to free the slot. Repeating the same progress state is a no-op. Older snapshots with multiple progress tasks are preserved, not rejected or rewritten; no additional task can enter progress until below the cap. The widget shows progress count/limit, and `tasks_list`/injected context expose `progress_limit: 1`. This is a ledger-state limit, not a limit on OS processes, background jobs, or external work.

## Optional pi-subagents assignment

With a compatible **pi-subagents** plugin loaded, `tasks_assign` supports both launching delegated work and tracking an existing **async** run:

```json
{"task_id": 1, "action": "launch", "agent": "worker"}
{"task_id": 2, "action": "link", "run_id": "exact-existing-run-id"}
{"task_id": 1, "action": "refresh"}
{"task_id": 2, "action": "detach"}
```

Launch sends the task ID, summary and acceptance criteria as a fresh-context brief through the plugin's public `spawn` RPC. The plugin retains ownership of agent discovery, model selection, authorization/capability checks, spawn limits, execution and native notifications. Existing delegation, disclosure and spending permissions still apply; recording a task is not permission to delegate or incur charges. There is no direct SDK child runner or dependency on a private plugin module.

Link requires an **exact active current-session async run ID**, not a prefix or opaque Fleet display key. It neither launches nor steers that run. The run's public display label supplies the recorded agent label (`agent` is optional for link). Foreground-only children must first be detached through the plugin's existing controls. Tasks with existing blockers, waiting questions or an earlier assignment refuse reassignment until explicitly resolved/detached.

Both paths persist a `subagent` record, block duplicate parent work and free the one parent progress slot. The widget shows `[delegated]`, agent and run; the full record is available in `tasks_list`, injected task context, the inspector and Markdown task snapshots. Other independent tasks remain actionable. Assignment guidance is injected only when the plugin's tools are registered; without the plugin, ordinary task operations are unchanged and assignment refuses before mutation.

Reconciliation runs on the parent's next `agent_start`, on matching native completion hints, or through explicit `refresh`. Completion hints alone are not proof: Tasks queries the public status RPC and requires the exact run in its versioned current-session async snapshot. `complete`, `failed`, `stopped` and `rejected` return the task to **pending**, retaining the run ID and outcome for parent inspection. `paused` and `partial` remain blocked for intervention, as do unknown, missing or omitted runs. This is execution tracking, **not completion judgment**: inspect results/artifacts and resolve failures before submitting ordinary `tasks_complete` evidence. Tasks adds no second completion wake and does not reset reminder budgets or resume a stop/pause.

Before launch, Tasks persists the RPC request ID. A missing reply, post-launch persistence failure or interruption leaves the task blocked with recovery correlation. **Never retry a launch on that basis.** `refresh` resolves the plugin's `rpc-spawn-<request_id>` alias to the actual run; an absent/expired/ambiguous result is not evidence that nothing launched. A saved assignment from another session remains blocked rather than being rebound. Session/task changes during an RPC cannot overwrite the replacement ledger.

`detach` removes tracking without stopping or cancelling the child. Explicit `tasks_unblock`, menu resume/removal/override, or changed acceptance criteria also detach tracking; avoid starting duplicate work while the old child is active. Use the plugin's own stop/interrupt controls when separately authorized. Restoring a ledger never launches new children. Missing/incompatible providers, bounded status omissions and pruned metadata fail closed; inspect through the plugin and make an explicit recovery decision.

Tests use fake RPC/execution with no model calls. An optional installed-plugin contract test additionally imports an explicitly selected `PI_TASKS_SUBAGENTS_RPC_MODULE` (the plugin's `src/extension/rpc.js`) and stubs only its executor. If peers are host-provided, `PI_TASKS_SUBAGENTS_PEER_ALIASES` is a JSON mapping of peer package names to absolute entrypoints (TypeBox root aliases also cover its subpaths). This verifies the installed bridge's real validation, spawn normalization, session-scoped status projection, linking and terminal reconciliation without launching work. No plugin path or account configuration is embedded in the extension.

## Optional background-job dependencies

When both extensions are loaded, link **one current-session job** with:

```text
tasks_block(task_id=2, reason="Waiting for test output", background_job_id="job-3")
```

The task becomes blocked and frees its progress slot. On **success, failure, cancellation or timeout**, it returns to its previous `pending`/`progress` state; if another task has taken the progress slot, it returns to `pending`. Finishing a job never marks a task complete or proves its criteria: inspect the job result, handle failures, then use the completion judge normally. Running, stopping, unknown, detached and deadline-exceeded observations are not terminal results.

The link stores the job ID, session ID, immutable systemd unit and previous status. This prevents a reused short ID or foreign-session event from releasing the wrong task. Already-finished jobs reconcile immediately; reload reconciles retained results in either extension startup order. A task with another manual/review blocker must be unblocked before adding a job link. Reblocking without `background_job_id` replaces the link with a manual blocker. Manual unblock/resume, changed acceptance criteria, removal, and accepted/user-overridden completion detach it, so a late job event cannot undo those decisions. Multiple tasks may await the same job; the single progress-slot limit still applies.

**Neither extension requires the other.** With no background provider, ordinary task operations work unchanged, but a new job-link request refuses without changing the task. A restored dependency with an absent provider, missing/pruned job, mismatched session or changed unit remains blocked. `tasks_list` and injected context expose `background_job` and `background_job_availability`; the widget/menu flag unavailable dependencies and manual unblock remains available. Losing a provider does not silently erase a prerequisite.

The integration uses two versioned Pi event-bus channels, with no cross-extension imports, polling from Tasks, or task-side filesystem/systemd access:

- `background:lookup-v1`: synchronous mutable query `{version: 1, session_id, job_id}`. A ready matching-session provider sets `available: true` and, for a known job, `job: {id, session_id, unit, status, finished_at}`. No commands, output, paths or credentials cross this interface.
- `background:changed-v1`: `{version: 1, session_id}` prompts Tasks to reconcile its matching dependencies by lookup. Emitted for retained terminal results before Background queues its existing completion steering, and after startup reconciliation. Duplicate events are harmless.

Tasks persists its resumed state before that completion wake; it does **not** send another wake, call a model, reset reminder limits or override stops/pauses. Background's existing completion notification remains the sole integration wake. If a result was already notified before Tasks loaded, reconciliation updates the ledger without replaying that notification. Retention may remove old registry entries; missing entries require manual recovery, not guessed success.

## Share the list or ask an aside for help

- **`/tasks-send`** posts a Markdown checkbox list to the current session, incomplete (pending, progress and blocked) tasks first, completed tasks last. Relative order within each group is preserved. IDs, in-progress labels, open linked-question IDs and blocked reasons are included; task text is escaped as literal Markdown and multiline summaries remain one item. An empty ledger posts “No tasks in this session.” No model is called to format the list.
- **`/tasks-summary [question]`** asks the **currently selected model** for a contextually useful summary/advice in an isolated, tool-free aside. For example: `/tasks-summary Why are we stuck waiting for review, and what can move independently?`
- **`tasks_summary(prompt?)`** lets the main agent ask that same aside for advice or help with a blocker. It uses the same publication path as the user command, not a private tool-only answer.
- Both actions are also available in the `/tasks` menu. The AI-summary action asks for an optional question; cancelling it does not call a model.

**Both the slash command and agent tool post the AI summary visibly into the session as an extension-attributed steering message** (`deliverAs: "steer", triggerTurn: true`). The plain list uses the same delivery. If the main agent is idle it starts a response; if busy, Pi delivers steering at its next safe boundary. Remaining sibling tools may be skipped by Pi's normal steering behavior. This is intentional, not a forged user message. At idle, messages are immediately recorded in the session; the agent receives them as context. Generated advice is labeled advisory, not instructions or a completion verdict, and never mutates/completes tasks itself.

This Pi version does not expose a native `/btw` extension API. The equivalent **aside-style isolation** here is a fresh model request with no executor system prompt, tools, files, or inherited agent loop; it is not a named native `/btw` session. It receives:

1. A task-list snapshot (IDs, statuses, summaries, and blockers).
2. The last **six user/assistant text messages**, at most 2,000 characters each with an explicit truncation marker. System prompts, hidden reasoning, attachments/images, tool calls/results, prior custom summaries/reminders, and other custom entries are excluded.
3. An optional user/model question, at most 2,000 characters.

The entire serialized input is capped at **48,000 characters**; oversized inputs are rejected without inference, not silently dropped. The model output is capped at 1,024 tokens and 8,000 characters. There is one request, a 60-second local deadline, and no automatic retry or provider fallback. Repeating the summary command/tool while an aside is active cancels it instead of starting a second request. Session transitions and caller cancellation abort it; late results never cross sessions. If tasks changed during generation, the posted snapshot explicitly says it is stale and directs the executor to the current ledger. Model provenance is captured before awaiting a response, including if `/model` changes meanwhile.

The summary uses **additional model inference**, and the resulting steering can also start main-agent inference. Existing provider, disclosure, and spending permissions apply to both. Recent conversation is sent only to the currently selected provider; do not use it if that disclosure is not authorized. The summary tool returns its nested usage to Pi; command-request usage is recorded in the posted message's details (not added to Pi's tool-usage total). Real-model advice is not guaranteed correct. Tests use fake responses, never live provider inference.

## Completion and judging

Each completion submission makes **one isolated, tool-free model request**, using the currently selected Pi model/provider and Pi's existing authentication. It receives the task summary, acceptance, submitted evidence, excerpts from any explicitly declared `evidence_files`, and bounded text from public URLs cited in that evidence, and optionally recent session messages—not the executor's system prompt, extensions, or callable tools. It is a fresh assessment context, not a long-lived judge agent. The extension does not give the judge general workspace or web browsing. Supply concrete test output, observed behavior, or relevant artifact excerpts; a path alone is not inspected.

`context_messages` is an optional integer from 0–10 (default 0). It shares the latest eligible messages from the current session branch in chronological order: user text, assistant text and tool calls (IDs, names and arguments), and tool-result text (call ID, name and error status). Each tool-result message counts separately; an assistant message containing tool calls counts as one message. Thinking, images/attachments, system/custom messages and extension state are excluded. Each message is limited to 8,000 characters plus a visible truncation marker. With 0 or omission no session context is read or sent. Context is untrusted evidence, not judge instructions, and does not grant tools. These excerpts go to the **currently selected model provider**; the caller must have disclosure permission, including for tool inputs/outputs. Context excerpts are not persisted in the task ledger.

`evidence_files` is an opt-in list of at most three paths within the current workspace. Each must resolve to a regular UTF-8 text file of at most 64 KiB; escapes outside the workspace, binary/invalid text and missing files refuse assessment without a model call. Only the first 8,000 characters of each file are included, with a visible truncation note; submit a focused excerpt for later lines. File contents go to the **currently selected model provider**, so the caller must have permission to disclose them. Full file contents are not persisted in the task ledger; paths successfully read, the submitted evidence text and the verdict are retained for provenance. Failed or refused reads are not recorded as inspected files.

Up to three distinct HTTP(S) URLs appearing in the submitted evidence are fetched before judging. This makes an outbound request to each public site. The extension reuses `web_read`'s public-address and DNS-rebinding checks, credential/private URL refusal, per-hop redirect checks, 2 MB response cap and timeouts. Only text pages are eligible; image URLs, unsupported content, unreadable pages or a fourth distinct URL leave the task open without a model request. The judge receives at most 5,000 characters of each extracted page, with a truncation note. Successful source URLs, not full pages, are retained in the ledger. Retrieved content is untrusted data; the judge has no ability to follow links or fetch more. Cite only public, credential-free URLs whose content may safely be fetched and sent to the selected model provider.

This is model inference, not a free local predicate. Selecting a provider is not spending approval. The caller/installer must already be authorized to send the evidence and incur any inference charges on the selected provider. The extension neither buys credits nor creates credentials, and has no automatic provider fallback. No live inference is needed to run its tests.

Judge output is strict JSON:

```json
{"verdict":"needs_work","reason":"The failure-path criterion is not evidenced.","missing":["Show output demonstrating that invalid inputs are rejected."]}
```

- `accepted`: complete, with an empty missing list and recorded `completedBy: judge`.
- `needs_work`: leave open; require a nonempty reason and specific missing-evidence list. Return the same verdict to the executor and include it in subsequent task context.
- `unable_to_assess`: leave open and explain the assessment problem, distinct from task rejection.

Malformed JSON, blank explanations, rejection without missing evidence, conflicting approval, truncated/provider-error responses, and unavailable models never complete tasks. A 60-second local deadline bounds the tool even if an adapter does not settle on cancellation. Abort is requested, but this cannot guarantee cancellation of remote inference/billing. Raw provider errors are not copied into the ledger. Returned nested inference usage is passed through to Pi. Cancelled submissions and stale results after session or task changes never apply a verdict. Unrelated task additions do not invalidate an assessment.

The judge is instructed to use only the recorded criteria, not invent optional polish, and treat evidence as untrusted data. This is an assessment of **provided evidence**, not independent artifact verification or a security boundary against a malicious executor. The host agent retains the same OS permissions as before. Session evidence may be private; ordinary session-storage and provider disclosure rules still apply.

## Awareness, continuation, and stop behavior

The active branch's open ledger is injected on **each model request**, including after compaction. The serialized ledger preview is capped at 16,000 characters; long fields carry an ellipsis, and an extreme snapshot falls back to task IDs/statuses/short titles. `truncated: true` tells the agent to call `tasks_list` for full criteria, blockers and verdicts before relying on omitted details. Full stored tasks and judging criteria are unchanged. The transient task-context snapshot is replaced rather than duplicated. Historical extension-owned reminder messages stay in the transcript but are omitted from model context: their old pending-work snapshots must not re-prompt after tasks change. Real user messages and explicit list/summary posts are preserved. Completed-only ledgers inject no task prompt. Blocked-only and explicitly waiting-question-only ledgers are informational; a due review checkpoint names the follow-up on the next turn but does not start one. Paused/suspended ledgers do not contain continuation instructions. Completed tasks remain available through `tasks_list`.

At `agent_before_settle`, pending and progress tasks can add an attributed reminder and request continuation. The extension appends a custom message before requesting continuation: an initial boundary with `canContinue: false` after an assistant response is normal, not a reason to abandon the task list. The SDK recalculates the proposed context with that message.

The fallback waits three minutes after settlement, then sends an attributed follow-up only if still idle, with no queued user/extension messages, pending assessment, or open menu. It never interprets a long-running tool/model request as idle and never interrupts streaming. Timers are deduplicated and cleared on new activity/session transitions/shutdown.

Both paths share a budget of **two automatic reminders without verified completion**. An accepted task, genuine new user input, or explicit menu resume resets this no-progress budget; rejection, tool activity, and extension-generated input do not. Independently, at most **six automatic continuations per genuine user turn** may be initiated by the extension, including final-boundary reminders, idle follow-ups, and menu-close follow-ups. Accepted completions, task additions and other menu edits do not reset this hard cap. Only new non-extension user input or explicit `/tasks` → **Resume reminders** resets it; an explicit Resume starts one counted follow-up when work is actionable. The six-continuation count persists in the active branch's task delta stream, so reload, fork and compaction do not replenish it. The separate two-reminder no-progress count remains in memory and resets on reload as before. Exhaustion leaves tasks open; the widget shows “auto budget exhausted” and `/tasks` offers Resume reminders. Explicit user list/summary/priority commands are not automatic continuations and still obey their own authority and inference rules.

Abort/error outcomes suspend automatic reminders until new user input or explicit menu resume. Common direct stop/pause phrases at the start of user input also suspend them; this is conservative phrase matching, not full natural-language intent recognition. **Escape cancellation and `/tasks` → Pause reminders are the reliable stop controls.** Menu pause persists across reloads; loading/resuming/navigating a session alone does not start inference. User or extension queued messages take priority over a task reminder. Blocking is explicit and is never inferred from arbitrary assistant prose. When no actionable tasks remain, the idle timer is cleared and settlement, idle and menu-close paths do not enqueue a new turn. A turn already accepted by the host before the state changed cannot be retracted by this extension; the current-context filter prevents it from receiving the stale reminder snapshot.

Older sessions may contain `tasks-state-v1` full snapshots. New mutations append `tasks-delta-v2` entries instead: a revision, at most one changed task or one removed task, or a confirmed batch removing only completed tasks, plus optional final ID order, next ID, pause state, and the automatic-continuation count. Each serialized delta is capped at 192 KiB; even a ledger with 100 retained maximum-size tasks writes only the changed task, not another full ledger. A user-input budget reset writes a small delta only when the count changed. Replay validates sequential revisions, task IDs/order and state limits before enabling mutations; a legacy snapshot after a v2 delta, a gap, or invalid data fails closed rather than silently dropping work. Restoration follows `getBranch()`, not all session entries, so forks/navigation use only their own history. Compaction does not erase the ledger or append a duplicate checkpoint. The extension does not write project files or a second database.

## Task-menu keys and clarification

The interactive `/tasks` popup keeps arrow/Enter navigation. Its shortcuts apply
only while that popup has focus, not to typing in the main editor:

| Key | Action |
| --- | --- |
| `p` | Move the selected incomplete task to the front of the ledger and post one visible steering request to work on it next at a safe boundary. Starts an idle agent; does not mark progress, unblock a task, expand authority or change a persistent reminder pause. |
| `c` | For the in-progress task only: confirm **Request stop…**, then return its marker to pending, suspend automatic reminders, and steer a stop request. Does not wake an idle agent, claim completion, or terminate an already-running tool/external job. Escape remains the agent interruption control. |
| `x` | Confirm deletion of a selected task that is **not** in progress. In-progress tasks cannot be deleted; cancel/return them to pending first. |
| `n` | Open Add task directly. |
| `q` / Escape | Close the menu without starting work. |

The same task actions are available after selecting a task with Enter. Completed
tasks cannot be prioritized as new work. Guards re-check the selected task after
asynchronous dialogs; stale choices cannot overwrite a newer task/session.
Prioritization changes authoritative ledger order explicitly; widget status
priority still applies separately. Cancellation posts a request, not proof that
execution has stopped. A failed steering post is reported rather than claimed
as delivered.

**Edit acceptance criteria** adds or clarifies task details while keeping title
and ID unchanged. It is user-only, validates the existing 1–1000-character
limit, and cancels obsolete in-flight assessments. Previous evidence remains
available, but an old verdict cannot approve changed criteria. Changing criteria
on a completed task requires confirmation to reopen it as pending, clears its
old verdict/completion attribution, and respects the open-task cap. Browsing or
editing does not itself wake the agent. No model-callable edit/delete tool is
added.

The task inspector shows bounded one-line previews of summary, criteria,
blocker, evidence and assessment instead of rendering a 12,000-character
submission in its heading. An ellipsis marks clipped fields. **View full
criteria (read-only)** and **View full evidence (read-only)** open the entire
stored text in the user's editor; changes there are discarded. These views do
not persist state, start inference or post private evidence into model context.

### Extension options

Pass these Pi extension flags when launching Pi:

- `--tasks-widget-count 3`: number of preview tasks, integer 0–100; `0` hides the widget.
- `--tasks-menu-focus progress`: initially select the in-progress task, or Add
  task when none is in progress. Use `--tasks-menu-focus add` to always select
  Add task instead.
- `--tasks-widget-single-line`: keep each task title to one terminal line
  using ANSI- and Unicode-cell-aware truncation. Blocked tasks show their reason
  separately underneath, indented and wrapped to at most two lines with an
  ellipsis when needed. The count option counts tasks, not these display lines.
  Full stored titles/reasons remain unchanged; the inspector shows bounded
  previews with on-demand full-text views. Omit this flag to retain the existing
  ordinary widget wrapping path.

Defaults are three rows, progress-or-Add initial focus, and normal wrapping.
Invalid count/focus values produce a clear extension-load error, not a silent
fallback. Options affect presentation only, not continuation/spending budgets.

## Widget display order

The on-screen preview shows the configured number of tasks (three by default), ordered **progress, blocked,
pending, complete**, preserving ledger order within each status. Completed
entries fill remaining preview slots; a completed-only ledger can remain visible
without starting reminders. This is presentation only: task IDs, authoritative
ledger order, menu order and model context are unchanged.

## Verification

```sh
node --test tests/tasks.test.cjs
```

The unit suite covers ordering, IDs, invalid inputs and limits; judge isolation/usage, acceptance, actionable rejection and assessment errors; branch restoration and compaction-style context refresh; bounded end/idle reminders; stops, blockers, busy/queued/menu guards; user-only mutations, confirmation cancellation, overrides; deadlines and stale/cancelled result races.

CI installs the locked `tests/pi-sdk` toolchain (Pi 0.99.1, TypeScript 5.9.3,
Node types 24.0.0), strictly typechecks this extension, and explicitly selects
that SDK for the offline contract test. Reproduce those checks from the repo root:

```sh
npm ci --prefix extensions --ignore-scripts --no-audit
npm ci --prefix tests/pi-sdk --ignore-scripts --no-audit
npm run --prefix tests/pi-sdk typecheck
PI_TASKS_SDK_MODULE="$PWD/tests/pi-sdk/node_modules/@earendil-works/pi-coding-agent/dist/index.js" \
  node --test tests/tasks.test.cjs tests/tasks-sdk.test.cjs
```

A separately installed SDK can still be selected with `PI_TASKS_SDK_MODULE`;
ordinary local unit runs skip the contract only when that variable is absent.
A configured but unavailable SDK fails rather than skipping.

It loads only this extension with isolated configuration and an in-memory session, replaces the model stream with fake responses, and checks **actual Pi settlement continuation**, model-visible task context, tool registration, direct-stop behavior, and `/tasks` dispatch without provider inference. It also exercises real slash-command steering and a model-issued `tasks_summary` call: both post visible summaries, wake/deliver to the main agent, and leave completion state unchanged. The pinned CI invocation does not skip this test. Real-model judge quality and interactive visual rendering still need a separately authorized live trial; unit/SDK tests do not establish them.

### Stop requests are not execution termination

The in-progress action is **Request stop…** (shortcut `c`). Confirmation explains
that running tools and external jobs may continue, and points to Escape and
`/jobs` for their separate controls. After confirmation the ledger displays
**[stop requested]**, persists `stop_requested: true`, returns the task to pending
and suspends automatic reminders for the whole ledger. This marker records the request, not an
acknowledgment that execution has stopped. User prioritization, marking the task
in progress, resuming it or overriding completion clears the marker. After an
explicit user restart instruction, the normal `tasks_progress(task_id)` path also
clears it so authorized progress remains actionable. Passive context retains the
stop-request interpretation even when all actionable work is stopped. It does not
authorize the agent to resume stopped work without a user instruction.


### Menu navigation and detail hierarchy

The list remembers the task selected while inspecting or editing it. Escape
returns from a task inspector to the list; Escape at the list closes it, and
`q` closes either level. The inspector leads with summary, acceptance, blocker
and waiting owner. Choose **View diagnostics (read-only)** for review metadata,
evidence, subagent tracking, assessment and completion ownership; edits to that
viewer are discarded. Full criteria/evidence views remain available.


### Compact global actions

The root task list ends with **Add task**, **More actions…** and **Close**.
**More actions…** contains only clearing completed tasks and pausing/resuming
reminders. Sending and AI help live on the selected task: **Send this task to
session** sends that task only; **Ask AI about this task… (extra model call)**
summarizes that task with recent conversation, with explicit model-call labeling. Escape or Back returns to the root; `q` closes.
Existing direct `/tasks-add`, `/tasks-prune`, `/tasks-send` and `/tasks-summary`
commands remain available. Removal/override confirmations are unchanged.
