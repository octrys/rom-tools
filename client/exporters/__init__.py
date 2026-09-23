"""Exporters: turn the leaked client artifacts into typed, server-facing data.

Run from the client dir as modules so the shared `libs` package resolves:
    python3 -m exporters.extract_tables      # runtime table dump -> typed JSON
    python3 -m exporters.extract_protocol    # metadata + type dump -> message catalog
"""
