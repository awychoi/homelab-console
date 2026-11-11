import * as ui from '../ui.js';
import { pageHead, stateBanner, emptyState, hostLabel, stackHref } from './common.js';
import { filterNoticeHtml } from '../notices.js';

const { esc, icon } = ui;

function tabState(ctx) {
  if (!ctx.uiState.activityTab) ctx.uiState.activityTab = 'events';
  return ctx.uiState.activityTab;
}

function cached(ctx) {
  return ctx.uiState.activity?.data || null;
}

function eventIcon(event) {
  if (event.kind === 'health') return event.status === 'error' ? 'alert' : 'activity';
  if (event.kind === 'update' || event.kind === 'release') return 'updates';
  if (event.kind === 'deploy') return 'upload';
  if (event.kind === 'git' || event.kind === 'repo') return 'git';
  if (event.kind === 'policy' || event.kind === 'settings') return 'settings';
  if (event.kind === 'rollback') return 'restart';
  return 'activity';
}

function eventTone(event) {
  if (event.status === 'error' || event.status === 'failed') return 'bad';
  if (event.status === 'warning' || event.status === 'warn') return 'warn';
  if (event.status === 'success' || event.status === 'ok') return 'ok';
  return 'info';
}

function eventsHtml(ctx, data) {
  const events = data?.events || [];
  if (!events.length) {
    return emptyState('activity', 'No events yet', 'Health transitions, releases, deploys, updates and commits show up here as they happen.');
  }
  return `<div class="card"><div class="table-wrap"><table class="data"><tbody>
    ${events.map((event) => `<tr>
      <td class="event-icon-cell"><span class="stat-icon ${eventTone(event)} event-icon">${icon(eventIcon(event), 13)}</span></td>
      <td class="main-cell">
        <span class="event-message">${esc(event.message)}</span>
        <span class="sub">${event.host ? `${hostLabel(event.host)}${event.svc ? ` · <a href="${stackHref(event.host, event.svc)}">${esc(event.svc)}</a>` : ''}` : 'console'}</span>
      </td>
      <td class="muted tiny nowrap right">${esc(ui.ago(event.ts))}<span class="sub">${esc(ui.dateTime(event.ts))}</span></td>
      <td class="actions">${event.run_id ? `<button class="btn ghost sm" data-run="${esc(event.run_id)}">${icon('terminal', 12)} Log</button>` : ''}</td>
    </tr>`).join('')}
  </tbody></table></div></div>`;
}

function runsHtml(ctx, data) {
  const runs = data?.runs || ctx.state?.runs || [];
  if (!runs.length) {
    return emptyState('terminal', 'No runs yet', 'Deploys, lifecycle actions, updates and rollbacks are recorded here with their full logs.');
  }
  return `<div class="card"><div class="table-wrap"><table class="data">
    <thead><tr><th>When</th><th>Kind</th><th>Target</th><th>Status</th><th>Summary</th><th class="actions"></th></tr></thead>
    <tbody>${runs.map((run) => `<tr>
      <td class="mono tiny nowrap">${esc(ui.dateTime(run.started))}</td>
      <td>${ui.pill(run.kind || 'run', run.kind || 'run')}</td>
      <td>${run.host ? hostLabel(run.host) : '<span class="muted">console</span>'}${run.svc ? ` / <a href="${stackHref(run.host, run.svc)}">${esc(run.svc)}</a>` : ''}</td>
      <td>${ui.pill(run.status)}</td>
      <td class="muted tiny run-summary">${esc((run.summary || run.error || '').slice(0, 180))}</td>
      <td class="actions"><button class="btn ghost sm" data-run="${esc(run.id)}">${icon('terminal', 12)} Log</button></td>
    </tr>`).join('')}</tbody>
  </table></div></div>`;
}

