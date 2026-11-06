import { api, ApiError } from './api.js';
import * as ui from './ui.js';
import fleetView from './views/fleet.js';
import reposView from './views/repos.js';
import repoStreetView from './views/repo-street.js';
import hostView from './views/host.js';
import servicesView from './views/services.js';
import stackView from './views/stack.js';
import updatesView from './views/updates.js';
import hostsView from './views/hosts.js';
import activityView from './views/activity.js';
import settingsView from './views/settings.js';
import { showCheckUpdates, showCommit } from './flows.js';

const WORKSPACES = [
  { id: 'pi', label: 'Pi fleet', href: '#/fleet', icon: 'fleet' },
  { id: 'repos', label: 'Repo street', href: '#/repo-street', icon: 'code' },
];

const PI_NAV = [
  { id: 'fleet', label: 'Fleet', href: '#/fleet', icon: 'fleet' },
  { id: 'services', label: 'Stacks', href: '#/services', icon: 'services' },
  { id: 'updates', label: 'Updates', href: '#/updates', icon: 'updates' },
  { id: 'hosts', label: 'Hosts', href: '#/hosts', icon: 'hosts' },
];

const REPO_NAV = [
  { id: 'repo-street', label: 'Overview', short: 'Street', href: '#/repo-street', icon: 'layers' },
  { id: 'repos', label: 'Repositories', short: 'Repos', href: '#/repos', icon: 'git' },
];

const CONSOLE_NAV = [
  { id: 'activity', label: 'Activity', href: '#/activity', icon: 'activity' },
  { id: 'settings', label: 'Settings', href: '#/settings', icon: 'settings' },
];

const ALL_NAV = [...PI_NAV, ...REPO_NAV, ...CONSOLE_NAV];

const CRUMBS = {
  fleet: 'Pi fleet / Fleet',
  host: 'Pi fleet / Host',
  services: 'Pi fleet / Stacks',
  stack: 'Pi fleet / Stack',
  updates: 'Pi fleet / Updates',
  hosts: 'Pi fleet / Hosts',
  'repo-street': 'Repo street / Overview',
  repos: 'Repo street / Repositories',
  activity: 'Console / Activity',
  settings: 'Console / Settings',
};

const THEME_MODES = ['dark', 'light', 'system'];
const THEME_LABELS = { dark: 'Dark', light: 'Light', system: 'System' };
const THEME_ICONS = { dark: 'moon', light: 'sun', system: 'monitor' };
const themeMedia = window.matchMedia('(prefers-color-scheme: light)');

/**
 * Keep the topbar crumb useful on detail pages: "Pi fleet / Host" alone does
 * not say which host. Fall back to the static map for top-level views.
 */
function crumbLabel(route) {
  if (route.name === 'host' && route.params[0]) return `Pi fleet / ${route.params[0]}`;
  if (route.name === 'stack' && route.params[0] && route.params[1]) {
    return `Pi fleet / ${route.params[0]} / ${route.params[1]}`;
  }
  return CRUMBS[route.name] || 'Pi fleet / Fleet';
}

function pageTitle(route) {
  if (route.name === 'host' && route.params[0]) return `${route.params[0]} · Host · Homelab Console`;
  if (route.name === 'stack' && route.params[0] && route.params[1]) {
    return `${route.params[1]} · ${route.params[0]} · Homelab Console`;
  }
  return `${CRUMBS[route.name] || 'Pi fleet / Fleet'} · Homelab Console`;
}

function resolveTheme(mode) {
  return mode === 'system' ? (themeMedia.matches ? 'light' : 'dark') : (mode === 'light' ? 'light' : 'dark');
}

function updateThemeButton() {
  const button = document.querySelector('[data-action="cycle-theme"]');
  if (!button) return;
  const mode = ctx.themeMode || 'dark';
  button.innerHTML = `<span class="theme-icon">${ui.icon(THEME_ICONS[mode] || 'moon', 13)}</span><span class="theme-label">${THEME_LABELS[mode] || 'Dark'}</span>`;
  button.title = `Theme: ${THEME_LABELS[mode] || 'Dark'} — click to change`;
  button.setAttribute('aria-label', `Theme: ${THEME_LABELS[mode] || 'Dark'}. Click to change.`);
}

