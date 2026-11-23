"""Counts how consistent the CSS is: distinct font sizes, border radii and hard-coded colours outside the token blocks.

Usage: .venv/Scripts/python.exe scripts/css_stats.py
"""
import re
from pathlib import Path

CSS = sorted(Path("frontend/css").glob("*.css"))
TOKEN_FILE = "tokens.css"  # colours defined there are the palette, not hard-coded


def strip_comments(s: str) -> str:
    return re.sub(r"/\*.*?\*/", "", s, flags=re.S)


sizes, radii, colours = {}, {}, {}
for f in CSS:
    s = strip_comments(f.read_text(encoding="utf-8"))
    for m in re.finditer(r"font-size\s*:\s*([^;}]+)", s):
        sizes[m.group(1).strip()] = sizes.get(m.group(1).strip(), 0) + 1
    for m in re.finditer(r"(?<![\w-])font\s*:\s*([^;}]+)", s):
        mm = re.search(r"(\d+(?:\.\d+)?px|var\(--fs-[\w-]+\))", m.group(1))
        if mm:
            sizes[mm.group(1)] = sizes.get(mm.group(1), 0) + 1
    for m in re.finditer(r"border(?:-(?:top|bottom)-(?:left|right))?-radius\s*:\s*([^;}]+)", s):
        for v in m.group(1).split():
            radii[v] = radii.get(v, 0) + 1
    if f.name == TOKEN_FILE:
        continue
    for m in re.finditer(r"#[0-9a-fA-F]{3,8}\b|rgba?\(\s*\d[^)]*\)", s):
        colours[m.group(0).lower()] = colours.get(m.group(0).lower(), 0) + 1

print(f"font sizes: {len(sizes)} distinct  {sorted(sizes, key=lambda k: -sizes[k])}")
print(f"radii: {len(radii)} distinct  {sorted(radii, key=lambda k: -radii[k])}")
print(f"hard-coded colours (outside tokens.css): {len(colours)} distinct, {sum(colours.values())} uses")
