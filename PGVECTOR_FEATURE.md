# pgvector-based semantic food search

## What it does

The dashboard's **Smart Food Search** section embeds a natural-language query with the same local sentence-transformer model used for menu records, then orders actual PostgreSQL `menu_item` rows by cosine similarity. pgvector is a PostgreSQL extension that stores and compares vectors; it is not an AI model. The existing MongoDB Atlas dashboard, orders, integrity page, health route, and data remain in place.

## Model and storage

- Model: `sentence-transformers/all-MiniLM-L6-v2` (downloaded from Hugging Face on first use and then cached locally).
- Embedding size: 384 dimensions.
- PostgreSQL column: `menu_item.embedding vector(384)`.
- Index: HNSW cosine index, created by the generation script when supported by the installed pgvector extension.
- Searchable text includes the actual menu item name, available category/description/cuisine/type/spice/tags columns, and linked restaurant name when those columns exist.

## Configure PostgreSQL

The current project `.env` only contains its MongoDB setting. Add PostgreSQL connection settings to that file without changing `MONGODB_URI`. Use either a URL:

```dotenv
POSTGRES_URL=postgresql://USER:PASSWORD@HOST:5432/food_ordering_db
```

Or the standard libpq variables:

```dotenv
PGHOST=localhost
PGPORT=5432
PGDATABASE=food_ordering_db
PGUSER=YOUR_POSTGRES_USER
PGPASSWORD=YOUR_POSTGRES_PASSWORD
```

Protect `.env` and do not commit credentials. The app can continue serving its MongoDB features if PostgreSQL search is not configured; the search endpoint then returns a setup error without exposing credentials.

## Setup and run

From PowerShell in the project directory:

```powershell
\.\venv\Scripts\python.exe -m pip install -r requirements.txt
\.\venv\Scripts\python.exe generate_menu_embeddings.py
\.\venv\Scripts\python.exe app.py
```

The script runs `CREATE EXTENSION IF NOT EXISTS vector`, adds the embedding column with `ADD COLUMN IF NOT EXISTS`, reads the existing menu, embeds it, and updates each existing row by its primary key. It does not recreate the table or replace menu data. It is safe to rerun to refresh embeddings. PostgreSQL must have a compatible pgvector extension installed and the configured role must be allowed to create the extension/column/index.

The first model load needs internet access to download the model if it is not already cached. The model is local at inference time; no paid embedding API is used.

## API and examples

`GET /api/semantic-search?q=<natural-language-query>` returns up to five results with item ID, name, price, availability (if present), restaurant name (if linked), and cosine similarity. Empty queries return HTTP 400. PostgreSQL/model/setup failures return HTTP 503 with a sanitized error.

Example queries:

- `spicy chicken`
- `biryani`
- `vegetarian food`
- `quick spicy meal`

The similarity value is derived as `1 - cosine_distance`; it is a ranking score, not a calibrated probability.

## Limitations

- Results depend on the actual records and descriptive fields in `menu_item`; the feature does not invent menu results.
- Search works after PostgreSQL credentials, pgvector, dependencies, and embeddings are configured.
- Restaurant details are included only when the existing schema has a `restaurant_id` relationship and a supported restaurant name column.
- The model is English-oriented. First-run model download requires network access and sufficient disk space.
