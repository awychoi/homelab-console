import * as ui from '../ui.js';
import { pageHead, stateBanner, statTile, emptyState, giteaAuthBanner } from './common.js';
import { nativePick } from '../native.js';
import { addRoot, bindRootRemoval, openRepoSelection, readRoots, rootsFromSettings, rootsHtml, rootsLabel } from '../repoRoots.js';
import { showGiteaSync, showPushBoth, showRepoCommit, refreshRemoteSoon } from '../flows.js';

const { esc, icon } = ui;

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'commit', label: 'To commit' },
  { id: 'push', label: 'To push' },
  { id: 'behind', label: 'Updates' },
  { id: 'gitea', label: 'Gitea' },
  { id: 'clean', label: 'Clean' },
];

function initUi(ctx) {
  if (!ctx.uiState.repos) ctx.uiState.repos = { filter: 'all', q: '', selection: null };
  return ctx.uiState.repos;
}

function reposState(ctx) {
  return ctx.state?.repos || ctx.bootstrap?.state?.repos || null;
}

function allRepos(ctx) {
  return reposState(ctx)?.repos || [];
}

function isClean(repo) {
  const status = repo.gitea?.status;
  return !repo.needs?.commit && !repo.needs?.push && !(repo.behind > 0) && (status === 'synced' || status === 'skip');
}

function isGiteaAttention(repo) {
  return ['behind', 'missing', 'auth', 'unreachable', 'error'].includes(repo.gitea?.status);
}

function filteredRepos(ctx) {
  const state = initUi(ctx);
  const query = state.q.trim().toLowerCase();
  return allRepos(ctx).filter((repo) => {
    if (state.filter === 'commit' && !repo.needs?.commit) return false;
    if (state.filter === 'push' && !repo.needs?.push) return false;
    if (state.filter === 'behind' && !(repo.behind > 0)) return false;
    if (state.filter === 'gitea' && !isGiteaAttention(repo)) return false;
    if (state.filter === 'clean' && !isClean(repo)) return false;
    if (!query) return true;
    const haystack = [
      repo.rel, repo.name, repo.container, repo.branch, repo.origin,
      repo.github?.owner, repo.github?.repo, repo.gitea?.target,
    ].filter(Boolean).join(' ').toLowerCase();
    return haystack.includes(query);
  });
}

function filterCounts(ctx) {
  const repos = allRepos(ctx);
  return {
    all: repos.length,
    commit: repos.filter((repo) => repo.needs?.commit).length,
    push: repos.filter((repo) => repo.needs?.push).length,
    behind: repos.filter((repo) => repo.behind > 0).length,
    gitea: repos.filter(isGiteaAttention).length,
    clean: repos.filter(isClean).length,
  };
}

function ensureSelection(ctx) {
  const state = initUi(ctx);
  const repos = allRepos(ctx);
  const ids = new Set(repos.map((repo) => repo.id));
  if (state.selection === null) {
    state.selection = new Set(
      repos.filter((repo) => (
        repo.needs?.push
        || (repo.gitea?.target && ['behind', 'missing'].includes(repo.gitea?.status))
      )).map((repo) => repo.id),
    );
  } else {
    for (const id of [...state.selection]) {
      if (!ids.has(id)) state.selection.delete(id);
    }
  }
  return state.selection;
}

function short(text, length = 46) {
  const value = String(text || '');
  return value.length > length ? `${value.slice(0, length - 1)}…` : value;
}

