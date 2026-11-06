import { streamRun, ApiError } from './api.js';

/* ------------------------------------------------------------------ icons */
const ICONS = {
  fleet: '<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2" fill="currentColor"/>',
  services: '<rect x="4" y="6.5" width="16" height="13.5" rx="2.5"/><path d="M4 13h16"/>',
  updates: '<path d="M12 20V5"/><path d="m6.5 10.5 5.5-5.5 5.5 5.5"/><path d="M3 20h6.8"/><path d="M14.2 20H21"/>',
  hosts: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/>',
  activity: '<path d="M3 12h4l2-5.5 4 11 2-5.5h6"/>',
  settings: '<path d="M3 6h9"/><path d="M18 6h3"/><circle cx="15" cy="6" r="3" fill="currentColor" stroke="none"/><path d="M3 12h3"/><path d="M12 12h9"/><circle cx="9" cy="12" r="3" fill="currentColor" stroke="none"/><path d="M3 18h10"/><path d="M19 18h2"/><circle cx="16" cy="18" r="3" fill="currentColor" stroke="none"/>',
  refresh: '<path d="M20.5 12a8.5 8.5 0 1 1-8.5-8.5c2.38 0 4.66.95 6.37 2.59L20.5 8"/><path d="M20.5 3.5V8h-5"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  play: '<path d="m7 4 13 8-13 8V4Z"/>',
  restart: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v6h6"/>',
  terminal: '<path d="M5 7l5 5-5 5"/><path d="M13 17h6"/>',
  upload: '<path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M4 18v2h16v-2"/>',
  download: '<path d="M12 4v12"/><path d="m6 10 6 6 6-6"/><path d="M4 20h16"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M20 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h5"/>',
  shield: '<path d="M12 3.2 19 6v5.8c0 4.4-2.9 7.4-7 8.9-4.1-1.5-7-4.5-7-8.9V6l7-2.8Z"/><path d="M8.6 12l2.4 2.4 4.4-4.4"/>',
  alert: '<path d="M12 3 2 21h20L12 3Z"/><path d="M12 9v5"/><circle cx="12" cy="18" r=".6" fill="currentColor"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  thermo: '<path d="M10 14.5V5a2 2 0 1 1 4 0v9.5a4 4 0 1 1-4 0Z"/>',
  memory: '<path d="M5 8h14v9H5z"/><path d="M8 8V4M12 8V4M16 8V4M8 17v3M12 17v3M16 17v3M2 11h3M2 14h3M19 11h3M19 14h3"/>',
  disk: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  network: '<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><path d="M12 7.5v4M12 11.5 6 17M12 11.5l6 5.5"/>',
  git: '<path d="M6 8v10"/><circle cx="6" cy="5.5" r="2.2" fill="currentColor" stroke="none"/><circle cx="6" cy="20.5" r="2.2" fill="currentColor" stroke="none"/><path d="M6 10h5.5a2 2 0 0 1 2 2v2.5"/><circle cx="13.5" cy="16.5" r="2.2" fill="currentColor" stroke="none"/>',
  push: '<path d="M2.5 12h17"/><path d="m15 7.5 4.5 4.5-4.5 4.5"/><path d="M8 3v6.8"/><path d="M8 14.2V21"/>',
  code: '<path d="m8 6-5 6 5 6"/><path d="m16 6 5 6-5 6"/>',
  commit: '<circle cx="12" cy="12" r="3.2"/><path d="M3 12h5.2M15.8 12H21"/>',
  branch: '<circle cx="6" cy="6" r="2.3"/><circle cx="6" cy="18" r="2.3"/><circle cx="18" cy="7" r="2.3"/><path d="M6 8.3v7.4M8.3 6h3.2A6.5 6.5 0 0 1 18 9.3"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l2 2M14 9l2 2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  chevronLeft: '<path d="m15 6-6 6 6 6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><circle cx="12" cy="7.6" r=".7" fill="currentColor"/>',
  file: '<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/>',
  edit: '<path d="M4 20h4l10-10-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h9"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 15h10l1-15"/><path d="M10 11v6M14 11v6"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  power: '<path d="M12 3v9"/><path d="M6.4 6.6a8 8 0 1 0 11.2 0"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 13 9 5 9-5"/>',
  box: '<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/>',
  drop: '<path d="M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11Z"/>',
  arrowUp: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  arrowDown: '<path d="M12 5v14M6 13l6 6 6-6"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4 2v-8L3 5Z"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  wifi: '<path d="M2 8.5a16 16 0 0 1 20 0M5.5 12.5a11 11 0 0 1 13 0M9 16.5a6 6 0 0 1 6 0"/><circle cx="12" cy="20" r=".8" fill="currentColor"/>',
  moon: '<path d="M20.5 14.6A8.6 8.6 0 0 1 9.4 3.5a8.6 8.6 0 1 0 11.1 11.1Z"/>',
  sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2v2.4M12 19.6V22M2 12h2.4M19.6 12H22M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M19.1 4.9l-1.7 1.7M6.6 17.4l-1.7 1.7"/>',
  monitor: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
  overview: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/>',
  cloud: '<path d="M17.5 19H7a5 5 0 1 1 1.3-9.8A6 6 0 0 1 19 12.5 3.5 3.5 0 0 1 17.5 19Z"/>',
};

