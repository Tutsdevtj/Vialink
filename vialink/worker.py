import json
import logging
import os
import shutil
import subprocess
import sys
from functools import cached_property
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.downloader.http import HttpFD
from yt_dlp.globals import plugin_dirs
from yt_dlp.networking import Request
from yt_dlp.networking._urllib import RedirectHandler, UrllibRH

from .policy import MAX_FILE, MIB, PrivateAddressError, UserError, check_url, restrict_worker_network

LOG = logging.getLogger('vialink.worker')
PREVIEW_LIMIT = 8 * MIB
AUDIO_BITRATE = 192
DEMUXERS = {
    'mp4': 'mov', 'm4a': 'mov', 'mov': 'mov', 'webm': 'matroska', 'mkv': 'matroska',
    'mp3': 'mp3', 'aac': 'aac', 'ogg': 'ogg', 'opus': 'ogg', 'flac': 'flac', 'wav': 'wav',
}


class PublicRedirect(RedirectHandler):
    handler_order = RedirectHandler.handler_order - 10

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check_url(newurl)
        except UserError as error:
            raise PrivateAddressError(f'Redirecionamento bloqueado: {error}') from error
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicHttpRH(UrllibRH):
    _SUPPORTED_URL_SCHEMES = ('http', 'https')

    def _get_proxies(self, request):
        return {}

    def _create_instance(self, proxies, cookiejar, legacy_ssl_support=None):
        opener = super()._create_instance({}, cookiejar, legacy_ssl_support)
        opener.add_handler(PublicRedirect())
        return opener


class MediaDL(YoutubeDL):
    @cached_property
    def _request_director(self):
        # A single Python HTTP handler keeps all connections under the socket guard.
        return self.build_request_director([PublicHttpRH])


def write_json(folder, name, payload):
    target = folder / name
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    # On Windows a parallel poll can briefly hold the destination open. The next
    # progress update can replace it. The final result has its own unique file.
    try:
        os.replace(temporary, target)
    except PermissionError:
        if name != 'progress.json':
            raise


def progress(folder, phase, percent=None):
    write_json(folder, 'progress.json', {'phase': phase, 'percent': percent})


def safe_origin(url, fallback):
    try:
        return check_url(url or '')
    except UserError:
        return fallback


def extract(ydl, url):
    info = ydl.extract_info(url, download=False)
    if not info or info.get('_type') in {'playlist', 'multi_video'}:
        raise UserError('Cole o link de um vídeo individual, pois este endereço aponta para uma lista.')
    if info.get('is_live'):
        raise UserError('Aguarde a transmissão ao vivo terminar para baixar a gravação.')
    return info


def usable_tracks(info):
    return [track for track in info.get('formats') or [info]
            if track.get('format_id') is not None and track.get('url')
            and urlsplit(track['url']).scheme in {'http', 'https'}
            and track.get('protocol') in {'http', 'https'}
            and track.get('ext') in DEMUXERS and not track.get('has_drm')
            and not track.get('fragments')
            and (track.get('filesize') or 0) <= MAX_FILE]


def size_of(track, duration):
    size = track.get('filesize') or track.get('filesize_approx')
    if not size and duration and track.get('tbr'):
        size = duration * track['tbr'] * 1000 / 8
    return round(size) if size else None


def video_options(tracks, duration):
    aac = next((track for track in reversed(tracks) if track.get('vcodec') == 'none'
                and track.get('ext') == 'm4a' and track.get('acodec') not in {None, 'none'}), None)
    by_height = {}
    for video in reversed(tracks):
        if video['ext'] not in {'mp4', 'mov'} or video.get('vcodec') == 'none':
            continue
        height = video.get('height')
        audio = aac if video.get('acodec') == 'none' else None
        parts = [video, audio] if audio else [video]
        sizes = [size_of(track, duration) for track in parts]
        estimated = sum(sizes) if all(sizes) else None
        if estimated and estimated > MAX_FILE:
            continue
        has_audio = bool(audio) or video.get('acodec') != 'none'
        score = (has_audio, (video.get('vcodec') or '').startswith(('avc', 'h264')))
        if height not in by_height or score > by_height[height][0]:
            by_height[height] = (score, {
                'id': f'v-{height or "original"}', 'format': 'mp4',
                'quality': f'{height}p' if height else 'Qualidade original',
                'estimated': estimated, 'silent': not has_audio,
                'tracks': [track['format_id'] for track in parts],
            })
    return [by_height[height][1] for height in sorted(by_height, key=lambda value: value or 0, reverse=True)]


