"""Entry point giữ tương thích: `python proxy.py`. Logic nằm ở app.py/upstream.py."""
from app import create_app

import config

app = create_app()

if __name__ == "__main__":
    from waitress import serve
    print(f"Website skeleton on http://127.0.0.1:{config.PORT} (read-only -> {config.AI_TOOL_API_BASE})")
    serve(app, host="127.0.0.1", port=config.PORT, threads=8)
