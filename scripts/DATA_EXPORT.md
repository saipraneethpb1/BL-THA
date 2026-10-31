Owner data export
=================

From the deployed project directory on AWS:

```sh
sudo python3 scripts/export_data.py --docker --output /tmp/all-task-data.zip
sudo chown ubuntu:ubuntu /tmp/all-task-data.zip
```

Download that file over SSH/SCP. Do not put it in the public website, GitHub,
or a publicly shared Drive folder. It contains all accounts' stored data,
including private emails and any encrypted Gmail credentials in the database.
The output file is created with owner-only permissions on Linux.

For local data, run `python scripts/export_data.py` from the repository root.
The default output is a dated ZIP under `exports/` (ignored by Git).
Local mode exports the local data, not the AWS volumes.

Contents:

- All three persistent data directories, including all account databases,
  model caches and other stored files.
- Consistent SQLite snapshots including search indexes, embeddings, articles,
  image descriptions and URLs, extracted table text, source records, ingestion
  history, expert answers, queries, emails, classifications, drafts, reviews,
  lessons, outbox records, meetings, scope versions, proposals and audit records,
  wherever those records exist in the database.
- Readable JSONL for every database table; binary fields have tagged base64 values.
- In Docker mode, the email dataset and meeting transcript files shipped in the
  running containers. Local mode includes them from the local data/transcript folders.
- A manifest with stored-file hashes, database row counts, and limitations.

Each database is backed up independently, so this is not a single transaction
across the three services. Services keep running. Data created after its database
snapshot is taken belongs in the next export.

The exporter does not fetch remote images or original HTML that was never stored.
Image URLs/descriptions and tables represented in article text are preserved as
stored. API keys, `.env`, TLS keys and source code outside the data directories
are not part of this data export.

Check the implementation with:

```sh
python -m unittest discover -s scripts -p test_export_data.py
```
