import * as ui from '../ui.js';
import { pageHead, statTile, stateBanner, safetyBadge, stackHref, hostLabel } from './common.js';
import { showUpdate, showBulkUpdate } from '../flows.js';

const { esc, icon } = ui;

function initFilter(ctx) {
  if (!ctx.uiState.updatesFilter) ctx.uiState.updatesFilter = { host: 'all' };
  return ctx.uiState.updatesFilter;
}

function detailsOpen(ctx) {
  if (!ctx.uiState.updatesDetails || ctx.uiState.updatesDetails.epoch !== ctx.viewEpoch) {
    ctx.uiState.updatesDetails = { epoch: ctx.viewEpoch, open: false };
  }
  return ctx.uiState.updatesDetails.open;
}

function policyFor(ctx, host, stack) {
  return (ctx.state?.policies || []).find((policy) => policy.host === host && policy.svc === stack) || null;
}

function policySelect(ctx, target) {
  const policy = policyFor(ctx, target.host, target.stack);
  const mode = policy?.mode || 'off';
  return `<select class="policy-select" data-policy data-host="${esc(target.host)}" data-stack="${esc(target.stack)}" aria-label="Update policy for ${esc(target.host)}/${esc(target.stack)}" title="Update policy">
    <option value="off" ${mode === 'off' ? 'selected' : ''}>off</option>
    <option value="notify" ${mode === 'notify' ? 'selected' : ''}>notify</option>
    <option value="auto" ${mode === 'auto' ? 'selected' : ''}>auto</option>
  </select>`;
}

function actionButtons(target) {
  const actions = target.actions || [];
  if (!actions.length) return '';
  const primary = actions[0];
  const label = (target.action_labels || {})[primary] || primary;
  const extra = actions.slice(1);
  return `<div class="btn-group">
    <button class="btn ${primary === 'update' ? 'primary' : ''} sm" data-update-target="${esc(target.host)}/${esc(target.stack)}/${esc(target.service)}" data-action-name="${esc(primary)}">${esc(label)}</button>
    ${extra.map((action) => `<button class="btn ghost sm" data-update-target="${esc(target.host)}/${esc(target.stack)}/${esc(target.service)}" data-action-name="${esc(action)}">${esc((target.action_labels || {})[action] || action)}</button>`).join('')}
  </div>`;
}

function latestNote(target) {
  if (!target.platform_limited || !target.upstream_latest || target.latest === target.upstream_latest) return '';
  return ` <span class="sub text-warn">${esc(target.upstream_latest)} has no ${esc(target.platform || '')} build</span>`;
}

function rowHtml(ctx, target) {
  const policy = policyFor(ctx, target.host, target.stack);
  const sourceLabel = target.source?.label || target.source?.kind || '—';
  return `<tr>
    <td class="main-cell"><a class="stack-title" href="${stackHref(target.host, target.stack)}">${esc(target.stack)}</a><span class="sub">${hostLabel(target.host)} · ${esc(target.service)}</span></td>
    <td class="mono">${esc(target.current_display || target.current || '(none)')}${target.running_tag && target.running_tag !== target.current ? `<span class="sub">running ${esc(target.running_tag)}</span>` : ''}</td>
    <td class="mono">${target.latest ? `<a href="${esc(target.url || '#')}" target="_blank" rel="noopener">${esc(target.latest)} ${icon('external', 10)}</a>` : '<span class="dim">—</span>'}${target.latest ? `<span class="sub" ${target.latest_published ? `title="${esc(ui.dateTime(target.latest_published))}"` : ''}>${target.latest_published ? `released ${esc(ui.ago(target.latest_published))}` : 'release age unavailable'}</span>` : ''}${latestNote(target)}</td>
    <td>${esc(sourceLabel)}${target.error ? `<span class="sub text-warn">${esc(target.error.slice(0, 80))}</span>` : ''}</td>
    <td>${safetyBadge(target.safety?.level)}</td>
    <td>${policySelect(ctx, target)}${policy?.last_applied_version ? `<span class="sub">last ${esc(policy.last_applied_version)}</span>` : ''}</td>
    <td class="actions">${actionButtons(target)}</td>
  </tr>`;
}

