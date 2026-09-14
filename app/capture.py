"""Descriptor capture: turn a ref (live element) into a three-tier TargetDescriptor."""

from __future__ import annotations

import re

from playwright.sync_api import Page, sync_playwright

from app.schema import FallbackTarget, LastTarget, PrimaryTarget, TargetDescriptor

_GENERATED_ID = re.compile(r"ctl\d+|\$ctl|^__|j_id|gwt-uid|:r[a-z0-9]+:|ember\d+|^radix-")
_VOLATILE = re.compile(r"^[\d\s.,$-]+$")


def _snapshot_line(snapshot: str, ref: str) -> tuple[str, str, str]:
    """Return (role, name, anchor_text) for a ref by reading its snapshot line."""
    lines = snapshot.splitlines()
    for i, line in enumerate(lines):
        if f"[ref={ref}]" not in line:
            continue
        m = re.search(r"-\s*'?([a-z]+)(?:\s+\"([^\"]*)\")?", line)
        role = m.group(1) if m else ""
        name = (m.group(2) or "") if m else ""
        #anchor: the element's own name, else the nearest stable text on a previous line
        anchor = name if name and not _VOLATILE.match(name) else ""
        if not anchor:
            for prev in reversed(lines[:i]):
                text = prev.split(":", 1)[1].strip() if ":" in prev else ""
                if text and not _VOLATILE.match(text):
                    anchor = text
                    break
        return role, name, anchor
    raise ValueError(f"ref {ref} not in snapshot")


def capture_target(page: Page, ref: str, snapshot: str) -> TargetDescriptor:
    el = page.locator(f"aria-ref={ref}")
    role, name, anchor = _snapshot_line(snapshot, ref)

    #tier 1: visible role+name, kept only if unique on the page
    primary = None
    reason = None
    if not name:
        reason = "empty_name"
    elif _VOLATILE.match(name):
        #per-run data (account numbers, amounts) must never become a descriptor
        reason = "volatile_name"
    else:
        n = page.get_by_role(role, name=name, exact=True).count()
        if n == 1:
            primary = PrimaryTarget(role=role, name=name)
        else:
            reason = f"ambiguous:{n}"

    #tier 2: hidden DOM labels, first candidate that matches exactly one element
    attrs = el.evaluate(
        "el => ({tag: el.tagName.toLowerCase(), id: el.id,"
        " name: el.getAttribute('name'), value: el.getAttribute('value'),"
        " href: el.getAttribute('href')})"
    )
    candidates = []
    if attrs["id"]:
        candidates.append(f"#{attrs['id']}")
    if attrs["name"]:
        candidates.append(f"{attrs['tag']}[name='{attrs['name']}']")
    if attrs["value"]:
        candidates.append(f"{attrs['tag']}[value='{attrs['value']}']")
    if attrs["href"]:
        #link address, stripped of session tokens and query noise
        path = attrs["href"].split(";")[0].split("?")[0]
        if path:
            candidates.append(f"a[href*='{path}']")
    fallback = None
    for css in candidates:
        if page.locator(css).count() == 1:
            kind = "generated" if _GENERATED_ID.search(css) else "attribute"
            fallback = FallbackTarget(css=css, kind=kind)
            break

    #tier 3: geometry, always recorded when the element is visible
    last = None
    box = el.bounding_box()
    if box and page.viewport_size:
        last = LastTarget(
            point=(int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)),
            viewport=(page.viewport_size["width"], page.viewport_size["height"]),
            anchor_text=anchor,
        )

    if primary:
        return TargetDescriptor(primary=primary, fallback=fallback, last=last)
    return TargetDescriptor(primary_null_reason=reason, fallback=fallback, last=last)


def main() -> None:
    """Demo on the local ParaBank login page, refs read from a fresh snapshot."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto("http://localhost:8080/parabank/index.htm")
        snapshot = page.aria_snapshot(mode="ai")

        username_ref = re.search(r"textbox(?: \[active\])? \[ref=(\w+)\]", snapshot).group(1)
        login_ref = re.search(r'button "Log In" \[ref=(\w+)\]', snapshot).group(1)

        for label, ref in [("username box", username_ref), ("Log In button", login_ref)]:
            t = capture_target(page, ref, snapshot)
            print(f"{label} ({ref}):")
            print("  ", t.model_dump_json(exclude_none=True), "\n")
        browser.close()


if __name__ == "__main__":
    main()
