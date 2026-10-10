"""Generate sentence-transformer embeddings for the existing PostgreSQL menu."""

import os

from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
import psycopg2


MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
DIMENSION = 384


def pg_connection():
    url = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    if url:
        return psycopg2.connect(url, connect_timeout=8)
    config = {
        "dbname": os.getenv("PGDATABASE", "food_ordering_db"),
        "host": os.getenv("PGHOST", "localhost"),
        "port": os.getenv("PGPORT", "5432"),
        "connect_timeout": 8,
    }
    for key, name in (("user", "PGUSER"), ("password", "PGPASSWORD")):
        if os.getenv(name):
            config[key] = os.getenv(name)
    return psycopg2.connect(**config)


def q(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def main():
    load_dotenv()
    model = SentenceTransformer(MODEL_NAME)
    connection = pg_connection()
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cursor.execute("ALTER TABLE menu_item ADD COLUMN IF NOT EXISTS embedding vector(384)")
                cursor.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name IN ('menu_item', 'restaurant')"
                )
                schema = {}
                for table, column in cursor.fetchall():
                    schema.setdefault(table, set()).add(column)
                menu = schema.get("menu_item", set())
                restaurant = schema.get("restaurant", set())

                item_id = next((c for c in ("item_id", "menu_item_id") if c in menu), None)
                item_name = next((c for c in ("item_name", "name", "food_name") if c in menu), None)
                if not item_id or not item_name:
                    raise RuntimeError("menu_item must have a supported item ID and item name column")

                searchable = [c for c in (
                    "category", "description", "cuisine", "food_type", "spice_level", "tags"
                ) if c in menu]
                restaurant_fk = "restaurant_id" if "restaurant_id" in menu else None
                restaurant_pk = "restaurant_id" if "restaurant_id" in restaurant else None
                restaurant_name = next((c for c in ("restaurant_name", "name") if c in restaurant), None)
                join = bool(restaurant_fk and restaurant_pk and restaurant_name)
                fields = [f"m.{q(item_id)}", f"m.{q(item_name)}"]
                fields.extend(f"m.{q(column)}" for column in searchable)
                fields.append(f"r.{q(restaurant_name)}" if join else "NULL::text")
                from_clause = (
                    f"menu_item m LEFT JOIN restaurant r ON m.{q(restaurant_fk)} = r.{q(restaurant_pk)}"
                    if join else "menu_item m"
                )
                cursor.execute(f"SELECT {', '.join(fields)} FROM {from_clause} ORDER BY m.{q(item_id)}")
                rows = cursor.fetchall()
                if not rows:
                    raise RuntimeError("The existing menu_item table contains no records")

                texts = []
                for row in rows:
                    pieces = [f"Food: {row[1]}"]
                    pieces.extend(f"{column.replace('_', ' ')}: {value}" for column, value in zip(searchable, row[2:-1]) if value)
                    if row[-1]:
                        pieces.append(f"Restaurant: {row[-1]}")
                    texts.append(". ".join(pieces))

                vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
                update_sql = f"UPDATE menu_item SET embedding = %s::vector WHERE {q(item_id)} = %s"
                cursor.executemany(
                    update_sql,
                    [("[" + ",".join(str(float(value)) for value in vector) + "]", row[0])
                     for row, vector in zip(rows, vectors)],
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS menu_item_embedding_hnsw_idx "
                    "ON menu_item USING hnsw (embedding vector_cosine_ops)"
                )
        print(f"pgvector ready: generated or refreshed embeddings for {len(rows)} existing menu items using {MODEL_NAME} ({DIMENSION} dimensions).")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
