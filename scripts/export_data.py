"""Owner-only ZIP export. Run on the Docker host with sudo python3 scripts/export_data.py --docker.

Uses SQLite backups, never raw copies of databases with live WAL files. No network
requests or AI calls are made. The ZIP contains private data: do not publish it.
"""

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SERVICES = {
    "knowledge-brain": "task1-knowledge-brain",
    "inbox-agent": "task2-inbox-agent",
    "scope-builder": "task3-meeting-to-scope",
}


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def json_value(value):
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def export_data(roots, output, extras=()):
    output = output.resolve()
    if output.exists():
        raise ValueError("Output already exists; choose a new filename")
    for root in roots.values():
        if not root.is_dir():
            raise ValueError(f"Missing data directory: {root}")
        if output.is_relative_to(root.resolve()):
            raise ValueError("Output must be outside the exported data directories")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "consistency": "Each SQLite database is a consistent online backup; tasks are captured sequentially.",
        "privacy": "Owner-only archive. May contain private emails, account identifiers and encrypted OAuth credentials. Do not publish.",
        "notes": [
            "Includes all files in the three persistent data directories, including model caches and all account databases.",
            "SQLite WAL/SHM/journal files are replaced by consistent database snapshots.",
            "Images stored as URLs/descriptions remain URLs/descriptions; remote image files and original HTML not stored by the application are not downloaded.",
            "Tables extracted into article text remain in that text. No original spreadsheet is invented.",
            "API keys, environment files, TLS certificates and source code outside the selected data directories are not included.",
            "Database tables also have readable JSONL copies; binary fields use tagged base64 values.",
        ],
        "files": [],
        "databases": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "export.zip"
        with zipfile.ZipFile(
            staging, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:

            def add(path, name):
                digest = hashlib.sha256()
                with (
                    path.open("rb") as source,
                    archive.open(name, "w", force_zip64=True) as dest,
                ):
                    while block := source.read(1024 * 1024):
                        dest.write(block)
                        digest.update(block)
                manifest["files"].append(
                    {
                        "path": name,
                        "bytes": path.stat().st_size,
                        "sha256": digest.hexdigest(),
                    }
                )

            for service, root in roots.items():
                print(f"Exporting {service}...", flush=True)
                for path in sorted(root.rglob("*")):
                    if not path.is_file():
                        continue
                    if not path.resolve().is_relative_to(root.resolve()):
                        raise ValueError(f"Symlink outside data directory: {path}")
                    relative = path.relative_to(root).as_posix()
                    name = f"{service}/data/{relative}"
                    if path.name.endswith(("-wal", "-shm", "-journal")):
                        continue
                    with path.open("rb") as f:
                        is_db = f.read(16) == b"SQLite format 3\x00"
                    if not is_db:
                        add(path, name)
                        continue
                    snapshot = Path(tmp) / "snapshot.db"
                    snapshot.unlink(missing_ok=True)
                    deadline = time.monotonic() + 120

                    def progress(
                        status, remaining, total, deadline=deadline, relative=relative
                    ):
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"Database remained busy: {relative}")

                    with (
                        closing(
                            sqlite3.connect(
                                path.resolve().as_uri() + "?mode=ro", uri=True
                            )
                        ) as source,
                        closing(sqlite3.connect(snapshot)) as target,
                    ):
                        source.backup(target, pages=1024, progress=progress)
                    add(snapshot, name)
                    tables = {}
                    with closing(sqlite3.connect(snapshot)) as db:
                        db.row_factory = sqlite3.Row
                        for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                        ).fetchall():
                            table = row[0]
                            quoted = '"' + table.replace('"', '""') + '"'
                            count = 0
                            with archive.open(
                                f"{service}/tables/{relative}/{table}.jsonl",
                                "w",
                                force_zip64=True,
                            ) as dest:
                                for record in db.execute(f"SELECT * FROM {quoted}"):
                                    dest.write(
                                        (
                                            json.dumps(
                                                {
                                                    k: json_value(v)
                                                    for k, v in dict(record).items()
                                                },
                                                ensure_ascii=False,
                                            )
                                            + "\n"
                                        ).encode("utf-8")
                                    )
                                    count += 1
                            tables[table] = count
                    manifest["databases"].append({"path": name, "tables": tables})
            for prefix, root in extras:
                if not root.is_dir():
                    raise ValueError(f"Missing dataset directory: {root}")
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        add(path, f"{prefix}/{path.relative_to(root).as_posix()}")
            archive.writestr(
                "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False)
            )
            archive.writestr(
                "README.txt",
                "PRIVATE OWNER EXPORT\n\n"
                + "\n".join(manifest["notes"])
                + "\n\n"
                + manifest["consistency"]
                + "\nSee manifest.json for file hashes and database row counts.\n",
            )
        # Exclusive creation avoids overwriting an earlier export.
        with (
            os.fdopen(
                os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as dest,
            staging.open("rb") as source,
        ):
            while block := source.read(1024 * 1024):
                dest.write(block)
    print(f"Created {output} ({output.stat().st_size:,} bytes)", flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Read deployed Compose data volumes; run from the project root on the Docker host",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("exports")
        / f"all-task-data-{datetime.now(UTC):%Y%m%d-%H%M%S}.zip",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        extras = []
        if args.docker:
            roots = {}
            for service in SERVICES:
                container = command(
                    "docker",
                    "compose",
                    "-f",
                    "docker-compose.yml",
                    "-f",
                    "deploy/compose.prod.yml",
                    "ps",
                    "-q",
                    service,
                )
                if not container or "\n" in container:
                    raise ValueError(f"Expected one running container for {service}")
                mounts = json.loads(command("docker", "inspect", container))[0][
                    "Mounts"
                ]
                roots[service] = Path(
                    next(m["Source"] for m in mounts if m["Destination"] == "/data")
                )
                fixture = {
                    "inbox-agent": "/app/data/emails",
                    "scope-builder": "/app/transcripts",
                }.get(service)
                if fixture:
                    dest = Path(tmp) / service
                    subprocess.run(
                        ["docker", "cp", f"{container}:{fixture}", str(dest)],
                        check=True,
                    )
                    extras.append((f"{service}/datasets", dest))
        else:
            repo = Path(__file__).resolve().parent.parent
            roots = {
                service: repo / folder / "data" for service, folder in SERVICES.items()
            }
            extras = [
                ("scope-builder/datasets", repo / "task3-meeting-to-scope/transcripts")
            ]
        export_data(roots, args.output, extras)


if __name__ == "__main__":
    main()
