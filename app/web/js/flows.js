import { api, ApiError } from './api.js';
import * as ui from './ui.js';

const { esc, icon, toast, openModal, confirmDialog } = ui;

function busy(button, label = 'Working…') {
  if (!button) return () => {};
  const original = button.innerHTML;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  button.innerHTML = `<span class="spinner sm"></span> ${esc(label)}`;
  return () => {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    button.innerHTML = original;
  };
}

function field(label, id, value = '', options = {}) {
  const { placeholder = '', type = 'text', hint = '', required = false, autocomplete = '' } = options;
  return `<div class="field"><label for="${esc(id)}">${esc(label)}${required ? ' *' : ''}</label>
    <input id="${esc(id)}" type="${esc(type)}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="${esc(autocomplete)}">
    ${hint ? `<div class="hint">${esc(hint)}</div>` : ''}</div>`;
}

function errorHtml(message) {
  return `<div class="danger-box">${icon('alert')}<div>${esc(message)}</div></div>`;
}

// Repo actions kick off a one-repo Gitea check in the background. Re-read the
// snapshot shortly after so the row moves off "not checked" without waiting
// for the normal poll; the 12s poll is still the backstop.
export function refreshRemoteSoon(ctx) {
  for (const delay of [1200, 4000]) {
    setTimeout(() => { ctx.refreshState({ force: true }); }, delay);
  }
}

function diffHtml(changes) {
  if (!changes?.length) {
    return `<div class="diff"><div class="diff-empty">No file changes — the repo already matches the host.</div></div>`;
  }
  return `<div class="diff">${changes.map((change) => {
    const kindPill = change.kind === 'added'
      ? ui.pill('ok', 'new file')
      : change.kind === 'changed'
        ? ui.pill('warn', 'changed')
        : ui.pill('neutral', 'extra on host');
    const lines = (change.diff || []).map((line) => {
      let cls = '';
      let marker = ' ';
      let display = line;
      if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-meta';
      else if (line.startsWith('+')) { cls = 'diff-add'; marker = '+'; display = line.slice(1); }
      else if (line.startsWith('-')) { cls = 'diff-del'; marker = '-'; display = line.slice(1); }
      else if (line.startsWith('@@')) cls = 'diff-hunk';
      return `<div class="diff-line ${cls}"><span class="marker">${marker}</span><span>${esc(display.slice(0, 400))}</span></div>`;
    }).join('');
    const note = change.kind === 'remote-only'
      ? `<div class="diff-line text-dim"><span class="marker"></span><span>This file exists only on the host; a push will leave it untouched. Investigate if it looks managed.</span></div>`
      : '';
    return `<div class="diff-file">
      <div class="diff-head">${kindPill}<span class="path mono">${esc(change.path)}</span></div>
      <div class="diff-body">${note}${lines || '<div class="diff-empty">no inline diff</div>'}</div>
    </div>`;
  }).join('')}</div>`;
}

export function renderPlan(plan) {
  const changes = plan.changes || [];
  const counts = {
    added: changes.filter((change) => change.kind === 'added').length,
    changed: changes.filter((change) => change.kind === 'changed').length,
    extra: changes.filter((change) => change.kind === 'remote-only').length,
  };
  return `<div class="chips mb-2">
      ${counts.added ? `<span class="chip good">${counts.added} new</span>` : ''}
      ${counts.changed ? `<span class="chip warn">${counts.changed} changed</span>` : ''}
      ${counts.extra ? `<span class="chip">${counts.extra} extra on host</span>` : ''}
      ${!changes.length ? '<span class="chip good">in sync</span>' : ''}
    </div>
    ${diffHtml(changes)}`;
}

/* ------------------------------------------------------------- check updates */
export async function showCheckUpdates(ctx) {
  if (ctx.uiState.checking) return;
  ctx.uiState.checking = true;
  const button = document.querySelector('[data-action="check-updates"]');
  const restore = busy(button, 'Checking…');
  toast('Checking upstream releases', 'ok', 'GitHub and Docker Hub, cached for six hours.');
  try {
    await api.post('/api/updates/check', {});
    const deadline = Date.now() + 90_000;
    let status = null;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1800));
      status = await api.get('/api/updates');
      if (!status.release_check?.checking) break;
    }
    // The release worker finishes just before the monitor's next collection;
    // give that collection a moment so the view isn't showing pre-check state.
    await new Promise((resolve) => setTimeout(resolve, 1800));
    const fresh = await ctx.refreshState({ force: true });
    if (fresh) status = await api.get('/api/updates');
    const summary = fresh?.summary || status?.summary || {};
    const updates = summary.updates_available ?? summary.updates ?? 0;
    if (updates) {
      toast(`${updates} update${updates === 1 ? '' : 's'} available`, 'warn', 'Review the Updates view before deploying.');
    } else if (summary.unpinned) {
      toast(`${summary.unpinned} unpinned image${summary.unpinned === 1 ? '' : 's'}`, 'warn', 'Pin a version before relying on the deployment.');
    } else {
      toast('Releases checked — everything is current', 'ok');
    }
    if (status?.release_check?.last_error) {
      toast('Some release checks failed', 'warn', status.release_check.last_error);
    }
  } catch (error) {
    toast('Release check failed', 'err', ui.errorText(error));
  } finally {
    ctx.uiState.checking = false;
    restore();
  }
}

/* --------------------------------------------------------------- permissions */
export async function showDeploy(ctx, host, name, action = 'up') {
  const actionLabel = { up: 'Push & up', pull: 'Pull & recreate', sync: 'Push files only' }[action] || action;
  const ref = openModal({
    title: `Deploy ${host}/${name}`,
    subtitle: `Reviewing the repo → host diff · ${actionLabel}`,
    className: 'wide',
    body: `<div class="muted">Reading the stack from <span class="mono">${esc(host)}</span>…</div>`,
    footer: `<span class="note">Nothing is written until you confirm.</span><div class="spacer"></div><button class="btn ghost" data-cancel>Cancel</button>`,
  });
  ref.footer.querySelector('[data-cancel]').addEventListener('click', ref.close);
  let plan;
  try {
    plan = await api.get(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/plan`);
  } catch (error) {
    ref.body.innerHTML = errorHtml(ui.errorText(error));
    return;
  }
  const hasChanges = (plan.changes || []).length > 0;
  if (!hasChanges && action === 'sync') {
    ref.body.innerHTML = `<div class="success-box">${icon('check')}<div>This stack is already in sync with the host — there is nothing to push.</div></div>`;
    return;
  }
  ref.body.innerHTML = `
    ${plan.destructive ? `<div class="danger-box mb-2">${icon('alert')}<div><strong>This push overwrites files on the host.</strong> The diff below is the complete list; remote-only files are left untouched.</div></div>` : ''}
    ${!hasChanges ? `<div class="info-box mb-2">${icon('info')}<div>No file changes. The compose action will still run (recreate/restart as chosen).</div></div>` : ''}
    ${plan.remote_listing_complete === false ? `<div class="warn-box mb-2">${icon('alert')}<div><strong>Some host files could not be read.</strong> The plan cannot prove those paths are absent, so matching local files are treated as overwrites and require approval.</div></div>` : ''}
    ${renderPlan(plan)}
    ${plan.destructive ? `<label class="check mt-2"><input type="checkbox" id="reviewed"><span>I reviewed this diff and want to overwrite the host files.</span></label>` : ''}
  `;
  ref.footer.innerHTML = `<span class="note">${plan.checked_at ? `plan checked ${esc(ui.ago(plan.checked_at))}` : ''}</span><div class="spacer"></div>
    <button class="btn ghost" data-cancel>Cancel</button>
    <button class="btn primary" data-deploy ${plan.destructive ? 'disabled' : ''}>${esc(actionLabel)}</button>`;
  ref.footer.querySelector('[data-cancel]').addEventListener('click', ref.close);
  const checkbox = ref.body.querySelector('#reviewed');
  const deployButton = ref.footer.querySelector('[data-deploy]');
  if (checkbox) {
    checkbox.addEventListener('change', () => { deployButton.disabled = !checkbox.checked; });
  }
  deployButton.addEventListener('click', async () => {
    const restore = busy(deployButton, 'Starting…');
    try {
      const result = await api.post(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/deploy`, {
        action,
        plan_hash: plan.hash,
        approve: checkbox ? checkbox.checked : true,
      });
      ref.close();
      ui.runModal(ctx, result.run_id, `${host}/${name} · ${action}`);
    } catch (error) {
      restore();
      if (error instanceof ApiError && error.status === 409) {
        toast('The diff changed', 'warn', 'Review the updated plan and try again.');
        ref.close();
        setTimeout(() => showDeploy(ctx, host, name, action), 50);
      } else if (error instanceof ApiError && error.status === 428) {
        toast('Approval required', 'warn', error.message);
      } else {
        toast('Deploy failed to start', 'err', ui.errorText(error));
      }
    }
  });
}