function contentHtml(ctx) {
  const filter = initFilter(ctx);
  const state = ctx.state;
  const targets = state?.updates || [];
  const release = state?.release_check || {};
  const summary = state?.summary || {};
  const autoEnabled = state?.auto_update_enabled;
  const autoPolicies = (state?.policies || []).filter((policy) => policy.mode === 'auto');
  const managedOpen = detailsOpen(ctx);
  const actionable = targets.filter((target) => target.actions?.length);
  const filters = [
    ['all', 'All hosts'],
    ...(ctx.bootstrap?.hosts || []).filter((host) => !host.local || targets.some((target) => target.host === host.alias)).map((host) => [host.alias, host.alias]),
  ];
  const visible = actionable.filter((target) => filter.host === 'all' || target.host === filter.host);
  const upToDate = targets.filter((target) => !target.actions?.length && !['check-failed'].includes(target.status));
  const failed = targets.filter((target) => target.status === 'check-failed');
  const platformLimited = targets.filter((target) => target.platform_limited && (filter.host === 'all' || target.host === filter.host));
  const platformSummary = platformLimited
    .slice(0, 4)
    .map((target) => `${esc(target.host)}/${esc(target.stack)} <span class="mono">${esc(target.upstream_latest)}</span> (${esc(target.platform || 'unknown')})`)
    .join(' · ');
  const unmanagedKeys = new Set(
    targets
      .filter((target) => target.status === 'unmanaged' || target.source?.kind === 'unknown')
      .map((target) => target.source?.key || target.image),
  );
  const unmanagedCount = unmanagedKeys.size;
  return `${pageHead({
    title: 'Updates',
    subtitle: `Checked ${release.checked_iso ? esc(ui.ago(release.checked_at)) : 'not yet'}<span class="dot">·</span>${ui.plural(release.sources || 0, 'source')}<span class="dot">·</span>${release.errors ? `<span class="text-warn">${ui.plural(release.errors, 'error')}</span>` : 'no errors'}`,
    actions: `<button class="btn ghost sm" type="button" data-manage-sources>${icon('settings', 12)} Update sources</button><button class="btn primary sm" data-action="bulk-update">${icon('updates', 12)} Update all…</button>`,
    badges: summary.updates_available ? ui.pill('warn', `${summary.updates_available} available`) : ui.pill('ok', 'up to date'),
  })}
  ${stateBanner(ctx)}
  ${unmanagedCount ? `<div class="warn-box mb-2">${icon('alert')}<div><strong>${unmanagedCount === 1 ? '1 image needs' : `${unmanagedCount} images need`} an update source.</strong> The console cannot check releases for ${unmanagedCount === 1 ? 'this image' : 'these images'} until a source is added.</div><button class="btn sm" type="button" data-manage-sources>Manage update sources</button></div>` : ''}
  ${autoPolicies.length && !autoEnabled ? `<div class="warn-box mb-2">${icon('alert')}<div><strong>${ui.plural(autoPolicies.length, 'stack')} ${autoPolicies.length === 1 ? 'has' : 'have'} an auto policy, but automatic updates are disabled.</strong> Enable it in Settings, or leave the policies as “notify”.</div><a class="btn sm" href="#/settings">Settings</a></div>` : ''}
  ${platformLimited.length ? `<div class="warn-box mb-2">${icon('alert')}<div><strong>Newer tags are missing a build for the host CPU.</strong> ${platformSummary}${platformLimited.length > 4 ? ` · and ${platformLimited.length - 4} more` : ''}. They stay visible, but the target remains on the newest compatible tag.</div></div>` : ''}
  <div class="grid cols-4">
    ${statTile({ label: 'Available updates', value: String(summary.updates_available || 0), sub: 'stable upstream releases', iconName: 'updates', toneName: summary.updates_available ? 'warn' : 'ok', valueTone: summary.updates_available ? 'text-warn' : '' })}
    ${statTile({ label: 'Unpinned images', value: String(summary.unpinned || 0), sub: 'no immutable tag or digest', iconName: 'alert', toneName: summary.unpinned ? 'warn' : 'ok', valueTone: summary.unpinned ? 'text-warn' : '' })}
    ${statTile({ label: 'Compatibility-managed', value: String(targets.filter((target) => target.status === 'managed').length), sub: 'never updated independently', iconName: 'lock' })}
    ${statTile({ label: 'Auto policies', value: String(autoPolicies.length), sub: autoEnabled ? 'sweeper enabled' : 'sweeper disabled', iconName: 'refresh', toneName: autoEnabled ? 'info' : '' })}
  </div>

  <div class="toolbar mt-3">
    <div class="segmented" data-host-filter aria-label="Filter updates by host">
      ${filters.map(([value, label]) => `<button type="button" class="${filter.host === value ? 'active' : ''}" data-host="${esc(value)}" aria-pressed="${filter.host === value}">${esc(label)}</button>`).join('')}
    </div>
  </div>

  <div class="card">
    <div class="table-wrap"><table class="data">
      <thead><tr><th>Stack / service</th><th>Current</th><th>Latest</th><th>Source</th><th>Safety</th><th>Policy</th><th class="actions">Action</th></tr></thead>
      <tbody>
        ${visible.length ? visible.map((target) => rowHtml(ctx, target)).join('') : `<tr><td colspan="7"><div class="empty empty-inline">
          ${summary.updates_available && filter.host !== 'all' ? `<strong>No updates match this host.</strong><p>Switch back to all hosts to see every pending update.</p><button class="btn mt-1" data-reset-updates-filter>Show all hosts</button>` : '<strong>Everything is up to date.</strong><p>Checks run every six hours and on demand. Compatibility-managed images and custom critical stacks are reported separately. If an image is unmanaged, add an update source in Settings.</p>'}
          </div></td></tr>`}
      </tbody>
    </table></div>
  </div>

  ${failed.length ? `<div class="section"><div class="section-head"><div class="section-title">Release checks needing attention</div></div>
    <div class="card"><div class="table-wrap"><table class="data"><tbody>${failed.map((target) => `<tr>
      <td>${hostLabel(target.host)} / ${esc(target.stack)} <span class="mono">${esc(target.service)}</span></td>
      <td class="muted tiny">${esc(target.error || 'no update source')}</td>
      <td class="actions"><a class="btn ghost sm" href="${stackHref(target.host, target.stack)}">Open stack</a></td>
    </tr>`).join('')}</tbody></table></div></div></div>` : ''}

  <div class="section">
    <div class="section-head"><div class="section-title">Policy notes</div><div class="section-sub">what the modes mean</div></div>
    <div class="card"><div class="card-body"><div class="grid cols-3">
      <div><div class="mini-label">off</div><p class="policy-note">Only manual updates from this page. Release detection still runs.</p></div>
      <div><div class="mini-label">notify</div><p class="policy-note">Surface a release in Updates and Activity. Never writes on its own.</p></div>
      <div><div class="mini-label">auto</div><p class="policy-note">If automatic updates are enabled, the console updates this stack when upstream moves. Critical stacks are always excluded.</p></div>
    </div></div></div>
  </div>

  <details class="section disclosure" data-updates-details ${managedOpen ? 'open' : ''}>
    <summary class="section-title">Up-to-date and managed targets <span class="dim">(${upToDate.length})</span></summary>
    <div class="card mt-1"><div class="table-wrap"><table class="data">
      <thead><tr><th>Stack / service</th><th>Current</th><th>Latest</th><th>Status</th><th>Source</th></tr></thead>
      <tbody>${upToDate.map((target) => `<tr>
        <td>${hostLabel(target.host)} / <a class="stack-title" href="${stackHref(target.host, target.stack)}">${esc(target.stack)}</a> <span class="mono tiny">${esc(target.service)}</span></td>
        <td class="mono">${esc(target.current_display || target.current || '—')}</td>
        <td class="mono">${esc(target.latest || '—')}${latestNote(target)}</td>
        <td>${ui.pill(target.status, target.status === 'managed' ? `managed by ${target.managed_by || 'parent stack'}` : target.status)}</td>
        <td class="muted tiny">${esc(target.source?.label || target.source?.kind || '—')}</td>
      </tr>`).join('') || '<tr><td colspan="5" class="table-empty">Nothing here yet.</td></tr>'}</tbody>
    </table></div></div>
  </details>`;
}

