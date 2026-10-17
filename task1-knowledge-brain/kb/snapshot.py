"""Write a self-contained copy of the knowledge base, for seeding a fresh install.

`VACUUM INTO` produces one consistent file even while the service is running. Copying
knowledge.db directly is unsafe: in WAL mode recent writes live in a separate -wal file.

Usage: uv run python -m kb.snapshot [out_path]   (default data/snapshot/knowledge.db)
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from kb.bootstrap import article_count
from kb.config import settings


def make_snapshot(db_path: Path, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    with closing(sqlite3.connect(db_path)) as c:
        c.execute("VACUUM INTO ?", (str(out),))
    return article_count(out)


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/snapshot/knowledge.db")
    n = make_snapshot(settings.db_path, target)
    print(f"wrote {target} ({target.stat().st_size / 1e6:.1f} MB, {n} articles)")
