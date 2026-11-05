const form = document.querySelector('#parse-form');
const input = document.querySelector('#share-input');
const clearButton = document.querySelector('#clear-button');
const parseButton = document.querySelector('#parse-button');
const parseButtonText = document.querySelector('#parse-button-text');
const message = document.querySelector('#message');
const result = document.querySelector('#result');
const mediaList = document.querySelector('#media-list');
let previewRetryUsed = false;
let autoRetrySubmit = false;

function showMessage(text, kind = 'error') {
  message.textContent = text;
  message.className = `message ${kind}`;
  message.hidden = !text;
}

function makeButton(label, className, action) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.textContent = label;
  button.addEventListener('click', action);
  return button;
}

async function downloadDirect(url, filename, button) {
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = '直连下载中...';
  showMessage('正在从抖音 CDN 直连下载，不会经过本站服务器流量...', 'info');
  try {
    const response = await fetch(url, { mode: 'cors', credentials: 'omit' });
    if (!response.ok) throw new Error(`CDN 返回 ${response.status}`);
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
    showMessage('直连下载已开始。', 'info');
  } catch (error) {
    showMessage('直连下载失败，请点击“服务器备用下载”。', 'error');
  } finally {
    button.disabled = false;
    button.textContent = previous;
  }
}

function makeDirectDownloadButton(label, url, filename) {
  return makeButton(label, 'small-button download direct', (event) => {
    downloadDirect(url, filename, event.currentTarget);
  });
}

function makeServerDownloadLink(label, downloadUrl, className = '') {
  const download = document.createElement('a');
  download.className = `small-button download server-fallback ${className}`.trim();
  download.href = downloadUrl;
  download.setAttribute('download', '');
  download.textContent = `${label} ↓`;
  return download;
}

function renderMedia(item, index, isAlbum) {
  const card = document.createElement('article');
  card.className = `media-card ${item.kind}`;

  const preview = document.createElement('div');
  preview.className = 'media-preview';
  if (item.kind === 'video') {
    const video = document.createElement('video');
    video.controls = true;
    video.preload = 'metadata';
    video.playsInline = true;
    video.src = item.preview_url;
    video.addEventListener('error', () => {
      if (!video.isConnected || result.hidden || parseButton.disabled) return;
      if (!previewRetryUsed && input.value.trim()) {
        previewRetryUsed = true;
        autoRetrySubmit = true;
        showMessage('预览链接失效，正在自动重新提取一次...', 'info');
        form.requestSubmit();
      } else {
        showMessage('视频预览暂不可用，可能是链接过期或源站波动。请重新点击“提取素材”，或尝试下载。');
      }
    });
    preview.append(video);
  } else {
    const image = document.createElement('img');
    image.src = item.preview_url;
    image.alt = `图集第 ${index + 1} 张`;
    image.loading = 'lazy';
    preview.append(image);
  }
  card.append(preview);

  const details = document.createElement('div');
  details.className = 'media-details';
  const title = document.createElement('p');
  title.className = 'media-title';
  const albumQuality = item.quality ? ` · ${item.quality}` : '';
  title.textContent = isAlbum ? `${item.kind === 'video' ? '实况视频' : '图片'} ${String(index + 1).padStart(2, '0')}${albumQuality}` : `无水印视频 · 当前可下载最高 ${item.quality || '平台可用画质'}`;
  details.append(title);
  if (item.reported_resolution) {
    const note = document.createElement('p');
    note.className = 'quality-note';
    note.textContent = `作品资料标注 ${item.reported_resolution}，但不代表该尺寸的视频文件可下载。`;
    details.append(note);
  }

  const directLink = document.createElement('p');
  directLink.className = 'media-url';
  directLink.title = item.direct_url;
  directLink.textContent = item.direct_url;
  details.append(directLink);

  const actions = document.createElement('div');
  actions.className = 'media-actions';
  actions.append(makeDirectDownloadButton(
    item.kind === 'image' ? '直连下载图片' : '直连下载无水印',
    item.direct_url,
    item.filename,
  ));
  actions.append(makeServerDownloadLink(
    item.kind === 'image' ? '服务器备用图片' : '服务器备用无水印',
    item.download_url,
  ));
  if (item.kind === 'video' && item.watermarked) {
    if (item.watermark_direct_available !== false) {
      actions.append(makeDirectDownloadButton(
        '直连下载带水印',
        item.watermarked.direct_url,
        item.watermarked.filename,
      ));
    }
    actions.append(makeServerDownloadLink(
      item.watermark_direct_available === false ? '服务器下载带水印' : '服务器备用带水印',
      item.watermarked.download_url,
      'watermarked',
    ));
  }
  details.append(actions);
  card.append(details);
  return card;
}

function renderResult(data) {
  document.querySelector('#result-type').textContent = data.kind === 'images' ? `图集 · ${data.media.length} 项` : '视频 · MP4';
  document.querySelector('#result-author').textContent = data.author;
  document.querySelector('#result-name').textContent = data.title;
  document.querySelector('#source-link').href = data.source_url;
  const albumArea = document.querySelector('#album-area');
  albumArea.replaceChildren();
  albumArea.hidden = !data.album_download_url;
  if (data.album_download_url) {
    const downloadAll = document.createElement('a');
    downloadAll.className = 'album-download server-fallback';
    downloadAll.href = data.album_download_url;
    downloadAll.textContent = `服务器打包全部 ${data.media.filter((item) => item.kind === 'image').length} 张图片 · ZIP ↓`;
    albumArea.append(downloadAll);
  }
  mediaList.replaceChildren();
  mediaList.classList.toggle('album', data.kind === 'images');
  data.media.forEach((item, index) => mediaList.append(renderMedia(item, index, data.kind === 'images')));

  const coverArea = document.querySelector('#cover-area');
  coverArea.replaceChildren();
  coverArea.hidden = !data.cover;
  if (data.cover) {
    const title = document.createElement('h4');
    title.textContent = '作品封面';
    const actions = document.createElement('div');
    actions.className = 'cover-actions';
    actions.append(
      makeDirectDownloadButton('直连下载封面', data.cover.direct_url, data.cover.filename),
      makeServerDownloadLink('服务器备用封面', data.cover.download_url),
    );
    coverArea.append(title, actions);
  }
  result.hidden = false;
  result.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!autoRetrySubmit) previewRetryUsed = false;
  autoRetrySubmit = false;
  const text = input.value.trim();
  if (!text) return;
  parseButton.disabled = true;
  parseButtonText.textContent = '正在提取...';
  showMessage('正在连接抖音并读取公开作品信息...', 'info');
  result.hidden = true;
  try {
    const response = await fetch('/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '解析失败，请稍后重试。');
    showMessage('');
    renderResult(data);
  } catch (error) {
    showMessage(error.message || '网络请求失败，请检查网络连接或稍后重试。');
  } finally {
    parseButton.disabled = false;
    parseButtonText.textContent = '提取素材';
  }
});

input.addEventListener('keydown', (event) => {
  if (
    event.key === 'Enter'
    && !event.shiftKey
    && !event.isComposing
    && input.value.trim()
    && !parseButton.disabled
  ) {
    event.preventDefault();
    form.requestSubmit();
  }
});

clearButton.addEventListener('click', () => {
  input.value = '';
  result.hidden = true;
  showMessage('');
  input.focus();
});
