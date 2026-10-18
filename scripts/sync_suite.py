"""Copy the shared suite look (shared/) into every page, between marker comments.

    python scripts/sync_suite.py           # update the pages
    python scripts/sync_suite.py --check   # exit 1 if any page is out of date (used by tests)

Each service is its own Docker image, so each page carries its own copy of the shared
CSS, JS and top bar. This script keeps the copies identical.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = [
    ROOT / "task1-knowledge-brain/kb/ui/index.html",
    ROOT / "task2-inbox-agent/ui/index.html",
    ROOT / "task3-meeting-to-scope/scope/ui/index.html",
    ROOT / "deploy/site/index.html",
]
SHARED = ROOT / "shared"


def blocks() -> dict[str, str]:
    css = (SHARED / "suite.css").read_text(encoding="utf-8").strip()
    js = (SHARED / "suite.js").read_text(encoding="utf-8").strip()
    bar = (SHARED / "suite-bar.html").read_text(encoding="utf-8").strip()
    return {
        "css": f"<style>\n{css}\n</style>",
        "bar": bar,
        "js": f"<script>\n{js}\n</script>",
    }


def render(page: str, parts: dict[str, str]) -> str:
    for name, content in parts.items():
        pattern = re.compile(rf"(<!-- suite:{name} -->)(.*?)(<!-- /suite:{name} -->)", re.S)
        if not pattern.search(page):
            raise SystemExit(f"missing <!-- suite:{name} --> markers")
        page = pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", page)
    return page


def main() -> int:
    check = "--check" in sys.argv
    parts = blocks()
    stale = []
    for path in PAGES:
        old = path.read_text(encoding="utf-8")
        new = render(old, parts)
        if new != old:
            stale.append(path.relative_to(ROOT).as_posix())
            if not check:
                path.write_text(new, encoding="utf-8", newline="\n")
    if check and stale:
        print("Out of date (run python scripts/sync_suite.py):", *stale, sep="\n  ")
        return 1
    print("updated:" if stale else "all pages up to date", *stale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
