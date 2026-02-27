## Disclaimer
I made this awhile ago, so if you encounter any issues make sure to let me know.
## Setup

1) Create a Notion integration and share your database with it.
2) Get:
   - `NOTION_TOKEN` (integration token)
   - `NOTION_DATA_SOURCE_ID` (database data source id used for query/create)
3) Get a Canvas access token and your Canvas base URL.

### Getting the NOTION_DATA_SOURCE_ID

1. Open your Notion database in a browser
2. Copy the URL

It will look like:

https://www.notion.so/workspace/Assignments-2f21c54c9f048137a6a4c6c3d6ebb56c?v=abcdef...

3. The long ID after the page name is your database ID:
2f21c54c9f048137a6a4c6c3d6ebb56c

4. Convert it to UUID format by inserting dashes:

2f21c54c-9f04-8137-a6a4-c6c3d6ebb56c

Use that as NOTION_DATA_SOURCE_ID.
## Run

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env
# fill in .env
python sync_canvas_to_notion.py
