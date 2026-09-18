# record-replay-agent

An LLM drives a real banking UI once to accomplish a goal. The run is recorded as a typed,
parameterized capability artifact. The artifact replays deterministically afterward — no model
in the loop — with typed inputs, typed outputs, checkpoints, an error taxonomy, a policy
allowlist, and human escalation on the live session.

Target app: [ParaBank](https://github.com/parasoft/parabank), a real legacy vendor demo app
(server-rendered JSP, unlabeled inputs, session tokens in URLs), self-hosted in Docker so
state is resettable and runs are deterministic.

## Setup

```bash
# 1. the target app (resets to seed data on demand)
docker run -d -p 8080:8080 -p 61616:61616 -p 9001:9001 --name parabank parasoft/parabank

# 2. python deps + browser
uv sync
uv run playwright install chromium

# 3. credentials and model key -> .env (gitignored)
cat > .env <<'ENV'
SECRET_USERNAME=john
SECRET_PASSWORD=demo
OPENROUTER_API=sk-or-...your key...
ENV
```

The OpenRouter key is needed **only for discovery**. Replay runs with no model and no key.

Reset the bank to seed data any time:

```bash
curl -X POST http://localhost:8080/parabank/services/bank/initializeDB
```

## Demo path

**1. Discovery** — GPT-5.6 Terra figures out the flow in a visible browser, one action per
turn; every acted-on element is captured as a three-tier target descriptor; the run is saved
as a schema-validated artifact plus a full transcript in `evidence/`:

```bash
uv run python -m app.discover
```

**2. Deterministic replay** — the artifact, different parameters, no model. Exit codes:
0 success, 2 known business outcome, 1 hard failure.

```bash
uv run python -m app.replay artifacts/open_new_account.json \
  --param account_type=CHECKING --param funding_account_id=12456 --unattended
```

**3. Error taxonomy** — a known "no" vs. an unknown break:

```bash
# business outcome: login_failed (a declared detector recognizes the page)
SECRET_PASSWORD=nope uv run python -m app.replay artifacts/open_new_account.json \
  --param account_type=CHECKING --param funding_account_id=12456 --unattended

# hard failure: bogus funding account -> expected/observed + screenshot in evidence/
uv run python -m app.replay artifacts/open_new_account.json \
  --param account_type=CHECKING --param funding_account_id=99999 --unattended
```

**4. Human escalation** — the submit step is marked `irreversible`; without `--unattended`
the run pauses there and hands you the live browser (`--headed` to watch). Answer `approve`,
or click the button yourself and answer `fixed` — the engine verifies your work against the
step's checkpoint before resuming:

```bash
uv run python -m app.replay artifacts/open_new_account.json \
  --param account_type=SAVINGS --param funding_account_id=12456 --headed
```

**5. Cross-tenant** — the same artifact against Parasoft's public instance (a different
"tenant" of the same vendor product; only a parameter changes):

```bash
uv run python -m app.replay artifacts/open_new_account.json \
  --base-url https://parabank.parasoft.com \
  --param account_type=SAVINGS --param funding_account_id=13344 --unattended
```

## Layout

```
app/schema.py     artifact schema: the typed capability contract (Pydantic)
app/discover.py   LLM discovery loop (OpenRouter, one structured action per turn)
app/capture.py    ref -> three-tier target descriptor (a11y name, DOM label, geometry)
app/replay.py     deterministic replay engine, error taxonomy, operator triggers
app/resolve.py    descriptor -> element: tier ladder, fail on ambiguity
app/operator.py   escalation channel: typed request/outcome, terminal operator
app/policy.py     allowlist: origins, actions, risk handling
policy.json       the allowlist itself
artifacts/        recorded capabilities
evidence/         discovery transcript, replay results, failure screenshots
```

Design write-up: [REPORT.md](REPORT.md)