/* ---------------------------------------------------------------- lifecycle */
export async function showLifecycle(ctx, host, name, action) {
  const labels = { restart: 'Restart containers', down: 'Take stack down', up: 'Compose up' };
  const danger = action === 'down';
  const confirmed = await confirmDialog({
    title: `${labels[action] || action} · ${host}/${name}`,
    message: danger
      ? `This stops every container in ${host}/${name}. It does not push files or remove images.`
      : `${labels[action] || action} for ${host}/${name}. No files are pushed; this acts on the running stack.`,
    confirmLabel: labels[action] || action,
    danger,
  });
  if (!confirmed) return;
  try {
    const result = await api.post(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/lifecycle`, { action });
    ui.runModal(ctx, result.run_id, `${host}/${name} · ${action}`);
  } catch (error) {
    toast('Could not start action', 'err', ui.errorText(error));
  }
}

export async function showRollback(ctx, host, name, snapshotId) {
  const confirmed = await confirmDialog({
    title: `Roll back ${host}/${name}`,
    message: 'This restores the stack files from the snapshot, pushes them to the host, and recreates the containers. Data volumes are not rolled back.',
    detail: `<span class="mono tiny">${esc(snapshotId)}</span>`,
    confirmLabel: 'Roll back',
    danger: true,
  });
  if (!confirmed) return;
  try {
    const result = await api.post(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/rollback`, { snapshot_id: snapshotId });
    ui.runModal(ctx, result.run_id, `rollback ${host}/${name}`);
  } catch (error) {
    toast('Rollback failed to start', 'err', ui.errorText(error));
  }
}

/* ------------------------------------------------------------ bulk update */
export function showBulkUpdate(ctx) {
  const actionable = (ctx.state?.updates || []).filter((target) => (target.actions || []).includes('update'));
  const critical = actionable.filter((target) => target.safety?.level === 'critical');
  const eligible = actionable.filter((target) => target.safety?.level !== 'critical');
  if (!eligible.length) {
    toast('No eligible updates', 'warn',
      critical.length ? 'Critical updates must be applied individually after a database dump.' : 'Run Check releases first; no stable updates are available.');
    return;
  }
  const hostOrder = (ctx.bootstrap?.hosts || []).map((host) => host.alias);
  const hosts = [...new Set(eligible.map((target) => target.host))]
    .sort((a, b) => (hostOrder.indexOf(a) + 1 || 99) - (hostOrder.indexOf(b) + 1 || 99));
  const countFor = (host) => eligible.filter((target) => target.host === host).length;
  const options = [
    `<option value="fleet">All Pis — ${eligible.length} update${eligible.length === 1 ? '' : 's'}</option>`,
    ...hosts.map((host) => `<option value="host:${esc(host)}">${esc(host)} — ${countFor(host)} update${countFor(host) === 1 ? '' : 's'}</option>`),
  ];
  const ref = openModal({
    title: 'Update all',
    subtitle: 'Queued one at a time; the next starts only after the previous update is verified',
    className: 'wide',
    body: `
      <div class="field"><label for="bulk-scope">Scope</label>
        <select id="bulk-scope">${options.join('')}</select>
      </div>
      <div id="bulk-preview"></div>
      ${critical.length ? `<div class="warn-box mt-2">${icon('alert')}<div><strong>Critical update${critical.length === 1 ? '' : 's'} excluded:</strong> ${critical.map((target) => `${esc(target.host)}/${esc(target.stack)}`).join(', ')}. Apply these individually after a database dump and acknowledgement.</div></div>` : ''}
      <div class="info-box mt-2">${icon('info')}<div>The queue runs serially and stops at the first failure; remaining targets stay queued as skipped. A critical stack is never included automatically.</div></div>
    `,
    footer: `<div class="spacer"></div><button class="btn ghost" data-cancel>Cancel</button><button class="btn primary" data-bulk-start>Queue updates</button>`,
  });
  const select = ref.body.querySelector('#bulk-scope');
  const preview = ref.body.querySelector('#bulk-preview');
  const start = ref.footer.querySelector('[data-bulk-start]');
  const selection = () => {
    const value = select.value;
    if (value === 'fleet') return { scope: 'fleet', host: '', targets: eligible };
    const host = value.slice('host:'.length);
    return { scope: 'host', host, targets: eligible.filter((target) => target.host === host) };
  };
  const renderPreview = () => {
    const { targets } = selection();
    start.disabled = !targets.length;
    start.textContent = `Queue ${targets.length} update${targets.length === 1 ? '' : 's'}`;
    if (!targets.length) {
      preview.innerHTML = `<div class="empty mini"><strong>No updates in this scope.</strong></div>`;
      return;
    }
    preview.innerHTML = `<div class="table-wrap mt-2"><table class="data">
      <thead><tr><th>Host / stack</th><th>Current</th><th>Target</th><th>Safety</th></tr></thead>
      <tbody>${targets.map((target) => `<tr>
        <td>${esc(target.host)} / <span class="mono">${esc(target.stack)}</span><span class="sub">${esc(target.service)}</span></td>
        <td class="mono">${esc(target.current_display || target.current || '—')}</td>
        <td class="mono">${esc(target.latest || '—')}</td>
        <td>${ui.safetyBadge(target.safety?.level)}</td>
      </tr>`).join('')}</tbody>
    </table></div>`;
  };
  select.addEventListener('change', renderPreview);
  ref.footer.querySelector('[data-cancel]').addEventListener('click', ref.close);
  start.addEventListener('click', async () => {
    const { scope, host, targets } = selection();
    const restore = busy(start, 'Queueing…');
    try {
      const result = await api.post('/api/updates/bulk', { scope, host });
      ref.close();
      toast(`Queued ${targets.length} update${targets.length === 1 ? '' : 's'}`, 'ok', 'Sequential queue started.');
      showBatchQueue(ctx, result.batch);
    } catch (error) {
      restore();
      toast('Could not queue updates', 'err', ui.errorText(error));
    }
  });
  renderPreview();
}

