"""Database access for the whole application: one engine, one schema source.

``OrmDatabase`` owns the connection; ``app.database.models`` owns the schema and
Alembic migrates it. There used to be a second layer here — a raw-SQL
``Database`` class with its own pool and its own ``CREATE TABLE`` statements,
serving the jobs queue alone. It is gone: the jobs tables are models like
everything else (see ``JobRecord`` in ``app.database.models``).
"""

from app.database.orm import OrmDatabase

__all__ = ["OrmDatabase"]
