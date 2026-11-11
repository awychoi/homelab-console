import * as ui from '../ui.js';
import { rootsFromSettings, rootsLabel } from '../repoRoots.js';
import {
  activityBars, activitySummaryHtml, allRepos, backupSummary, bindRepoFocus, commitListHtml,
  focusRepos, formatAgeDays, medianCommitAge, quietReposHtml, reposSnapshot,
} from '../repoMetrics.js';
import { pageHead, statTile, emptyState, giteaAuthBanner } from './common.js';

const { esc, icon } = ui;

function reposState(ctx) {
  return reposSnapshot(ctx);
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
  if (kind === 'Sync queue') return state === 'unknown' ? 'idle' : state;
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

function attentionItems(repos) {
  const items = [];
  for (const repo of repos) {
    const name = repo.rel;
    if (repo.error) {
      items.push({ tone: 'bad', title: name, detail: `git error: ${repo.error}`, filter: 'all' });
      continue;
    }
    if (repo.conflicts) {
      items.push({
        tone: 'bad',
        title: name,
        detail: `${repo.conflicts} unresolved merge conflict${repo.conflicts === 1 ? '' : 's'} in the working tree`,
        filter: 'commit',
      });
    }
    if (repo.needs?.commit) {
      const entries = (repo.entries || []).filter((entry) => !entry.ignored);
      const staged = entries.filter((entry) => entry.staged).length;
      const unstaged = entries.filter((entry) => entry.unstaged && !entry.untracked).length;
      const untracked = entries.filter((entry) => entry.untracked).length;
      const composition = [
        staged ? `${staged} staged` : '',
        unstaged ? `${unstaged} unstaged` : '',
        untracked ? `${untracked} new` : '',
        repo.ignored_count ? `${repo.ignored_count} ignored hidden` : '',
      ].filter(Boolean).join(' · ');
      const touched = repo.last_touch_at ? ` · last touched ${ui.ago(repo.last_touch_at)}` : '';
      items.push({
        tone: 'warn',
        title: name,
        detail: `${repo.dirty_count} file${repo.dirty_count === 1 ? '' : 's'} to commit${composition ? ` · ${composition}` : ''}${touched}`,
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
    return `<div class="card"><div class="card-body attention-empty"><span class="status-dot ok"></span><span class="muted">Every project is clean, pushed to GitHub and backed up.</span></div></div>`;
  }
  return `<div class="card"><ul class="attention-list">
    ${items.slice(0, 9).map((item) => `<li class="attention-item">
      <span class="status-dot ${item.tone === 'bad' ? 'bad' : item.tone === 'warn' ? 'warn' : ''}"></span>
      <div class="attention-text"><span class="attention-title">${esc(item.title)}</span><span class="attention-detail">${esc(item.detail)}</span></div>
      <button class="btn ghost sm" type="button" data-repo-review="${esc(item.filter)}" data-repo-name="${esc(item.title)}">Review</button>
    </li>`).join('')}
  </ul>${items.length > 9 ? `<div class="card-foot muted tiny">+${items.length - 9} more in Projects</div>` : ''}</div>`;
}

function queueHtml(queue) {
  if (!queue) {
    return healthCard({
      title: 'Sync queue',
      iconName: 'layers',
      entry: { state: 'unknown', detail: 'No queue has run in this session.' },
      detail: 'GitHub then Gitea',
      action: '<a class="btn ghost sm mt-2" href="#/repos">Open Projects</a>',
    });
  }
  const tone = queue.status === 'done' ? 'ok' : queue.status === 'partial' ? 'warn' : queue.status === 'running' ? 'info' : 'neutral';
  return `<div class="card health-card">
    <div class="card-head">
      <div class="health-icon ${tone}">${icon('layers', 16)}</div>
      <div><div class="card-title">Sync queue</div><div class="card-sub">GitHub → Gitea</div></div>
      <div class="card-actions">${ui.pill(tone, queue.status)}</div>
    </div>
    <div class="card-body health-body">
      <div class="health-state"><span class="status-dot ${tone === 'bad' ? 'bad' : tone === 'warn' ? 'warn' : 'ok'}"></span><span>${queue.completed}/${queue.total} complete${queue.error ? ` · ${esc(queue.error)}` : ''}</span></div>
      <div class="queue-progress compact"><div class="bar-track"><div class="bar-fill ${tone}" style="width:${queue.total ? Math.round((queue.completed / queue.total) * 100) : 0}%"></div></div></div>
      <div class="health-foot">${queue.run_id ? `<button class="btn ghost sm" data-repo-queue-log="${esc(queue.run_id)}">${icon('terminal', 12)} View log</button>` : ''}<a class="btn ghost sm" href="#/repos">Projects</a></div>
    </div>
  </div>`;
}

function contentHtml(ctx) {
  const snapshot = reposState(ctx);
  if (!snapshot) {
    return pageHead({ title: 'Overview', subtitle: 'Loading projects…' }) + emptyState('git', 'Loading projects', 'The console is scanning the local checkout root.');
  }
  const repos = allRepos(ctx);
  if (snapshot.root_configured === false) {
    return `${pageHead({ title: 'Overview', subtitle: 'Choose a folder for your git checkouts' })}
      ${emptyState('git', 'No project folder yet', 'Repos scans a folder you choose; it is not enabled by default.', '<a class="btn primary mt-2" href="#/repos">Choose project folder</a>')}`;
  }
  const summary = snapshot.summary || {};
  const connectivity = snapshot.connectivity || {};
  const gitea = snapshot.gitea || {};
  const giteaChecking = gitea.state === 'checking';
  const queue = snapshot.queue;
  const behind = repos.filter((repo) => repo.behind > 0).length;
  const authErrors = repos.filter((repo) => repo.gitea?.status === 'auth').length;
  const giteaBehind = repos.filter((repo) => ['behind', 'missing'].includes(repo.gitea?.status)).length;
  const items = attentionItems(repos);
  const giteaConfigured = snapshot.gitea_configured !== false;
  const backup = backupSummary(repos, { configured: giteaConfigured });
  const medianAge = medianCommitAge(repos);
  const stale = !connectivity.checked_at || (Date.now() / 1000 - Number(connectivity.checked_at) > 300);
  const checking = connectivity.state === 'checking' || connectivity.github?.state === 'checking' || connectivity.gitea?.state === 'checking';
  const checkButton = `<button class="btn ghost sm" type="button" data-repo-check-connections ${checking ? 'disabled' : ''}>${checking ? '<span class="spinner sm"></span> Checking…' : `${icon('refresh', 12)} Check connections`}</button>`;
  return `${pageHead({
    title: 'Overview',
    subtitle: `<strong>${summary.total || 0}</strong> projects<span class="dot">·</span><strong>${summary.attention || 0}</strong> need attention<span class="dot">·</span><span class="mono" title="${esc((snapshot.roots || rootsFromSettings(ctx.bootstrap?.settings)).join('\n'))}">${esc(rootsLabel(snapshot.roots || rootsFromSettings(ctx.bootstrap?.settings)))}</span>`,
    actions: `${checkButton}<a class="btn primary sm" href="#/repos">${icon('git', 12)} Projects</a>`,
    badges: `${behind ? `<span class="pill info" title="Projects behind upstream, based on the last fetch">${icon('download', 11)} ${behind} update${behind === 1 ? '' : 's'}</span>` : ''}${stale ? ui.pill('neutral', 'connections not checked') : ui.pill(checking ? 'info' : 'ok', checking ? 'checking connections' : `checked ${ui.ago(connectivity.checked_at)}`)}`,
  })}
  ${snapshot.root_error ? `<div class="banner bad">${icon('alert')}<span><strong>Project folder unavailable.</strong> ${esc(snapshot.root_error)}</span><a class="btn sm ghost" href="#/settings">Settings</a></div>` : ''}
  ${snapshot.gitea_config_error ? `<div class="banner bad">${icon('alert')}<span><strong>Gitea configuration is invalid.</strong> ${esc(snapshot.gitea_config_error)}</span><a class="btn sm ghost" href="#/settings">Settings</a></div>` : ''}
  ${queue && ['queued', 'running'].includes(queue.status) ? `<div class="banner" data-no-dismiss>${icon('layers')}<span><strong>Queue running.</strong> ${queue.completed}/${queue.total} complete — GitHub then Gitea for each project.</span>${queue.run_id ? `<button class="btn sm ghost" data-repo-queue-log="${esc(queue.run_id)}">${icon('terminal', 12)} View log</button>` : ''}</div>` : ''}
  ${giteaAuthBanner(ctx, authErrors)}
  <div class="grid cols-4 mb-2">
    ${statTile({ label: 'Projects', value: String(summary.total || 0), sub: `${summary.github || 0} with GitHub · ${(summary.total || 0) - (summary.github || 0)} local-only`, iconName: 'git' })}
    ${statTile({ label: 'Files to commit', value: String(summary.dirty_files || 0), sub: summary.dirty_repos ? `${ui.plural(summary.dirty_repos, 'project')} dirty${summary.ignored_files ? ` · ${summary.ignored_files} ignored hidden` : ''}` : 'all working trees clean', iconName: 'commit', toneName: summary.dirty_repos ? 'warn' : 'ok', valueTone: summary.dirty_repos ? 'text-warn' : '' })}
    ${statTile({ label: 'Commits to push', value: String(summary.ahead_commits || 0), sub: summary.ahead_repos ? `${ui.plural(summary.ahead_repos, 'project')} ahead of GitHub` : 'GitHub is current', iconName: 'upload', toneName: summary.ahead_repos ? 'warn' : 'ok', valueTone: summary.ahead_repos ? 'text-warn' : '' })}
    ${statTile({
      label: 'Backup coverage',
      value: !backup.configured || !backup.checked || !backup.eligible ? '—' : `${backup.synced}<small> / ${backup.eligible}</small>`,
      sub: !backup.configured ? 'Gitea is not configured' : !backup.checked ? 'refs not checked yet' : !backup.eligible ? 'no projects eligible for backup' : `${backup.pct}% of eligible refs in sync · ${ui.ago(gitea.checked_at)}`,
      iconName: 'shield',
      toneName: backup.configured && backup.checked && backup.eligible && backup.synced === backup.eligible ? 'ok' : backup.configured && backup.checked && backup.synced < backup.eligible ? 'warn' : '',
      valueTone: backup.configured && backup.checked && backup.eligible && backup.synced < backup.eligible ? 'text-warn' : '',
    })}
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
      entry: { state: snapshot.vscode?.available ? 'up' : 'down', detail: snapshot.vscode?.available ? 'code command detected — opens a new window per project' : 'code command not found; install the Shell Command or keep Visual Studio Code in /Applications' },
      detail: 'local editor',
      chips: ['<a class="chip" href="#/repos">Open projects</a>'],
    })}
    ${queueHtml(queue)}
  </div>
  <div class="card repo-pulse-card section">
    <div class="card-head">
      <div class="health-icon info">${icon('activity', 16)}</div>
      <div><div class="card-title">Project pulse</div><div class="card-sub">commit cadence and project freshness · last 14 days</div></div>
      <div class="card-actions"><a class="btn ghost sm" href="#/repo-pulse">${icon('activity', 12)} Open pulse</a></div>
    </div>
    <div class="card-body">
      <div class="repo-pulse-chart">${activityBars(summary.activity_days || [])}</div>
      <div class="repo-pulse-side">
        ${activitySummaryHtml(summary, repos)}
        <div class="pulse-note">${medianAge === null ? 'No commit history yet.' : `Median repository is ${esc(formatAgeDays(medianAge))} since its last commit.`}</div>
      </div>
    </div>
  </div>
  <div class="grid cols-2 section">
    <div>
      <div class="section-head"><div class="section-title">Needs attention <span class="count">${items.length}</span></div><div class="section-sub">commit, push, pull and backup work</div></div>
      ${attentionHtml(items)}
      <div class="section-head mt-3"><div class="section-title">Quiet projects <span class="count">${Number(summary.quiet_repos || 0)}</span></div><div class="section-sub">no commits or edits in 90 days</div></div>
      ${quietReposHtml(repos, 5)}
    </div>
    <div>
      <div class="section-head"><div class="section-title">Recent commits</div><div class="section-sub">latest commit across every project</div></div>
      ${commitListHtml(repos, ctx.state?.activity || [], 5)}
    </div>
  </div>
  <div class="hint mt-2">Activity metrics come from local git history and are cached until a branch tip changes. Connectivity checks are a short HTTPS probe to github.com and the Gitea version endpoint; they run when you open this overview, press Check connections, or finish a GitHub push or Gitea sync. Gitea ref comparison runs at startup, after a project commit/push, and on demand; mirroring stays explicit.</div>`;
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

    bindRepoFocus(root, ctx);
    root.querySelectorAll('[data-repo-review]').forEach((button) => {
      button.addEventListener('click', () => focusRepos(ctx, button.dataset.repoReview, button.dataset.repoName || ''));
    });
    root.querySelectorAll('[data-repo-check-connections]').forEach((button) => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        button.innerHTML = '<span class="spinner sm"></span> Checking…';
        ctx.uiState.repoConnRequested = Date.now();
        try {
          await ctx.api.post('/api/repos/connectivity/check');
          ui.toast('Checking GitHub and Gitea', 'ok', 'Short HTTPS reachability probes; no project writes.');
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
      button.addEventListener('click', () => ui.runModal(ctx, button.dataset.repoQueueLog, 'Projects: GitHub + Gitea'));
    });
  },
};