const ctx = {
  themeMode: document.documentElement.dataset.themeMode || (window.__hlcTheme?.mode?.() || 'dark'),
  themeMedia,
  api,
  ui,
  state: null,
  bootstrap: null,
  route: null,
  workspace: 'pi',
  uiState: {},
  viewEpoch: 0,
  routeKey: '',
  started: Date.now(),
  navigate(href) {
    if (location.hash === href) { render(); return; }
    location.hash = href;
  },
  async refreshState({ force = false, render = true } = {}) {
    try {
      const state = await api.get('/api/state');
      ctx.state = state;
      updateChrome();
      if (render && !ui.modalOpen() && !isEditing() && !ctx.suppressRender) {
        renderCurrent();
      }
      return state;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return null;
      if (force) ui.toast('Could not refresh fleet state', 'err', ui.errorText(error));
      return null;
    }
  },
  async reloadBootstrap() {
    try {
      const bootstrap = await api.get('/api/bootstrap');
      ctx.bootstrap = bootstrap;
      ctx.state = bootstrap.state;
      if (bootstrap.settings?.theme) ctx.applyTheme(bootstrap.settings.theme);
      updateChrome();
      return bootstrap;
    } catch (error) {
      ui.toast('Could not reload console data', 'err', ui.errorText(error));
      return null;
    }
  },
  runModal: (runId, title) => ui.runModal(ctx, runId, title),
  applyTheme(mode, { persist = false } = {}) {
    const next = THEME_MODES.includes(mode) ? mode : 'dark';
    const resolved = resolveTheme(next);
    ctx.themeMode = next;
    window.__hlcTheme?.apply?.(next);
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.themeMode = next;
    try { localStorage.setItem('hlc-theme', next); } catch { /* private browsing */ }
    document.getElementById('theme-color')?.setAttribute('content', resolved === 'light' ? '#eef3fa' : '#06080c');
    if (ctx.bootstrap?.settings) ctx.bootstrap.settings.theme = next;
    document.querySelectorAll('[data-theme-choice]').forEach((node) => {
      const active = node.dataset.themeChoice === next;
      node.classList.toggle('active', active);
      node.setAttribute('aria-pressed', String(active));
    });
    updateThemeButton();
    if (persist) {
      api.post('/api/settings', { theme: next }).catch((error) => {
        ui.toast('Theme not saved to the console', 'warn', ui.errorText(error));
      });
    }
    return resolved;
  },
  async setTheme(mode) {
    const next = THEME_MODES.includes(mode) ? mode : 'dark';
    ctx.applyTheme(next, { persist: true });
    return next;
  },
  cycleTheme() {
    const index = THEME_MODES.indexOf(ctx.themeMode || 'dark');
    const next = THEME_MODES[(index + 1) % THEME_MODES.length];
    ctx.setTheme(next);
    ui.toast(`Theme: ${THEME_LABELS[next]}`, 'ok', next === 'system' ? 'Following the macOS appearance.' : '');
  },
  checkUpdates: () => showCheckUpdates(ctx),
  newStack: () => showNewStack(ctx),
  rerender: () => renderCurrent(),
  showError: (error, fallback = 'Something went wrong') => {
    ui.toast(fallback, 'err', ui.errorText(error));
  },
};

window.hlc = ctx;

// The dirty-state banner (and the contextual Repository button) open the
// commit dialog without a detour through Settings.
document.addEventListener('click', (event) => {
  const trigger = event.target.closest('[data-open-commit]');
  if (!trigger) return;
  event.preventDefault();
  showCommit(ctx);
});

function isEditing() {
  const element = document.activeElement;
  if (!element || element.closest('.topbar')) return false;
  const tag = element.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || element.isContentEditable) return true;
  // Keep local controls (filters, tabs, policy pickers) focused across background
  // polls; replacing the view would otherwise drop keyboard focus mid-use.
  return Boolean(element.closest('#view') && element.matches(
    '[data-tab], [data-activity-tab], [data-host], [data-theme-choice], [data-policy], [data-settings-jump], [data-log-tail] button',
  ));
}

function parseRoute() {
  const hash = location.hash || '#/fleet';
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  if (!parts.length) return { name: 'fleet', params: [] };
  const name = parts[0];
  if (name === 'host' && parts[1]) return { name: 'host', params: [decodeURIComponent(parts[1])] };
  if (name === 'stack' && parts[1] && parts[2]) {
    return { name: 'stack', params: [decodeURIComponent(parts[1]), decodeURIComponent(parts[2])] };
  }
  if (['repo-street', 'repos', 'services', 'updates', 'hosts', 'activity', 'settings'].includes(name)) return { name, params: [] };
  return { name: 'fleet', params: [] };
}

