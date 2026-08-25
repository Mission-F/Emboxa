import os
import tempfile
from pathlib import Path


TEST_ROOT = Path(tempfile.mkdtemp(prefix="mailvault-tests-"))
os.environ["DATA_DIR"] = str(TEST_ROOT)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_ROOT / 'mailvault.db'}"
os.environ["SESSION_SECRET_FILE"] = str(TEST_ROOT / "secrets" / "session.key")
os.environ["ENCRYPTION_KEY_FILE"] = str(TEST_ROOT / "secrets" / "fernet.key")
os.environ["COOKIE_SECURE"] = "false"

