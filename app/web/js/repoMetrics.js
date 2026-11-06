import * as ui from './ui.js';

const { esc, icon } = ui;
const DAY = 86400;
export const ACTIVITY_DAYS = 14;

export function reposSnapshot(ctx) {
  return ctx.state?.repos || ctx.bootstrap?.state?.repos || null;
}

export function allRepos(ctx) {
  return reposSnapshot(ctx)?.repos || [];
}

export function focusRepos(ctx, filter = 'all', q = '') {
  if (!ctx.uiState.repos) ctx.uiState.repos = { filter: 'all', q: '', selection: null };
  ctx.uiState.repos.filter = filter || 'all';
  ctx.uiState.repos.q = q || '';
  ctx.navigate('#/repos');
}

export function bindRepoFocus(root, ctx) {
  root.querySelectorAll('[data-repo-focus]').forEach((node) => {
    node.addEventListener('click', () => {
      if (!ctx.uiState.repos) ctx.uiState.repos = { filter: 'all', q: '', selection: null };
      ctx.uiState.repos.filter = 'all';
      ctx.uiState.repos.q = node.dataset.repoFocus || '';
    });
  });
}

export function ageDays(timestamp, now = Date.now() / 1000) {
  if (!timestamp) return null;
  return Math.max(0, (now - Number(timestamp)) / DAY);
}

export function lastCommitAge(repo) {
  return ageDays(repo?.last_commit?.at);
}

export function lastTouchAge(repo) {
  return ageDays(repo?.last_touch_at || repo?.last_commit?.at);
}

export function formatAgeDays(days) {
  if (days === null || days === undefined) return 'no commits';
  if (days < 1) return 'today';
  if (days < 2) return '1 day';
  return `${Math.round(days)} days`;
}

export function commitHref(repo) {
  const commit = repo?.last_commit || {};
  if (!commit.sha || !repo?.github?.url) return '';
  return `${repo.github.url}/commit/${commit.sha}`;
}

export function topActiveRepos(repos, limit = 6) {
  return [...(repos || [])]
    .filter((repo) => Number(repo?.commit_activity?.last_30d || 0) > 0)
    .sort((a, b) => Number(b.commit_activity.last_30d) - Number(a.commit_activity.last_30d)
      || String(a.rel).localeCompare(String(b.rel)))
    .slice(0, limit);
}

export function recentCommits(repos, limit = 8) {
  return [...(repos || [])]
    .filter((repo) => repo?.last_commit?.at && repo?.last_commit?.sha)
    .sort((a, b) => Number(b.last_commit.at) - Number(a.last_commit.at) || String(a.rel).localeCompare(String(b.rel)))
    .slice(0, limit)
    .map((repo) => ({ repo, commit: repo.last_commit }));
}

export function quietRepos(repos) {
  return [...(repos || [])]
    .filter((repo) => {
      const at = repo?.last_touch_at || repo?.last_commit?.at;
      return !at || Number(at) < Date.now() / 1000 - 90 * DAY;
    })
    .sort((a, b) => Number(a.last_touch_at || a.last_commit?.at || 0) - Number(b.last_touch_at || b.last_commit?.at || 0));
}

export function freshnessBuckets(repos) {
  const buckets = [
    { id: 'week', label: 'This week', range: '0–7 days', count: 0 },
    { id: 'month', label: 'This month', range: '8–30 days', count: 0 },
    { id: 'quarter', label: 'Last quarter', range: '31–90 days', count: 0 },
    { id: 'dormant', label: 'Dormant', range: '90+ days', count: 0 },
  ];
  const now = Date.now() / 1000;
  for (const repo of repos || []) {
    const at = Number(repo?.last_touch_at || repo?.last_commit?.at || 0);
    if (!at) {
      buckets[3].count += 1;
      continue;
    }
    const days = (now - at) / DAY;
    if (days <= 7) buckets[0].count += 1;
    else if (days <= 30) buckets[1].count += 1;
    else if (days <= 90) buckets[2].count += 1;
    else buckets[3].count += 1;
  }
  return buckets;
}

export function freshnessHtml(repos) {
  const buckets = freshnessBuckets(repos);
  const total = Math.max(1, (repos || []).length);
  const summary = buckets.map((bucket) => `${bucket.count} ${bucket.label.toLowerCase()}`).join(' · ');
  return `<div class="freshness">
    <div class="freshness-bar" role="img" aria-label="Repository freshness: ${esc(summary)}">
      ${buckets.map((bucket) => bucket.count ? `<span class="freshness-seg ${bucket.id}" style="width:${(bucket.count / total) * 100}%"></span>` : '').join('')}
    </div>
    <div class="freshness-list">
      ${buckets.map((bucket) => `<div class="freshness-row">
        <span class="freshness-dot ${bucket.id}"></span>
        <span class="freshness-label">${esc(bucket.label)}</span>
        <span class="freshness-range">${esc(bucket.range)}</span>
        <span class="freshness-count mono">${bucket.count}</span>
      </div>`).join('')}
    </div>
  </div>`;
}

