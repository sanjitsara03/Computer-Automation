"""Discovery: an LLM figures out the flow once. v4: typed actions, schema-enforced replies."""

from __future__ import annotations

import json
import re
from typing import Literal

from openai import OpenAI
from playwright.sync_api import Page, sync_playwright
from pydantic import model_validator

from app.replay import load_env
from app.schema import SecretRef, Strict

MODEL = "openai/gpt-5.6-terra"
BASE_URL = "http://localhost:8080"
MAX_STEPS = 15
GOAL = (
    "Log into ParaBank, open a new SAVINGS account funded from account 12345, "
    "and report the new account number."
)


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
  then "done"."""


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

        messages = [{
            "role": "user",
            "content": f'Your goal, driving a bank web app:\n"{GOAL}"\n\n'
                       f"{ACTION_RULES}\n\nCurrent page snapshot:\n{take_snapshot(page)}",
        }]

        for step in range(1, MAX_STEPS + 1):
            raw = ask_model(client, messages)
            try:
                action = parse_action(raw)
            except Exception as exc:
                reason = str(exc).splitlines()[0]
                print(f"[{step}] invalid reply, sent back for correction: {reason}")
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Your action was invalid: {reason}. "
                               "Send ONE corrected JSON action with all required fields.",
                })
                continue
            print(f"[{step}] {action.model_dump_json(exclude_none=True)}")
            if action.action in ("done", "stuck"):
                break
            try:
                result = execute(page, action, secrets)
            except Exception as exc:
                result = f"ACTION FAILED: {type(exc).__name__}: {str(exc).splitlines()[0]}"
            print(f"    -> {result}")
            page.wait_for_timeout(600)
            messages.append(
                {"role": "assistant", "content": action.model_dump_json(exclude_none=True)}
            )
            messages.append({
                "role": "user",
                "content": f"result: {result}\n\nCurrent page snapshot:\n{take_snapshot(page)}",
            })
        else:
            print(f"stopped: hit MAX_STEPS ({MAX_STEPS})")

        input("browser stays open, press Enter to close ")
        browser.close()


if __name__ == "__main__":
    main()
