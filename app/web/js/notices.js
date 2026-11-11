/**
 * Dismissible page notices.
 *
 * Every `.banner` and `.warn-box` rendered in a view gets a close control. The
 * dismissed keys live in the workspace settings (`dismissed_notices`), loaded
 * from the bootstrap, so a dismissal survives polls, app restarts and the
 * random loopback port the macOS shell picks each launch.
 *
 * A key is content-derived by default, so a notice returns when its text
 * changes (a count, a host list, an error). `data-notice-id="stable-name"` opts
 * a notice into a stable key, for persistent hints that should not come back
 * just because a count moved. `data-no-dismiss` keeps status/safety notices
 * (queue progress, prune warning) non-dismissible.
 */
import { api } from './api.js';

let dismissed = {};

function hashText(text) {
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function noticeKey(node) {
  if (node.dataset.noticeId) return `id:${node.dataset.noticeId}`;
  const text = (node.textContent || '').replace(/\s+/g, ' ').trim();
  const kind = node.classList.contains('banner') ? 'banner' : 'box';
  return `${kind}:${hashText(text)}`;
}

/** Replace the local view of the server's dismissal map. */
export function setDismissedNotices(map) {
  dismissed = map && typeof map === 'object' ? map : {};
}

function decorate(node) {
  if (node.dataset.noticeReady === '1') return;
  if (node.hasAttribute('data-no-dismiss')) return;
  const key = noticeKey(node);
  if (Object.prototype.hasOwnProperty.call(dismissed, key)) {
    node.remove();
    return;
  }
  node.dataset.noticeReady = '1';
  node.dataset.noticeKey = key;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'notice-close';
  button.dataset.dismissNotice = '1';
  button.setAttribute('aria-label', 'Dismiss notice');
  button.title = 'Dismiss';
  button.textContent = '×';
  node.appendChild(button);
  node.classList.add('notice-dismissible');
}

/** Decorate (and drop dismissed) notices inside an element or fragment. */
export function applyDismissals(root) {
  root.querySelectorAll('.banner, .warn-box').forEach((node) => decorate(node));
}

let templateNode = null;

function template() {
  if (!templateNode) templateNode = document.createElement('template');
  return templateNode;
}

/** Filter a view's HTML before it is inserted or compared with the DOM. */
export function filterNoticeHtml(html) {
  if (!html || (!html.includes('class="banner') && !html.includes('class="warn-box'))) {
    return html;
  }
  const node = template();
  node.innerHTML = html;
  applyDismissals(node.content);
  return node.innerHTML;
}

/** Forget every dismissal so closed notices appear again. */
export async function clearDismissedNotices() {
  try {
    await api.post('/api/notices', { restore: true });
    dismissed = {};
    return true;
  } catch {
    return false;
  }
}

export function installNoticeDismissal() {
  document.addEventListener('click', (event) => {
    const button = event.target.closest?.('[data-dismiss-notice]');
    if (!button) return;
    event.preventDefault();
    const notice = button.closest('[data-notice-key]');
    if (!notice) return;
    const key = notice.dataset.noticeKey;
    if (key) {
      dismissed[key] = Date.now();
      // The local map already hides it; the request only makes it durable.
      api.post('/api/notices', { key }).catch(() => {});
    }
    notice.remove();
  });
}
