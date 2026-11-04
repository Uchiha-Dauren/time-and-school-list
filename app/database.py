"""SQLite configuration and shared mutation lock for the single-process appliance."""
from threading import RLock
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from app.config import DATA_DIR, DATABASE_URL, UPLOAD_DIR
from app.models import Base, Settings
from app.migrations import upgrade_database

school_lock = RLock()
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False, 'timeout': 30})

@event.listens_for(engine, 'connect')
def configure_sqlite(connection, _record):
    cursor = connection.cursor()
    cursor.execute('PRAGMA foreign_keys=ON')
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def init_db() -> None:
    Base.metadata.create_all(engine)
    upgraded = upgrade_database(engine)
    with SessionLocal.begin() as session:
        if session.get(Settings, 1) is None:
            session.add(Settings(id=1))
        if upgraded:
            from app.services.bells import rebuild_bell_events
            session.flush()
            rebuild_bell_events(session, session.get(Settings, 1))

def get_db():
    with SessionLocal() as session:
        yield session

def get_settings(session: Session) -> Settings:
    settings = session.get(Settings, 1)
    if settings is None:
        settings = Settings(id=1)
        session.add(settings)
        session.flush()
    return settings
