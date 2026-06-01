import os
import sys

# Add backend to path so FastAPI imports resolve correctly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

# Import the FastAPI app instance (main.py handles all routes + static files)
from main import app  # noqa: E402, F401

# main.py already mounts static files and defines the SPA catch-all route
# serve.py just exports the app for uvicorn to run
