"""Application entry point: uvicorn app.main:app --host 127.0.0.1 --port 8000."""
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from app.config import PROJECT_ROOT
from app.database import init_db
from app.routers import music, school
from app.scheduler.runner import BellScheduler
from app.services.audio import AudioPlayer
from app.services.timetable import TimetableError

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
# The one-second polling job should not print two log lines every second.
logging.getLogger('apscheduler.executors.default').setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.player = AudioPlayer()
    app.state.bell_scheduler = BellScheduler(app.state.player)
    app.state.bell_scheduler.start()
    try:
        yield
    finally:
        app.state.bell_scheduler.shutdown()
        app.state.player.close()


app = FastAPI(title='School Timetable & Music Bell System', version='1.1.1', lifespan=lifespan,
              description='Local school administration and speaker appliance. Run one worker on the speaker computer.')
app.include_router(school.router)
app.include_router(music.router)
app.mount('/static', StaticFiles(directory=PROJECT_ROOT / 'app' / 'static'), name='static')
templates = Jinja2Templates(directory=PROJECT_ROOT / 'app' / 'templates')


@app.middleware('http')
async def same_origin_mutations(request: Request, call_next):
    """Reject browser writes initiated by other websites; this is not account auth."""
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            return JSONResponse(status_code=403, content={'detail': 'Cross-origin writes are not allowed.'})
    return await call_next(request)


@app.exception_handler(IntegrityError)
async def integrity_error(_request: Request, _exc: IntegrityError):
    return JSONResponse(status_code=409, content={'detail': 'Duplicate or conflicting data. Check names, grades, assignments, and occupied lesson slots.'})


@app.exception_handler(TimetableError)
async def timetable_error(_request: Request, exc: TimetableError):
    return JSONResponse(status_code=422 if exc.status == 'invalid' else 409,
                        content={'detail': {'message': exc.message, 'status': exc.status, 'details': exc.details}})


@app.get('/', include_in_schema=False)
def home(request: Request):
    return templates.TemplateResponse(request=request, name='index.html', context={})


@app.get('/health', tags=['System'])
def health():
    return {'status': 'ok', 'service': 'school-timetable-music-bell'}
