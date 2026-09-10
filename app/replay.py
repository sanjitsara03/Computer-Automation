"""Deterministic replay: execute a recorded artifact with no model in the loop."""

from __future__ import annotations

import argparse
import re
import time

from playwright.sync_api import Page, expect, sync_playwright

from app.resolve import resolve
from app.schema import (
    Artifact,
    Checkpoint,
    LiteralValue,
    RoleVisible,
    Step,
    Strict,
    TextMatches,
    UrlMatches,
    Value,
    ValueMatches,
    load_artifact,
)

#result contract
class StepResult(Strict):
    id: str
    action: str
    ok: bool
    ms: int
    tier: str | None = None

class ReplayResult(Strict):
    ok: bool
    capability: str
    steps: list[StepResult]
    outputs: dict[str, str] = {}
    failure: str | None = None

#session
class Session:
    """Owns the live browser session so a human can take it over later."""

    def __init__(self, base_url: str, headless: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.page: Page | None = None
        self._pw = None
        self._browser = None

    def __enter__(self) -> "Session":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.page = self._browser.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


#execution
def bind_value(value: Value) -> str:
    if isinstance(value, LiteralValue):
        return value.literal
    raise NotImplementedError("param and secret binding not implemented yet")


def assert_checkpoint(
    page: Page,
    checkpoint: Checkpoint,
    timeout_ms: int,
    locator=None,
    value: str | None = None,
) -> None:
    if isinstance(checkpoint, RoleVisible):
        expect(page.get_by_role(checkpoint.role, name=checkpoint.name)).to_be_visible(
            timeout=timeout_ms
        )
    elif isinstance(checkpoint, TextMatches):
        expect(page.locator("body")).to_contain_text(
            re.compile(checkpoint.pattern), timeout=timeout_ms
        )
    elif isinstance(checkpoint, UrlMatches):
        expect(page).to_have_url(re.compile(checkpoint.pattern), timeout=timeout_ms)
    elif isinstance(checkpoint, ValueMatches):
        if locator is None or value is None:
            raise ValueError("value_matches requires the acted-on locator and value")
        expect(locator).to_have_value(value, timeout=timeout_ms)
    else:
        raise NotImplementedError(f"checkpoint not implemented: {type(checkpoint).__name__}")


def run_step(session: Session, step: Step) -> str | None:
    tier = None
    locator = None
    value = None
    if step.action == "navigate":
        session.page.goto(session.base_url + step.path)
    elif step.action == "click":
        locator, tier = resolve(session.page, step.target)
        locator.click()
    elif step.action == "type":
        locator, tier = resolve(session.page, step.target)
        value = bind_value(step.value)
        locator.fill(value)
    else:
        raise NotImplementedError(f"action not implemented yet: {step.action}")
    assert_checkpoint(session.page, step.checkpoint, step.timeout_ms, locator, value)
    return tier


def replay(artifact: Artifact, session: Session) -> ReplayResult:
    results: list[StepResult] = []
    for step in artifact.steps:
        started = time.monotonic()
        try:
            tier = run_step(session, step)
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            results.append(StepResult(id=step.id, action=step.action, ok=False, ms=elapsed))
            return ReplayResult(
                ok=False,
                capability=artifact.name,
                steps=results,
                failure=f"step {step.id} ({step.action}): {type(exc).__name__}: {exc}",
            )
        elapsed = int((time.monotonic() - started) * 1000)
        results.append(
            StepResult(id=step.id, action=step.action, ok=True, ms=elapsed, tier=tier)
        )
    return ReplayResult(ok=True, capability=artifact.name, steps=results)


def main() -> None:
    parser = argparse.ArgumentParser(description="replay an artifact")
    parser.add_argument("artifact")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    artifact = load_artifact(args.artifact)
    with Session(args.base_url, headless=not args.headed) as session:
        result = replay(artifact, session)
        if args.headed:
            input("press Enter to close the browser ")
    print(result.model_dump_json(indent=2))
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
