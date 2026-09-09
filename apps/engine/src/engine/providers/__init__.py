"""Concrete provider adapters.

Nothing here may import psycopg, a schema module or a repository: an adapter
receives an HttpTransport and a RawArchive and depends on nothing else. That is
what makes providers replaceable and adapters testable with no database.
"""