def audio_option(tracks, duration):
    estimated = round(duration * AUDIO_BITRATE * 1000 / 8) if duration else None
    if estimated and estimated > MAX_FILE:
        return []
    sources = [track for track in tracks if track.get('acodec') != 'none']
    if not sources:
        return []
    source = max(sources, key=lambda track: (
        track.get('acodec') is not None, track.get('vcodec') == 'none', track.get('abr') or 0,
    ))
    return [{'id': 'a-192', 'format': 'mp3', 'quality': '192 kb/s', 'estimated': estimated,
             'silent': False, 'tracks': [source['format_id']]}]


def fetch_small(ydl, url, limit, headers=None):
    try:
        check_url(url)
        deadline = monotonic() + 5
        request = Request(url, headers=headers, extensions={'timeout': 3})
        with ydl.urlopen(request) as response:
            size = response.get_header('Content-Length')
            if size and int(size) > limit:
                return None
            data = bytearray()
            while monotonic() < deadline:
                chunk = response.read(64 * 1024)
                if not chunk:
                    return data
                data.extend(chunk)
                if len(data) > limit:
                    break
    except Exception as error:
        LOG.info('Prévia indisponível: %s', error)
    return None


def thumbnail(ydl, info, folder):
    if not info.get('thumbnail'):
        return None
    data = fetch_small(ydl, info['thumbnail'], 2 * MIB)
    if not data:
        return None
    signatures = {b'\xff\xd8\xff': 'jpg', b'\x89PNG\r\n\x1a\n': 'png', b'GIF8': 'gif'}
    extension = next((ext for prefix, ext in signatures.items() if data.startswith(prefix)), None)
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        extension = 'webp'
    if not extension:
        return None
    name = f'thumbnail.{extension}'
    (folder / name).write_bytes(data)
    return name


def local_input(path, extension):
    return ['-protocol_whitelist', 'file', '-f', DEMUXERS[extension], '-i', str(path)]


def make_preview(ydl, tracks, duration, folder):
    candidates = [track for track in tracks if track['ext'] == 'mp4'
                  and track.get('vcodec') != 'none'
                  and (track.get('vcodec') or 'avc').startswith(('avc', 'h264'))
                  and (track.get('acodec') != 'none' or len(tracks) == 1)
                  and (size_of(track, duration) or 0) <= PREVIEW_LIMIT
                  and (track.get('height') or 0) <= 1080]
    if not candidates:
        return None, None, {}
    track = max(candidates, key=lambda item: item.get('height') or 0)
    data = fetch_small(ydl, track['url'], PREVIEW_LIMIT, track.get('http_headers'))
    if not data or data[4:8] != b'ftyp':
        return None, None, {}
    path = folder / 'preview.mp4'
    path.write_bytes(data)

    details = {}
    if ffprobe := shutil.which('ffprobe'):
        try:
            output = subprocess.check_output([
                ffprobe, '-v', 'error', '-show_entries',
                'format=duration:stream=codec_type,codec_name,height', '-of', 'json',
                *local_input(path, 'mp4'),
            ], timeout=4)
            details = json.loads(output)
        except (subprocess.SubprocessError, ValueError):
            LOG.info('Não foi possível ler os dados da prévia', exc_info=True)

    poster = None
    if ffmpeg := shutil.which('ffmpeg'):
        try:
            length = float(details.get('format', {}).get('duration') or duration or 0)
            subprocess.run([
                ffmpeg, '-nostdin', '-v', 'error', '-y', *local_input(path, 'mp4'),
                '-ss', str(min(6, max(0, length / 3))), '-frames:v', '1',
                '-vf', 'scale=640:-2', '-update', '1', str(folder / 'poster.jpg'),
            ], check=True, timeout=4)
            if (folder / 'poster.jpg').is_file():
                poster = 'poster.jpg'
        except (subprocess.SubprocessError, ValueError):
            LOG.info('Não foi possível gerar a miniatura da prévia', exc_info=True)
    return 'preview.mp4', poster, details