function contentHtml(ctx) {
  const data = cached(ctx);
  const tab = tabState(ctx);
  const events = data?.events || ctx.state?.activity || [];
  const runs = data?.runs || ctx.state?.runs || [];
  const active = data?.active || [];
  return `${pageHead({
    title: 'Activity',
    subtitle: `${ui.plural(events.length, 'event')}<span class="dot">·</span>${ui.plural(runs.length, 'run')}<span class="dot">·</span>${active.length} active`,
    actions: `<button class="btn ghost sm" data-action="refresh-activity">${icon('refresh', 12)} Refresh</button>`,
  })}
  ${stateBanner(ctx)}
  <div class="tabs" role="tablist" aria-label="Activity sections">
    <button class="tab ${tab === 'events' ? 'active' : ''}" id="activity-tab-events" role="tab" aria-selected="${tab === 'events'}" aria-controls="activity-panel" tabindex="${tab === 'events' ? '0' : '-1'}" data-activity-tab="events">Events <span class="count">${events.length}</span></button>
    <button class="tab ${tab === 'runs' ? 'active' : ''}" id="activity-tab-runs" role="tab" aria-selected="${tab === 'runs'}" aria-controls="activity-panel" tabindex="${tab === 'runs' ? '0' : '-1'}" data-activity-tab="runs">Runs <span class="count">${runs.length}</span></button>
  </div>
  <div id="activity-panel" role="tabpanel" aria-labelledby="activity-tab-${tab}">
    ${tab === 'events' ? eventsHtml(ctx, data) : runsHtml(ctx, data)}
  </div>`;
}

async function openStoredRun(ctx, runId) {
  try {
    const run = await ctx.api.get(`/api/runs/${encodeURIComponent(runId)}`);
    if (run.status === 'running') {
      ui.runModal(ctx, runId, `${run.kind || 'run'} · ${run.host || ''}`);
      return;
    }
    const ref = ui.openModal({
      title: `${run.kind || 'run'} ${run.host ? `· ${run.host}` : ''}${run.svc ? `/${run.svc}` : ''}`,
      subtitle: `${run.started_iso || ui.dateTime(run.started)} · ${run.status}`,
      className: 'wide',
      body: `<div class="run-status">${ui.pill(run.status)}${run.error ? `<span class="muted">${esc(run.error)}</span>` : ''}</div>
        <div class="log">${(run.lines || []).map((line) => `<span class="ln"><span class="ts">${esc(ui.clock(line.ts))}</span>${esc(line.text)}</span>`).join('') || '<span class="log-empty">No log retained.</span>'}</div>`,
      footer: `<span class="note">Historical run — output read from .hlc/runs.</span><div class="spacer"></div><button class="btn ghost" data-close>Close</button>`,
    });
    ref.footer.querySelector('[data-close]').addEventListener('click', ref.close);
  } catch (error) {
    ui.toast('Could not load run', 'err', ui.errorText(error));
  }
}

export default {
  render(ctx) {
    return `<div id="activity-page">${contentHtml(ctx)}</div>`;
  },
  mount(root, ctx) {
    const page = root.querySelector('#activity-page');
    if (!page) return;

    const paint = () => {
      page.innerHTML = filterNoticeHtml(contentHtml(ctx));
      attach();
    };

    const load = async () => {
      try {
        const data = await ctx.api.get('/api/activity?limit=200');
        if (!ctx.uiState.activity) ctx.uiState.activity = {};
        ctx.uiState.activity.data = data;
        ctx.uiState.activity.fetchedAt = Date.now();
        if (document.getElementById('activity-page') === page) paint();
      } catch (error) {
        ui.toast('Could not load activity', 'err', ui.errorText(error));
      }
    };

    const activateTab = (name, { focus = false } = {}) => {
      if (ctx.uiState.activityTab === name) return;
      ctx.uiState.activityTab = name;
      paint();
      if (focus) page.querySelector(`[data-activity-tab="${name}"]`)?.focus();
    };

    const attach = () => {
      const tabs = [...page.querySelectorAll('[data-activity-tab]')];
      tabs.forEach((button, index) => {
        button.addEventListener('click', () => activateTab(button.dataset.activityTab));
        button.addEventListener('keydown', (event) => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[tabs.length - 1]
            : tabs[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
          activateTab(next.dataset.activityTab, { focus: true });
        });
      });
      page.querySelectorAll('[data-run]').forEach((button) => {
        button.addEventListener('click', () => {
          const runId = button.dataset.run;
          const inMemory = (ctx.state?.runs || []).find((run) => run.id === runId);
          if (inMemory && inMemory.status === 'running') ui.runModal(ctx, runId, `${inMemory.kind || 'run'} · ${inMemory.host || ''}`);
          else openStoredRun(ctx, runId);
        });
      });
      page.querySelector('[data-action="refresh-activity"]')?.addEventListener('click', load);
    };

    attach();
    const fetchedAt = ctx.uiState.activity?.fetchedAt || 0;
    if (Date.now() - fetchedAt > 8000) load();
  },
};