export function showBatchQueue(ctx, batch) {
  let timer = null;
  let stopped = false;
  const ref = openModal({
    title: 'Update queue',
    subtitle: 'Sequential updates · one verification at a time',
    className: 'wide',
    body: `<div id="batch-summary"></div><div id="batch-list" class="mt-2"></div>`,
    footer: `<div class="spacer"></div><button class="btn ghost" data-batch-close>Close</button>`,
    onClose: () => {
      if (timer) clearInterval(timer);
      timer = null;
      ctx.refreshState({ force: true });
    },
  });
  const summary = ref.body.querySelector('#batch-summary');
  const list = ref.body.querySelector('#batch-list');
  const render = (data) => {
    const items = data.items || [];
    const done = items.filter((item) => ['ok', 'failed', 'skipped'].includes(item.status)).length;
    const status = data.status || 'queued';
    const tone = status === 'ok' ? 'ok' : status === 'failed' ? 'bad' : 'info';
    const label = status === 'ok' ? 'completed' : status === 'failed' ? 'stopped' : status === 'running' ? 'running' : 'queued';
    summary.innerHTML = `<div class="toolbar"><div><strong>${data.scope === 'fleet' ? 'All Pis' : esc(data.host)}</strong>
      <span class="muted">${done}/${data.total} finished</span></div>${ui.pill(tone, label)}</div>
      ${data.excluded?.length ? `<div class="warn-box mt-2">${icon('alert')}<div><strong>Excluded:</strong> ${data.excluded.map((item) => `${esc(item.host)}/${esc(item.stack)}`).join(', ')} — ${esc(data.excluded[0].reason || 'applied individually')}</div></div>` : ''}
      ${data.error ? `<div class="danger-box mt-2">${icon('alert')}<div>${esc(data.error)}</div></div>` : ''}
      <div class="hint">${status === 'running' ? 'The current update must finish its health checks before the next one starts.' : status === 'ok' ? 'All queued updates completed and verified.' : 'The queue stops at the first failure; remaining targets are skipped.'}</div>`;
    list.innerHTML = `<div class="table-wrap"><table class="data">
      <thead><tr><th>Host / stack</th><th>Service</th><th>Current → target</th><th>Status</th></tr></thead>
      <tbody>${items.map((item) => `<tr>
        <td>${esc(item.host)} / <span class="mono">${esc(item.stack)}</span></td>
        <td class="mono">${esc(item.service)}</td>
        <td class="mono">${esc(item.current || '—')} → ${esc(item.latest || '—')}</td>
        <td>${ui.pill(item.status === 'ok' ? 'ok' : item.status === 'failed' ? 'bad' : item.status === 'running' ? 'info' : 'neutral', item.status === 'ok' ? 'updated' : item.status)}${item.error ? `<span class="sub text-warn">${esc(item.error)}</span>` : ''}</td>
      </tr>`).join('')}</tbody>
    </table></div>`;
    if (['ok', 'failed', 'cancelled'].includes(status) && !stopped) {
      stopped = true;
      if (timer) clearInterval(timer);
      timer = null;
      ctx.refreshState({ force: true });
    }
  };
  render(batch);
  const poll = async () => {
    if (stopped) return;
    try {
      const data = await api.get(`/api/updates/bulk/${encodeURIComponent(batch.id)}`);
      render(data.batch);
    } catch (error) {
      if (summary) summary.innerHTML = `<div class="danger-box">${icon('alert')}<div>${esc(ui.errorText(error))}</div></div>`;
      if (timer) clearInterval(timer);
      timer = null;
    }
  };
  if (!stopped) timer = setInterval(poll, 2000);
  ref.footer.querySelector('[data-batch-close]').addEventListener('click', ref.close);
}

/* ------------------------------------------------------------------- update */
export async function showUpdate(ctx, target, preferredAction = null) {
  const actions = target.actions || [];
  if (!actions.length) {
    toast('No action available', 'warn', 'Refresh the updates view; the release may have changed.');
    return;
  }
  const actionLabels = target.action_labels || { update: 'Update & deploy', 'pin-latest': 'Pin to latest', 'pin-digest': 'Pin to running digest' };
  const initial = preferredAction && actions.includes(preferredAction) ? preferredAction : actions[0];
  const critical = target.safety?.level === 'critical';
  const safety = target.safety || {};
  const notes = target.notes || [];
  const databaseBackup = Boolean(safety.database_backup);
  const databaseLabel = safety.database_label || 'Database';
  const backupsPath = `/api/stacks/${encodeURIComponent(target.host)}/${encodeURIComponent(target.stack)}/database-backups`;
  const backupPanel = databaseBackup ? `<div class="backup-box mb-2" id="db-backup-panel">
    <div class="backup-head">
      <div><strong>Database backups</strong>
        <div class="tiny" id="db-backup-scheduled">Checking scheduled ${esc(databaseLabel)} backups…</div>
        <div class="tiny" id="db-backup-latest">Checking console copies…</div>
      </div>
      <button class="btn sm" type="button" data-db-dump>${icon('download', 12)} Dump fresh copy on Pi</button>
    </div>
    <div class="hint" id="db-backup-status">The stack's own backup job (if any) writes to its configured backups directory. The button creates a fresh validated copy in the console folder on the host, ready for a local restore — no Mac round-trip. Database dumps are PostgreSQL only; data volumes are never included.</div>
  </div>` : '';
  const ref = openModal({
    title: `${target.host}/${target.stack} · ${target.service}`,
    subtitle: 'Pin the repo, push, pull only the affected services, verify health',
    className: 'wide',
    body: `
      <div class="grid cols-2 mb-2">
        <div class="kv">
          <div class="kv-row"><div class="kv-key">Current pin</div><div class="kv-val mono">${esc(target.current_display || target.current || '—')}</div></div>
          <div class="kv-row"><div class="kv-key">Latest release</div><div class="kv-val mono">${esc(target.latest || '—')}${target.latest_published ? `<span class="sub" title="${esc(ui.dateTime(target.latest_published))}">released ${esc(ui.ago(target.latest_published))}</span>` : ''}</div></div>
          <div class="kv-row"><div class="kv-key">Running image</div><div class="kv-val mono">${esc(target.running_tag || '—')}</div></div>
          <div class="kv-row"><div class="kv-key">Source</div><div class="kv-val">${target.url ? `<a href="${esc(target.url)}" target="_blank" rel="noopener">${esc(target.source?.label || target.source?.kind || 'upstream')} ${icon('external', 11)}</a>` : esc(target.source?.label || '—')}</div></div>
        </div>
        <div class="kv">
          <div class="kv-row"><div class="kv-key">Safety</div><div class="kv-val">${ui.safetyBadge(safety.level)}</div></div>
          <div class="kv-row"><div class="kv-key">Data</div><div class="kv-val">${esc((safety.data || []).join(', ') || 'no persistent state declared')}</div></div>
          <div class="kv-row"><div class="kv-key">Checked</div><div class="kv-val">${target.checked_at ? esc(ui.dateTime(target.checked_at)) : '—'}</div></div>
          <div class="kv-row"><div class="kv-key">Affects</div><div class="kv-val mono">${esc((target.services_affected || []).join(', '))}</div></div>
        </div>
      </div>
      ${critical ? `<div class="danger-box mb-2">${icon('alert')}<div><strong>Critical stack.</strong> ${esc(safety.backup || 'Take a data backup before updating.')}${databaseBackup ? ' Use the latest scheduled dump below; create a fresh copy on the Pi if it is stale.' : ' The console snapshots repo files only; it does not dump the database.'}</div></div>`
        : `<div class="warn-box mb-2">${icon('alert')}<div><strong>Before updating:</strong> ${esc(safety.backup || 'snapshot stateful data if relevant')}</div></div>`}
      ${backupPanel}
      ${notes.length ? `<ul class="note-list mb-2">${notes.map((note) => `<li>${esc(note)}</li>`).join('')}</ul>` : ''}
      <div class="field"><label for="update-action">Action</label>
        <select id="update-action">${actions.map((action) => `<option value="${esc(action)}" ${action === initial ? 'selected' : ''}>${esc(actionLabels[action] || action)}</option>`).join('')}</select>
      </div>
      <div id="update-preview" class="info-box"></div>
      ${critical ? `<label class="check mt-2"><input type="checkbox" id="ack"><span><strong>I confirm a recent backup exists</strong> (or I accept the risk) and want to update this critical stack.</span></label>` : ''}
      <div class="hint mt-1">The console edits the compose file in the repo, snapshots it, expands the change, then pushes and recreates. On a failed health check it automatically restores the previous compose files and re-ups.</div>
    `,
    footer: `<span class="note">Compose-only rollback is automatic.</span><div class="spacer"></div>
      <button class="btn ghost" data-cancel>Cancel</button>
      <button class="btn primary" data-apply ${critical ? 'disabled' : ''}>Update & deploy</button>`,
  });
  const select = ref.body.querySelector('#update-action');
  const preview = ref.body.querySelector('#update-preview');
  const ack = ref.body.querySelector('#ack');
  const applyButton = ref.footer.querySelector('[data-apply]');
  const dumpButton = ref.body.querySelector('[data-db-dump]');
  const backupScheduled = ref.body.querySelector('#db-backup-scheduled');
  const backupLatest = ref.body.querySelector('#db-backup-latest');
  const backupStatus = ref.body.querySelector('#db-backup-status');
  const showLatestBackup = (data) => {
    const scheduled = data?.scheduled;
    if (backupScheduled) {
      if (!scheduled) {
        backupScheduled.textContent = 'Scheduled backup check unavailable.';
      } else if (!scheduled.available) {
        backupScheduled.textContent = `Scheduled backup check unavailable: ${scheduled.error || 'host unreachable'}`;
      } else if (!scheduled.latest) {
        backupScheduled.textContent = `No scheduled ${databaseLabel} backup found on the host.`;
      } else {
        const latest = scheduled.latest;
        const stale = Math.max(0, Date.now() / 1000 - Number(latest.created)) > 36 * 3600;
        backupScheduled.innerHTML = `Scheduled (host): <span class="mono">${esc(latest.name)}</span> · ${esc(ui.bytes(latest.bytes))} · ${esc(ui.ago(latest.created))} · ${esc(ui.dateTime(latest.created))}${stale ? ' · <span class="text-warn">stale</span>' : ''}`;
        backupScheduled.title = `${scheduled.directory || ''}\n${ui.dateTime(latest.created)}`;
      }
    }
    const consoleCopy = data?.console;
    if (!backupLatest) return;
    if (!consoleCopy || !consoleCopy.available) {
      backupLatest.textContent = `Console copy (Pi): ${consoleCopy?.error || 'unavailable'}`;
      return;
    }
    const latest = consoleCopy.backups?.[0];
    if (!latest) {
      backupLatest.textContent = 'Console copy (Pi): none yet.';
      return;
    }
    backupLatest.innerHTML = `Console copy (Pi): <span class="mono">${esc(latest.name)}</span> · ${esc(ui.bytes(latest.bytes))} · ${esc(ui.ago(latest.created))} · ${esc(ui.dateTime(latest.created))}`;
    backupLatest.title = latest.path;
  };
  if (databaseBackup) {
    api.get(backupsPath).then(showLatestBackup).catch((error) => {
      if (backupLatest) backupLatest.textContent = `Could not list dumps: ${ui.errorText(error)}`;
    });
    dumpButton?.addEventListener('click', async () => {
      const restore = busy(dumpButton, 'Dumping…');
      if (backupStatus) backupStatus.textContent = 'Running pg_dumpall… this can take a minute or two.';
      try {
        const data = await api.post(backupsPath, {});
        showLatestBackup(data);
        if (backupStatus) {
          backupStatus.innerHTML = `${icon('check', 13)} Dump complete on the Pi: <span class="mono">${esc(data.backup?.path || '')}</span> · ${esc(ui.bytes(data.backup?.bytes || 0))}. You can acknowledge and update.`;
        }
        toast('Database dump complete', 'ok', data.backup?.path || '');
      } catch (error) {
        if (backupStatus) backupStatus.textContent = 'Dump failed — do not update without a separate backup.';
        toast('Database dump failed', 'err', ui.errorText(error));
      } finally {
        restore();
      }
    });
  }
  const renderPreview = () => {
    const action = select.value;
    const value = action === 'update' ? target.new_value : action === 'pin-latest' ? target.pin_latest : target.digest_value;
    const label = actionLabels[action] || action;
    preview.innerHTML = `${icon('info')}<div><strong>${esc(label)}</strong><br>
      <span class="mono">${esc(target.current_display || target.current || '(none)')}</span> → <span class="mono">${esc(value || '—')}</span>
      <div class="tiny">Writes to <span class="mono">${esc(target.file)}</span></div></div>`;
  };
  renderPreview();
  select.addEventListener('change', renderPreview);
  ack?.addEventListener('change', () => { applyButton.disabled = !ack.checked; });
  ref.footer.querySelector('[data-cancel]').addEventListener('click', ref.close);
  applyButton.addEventListener('click', async () => {
    const restore = busy(applyButton, 'Starting…');
    try {
      const result = await api.post('/api/updates/apply', {
        host: target.host,
        stack: target.stack,
        service: target.service,
        action: select.value,
        acknowledge: critical ? Boolean(ack?.checked) : true,
        review_hash: target.review_hashes?.[select.value] || '',
      });
      ref.close();
      ui.runModal(ctx, result.run_id, `${target.host}/${target.stack} update`);
    } catch (error) {
      restore();
      if (error instanceof ApiError && error.status === 428) {
        const needsReview = /review/i.test(error.message || '');
        toast(needsReview ? 'Review the target first' : 'Backup acknowledgement required', 'warn', error.message);
      } else if (error instanceof ApiError && error.status === 409) {
        toast('Update plan changed', 'warn', 'Refresh and try again.');
      } else {
        toast('Update failed to start', 'err', ui.errorText(error));
      }
    }
  });
}

