import * as ui from '../ui.js';
import { rootsFromSettings, rootsLabel } from '../repoRoots.js';
import { pageHead, statTile, emptyState, giteaAuthBanner } from './common.js';

const { esc, icon } = ui;

function reposState(ctx) {
  return ctx.state?.repos || null;
}

function allRepos(ctx) {
  return reposState(ctx)?.repos || [];
}

function healthTone(state) {
  if (state === 'up') return 'ok';
  if (state === 'auth') return 'warn';
  if (state === 'down' || state === 'error') return 'bad';
  if (state === 'checking') return 'info';
  return '';
}

function healthLabel(state, kind) {
  if (kind === 'VS Code') return state === 'up' ? 'available' : 'missing';
  if (kind === 'Repo queue') return state === 'unknown' ? 'idle' : state;
  if (state === 'up') return 'reachable';
  if (state === 'auth') return 'auth needed';
  if (state === 'down') return 'unreachable';
  if (state === 'error') return 'error';
  if (state === 'checking') return 'checking…';
  return 'not checked';
}

function healthCard({ title, iconName, entry, detail, chips = [], action = '', tone = '' }) {
  const state = entry?.state || 'unknown';
  const statusTone = tone || healthTone(state);
  const dot = statusTone === 'ok' ? 'ok' : statusTone === 'warn' ? 'warn' : statusTone === 'bad' ? 'bad' : '';
  return `<div class="card health-card">
    <div class="card-head">
      <div class="health-icon ${statusTone}">${icon(iconName, 16)}</div>
      <div><div class="card-title">${esc(title)}</div><div class="card-sub">${esc(detail || '')}</div></div>
      <div class="card-actions">${ui.pill(statusTone || 'neutral', healthLabel(state, title))}</div>
    </div>
    <div class="card-body health-body">
      <div class="health-state">
        <span class="status-dot ${dot}"></span>
        <span>${esc(entry?.detail || 'No probe has run yet.')}</span>
      </div>
      ${entry?.version ? `<div class="health-version mono tiny">Gitea v${esc(entry.version)}</div>` : ''}
      ${chips.length ? `<div class="health-foot">${chips.join('')}</div>` : ''}
      ${action}
    </div>
  </div>`;
}

function attentionItems(repos, connectivity) {
  const items = [];
  for (const repo of repos) {
    const name = repo.rel;
    if (repo.needs?.commit) {
      items.push({
        tone: 'warn',
        title: name,
        detail: `${repo.dirty_count} file${repo.dirty_count === 1 ? '' : 's'} to commit${repo.ignored_count ? ` · ${repo.ignored_count} ignored hidden` : ''}`,
        filter: 'commit',
      });
    }
    if (repo.needs?.push) {
      const ahead = repo.ahead === null || repo.ahead === undefined ? 'a new branch' : `${repo.ahead} commit${repo.ahead === 1 ? '' : 's'}`;
      items.push({ tone: 'warn', title: name, detail: `${ahead} ahead of GitHub — push to update`, filter: 'push' });
    }
    if (repo.behind > 0) {
      items.push({ tone: 'info', title: name, detail: `${repo.behind} commit${repo.behind === 1 ? '' : 's'} behind origin (last fetch)`, filter: 'behind' });
    }
    if (['behind', 'missing'].includes(repo.gitea?.status)) {
      items.push({
        tone: 'warn',
        title: name,
        detail: repo.gitea.status === 'missing' ? 'missing from Gitea — backup will create it' : (repo.gitea.detail || 'Gitea backup is behind'),
        filter: 'gitea',
      });
    }
    if (['auth', 'unreachable', 'error'].includes(repo.gitea?.status)) {
      items.push({
        tone: 'bad',
        title: name,
        detail: repo.gitea.status === 'auth' ? 'Gitea token needed' : `Gitea check failed: ${repo.gitea.detail || repo.gitea.status}`,
        filter: 'gitea',
      });
    }
  }
  const order = { bad: 0, warn: 1, info: 2 };
  return items.sort((a, b) => (order[a.tone] - order[b.tone]) || a.title.localeCompare(b.title));
}

function attentionHtml(items) {
  if (!items.length) {
    return `<div class="card"><div class="card-body attention-empty"><span class="status-dot ok"></span><span class="muted">Every repo is clean, pushed to GitHub and backed up.</span></div></div>`;
  }
  return `<div class="card"><ul class="attention-list">
    ${items.slice(0, 9).map((item) => `<li class="attention-item">
      <span class="status-dot ${item.tone === 'bad' ? 'bad' : item.tone === 'warn' ? 'warn' : ''}"></span>
      <div class="attention-text"><span class="attention-title">${esc(item.title)}</span><span class="attention-detail">${esc(item.detail)}</span></div>
      <button class="btn ghost sm" type="button" data-repo-review="${esc(item.filter)}" data-repo-name="${esc(item.title)}">Review</button>
    </li>`).join('')}
  </ul>${items.length > 9 ? `<div class="card-foot muted tiny">+${items.length - 9} more in Repositories</div>` : ''}</div>`;
}

