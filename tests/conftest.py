import os, sys
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("GENESIS_MS", "1000000000000")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
