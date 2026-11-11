import * as ui from '../ui.js';

const { esc, icon } = ui;

export function pageHead({ title, subtitle = '', actions = '', back = null, badges = '' }) {
  return `<div class="view-head">
    <div class="view-head-text">
      ${back ? `<a class="back-link" href="${esc(back.href)}">${icon('chevronLeft', 11)} ${esc(back.label)}</a>` : ''}
      <h1 class="view-title">${esc(title)} ${badges}</h1>
      ${subtitle ? `<div class="view-sub">${subtitle}</div>` : ''}
    </div>
    <div class="view-actions">${actions}</div>
  </div>`;
}

export function statTile({ label, value, sub = '', iconName = 'box', toneName = '', valueTone = '' }) {
  return `<div class="stat">
    <div class="stat-icon ${toneName}">${icon(iconName)}</div>
    <div>
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value ${valueTone}">${value}</div>
      ${sub ? `<div class="stat-sub">${sub}</div>` : ''}
    </div>
  </div>`;
}

export function emptyState(iconName, title, text, action = '') {
  return `<div class="empty">${icon(iconName, 28)}<strong>${esc(title)}</strong><p>${esc(text)}</p>${action}</div>`;
}

export function hostHref(alias) {
  return `#/host/${encodeURIComponent(alias)}`;
}

export function stackHref(host, name) {
  return `#/stack/${encodeURIComponent(host)}/${encodeURIComponent(name)}`;
}

export function hostLabel(alias) {
  return `<a href="${hostHref(alias)}" class="mono">${esc(alias)}</a>`;
}

export function stackLabel(host, name) {
  return `<a href="${stackHref(host, name)}" class="stack-title">${esc(name)}</a><span class="sub mono">${esc(host)}</span>`;
}

export function stateBanner(ctx) {
  const state = ctx.state;
  if (!state) return '';
  const summary = state.summary || {};
  if (state.warming_up) {
    return `<div class="banner" data-no-dismiss>${icon('clock')}<span>Collecting the first fleet snapshot…</span></div>`;
  }
  if (summary.hosts_offline?.length) {
    return `<div class="banner bad">${icon('alert')}<span><strong>${esc(summary.hosts_offline.join(', '))}</strong> unreachable — data shown for those hosts may be stale.</span><a class="btn sm ghost" href="#/hosts">Hosts</a></div>`;
  }
  if (state.git?.dirty) {
    return `<div class="banner">${icon('git')}<span><strong>${state.git.entries.length} repo file(s) modified</strong> under stacks/ — deploy is from the working tree; commit when the change is verified.</span><button class="btn sm ghost" data-open-commit>${icon('git', 12)} Review &amp; commit</button></div>`;
  }
  return '';
}

export function syncPill(state) {
  const value = String(state || 'unknown');
  const labels = {
    synced: 'in sync', drift: 'drift', extra: 'extra files',
    'not-deployed': 'not deployed', unknown: 'sync unknown',
  };
  const tone = value === 'drift' ? 'degraded' : value === 'extra' ? 'info' : value;
  return ui.pill(tone, labels[value] || value);
}

export function runtimePill(status) {
  const labels = {
    up: 'up', degraded: 'degraded', down: 'down',
    'not-deployed': 'not deployed', unknown: 'unknown',
  };
  return ui.pill(status, labels[status] || status);
}

export function updateBadge(row) {
  if (!row?.updates?.length) return '';
  return `<a class="pill warn" href="#/updates">${icon('updates', 11)} ${row.updates.length} update${row.updates.length === 1 ? '' : 's'}</a>`;
}

export function tempTone(value) {
  if (value === null || value === undefined) return '';
  if (value >= 82) return 'bad';
  if (value >= 75) return 'warn';
  return '';
}

function percentTone(value, warnAt, badAt) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '';
  if (Number(value) >= badAt) return 'bad';
  if (Number(value) >= warnAt) return 'warn';
  return '';
}

export function memTone(value) {
  return percentTone(value, 80, 90);
}

export function diskTone(value) {
  return percentTone(value, 85, 92);
}

export function loadTone(value) {
  return percentTone(value, 75, 90);
}

export function toneClass(tone) {
  return tone === 'bad' ? 'text-bad' : tone === 'warn' ? 'text-warn' : '';
}

export function bar(value, label, tone = '') {
  const safe = value === null || value === undefined ? 0 : Math.max(0, Math.min(100, Number(value)));
  return `<div class="bar-row">
    <span class="bar-label">${esc(label)}</span>
    <div class="bar-track"><div class="bar-fill ${tone}" style="width:${safe}%"></div></div>
    <span class="bar-value">${value === null || value === undefined ? '—' : `${Math.round(value)}%`}</span>
  </div>`;
}

export function throttleChips(summary) {
  const throttled = summary?.throttled || {};
  if (!throttled.supported) return '<span class="chip">throttle n/a</span>';
  if (throttled.now?.length) {
    return throttled.now.map((flag) => `<span class="chip bad">${esc(flag)}</span>`).join('');
  }
  if (throttled.since_boot?.length) {
    return `<span class="chip warn">throttle event since boot</span>`;
  }
  return '<span class="chip good">no throttling</span>';
}

export function tailscaleChip(summary) {
  const tailscale = summary?.tailscale || {};
  if (!tailscale.installed) return '';
  return `<span class="chip info" title="Tailscale ${esc(tailscale.version || '')}">${icon('network', 11)} ${esc(tailscale.ipv4 || tailscale.hostname || 'tailnet')}</span>`;
}

export function safetyBadge(level) {
  return ui.safetyBadge(level);
}

export function containerStatePill(container) {
  if (container.state !== 'running') return ui.pill('down', container.state || 'stopped');
  if (container.health === 'unhealthy') return ui.pill('unhealthy');
  if (container.health === 'starting') return ui.pill('starting');
  if (container.health === 'healthy') return ui.pill('ok', 'healthy');
  return ui.pill('ok', 'running');
}

export function relTime(ts) {
  return ts ? ui.ago(ts) : '—';
}

export function copyHostButton(host) {
  return `<button class="btn ghost sm" data-copy-host="${esc(host)}">${icon('copy', 12)} Copy ssh</button>`;
}


/**
 * What to do when Gitea rejects the credentials depends on who owns the token.
 * In the app the shell owns it, so pointing at Settings → Repos sends the
 * user to a page that cannot help; deep-link straight to the app's window.
 */
export function giteaAuthBanner(ctx, count) {
  if (!count) return '';
  const title = `${ui.plural(count, 'project')} could not authenticate to Gitea.`;
  if (ctx.bootstrap?.settings?.app_managed) {
    return `<div class="banner bad">${icon('key')}<span><strong>${title}</strong> No Gitea token is stored. Open <strong>Homelab Console → Settings… (⌘,)</strong> from the menu bar and paste one. Saving restarts the console and checks Gitea automatically.</span><a class="btn sm ghost" href="homelab-console://settings">Open app Settings…</a></div>`;
  }
  return `<div class="banner bad">${icon('key')}<span><strong>${title}</strong> Add a token in Settings → Repos, then run Check Gitea again.</span><a class="btn sm ghost" href="#/settings">Settings</a></div>`;
}