function recentHtml(events) {
  const rows = events.filter((event) => event.kind === 'repo').slice(0, 8);
  if (!rows.length) {
    return `<div class="card"><div class="card-body attention-empty"><span class="status-dot"></span><span class="muted">No repo activity yet. Commits, pushes and Gitea syncs appear here.</span></div></div>`;
  }
  return `<div class="card"><ul class="repo-events">
    ${rows.map((event) => `<li class="repo-event">
      <span class="stat-icon ${event.status === 'warning' ? 'warn' : event.status === 'error' ? 'bad' : 'ok'}">${icon('git', 13)}</span>
      <div><div class="repo-event-message">${esc(event.message)}</div><div class="repo-event-time muted tiny">${esc(ui.ago(event.ts))} · ${esc(event.status || 'info')}</div></div>
    </li>`).join('')}
  </ul></div>`;
}

function queueHtml(queue) {
  if (!queue) {
    return healthCard({
      title: 'Repo queue',
      iconName: 'layers',
      entry: { state: 'unknown', detail: 'No queue has run in this session.' },
      detail: 'GitHub then Gitea',
      action: '<a class="btn ghost sm mt-2" href="#/repos">Open Repositories</a>',
    });
  }
  const tone = queue.status === 'done' ? 'ok' : queue.status === 'partial' ? 'warn' : queue.status === 'running' ? 'info' : 'neutral';
  return `<div class="card health-card">
    <div class="card-head">
      <div class="health-icon ${tone}">${icon('layers', 16)}</div>
      <div><div class="card-title">Repo queue</div><div class="card-sub">GitHub → Gitea</div></div>
      <div class="card-actions">${ui.pill(tone, queue.status)}</div>
    </div>
    <div class="card-body health-body">
      <div class="health-state"><span class="status-dot ${tone === 'bad' ? 'bad' : tone === 'warn' ? 'warn' : 'ok'}"></span><span>${queue.completed}/${queue.total} complete${queue.error ? ` · ${esc(queue.error)}` : ''}</span></div>
      <div class="queue-progress compact"><div class="bar-track"><div class="bar-fill ${tone}" style="width:${queue.total ? Math.round((queue.completed / queue.total) * 100) : 0}%"></div></div></div>
      <div class="health-foot">${queue.run_id ? `<button class="btn ghost sm" data-repo-queue-log="${esc(queue.run_id)}">${icon('terminal', 12)} View log</button>` : ''}<a class="btn ghost sm" href="#/repos">Repositories</a></div>
    </div>
  </div>`;
}