function giteaCell(repo) {
  const gitea = repo.gitea || {};
  const status = gitea.status || 'unknown';
  if (status === 'unknown') {
    return `<span class="chip neutral">not checked</span><span class="sub">on demand</span>`;
  }
  if (status === 'skip') {
    return `<span class="chip neutral">skipped</span><span class="sub" title="${esc(gitea.detail || '')}">${esc(short(gitea.detail, 44))}</span>`;
  }
  if (status === 'checking') {
    return `<span class="chip info"><span class="spinner sm"></span> checking</span>`;
  }
  if (status === 'synced' || status === 'created' || status === 'up-to-date') {
    return `<span class="chip good">in sync</span>${gitea.checked_at ? `<span class="sub">checked ${esc(ui.ago(gitea.checked_at))}</span>` : ''}`;
  }
  if (status === 'missing') {
    return `<button type="button" class="chip chip-action warn" data-repo-action="gitea" title="Create this project in Gitea and push the backup" aria-label="Create ${esc(repo.rel)} in Gitea">${icon('git', 11)} missing · create</button><span class="sub">${gitea.checked_at ? `checked ${esc(ui.ago(gitea.checked_at))}` : 'sync will create it'}</span>`;
  }
  if (status === 'behind') {
    const push = gitea.counts?.push || 0;
    const prune = gitea.counts?.prune || 0;
    return `<button type="button" class="chip chip-action warn" data-repo-action="gitea" title="Preview and sync the Gitea backup" aria-label="Sync ${esc(repo.rel)} to Gitea">${icon('git', 11)} ${push ? `${push} ref${push === 1 ? '' : 's'} to sync` : 'refs differ'}</button>
      ${prune ? `<span class="chip bad">${prune} to prune</span>` : ''}
      <span class="sub">checked ${esc(ui.ago(gitea.checked_at))}</span>`;
  }
  if (status === 'auth') {
    return `<span class="chip bad">token needed</span><span class="sub"><a href="#/settings">Open Settings → Repos</a></span>`;
  }
  if (status === 'unreachable' || status === 'error') {
    return `<span class="chip bad">${status === 'unreachable' ? 'unreachable' : 'check failed'}</span><span class="sub" title="${esc(gitea.detail || '')}">${esc(short(gitea.detail, 44))}</span>`;
  }
  return `<span class="chip neutral">${esc(status)}</span>`;
}

function workingTreeCell(repo) {
  const entries = (repo.entries || []).filter((entry) => !entry.ignored);
  if (repo.conflicts) {
    return `<span class="chip bad" title="Unresolved merge conflicts block commit and push">${icon('alert', 11)} ${repo.conflicts} conflict${repo.conflicts === 1 ? '' : 's'}</span><span class="sub">resolve before committing</span>`;
  }
  if (!repo.needs?.commit) {
    if (repo.ignored_count) {
      return `<span class="chip neutral" title="Tracked paths matching .gitignore are hidden from commit selection">${repo.ignored_count} ignored</span><span class="sub">not shown in commit selection</span>`;
    }
    return '<span class="chip good">clean</span>';
  }
  const staged = entries.filter((entry) => entry.staged).length;
  const unstaged = entries.filter((entry) => entry.unstaged && !entry.untracked).length;
  const untracked = entries.filter((entry) => entry.untracked).length;
  const bits = [
    staged ? `${staged} staged` : '',
    unstaged ? `${unstaged} unstaged` : '',
    untracked ? `${untracked} new` : '',
    repo.ignored_count ? `${repo.ignored_count} ignored hidden` : '',
  ].filter(Boolean).join(' · ');
  return `<button type="button" class="chip chip-action warn" data-repo-action="commit" title="Review and commit changed files" aria-label="Review and commit changed files in ${esc(repo.rel)}">${icon('commit', 11)} ${repo.dirty_count} file${repo.dirty_count === 1 ? '' : 's'}</button><span class="sub">${esc(bits || 'working tree changed')}</span>`;
}

function unpushedCell(repo) {
  const bits = [];
  if (repo.needs?.push) {
    const label = repo.ahead === null || repo.ahead === undefined ? 'new branch' : `${repo.ahead} ahead`;
    bits.push(`<button type="button" class="chip chip-action warn" data-repo-action="push" title="Push ${esc(repo.rel)} to ${repo.github ? 'GitHub' : 'origin'}" aria-label="Push ${esc(repo.rel)} to ${repo.github ? 'GitHub' : 'origin'}">${icon('push', 11)} ${esc(label)}</button>`);
  }
  if (repo.behind > 0) bits.push(`<span class="chip info" title="Updates available from origin; pull before pushing"><span class="repo-update-icon">${icon('download', 11)}</span> ${repo.behind} behind</span>`);
  if (!bits.length) {
    if (!repo.origin) bits.push('<span class="chip neutral">local only</span>');
    else if (repo.detached) bits.push('<span class="chip warn">detached</span>');
    else bits.push('<span class="chip good">up to date</span>');
  }
  if (repo.upstream) bits.push(`<span class="sub mono">${esc(repo.upstream)}</span>`);
  return bits.join('');
}