function workspaceForRoute(name) {
  if (name === 'repos' || name === 'repo-street') return 'repos';
  if (['fleet', 'host', 'services', 'stack', 'updates', 'hosts'].includes(name)) return 'pi';
  return null;
}

function navIdForRoute(route) {
  if (route.name === 'host') return 'fleet';
  if (route.name === 'stack') return 'services';
  return route.name;
}

function navLink(item, { compact = false } = {}) {
  const text = compact && item.short ? item.short : item.label;
  return `<a class="nav-item" data-nav="${item.id}" href="${item.href}" aria-label="${item.label}" title="${item.label}">
    <span class="nav-icon">${ui.icon(item.icon, 17)}</span>
    <span class="nav-text">${text}</span>
    <span class="nav-count hidden"></span>
  </a>`;
}

function navCounts() {
  const summary = ctx.state?.summary || {};
  const repoSummary = ctx.state?.repos?.summary || {};
  return {
    fleet: summary.hosts_total ? `${summary.hosts_online}/${summary.hosts_total}` : '',
    repos: repoSummary.attention || '',
    'repo-street': repoSummary.attention || '',
    services: summary.stacks_total ?? '',
    updates: summary.updates_available || '',
    hosts: summary.hosts_total ?? '',
    activity: '',
    settings: '',
  };
}

function updateChrome() {
  const state = ctx.state;
  if (!state) return;
  const route = ctx.route || parseRoute();
  const workspace = workspaceForRoute(route.name);
  if (workspace) ctx.workspace = workspace;
  const ct = ctx.workspace || 'pi';
  const repoSummary = state.repos?.summary || {};
  document.querySelectorAll('[data-workspace]').forEach((node) => {
    const active = node.dataset.workspace === ct;
    node.classList.toggle('active', active);
    if (active) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  });
  document.querySelectorAll('[data-workspace-nav]').forEach((node) => {
    node.classList.toggle('hidden', node.dataset.workspaceNav !== ct);
  });

  // Releases are a Pi-fleet concern; on Repo street the same topbar action was
  // a dead end. Swap in the right label so the controls match the workspace.
  const inRepoStreet = ['repos', 'repo-street'].includes(route.name);
  const releaseButton = document.querySelector('[data-action="check-updates"]');
  if (releaseButton) releaseButton.classList.toggle('hidden', inRepoStreet);
  const refreshButton = document.querySelector('[data-action="refresh"]');
  if (refreshButton && !refreshButton.disabled) {
    const refreshLabel = inRepoStreet ? 'Refresh repos' : 'Refresh';
    const labelNode = refreshButton.querySelector('.btn-label');
    if (labelNode) labelNode.textContent = refreshLabel;
    refreshButton.title = inRepoStreet ? 'Refresh repositories' : 'Refresh fleet data';
    refreshButton.setAttribute('aria-label', refreshButton.title);
  }
  const piCount = document.querySelector('[data-workspace="pi"] .workspace-count');
  if (piCount) {
    // The Fleet view already shows online/total hosts; use this badge for the
    // actionable number instead, matching Repo street's attention count.
    const updates = Number(state.summary?.updates_available) || 0;
    piCount.textContent = updates ? String(updates) : '';
    piCount.classList.toggle('hidden', !updates);
    piCount.classList.toggle('alert', updates > 0);
    piCount.classList.remove('bad');
    const item = piCount.closest('[data-workspace="pi"]');
    if (item) {
      const label = updates ? `Pi fleet — ${ui.plural(updates, 'update')} available` : 'Pi fleet';
      item.title = label;
      item.setAttribute('aria-label', label);
    }
  }
  const repoCount = document.querySelector('[data-workspace="repos"] .workspace-count');
  if (repoCount) {
    const value = repoSummary.attention || '';
    repoCount.textContent = value && value !== '0' ? String(value) : '';
    repoCount.classList.toggle('hidden', !value || value === '0');
    repoCount.classList.toggle('alert', Number(value) > 0);
  }

  const counts = navCounts();
  const activeId = navIdForRoute(route);
  for (const item of ALL_NAV) {
    const nodes = document.querySelectorAll(`[data-nav="${item.id}"]`);
    const active = item.id === activeId;
    for (const node of nodes) {
      node.classList.toggle('active', active);
      if (active) node.setAttribute('aria-current', 'page');
      else node.removeAttribute('aria-current');
      const countNode = node.querySelector('.nav-count');
      if (!countNode) continue;
      const value = counts[item.id];
      countNode.textContent = value && value !== '0' ? String(value) : '';
      countNode.classList.toggle('hidden', !value || value === '0');
      countNode.classList.toggle('alert', (item.id === 'updates' && Number(counts.updates) > 0) || ((item.id === 'repos' || item.id === 'repo-street') && Number(value) > 0));
      countNode.classList.toggle('bad', item.id === 'fleet' && Number(state.summary?.hosts_online) < Number(state.summary?.hosts_total));
    }
  }

  const age = state.age_s ?? 0;
  const updated = document.getElementById('updated-at');
  if (updated) {
    updated.textContent = age < 10 ? 'updated just now' : `updated ${ui.ago(state.generated_at)}`;
    if (state.generated_at) updated.title = new Date(Number(state.generated_at) * 1000).toLocaleString();
  }
  const dot = document.getElementById('live-dot');
  if (dot) {
    dot.classList.toggle('stale', age > 90);
    dot.classList.toggle('dead', Boolean(state.warming_up));
    dot.title = state.warming_up ? 'collecting the first snapshot' : age > 90 ? 'snapshot is stale' : 'live telemetry';
  }
  const crumb = document.getElementById('crumb');
  if (crumb) crumb.textContent = crumbLabel(route);
}