function contentHtml(ctx) {
  const snapshot = reposState(ctx);
  if (!snapshot) {
    return pageHead({ title: 'Repo street', subtitle: 'Loading repositories…' }) + emptyState('git', 'Loading repos', 'The console is scanning the local checkout root.');
  }
  const repos = allRepos(ctx);
  if (snapshot.root_configured === false) {
    return `${pageHead({ title: 'Repo street', subtitle: 'Choose a folder for your git checkouts' })}
      ${emptyState('git', 'No repo folder yet', 'Repo street scans a folder you choose; it is not enabled by default.', '<a class="btn primary mt-2" href="#/repos">Choose repo folder</a>')}`;
  }
  const summary = snapshot.summary || {};
  const connectivity = snapshot.connectivity || {};
  const gitea = snapshot.gitea || {};
  const giteaChecking = gitea.state === 'checking';
  const queue = snapshot.queue;
  const behind = repos.filter((repo) => repo.behind > 0).length;
  const authErrors = repos.filter((repo) => repo.gitea?.status === 'auth').length;
  const giteaBehind = repos.filter((repo) => ['behind', 'missing'].includes(repo.gitea?.status)).length;
  const items = attentionItems(repos, connectivity);
  const stale = !connectivity.checked_at || (Date.now() / 1000 - Number(connectivity.checked_at) > 300);
  const checking = connectivity.state === 'checking' || connectivity.github?.state === 'checking' || connectivity.gitea?.state === 'checking';
  const checkButton = `<button class="btn ghost sm" type="button" data-repo-check-connections ${checking ? 'disabled' : ''}>${checking ? '<span class="spinner sm"></span> Checking…' : `${icon('refresh', 12)} Check connections`}</button>`;
  return `${pageHead({
    title: 'Repo street',
    subtitle: `<strong>${summary.total || 0}</strong> repositories<span class="dot">·</span><strong>${summary.attention || 0}</strong> repos need attention<span class="dot">·</span><span class="mono" title="${esc((snapshot.roots || rootsFromSettings(ctx.bootstrap?.settings)).join('\n'))}">${esc(rootsLabel(snapshot.roots || rootsFromSettings(ctx.bootstrap?.settings)))}</span>`,
    actions: `${checkButton}<a class="btn primary sm" href="#/repos">${icon('git', 12)} Repositories</a>`,
    badges: `${behind ? `<span class="pill info" title="Repos behind upstream, based on the last fetch">${icon('download', 11)} ${behind} update${behind === 1 ? '' : 's'}</span>` : ''}${stale ? ui.pill('neutral', 'connections not checked') : ui.pill(checking ? 'info' : 'ok', checking ? 'checking connections' : `checked ${ui.ago(connectivity.checked_at)}`)}`,
  })}
  ${snapshot.root_error ? `<div class="banner bad">${icon('alert')}<span><strong>Repo root unavailable.</strong> ${esc(snapshot.root_error)}</span><a class="btn sm ghost" href="#/settings">Settings</a></div>` : ''}
  ${snapshot.gitea_config_error ? `<div class="banner bad">${icon('alert')}<span><strong>Gitea configuration is invalid.</strong> ${esc(snapshot.gitea_config_error)}</span><a class="btn sm ghost" href="#/settings">Settings</a></div>` : ''}
  ${queue && ['queued', 'running'].includes(queue.status) ? `<div class="banner">${icon('layers')}<span><strong>Queue running.</strong> ${queue.completed}/${queue.total} complete — GitHub then Gitea for each repo.</span>${queue.run_id ? `<button class="btn sm ghost" data-repo-queue-log="${esc(queue.run_id)}">${icon('terminal', 12)} View log</button>` : ''}</div>` : ''}
  ${giteaAuthBanner(ctx, authErrors)}
  <div class="grid cols-4 mb-2">
    ${statTile({ label: 'Repositories', value: String(summary.total || 0), sub: `${summary.github || 0} with GitHub · ${(summary.total || 0) - (summary.github || 0)} local-only`, iconName: 'git' })}
    ${statTile({ label: 'Files to commit', value: String(summary.dirty_files || 0), sub: summary.dirty_repos ? `${ui.plural(summary.dirty_repos, 'repo')} dirty${summary.ignored_files ? ` · ${summary.ignored_files} ignored hidden` : ''}` : 'all working trees clean', iconName: 'commit', toneName: summary.dirty_repos ? 'warn' : 'ok', valueTone: summary.dirty_repos ? 'text-warn' : '' })}
    ${statTile({ label: 'Commits to push', value: String(summary.ahead_commits || 0), sub: summary.ahead_repos ? `${ui.plural(summary.ahead_repos, 'repo')} ahead of GitHub` : 'GitHub is current', iconName: 'upload', toneName: summary.ahead_repos ? 'warn' : 'ok', valueTone: summary.ahead_repos ? 'text-warn' : '' })}
    ${statTile({ label: 'Gitea backup', value: gitea.checked_at ? String(summary.gitea_behind || 0) : '—', sub: gitea.checked_at ? `${ui.plural(summary.gitea_checked || 0, 'repo')} checked · ${ui.ago(gitea.checked_at)}` : 'not checked yet', iconName: 'shield', toneName: summary.gitea_behind ? 'warn' : gitea.checked_at ? 'ok' : '', valueTone: summary.gitea_behind ? 'text-warn' : '' })}
  </div>
  <div class="grid cols-4 health-grid">
    ${healthCard({
      title: 'GitHub',
      iconName: 'cloud',
      entry: connectivity.github,
      detail: 'github.com reachability',
      chips: [behind ? `<button class="chip chip-action info" type="button" data-repo-review="behind">${icon('download', 11)} ${behind} behind origin</button>` : '<span class="chip good">no incoming updates</span>'],
    })}
    ${healthCard({
      title: 'Gitea',
      iconName: 'git',
      entry: connectivity.gitea,
      detail: 'backup server',
      chips: [giteaBehind ? `<button class="chip chip-action warn" type="button" data-repo-review="gitea">${icon('git', 11)} ${giteaBehind} backup${giteaBehind === 1 ? '' : 's'} behind</button>` : gitea.checked_at ? '<span class="chip good">backups current</span>' : '<span class="chip neutral">refs not checked</span>'],
      action: `<button class="btn ghost sm mt-2" type="button" data-repo-check-gitea ${giteaChecking || snapshot.gitea_configured === false ? 'disabled' : ''} title="${snapshot.gitea_configured === false ? 'Set a Gitea URL and owner in Settings first' : 'Compare local refs with the backup'}">${giteaChecking ? '<span class="spinner sm"></span> Checking…' : `${icon('git', 12)} Check backup refs`}</button>`,
    })}
    ${healthCard({
      title: 'VS Code',
      iconName: 'code',
      entry: { state: snapshot.vscode?.available ? 'up' : 'down', detail: snapshot.vscode?.available ? 'code command detected — opens a new window per repo' : 'code command not found; install the Shell Command or keep Visual Studio Code in /Applications' },
      detail: 'local editor',
      chips: ['<a class="chip" href="#/repos">Open repositories</a>'],
    })}
    ${queueHtml(queue)}
  </div>
  <div class="grid cols-2 section">
    <div>
      <div class="section-head"><div class="section-title">Needs attention <span class="count">${items.length}</span></div><div class="section-sub">commit, push, pull and backup work</div></div>
      ${attentionHtml(items)}
    </div>
    <div>
      <div class="section-head"><div class="section-title">Recent repo activity</div><div class="section-sub">commits, pushes and Gitea syncs</div></div>
      ${recentHtml(ctx.state?.activity || [])}
    </div>
  </div>
  <div class="hint mt-2">Connectivity checks are a short HTTPS probe to github.com and the Gitea version endpoint; they run when you open this overview, press Check connections, or finish a GitHub push or Gitea sync. Gitea ref comparison runs at startup, after a repo commit/push, and on demand; mirroring stays explicit.</div>`;
}

