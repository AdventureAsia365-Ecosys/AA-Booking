"""AA-825 — Booking retention: owner-run TTL job kinds on ``booking.job`` (data standards DM-6.6, DM-7).

``policy`` (windows), ``redact`` (inbox payload PII), ``archive`` (S3 JSONL.gz before delete), ``runner`` (batched
count / delete / redact), ``jobs`` (handlers, modes, daily schedule, admin status).
"""
