"""Run the Flask dashboard locally."""

import os

from wsgi import app


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        use_reloader=False,
        threaded=True,
    )
