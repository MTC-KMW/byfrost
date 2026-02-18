# BYFROST - Agent Workflow Dynamics

> **Audience:** Developers building Byfrost.
> **Companion to:** `byfrost-build-plan.md` (architecture and tasks).
> **Purpose:** Define how agents behave at runtime across operating
> modes. The build plan defines what gets built. This document defines
> how agents coordinate.

---

## 1. Operating Modes

Byfrost supports multiple operating modes depending on where the
developer is sitting and what they're building. The bridge, wire
protocol, and compound cycle are the same in every mode. What changes
is which agents run on which machine, who spawns whom, and how tasks
cross the bridge.

| Mode | Developer Location | Worker Machine | Primary Use Case |
|---|---|---|---|
| Normal (web-first) | Controller (Linux/Windows) | Mac | Building backend/web + iOS app from a non-Mac machine |
| UI mode | Mac | Mac (local) + Controller (autonomous) | Xcode/simulator work that requires visual access to the Mac |
| App-first | Mac (controller) | Linux | Mac-primary projects where Linux handles backend/CI |

---

## 2. Normal Mode (Web-First)

The developer sits at the controller machine. PM is their direct
conversation. The Apple Engineer runs remotely on the Mac via the
bridge. This is the default mode and the primary use case for v1.0.

### 2.1 Agent Locations

**Controller (where the developer is sitting):**

- PM - developer's direct conversation
- QA Engineer - monitors Apple Engineer stream, 8-lens review
- Backend Engineer - local Claude Code via Agent Teams (if present)
- Frontend Engineer - local Claude Code via Agent Teams (if present)

**Worker (Mac):**

- Apple Engineer - Claude Code in tmux, spawned by daemon

### 2.2 The Compound Cycle

**User Request.** You describe what you want in plain language to PM.
PM asks clarifying questions if needed, then takes over.

