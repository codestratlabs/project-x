"""px_runner: runs one task of the company evidence pipeline against S3 (deploy/INTERFACES.md).

    python -m px_runner <task> --company <co> --run-id <run_id>

Modules: tasks (settings and the eight tasks), s3sync (S3 key <-> local path table and transfers), runrecord
(runs/<co>/<run_id>.json), secrets (Secrets Manager and redaction).
"""
__version__ = "0.1.0"