export function coverageRows(repos, { configured = true } = {}) {
  const all = repos || [];
  const total = all.length;
  const now = Date.now() / 1000;
  const synced = all.filter((repo) => ['synced', 'created', 'up-to-date'].includes(repo?.gitea?.status)).length;
  const checked = all.filter((repo) => ['synced', 'created', 'up-to-date', 'behind', 'missing', 'auth', 'unreachable', 'error', 'skip'].includes(repo?.gitea?.status)).length;
  const skipped = all.filter((repo) => repo?.gitea?.status === 'skip').length;
  const eligible = Math.max(0, total - skipped);
  const fetched = all.filter((repo) => repo.last_fetch_at && now - Number(repo.last_fetch_at) <= 7 * DAY).length;
  const latestFetch = Math.max(0, ...all.map((repo) => Number(repo.last_fetch_at || 0)));
  const rows = [
    { label: 'GitHub origin', value: all.filter((repo) => repo.github).length, total, hint: 'remote configured' },
    { label: 'Upstream tracking', value: all.filter((repo) => repo.upstream).length, total, hint: 'branch has an upstream' },
    { label: 'Fetched this week', value: fetched, total, hint: latestFetch ? `latest fetch ${ui.ago(latestFetch)}` : 'no fetch recorded yet' },
    { label: 'Clean working trees', value: all.filter((repo) => !repo?.needs?.commit).length, total, hint: 'no files waiting to commit' },
  ];
  if (!configured) {
    rows.splice(3, 0, { label: 'Gitea in sync', disabled: true, hint: 'Gitea is not configured' });
  } else {
    rows.splice(3, 0, {
      label: 'Gitea in sync',
      value: synced,
      total: eligible,
      hint: checked ? `${checked} ref set${checked === 1 ? '' : 's'} checked${skipped ? ` · ${skipped} skipped` : ''}` : 'not checked yet',
    });
  }
  return rows;
}

export function coverageHtml(repos, options = {}) {
  const rows = coverageRows(repos, options);
  return `<div class="coverage-list">
    ${rows.map((row) => {
      if (row.disabled) {
        return `<div class="coverage-row disabled">
          <div class="coverage-head"><span>${esc(row.label)}</span><span class="mono">not configured</span></div>
          <div class="coverage-hint">${esc(row.hint)}</div>
        </div>`;
      }
      const pct = row.total ? Math.round((row.value / row.total) * 100) : 0;
      return `<div class="coverage-row">
        <div class="coverage-head"><span>${esc(row.label)}</span><span class="mono">${row.value}/${row.total}</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
        <div class="coverage-hint">${esc(row.hint)}</div>
      </div>`;
    }).join('')}
  </div>`;
}

export function medianCommitAge(repos) {
  const ages = (repos || [])
    .map((repo) => lastCommitAge(repo))
    .filter((value) => value !== null && value !== undefined)
    .sort((a, b) => a - b);
  if (!ages.length) return null;
  const middle = Math.floor(ages.length / 2);
  return ages.length % 2 ? ages[middle] : (ages[middle - 1] + ages[middle]) / 2;
}

export function oldestCommitAge(repos) {
  const ages = (repos || [])
    .map((repo) => lastCommitAge(repo))
    .filter((value) => value !== null && value !== undefined);
  return ages.length ? Math.max(...ages) : null;
}

export function backupSummary(repos, { configured = true } = {}) {
  const all = repos || [];
  if (!configured) {
    return { configured: false, total: all.length, synced: 0, checked: 0, skipped: 0, eligible: 0, pct: 0 };
  }
  const synced = all.filter((repo) => ['synced', 'created', 'up-to-date'].includes(repo?.gitea?.status)).length;
  const checked = all.filter((repo) => ['synced', 'created', 'up-to-date', 'behind', 'missing', 'auth', 'unreachable', 'error', 'skip'].includes(repo?.gitea?.status)).length;
  const skipped = all.filter((repo) => repo?.gitea?.status === 'skip').length;
  const eligible = Math.max(0, all.length - skipped);
  return {
    configured: true,
    total: all.length,
    synced,
    checked,
    skipped,
    eligible,
    pct: eligible ? Math.round((synced / eligible) * 100) : 0,
  };
}

