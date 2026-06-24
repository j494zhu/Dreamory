"""
One-shot DB bootstrap: enable pgvector + create all tables/indexes.

    python -m scripts.init_db
"""
import asyncio

from app.db import init_db


async def main() -> None:
    await init_db()
    print("✓ pgvector enabled, tables + HNSW/GIN indexes created.")


if __name__ == "__main__":
    asyncio.run(main())