function lastCommitCell(repo) {
  const commit = repo.last_commit || {};
  if (!commit.sha) return '<span class="muted tiny">no commits yet</span>';
  const link = repo.github?.url
    ? `<a class="mono repo-sha" href="${esc(repo.github.url)}/commit/${esc(commit.sha)}" target="_blank" rel="noopener noreferrer">${esc(commit.sha)}</a>`
    : `<span class="mono repo-sha">${esc(commit.sha)}</span>`;
  return `${link}<span class="repo-commit-subject" title="${esc(commit.subject || '')}">${esc(short(commit.subject, 64))}</span><span class="repo-commit-date">${esc(commit.date || '')}</span>`;
}

function actionsCell(repo) {
  const actions = [];
  actions.push(`<button class="btn ghost sm icon" type="button" data-repo-action="open" title="Open ${esc(repo.rel)} in VS Code" aria-label="Open ${esc(repo.rel)} in VS Code">${icon('code', 13)}</button>`);
  if (repo.github?.url) {
    actions.push(`<a class="btn ghost sm icon" href="${esc(repo.github.url)}" target="_blank" rel="noopener noreferrer" title="Open ${esc(repo.rel)} on GitHub" aria-label="Open ${esc(repo.rel)} on GitHub">${icon('external', 13)}</a>`);
  } else {
    actions.push(`<span class="btn ghost sm icon disabled" title="No GitHub origin" aria-hidden="true">${icon('external', 13)}</span>`);
  }
  return actions.join('');
}

function rowHtml(ctx, repo, selected) {
  const checked = selected.has(repo.id) ? 'checked' : '';
  const container = repo.container ? `<span class="pill tiny neutral">${esc(repo.container)}</span>` : '';
  const badges = [
    repo.lfs ? '<span class="pill tiny warn">LFS</span>' : '',
    !repo.github ? '<span class="pill tiny neutral">local only</span>' : '',
  ].join('');
  const branch = repo.detached
    ? '<span class="chip warn">detached HEAD</span>'
    : `<span class="mono">${esc(repo.branch || '—')}</span>`;
  return `<tr data-repo-row data-repo-id="${esc(repo.id)}">
    <td class="repo-check"><input type="checkbox" class="repo-select" data-repo-select="${esc(repo.id)}" aria-label="Select ${esc(repo.rel)} for the push queue" ${checked}></td>
    <td class="main-cell repo-main">
      <div class="repo-name"><span class="status-dot ${repo.error ? 'bad' : isClean(repo) ? 'ok' : repo.needs?.commit || repo.needs?.push ? 'warn' : ''}"></span><strong>${esc(repo.name)}</strong>${container}${badges}</div>
      <div class="repo-path mono tiny" title="${esc(repo.path)}">${esc(repo.rel)}</div>
      <div class="repo-branch">${branch}${repo.upstream ? `<span class="sub mono">→ ${esc(repo.upstream)}</span>` : ''}</div>
      <div class="repo-last">${lastCommitCell(repo)}</div>
    </td>
    <td data-label="Working tree">${repo.error ? `<span class="chip bad">read failed</span><span class="sub" title="${esc(repo.error)}">${esc(short(repo.error, 42))}</span>` : workingTreeCell(repo)}</td>
    <td data-label="GitHub">${unpushedCell(repo)}</td>
    <td class="repo-gitea" data-label="Gitea">${giteaCell(repo)}</td>
    <td class="actions repo-actions" data-label="Open">${actionsCell(repo)}</td>
  </tr>`;
}

function rowsHtml(ctx) {
  const selected = ensureSelection(ctx);
  const rows = filteredRepos(ctx);
  if (!rows.length) {
    const state = initUi(ctx);
    const filtered = state.q.trim() || state.filter !== 'all';
    return `<tr><td colspan="6"><div class="empty empty-inline">
      <strong>No projects match</strong>
      <p>${filtered ? 'Clear the filters to see the whole project list again.' : 'No git projects were found under the configured root.'}</p>
      ${filtered ? '<button class="btn primary mt-1" data-clear-repo-filters>Clear filters</button>' : '<a class="btn primary mt-1" href="#/settings">Open Settings</a>'}
    </div></td></tr>`;
  }
  return rows.map((repo) => rowHtml(ctx, repo, selected)).join('');
}