def analyze(ydl, url, folder):
    info = extract(ydl, url)
    tracks = usable_tracks(info)
    duration = info.get('duration')
    image = thumbnail(ydl, info, folder)
    preview, poster, details = make_preview(ydl, tracks, duration, folder)
    streams = details.get('streams') or []
    if streams and len(tracks) == 1:
        # Direct files can lack dimensions and codec information in yt-dlp.
        video = next((stream for stream in streams if stream['codec_type'] == 'video'), {})
        audio = next((stream for stream in streams if stream['codec_type'] == 'audio'), {})
        tracks[0].update(height=video.get('height'), vcodec=video.get('codec_name', 'none'),
                         acodec=audio.get('codec_name', 'none'), filesize=(folder / preview).stat().st_size)
    if not duration:
        try:
            duration = float(details.get('format', {}).get('duration', ''))
            if not 0 < duration < float('inf'):
                duration = None
        except ValueError:
            duration = None
    options = video_options(tracks, duration) + audio_option(tracks, duration)
    return {
        'title': str(info.get('title') or 'Vídeo sem título')[:240], 'duration': duration,
        'origin': safe_origin(info.get('webpage_url'), url), 'thumbnail': image or poster,
        'preview': preview, 'options': options,
    }


def download_tracks(ydl, folder, tracks, kind):
    downloaded = 0
    paths = []
    totals = [track.get('filesize') or track.get('filesize_approx') for track in tracks]
    phase = 'download_audio' if kind == 'mp3' else 'download_video'
    last_update = 0
    progress(folder, phase)

    for index, track in enumerate(tracks):
        check_url(track['url'])
        path = folder / f'track-{index}.{track["ext"]}'

        def report(event):
            nonlocal last_update
            if event.get('total_bytes'):
                totals[index] = event['total_bytes']
            current = event.get('downloaded_bytes', 0)
            if downloaded + max(current, event.get('total_bytes') or 0) > MAX_FILE:
                raise UserError('Este download ultrapassou o limite de 300 MB.')
            now = monotonic()
            if now - last_update >= .20:
                percent = min(100, int((downloaded + current) * 100 / sum(totals))) if all(totals) else None
                progress(folder, phase, percent)
                last_update = now

        downloader = HttpFD(ydl, {**ydl.params, 'max_filesize': MAX_FILE - downloaded})
        downloader.add_progress_hook(report)
        success, _ = downloader.download(str(path), track)
        if not success or not path.is_file() or not path.stat().st_size:
            raise UserError('Não foi possível receber o arquivo da origem. Tente novamente.')
        downloaded += path.stat().st_size
        totals[index] = path.stat().st_size
        if downloaded > MAX_FILE:
            raise UserError('Este download ultrapassou o limite de 300 MB.')
        paths.append(path)
    progress(folder, phase, 100)
    return paths


def prepare(ydl, input_data, folder):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise UserError('FFmpeg não encontrado. Confira a instalação e reinicie a aplicação.')

    selection = input_data['selection']
    extension = selection['format']
    info = extract(ydl, input_data['url'])
    available = {track['format_id']: track for track in usable_tracks(info)}
    try:
        tracks = [available[identifier] for identifier in selection['tracks']]
    except KeyError:
        raise UserError('A origem mudou os formatos. Consulte o link novamente.') from None
    paths = download_tracks(ydl, folder, tracks, extension)
    arguments = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y']
    for path, track in zip(paths, tracks, strict=True):
        arguments.extend(local_input(path, track['ext']))

    if extension == 'mp3':
        progress(folder, 'convert')
        arguments.extend(['-map', '0:a:0', '-c:a', 'libmp3lame', '-b:a', f'{AUDIO_BITRATE}k'])
    else:
        progress(folder, 'merge' if len(paths) > 1 else 'mux')
        audio_map = '1:a:0' if len(paths) > 1 else '0:a:0?'
        arguments.extend(['-map', '0:v:0', '-map', audio_map, '-c', 'copy', '-movflags', '+faststart'])

    output = folder / f'arquivo.{extension}'
    try:
        subprocess.run([*arguments, str(output)], check=True)
    except subprocess.CalledProcessError:
        raise UserError('O FFmpeg não conseguiu preparar este formato. Tente outra opção ou outro link.') from None
    if not output.is_file() or not 0 < output.stat().st_size <= MAX_FILE:
        raise UserError('O arquivo final está vazio ou ultrapassou o limite de 300 MB.')
    for path in paths:
        path.unlink()
    return {'size': output.stat().st_size}


