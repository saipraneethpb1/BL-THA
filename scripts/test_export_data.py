"""Run with python -m unittest discover -s scripts -p test_export_data.py."""

import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from export_data import export_data


class ExportTest(unittest.TestCase):
    def test_live_wal_binary_fields_assets_and_account_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            (root / "accounts").mkdir()
            (root / "image.png").write_bytes(b"stored-image")
            with closing(sqlite3.connect(root / "accounts/user.db")) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE records (body TEXT, embedding BLOB)")
                db.execute(
                    "INSERT INTO records VALUES (?, ?)", ("Customer email", b"\x00\xff")
                )
                db.commit()
                output = Path(tmp) / "export.zip"
                manifest = export_data({"inbox-agent": root}, output)
                with zipfile.ZipFile(output) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(
                        archive.read("inbox-agent/data/image.png"), b"stored-image"
                    )
                    row = json.loads(
                        archive.read(
                            "inbox-agent/tables/accounts/user.db/records.jsonl"
                        )
                    )
                    self.assertEqual(row["body"], "Customer email")
                    self.assertEqual(
                        row["embedding"], {"encoding": "base64", "data": "AP8="}
                    )
                    self.assertFalse(
                        any(n.endswith(("-wal", "-shm")) for n in archive.namelist())
                    )
                    restored = Path(tmp) / "restored.db"
                    restored.write_bytes(
                        archive.read("inbox-agent/data/accounts/user.db")
                    )
                with closing(sqlite3.connect(restored)) as snapshot:
                    self.assertEqual(
                        snapshot.execute("SELECT body FROM records").fetchone()[0],
                        "Customer email",
                    )
                self.assertEqual(manifest["databases"][0]["tables"]["records"], 1)
                with self.assertRaises(ValueError):
                    export_data({"inbox-agent": root}, output)

    def test_missing_data_fails_without_partial_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "export.zip"
            with self.assertRaises(ValueError):
                export_data({"knowledge-brain": Path(tmp) / "missing"}, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