**Plan.** PM reads `byfrost/compound/patterns.md`,
`byfrost/compound/anti-patterns.md`, and
`byfrost/compound/learnings.md` to understand what the team has
learned from past cycles. It researches the codebase to understand
the current state of all stacks. It writes task specs for each agent
involved, referencing proven patterns by number ("follow P-002 for
error handling") and calling out anti-patterns to avoid ("avoid
A-001"). Backend and web tasks are dispatched through Agent Teams
messaging. The Apple Engineer's task spec is written to
`byfrost/tasks/apple/current.md`, which the bridge syncs to the Mac
automatically - the file change triggers task pickup. PM messages QA:
"Apple Engineer task dispatched, monitor the stream and build a change
inventory."

**Work.** Backend and Web agents pick up their tasks on the controller
machine and work independently. They message PM or each other through
Agent Teams as needed.

The Apple Engineer receives its trigger when the bridge syncs the task
file to the Mac. It reads the task from
`byfrost/tasks/apple/current.md`, reads compound knowledge from
`byfrost/compound/`, implements, and tests. Its full terminal output
streams back over the WebSocket to the controller. All file changes
are synced to the controller in real time via the bridge.

QA runs `byfrost attach` and watches the Apple Engineer's streamed
output in real time. As it sees file creates, edits, and deletes in
the Claude Code terminal output, it writes a structured change
inventory to `byfrost/qa/mac-changes.md`. QA is not reading `apple/`
directly - it is parsing the stream only. Because `byfrost/qa/` is
bridge-synced, PM can see this inventory updating live.

The Apple Engineer's task completes. All file changes have already
been synced to the controller during the Work phase.

**Handoff.** PM receives the `task.complete` message from the bridge.
It reads `byfrost/qa/mac-changes.md` to see what files the Apple
Engineer created, edited, or deleted. Bridge sync has already
delivered these files to the controller. PM verifies the files match
QA's inventory. Any discrepancy is caught immediately.

**Review.** PM triggers QA for the 8-lens review. QA already knows
which files to focus on from the inventory it built during the Work
phase. It reviews across all stacks. It writes the review report to
`byfrost/qa/review-report.md`, which PM and the Apple Engineer can
see immediately through bridge sync. If issues are found, PM routes
fixes back to the appropriate agent.

**Compound.** PM reads the review report and extracts observations
into `compound/learnings.md` as raw dated notes. If a learning recurs
across cycles or proves significant, PM promotes it to a numbered
entry in `compound/patterns.md` or `compound/anti-patterns.md`. The
original learning stays with a note that it was promoted, preserving
the trail. PM reports back to you with a summary of what was built,
issues QA found, and what it learned. PM commits and pushes to GitHub.
You decide what to work on next.

The Apple Engineer sees updated patterns and anti-patterns instantly
on its next task through bridge sync. The next cycle starts better
than the last.

### 2.3 Communication Flow

```
Developer
  |
  v
PM (controller) --Agent Teams--> Backend Engineer (controller)
  |                               Frontend Engineer (controller)
  |                               QA Engineer (controller)
  |
  |--bridge file sync-----------> Apple Engineer (Mac)
  |                                  |
  |<--bridge WebSocket stream--------|
  |<--bridge file sync (code)--------|
  |
QA --byfrost attach--> Apple Engineer stream (read-only)
QA --bridge file sync--> byfrost/qa/mac-changes.md
```

### 2.4 Spawn Order

1. Developer starts PM (their Claude Code conversation)
2. PM spawns Backend/Frontend Engineers via Agent Teams as needed
3. PM writes Apple Engineer task file, bridge syncs it to Mac
4. Daemon on Mac spawns Apple Engineer in tmux
5. PM spawns QA, tells it to monitor the stream

All agents on the controller are spawned by PM. The Apple Engineer
on the Mac is spawned by the daemon when a task file arrives.

---

## 3. UI Mode (Mac-Side Development)

The developer needs to see Xcode, the simulator, or the app's visual
output. They sit at the Mac and work directly with the Apple Engineer.
The compound cycle continues - backend changes triggered by UI work
are dispatched to controller agents in real time through a lazy spawn
chain.

### 3.1 When This Mode Activates

- The developer needs visual access to iOS simulator output
- Layout debugging, UI polish, or visual testing
- Any work where seeing the rendered app is essential
- Developer tells PM "switching to UI mode" or simply walks to the
  Mac and starts an Apple Engineer session

### 3.2 Agent Locations

**Mac (where the developer is sitting):**

- Apple Engineer - developer's direct conversation, acts as both
  implementer and requester for backend changes

**Controller (running autonomously):**

- QA - spawned by daemon when bridge traffic detected
- PM - spawned by QA when a backend task is detected
- Backend Engineer - spawned by PM on demand
- Frontend Engineer - spawned by PM if required (unlikely during
  UI work)

### 3.3 Spawn Chain

```
Apple Engineer starts work on Mac
  -> Bridge traffic flows to controller
    -> Controller daemon spawns QA
      -> QA attaches to stream (buffered history + live)
        -> QA detects backend task in stream
          -> QA spawns PM via Agent Teams
            -> PM dispatches to Backend Engineer via Agent Teams
```

Each agent spawns only when needed. No agent runs until triggered.
Token cost while idle is zero - sessions waiting for input make no
API calls.

### 3.4 The UI Mode Cycle

**1. Developer works with Apple Engineer on Mac.** Building UI,
adjusting layouts, wiring up views. This is a normal Claude Code
conversation. The developer can see Xcode, the simulator, and the
app rendering in real time.

**2. Controller daemon spawns QA automatically.** When the daemon
detects bridge traffic (Apple Engineer session active), it launches
a Claude Code session on the controller with QA's CLAUDE.md. QA runs
`byfrost attach` and receives buffered stream history from the start
of the session, then transitions to live output. No gap in coverage.

**3. QA monitors stream and builds change inventory.** QA watches the
Apple Engineer's terminal output. It records file creates, edits, and
deletes to `byfrost/qa/mac-changes.md`, same as normal mode. Bridge
syncs this file to the Mac so the developer can see it if needed.

**4. Apple Engineer triggers a backend need.** The developer tells
the Apple Engineer something like "add a dashboard that shows user
stats." The Apple Engineer builds the SwiftUI view, writes the
networking layer to call an endpoint that doesn't exist yet, and
writes a task spec to `byfrost/tasks/backend/current.md`. This file
syncs to the controller via the bridge.

**5. QA detects the backend task.** QA sees the Apple Engineer write
the task file in the stream output and confirms it has arrived locally
on the controller. QA spawns PM via Agent Teams with a message:
"Apple Engineer has sent a backend task. Task spec at
tasks/backend/current.md."

**6. PM analyzes and dispatches.** PM reads the task spec. If the
task is trivial (small data change, simple query), PM may implement
it directly. If substantive, PM dispatches to Backend Engineer via
Agent Teams. PM stays alive for the remainder of the UI session,
maintaining context of what has already been dispatched to prevent
duplicate or conflicting tasks.

**7. Backend Engineer implements.** Backend Engineer picks up the
task, implements, and tests. Files are saved locally on the controller.
Bridge syncs the changes to the Mac in real time. Reports completion
to PM via Agent Teams.

**8. Cycle repeats.** The developer continues UI work. More backend
needs may arise. QA detects them, messages PM (already running), PM
dispatches. Backend changes are synced to the Mac via bridge as
they are written. The Apple Engineer can immediately integrate
against the updated code.

**9. Developer finishes UI session and returns to controller.** The
developer tells the Apple Engineer the session is done. PM on the
controller runs the compound phase: reads QA's change inventory,
spawns QA for the 8-lens review across all stacks, extracts
learnings, promotes patterns and anti-patterns. PM commits and pushes
to GitHub. Normal compound cycle resumes.

### 3.5 Communication Flow

```
Developer
  |
  v
Apple Engineer (Mac)
  |
  |--writes task file to byfrost/tasks/backend/current.md
  |--bridge syncs file to controller
  |
  |--bridge WebSocket stream-----> QA (controller)
  |                                  |
  |                                  |--Agent Teams
  |                                  v
  |                                PM (controller)
  |                                  |
  |                                  |--Agent Teams
  |                                  v
  |                                Backend Engineer
  |                                  |
  |<--bridge file sync (code)--------|
  |
Apple Engineer reads updated backend code, integrates
```

### 3.6 Key Differences from Normal Mode

| Aspect | Normal Mode | UI Mode |
|---|---|---|
| Developer location | Controller | Mac |
| Developer's conversation | PM | Apple Engineer |
| Apple Engineer trigger | Bridge-synced task file | Developer directly |
| Backend task dispatch | PM writes task, dispatches | Apple Engineer writes task, QA detects, PM dispatches |
| QA spawn | PM spawns via Agent Teams | Daemon spawns on bridge activity |
| PM spawn | Already running (developer's session) | QA spawns on backend task detection |
| Code transport | Bridge file sync (both directions) | Bridge file sync (both directions) |
| Git operations | PM commits and pushes from controller | PM commits and pushes from controller |
| Compound phase | PM runs after Work/Review | PM runs after developer returns to controller |

### 3.7 What Each Agent Needs to Know

**Apple Engineer (UI mode additions to CLAUDE.md):**

- When the developer is working directly with it, it is the primary
  requester for backend changes
- Writes backend task specs to
  `byfrost/tasks/backend/current.md` with clear requirements,
  expected request/response shapes, and references to existing
  patterns
- Backend code arrives via bridge sync in real time - no git pull needed
- It does not dispatch via Agent Teams (no access across the bridge) -
  it writes files, the bridge handles delivery
- It does not use git - the bridge syncs all file changes

**QA (UI mode additions to CLAUDE.md):**

- When spawned by the daemon during UI mode, its job is stream
  monitoring and backend task detection
- When it sees a backend task file written in the stream, it spawns
  PM and passes the task location
- It continues monitoring after spawning PM - more tasks may follow
- At the end of the UI session, it performs the standard 8-lens review
  when PM requests it

**PM (UI mode additions to CLAUDE.md):**

- It may be spawned mid-session by QA rather than being the initial
  conversation
- It receives backend task specs written by the Apple Engineer, synced
  via bridge
- It dispatches to Backend Engineer (or implements directly if
  trivial)
- It stays alive after dispatch to maintain context across multiple
  tasks in the session
- When the developer returns, it runs the compound phase as normal
- It commits and pushes to GitHub after parity validation

---

## 4. App-First Mode

The same controller-worker model works when the Mac is the controller
and Linux is the worker. Roles are determined by which machine
initiated the pairing, not by platform.

| Aspect | Web-first (normal) | App-first |
|---|---|---|
| Controller | Linux/Windows | Mac |
| Worker | Mac | Linux |
| Initial codebase | Controller bundles to worker | Controller (Mac) bundles to worker (Linux) |
| Bridge file sync | Both directions | Both directions |
| Commits and pushes | PM on controller | PM on controller (Mac) |
| Worker talks to GitHub | Never | Never |

App-first mode follows the exact same controller-owns-git model. The
only difference is which machine sits in which role. Everything else -
bridge sync, git bundle transfer, spawn chains, Agent Teams - works
identically.

In app-first mode:

- The Mac is the controller. Developer sits at the Mac. PM is their
  conversation.
- Linux is the worker. Daemon runs on Linux, spawns agents in tmux
  when task files arrive.
- PM on the Mac commits and pushes to GitHub.
- The Linux worker never needs GitHub credentials.
- Bridge syncs all project files between Mac and Linux.

This mode is useful for projects where the Mac is the primary
development machine and Linux handles backend services, CI, or
server-side work.

---

## 5. Mode Transitions

Developers switch between modes during a project. The bridge supports
this without restart or reconfiguration.

### Controller to Mac (entering UI mode)

1. Developer tells PM: "switching to UI mode" (optional but
   recommended - lets PM wrap up any in-progress coordination)
2. Developer walks to the Mac
3. Developer starts or resumes an Apple Engineer session
4. Bridge traffic triggers QA spawn on controller
5. Normal mode agents (Backend Engineer, etc.) may still be running
   from previous tasks - they continue or idle naturally

### Mac to Controller (exiting UI mode)

1. Developer tells Apple Engineer: "done with UI work"
2. Developer returns to controller
3. PM runs compound phase: review, extract learnings, promote patterns
4. PM commits and pushes to GitHub
5. Normal mode resumes - PM is the developer's conversation again

### Mid-Session Switch

If the developer needs to check something on the controller without
fully exiting UI mode (SSH, quick check), the Apple Engineer session
stays alive on the Mac. QA keeps monitoring. No disruption.

---

## 6. Stream Buffering

A critical requirement across all modes: QA must never miss stream
output, even if it spawns after the Apple Engineer starts working.

The daemon records all bridge output from the moment an Apple Engineer
session starts. When QA runs `byfrost attach`, the `session.output`
message returns historical output first, then transitions to live
streaming. QA receives everything from the start of the session
regardless of when it attaches.

This eliminates spawn timing gaps. The Apple Engineer's plan, initial
file reads, and early implementation are all captured even if QA takes
15-30 seconds to launch and attach.

---

## 7. Cross-Machine Dispatch

Agent Teams messaging is local-only. It works between Claude Code
instances on the same machine. Agents on different machines cannot
message each other through Agent Teams.

All cross-machine communication flows through bridge-synced files:

| Direction | Mechanism |
|---|---|
| Controller to Mac | Write task file locally, bridge syncs to Mac, daemon spawns session |
| Mac to Controller | Write task file locally, bridge syncs to controller, QA detects in stream and dispatches via Agent Teams |
| Code (both directions) | Bridge file sync (real-time, changed files only) |
| GitHub | PM commits and pushes from controller only |

The bridge is the only channel between machines. No agent communicates
across the bridge via Agent Teams, SSH, or any other mechanism.

### Why Bridge-Synced Files

Files are the medium for cross-machine dispatch because:

1. **Durable.** Files persist. If an agent crashes before reading a
   task, the file is still there when the replacement spawns.
2. **Inspectable.** QA can read any task file. The developer can
   read any task file. Everything is visible.
3. **Version-controllable.** Task files live in the project directory.
   PM commits them to git as part of the project history.
4. **Simple.** Writing a file is something every agent already knows
   how to do. No special API, no message broker, no queue.

---

## 8. Git Model

Git is for version history and GitHub backup, not transport. The
bridge handles all file transport between machines.

### Controller Owns Git

Only the controller commits and pushes to GitHub. PM handles all git
operations. No other agent on either machine needs git commands. The
worker never talks to GitHub directly.

### Initial Codebase Transfer

The worker receives the initial codebase via git bundle sent over the
bridge during `byfrost init`:

1. Controller has the repo (already cloned from GitHub)
2. Controller runs `git bundle create repo.bundle --all`
3. Bundle is sent over the bridge as a binary transfer
   (`project.bundle` message)
4. Worker runs `git clone repo.bundle` and has full repo with history
5. From this point, bridge file sync keeps both machines current

The worker never needs GitHub credentials.

### Parity Validation

Before PM pushes to GitHub, QA validates that both machines have
identical file state:

1. Apple Engineer generates checksum manifest on the Mac
2. Manifest is sent to controller via bridge
3. QA compares against controller's local files
4. Match = PM pushes. Mismatch = QA flags, PM resolves.

### Fallback

If the bridge disconnects long-term, the developer manually
configures GitHub access on the worker as a recovery step. This is
the exception, not the normal flow.

---

*End of Workflow Dynamics*