def public_error(error):
    if isinstance(error, UserError):
        return {'error': str(error), 'status': error.status}
    cause = str(error).lower()
    if any(term in cause for term in ('endereço bloqueado', 'redirecionamento bloqueado')):
        message = 'A origem apontou para um endereço bloqueado. Use um link público.'
    elif any(term in cause for term in ('sign in', 'log in', 'login', 'not a bot', 'po token', 'cookies')):
        message = 'A origem solicitou verificação ou login. Tente um vídeo público acessível.'
    elif '403' in cause or 'forbidden' in cause:
        message = 'A origem recusou a solicitação. O vídeo pode estar restrito nesta rede.'
    elif any(term in cause for term in ('404', 'private video', 'unavailable', 'not available')):
        message = 'O vídeo está indisponível ou é privado. Confira o link e tente novamente.'
    elif 'timed out' in cause or 'timeout' in cause:
        message = 'A origem demorou para responder. Tente novamente.'
    else:
        message = 'Não foi possível acessar o vídeo. Confira o link e tente novamente.'
    return {'error': message, 'status': 422}


def restrict_child_processes(deno, folder):
    # Allow yt-dlp's bundled EJS runtime and this worker's local FFmpeg calls.
    binaries = {path: name for name in ('ffmpeg', 'ffprobe') if (path := shutil.which(name))}
    if deno:
        binaries[deno] = 'deno'
    os.environ['DENO_NO_UPDATE_CHECK'] = '1'
    os.environ['DENO_DIR'] = str(folder / 'deno-cache')

    def check_process(event, values):
        if event != 'subprocess.Popen':
            return
        executable, arguments, _, _ = values
        for path, name in binaries.items():
            if isinstance(arguments, str):
                prefix = subprocess.list2cmdline([path]) + ' '
                if not arguments.startswith(prefix) or executable not in (None, path):
                    continue
                command = arguments[len(prefix):].split()
            else:
                if executable != path:
                    continue
                command = arguments[1:]
            if name == 'deno' and (command == ['--version'] or (
                command[:1] == ['run'] and {'--no-remote', '--cached-only'} <= set(command)
            )):
                return
            if name == 'ffmpeg' and command[:1] == ['-nostdin'] and '-protocol_whitelist' in command:
                return
            if name == 'ffprobe' and command[:2] == ['-v', 'error'] and '-protocol_whitelist' in command:
                return
        raise UserError('A origem requer um recurso externo que a aplicação não oferece.')

    sys.addaudithook(check_process)


def main(folder):
    # The supervisor attaches the Windows Job Object before releasing this gate.
    if sys.stdin.buffer.read(1) != b'1':
        return
    logging.basicConfig(level=logging.INFO)
    restrict_worker_network()
    plugin_dirs.value = []
    input_data = json.loads((folder / 'input.json').read_text(encoding='utf-8'))
    deno = shutil.which('deno')
    restrict_child_processes(deno, folder)
    runtimes = {'deno': {'path': deno}} if deno else {}
    parameters = {
        'quiet': True, 'noprogress': True, 'noplaylist': True, 'playlist_items': '1',
        'extract_flat': 'in_playlist', 'allow_playlist_files': False, 'socket_timeout': 8,
        'cachedir': False, 'proxy': '', 'enable_file_urls': False, 'retries': 1,
        'extractor_retries': 1, 'continuedl': False, 'js_runtimes': runtimes,
        # FFmpeg is invoked by this worker after download, never by an extractor.
        'remote_components': set(), 'ffmpeg_location': str(folder / 'unused-ffmpeg'),
    }
    try:
        url = check_url(input_data['url'])
        with MediaDL(parameters) as ydl:
            if input_data['kind'] == 'analysis':
                result = analyze(ydl, url, folder)
            else:
                result = prepare(ydl, input_data, folder)
    except Exception as error:
        LOG.exception('Operação interrompida')
        result = public_error(error)
    write_json(folder, 'result.json', result)


if __name__ == '__main__':
    main(Path(sys.argv[1]))