function renderShell() {
  const app = document.getElementById('app');
  app.innerHTML = `<a class="skip-link" href="#view" data-skip-link>Skip to content</a>
    <div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="#/fleet" aria-label="Homelab Console home">
        <span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 100 100"><rect x="18" y="6" width="21" height="21" rx="6" fill="#FFFFFF"/><rect x="18" y="71" width="21" height="21" rx="6" fill="#FFFFFF"/><rect x="59" y="35" width="21" height="21" rx="6" fill="#FFFFFF"/><rect x="59" y="71" width="21" height="21" rx="6" fill="#5B8DFF"/><rect x="24" y="27" width="9" height="44" fill="#FFFFFF"/><rect x="24" y="41" width="35" height="9" fill="#FFFFFF"/><rect x="65" y="56" width="9" height="15" fill="#FFFFFF"/></svg></span>
        <span class="brand-text"><strong>Homelab</strong><span>console</span></span>
      </a>
      <nav class="workspace-switch" aria-label="Workspaces">
        ${WORKSPACES.map((item) => `<a class="workspace-item" data-workspace="${item.id}" href="${item.href}" aria-label="${item.label}" title="${item.label}">
          <span class="nav-icon">${ui.icon(item.icon, 16)}</span>
          <span class="workspace-text">${item.label}</span>
          <span class="workspace-count hidden"></span>
        </a>`).join('')}
      </nav>
      <nav class="nav nav-compact" aria-label="All views">
        ${PI_NAV.map((item) => navLink(item, { compact: true })).join('')}
        <div class="nav-sep" role="separator"></div>
        ${REPO_NAV.map((item) => navLink(item, { compact: true })).join('')}
      </nav>
      <nav class="nav nav-workspace" data-workspace-nav="pi" aria-label="Pi fleet views">
        <div class="nav-label">Views</div>
        ${PI_NAV.map((item) => navLink(item)).join('')}
      </nav>
      <nav class="nav nav-workspace hidden" data-workspace-nav="repos" aria-label="Repo street views">
        <div class="nav-label">Views</div>
        ${REPO_NAV.map((item) => navLink(item)).join('')}
      </nav>
      <div class="sidebar-bottom">
        <nav class="nav nav-utility" aria-label="Console">
          <div class="nav-label">Console</div>
          ${CONSOLE_NAV.map((item) => navLink(item)).join('')}
        </nav>
        <a class="sidebar-author" href="https://wychoi.com" target="_blank" rel="noreferrer noopener" title="made by wychoi" aria-label="made by wychoi">
          <svg class="author-abacus" viewBox="106 192 812 640" aria-hidden="true"><rect x="150" y="236" width="724" height="552" rx="96" fill="none" stroke="currentColor" stroke-width="72"/><line x1="214" y1="400" x2="810" y2="400" stroke="currentColor" stroke-width="22" opacity=".55"/><line x1="214" y1="512" x2="810" y2="512" stroke="currentColor" stroke-width="22" opacity=".55"/><line x1="214" y1="624" x2="810" y2="624" stroke="currentColor" stroke-width="22" opacity=".55"/><circle cx="318" cy="400" r="86" fill="currentColor"/><circle cx="512" cy="400" r="86" fill="#5B8DFF"/><circle cx="706" cy="400" r="86" fill="currentColor"/><circle cx="414" cy="624" r="86" fill="currentColor"/><circle cx="608" cy="624" r="86" fill="currentColor"/></svg>
          <span class="author-text">made by <strong>wychoi</strong></span>
        </a>
      </div>
    </aside>
    <div class="main">
      <header class="topbar">
        <div class="crumb"><span class="crumb-root">homelab</span><span class="sep">/</span><strong id="crumb">Pi fleet / Fleet</strong></div>
        <div class="topbar-actions">
          <span class="updated"><span class="live-dot" id="live-dot" aria-hidden="true"></span><span id="updated-at">—</span></span>
          <button class="btn ghost sm theme-btn" type="button" data-action="cycle-theme" title="Change theme"></button>
          <button class="btn ghost sm" type="button" data-action="check-updates" aria-label="Check upstream releases" title="Check upstream releases">${ui.icon('updates', 12)}<span class="btn-label">Check releases</span></button>
          <button class="btn primary sm" type="button" data-action="refresh" aria-label="Refresh fleet data" title="Refresh fleet data">${ui.icon('refresh', 12)}<span class="btn-label">Refresh</span></button>
        </div>
      </header>
      <main id="view" class="content" tabindex="-1"></main>
    </div>
  </div>`;
  document.querySelector('[data-action="refresh"]').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    if (button.disabled) return;
    const original = button.innerHTML;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.innerHTML = `<span class="spinner sm"></span><span class="btn-label">Refreshing…</span>`;
    try {
      await api.post('/api/refresh');
      const inRepos = ['repos', 'repo-street'].includes(ctx.route?.name);
      if (inRepos) await api.post('/api/repos/refresh');
      ui.toast(inRepos ? 'Refreshing repositories' : 'Refreshing fleet', 'ok');
      setTimeout(() => ctx.refreshState({ force: true }), 900);
    } catch (error) {
      ui.toast('Could not refresh', 'err', ui.errorText(error));
    } finally {
      setTimeout(() => {
        button.disabled = false;
        button.removeAttribute('aria-busy');
        button.innerHTML = original;
      }, 900);
    }
  });
  document.querySelector('[data-action="check-updates"]').addEventListener('click', () => ctx.checkUpdates());
  document.querySelector('[data-action="cycle-theme"]').addEventListener('click', () => ctx.cycleTheme());
  const topbar = document.querySelector('.topbar');
  const syncTopbarHeight = () => {
    if (!topbar) return;
    document.documentElement.style.setProperty('--topbar-h', `${topbar.offsetHeight || 58}px`);
  };
  syncTopbarHeight();
  if ('ResizeObserver' in window) new ResizeObserver(syncTopbarHeight).observe(topbar);
  else window.addEventListener('resize', syncTopbarHeight);
  updateThemeButton();
  updateChrome();
}