/* ----------------------------------------------------------------- new stack */
export async function showNewStack(ctx) {
  const hosts = (ctx.bootstrap?.hosts || []).filter((host) => !host.local);
  if (!hosts.length) {
    toast('No deployable hosts', 'warn', 'Add a Pi first in Hosts.');
    return;
  }
  const templates = ctx.bootstrap?.templates || [];
  const remoteRoot = String(ctx.bootstrap?.settings?.remote_root || '/opt/stacks').replace(/\/+$/, '');
  const ref = openModal({
    title: 'New stack',
    subtitle: 'Create the repo copy first; deployment is a separate reviewable step',
    className: 'wide',
    body: `
      <div class="grid cols-2">
        <div>
          <div class="field"><label for="stack-host">Host</label><select id="stack-host"></select></div>
          <div class="field"><label for="stack-template">Template</label>
            <select id="stack-template">${templates.map((template) => `<option value="${esc(template.id)}">${esc(template.name)} — ${esc(template.description)}</option>`).join('')}</select>
          </div>
          ${field('Stack name', 'stack-name', '', { placeholder: 'e.g. uptime-kuma', hint: 'lowercase letters, numbers, dots and dashes' })}
          ${field('Public hostname (optional note)', 'stack-vhost', '', { placeholder: 'e.g. uptime.example.com', hint: 'Saved as a comment only; add the proxy or DNS route separately.' })}
        </div>
        <div>
          <div class="field"><label for="stack-compose">docker-compose.yml</label><textarea id="stack-compose" class="compose-input"></textarea></div>
          <div class="field"><label for="stack-env">.env <span class="opt">optional, gitignored</span></label><textarea id="stack-env" class="env-input"></textarea></div>
        </div>
      </div>
      <div class="info-box" id="stack-note">${icon('info')}<div><strong>A stack is a Docker Compose project</strong> and can contain one or more services. The compose file lands in <span class="mono">stacks/&lt;host&gt;/&lt;name&gt;/</span>. The app pushes it to <span class="mono">${esc(remoteRoot)}/&lt;name&gt;/</span> on deploy; it never edits the Pi directly.</div></div>
      <div id="stack-error"></div>
    `,
    footer: `<span class="note">.env files never commit</span><div class="spacer"></div>
      <button class="btn ghost" data-cancel>Cancel</button>
      <button class="btn primary" data-create>Create stack</button>`,
  });
  const hostSelect = ref.body.querySelector('#stack-host');
  hostSelect.innerHTML = hosts.map((host) => `<option value="${esc(host.alias)}">${esc(host.alias)} — ${esc(host.address)}</option>`).join('');
  const templateSelect = ref.body.querySelector('#stack-template');
  const nameInput = ref.body.querySelector('#stack-name');
  const composeArea = ref.body.querySelector('#stack-compose');
  const envArea = ref.body.querySelector('#stack-env');
  const errorBox = ref.body.querySelector('#stack-error');
  const vhostInput = ref.body.querySelector('#stack-vhost');
  const applyTemplate = () => {
    const template = templates.find((item) => item.id === templateSelect.value);
    if (!template) return;
    composeArea.value = template.compose;
    envArea.value = template.env || '';
    if (template.id !== 'blank' && !nameInput.value) nameInput.value = template.id;
  };
  applyTemplate();
  templateSelect.addEventListener('change', applyTemplate);
  ref.footer.querySelector('[data-cancel]').addEventListener('click', ref.close);
  const createButton = ref.footer.querySelector('[data-create]');
  createButton.addEventListener('click', async () => {
    errorBox.innerHTML = '';
    const name = nameInput.value.trim();
    if (!/^[a-z0-9][a-z0-9._-]{0,63}$/.test(name)) {
      errorBox.innerHTML = errorHtml('Stack name must be lowercase letters, numbers, dots or dashes.');
      return;
    }
    if (!composeArea.value.trim()) {
      errorBox.innerHTML = errorHtml('The compose file is empty.');
      return;
    }
    let compose = composeArea.value;
    const vhost = vhostInput.value.trim();
    if (vhost) {
      compose = `# Front door: add a route for ${vhost} in your reverse proxy or DNS.\n` + compose;
    }
    const restore = busy(createButton, 'Creating…');
    try {
      const result = await api.post('/api/stacks', {
        host: hostSelect.value,
        name,
        compose,
        env: envArea.value,
      });
      ref.close();
      toast('Stack created in the repo', 'ok', `${hostSelect.value}/${name}`);
      await ctx.refreshState({ force: true });
      ctx.navigate(`#/stack/${encodeURIComponent(hostSelect.value)}/${encodeURIComponent(name)}`);
    } catch (error) {
      restore();
      errorBox.innerHTML = errorHtml(ui.errorText(error));
    }
  });
}