function queueHtml(ctx) {
  const queue = reposState(ctx)?.queue;
  if (!queue) return '';
  const tone = queue.status === 'done' ? 'ok' : queue.status === 'partial' ? 'warn' : queue.status === 'running' ? 'info' : 'neutral';
  const pct = queue.total ? Math.round((queue.completed / queue.total) * 100) : 0;
  return `<div class="section">
    <div class="section-head"><div class="section-title">GitHub + Gitea queue</div><div class="section-sub">GitHub → Gitea, serial</div></div>
    <div class="card">
      <div class="card-head">
        <div class="card-title">${ui.pill(tone, queue.status)} <span class="muted tiny">${queue.completed}/${queue.total} complete</span></div>
        <div class="card-sub">${queue.error ? esc(queue.error) : queue.run_id ? esc(queue.run_id) : 'queued'}</div>
        <div class="card-actions">
          ${queue.run_id ? `<button class="btn ghost sm" type="button" data-repo-queue-log="${esc(queue.run_id)}">${icon('terminal', 12)} View log</button>` : ''}
        </div>
      </div>
      <div class="card-body tight">
        <div class="queue-progress"><div class="bar-track"><div class="bar-fill ${tone}" style="width:${pct}%"></div></div><span class="mono tiny">${pct}%</span></div>
        <div class="table-wrap"><table class="data queue-table">
          <thead><tr><th>Project</th><th>GitHub</th><th>Gitea</th><th>Result</th></tr></thead>
          <tbody>${queue.items.map((item) => `<tr>
            <td class="mono">${esc(item.name)}</td>
            <td data-label="GitHub">${ui.pill(item.github === 'ok' ? 'ok' : item.github === 'failed' ? 'bad' : item.github === 'skipped' ? 'neutral' : item.github === 'running' ? 'info' : 'neutral', item.github)}<span class="sub">${esc(short(item.github_detail, 42))}</span></td>
            <td data-label="Gitea">${ui.pill(item.gitea === 'ok' ? 'ok' : item.gitea === 'failed' ? 'bad' : item.gitea === 'skipped' ? 'neutral' : item.gitea === 'running' ? 'info' : 'neutral', item.gitea)}<span class="sub">${esc(short(item.gitea_detail, 42))}</span></td>
            <td data-label="Result">${ui.pill(item.status === 'ok' ? 'ok' : item.status === 'failed' ? 'bad' : item.status === 'partial' ? 'warn' : item.status === 'running' ? 'info' : 'neutral', item.status)}${item.error ? `<span class="sub">${esc(short(item.error, 60))}</span>` : ''}</td>
          </tr>`).join('')}</tbody>
        </table></div>
      </div>
    </div>
  </div>`;
}

function repoRootSetup(ctx) {
  const settings = ctx.bootstrap?.settings || {};
  if (!ctx.uiState.repoRootsDraft) ctx.uiState.repoRootsDraft = rootsFromSettings(settings);
  const roots = ctx.uiState.repoRootsDraft;
  return `<div class="card">
    <div class="card-head"><div class="card-title">Choose project folders</div><div class="card-sub">Projects are off until you add one</div></div>
    <div class="card-body">
      <div class="field">
        <label>Project folders</label>
        <div id="repo-roots-first" class="root-list">${rootsHtml(roots)}</div>
        <div class="field-row mt-1">
          <label class="sr-only" for="repo-root-first">Project folder path</label>
          <input id="repo-root-first" placeholder="/Users/you/code" autocomplete="off" spellcheck="false">
          <button class="btn ghost sm" type="button" data-add-repo-root-first>Add path</button>
          <button class="btn sm" type="button" data-pick-repo-roots-first>Choose folders…</button>
        </div>
        <div class="hint">Each folder is scanned recursively for git projects. Hidden and dependency/build folders are skipped; choose a single project folder too if that is all you need.</div>
      </div>
      <div class="btn-group"><button class="btn primary" data-save-repo-root>Use these folders</button><a class="btn ghost" href="#/settings">Other settings</a></div>
      <div id="repo-root-error"></div>
    </div>
  </div>`;
}

