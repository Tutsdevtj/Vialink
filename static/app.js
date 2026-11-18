'use strict';

const lookup = document.querySelector('#lookup');
const urlField = document.querySelector('#url');
const lookupButton = document.querySelector('#lookup-button');
const lookupLabel = document.querySelector('#lookup-label');
const lookupError = document.querySelector('#lookup-error');
const lookupStatus = document.querySelector('#lookup-status');
const resultArea = document.querySelector('#result-area');
const empty = document.querySelector('#empty');
const skeleton = document.querySelector('#skeleton');
const result = document.querySelector('#result');
const title = document.querySelector('#video-title');
const duration = document.querySelector('#duration');
const durationText = document.querySelector('#duration-text');
const origin = document.querySelector('#origin');
const thumbnail = document.querySelector('#thumbnail');
const player = document.querySelector('#player');
const noOptions = document.querySelector('#no-options');
const form = document.querySelector('#download-form');
const option = document.querySelector('#option');
const prepareButton = document.querySelector('#prepare-button');
const prepareLabel = document.querySelector('#prepare-label');
const downloadError = document.querySelector('#download-error');
const downloadStatus = document.querySelector('#download-status');
const progressPanel = document.querySelector('#progress-panel');
const phase = document.querySelector('#phase');
const percent = document.querySelector('#percent');
const meter = document.querySelector('#progress');
const ready = document.querySelector('#ready');
const readyDetails = document.querySelector('#ready-details');
const fileLink = document.querySelector('#file');

const phases = {
  starting: 'Preparando o download…',
  download_video: 'Baixando vídeo…',
  download_audio: 'Baixando áudio…',
  merge: 'Juntando áudio e vídeo…',
  mux: 'Finalizando MP4…',
  convert: 'Convertendo para MP3…',
};
const number = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 1 });
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let analysisId;
let busy = false;
let expiryTimer;

function showError(element, message = '') {
  element.hidden = !message;
  element.textContent = message;
}

class ApiError extends Error {}