export default {
  render(ctx) {
    if (!ctx.state) return '';
    return contentHtml(ctx);
  },
  mount(root, ctx) {
    const filter = initFilter(ctx);
    root.querySelector('[data-action="bulk-update"]')?.addEventListener('click', () => showBulkUpdate(ctx));
    root.querySelectorAll('[data-manage-sources]').forEach((button) => {
      button.addEventListener('click', () => {
        ctx.uiState.settingsScrollTo = 'detected-images';
        ctx.navigate('#/settings');
      });
    });
    root.querySelectorAll('[data-host-filter] button').forEach((button) => {
      button.addEventListener('click', () => {
        filter.host = button.dataset.host;
        ctx.rerender?.();
        root.querySelector(`[data-host-filter] [data-host="${CSS.escape(filter.host)}"]`)?.focus();
      });
    });
    root.querySelector('[data-reset-updates-filter]')?.addEventListener('click', () => {
      filter.host = 'all';
      ctx.rerender?.();
      root.querySelector('[data-host-filter] [data-host="all"]')?.focus();
    });
    const details = root.querySelector('[data-updates-details]');
    if (details) {
      details.addEventListener('toggle', () => {
        ctx.uiState.updatesDetails = { epoch: ctx.viewEpoch, open: details.open };
      });
    }
    root.querySelectorAll('[data-policy]').forEach((select) => {
      select.dataset.previous = select.value;
      select.addEventListener('change', async () => {
        const previous = select.dataset.previous || 'off';
        const host = select.dataset.host;
        const stack = select.dataset.stack;
        try {
          await ctx.api.post('/api/policies', { host, stack, mode: select.value });
          select.dataset.previous = select.value;
          await ctx.refreshState({ force: true });
          if (select.value === 'auto' && !ctx.state?.auto_update_enabled) {
            ui.toast('Auto policy saved', 'warn', 'Automatic updates are still disabled in Settings.');
          } else {
            ui.toast('Policy updated', 'ok', `${host}/${stack}: ${select.value}`);
          }
        } catch (error) {
          select.value = previous;
          ui.toast('Policy update failed', 'err', ui.errorText(error));
        }
      });
    });
    root.querySelectorAll('[data-update-target]').forEach((button) => {
      button.addEventListener('click', () => {
        const [host, stack, service] = button.dataset.updateTarget.split('/');
        const target = (ctx.state?.updates || []).find((item) => item.host === host && item.stack === stack && item.service === service);
        if (!target) {
          ui.toast('Target changed', 'warn', 'Re-check releases and try again.');
          return;
        }
        showUpdate(ctx, target, button.dataset.actionName);
      });
    });
  },
};