function contentHtml(ctx) {
  const state = initUi(ctx);
  const snapshot = reposState(ctx);
  const configured = Boolean(snapshot?.roots?.length || snapshot?.root) && snapshot.root_configured !== false;
  if (!configured) {
    return `${pageHead({ title: 'Projects', subtitle: 'Choose where your git checkouts live' })}
      ${stateBanner(ctx)}
      ${repoRootSetup(ctx)}`;
  }
  const repos = allRepos(ctx);
  const summary = snapshot?.summary || {};
  const counts = filterCounts(ctx);
  const selected = ensureSelection(ctx);
  const gitea = snapshot?.gitea || {};
  const giteaConfigured = snapshot?.gitea_configured !== false;
  const checking = gitea.state === 'checking';
  const queueActive = snapshot?.queue && ['queued', 'running'].includes(snapshot.queue.status);
  const root = snapshot?.root || '';
  const roots = snapshot?.roots?.length ? snapshot.roots : rootsFromSettings(ctx.bootstrap?.settings);
  const rootError = snapshot?.root_error || '';
  const giteaConfigError = snapshot?.gitea_config_error || '';
  const authErrors = repos.filter((repo) => repo.gitea?.status === 'auth').length;
  const giteaTile = gitea.checked_at
    ? { value: String(summary.gitea_behind || 0), sub: `${ui.plural(summary.gitea_checked || 0, 'project')} checked · ${ui.ago(gitea.checked_at)}` }
    : { value: '—', sub: 'not checked yet' };
  const actions = `
    <button class="btn ghost sm" type="button" data-repo-manage title="Choose which discovered projects Repos manages">${icon('settings', 12)} Manage</button>
    <button class="btn ghost sm" type="button" data-repo-refresh title="Rescan local projects" aria-label="Rescan local projects">${icon('refresh', 12)} Rescan</button>
    <button class="btn ghost sm" type="button" data-repo-check-gitea ${checking || !giteaConfigured ? 'disabled' : ''} title="${giteaConfigured ? 'Compare local refs with Gitea' : 'Set a Gitea URL and owner in Settings first'}">${checking ? '<span class="spinner sm"></span> Checking…' : `${icon('git', 12)} Check Gitea`}</button>
    <button class="btn primary sm" type="button" data-repo-push-both ${selected.size ? '' : 'disabled'}>${icon('layers', 12)} Queue both${selected.size ? ` (${selected.size})` : ''}</button>`;
  return `${pageHead({
    title: 'Projects',
    subtitle: `<strong>${summary.total || 0}</strong> projects<span class="dot">·</span><strong>${summary.attention || 0}</strong> need attention<span class="dot">·</span><span class="mono" title="${esc(roots.join('\n'))}">${esc(rootsLabel(roots))}</span>`,
    actions,
    badges: gitea.checked_at ? ui.pill('ok', `Gitea checked ${ui.ago(gitea.checked_at)}`) : ui.pill('neutral', 'Gitea on demand'),
  })}
  ${rootError ? `<div class="banner bad">${icon('alert')}<span><strong>Project folder unavailable.</strong> ${esc(rootError)}</span><a class="btn sm ghost" href="#/settings">Settings</a></div>` : ''}
  ${giteaConfigError ? `<div class="banner bad">${icon('alert')}<span><strong>Gitea configuration is invalid.</strong> ${esc(giteaConfigError)}</span><a class="btn sm ghost" href="#/settings">Settings</a></div>` : ''}
  ${queueActive ? `<div class="banner">${icon('layers')}<span><strong>Queue running.</strong> ${snapshot.queue.completed}/${snapshot.queue.total} complete — GitHub then Gitea for each project.</span>${snapshot.queue.run_id ? `<button class="btn sm ghost" data-repo-queue-log="${esc(snapshot.queue.run_id)}">${icon('terminal', 12)} View log</button>` : ''}</div>` : ''}
  ${giteaAuthBanner(ctx, authErrors)}
  ${!repos.length && Number(summary.discovered || 0) > 0 ? `<div class="banner">${icon('info')}<span><strong>All discovered projects are deselected.</strong> They stay on disk; use <strong>Manage</strong> to add them back.</span><button class="btn sm ghost" type="button" data-repo-manage>${icon('settings', 12)} Manage</button></div>` : ''}
  <div class="grid cols-4 mb-2">
    ${statTile({ label: 'Projects', value: String(summary.total || 0), sub: `${summary.github || 0} with GitHub · ${(summary.total || 0) - (summary.github || 0)} local-only`, iconName: 'git', toneName: '', valueTone: '' })}
    ${statTile({ label: 'Files to commit', value: String(summary.dirty_files || 0), sub: summary.dirty_repos ? `${ui.plural(summary.dirty_repos, 'project')} dirty${summary.ignored_files ? ` · ${summary.ignored_files} ignored hidden` : ''}` : 'all working trees clean', iconName: 'commit', toneName: summary.dirty_repos ? 'warn' : 'ok', valueTone: summary.dirty_repos ? 'text-warn' : '' })}
    ${statTile({ label: 'Commits to push', value: String(summary.ahead_commits || 0), sub: summary.ahead_repos ? `${ui.plural(summary.ahead_repos, 'project')} ahead of GitHub` : 'GitHub is current', iconName: 'upload', toneName: summary.ahead_repos ? 'warn' : 'ok', valueTone: summary.ahead_repos ? 'text-warn' : '' })}
    ${statTile({ label: 'Gitea backup', value: giteaTile.value, sub: giteaTile.sub, iconName: 'shield', toneName: summary.gitea_behind ? 'warn' : gitea.checked_at ? 'ok' : '', valueTone: summary.gitea_behind ? 'text-warn' : '' })}
  </div>
  ${!gitea.checked_at && !rootError ? `<div class="banner">${icon('info')}<span><strong>Gitea state refreshes at startup and after project actions.</strong> Local dirty/ahead status refreshes every 10 seconds. Use <strong>Check Gitea</strong> to compare every eligible project's refs with the backup.</span></div>` : ''}
  <div class="toolbar">
    <div class="segmented" aria-label="Filter projects">
      ${FILTERS.map((item) => `<button type="button" class="${state.filter === item.id ? 'active' : ''}" data-repo-filter="${item.id}" aria-pressed="${state.filter === item.id}" title="${item.id === 'behind' ? 'Projects behind their upstream, based on the last fetch' : ''}">${item.label} <span class="mono">${counts[item.id] || 0}</span></button>`).join('')}
    </div>
    <div class="spacer"></div>
    <div class="search">${icon('search')}<input id="repo-search" type="search" placeholder="Filter projects or paths…" aria-label="Filter projects" value="${esc(state.q)}" autocomplete="off" spellcheck="false"></div>
  </div>
  <div class="card"><div class="table-wrap"><table class="data repo-table">
    <thead><tr>
      <th class="repo-check"><input type="checkbox" id="repo-select-all" aria-label="Select all visible projects"></th>
      <th>Project</th><th>Working tree</th><th>GitHub</th><th>Gitea</th><th class="actions">Open</th>
    </tr></thead>
    <tbody id="repo-rows">${rowsHtml(ctx)}</tbody>
  </table></div></div>
  ${queueHtml(ctx)}
  <div class="hint mt-2">VS Code opens a new window per project. GitHub links use your browser's existing session, so private projects work without exposing a token to the console. Gitea is a backup mirror: it is only synced when you ask.</div>`;
}