function renderCurrent() {
  const route = ctx.route || parseRoute();
  ctx.route = route;
  const view = {
    fleet: fleetView,
    'repo-street': repoStreetView,
    repos: reposView,
    host: hostView,
    services: servicesView,
    stack: stackView,
    updates: updatesView,
    hosts: hostsView,
    activity: activityView,
    settings: settingsView,
  }[route.name] || fleetView;
  const container = document.getElementById('view');
  if (!container) return;
  document.title = pageTitle(route);
  let html;
  try {
    html = view.render(ctx, route.params);
  } catch (error) {
    console.error(error);
    html = `<div class="empty"><strong>This view failed to render</strong><p>${ui.esc(ui.errorText(error))}</p></div>`;
    ui.toast('View render failed', 'err', ui.errorText(error));
  }
  // If nothing changed, keep the existing DOM and its listeners. This avoids
  // pointless replacement on poll ticks (especially on static views).
  if (container.innerHTML === html) { updateChrome(); return; }
  container.innerHTML = html;
  if (typeof view.mount === 'function') {
    try { view.mount(container, ctx, route.params); } catch (error) { console.error(error); }
  }
  updateChrome();
}

function render() {
  if (!ctx.state) return;
  ctx.route = parseRoute();
  if (!ctx.bootstrap) return;
  const routeKey = JSON.stringify(ctx.route);
  if (routeKey !== ctx.routeKey) {
    ctx.routeKey = routeKey;
    ctx.viewEpoch += 1;
  }
  renderCurrent();
  window.scrollTo({ top: 0, behavior: 'auto' });
}

