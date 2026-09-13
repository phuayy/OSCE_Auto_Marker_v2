"""Which tables in a deployed database are *not* described by the ORM metadata.

Alembic's autogenerate proposes a drop for every table it finds in the database
but not in ``Base.metadata``, so anything outside the metadata has to be named
somewhere. This list used to be four times as long: the queue's three tables
were described by hand in a raw-SQL module with a connection pool of their own,
which meant autogenerate was blind to them and ``alembic check`` could not
report drift in that half of the schema. They are models now.

What is left is Alembic's own bookkeeping table and one dead table:
``app_metadata`` held a schema-version row the removed layer wrote on
PostgreSQL. Nothing reads or writes it. It is listed rather than dropped
because a migration that deletes a table is not worth the risk for a few bytes,
and a deployment that wants it gone can drop it by hand.

Kept in its own module so ``alembic/env.py`` and the tests that pin this
property read the same value.
"""

from __future__ import annotations

UNMANAGED_TABLES = frozenset({"app_metadata", "alembic_version"})

__all__ = ["UNMANAGED_TABLES"]
