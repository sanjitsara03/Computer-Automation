"""Discovery: an LLM figures out the flow once. v4: typed actions, schema-enforced replies."""

from __future__ import annotations

import json
import re
from typing import Literal

from openai import OpenAI
from playwright.sync_api import Page, sync_playwright
from pydantic import model_validator

from app.capture import _VOLATILE, capture_target
from app.replay import load_env
from app.schema import Artifact, SecretRef, Strict

MODEL = "openai/gpt-5.6-terra"
BASE_URL = "http://localhost:8080"
MAX_STEPS = 15
ARTIFACT_PATH = "artifacts/open_new_account.json"

#the capability contract, declared before discovery
PARAMS = {"account_type": "SAVINGS", "funding_account_id": "13122"}
OUTPUTS = {"new_account_id": {"type": "str", "pattern": "^\\d+$"}}
GOAL_TEMPLATE = (
    "Log into ParaBank, open a new {account_type} account funded from account "
    "{funding_account_id}, and report the new account number."
)
GOAL = GOAL_TEMPLATE.format(**PARAMS)


#the model's action: a draft step, ref-addressed
_ACTION_SHAPE: dict[str, list[str]] = {
    "navigate": ["path"],
    "type": ["ref", "value"],
    "select": ["ref", "value"],
    "click": ["ref"],
    "extract": ["ref", "output"],
    "done": ["summary"],
    "stuck": ["reason"],
}


class ModelAction(Strict):
    action: Literal["navigate", "type", "select", "click", "extract", "done", "stuck"]
    ref: str | None = None
    path: str | None = None
    value: str | SecretRef | None = None
    output: str | None = None
    summary: str | None = None
    reason: str | None = None
    why: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ModelAction":
        for field in _ACTION_SHAPE[self.action]:
            if getattr(self, field) is None:
                raise ValueError(f"{self.action} requires '{field}'")
        return self


def _strict_schema() -> dict:
    #openrouter strict mode wants every property required and no defaults
    schema = ModelAction.model_json_schema()
    for obj in [schema, *schema.get("$defs", {}).values()]:
        if obj.get("type") == "object":
            obj["required"] = list(obj.get("properties", {}))
            for prop in obj["properties"].values():
                prop.pop("default", None)
    return schema


ACTION_RULES = """You drive the app one action at a time: navigate, type, select, click,
extract (read a value off the page), done (goal accomplished, give summary), or
stuck (cannot proceed, give reason).

Rules:
- Reply with EXACTLY ONE JSON object and nothing after it.
- ONE action per response. refs must come from the CURRENT snapshot.
- Unused fields must be null.
- Never invent usernames or passwords. For credential fields set value to
  {"secret": "username"} or {"secret": "password"}.
- When the goal asks you to report a value, "extract" it from the element showing it,
  then "done". "output" is a NAME for the value (e.g. "new_account_id"), never the
  value itself."""


def take_snapshot(page: Page) -> str:
    snapshot = page.aria_snapshot(mode="ai")
    #strip session tokens before the text leaves this machine
    return re.sub(r";jsessionid=[^\s\"'?#&]+", "", snapshot)


def ask_model(client: OpenAI, messages: list[dict]) -> str:
    r = client.chat.completions.create(
        model=MODEL,
        max_tokens=500,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "action", "strict": True, "schema": _strict_schema()},
        },
        messages=messages,
    )
    print(f"    [tokens: {r.usage.prompt_tokens} in / {r.usage.completion_tokens} out]")
    return r.choices[0].message.content.strip()


def parse_action(text: str) -> ModelAction:
    text = re.sub(r"^```(json)?", "", text).strip()
    #the model sometimes emits more than one object; take the first, ignore the rest
    first, _ = json.JSONDecoder().raw_decode(text)
    return ModelAction.model_validate(first)


def bind(value: str | SecretRef, secrets: dict[str, str]) -> str:
    if isinstance(value, SecretRef):
        return secrets[value.secret]
    return value


def record_value(value: str | SecretRef) -> dict:
    """How a value is written into the artifact: secrets and params as placeholders."""
    if isinstance(value, SecretRef):
        return {"secret": value.secret}
    for param_name, param_value in PARAMS.items():
        if value == param_value:
            return {"param": param_name}
    return {"literal": value}


def record_step(n: int, action: ModelAction, page: Page, snapshot: str) -> dict:
    step: dict = {"id": f"s{n}", "action": action.action}
    if action.action == "navigate":
        step["path"] = action.path
    else:
        step["target"] = capture_target(page, action.ref, snapshot).model_dump(
            exclude_none=True, mode="json"
        )
    if action.value is not None:
        step["value"] = record_value(action.value)
    if action.output is not None:
        step["output"] = action.output
        step["parse"] = {"pattern": OUTPUTS[action.output]["pattern"].strip("^$")}
    return step


def _stable_texts(snapshot: str) -> list[str]:
    texts = []
    for line in snapshot.splitlines():
        if ":" in line:
            text = line.split(":", 1)[1].strip().strip('"')
            if text and not _VOLATILE.match(text):
                texts.append(text)
    return texts


