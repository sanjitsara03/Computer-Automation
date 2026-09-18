# Design Report

## 1. Architecture

Four stages, one direction of data flow: **discover → capture → artifact → replay**.

Discovery (`app/discover.py`) is a plain loop, no agent framework: send the goal and an
accessibility snapshot of the page to the model, get back exactly one structured action,
execute it, send back the result and a fresh snapshot, repeat until `done`/`stuck`/15 steps.
The model points at elements by snapshot reference ids; it never sees the DOM and cannot
write a selector — its reply is schema-constrained and validated, and invalid actions are
sent back for correction rather than crashing the run. Model: GPT-5.6 Terra via OpenRouter.

Capture (`app/capture.py`) runs before each action executes: it converts the model's pointer
into measured facts about the element (see §2). Only actions that actually succeeded are
recorded. The recorder — not the model — assembles the artifact, synthesizes checkpoints from
observed page changes, and substitutes placeholders for parameter and secret values.

Replay (`app/replay.py`) is a for loop over recorded steps: resolve target, act, assert
checkpoint. No model, no improvisation; the interesting machinery is the error taxonomy (§3)
and the operator handoff (§5). The browser session is owned by a `Session` object rather than
the replay function precisely so a human can take it over mid-run.

Target choice: a **self-hosted ParaBank** (Parasoft's demo bank) in Docker. It is a real
vendor application we did not write — server-rendered JSP, unlabeled inputs, `;jsessionid`
tokens in every URL — which answers "you built the target so of course it works" while
staying deterministic: our own database, reset to seed data via one HTTP call. Parasoft's
public instance serves as a second "tenant" of the same product (§4). We deliberately ignore
ParaBank's REST API: the brief scopes this system to apps that have none, and ParaBank
stands in for those.

Perception is the accessibility tree (`aria_snapshot`), not screenshots: ~2K tokens per
observation instead of images, keyed to what a human sees (survives restyling), and the same
kind of tree exists on desktop apps, which is the heterogeneity story. Trade-off accepted:
a surface with a broken accessibility tree would need a screenshot/OCR observation source;
the loop's contract (snapshot in, pointer out) would not change.

## 2. Artifact schema

The artifact is a typed capability contract (`app/schema.py`, Pydantic, every model rejects
unknown fields): name, version, a goal template, typed parameters (secrets flagged), typed
outputs with value patterns, and ordered steps. The contract is **declared before discovery**
by a human — never inferred by the model, because a missed inference bakes a literal into the
artifact and fails silently on other inputs. After recording, validation reconciles contract
against behavior: every declared parameter must be used by a step, every declared output must
have an extraction step, ambiguous descriptors and uncompilable patterns are rejected at load
time rather than discovered mid-replay.

Each step's target is a **three-tier descriptor**, all tiers captured at discovery time from
different views of the same element:

- `primary` — role + visible name from the accessibility tree, recorded only if it matches
  exactly one element; otherwise recorded as null **with a machine-readable reason**
  (`empty_name`, `ambiguous:2`, `volatile_name`).
- `fallback` — a CSS address built from hidden DOM labels (id, name, value, href), kept only
  if it uniquely matches, and classified by trustworthiness (`attribute` vs `generated` for
  machine-minted ids like `ctl00$...`).
- `last` — geometry plus stable anchor text, the last resort.

Values are never raw: `{"param": name}`, `{"secret": name}`, or `{"literal": ...}`. Secrets
exist in the artifact and the model's context only as placeholders; real values are injected
from the environment at the keystroke. Steps also carry a `checkpoint` (§3), optional
`on_fail` detectors (§3), and a `risk` class (§5) — both of the latter added by a human
reviewing the artifact, which is the intended workflow: the model records, a person curates.

## 3. Determinism & error handling

Determinism is manufactured at record time and enforced at replay time.

**Resolution** (`app/resolve.py`): walk the tiers in order; at each tier count matches.
Exactly one wins; zero falls through to the next tier; more than one raises — ambiguity is
never a tiebreak, at capture or at replay, because a wrong-target write that reports success
is strictly worse than a failed run. Which tier resolved is logged per step: an artifact whose
steps drift from `primary` to `fallback` still works but is visibly degrading — a free drift
signal on every run.

**Waiting**: there are no sleeps. Checkpoints compile to auto-retrying assertions, so each
step proceeds the moment its proof is true and fails cleanly at its own `timeout_ms`.
Checkpoints assert stable predicates ("Account Opened!" heading visible), never per-run
values — the new account number is an extracted output, which is what keeps a write flow
replayable. Checkpoints are synthesized by the recorder from observed change (new heading,
else first new stable text), with a volatility filter so a per-run number can never become a
checkpoint; the extract step's checkpoint requires label *and* value shape, closing a real
race we hit where a static label rendered before the dynamic value.

**Runtime errors** are split three ways, and the split is data, not engine guesswork:

- *Business outcome* — a step carries human-declared detectors ("page says 'could not be
  verified' → `login_failed`"). On failure, detectors are checked first; a match returns a
  typed outcome, exit code 2 — an answer, not a crash.
- *Recoverable* — transient slowness is absorbed by the retrying checkpoints.
- *Hard failure* — anything undeclared: expected (the checkpoint in words), observed (error,
  URL, headings, secrets scrubbed), a screenshot into `evidence/`, exit code 1. Unknown
  situations are reported, never interpreted — interpretation would put a model back on the
  replay path. The detector list grows by curation: triage a hard failure once, add one line,
  and that failure becomes a named outcome forever.

UI drift is the secondary concern (these apps change slowly): tier fallthrough absorbs
single-tier breakage, the tier log surfaces it, and `generated`-kind selectors are flagged as
the ones that will rot first.

## 4. Heterogeneity & multi-tenant

**Surface seam.** The artifact stores what an element *is* (role + name), how its surface
addresses it (a CSS string tagged with its trust class), and where it sat — nothing
browser-shaped beyond the one `fallback.css` string, which is itself data with a `kind`. The
model's contract is surface-neutral: snapshot text in, opaque pointer out. A desktop surface
via Windows UI Automation supplies the same fields from the same three views — ControlType/
Name for `primary`, AutomationId (with the same generated-id screen) for `fallback`,
BoundingRectangle for `last`, UIA's tree as the snapshot. All Playwright-touching code lives
in three small modules (`capture`, `resolve`, the execute paths); a `DesktopSurface` would
reimplement those and leave schema, recorder logic, policy, taxonomy, and operator flow
untouched. We did not build a formal Surface interface for one implementation — the seam is
documented here rather than abstracted prematurely.

**Multi-tenant.** Demonstrated, not just designed: the artifact stores no origin (paths
only; the base URL is supplied at invocation and checked against the allowlist), so the
artifact recorded on our container was replayed unchanged against Parasoft's public instance
— a different server, different session tokens, different data. Structure transferred (every
descriptor resolved, at the same tiers); data did not (that tenant lacks account 12345), and
the run failed *cleanly* at the funding-account step — then succeeded end to end once the
parameter was changed to an account that exists there (`evidence/replay_public_instance*.json`).
That is the tenant model: shared structure lives in the artifact, per-tenant data lives in
parameters, and per-tenant structural overrides would be a patch layer over a base artifact
rather than a re-recording. Version drift shows up as tier fallthrough and checkpoint
failures — detectable per step, per tenant, from the replay logs alone.

## 5. Escalation & handoff

"Stuck" has three concrete triggers: a hard failure at replay (unknown situation), a step
whose risk class requires a decision (before it runs), and the discovery agent declaring
`stuck`. All three produce a typed `InterventionRequest` — capability, step, reason,
expected/observed, screenshot — through an `OperatorChannel` protocol (`app/operator.py`).

The v1 operator surface is the terminal, deliberately: the *mechanism* is real and the pixels
are mocked, which is the seam the brief allows. The browser session is owned by a `Session`
object, discovery and replay both run it headed on demand, so when the engine pauses the
human is looking at — and can drive — **the same live session**, not a fresh one. Outcomes:
`approve` (engine performs the risky step), `abort` (clean stop), or `fixed` — the human
performed the step manually, and the engine then **verifies the human's work against the
step's recorded checkpoint** before resuming, logging the step with `tier: "human"`. Every
decision, including unattended pre-approvals, lands in an `interventions` audit list in the
result. A web console would implement the same one-method protocol; nothing else changes.

Unattended mode is explicit: `--unattended` is the caller's standing approval for
`require_confirmation` steps and is recorded as such. With no operator and no pre-approval,
a risky step refuses to run at all.

## 6. Safety

`policy.json` is the allowlist: permitted origins (checked before the browser launches, and
re-checked in discovery so the model cannot steer off-origin — absolute navigation URLs are
rejected outright), permitted action types (enforced per step in both engines), and risk
handling per class: `safe → allow`, `irreversible → require_confirmation`,
`destructive → block`, where block cannot be overridden by any approval. Risk classes are
assigned by a human during artifact review — deliberately: "is this irreversible?" is a
judgment a person must own in a bank, and the model's guess must not silently carry it.

Secrets never exist in the artifact, the model's context, the transcript, or the logs —
only placeholders do; values are read from the environment at the keystroke, and failure
messages are scrubbed of secret values before they are returned. Session tokens
(`;jsessionid`) are stripped from snapshots before they leave the machine and from recorded
URLs and evidence.

Known limits, stated: failure screenshots can contain on-screen account data (acceptable for
a demo bank; a real deployment needs the redaction pass applied to images or must disable
screenshots); the volatility screen is a heuristic (digits/currency), not a PII classifier;
page content shown to the model necessarily includes the task's own data — the compliance
boundary here is what persists, not what the model reads; and steps default to `safe`, so an
unreviewed artifact must not be granted `--unattended` — the missing draft→approved gate is
listed in §7.

## 7. Cuts

Cut deliberately, each behind a clean seam, roughly in the order I would build them next:

- **Approval lifecycle** (draft → approved): artifacts fresh from discovery should be barred
  from `--unattended` until a human review stamps them. The audit and risk machinery exist;
  the state field and gate do not.
- **Fault-injection proxy**: session-expiry, 500s, and slow responses injected at the network
  boundary of the unmodified vendor app, to demonstrate the remaining taxonomy rows
  (timeout/interstitial) beyond what ParaBank produces natively.
- **Second capability + catalog** (`request_loan`, approved/denied both being legitimate
  business outcomes): would make the capability catalog non-degenerate and exercise the
  schema's enum parameters.
- **Tier-3 execution**: geometry descriptors are recorded but replay does not yet execute
  coordinate clicks (it raises honestly instead); needs viewport pinning and anchor
  verification before the click, per the recorded viewport and anchor text.
- **Confidence scoring**: the inputs are already logged (tier per step per run, selector
  kind); the scorer and its gate on unattended replay are not built.
- **Assisted fallback**: on hard failure, one bounded, policy-checked model call to classify
  the unknown screen — recorded as evidence, never silently resuming.
- **Model-proposed curation**: the model suggesting risk labels and checkpoints at discovery
  time for the human to confirm, rather than review starting from nothing.
- **Operator web console**: the `OperatorChannel` protocol is the seam; the terminal
  implementation is the mock.
