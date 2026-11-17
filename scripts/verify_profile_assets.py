#!/usr/bin/env python3
"""Validate profile SVG assets and render responsive/reduced-motion previews.

Usage:
  python -m pip install playwright
  python -m playwright install chromium
  python scripts/verify_profile_assets.py

Screenshots are written to artifacts/profile-svg/ (32 PNG files).
Each screenshot attempt has a 30-second timeout and one retry.
"""
from __future__ import annotations

import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "assets"
OUTPUT_DIR = ROOT / "artifacts" / "profile-svg"
MAX_BYTES = 60 * 1024
VIEWPORTS = (375, 1440)
MOTION_MODES = ("no-preference", "reduce")
TIMEOUT_MS = 30_000

FORBIDDEN_ELEMENTS = re.compile(r"<\s*(script|foreignObject)\b", re.IGNORECASE)
REMOTE_FONT = re.compile(r"@import|fonts\.googleapis|fonts\.gstatic|typekit", re.IGNORECASE)
EXTERNAL_REFERENCE = re.compile(
    r"""(?:href|src)\s*=\s*["']https?://|url\(\s*["']?https?://""",
    re.IGNORECASE,
)

def luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    def linear(value: float) -> float:
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (linear(value) for value in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue

def contrast(foreground: str, background: str) -> float:
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)

def validate_assets() -> list[Path]:
    files = sorted(ASSET_DIR.rglob("*.svg"))
    if not files:
        raise RuntimeError("No SVG files found under assets/")
    total = 0
    for path in files:
        raw = path.read_bytes()
        total += len(raw)
        if len(raw) >= MAX_BYTES:
            raise ValueError(f"{path.relative_to(ROOT)} exceeds 60 KB")
        text = raw.decode("utf-8")
        ET.fromstring(text)
        if FORBIDDEN_ELEMENTS.search(text):
            raise ValueError(f"Forbidden SVG element in {path.relative_to(ROOT)}")
        if REMOTE_FONT.search(text):
            raise ValueError(f"Remote font or @import in {path.relative_to(ROOT)}")
        if EXTERNAL_REFERENCE.search(text):
            raise ValueError(f"External image/reference in {path.relative_to(ROOT)}")
        if "prefers-reduced-motion: reduce" not in text:
            raise ValueError(f"Missing reduced-motion rule in {path.relative_to(ROOT)}")
    print(f"XML/security/reduced-motion: PASS ({len(files)} SVGs; {total:,} bytes total)")
    for color in ("#F9FAFB", "#F59E0B", "#E5E7EB", "#D1D5DB", "#818CF8"):
        value = contrast(color, "#0D1117")
        print(f"Contrast {color} on #0D1117: {value:.2f}:1")
        if value < 4.5:
            raise ValueError(f"WCAG AA contrast failure for {color}")
    return files

def render(files: list[Path]) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    completed = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            for asset in files:
                relative = asset.relative_to(ROOT).as_posix()
                for width in VIEWPORTS:
                    for motion in MOTION_MODES:
                        destination = OUTPUT_DIR / (
                            f"{asset.stem}-{width}-{motion}.png"
                        )
                        html_path = OUTPUT_DIR / "preview.html"
                        html_path.write_text(
                            "<!doctype html><html><head><meta charset='utf-8'>"
                            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                            "<style>html,body{margin:0;background:#0D1117}"
                            "img{display:block;width:100%;height:auto}</style></head>"
                            f"<body><img id='asset' src='{asset.resolve().as_uri()}' "
                            "alt='SVG preview'></body></html>",
                            encoding="utf-8",
                        )
                        success = False
                        last_error = ""
                        for attempt in (1, 2):
                            page = browser.new_page(
                                viewport={"width": width, "height": 900},
                                device_scale_factor=1,
                            )
                            page.set_default_timeout(TIMEOUT_MS)
                            page.emulate_media(
                                reduced_motion="reduce" if motion == "reduce"
                                else "no-preference"
                            )
                            try:
                                page.goto(html_path.as_uri(), wait_until="load",
                                          timeout=TIMEOUT_MS)
                                page.wait_for_function(
                                    "const img=document.querySelector('#asset');"
                                    "img && img.complete && img.naturalWidth > 0",
                                    timeout=TIMEOUT_MS,
                                )
                                page.screenshot(
                                    path=str(destination), full_page=True,
                                    timeout=TIMEOUT_MS,
                                )
                                reduced = page.evaluate(
                                    "matchMedia('(prefers-reduced-motion: reduce)').matches"
                                )
                                expected = motion == "reduce"
                                if reduced != expected:
                                    raise RuntimeError("Reduced-motion emulation mismatch")
                                if expected:
                                    svg_page = browser.new_page(
                                        viewport={"width": width, "height": 900},
                                        device_scale_factor=1,
                                    )
                                    svg_page.set_default_timeout(TIMEOUT_MS)
                                    svg_page.emulate_media(reduced_motion="reduce")
                                    svg_page.goto(asset.resolve().as_uri(), wait_until="load",
                                                  timeout=TIMEOUT_MS)
                                    active_animations = svg_page.evaluate(
                                        "Array.from(document.querySelectorAll('*'))"
                                        ".map(node => getComputedStyle(node).animationName)"
                                        ".filter(name => name !== 'none')"
                                    )
                                    svg_page.close()
                                    if active_animations:
                                        raise RuntimeError(
                                            f"Animations remain active under reduced motion: {active_animations}"
                                        )
                                success = True
                                completed += 1
                                print(f"PASS {relative} {width}px {motion} "
                                      f"(attempt {attempt}) -> {destination}")
                                break
                            except (PlaywrightTimeoutError, Exception) as error:
                                last_error = str(error)
                                if attempt == 1:
                                    time.sleep(1)
                            finally:
                                page.close()
                        if not success:
                            print(f"NOT RUN {relative} {width}px {motion}: {last_error}",
                                  file=sys.stderr)
                            return completed
        finally:
            browser.close()
    return completed

def main() -> int:
    files = validate_assets()
    expected = len(files) * len(VIEWPORTS) * len(MOTION_MODES)
    completed = render(files)
    print(f"Browser captures: {completed}/{expected}")
    return 0 if completed == expected else 2

if __name__ == "__main__":
    raise SystemExit(main())