def synthesize_checkpoint(action: ModelAction, draft: dict, before: str, after: str) -> dict:
    """A verifiable 'did it work' condition, derived from what the action changed."""
    if action.action in ("type", "select"):
        return {"kind": "value_matches"}
    if action.action == "extract":
        #gate on label AND value shape, so replay cannot read the field too early
        anchor = draft["target"].get("last", {}).get("anchor_text", "")
        core = OUTPUTS[action.output]["pattern"].strip("^$")
        prefix = re.escape(anchor) + r"\s*" if anchor else ""
        return {"kind": "text_matches", "pattern": prefix + core}
    #click/navigate: prefer a heading that newly appeared, else the first new stable text
    heads = lambda s: re.findall(r'- heading "([^"]+)"', s)
    new_heads = [h for h in heads(after) if h not in heads(before)]
    if new_heads:
        return {"kind": "role_visible", "role": "heading", "name": new_heads[0]}
    seen = set(_stable_texts(before))
    for text in _stable_texts(after):
        if text not in seen:
            return {"kind": "text_matches", "pattern": re.escape(text)}
    return {"kind": "text_matches", "pattern": re.escape(_stable_texts(after)[0])}


def execute(page: Page, action: ModelAction, secrets: dict[str, str]) -> str:
    if action.action == "navigate":
        page.goto(BASE_URL + action.path)
        return f"navigated to {action.path}"
    el = page.locator(f"aria-ref={action.ref}")
    if action.action == "click":
        el.click(timeout=5000)
        return f"clicked {action.ref}"
    if action.action == "type":
        el.fill(bind(action.value, secrets), timeout=5000)
        return f"typed into {action.ref}"
    if action.action == "select":
        el.select_option(label=bind(action.value, secrets), timeout=5000)
        return f"selected {action.value!r} in {action.ref}"
    if action.action == "extract":
        text = (el.text_content(timeout=5000) or "").strip()
        return f"extracted {action.output} = {text!r}"
    return f"unknown action {action.action!r}"


def main() -> None:
    env = load_env()
    secrets = {k[len("SECRET_"):].lower(): v for k, v in env.items() if k.startswith("SECRET_")}
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=env["OPENROUTER_API"])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(BASE_URL + "/parabank/index.htm")

        snapshot = take_snapshot(page)
        messages = [{
            "role": "user",
            "content": f'Your goal, driving a bank web app:\n"{GOAL}"\n\n'
                       f"{ACTION_RULES}\n\nCurrent page snapshot:\n{snapshot}",
        }]
        recorded: list[dict] = []
        finished = False

        for step in range(1, MAX_STEPS + 1):
            raw = ask_model(client, messages)
            try:
                action = parse_action(raw)
            except Exception as exc:
                #keep the lines that name the field, not just the header
                reason = " ".join(l.strip() for l in str(exc).splitlines()[:3])
                print(f"[{step}] invalid reply, sent back for correction: {reason[:120]}")
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Your action was invalid: {reason}. "
                               "Send ONE corrected JSON action with all required fields.",
                })
                continue
            print(f"[{step}] {action.model_dump_json(exclude_none=True)}")
            if action.action in ("done", "stuck"):
                finished = action.action == "done"
                break
            draft = None
            try:
                #capture the target BEFORE acting: acting can change the page
                draft = record_step(len(recorded) + 1, action, page, snapshot)
                result = execute(page, action, secrets)
            except Exception as exc:
                draft = None
                result = f"ACTION FAILED: {type(exc).__name__}: {str(exc).splitlines()[0]}"
            print(f"    -> {result}")
            page.wait_for_timeout(600)
            new_snapshot = take_snapshot(page)
            if draft is not None:
                draft["checkpoint"] = synthesize_checkpoint(action, draft, snapshot, new_snapshot)
                recorded.append(draft)
            snapshot = new_snapshot
            messages.append(
                {"role": "assistant", "content": action.model_dump_json(exclude_none=True)}
            )
            messages.append({
                "role": "user",
                "content": f"result: {result}\n\nCurrent page snapshot:\n{snapshot}",
            })
        else:
            print(f"stopped: hit MAX_STEPS ({MAX_STEPS})")

        print(f"\nrecorded {len(recorded)} steps")
        if finished and recorded:
            artifact = {
                "schema_version": 1,
                "name": "open_new_account",
                "version": 2,
                "goal": GOAL_TEMPLATE,
                "surface": {"kind": "web", "entry_path": "/parabank/index.htm"},
                "params": {
                    "account_type": {"type": "str"},
                    "funding_account_id": {"type": "str"},
                    "username": {"type": "str", "secret": True},
                    "password": {"type": "str", "secret": True},
                },
                "outputs": OUTPUTS,
                "steps": recorded,
            }
            Artifact.model_validate(artifact)
            with open(ARTIFACT_PATH, "w") as f:
                json.dump(artifact, f, indent=2)
            print(f"artifact is schema-valid, saved to {ARTIFACT_PATH}")
        else:
            print("run did not finish cleanly; artifact NOT saved")
            print(json.dumps(recorded, indent=2))
        input("browser stays open, press Enter to close ")
        browser.close()


if __name__ == "__main__":
    main()