window.addEventListener('hashchange', () => {
  ctx.route = parseRoute();
  if (!document.getElementById('view')) return;
  render();
  // Refresh the counters in the background, but do not replace the view that
  // was just drawn. Replacing it here caused a visible double-render flicker
  // on every tab change; the 12s poll handles the next visual update.
  ctx.refreshState({ render: false });
});

window.addEventListener('hlc:unauthorized', () => {
  ui.toast('Session expired', 'err', 'Reload the page to reconnect to the console.');
});

document.addEventListener('click', (event) => {
  const skip = event.target.closest('[data-skip-link]');
  if (!skip) return;
  event.preventDefault();
  const view = document.getElementById('view');
  view?.focus();
  view?.scrollIntoView({ block: 'start' });
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') ctx.refreshState();
});

const WARMUP_POLL_MS = 500;
const WARMUP_MAX_MS = 25000;

function warmupSplash(warmup) {
  const tasks = warmup?.tasks || [];
  return `<div class="boot boot-warm" role="status" aria-live="polite" aria-busy="true">
    <div class="boot-mark" aria-hidden="true"></div>
    <div class="boot-panel">
      <strong>Homelab Console</strong>
      <span>warming up the control plane…</span>
      <ul class="boot-tasks">
        ${tasks
          .map(
            (task) => `<li class="boot-task ${ui.esc(task.state)}">
          <span class="boot-dot" aria-hidden="true"></span>
          <span class="boot-task-label">${ui.esc(task.label)}</span>
          <span class="boot-task-detail">${ui.esc(task.detail || '')}</span>
        </li>`,
          )
          .join('')}
      </ul>
      <span class="boot-foot">Startup checks run once. Use Refresh any time.</span>
    </div>
  </div>`;
}

// First paint is a real screen, not six empty tiles filling in one by one.
// The server caps the wait (warmup.deadline_s); this is only the client-side
// backstop for a server that stops answering mid-wait.
async function waitForWarmup() {
  let warmup = ctx.bootstrap?.state?.warmup || null;
  if (warmup?.state !== 'warming') return;
  const deadline = Date.now() + WARMUP_MAX_MS;
  const host = document.getElementById('app');
  let painted = '';
  while (Date.now() < deadline) {
    // Repaint only when a row actually changes, so the live region does not
    // re-announce the whole list twice a second.
    const signature = JSON.stringify(warmup?.tasks || []);
    if (signature !== painted) {
      host.innerHTML = warmupSplash(warmup);
      painted = signature;
    }
    await new Promise((resolve) => setTimeout(resolve, WARMUP_POLL_MS));
    const state = await ctx.refreshState({ render: false });
    if (!state) return;
    warmup = state.warmup || warmup;
    if (warmup?.state !== 'warming') return;
  }
}

async function boot() {
  try {
    ctx.bootstrap = await api.get('/api/bootstrap');
    ctx.state = ctx.bootstrap.state;
  } catch (error) {
    document.getElementById('app').innerHTML = `<div class="empty boot-error">
      <strong>Cannot reach the console server</strong>
      <p>${ui.esc(ui.errorText(error))}</p>
      <button class="btn primary" data-reload>Reload</button>
    </div>`;
    document.querySelector('[data-reload]')?.addEventListener('click', () => location.reload());
    return;
  }
  if (!location.hash) location.hash = '#/fleet';
  ctx.applyTheme(ctx.bootstrap.settings?.theme || ctx.themeMode, { persist: false });
  themeMedia.addEventListener('change', () => {
    if (ctx.themeMode === 'system') ctx.applyTheme('system');
  });
  ctx.route = parseRoute();
  await waitForWarmup();
  renderShell();
  render();
  // Poll loop: respect edits/modals so we never clobber a form or log view.
  const tick = async () => {
    if (document.visibilityState === 'visible' && !ui.modalOpen() && !isEditing()) {
      await ctx.refreshState();
    }
    setTimeout(tick, 12000);
  };
  setTimeout(tick, 4000);
}

boot();
