"""Deterministic replay: execute a recorded artifact with no model in the loop."""

from __future__ import annotations

import argparse
import os
import re
import time

from playwright.sync_api import Page, expect, sync_playwright

from app.resolve import resolve
from app.schema import (
    Artifact,
    Checkpoint,
    LiteralValue,
    ParamRef,
    RoleVisible,
    SecretRef,
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
def bind_value(value: Value, params: dict[str, str], secrets: dict[str, str]) -> str:
    if isinstance(value, LiteralValue):
        return value.literal
    if isinstance(value, ParamRef):
        if value.param not in params:
            raise KeyError(f"param not provided: {value.param}")
        return params[value.param]
    if isinstance(value, SecretRef):
        if value.secret not in secrets:
            raise KeyError(f"secret not provided: {value.secret}")
        return secrets[value.secret]
    raise NotImplementedError(f"unknown value type: {type(value).__name__}")


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
        tag = locator.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            selected = locator.evaluate("el => el.options[el.selectedIndex].text")
            if selected != value:
                raise AssertionError(f"selected option {selected!r}, expected {value!r}")
        else:
            expect(locator).to_have_value(value, timeout=timeout_ms)
    else:
        raise NotImplementedError(f"checkpoint not implemented: {type(checkpoint).__name__}")


def run_step(
    session: Session, step: Step, params: dict[str, str], secrets: dict[str, str]
) -> tuple[str | None, tuple[str, str] | None]:
    tier = None
    locator = None
    value = None
    extracted = None
    if step.action == "navigate":
        session.page.goto(session.base_url + step.path)
    elif step.action == "click":
        locator, tier = resolve(session.page, step.target)
        locator.click()
    elif step.action == "type":
        locator, tier = resolve(session.page, step.target)
        value = bind_value(step.value, params, secrets)
        locator.fill(value)
    elif step.action == "select":
        locator, tier = resolve(session.page, step.target)
        value = bind_value(step.value, params, secrets)
        locator.select_option(label=value)
    elif step.action == "extract":
        assert_checkpoint(session.page, step.checkpoint, step.timeout_ms)
        locator, tier = resolve(session.page, step.target)
        text = locator.text_content() or ""
        match = re.search(step.parse.pattern, text)
        if match is None:
            raise ValueError(f"parse {step.parse.pattern!r} found nothing in {text!r}")
        return tier, (step.output, match.group(0))
    else:
        raise NotImplementedError(f"action not implemented yet: {step.action}")
    assert_checkpoint(session.page, step.checkpoint, step.timeout_ms, locator, value)
    return tier, extracted


def replay(
    artifact: Artifact,
    session: Session,
    params: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
) -> ReplayResult:
    params = params or {}
    secrets = secrets or {}
    results: list[StepResult] = []
    outputs: dict[str, str] = {}
    for step in artifact.steps:
        started = time.monotonic()
        try:
            tier, extracted = run_step(session, step, params, secrets)
            if extracted is not None:
                outputs[extracted[0]] = extracted[1]
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            results.append(StepResult(id=step.id, action=step.action, ok=False, ms=elapsed))
            failure = f"step {step.id} ({step.action}): {type(exc).__name__}: {exc}"
            for secret_value in secrets.values():
                failure = failure.replace(secret_value, "[secret]")
            return ReplayResult(
                ok=False,
                capability=artifact.name,
                steps=results,
                failure=failure,
            )
        elapsed = int((time.monotonic() - started) * 1000)
        results.append(
            StepResult(id=step.id, action=step.action, ok=True, ms=elapsed, tier=tier)
        )
    return ReplayResult(ok=True, capability=artifact.name, steps=results, outputs=outputs)


def load_env(path: str = ".env") -> dict[str, str]:
    #.env values, falling back to the process environment
    env = dict(os.environ)
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def main() -> None:
    parser = argparse.ArgumentParser(description="replay an artifact")
    parser.add_argument("artifact")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()

    artifact = load_artifact(args.artifact)
    params = dict(p.split("=", 1) for p in args.param)
    secrets = {
        k[len("SECRET_"):].lower(): v
        for k, v in load_env().items()
        if k.startswith("SECRET_")
    }
    with Session(args.base_url, headless=not args.headed) as session:
        result = replay(artifact, session, params, secrets)
        if args.headed:
            input("press Enter to close the browser ")
    print(result.model_dump_json(indent=2))
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