async function api(path, body) {
  const init = { signal: AbortSignal.timeout(8000), cache: 'no-store' };
  if (body !== undefined) {
    init.method = 'POST';
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const data = await response.json();
  if (!response.ok) {
    throw new ApiError(data.error || 'Não foi possível concluir a solicitação. Tente novamente.');
  }
  return data;
}

async function poll(path, onProgress) {
  let failures = 0;
  for (;;) {
    let current;
    try {
      current = await api(path);
      failures = 0;
    } catch (error) {
      if (error instanceof ApiError || ++failures >= 3) throw error;
      await pause(650);
      continue;
    }
    if (current.state === 'ready') return current;
    if (current.state === 'error') throw new ApiError(current.error);
    onProgress?.(current);
    await pause(350);
  }
}

function messageOf(error) {
  return error instanceof ApiError ? error.message : 'Não foi possível falar com a aplicação local. Tente novamente.';
}

function fileSize(bytes) {
  if (bytes < 1000) return `${number.format(bytes)} B`;
  if (bytes < 1000000) return `${number.format(bytes / 1000)} kB`;
  return `${number.format(bytes / 1000000)} MB`;
}

function displayDuration(seconds) {
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor(whole / 60) % 60;
  const tail = `${String(whole % 60).padStart(2, '0')}`;
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${tail}` : `${minutes}:${tail}`;
}

function clearReady() {
  clearTimeout(expiryTimer);
  ready.hidden = true;
  fileLink.removeAttribute('href');
}

function renderVideo(video) {
  title.textContent = video.title;
  title.tabIndex = -1;
  duration.hidden = !Number.isFinite(video.duration);
  if (!duration.hidden) durationText.textContent = displayDuration(video.duration);
  origin.href = video.origin;

  thumbnail.hidden = !video.thumbnail;
  if (video.thumbnail) thumbnail.src = video.thumbnail;
  player.hidden = !video.preview || !player.canPlayType('video/mp4');
  if (!player.hidden) {
    if (video.thumbnail) player.poster = video.thumbnail;
    player.src = video.preview;
  }

  option.replaceChildren();
  for (const item of video.options) {
    const parts = [item.format.toUpperCase(), item.quality];
    if (item.silent) parts.push('sem áudio');
    if (item.estimated) parts.push(`≈ ${fileSize(item.estimated)}`);
    option.add(new Option(parts.join(' · '), item.id));
  }
  // Prefer a common resolution when multiple video formats are available.
  if (video.options.some((item) => item.id === 'v-720')) option.value = 'v-720';
  form.hidden = !video.options.length;
  noOptions.hidden = !!video.options.length;
  result.hidden = false;
}

thumbnail.addEventListener('error', () => { thumbnail.hidden = true; });
player.addEventListener('error', () => { player.hidden = true; });

urlField.addEventListener('input', () => {
  urlField.removeAttribute('aria-invalid');
  showError(lookupError);
  lookupLabel.textContent = 'Download';
});

lookup.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  showError(lookupError);
  urlField.removeAttribute('aria-invalid');
  const url = urlField.value.trim();
  if (!/^https?:\/\//i.test(url) || !URL.canParse(url)) {
    urlField.setAttribute('aria-invalid', 'true');
    showError(lookupError, 'Informe uma URL completa que comece com http:// ou https://.');
    urlField.focus();
    return;
  }

  busy = true;
  lookupButton.disabled = true;
  urlField.readOnly = true;
  lookupLabel.textContent = 'Consultando…';
  resultArea.setAttribute('aria-busy', 'true');
  lookupStatus.textContent = 'Consultando o vídeo…';
  empty.hidden = true;
  result.hidden = true;
  skeleton.hidden = false;
  clearReady();
  player.removeAttribute('src');
  player.removeAttribute('poster');
  player.load();
  thumbnail.removeAttribute('src');
  showError(downloadError);
  prepareLabel.textContent = 'Baixar arquivo';
  downloadStatus.textContent = '';

  try {
    const { id } = await api('/api/analises', { url });
    const { video } = await poll(`/api/analises/${id}`);
    analysisId = id;
    renderVideo(video);
    lookupStatus.textContent = 'Consulta concluída.';
    if (lookup.contains(document.activeElement) || document.activeElement === document.body) title.focus();
    lookupLabel.textContent = 'Download';
  } catch (error) {
    empty.hidden = false;
    lookupStatus.textContent = '';
    showError(lookupError, messageOf(error));
    lookupLabel.textContent = 'Tentar novamente';
  } finally {
    skeleton.hidden = true;
    resultArea.removeAttribute('aria-busy');
    lookupButton.disabled = false;
    urlField.readOnly = false;
    busy = false;
  }
});

function updateProgress({ phase: current, percent: value }) {
  const label = phases[current] || phases.starting;
  if (phase.textContent !== label) {
    phase.textContent = label;
    downloadStatus.textContent = label;
  }
  if (Number.isFinite(value)) {
    meter.value = value;
    percent.textContent = `${value}%`;
  } else {
    meter.removeAttribute('value');
    percent.textContent = '';
  }
}

function showReady(download) {
  ready.hidden = false;
  fileLink.href = download.file;
  readyDetails.textContent = fileSize(download.size);
  downloadStatus.textContent = 'Arquivo pronto. Se o download não começar, selecione Salvar arquivo.';
  fileLink.click();
  expiryTimer = setTimeout(() => {
    clearReady();
    downloadStatus.textContent = '';
    showError(downloadError, 'O arquivo expirou. Use Baixar arquivo para prepará-lo novamente.');
  }, Math.max(0, Date.parse(download.expires) - Date.now()));
}

option.addEventListener('change', () => {
  clearReady();
  showError(downloadError);
  prepareLabel.textContent = 'Baixar arquivo';
  downloadStatus.textContent = '';
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !analysisId || !option.value) return;
  busy = true;
  showError(downloadError);
  clearReady();
  lookupButton.disabled = true;
  urlField.readOnly = true;
  prepareButton.disabled = true;
  option.disabled = true;
  prepareLabel.textContent = 'Preparando…';
  phase.textContent = '';
  updateProgress({ phase: 'starting' });
  progressPanel.hidden = false;
  try {
    const { id } = await api('/api/downloads', { analysis: analysisId, option: option.value });
    const download = await poll(`/api/downloads/${id}`, updateProgress);
    showReady(download);
    prepareLabel.textContent = 'Baixar arquivo';
  } catch (error) {
    downloadStatus.textContent = '';
    showError(downloadError, messageOf(error));
    prepareLabel.textContent = 'Tentar novamente';
  } finally {
    progressPanel.hidden = true;
    lookupButton.disabled = false;
    urlField.readOnly = false;
    prepareButton.disabled = false;
    option.disabled = false;
    busy = false;
  }
});
