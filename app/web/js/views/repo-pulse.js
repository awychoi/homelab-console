import * as ui from '../ui.js';
import { rootsFromSettings, rootsLabel } from '../repoRoots.js';
import {
  activityBars, allRepos, backupSummary, bindRepoFocus, commitListHtml,
  coverageHtml, formatAgeDays, freshnessHtml, lastCommitAge, medianCommitAge, oldestCommitAge,
  quietReposHtml, reposSnapshot, topActiveRepos,
} from '../repoMetrics.js';
import { pageHead, statTile, emptyState } from './common.js';

const { esc, icon } = ui;

function activeReposHtml(repos) {
  const top = topActiveRepos(repos, 6);
  if (!top.length) {
    return `<div class="card"><div class="card-body attention-empty"><span class="status-dot"></span><span class="muted">No commits in the last 30 days. The projects are quiet but not necessarily unhealthy.</span></div></div>`;
  }
  const max = Math.max(1, ...top.map((repo) => Number(repo.commit_activity?.last_30d || 0)));
  return `<div class="card"><ul class="active-list">
    ${top.map((repo, index) => {
      const count = Number(repo.commit_activity?.last_30d || 0);
      const age = lastCommitAge(repo);
      return `<li class="active-item">
        <span class="active-rank mono">${String(index + 1).padStart(2, '0')}</span>
        <div class="active-main">
          <a href="#/repos" data-repo-focus="${esc(repo.rel)}">${esc(repo.rel)}</a>
          <span class="active-meta">${esc(repo.branch || 'no branch')} · last commit ${esc(formatAgeDays(age))}</span>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.round((count / max) * 100)}%"></div></div>
        </div>
        <span class="active-count mono" title="${count} commit${count === 1 ? '' : 's'} in the last 30 days">${count}</span>
      </li>`;
    }).join('')}
  </ul></div>`;
}