export default {
  render(ctx) {
    return contentHtml(ctx);
  },
  mount(root, ctx) {
    const snapshot = reposState(ctx);
    if (!snapshot) return;
    if (snapshot.root_configured === false) return;
    const connectivity = snapshot.connectivity || {};
    const stale = !connectivity.checked_at || (Date.now() / 1000 - Number(connectivity.checked_at) > 300);
    const checking = connectivity.state === 'checking';
    const lastRequest = Number(ctx.uiState.repoConnRequested || 0);
    if (stale && !checking && Date.now() - lastRequest > 300000) {
      ctx.uiState.repoConnRequested = Date.now();
      ctx.api.post('/api/repos/connectivity/check').then(() => {
        setTimeout(() => ctx.refreshState({ force: true }), 1200);
        setTimeout(() => ctx.refreshState({ force: true }), 4000);
      }).catch(() => {});
    }

    const focusRepos = (filter, name = '') => {
      if (!ctx.uiState.repos) ctx.uiState.repos = { filter: 'all', q: '', selection: null };
      ctx.uiState.repos.filter = filter || 'all';
      ctx.uiState.repos.q = name || '';
      ctx.navigate('#/repos');
    };

    root.querySelectorAll('[data-repo-review]').forEach((button) => {
      button.addEventListener('click', () => focusRepos(button.dataset.repoReview, button.dataset.repoName || ''));
    });
    root.querySelectorAll('[data-repo-check-connections]').forEach((button) => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        button.innerHTML = '<span class="spinner sm"></span> Checking…';
        ctx.uiState.repoConnRequested = Date.now();
        try {
          await ctx.api.post('/api/repos/connectivity/check');
          ui.toast('Checking GitHub and Gitea', 'ok', 'Short HTTPS reachability probes; no repo writes.');
          setTimeout(() => ctx.refreshState({ force: true }), 1200);
          setTimeout(() => ctx.refreshState({ force: true }), 4000);
        } catch (error) {
          button.disabled = false;
          button.innerHTML = `${icon('refresh', 12)} Check connections`;
          ui.toast('Could not start the connection check', 'err', ui.errorText(error));
        }
      });
    });
    root.querySelectorAll('[data-repo-check-gitea]').forEach((button) => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        button.innerHTML = '<span class="spinner sm"></span> Checking…';
        try {
          await ctx.api.post('/api/repos/gitea/check');
          ui.toast('Checking Gitea refs', 'ok', 'Backup comparison is read-only.');
          setTimeout(() => ctx.refreshState({ force: true }), 1200);
          setTimeout(() => ctx.refreshState({ force: true }), 4000);
        } catch (error) {
          button.disabled = false;
          button.innerHTML = `${icon('git', 12)} Check backup refs`;
          ui.toast('Could not start the Gitea check', 'err', ui.errorText(error));
        }
      });
    });
    root.querySelectorAll('[data-repo-queue-log]').forEach((button) => {
      button.addEventListener('click', () => ui.runModal(ctx, button.dataset.repoQueueLog, 'repo fleet: GitHub + Gitea'));
    });
  },
};
