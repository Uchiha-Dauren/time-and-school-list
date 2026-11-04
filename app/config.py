"""Paths are local to the project unless SCHOOL_DATA_DIR is supplied."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get('SCHOOL_DATA_DIR', PROJECT_ROOT / 'data')).resolve()
UPLOAD_DIR = DATA_DIR / 'music'
DATABASE_URL = os.environ.get('DATABASE_URL', f'sqlite:///{DATA_DIR / "school.db"}')
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