function contentHtml(ctx) {
  const snapshot = reposSnapshot(ctx);
  if (!snapshot) {
    return pageHead({ title: 'Pulse', subtitle: 'Loading project metrics…' })
      + emptyState('activity', 'Loading pulse', 'The console is reading local git history.');
  }
  if (snapshot.root_configured === false) {
    return `${pageHead({ title: 'Pulse', subtitle: 'Choose a folder for your git checkouts' })}
      ${emptyState('activity', 'No project folder yet', 'Pulse reads commit history from the projects under your configured checkout folders.', '<a class="btn primary mt-2" href="#/repos">Choose project folder</a>')}`;
  }
  const repos = allRepos(ctx);
  if (!repos.length) {
    return `${pageHead({ title: 'Pulse', subtitle: 'No projects selected' })}
      ${emptyState('git', 'No projects to measure', 'Choose a project folder or enable a project on the Projects page.', '<a class="btn primary mt-2" href="#/repos">Open Projects</a>')}`;
  }
  const summary = snapshot.summary || {};
  const giteaConfigured = snapshot.gitea_configured !== false;
  const giteaChecking = snapshot.gitea?.state === 'checking';
  const backup = backupSummary(repos, { configured: giteaConfigured });
  const medianAge = medianCommitAge(repos);
  const oldestAge = oldestCommitAge(repos);
  const days = summary.activity_days || [];
  const activeDays = days.filter((value) => Number(value) > 0).length;
  const commits30 = Number(summary.commits_30d || 0);
  const commits7 = Number(summary.commits_7d || 0);
  const activeCount = Number(summary.active_repos || 0);
  const roots = snapshot.roots || rootsFromSettings(ctx.bootstrap?.settings);
  return `${pageHead({
    title: 'Pulse',
    subtitle: `<strong>${repos.length}</strong> projects<span class="dot">·</span><strong>${commits30}</strong> commits in 30 days<span class="dot">·</span>${esc(rootsLabel(roots))}`,
    actions: `<a class="btn ghost sm" href="#/repo-street">${icon('overview', 12)} Overview</a><a class="btn primary sm" href="#/repos">${icon('git', 12)} Projects</a>`,
    badges: ui.pill(activeDays ? 'info' : 'neutral', activeDays ? `${activeDays} active day${activeDays === 1 ? '' : 's'}` : 'no recent commits'),
  })}
  <div class="grid cols-4 mb-2">
    ${statTile({ label: 'Commits · 30 days', value: String(commits30), sub: `${commits7} in the last 7 days`, iconName: 'commit', toneName: commits30 ? 'info' : '' })}
    ${statTile({ label: 'Active projects', value: `${activeCount}<small> / ${repos.length}</small>`, sub: 'committed in the last 30 days', iconName: 'git', toneName: activeCount ? 'ok' : '' })}
    ${statTile({ label: 'Median last commit', value: medianAge === null ? '—' : esc(formatAgeDays(medianAge)), sub: oldestAge === null ? 'no commit history' : `oldest ${esc(formatAgeDays(oldestAge))}`, iconName: 'clock', toneName: medianAge !== null && medianAge > 30 ? 'warn' : 'ok' })}
    ${statTile({ label: 'Backup coverage', value: !backup.configured || !backup.checked || !backup.eligible ? '—' : `${backup.pct}<small>%</small>`, sub: !backup.configured ? 'Gitea is not configured' : !backup.checked ? 'refs not checked yet' : !backup.eligible ? 'no projects eligible for backup' : `${backup.synced} of ${backup.eligible} eligible refs in sync`, iconName: 'shield', toneName: backup.configured && backup.checked && backup.eligible && backup.synced === backup.eligible ? 'ok' : backup.configured && backup.checked && backup.synced < backup.eligible ? 'warn' : '', valueTone: backup.configured && backup.checked && backup.eligible && backup.synced < backup.eligible ? 'text-warn' : '' })}
  </div>
  <div class="grid cols-2 section">
    <div class="card">
      <div class="card-head">
        <div class="health-icon info">${icon('activity', 16)}</div>
        <div><div class="card-title">Commit activity</div><div class="card-sub">Every project · last 14 days</div></div>
        <div class="card-actions">${ui.pill(commits30 ? 'info' : 'neutral', `${commits30} in 30 days`)}</div>
      </div>
      <div class="card-body">${activityBars(days)}</div>
    </div>
    <div class="card">
      <div class="card-head">
        <div class="health-icon ok">${icon('clock', 16)}</div>
        <div><div class="card-title">Project freshness</div><div class="card-sub">When each working tree last moved</div></div>
      </div>
      <div class="card-body">${freshnessHtml(repos)}</div>
    </div>
  </div>
  <div class="grid cols-2 section">
    <div>
      <div class="section-head"><div class="section-title">Most active projects</div><div class="section-sub">commits in the last 30 days</div></div>
      ${activeReposHtml(repos)}
    </div>
    <div>
      <div class="section-head"><div class="section-title">Remote &amp; backup coverage</div><div class="section-sub">what is pushed and mirrored</div></div>
      <div class="card">
        <div class="card-head">
          <div class="card-title">Coverage</div>
          <div class="card-actions"><button class="btn ghost sm" type="button" data-pulse-gitea-check ${!giteaConfigured || giteaChecking ? 'disabled' : ''} title="${giteaConfigured ? 'Compare local refs with the Gitea backup' : 'Set a Gitea URL and owner in Settings first'}">${giteaChecking ? '<span class="spinner sm"></span> Checking…' : `${icon('git', 12)} Check refs`}</button></div>
        </div>
        <div class="card-body">${coverageHtml(repos, { configured: giteaConfigured })}</div>
      </div>
    </div>
  </div>
  <div class="grid cols-2 section">
    <div>
      <div class="section-head"><div class="section-title">Recent commits</div><div class="section-sub">newest first across all projects</div></div>
      ${commitListHtml(repos, ctx.state?.activity || [], 8)}
    </div>
    <div>
      <div class="section-head"><div class="section-title">Quiet projects <span class="count">${Number(summary.quiet_repos || 0)}</span></div><div class="section-sub">no commits or edits in 90 days</div></div>
      ${quietReposHtml(repos, 6)}
    </div>
  </div>
  <div class="hint mt-2">Pulse uses local git history only: commit counts come from the checked-out branch, so they exclude work that exists only on another clone. Activity is cached by branch tip; the 14-day chart updates as soon as a new commit is scanned. Backup coverage compares the stored Gitea ref checks; run <strong>Check backup refs</strong> on Overview to refresh it.</div>`;
}

export default {
  render(ctx) {
    return contentHtml(ctx);
  },
  mount(root, ctx) {
    bindRepoFocus(root, ctx);
    root.querySelectorAll('[data-pulse-gitea-check]').forEach((button) => {
      button.addEventListener('click', async () => {
        if (button.disabled) return;
        button.disabled = true;
        button.innerHTML = '<span class="spinner sm"></span> Checking…';
        try {
          await ctx.api.post('/api/repos/gitea/check');
          ui.toast('Checking Gitea refs', 'ok', 'Backup comparison is read-only.');
          setTimeout(() => ctx.refreshState({ force: true }), 1200);
          setTimeout(() => ctx.refreshState({ force: true }), 4000);
        } catch (error) {
          button.disabled = false;
          button.innerHTML = `${icon('git', 12)} Check refs`;
          ui.toast('Could not start the Gitea check', 'err', ui.errorText(error));
        }
      });
    });
  },
};
