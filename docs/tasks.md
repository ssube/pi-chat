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

- View task summary, acceptance, blocker, submitted evidence, verdict, and completion owner.
- Add a task with optional acceptance criteria and insertion position.
- Remove a task, with confirmation.
- Mark complete with a confirmed, explicitly recorded **user override**. This does not fabricate a judge approval.
- Resume a blocked task.
- Pause or resume automatic reminders.

TUI status labels retain their text and use distinct theme colors: pending = `accent`, blocked = `warning`, complete = `success`. The widget, menu choices, and detail heading share this mapping; model-visible task data and Markdown snapshots contain no terminal color escapes.

Cancelling input or a confirmation leaves the task unchanged. Dialogs detect intervening task/session changes rather than overwriting them. Adding/resuming work or overriding completion queues an extension-attributed follow-up after closing the menu, unless paused or no pending work remains. Merely inspecting tasks does not start inference. The menu requires TUI; tools, context injection, judging, and reminders also work headlessly.

## Agent tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `tasks_list` | none | Ordered full ledger including last evidence, verdicts, and blockers. |
| `tasks_add` | `summary`, optional `order`, `acceptance` | Stable integer ID. `order` is a 1-based insertion position in the full list (including completed tasks); defaults to append. Acceptance defaults to summary. |
| `tasks_complete` | `task_id`, `evidence` | Assess one claim; complete only on accepted verdict. Return verdict, reason, missing evidence, and remaining pending IDs. |
| `tasks_block` | `task_id`, `reason` | Record why work is blocked and exactly what user/LLM decision, information, or help is needed; suppress reminders for that task. Does not complete/remove it. |

There is **no agent removal or override tool**. `tasks_summary(prompt?)` is advisory only; see the sharing commands below. A blocked task can be submitted for assessment once resolved; a user can also resume it from the menu. Blocking all open tasks stops reminders while retaining the work and reasons. A blocked task's contextual assistance request is visible in the widget (first 160 characters), menu, `tasks_list`, injected task context, and summary-aside input. `tasks_summary` can request LLM help without silently unblocking/completing it; resolving the prerequisite remains a separate action. Tasks convey requested work, not additional spending, access, or destructive-operation authority.

Limits: 100 retained tasks, 20 open tasks, 500-character summaries, 1,000-character criteria/blockers, and 12,000-character submitted evidence. At capacity the agent must ask the user to remove stale tasks. IDs are not reused within a branch, including after removal.

## Share the list or ask an aside for help

- **`/tasks-send`** posts a Markdown checkbox list to the current session, incomplete (pending and blocked) tasks first, completed tasks last. Relative order within each group is preserved. IDs and blocked reasons are included; task text is escaped as literal Markdown and multiline summaries remain one item. An empty ledger posts “No tasks in this session.” No model is called to format the list.
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

Each completion submission makes **one isolated, tool-free model request**, using the currently selected Pi model/provider and Pi's existing authentication. It receives only the task summary, acceptance, and submitted evidence, not the executor's system prompt, conversation, extensions, or tools. It is a fresh assessment context, not a long-lived judge agent. No workspace/file access or arbitrary evidence-URL fetching is performed. Supply concrete test output, observed behavior, or relevant artifact excerpts; a path alone is not inspected.

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

The active branch's open ledger is injected on **each model request**, including after compaction. Existing messages are preserved, and the transient task-context snapshot is replaced rather than duplicated. Completed tasks are available through `tasks_list`, not repeatedly injected.

At `agent_before_settle`, pending tasks can add an attributed reminder and request continuation. The extension appends a custom message before requesting continuation: an initial boundary with `canContinue: false` after an assistant response is normal, not a reason to abandon the task list. The SDK recalculates the proposed context with that message.

The fallback waits three minutes after settlement, then sends an attributed follow-up only if still idle, with no queued user/extension messages, pending assessment, or open menu. It never interprets a long-running tool/model request as idle and never interrupts streaming. Timers are deduplicated and cleared on new activity/session transitions/shutdown.

Both paths share a budget of **two automatic reminders without verified completion**. An accepted task, genuine new user input, or explicit menu resume resets the budget; rejection, tool activity, and extension-generated input do not. Thus a list can progress through many verified tasks, but cannot generate an unbounded no-progress continuation loop. Exhaustion leaves tasks open; `/tasks` offers Resume reminders.

Abort/error outcomes suspend automatic reminders until new user input or explicit menu resume. Common direct stop/pause phrases at the start of user input also suspend them; this is conservative phrase matching, not full natural-language intent recognition. **Escape cancellation and `/tasks` → Pause reminders are the reliable stop controls.** Menu pause persists across reloads; loading/resuming/navigating a session alone does not start inference. User or extension queued messages take priority over a task reminder. Blocking is explicit and is never inferred from arbitrary assistant prose.

Snapshots are appended as versioned Pi custom entries. Restoration follows `getBranch()`, not all session entries, so forks/navigation use only their own history. Compaction does not erase the ledger. Invalid state disables mutations/reminders with a visible diagnostic rather than silently replacing tasks with an empty list. The extension does not write project files or a second database.

## Verification

```sh
node --test tests/tasks.test.cjs
```

The unit suite covers ordering, IDs, invalid inputs and limits; judge isolation/usage, acceptance, actionable rejection and assessment errors; branch restoration and compaction-style context refresh; bounded end/idle reminders; stops, blockers, busy/queued/menu guards; user-only mutations, confirmation cancellation, overrides; deadlines and stale/cancelled result races.

Run the optional offline contract test against an explicitly selected installed Pi SDK:

```sh
PI_TASKS_SDK_MODULE=/absolute/path/to/pi-coding-agent/dist/index.js \
  node --test tests/tasks-sdk.test.cjs
```

It loads only this extension with isolated configuration and an in-memory session, replaces the model stream with fake responses, and checks **actual Pi settlement continuation**, model-visible task context, tool registration, direct-stop behavior, and `/tasks` dispatch without provider inference. It also exercises real slash-command steering and a model-issued `tasks_summary` call: both post visible summaries, wake/deliver to the main agent, and leave completion state unchanged. Its ordinary CI invocation skips unless the SDK module is specified. Verified locally against Pi 0.99.1. Real-model judge quality and interactive visual rendering still need a separately authorized live trial; unit/SDK tests do not establish them.
