"""Discovery: an LLM figures out the flow once. v2: one model turn, structured action, no acting."""

from __future__ import annotations

import json
import re

from openai import OpenAI
from playwright.sync_api import sync_playwright

from app.replay import load_env

MODEL = "openai/gpt-5.6-terra"
BASE_URL = "http://localhost:8080"
GOAL = (
    "Log into ParaBank, open a new SAVINGS account funded from account 12345, "
    "and report the new account number."
)


def take_snapshot(page) -> str:
    snapshot = page.aria_snapshot(mode="ai")
    #strip session tokens before the text leaves this machine
    return re.sub(r";jsessionid=[^\s\"'?#&]+", "", snapshot)


ACTION_FORMAT = """Respond with ONLY a JSON object, no other text. One of:
{"action": "navigate", "path": "/some/path", "why": "..."}
{"action": "type", "ref": "e12", "value": "text to type", "why": "..."}
{"action": "select", "ref": "e12", "value": "visible option text", "why": "..."}
{"action": "click", "ref": "e12", "why": "..."}
{"action": "extract", "ref": "e12", "output": "name_of_value", "why": "..."}
{"action": "done", "summary": "..."}
{"action": "stuck", "reason": "..."}

Never invent usernames or passwords. For credential fields use the placeholder
{"secret": "username"} or {"secret": "password"} as the value, e.g.
{"action": "type", "ref": "e12", "value": {"secret": "username"}, "why": "..."}"""


def ask_model(client: OpenAI, goal: str, snapshot: str) -> dict:
    prompt = f"""You are driving a bank web app to accomplish this goal:
"{goal}"

Below is an accessibility snapshot of the current page. Elements have [ref=...] ids.
Decide the SINGLE next action.

{ACTION_FORMAT}

{snapshot}"""
    r = client.chat.completions.create(
        model=MODEL,
        max_tokens=300,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    print(f"[tokens: {r.usage.prompt_tokens} in / {r.usage.completion_tokens} out]")
    text = r.choices[0].message.content.strip()
    text = re.sub(r"^```(json)?|```$", "", text).strip()
    return json.loads(text)


def main() -> None:
    env = load_env()
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=env["OPENROUTER_API"])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(BASE_URL + "/parabank/index.htm")
        snapshot = take_snapshot(page)
        browser.close()
    action = ask_model(client, GOAL, snapshot)
    print("parsed action:", json.dumps(action, indent=2))


if __name__ == "__main__":
    main()