function repoById(ctx, id) {
  return allRepos(ctx).find((repo) => repo.id === id) || null;
}

function selectedRepos(ctx) {
  const selected = ensureSelection(ctx);
  return allRepos(ctx).filter((repo) => selected.has(repo.id));
}

function syncHeaderCheckbox(root, ctx) {
  const header = root.querySelector('#repo-select-all');
  if (!header) return;
  const rows = filteredRepos(ctx);
  const selected = ensureSelection(ctx);
  const selectedVisible = rows.filter((repo) => selected.has(repo.id)).length;
  header.checked = rows.length > 0 && selectedVisible === rows.length;
  header.indeterminate = selectedVisible > 0 && selectedVisible < rows.length;
}

function syncPushButton(root, ctx) {
  const button = root.querySelector('[data-repo-push-both]');
  if (!button) return;
  const count = ensureSelection(ctx).size;
  button.disabled = count === 0;
  button.innerHTML = `${icon('layers', 12)} Queue both${count ? ` (${count})` : ''}`;
}

export default {
  render(ctx) {
    return contentHtml(ctx);
  },
  mount(root, ctx) {
    const snapshot = reposState(ctx);
    if (!snapshot) return;
    const bindManage = () => {
      root.querySelectorAll('[data-repo-manage]').forEach((button) => {
        if (button.dataset.bound === '1') return;
        button.dataset.bound = '1';
        button.addEventListener('click', () => openRepoSelection(ctx));
      });
    };
    bindManage();
    const saveRootButton = root.querySelector('[data-save-repo-root]');
    if (saveRootButton) {
      const input = root.querySelector('#repo-root-first');
      const errorBox = root.querySelector('#repo-root-error');
      const list = root.querySelector('#repo-roots-first');
      bindRootRemoval(list);
      const add = () => { if (addRoot(list, input?.value || '')) input.value = ''; };
      root.querySelector('[data-add-repo-root-first]')?.addEventListener('click', add);
      input?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); add(); }
      });
      root.querySelector('[data-pick-repo-roots-first]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const picked = await nativePick({ directories: true, multiple: true });
          if (picked === null) { input?.focus(); ui.toast('Type a folder path, then Add path', 'info'); }
          else for (const path of picked) addRoot(list, path);
        } finally { button.disabled = false; }
      });
      saveRootButton.addEventListener('click', async () => {
        const roots = readRoots(list);
        if (!roots.length) {
          if (errorBox) errorBox.innerHTML = '<div class="danger-box">Add at least one folder.</div>';
          input?.focus();
          return;
        }
        saveRootButton.disabled = true;
        try {
          await ctx.api.post('/api/settings', { repo_roots: roots });
          ctx.uiState.repoRootsDraft = roots;
          await ctx.reloadBootstrap();
          await ctx.refreshState({ force: true });
          ui.toast('Project folders saved', 'ok', roots.length === 1 ? roots[0] : `${roots.length} folders`);
          ctx.navigate('#/repos');
        } catch (error) {
          if (errorBox) errorBox.innerHTML = `<div class="danger-box">${esc(ui.errorText(error))}</div>`;
        } finally {
          saveRootButton.disabled = false;
        }
      });
      return;
    }
    const state = initUi(ctx);
    const tbody = root.querySelector('#repo-rows');
    const search = root.querySelector('#repo-search');

    const repaint = () => {
      if (!tbody) return;
      tbody.innerHTML = rowsHtml(ctx);
      bindRows();
      syncHeaderCheckbox(root, ctx);
      syncPushButton(root, ctx);
      root.querySelectorAll('[data-repo-filter]').forEach((node) => {
        const active = node.dataset.repoFilter === state.filter;
        node.classList.toggle('active', active);
        node.setAttribute('aria-pressed', String(active));
      });
    };

    const reload = async ({ message = '', force = true } = {}) => {
      await ctx.refreshState({ force });
      if (message) ui.toast(message, 'ok');
    };

    const bindRows = () => {
      if (!tbody) return;
      tbody.querySelectorAll('.repo-select').forEach((node) => {
        node.addEventListener('change', () => {
          if (node.checked) state.selection.add(node.dataset.repoSelect);
          else state.selection.delete(node.dataset.repoSelect);
          syncHeaderCheckbox(root, ctx);
          syncPushButton(root, ctx);
        });
      });
      tbody.querySelectorAll('[data-repo-action]').forEach((button) => {
        button.addEventListener('click', async () => {
          const repo = repoById(ctx, button.closest('[data-repo-id]')?.dataset.repoId);
          if (!repo) return;
          const action = button.dataset.repoAction;
          if (action === 'commit') {
            showRepoCommit(ctx, repo);
            return;
          }
          if (action === 'gitea') {
            showGiteaSync(ctx, repo);
            return;
          }
          if (action === 'open') {
            const original = button.innerHTML;
            button.disabled = true;
            try {
              await ctx.api.post(`/api/repos/${encodeURIComponent(repo.id)}/open`);
              ui.toast('Opening in VS Code', 'ok', repo.rel);
            } catch (error) {
              ui.toast('Could not open VS Code', 'err', ui.errorText(error));
            } finally {
              button.disabled = false;
              button.innerHTML = original;
            }
            return;
          }
          if (action === 'push') {
            const original = button.innerHTML;
            button.disabled = true;
            button.innerHTML = '<span class="spinner sm"></span> Pushing…';
            try {
              const result = await ctx.api.post(`/api/repos/${encodeURIComponent(repo.id)}/push`);
              ui.toast('Pushed to GitHub', 'ok', `${repo.rel} · ${result.detail || ''}`);
              await ctx.refreshState({ force: true });
              refreshRemoteSoon(ctx);
            } catch (error) {
              ui.toast('GitHub push failed', 'err', ui.errorText(error));
              button.disabled = false;
              button.innerHTML = original;
            }
          }
        });
      });
    };

    root.querySelectorAll('[data-repo-filter]').forEach((button) => {
      button.addEventListener('click', () => {
        state.filter = button.dataset.repoFilter;
        repaint();
      });
    });

    root.querySelector('#repo-select-all')?.addEventListener('change', (event) => {
      const visible = filteredRepos(ctx);
      for (const repo of visible) {
        if (event.currentTarget.checked) state.selection.add(repo.id);
        else state.selection.delete(repo.id);
      }
      repaint();
    });

    search?.addEventListener('input', () => {
      state.q = search.value;
      repaint();
    });
    search?.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && search.value) {
        event.preventDefault();
        state.q = '';
        search.value = '';
        repaint();
      }
    });

    root.querySelector('[data-clear-repo-filters]')?.addEventListener('click', () => {
      state.filter = 'all';
      state.q = '';
      if (search) search.value = '';
      repaint();
      search?.focus();
    });

    root.querySelector('[data-repo-check-gitea]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      button.innerHTML = '<span class="spinner sm"></span> Checking…';
      try {
        await ctx.api.post('/api/repos/gitea/check');
        ui.toast('Checking Gitea refs', 'ok', 'The column fills as each project is compared.');
        setTimeout(() => ctx.refreshState({ force: true }), 1200);
        setTimeout(() => ctx.refreshState({ force: true }), 4000);
      } catch (error) {
        button.disabled = false;
        button.innerHTML = `${icon('git', 12)} Check Gitea`;
        ui.toast('Could not start the Gitea check', 'err', ui.errorText(error));
      }
    });

    root.querySelector('[data-repo-refresh]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await ctx.api.post('/api/repos/refresh');
        await ctx.refreshState({ force: true });
        ui.toast('Project status refreshed', 'ok');
      } catch (error) {
        ui.toast('Could not rescan projects', 'err', ui.errorText(error));
      } finally {
        button.disabled = false;
      }
    });

    root.querySelector('[data-repo-push-both]')?.addEventListener('click', () => {
      const repos = selectedRepos(ctx);
      if (!repos.length) {
        ui.toast('Select at least one project', 'warn', 'Use the row checkboxes to build the queue.');
        return;
      }
      showPushBoth(ctx, repos);
    });

    root.querySelectorAll('[data-repo-queue-log]').forEach((button) => {
      button.addEventListener('click', () => ui.runModal(ctx, button.dataset.repoQueueLog, 'Projects: GitHub + Gitea'));
    });

    bindRows();
    syncHeaderCheckbox(root, ctx);
    syncPushButton(root, ctx);

    if (snapshot.queue && ['queued', 'running'].includes(snapshot.queue.status)) {
      setTimeout(() => {
        if (document.getElementById('repo-rows')) ctx.refreshState({ force: true });
      }, 3000);
    }

    const active = document.activeElement;
    if (active && active.id === 'repo-search') {
      active.focus();
      active.setSelectionRange(active.value.length, active.value.length);
    }
  },
};