/* ------------------------------------------------------------------- hosts */
export async function showAddHost(ctx) {
  const wiz = {
    step: 1,
    data: { alias: '', address: '', user: 'pi', port: 22, role: '', notes: '', sync: true, key: ctx.bootstrap?.ssh?.default_key?.private || '' },
    fingerprints: [],
    known: false,
    host: null,
    connection: null,
    warnings: [],
    installing: false,
  };
  const ref = openModal({
    title: 'Add a host',
    subtitle: 'Guided ed25519 onboarding — verify the fingerprint, then install trust',
    className: 'wide',
    body: '',
    footer: `<span class="note" id="wiz-note">Nothing on the Pi is changed until you install the key.</span><div class="spacer"></div><div class="btn-group" id="wiz-buttons"></div>`,
  });
  const body = ref.body;
  const buttons = ref.footer.querySelector('#wiz-buttons');
  const note = ref.footer.querySelector('#wiz-note');

  const keyOptions = () => {
    const keys = ctx.bootstrap?.ssh?.keys || [];
    const options = keys.map((key) => `<option value="${esc(key.private)}" ${wiz.data.key === key.private ? 'selected' : ''}>${esc(key.type)} · ${esc(key.fingerprint)} · ${esc(key.private.replace(/^.*\//, ''))}</option>`).join('');
    return `<select id="wiz-key" class="wizard-select">${options || '<option value="">no keys found — create one</option>'}</select>`;
  };

  const renderButtons = (items) => {
    buttons.innerHTML = items.map((item) => `<button class="btn ${item.className || 'ghost'}" data-wiz="${esc(item.id)}" ${item.disabled ? 'disabled' : ''}>${esc(item.label)}</button>`).join('');
    for (const item of items) {
      const button = buttons.querySelector(`[data-wiz="${item.id}"]`);
      if (button) button.addEventListener('click', () => item.onClick(button));
    }
  };

  const stepHeader = () => `<div class="wizard-steps">
    ${[1, 2, 3].map((step) => `<div class="wizard-step ${wiz.step === step ? 'active' : wiz.step > step ? 'done' : ''}"><span class="n">${wiz.step > step ? '✓' : step}</span>${['Address', 'Fingerprint', 'Key & trust'][step - 1]}</div>`).join('')}
  </div>`;

  const render = () => {
    if (wiz.step === 1) {
      body.innerHTML = `${stepHeader()}
        <div class="field-row three">
          <div class="field"><label for="wiz-alias">Alias *</label><input id="wiz-alias" value="${esc(wiz.data.alias)}" placeholder="pi5" autocomplete="off"></div>
          <div class="field"><label for="wiz-user">SSH user *</label><input id="wiz-user" value="${esc(wiz.data.user)}" placeholder="pi" autocomplete="off"></div>
          <div class="field"><label for="wiz-port">Port</label><input id="wiz-port" type="number" value="${esc(wiz.data.port)}" min="1" max="65535"></div>
        </div>
        <div class="field"><label for="wiz-address">Address *</label><input id="wiz-address" value="${esc(wiz.data.address)}" placeholder="192.0.2.10 or pi5.local" autocomplete="off">
          <div class="hint">The LAN address or hostname is fine. Tailnet addresses work too if SSH is enabled there.</div></div>
        <div class="field-row">
          <div class="field"><label for="wiz-role">Role <span class="opt">optional</span></label><input id="wiz-role" value="${esc(wiz.data.role)}" placeholder="DNS, media, backup…"></div>
          <div class="field"><label for="wiz-notes">Notes <span class="opt">optional</span></label><input id="wiz-notes" value="${esc(wiz.data.notes)}" placeholder="anything future-you should know"></div>
        </div>
        <label class="check"><input type="checkbox" id="wiz-sync" ${wiz.data.sync ? 'checked' : ''}><span>Also add/update <span class="mono">~/.ssh/config</span> so <span class="mono">ssh ${esc(wiz.data.alias || '<alias>')}</span> works in the terminal.</span></label>
        <div class="info-box">${icon('info')}<div>Next step fetches the host key fingerprint with <span class="mono">ssh-keyscan</span>. Verify it before trusting; then the console can test and install your key.</div></div>`;
      buttons.innerHTML = '';
      renderButtons([{ id: 'cancel', label: 'Cancel', onClick: ref.close }, { id: 'next', label: 'Continue', className: 'primary', onClick: () => step2() }]);
      note.textContent = 'Nothing on the Pi is changed until you install the key.';
    } else if (wiz.step === 2) {
      const keys = wiz.fingerprints || [];
      body.innerHTML = `${stepHeader()}
        <div class="field"><label>Host key fingerprints for <span class="mono">${esc(wiz.data.address)}:${esc(wiz.data.port)}</span></label>
          ${keys.length ? `<div class="flex-col gap-1">${keys.map((key) => `<div class="key-box"><div class="key-line"><span class="pill ${key.type.toLowerCase().includes('ed25519') ? 'ok' : 'neutral'}">${esc(key.type)}</span><div class="fingerprint">${esc(key.fingerprint)}</div></div></div>`).join('')}</div>`
            : `<div class="warn-box">${icon('alert')}<div>Could not fetch a host key (the host may be down or SSH not running yet). You can still add the host and verify later.</div></div>`}
        </div>
        ${wiz.known ? `<div class="success-box">${icon('check')}<div>This address already exists in <span class="mono">known_hosts</span>. The fingerprint above still needs to match the machine you intend to reach.</div></div>` : ''}
        <label class="check"><input type="checkbox" id="wiz-verified"><span>I verified these fingerprints against the host or its console${keys.length ? '' : ' (no fingerprint was available, so this is an explicit trust decision)'}.</span></label>
        <div id="wiz-step2-error"></div>`;
      renderButtons([
        { id: 'back', label: 'Back', onClick: () => { readStep1(); wiz.step = 1; render(); } },
        { id: 'next2', label: 'Continue', className: 'primary', disabled: true, onClick: () => step3() },
      ]);
      const verified = body.querySelector('#wiz-verified');
      const next2 = buttons.querySelector('[data-wiz="next2"]');
      verified?.addEventListener('change', () => { if (next2) next2.disabled = !verified.checked; });
      note.textContent = 'The verified key is pinned in ~/.ssh/known_hosts before the first login.';
    } else {
      const keys = ctx.bootstrap?.ssh?.keys || [];
      const selected = keys.find((key) => key.private === wiz.data.key) || keys[0];
      const pub = selected?.public || '';
      const manual = pub ? `mkdir -p ~/.ssh && chmod 700 ~/.ssh\nprintf '%s\\n' '${pub}' >> ~/.ssh/authorized_keys\nchmod 600 ~/.ssh/authorized_keys` : '';
      const status = wiz.connection;
      body.innerHTML = `${stepHeader()}
        <div class="grid cols-2">
          <div>
            <div class="field"><label for="wiz-key">SSH identity</label>${keyOptions()}
              <div class="hint">Prefer an ed25519 key. ${selected ? `Fingerprint ${esc(selected.fingerprint)}` : ''}</div>
            </div>
            <div class="btn-group">
              <button class="btn sm" data-wiz-extra="generate">${icon('plus', 12)} Create dedicated key</button>
              <button class="btn sm" data-wiz-extra="test" ${wiz.host ? '' : 'disabled'}>${icon('wifi', 12)} Test connection</button>
            </div>
            ${wiz.host ? '' : `<div class="mt-2"><button class="btn primary" data-wiz-extra="add">${icon('key', 13)} Add host & test</button></div>`}
          </div>
          <div>
            <div class="field"><label>Authentication status</label>
              ${wiz.connection
                ? wiz.connection.ok
                  ? `<div class="success-box">${icon('check')}<div><strong>Connected.</strong> ${esc(wiz.connection.hostname || '')}${wiz.connection.arch ? ` · ${esc(wiz.connection.arch)}` : ''} · ${esc(String(wiz.connection.latency_ms))} ms</div></div>`
                  : `<div class="danger-box">${icon('alert')}<div>${esc(wiz.connection.error || 'Authentication failed.')}</div></div>`
                : `<div class="info-box">${icon('info')}<div>The host is not tested yet.</div></div>`}
            </div>
            ${wiz.host && !wiz.connection?.ok ? `
              <div class="field"><label for="wiz-password">Pi password <span class="opt">one-time, never stored</span></label>
                <input id="wiz-password" type="password" placeholder="password for ${esc(wiz.data.user)}" autocomplete="off">
                <div class="hint">Used once to append the public key to <span class="mono">authorized_keys</span>, then discarded. Requires password auth to be enabled.</div>
              </div>
              <button class="btn primary" data-wiz-extra="install">${icon('lock', 13)} Install key with password</button>
              <details class="mt-2"><summary class="muted tiny plain-summary">Manual fallback (no password auth)</summary>
                <div class="key-box mt-1"><div class="key-line"><code>${esc(pub)}</code><button class="btn sm icon" data-wiz-extra="copy" title="Copy public key">${icon('copy', 12)}</button></div>
                <pre class="log mt-1">${esc(manual)}</pre></div>
              </details>` : ''}
          </div>
        </div>
        <div id="wiz-error"></div>`;
      const keySelect = body.querySelector('#wiz-key');
      keySelect?.addEventListener('change', () => { wiz.data.key = keySelect.value; render(); });
      renderButtons([
        ...(wiz.host ? [] : [{ id: 'back', label: 'Back', onClick: () => { wiz.step = 2; render(); } }]),
        ...(wiz.connection?.ok ? [{ id: 'finish', label: 'Finish', className: 'primary', onClick: finish }] : []),
        ...(wiz.host && !wiz.connection?.ok ? [{ id: 'skip', label: 'Finish later', onClick: finish }] : []),
        { id: 'cancel', label: 'Cancel', onClick: ref.close },
      ]);
      note.textContent = wiz.connection?.ok ? 'Host added. Future deploys use this key.' : wiz.host ? 'Host entry exists; install trust to deploy.' : 'The host is added only when you click Add host & test.';
      body.querySelector('[data-wiz-extra="generate"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        const restore = busy(button, 'Creating…');
        try {
          const name = 'homelab_console_ed25519';
          const result = await api.post('/api/ssh/generate', { name });
          wiz.data.key = result.key.private;
          await refreshKeys();
          render();
          toast('ed25519 key created', 'ok', result.key.fingerprint);
        } catch (error) {
          restore();
          toast('Could not create key', 'err', ui.errorText(error));
        }
      });
      body.querySelector('[data-wiz-extra="test"]')?.addEventListener('click', async (event) => {
        if (!wiz.host) return;
        const button = event.currentTarget;
        const restore = busy(button, 'Testing…');
        try {
          const result = await api.post(`/api/hosts/${encodeURIComponent(wiz.host.alias)}/test`, {});
          wiz.connection = result.result;
          render();
          toast(result.result.ok ? 'SSH works' : 'SSH failed', result.result.ok ? 'ok' : 'warn', result.result.error || '');
        } catch (error) {
          restore();
          toast('Test failed', 'err', ui.errorText(error));
        }
      });
      body.querySelector('[data-wiz-extra="add"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        const restore = busy(button, 'Adding…');
        try {
          const result = await api.post('/api/hosts', {
            alias: wiz.data.alias,
            address: wiz.data.address,
            user: wiz.data.user,
            port: Number(wiz.data.port),
            key: wiz.data.key || '',
            role: wiz.data.role,
            notes: wiz.data.notes,
            sync_ssh_config: wiz.data.sync,
          });
          wiz.host = result.host;
          wiz.connection = result.connection;
          wiz.warnings = result.warnings || [];
          if (wiz.warnings.length) toast('Host saved with warnings', 'warn', wiz.warnings.join(' · '));
          await ctx.reloadBootstrap();
          await ctx.refreshState({ force: true });
          render();
          if (result.connection?.ok) toast('Host added and authenticated', 'ok', wiz.data.alias);
          else toast('Host added — key trust pending', 'warn', result.connection?.error || '');
        } catch (error) {
          restore();
          const target = document.querySelector('#wiz-error');
          if (target) target.innerHTML = errorHtml(ui.errorText(error));
          toast('Could not add host', 'err', ui.errorText(error));
        }
      });
      body.querySelector('[data-wiz-extra="install"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        const password = body.querySelector('#wiz-password')?.value || '';
        if (!password) {
          toast('Enter the Pi password first', 'warn');
          return;
        }
        const restore = busy(button, 'Installing…');
        try {
          const result = await api.post('/api/ssh/install', { alias: wiz.host.alias, key: wiz.data.key, password });
          body.querySelector('#wiz-password').value = '';
          wiz.connection = result.result?.installed ? { ok: true, hostname: wiz.host.alias, latency_ms: 0 } : null;
          if (!result.result?.ok) {
            toast('Key install did not verify', 'warn', result.result?.message || '');
          } else {
            toast('Key installed', 'ok', 'SSH authentication works now.');
          }
          await ctx.reloadBootstrap();
          await ctx.refreshState({ force: true });
          render();
        } catch (error) {
          restore();
          toast('Key install failed', 'err', ui.errorText(error));
        }
      });
      body.querySelector('[data-wiz-extra="copy"]')?.addEventListener('click', () => ui.copyText(pub));
    }
  };

  const refreshKeys = async () => {
    try {
      const result = await api.get('/api/ssh');
      if (ctx.bootstrap) ctx.bootstrap.ssh = result;
    } catch { /* keep old list */ }
  };

  const readStep1 = () => {
    wiz.data.alias = body.querySelector('#wiz-alias')?.value.trim().toLowerCase() || wiz.data.alias;
    wiz.data.address = body.querySelector('#wiz-address')?.value.trim() || wiz.data.address;
    wiz.data.user = body.querySelector('#wiz-user')?.value.trim() || wiz.data.user;
    wiz.data.port = Number(body.querySelector('#wiz-port')?.value || wiz.data.port);
    wiz.data.role = body.querySelector('#wiz-role')?.value.trim() || '';
    wiz.data.notes = body.querySelector('#wiz-notes')?.value.trim() || '';
    wiz.data.sync = Boolean(body.querySelector('#wiz-sync')?.checked);
  };

  const step2 = async () => {
    readStep1();
    const error = body.querySelector('#wiz-error');
    if (!/^[a-z][a-z0-9-]{0,31}$/.test(wiz.data.alias)) {
      toast('Alias must be lowercase letters/numbers/dashes', 'warn');
      return;
    }
    if (!wiz.data.address || /[\s]/.test(wiz.data.address)) {
      toast('Enter a valid address', 'warn');
      return;
    }
    const next = buttons.querySelector('[data-wiz="next"]');
    const restore = busy(next, 'Scanning…');
    try {
      const result = await api.post('/api/ssh/fingerprint', { address: wiz.data.address, port: wiz.data.port });
      wiz.fingerprints = result.fingerprints || [];
      wiz.known = result.known;
      wiz.step = 2;
      render();
    } catch (error) {
      restore();
      toast('Fingerprint lookup failed', 'warn', ui.errorText(error));
      wiz.fingerprints = [];
      wiz.known = false;
      wiz.step = 2;
      render();
    }
  };

  const step3 = async () => {
    const checkbox = body.querySelector('#wiz-verified');
    if (!checkbox?.checked) {
      toast('Verify the fingerprint first', 'warn');
      return;
    }
    try {
      await api.post('/api/ssh/trust', { address: wiz.data.address, port: wiz.data.port });
    } catch (error) {
      toast('Host key was not pinned', 'err', ui.errorText(error));
      return;
    }
    wiz.step = 3;
    render();
  };

  const finish = async () => {
    ref.close();
    await ctx.reloadBootstrap();
    await ctx.refreshState({ force: true });
    ctx.navigate('#/hosts');
    toast('Host saved', 'ok', wiz.data.alias);
  };

  render();
}