export function activityBars(days, { compact = false } = {}) {
  const values = Array.from({ length: ACTIVITY_DAYS }, (_, index) => {
    const value = (days || [])[index];
    return Number.isFinite(Number(value)) ? Number(value) : 0;
  });
  const max = Math.max(1, ...values);
  const total = values.reduce((sum, value) => sum + value, 0);
  const activeDays = values.filter((value) => value > 0).length;
  const today = new Date();
  const dates = values.map((_, index) => {
    const value = new Date(today);
    value.setDate(today.getDate() - (ACTIVITY_DAYS - 1 - index));
    return value;
  });
  return `<div class="activity-chart ${compact ? 'compact' : ''}" role="img" aria-label="Commits per day for the last ${ACTIVITY_DAYS} days: ${total} commits across ${activeDays} active day${activeDays === 1 ? '' : 's'}.">
    <div class="activity-bars">
      ${values.map((value, index) => {
        const date = dates[index];
        const height = value ? Math.max(4, Math.round((value / max) * 100)) : 0;
        const dayLabel = date.toLocaleDateString([], { day: 'numeric' });
        const fullLabel = date.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
        return `<div class="activity-col ${index === values.length - 1 ? 'today' : ''}" title="${esc(`${value} commit${value === 1 ? '' : 's'} · ${fullLabel}`)}">
          <div class="activity-track"><span class="activity-bar" style="height:${height}%"></span></div>
          <span class="activity-day">${esc(dayLabel)}</span>
        </div>`;
      }).join('')}
    </div>
    <div class="activity-foot"><span>${total} commit${total === 1 ? '' : 's'}</span><span>${activeDays} active day${activeDays === 1 ? '' : 's'}</span></div>
  </div>`;
}

export function activitySummaryHtml(summary, repos) {
  const commits30 = Number(summary?.commits_30d || 0);
  const commits7 = Number(summary?.commits_7d || 0);
  const active = Number(summary?.active_repos || 0);
  return `<div class="pulse-glance">
    <div class="pulse-glance-item"><span class="pulse-glance-value mono">${commits30}</span><span class="pulse-glance-label">commits · 30 days</span><span class="pulse-glance-sub">${commits7} in the last 7 days</span></div>
    <div class="pulse-glance-item"><span class="pulse-glance-value mono">${active}<small>/${(repos || []).length}</small></span><span class="pulse-glance-label">active repositories</span><span class="pulse-glance-sub">committed in the last 30 days</span></div>
    <div class="pulse-glance-item"><span class="pulse-glance-value mono">${Number(summary?.quiet_repos || 0)}</span><span class="pulse-glance-label">quiet repositories</span><span class="pulse-glance-sub">no commits or edits in 90 days</span></div>
  </div>`;
}

export function commitListHtml(repos, events = [], limit = 8) {
  const commits = recentCommits(repos, limit);
  const repoEvents = (events || []).filter((event) => event.kind === 'repo').slice(0, 4);
  if (!commits.length && !repoEvents.length) {
    return `<div class="card"><div class="card-body attention-empty"><span class="status-dot"></span><span class="muted">No commits or repo activity yet.</span></div></div>`;
  }
  return `<div class="card"><ul class="commit-list">
    ${commits.map(({ repo, commit }) => {
      const href = commitHref(repo);
      const name = href
        ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(repo.rel)}</a>`
        : `<a href="#/repos" data-repo-focus="${esc(repo.rel)}">${esc(repo.rel)}</a>`;
      return `<li class="commit-item">
        <span class="commit-icon">${icon('commit', 13)}</span>
        <div class="commit-main">
          <div class="commit-subject" title="${esc(commit.subject || '')}">${esc(commit.subject || 'no subject')}</div>
          <div class="commit-meta">${name}<span class="sep">·</span><span class="mono">${esc(commit.sha)}</span><span class="sep">·</span>${esc(ui.ago(commit.at))}</div>
        </div>
        ${href ? `<a class="btn ghost sm icon" href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="Open commit on GitHub" aria-label="Open ${esc(repo.rel)} commit ${esc(commit.sha)} on GitHub">${icon('external', 12)}</a>` : ''}
      </li>`;
    }).join('')}
    ${repoEvents.map((event) => `<li class="commit-item repo-event-line">
      <span class="commit-icon event">${icon('activity', 13)}</span>
      <div class="commit-main"><div class="commit-subject">${esc(event.message)}</div><div class="commit-meta"><span class="mono">${esc(event.status || 'info')}</span><span class="sep">·</span>${esc(ui.ago(event.ts))}</div></div>
    </li>`).join('')}
  </ul></div>`;
}

export function quietReposHtml(repos, limit = 5) {
  const quiet = quietRepos(repos).slice(0, limit);
  if (!quiet.length) {
    return `<div class="card"><div class="card-body attention-empty"><span class="status-dot ok"></span><span class="muted">Every repository has a commit or working-tree edit in the last 90 days.</span></div></div>`;
  }
  return `<div class="card"><ul class="quiet-list">
    ${quiet.map((repo) => `<li class="quiet-item">
      <span class="quiet-dot"></span>
      <div class="quiet-main"><a href="#/repos" data-repo-focus="${esc(repo.rel)}">${esc(repo.rel)}</a><span class="quiet-meta">${esc(repo.branch || 'no branch')} · ${repo.last_touch_at || repo.last_commit?.at ? `last touched ${esc(ui.ago(repo.last_touch_at || repo.last_commit.at))}` : 'no working-tree activity'}</span></div>
      <span class="quiet-age mono">${repo.last_touch_at || repo.last_commit?.at ? esc(formatAgeDays(lastTouchAge(repo))) : '—'}</span>
    </li>`).join('')}
  </ul></div>`;
}
