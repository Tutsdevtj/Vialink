import asyncio
import json
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .jobs import ROOT, Jobs
from .policy import MAX_URL_LENGTH, UserError, check_url

STATIC = ROOT / 'static'
FILE_TYPES = {'mp4': 'video/mp4', 'mp3': 'audio/mpeg', 'jpg': 'image/jpeg',
              'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp'}


@asynccontextmanager
async def lifespan(app):
    jobs = app.state.jobs = Jobs()
    cleanup = asyncio.create_task(jobs.reap())
    try:
        yield
    finally:
        cleanup.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup
        await jobs.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])
app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.middleware('http')
async def browser_policy(request, call_next):
    if request.method == 'POST':
        origin = request.headers.get('origin')
        site = request.headers.get('sec-fetch-site')
        if site in {'cross-site', 'same-site'} or (origin and origin != f'http://{request.headers.get("host")}'):
            return JSONResponse({'error': 'A solicitação deve partir desta página local.'}, status_code=403)

    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'"
    )
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(max_length=MAX_URL_LENGTH)


class DownloadInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    analysis: str = Field(pattern=r'^[a-f0-9]{32}$')
    option: str = Field(pattern=r'^(v-(original|[0-9]{1,6})|a-192)$')


async def read_input(request, model, message):
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise UserError('Envie uma solicitação no formato JSON.', 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 28 * 1024:
            raise UserError('A solicitação ultrapassou o tamanho permitido.', 413)
        body.extend(chunk)
    try:
        return model.model_validate_json(body)
    except ValidationError:
        raise UserError(message) from None


@app.exception_handler(UserError)
async def show_user_error(request, error):
    return JSONResponse({'error': str(error)}, status_code=error.status)


@app.get('/')
async def index():
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-cache'})


@app.post('/api/analises', status_code=202)
async def start_analysis(request: Request):
    data = await read_input(request, AnalysisInput, 'Informe uma URL de vídeo válida.')
    url = check_url(data.url.strip())
    job = request.app.state.jobs.start('analysis', {'kind': 'analysis', 'url': url})
    return {'id': job.id}


@app.get('/api/analises/{identifier}')
async def get_analysis(identifier: str, request: Request):
    job = request.app.state.jobs.find(identifier, 'analysis')
    if job.state == 'error':
        return {'state': 'error', 'error': job.result['error']}
    if job.state == 'running':
        return {'state': 'running'}

    result = job.result
    metadata = {key: result[key] for key in ('title', 'duration', 'origin')}
    metadata['options'] = [{key: value for key, value in option.items() if key != 'tracks'}
                           for option in result['options']]
    for key in ('thumbnail', 'preview'):
        metadata[key] = f'/api/analises/{identifier}/{key}' if result[key] else None
    return {'state': 'ready', 'video': metadata}


class JobFile(FileResponse):
    def __init__(self, job, stored_name, **kwargs):
        self.job = job
        extension = stored_name.rsplit('.', 1)[1]
        super().__init__(job.folder / stored_name, media_type=FILE_TYPES[extension], **kwargs)

    async def __call__(self, scope, receive, send):
        self.job.readers += 1
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.job.readers -= 1


@app.get('/api/analises/{identifier}/{asset}')
async def get_asset(identifier: str, asset: str, request: Request):
    job = request.app.state.jobs.find(identifier, 'analysis')
    if asset not in {'thumbnail', 'preview'} or job.state != 'ready' or not job.result[asset]:
        raise UserError('Esta prévia está indisponível.', 404)
    return JobFile(job, job.result[asset])


@app.post('/api/downloads', status_code=202)
async def start_download(request: Request):
    data = await read_input(request, DownloadInput, 'Escolha um formato disponível para este vídeo.')
    jobs = request.app.state.jobs
    analysis = jobs.find(data.analysis, 'analysis')
    if analysis.state != 'ready':
        raise UserError('Faça a consulta do vídeo antes de preparar o arquivo.', 409)
    selection = next((item for item in analysis.result['options'] if item['id'] == data.option), None)
    if not selection:
        raise UserError('O formato escolhido está indisponível para este vídeo.')
    job = jobs.start('download', {'kind': 'download', 'url': analysis.payload['url'], 'selection': selection,
                                  'title': analysis.result['title']})
    return {'id': job.id}


@app.get('/api/downloads/{identifier}')
async def get_download(identifier: str, request: Request):
    job = request.app.state.jobs.find(identifier, 'download')
    if job.state == 'error':
        return {'state': 'error', 'error': job.result['error']}
    if job.state == 'ready':
        expires = datetime.now(timezone.utc) + timedelta(seconds=max(0, job.expires - monotonic()))
        return {'state': 'ready', 'file': f'/api/downloads/{identifier}/arquivo',
                'size': job.result['size'], 'expires': expires.isoformat()}
    try:
        progress = json.loads((job.folder / 'progress.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        progress = {'phase': 'starting', 'percent': None}
    return {'state': 'running', **progress}


@app.get('/api/downloads/{identifier}/arquivo')
async def get_file(identifier: str, request: Request):
    job = request.app.state.jobs.find(identifier, 'download')
    if job.state != 'ready':
        raise UserError('O arquivo ainda está sendo preparado.', 409)
    extension = job.payload['selection']['format']
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', job.payload['title']).strip(' .')
    title = title[:96].strip(' .') or 'vídeo'
    if re.fullmatch(r'CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³]', title.split('.')[0], re.IGNORECASE):
        title = f'Vídeo - {title}'
    return JobFile(job, f'arquivo.{extension}', filename=f'{title}.{extension}')
