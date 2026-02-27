## Setup

1) Create a Notion integration and share your database with it.
2) Get:
   - `NOTION_TOKEN` (integration token)
   - `NOTION_DATA_SOURCE_ID` (database data source id used for query/create)
3) Get a Canvas access token and your Canvas base URL.

## Run

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env
# fill in .env
python sync_canvas_to_notion.py
