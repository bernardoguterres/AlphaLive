"""
AlphaLive Schema Migrations

Handles versioned schema migrations for strategy configurations.
"""

from .schema_migrations import migrate_schema

__all__ = ["migrate_schema"]