export function icon(name, size = 16) {
  const path = ICONS[name] || ICONS.box;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
}

/* ----------------------------------------------------------------- format */
export const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));

export function duration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '—';
  let value = Math.max(0, Math.floor(Number(seconds)));
  const days = Math.floor(value / 86400);
  value %= 86400;
  const hours = Math.floor(value / 3600);
  value %= 3600;
  const minutes = Math.floor(value / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${value}s`;
}

export function ago(timestamp) {
  if (!timestamp) return 'never';
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(timestamp)));
  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function clock(timestamp) {
  if (!timestamp) return '—';
  return new Date(Number(timestamp) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function dateTime(timestamp) {
  if (!timestamp) return '—';
  return new Date(Number(timestamp) * 1000).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

export function bytes(value) {
  if (value === null || value === undefined) return '—';
  let number = Number(value);
  if (!Number.isFinite(number)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  while (number >= 1024 && index < units.length - 1) { number /= 1024; index += 1; }
  return `${number >= 100 || index === 0 ? number.toFixed(0) : number.toFixed(1)} ${units[index]}`;
}

export function tempClass(value) {
  if (value === null || value === undefined) return '';
  if (value >= 82) return 'temp-hot';
  if (value >= 75) return 'temp-warm';
  return 'temp-cool';
}

export function pct(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return `${Number(value).toFixed(digits)}%`;
}

export function plural(count, singular, pluralForm = '') {
  const value = Number(count) || 0;
  return `${value} ${value === 1 ? singular : (pluralForm || `${singular}s`)}`;
}

export function shortSha(value, length = 12) {
  return value ? String(value).replace('sha256:', '').slice(0, length) : '';
}

/* ----------------------------------------------------------------- status */
const STATUS_TONE = {
  up: 'ok', online: 'ok', synced: 'ok', current: 'ok', ok: 'ok', success: 'ok', running: 'ok',
  degraded: 'warn', warn: 'warn', warning: 'warn', extra: 'warn', unpinned: 'warn',
  'check-failed': 'warn', 'ahead-of-stable': 'warn', 'arch-unavailable': 'warn', starting: 'warn', restarting: 'warn',
  down: 'bad', offline: 'bad', failed: 'bad', error: 'bad', unhealthy: 'bad', oom: 'bad',
  'update-available': 'warn', notify: 'info', info: 'info', partial: 'info',
  managed: 'purple', 'digest-pinned': 'purple', unmanaged: 'neutral', 'not-deployed': 'neutral',
  unknown: 'neutral', 'not-checked': 'neutral', off: 'neutral', interrupted: 'warn', cancelled: 'neutral',
};

export function tone(status) {
  return STATUS_TONE[String(status || '').toLowerCase()] || 'neutral';
}

export function pill(status, label = null) {
  const value = String(status || 'unknown');
  const text = label ?? value.replace(/-/g, ' ');
  return `<span class="pill ${tone(value)}"><span class="dot"></span>${esc(text)}</span>`;
}

export function statusDot(status) {
  return `<span class="status-dot ${tone(status)}"></span>`;
}

export function safetyBadge(level) {
  const value = String(level || 'medium');
  return `<span class="safety ${esc(value)}">${esc(value)}</span>`;
}

/* ---------------------------------------------------------------- graphics */
export function sparkline(points, valueKey = 'temp_c', options = {}) {
  const width = options.width || 120;
  const height = options.height || 30;
  const values = (points || []).map((point) => {
    const raw = typeof point === 'number' ? point : point?.[valueKey];
    return raw === null || raw === undefined || Number.isNaN(Number(raw)) ? null : Number(raw);
  });
  if (!values.some((value) => value !== null)) return '';
  const known = values.filter((value) => value !== null);
  const min = Math.min(...known);
  const max = Math.max(...known);
  const span = max - min || 1;
  const step = values.length > 1 ? width / (values.length - 1) : width;
  let line = '';
  let started = false;
  let first = -1;
  let last = -1;
  values.forEach((value, index) => {
    if (value === null) { started = false; return; }
    if (first === -1) first = index;
    last = index;
    const x = index * step;
    const y = height - 3 - ((value - min) / span) * (height - 6);
    if (!started) {
      line += `M${x.toFixed(1)} ${y.toFixed(1)}`;
      started = true;
    } else {
      line += `L${x.toFixed(1)} ${y.toFixed(1)}`;
    }
  });
  const areaPath = line ? `${line}L${(last * step).toFixed(1)} ${height}L${(first * step).toFixed(1)} ${height}Z` : '';
  const color = options.color || 'var(--accent)';
  const label = options.label || 'trend';
  return `<div class="spark" style="height:${height}px">
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">
      <path d="${areaPath}" fill="${color}" opacity="0.09"></path>
      <path d="${line}" fill="none" stroke="${color}" stroke-width="1.4" vector-effect="non-scaling-stroke"></path>
    </svg>
  </div>`;
}

export function uptimeStrip(points, key = 'status', label = 'Availability history') {
  const rows = (points || []).slice(-48);
  if (!rows.length) {
    return `<div class="uptime-strip" role="img" aria-label="${esc(label)}: no samples yet">${'<span class="uptime-seg off"></span>'.repeat(24)}</div>`;
  }
  return `<div class="uptime-strip" role="img" aria-label="${esc(label)}: ${rows.length} samples">${rows.map((point) => {
    const value = point[key] || (point.reachable ? 'up' : 'down');
    const cls = value === 'up' ? '' : value === 'degraded' ? 'warn' : value === 'unknown' ? 'off' : 'bad';
    return `<span class="uptime-seg ${cls}" title="${esc(value)} · ${esc(dateTime(point.ts))}"></span>`;
  }).join('')}</div>`;
}

/* ------------------------------------------------------------------ toasts */
export function toast(message, type = 'ok', detail = '', timeout = 5200) {
  const root = document.getElementById('toast-root');
  if (!root) return;
  const element = document.createElement('div');
  element.className = `toast ${type}`;
  element.setAttribute('role', type === 'err' ? 'alert' : 'status');
  const iconName = type === 'err' ? 'alert' : type === 'warn' ? 'alert' : 'check';
  element.innerHTML = `${icon(iconName)}<div class="body"><div class="title">${esc(message)}</div>${detail ? `<div class="detail">${esc(detail)}</div>` : ''}</div>
    <button class="toast-close" type="button" aria-label="Dismiss notification">${icon('x', 12)}</button>`;
  root.appendChild(element);
  const persistent = timeout === 0;
  const life = persistent ? 0 : (timeout || (type === 'err' ? 9000 : 5200));
  let timer = null;
  let removed = false;
  const remove = () => {
    if (removed) return;
    removed = true;
    if (timer) clearTimeout(timer);
    element.classList.add('out');
    setTimeout(() => element.remove(), 220);
  };
  const arm = (delay) => {
    if (timer) clearTimeout(timer);
    if (delay > 0) timer = setTimeout(remove, delay);
  };
  const pause = () => { if (timer) clearTimeout(timer); };
  const resume = () => { if (!persistent && !removed) arm(2600); };
  element.addEventListener('click', (event) => {
    if (!event.target.closest('.toast-close')) remove();
  });
  element.querySelector('.toast-close').addEventListener('click', (event) => {
    event.stopPropagation();
    remove();
  });
  element.addEventListener('mouseenter', pause);
  element.addEventListener('mouseleave', resume);
  element.addEventListener('focusin', pause);
  element.addEventListener('focusout', resume);
  arm(life);
  return element;
}

/* ------------------------------------------------------------------ modals */
const modalStack = [];
let modalCounter = 0;

export function modalOpen() {
  return modalStack.length > 0;
}

export function closeTopModal() {
  const top = modalStack[modalStack.length - 1];
  if (top) top.close();
}

function setShellInert(inert) {
  const shell = document.getElementById('app');
  if (!shell) return;
  if ('inert' in shell) shell.inert = inert;
  if (inert) shell.setAttribute('aria-hidden', 'true');
  else shell.removeAttribute('aria-hidden');
}

export function openModal({ title, subtitle = '', body = '', footer = '', className = '', onClose = null, dismissible = true }) {
  const root = document.getElementById('modal-root');
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  const titleId = `modal-title-${++modalCounter}`;
  overlay.innerHTML = `<div class="modal ${className}" role="dialog" aria-modal="true" aria-labelledby="${titleId}" tabindex="-1">
    <div class="modal-head">
      <div><div class="modal-title" id="${titleId}">${esc(title)}</div>${subtitle ? `<div class="modal-sub">${esc(subtitle)}</div>` : ''}</div>
      <button class="modal-close" type="button" aria-label="Close dialog">${icon('x', 16)}</button>
    </div>
    <div class="modal-body"></div>
    ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
  </div>`;
  const modal = overlay.querySelector('.modal');
  const bodyElement = overlay.querySelector('.modal-body');
  if (body instanceof Node) bodyElement.appendChild(body);
  else bodyElement.innerHTML = body;
  const footElement = overlay.querySelector('.modal-foot');
  const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;

  const focusableElements = () => [...overlay.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])',
  )].filter((element) => element.offsetParent !== null);

  overlay.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab' || !modalStack.length || modalStack[modalStack.length - 1].overlay !== overlay) return;
    const items = focusableElements();
    if (!items.length) {
      event.preventDefault();
      modal.focus();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === modal || !overlay.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === modal)) {
      event.preventDefault();
      first.focus();
    }
  });

  const entry = { overlay, close };
  let closing = false;
  function close() {
    if (closing) return;
    closing = true;
    const index = modalStack.indexOf(entry);
    if (index !== -1) modalStack.splice(index, 1);
    overlay.classList.add('out');
    overlay.style.animation = 'fade .12s ease both reverse';
    document.body.style.overflow = modalStack.length ? 'hidden' : '';
    if (!modalStack.length) setShellInert(false);
    setTimeout(() => {
      overlay.remove();
      if (modalStack.length) {
        const next = modalStack[modalStack.length - 1];
        next.overlay.querySelector('input, textarea, select, button.primary, .modal-close')?.focus();
      } else if (previousFocus?.isConnected) {
        previousFocus.focus();
      }
    }, 130);
    if (onClose) { try { onClose(); } catch { /* ignore */ } }
  }

  overlay.querySelector('.modal-close').addEventListener('click', close);
  if (dismissible) {
    overlay.addEventListener('mousedown', (event) => { if (event.target === overlay) close(); });
  }
  modalStack.push(entry);
  root.appendChild(overlay);
  document.body.style.overflow = 'hidden';
  setShellInert(true);
  setTimeout(() => {
    const focusTarget = [
      '[autofocus]',
      'input:not([disabled]):not([type="hidden"])',
      'textarea:not([disabled])',
      'select:not([disabled])',
      'button.primary',
      '[data-cancel]',
      '.modal-close',
      'button:not([disabled])',
    ].map((selector) => overlay.querySelector(selector)).find(Boolean);
    (focusTarget || modal).focus();
  }, 30);
  return { overlay, modal, body: bodyElement, footer: footElement, close };
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && modalStack.length) {
    const top = modalStack[modalStack.length - 1];
    top.close();
  }
});

export function confirmDialog({
  title,
  message,
  detail = '',
  confirmLabel = 'Confirm',
  danger = false,
  requireText = '',
}) {
  return new Promise((resolve) => {
    let settled = false;
    const prompt = requireText
      ? `<div class="field"><label>Type <span class="mono">${esc(requireText)}</span> to confirm</label><input type="text" id="confirm-text" autocomplete="off"></div>`
      : '';
    const ref = openModal({
      title,
      className: 'narrow',
      body: `<div class="muted">${esc(message)}</div>${detail ? `<div class="mt-2">${detail}</div>` : ''}${prompt}`,
      footer: `<div class="spacer"></div><button class="btn ghost" data-cancel>Cancel</button><button class="btn ${danger ? 'danger' : 'primary'}" data-confirm disabled>${esc(confirmLabel)}</button>`,
      onClose: () => { if (!settled) { settled = true; resolve(false); } },
    });
    const confirmButton = ref.footer.querySelector('[data-confirm]');
    const input = ref.body.querySelector('#confirm-text');
    const validate = () => {
      confirmButton.disabled = Boolean(requireText) && input && input.value.trim() !== requireText;
    };
    if (input) {
      input.addEventListener('input', validate);
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !confirmButton.disabled) {
          event.preventDefault();
          settled = true;
          ref.close();
          resolve(true);
        }
      });
      validate();
    } else {
      confirmButton.disabled = false;
    }
    ref.footer.querySelector('[data-cancel]').addEventListener('click', () => { settled = true; ref.close(); resolve(false); });
    confirmButton.addEventListener('click', () => { settled = true; ref.close(); resolve(true); });
  });
}

/* ------------------------------------------------------------------- runs */
export function runModal(ctx, runId, title = 'Run') {
  const ref = openModal({
    title,
    subtitle: 'Streaming live from the console server',
    className: 'wide',
    body: `<div class="run-status" id="run-status"><span class="spinner"></span><span class="muted">starting…</span></div>
      <div class="log" id="run-log"><span class="log-empty">waiting for output…</span></div>
      <div id="run-result" class="mt-2"></div>`,
    footer: `<span class="note" id="run-note">You can close this and follow the run from Activity.</span><div class="spacer"></div>
      <button class="btn ghost" data-close>Close</button><button class="btn primary" data-refresh>Refresh dashboard</button>`,
    onClose: () => { if (closeStream) closeStream(); },
  });
  let closeStream = null;
  const logElement = ref.body.querySelector('#run-log');
  const statusElement = ref.body.querySelector('#run-status');
  const resultElement = ref.body.querySelector('#run-result');
  const noteElement = ref.footer.querySelector('#run-note');
  let lineCount = 0;
  let autoScroll = true;
  let finished = false;

  const append = (lines) => {
    if (!lines.length) return;
    if (lineCount === 0) logElement.innerHTML = '';
    const nearBottom = logElement.scrollHeight - logElement.scrollTop - logElement.clientHeight < 60;
    autoScroll = nearBottom;
    const fragment = document.createDocumentFragment();
    for (const line of lines) {
      const row = document.createElement('span');
      row.className = 'ln';
      const text = line.text || '';
      let cls = '';
      if (text.startsWith('✓')) cls = 'ok';
      else if (text.startsWith('!') || text.startsWith('⚠')) cls = 'warn';
      else if (text.startsWith('✗')) cls = 'err';
      else if (text.startsWith('>>')) cls = 'head';
      else if (text.trim().startsWith('$')) cls = 'cmd';
      else if (text.startsWith('   ')) cls = '';
      const time = new Date((line.ts || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      row.innerHTML = `<span class="ts">${esc(time)}</span><span class="${cls}">${esc(text)}</span>`;
      fragment.appendChild(row);
      lineCount += 1;
    }
    logElement.appendChild(fragment);
    if (autoScroll) logElement.scrollTop = logElement.scrollHeight;
  };

  const setStatus = (status, error = '') => {
    if (status === 'running') {
      statusElement.innerHTML = `<span class="spinner"></span><span class="muted">running…</span>`;
    } else if (status === 'ok') {
      statusElement.innerHTML = `${pill('ok', 'complete')}<span class="muted">finished successfully</span>`;
      noteElement.textContent = 'Run complete. Review the repo diff, then commit when ready.';
    } else {
      statusElement.innerHTML = `${pill('failed', status || 'failed')}<span class="muted">${esc(error || 'run failed')}</span>`;
      noteElement.textContent = 'The run failed. Check the log and the stack state before retrying.';
    }
  };

  ref.footer.querySelector('[data-close]').addEventListener('click', ref.close);
  ref.footer.querySelector('[data-refresh]').addEventListener('click', () => {
    ref.close();
    ctx.refreshState({ force: true });
  });

  closeStream = streamRun(runId, {
    onLines: (lines) => append(lines),
    onStatus: (status) => setStatus(status),
    onDone: (meta) => {
      finished = true;
      setStatus(meta.status, meta.error);
      if (meta.result && Object.keys(meta.result).length) {
        resultElement.innerHTML = `<details><summary class="muted tiny plain-summary">run details</summary><pre class="log mt-1">${esc(JSON.stringify(meta.result, null, 2))}</pre></details>`;
      }
      if (meta.status === 'ok') toast('Run complete', 'ok', title);
      else toast('Run failed', 'err', meta.error || title);
      ctx.refreshState({ force: true });
    },
    onError: (error) => {
      if (finished) return;
      setStatus('failed', error?.message || 'stream error');
    },
  });

  return ref;
}

/* -------------------------------------------------------------- clipboard */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied to clipboard', 'ok');
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); toast('Copied to clipboard', 'ok'); }
    catch { toast('Copy failed', 'err'); }
    area.remove();
  }
}

export function errorText(error) {
  if (error instanceof ApiError) {
    return error.message || `HTTP ${error.status}`;
  }
  return error?.message || String(error);
}