export async function showEditHost(ctx, host) {
  const hosts = ctx.bootstrap?.hosts || [];
  const live = hosts.find((item) => item.alias === host.alias) || host;
  const keys = ctx.bootstrap?.ssh?.keys || [];
  const ref = openModal({
    title: `Edit ${host.alias}`,
    subtitle: 'Inventory and SSH addressing — stack files are not touched',
    body: `
      <div class="field-row three">
        <div class="field"><label for="edit-alias">Alias</label><input id="edit-alias" value="${esc(live.alias)}" disabled></div>
        <div class="field"><label for="edit-user">SSH user</label><input id="edit-user" value="${esc(live.user)}"></div>
        <div class="field"><label for="edit-port">Port</label><input id="edit-port" type="number" value="${esc(live.port)}"></div>
      </div>
      <div class="field"><label for="edit-address">Address</label><input id="edit-address" value="${esc(live.address)}"></div>
      <div class="field-row">
        <div class="field"><label for="edit-role">Role</label><input id="edit-role" value="${esc(live.role || '')}"></div>
        <div class="field"><label for="edit-notes">Notes</label><input id="edit-notes" value="${esc(live.notes || '')}"></div>
      </div>
      <div class="field"><label for="edit-key">SSH identity</label>
        <select id="edit-key"><option value="" ${!live.key ? 'selected' : ''}>agent / ~/.ssh/config default</option>
        ${keys.map((key) => `<option value="${esc(key.private)}" ${live.key === key.private ? 'selected' : ''}>${esc(key.fingerprint)} · ${esc(key.private.replace(/^.*\//, ''))}</option>`).join('')}
        </select>
      </div>
      <label class="check"><input type="checkbox" id="edit-sync" checked><span>Update <span class="mono">~/.ssh/config</span> to match this host.</span></label>
      <div id="edit-error"></div>`,
    footer: `<div class="spacer"></div><button class="btn ghost" data-cancel>Cancel</button><button class="btn primary" data-save>Save host</button>`,
  });
  ref.footer.querySelector('[data-cancel]').addEventListener('click', ref.close);
  const saveButton = ref.footer.querySelector('[data-save]');
  saveButton.addEventListener('click', async () => {
    const restore = busy(saveButton, 'Saving…');
    try {
      await api.patch(`/api/hosts/${encodeURIComponent(host.alias)}`, {
        address: ref.body.querySelector('#edit-address').value.trim(),
        user: ref.body.querySelector('#edit-user').value.trim(),
        port: Number(ref.body.querySelector('#edit-port').value),
        role: ref.body.querySelector('#edit-role').value.trim(),
        notes: ref.body.querySelector('#edit-notes').value.trim(),
        key: ref.body.querySelector('#edit-key').value,
        sync_ssh_config: ref.body.querySelector('#edit-sync').checked,
      });
      ref.close();
      await ctx.reloadBootstrap();
      await ctx.refreshState({ force: true });
      toast('Host updated', 'ok', host.alias);
    } catch (error) {
      restore();
      ref.body.querySelector('#edit-error').innerHTML = errorHtml(ui.errorText(error));
    }
  });
}

export async function showRemoveHost(ctx, host) {
  const stacks = (ctx.state?.stacks || []).filter((row) => row.host === host.alias).map((row) => row.name);
  const warning = stacks.length
    ? `<div class="warn-box mt-2">${icon('alert')}<div><strong>${ui.plural(stacks.length, 'stack')} still live in the repo:</strong> ${esc(stacks.join(', '))}. Removing the host entry leaves those files and the Pi untouched; deploys for them will stop.</div></div>`
    : '';
  const confirmed = await confirmDialog({
    title: `Remove ${host.alias}?`,
    message: 'This removes the host from scripts/hosts.json and the console. Nothing on the Pi is deleted, and its ~/.ssh/config stanza is left in place.',
    detail: warning,
    confirmLabel: 'Remove host',
    danger: true,
  });
  if (!confirmed) return;
  try {
    await api.del(`/api/hosts/${encodeURIComponent(host.alias)}${stacks.length ? '?force=1' : ''}`);
    await ctx.reloadBootstrap();
    await ctx.refreshState({ force: true });
    toast('Host removed', 'ok', host.alias);
    ctx.navigate('#/hosts');
  } catch (error) {
    toast('Could not remove host', 'err', ui.errorText(error));
  }
}

/* --------------------------------------------------------------------- git */
export function showCommit(ctx) {
  const git = ctx.state?.git || {};
  const entries = git.entries || [];
  if (!entries.length) {
    toast('Nothing to commit', 'ok', 'stacks/ and scripts/hosts.json are clean.');
    return;
  }
  const defaultMessage = `homelab: apply console changes ${new Date().toISOString().slice(0, 10)}`;
  const ref = openModal({
    title: 'Commit repo changes',
    subtitle: `${entries.length} file(s) under stacks/ and scripts/hosts.json`,
    body: `<div class="diff">
        ${entries.map((entry) => `<div class="diff-line"><span class="marker">${esc(entry.code)}</span><span class="mono">${esc(entry.path)}</span></div>`).join('')}
      </div>
      <div class="info-box mt-2">${icon('info')}<div>.env-family files are gitignored and never committed. The commit stays local; push with git when you are ready.</div></div>
      <div class="field mt-2"><label for="commit-message">Commit message</label><input id="commit-message" value="${esc(defaultMessage)}" autocomplete="off"></div>
      <div id="commit-error"></div>`,
    footer: `<div class="spacer"></div><button class="btn ghost" data-cancel>Cancel</button><button class="btn primary" data-commit>Commit changes</button>`,
  });
  ref.footer.querySelector('[data-cancel]').addEventListener('click', ref.close);
  const button = ref.footer.querySelector('[data-commit]');
  button.addEventListener('click', async () => {
    const restore = busy(button, 'Committing…');
    try {
      const result = await api.post('/api/git/commit', { message: ref.body.querySelector('#commit-message').value });
      ref.close();
      await ctx.refreshState({ force: true });
      refreshRemoteSoon(ctx);
      toast('Committed', 'ok', result.commit || '');
    } catch (error) {
      restore();
      ref.body.querySelector('#commit-error').innerHTML = errorHtml(ui.errorText(error));
    }
  });
}

/* --------------------------------------------------------------- repo fleet */
const repoApi = (repo, suffix = '') => `/api/repos/${encodeURIComponent(repo.id)}${suffix}`;


export function showRepoCommit(ctx, repo) {
  const allEntries = repo.entries || [];
  const entries = allEntries.filter((entry) => !entry.ignored);
  const ignoredEntries = allEntries.filter((entry) => entry.ignored);
  if (!entries.length) {
    if (ignoredEntries.length) {
      toast('Only ignored changes', 'warn', `${ignoredEntries.length} path(s) in ${repo.rel} match .gitignore and are hidden from commit selection.`);
    } else {
      toast('Nothing to commit', 'ok', `${repo.rel} is clean.`);
    }
    return;
  }
  const defaultMessage = `${repo.name}: update files ${new Date().toISOString().slice(0, 10)}`;
  const isNoise = (value) => /(^|\/)(__pycache__|node_modules|\.venv)(\/|$)|\.pyc$|(^|\/)\.DS_Store$|(^|\/)\.env$/.test(value);
  const ref = openModal({
    title: 'Commit project changes',
    subtitle: `${repo.rel} · ${entries.length} selectable file(s)${ignoredEntries.length ? ` · ${ignoredEntries.length} ignored hidden` : ''}`,
    className: 'wide',
    body: `<div class="repo-commit-toolbar">
        <div class="muted tiny">Choose exactly what enters this commit. Hooks run normally; nothing is pushed.</div>
        <div class="btn-group">
          <button class="btn ghost sm" type="button" data-select-all>All</button>
          <button class="btn ghost sm" type="button" data-select-none>None</button>
          <span class="pill tiny neutral" data-selected-count>${entries.filter((entry) => !isNoise(entry.path)).length} selected</span>
        </div>
      </div>
      <div class="repo-file-list">
        ${entries.map((entry, index) => {
          const noise = isNoise(entry.path);
          return `<label class="repo-file-row" for="repo-file-${index}">
          <input type="checkbox" class="repo-file-check" id="repo-file-${index}" data-path="${esc(entry.path)}" ${noise ? '' : 'checked'}>
          <span class="repo-file-code mono">${esc(entry.code_raw || entry.code)}</span>
          <span class="repo-file-path mono">${esc(entry.path)}${entry.orig_path ? `<span class="repo-file-orig"> ← ${esc(entry.orig_path)}</span>` : ''}</span>
          <span class="repo-file-tag">${noise ? 'noise · unchecked' : entry.untracked ? 'new' : entry.staged ? 'staged' : 'modified'}</span>
        </label>`;
        }).join('')}
      </div>
      ${ignoredEntries.length ? `<div class="info-box mt-2">${icon('info')}<div>${ui.plural(ignoredEntries.length, 'path')} match <span class="mono">.gitignore</span> and are hidden from this commit. They stay in the working tree.</div></div>` : ''}
      ${repo.entries_truncated || repo.entries_total > allEntries.length ? `<div class="warn-box mt-2">${icon('alert')}<div>Showing the first ${allEntries.length} of ${repo.entries_total || allEntries.length} files. Commit from a terminal for the remaining paths.</div></div>` : ''}
      <div class="field mt-2"><label for="repo-commit-message">Commit message</label><input id="repo-commit-message" value="${esc(defaultMessage)}" autocomplete="off" maxlength="200"></div>
      <div id="repo-commit-error"></div>`,
    footer: `<div class="spacer"></div><button class="btn ghost" data-cancel>Cancel</button><button class="btn primary" data-commit>Commit selected</button>`,
  });
  const checks = () => [...ref.body.querySelectorAll('.repo-file-check')];
  const selected = () => checks().filter((node) => node.checked);
  const syncCount = () => {
    ref.body.querySelector('[data-selected-count]').textContent = `${selected().length} selected`;
    ref.footer.querySelector('[data-commit]').disabled = selected().length === 0;
  };
  ref.body.querySelector('[data-select-all]').addEventListener('click', () => {
    checks().forEach((node) => { node.checked = true; });
    syncCount();
  });
  ref.body.querySelector('[data-select-none]').addEventListener('click', () => {
    checks().forEach((node) => { node.checked = false; });
    syncCount();
  });
  checks().forEach((node) => node.addEventListener('change', syncCount));
  syncCount();
  ref.footer.querySelector('[data-cancel]').addEventListener('click', ref.close);
  const button = ref.footer.querySelector('[data-commit]');
  button.addEventListener('click', async () => {
    const files = selected().map((node) => node.dataset.path);
    if (!files.length) {
      ref.body.querySelector('#repo-commit-error').innerHTML = errorHtml('Select at least one file.');
      return;
    }
    const restore = busy(button, 'Committing…');
    try {
      const result = await api.post(repoApi(repo, '/commit'), {
        message: ref.body.querySelector('#repo-commit-message').value,
        files,
      });
      ref.close();
      await ctx.refreshState({ force: true });
      refreshRemoteSoon(ctx);
      toast('Committed', 'ok', `${repo.rel} · ${result.commit || `${files.length} file(s)`}`);
    } catch (error) {
      restore();
      ref.body.querySelector('#repo-commit-error').innerHTML = errorHtml(ui.errorText(error));
    }
  });
}

export async function syncGitea(ctx, repo) {
  if (!repo.gitea?.target) {
    toast('No Gitea target', 'warn', repo.gitea?.detail || 'This project has no GitHub origin or its owner does not match Gitea.');
    return false;
  }
  if (repo.lfs) {
    toast('Git LFS project skipped', 'warn', 'Gitea sync is deliberately disabled for LFS projects.');
    return false;
  }
  try {
    const dry = await api.post(repoApi(repo, '/gitea/dry-run'));
    if (!dry.ok) {
      toast('Gitea sync failed', 'err', dry.detail || 'The Gitea dry run failed.');
      return false;
    }
    if (!['would-create', 'would-sync'].includes(dry.status)) {
      await ctx.refreshState({ force: true });
      toast(dry.status === 'up-to-date' ? 'Gitea already in sync' : 'Nothing to sync', 'ok', dry.detail || '');
      return true;
    }
    const result = await api.post(repoApi(repo, '/gitea/sync'), { review_hash: dry.review_hash || '' });
    await ctx.refreshState({ force: true });
    refreshRemoteSoon(ctx);
    toast(result.ok ? 'Gitea backup updated' : 'Gitea sync failed', result.ok ? 'ok' : 'err', result.detail || '');
    return result.ok;
  } catch (error) {
    toast('Gitea sync failed', 'err', ui.errorText(error));
    return false;
  }
}

export function showPushBoth(ctx, repos) {
  const selected = (repos || []).filter(Boolean);
  if (!selected.length) {
    toast('Select at least one project', 'warn', 'Use the row checkboxes to choose what enters the queue.');
    return;
  }
  const unknown = selected.filter((repo) => repo.gitea?.status === 'unknown').length;
  const rows = selected.map((repo) => {
    const ahead = repo.ahead === null || repo.ahead === undefined ? 'new branch' : `${repo.ahead} ahead`;
    const github = repo.needs?.push ? ahead : 'up to date';
    const gitea = repo.gitea?.status === 'unknown'
      ? 'mirror (not checked)'
      : ['behind', 'missing'].includes(repo.gitea?.status)
        ? 'mirror refs'
        : repo.gitea?.status === 'synced' && !repo.needs?.push ? 'already in sync'
          : (repo.gitea?.detail || 'mirror');
    return `<tr>
      <td class="mono">${esc(repo.rel)}</td>
      <td>${repo.needs?.push ? ui.pill('info', github) : ui.pill('ok', github)}</td>
      <td>${repo.gitea?.target ? ui.pill('neutral', gitea) : ui.pill('neutral', gitea)}</td>
    </tr>`;
  }).join('');
  const ref = openModal({
    title: 'Queue GitHub + Gitea',
    subtitle: `${selected.length} project(s) → GitHub then Gitea`,
    className: 'wide',
    body: `<div class="info-box">${icon('info')}<div>GitHub receives the current branch, then Gitea receives every local branch and tag. If GitHub fails, the Gitea backup still runs and the queue continues.</div></div>
      ${unknown ? `<div class="warn-box mt-2">${icon('clock')}<div>${ui.plural(unknown, 'project')} not checked against Gitea yet. The queue will mirror them and run the duplicate-history check before writing.</div></div>` : ''}
      <div class="table-wrap mt-2"><table class="data">
        <thead><tr><th>Project</th><th>GitHub</th><th>Gitea</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`,
    footer: `<div class="spacer"></div><button class="btn ghost" data-cancel>Cancel</button><button class="btn primary" data-queue>Queue ${selected.length} project(s)</button>`,
  });
  ref.footer.querySelector('[data-cancel]').addEventListener('click', ref.close);
  const queueButton = ref.footer.querySelector('[data-queue]');
  queueButton.addEventListener('click', async () => {
    const restore = busy(queueButton, 'Queueing…');
    try {
      const result = await api.post('/api/repos/push-both', { repos: selected.map((repo) => repo.id) });
      ref.close();
      await ctx.refreshState({ force: true });
      toast('Queue started', 'ok', `${selected.length} project(s) · GitHub → Gitea`);
      if (result.run_id) ui.runModal(ctx, result.run_id, 'Projects: GitHub + Gitea');
    } catch (error) {
      restore();
      ref.body.querySelector('#repo-queue-error')?.remove();
      ref.body.insertAdjacentHTML('beforeend', `<div id="repo-queue-error" class="mt-2">${errorHtml(ui.errorText(error))}</div>`);
    }
  });
}
