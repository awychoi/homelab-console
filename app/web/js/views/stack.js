import * as ui from '../ui.js';
import { pageHead, stateBanner, runtimePill, syncPill, safetyBadge, containerStatePill } from './common.js';
import { showDeploy, showLifecycle, showUpdate, showRollback, renderPlan } from '../flows.js';
import { filterNoticeHtml } from '../notices.js';
import { openHostTerminal } from '../terminal.js';

const { esc, icon } = ui;

const keyOf = (host, name) => `${host}/${name}`;

function getCache(ctx, host, name) {
  return ctx.uiState.stackCache?.[keyOf(host, name)] || null;
}

function setCache(ctx, host, name, data) {
  if (!ctx.uiState.stackCache) ctx.uiState.stackCache = {};
  ctx.uiState.stackCache[keyOf(host, name)] = { data, fetchedAt: Date.now() };
}

function getTab(ctx, host, name) {
  return ctx.uiState.stackTabs?.[keyOf(host, name)] || 'overview';
}

function setTab(ctx, host, name, tab) {
  if (!ctx.uiState.stackTabs) ctx.uiState.stackTabs = {};
  ctx.uiState.stackTabs[keyOf(host, name)] = tab;
}

function getPlan(ctx, host, name) {
  return ctx.uiState.stackPlans?.[keyOf(host, name)] || null;
}

function setPlan(ctx, host, name, plan) {
  if (!ctx.uiState.stackPlans) ctx.uiState.stackPlans = {};
  ctx.uiState.stackPlans[keyOf(host, name)] = plan;
}

function getSelectedFile(ctx, host, name) {
  const cache = getCache(ctx, host, name)?.data;
  const files = cache?.files || [];
  const selected = ctx.uiState.stackFiles?.[keyOf(host, name)];
  if (selected && files.some((file) => file.path === selected)) return selected;
  return cache?.stack?.compose_file || files[0]?.path || 'docker-compose.yml';
}

function setSelectedFile(ctx, host, name, path) {
  if (!ctx.uiState.stackFiles) ctx.uiState.stackFiles = {};
  ctx.uiState.stackFiles[keyOf(host, name)] = path;
}

function targetAction(target) {
  if (!target.actions?.length) return '';
  const shortLabels = { update: 'Update', 'pin-latest': 'Pin latest', 'pin-digest': 'Pin digest' };
  const action = target.actions[0];
  return `<button class="btn ghost sm" data-update-target="${esc(target.service)}">${icon('updates', 12)} ${esc(shortLabels[action] || action)}</button>`;
}

function pinsTable(ctx, host, name, detail) {
  const targets = detail.targets || [];
  if (!targets.length) return '<div class="muted tiny">No image pins detected in this compose file.</div>';
  return `<div class="table-wrap"><table class="data">
    <thead><tr><th>Service</th><th>Current</th><th>Latest</th><th>Status</th><th class="actions"></th></tr></thead>
    <tbody>${targets.map((target) => `<tr>
      <td class="main-cell"><span class="mono cell-strong">${esc(target.service)}</span>${target.services_affected?.length > 1 ? `<span class="sub">+ ${esc(target.services_affected.slice(1).join(', '))}</span>` : '<span class="sub">' + esc(target.source?.label || 'image') + '</span>'}</td>
      <td class="mono">${esc(target.current_display === '(no tag)' ? '—' : target.current_display || '—')}${target.drift ? `<span class="sub text-warn">running ${esc(target.running_tag)}</span>` : ''}</td>
      <td class="mono">${target.latest ? `<a href="${esc(target.url || '#')}" target="_blank" rel="noopener">${esc(target.latest)}</a>` : '<span class="dim">—</span>'}${target.latest_published ? `<span class="sub" title="${esc(ui.dateTime(target.latest_published))}">released ${esc(ui.ago(target.latest_published))}</span>` : ''}${target.platform_limited && target.upstream_latest && target.latest !== target.upstream_latest ? `<span class="sub text-warn">${esc(target.upstream_latest)} has no ${esc(target.platform || '')} build</span>` : ''}</td>
      <td>${ui.pill(target.status, target.status === 'update-available' ? 'update' : target.status)}<span class="sub">${safetyBadge(target.safety?.level)}</span></td>
      <td class="actions">${targetAction(target)}</td>
    </tr>`).join('')}</tbody>
  </table></div>`;
}

function containersTable(runtime) {
  const containers = runtime?.containers || [];
  if (!containers.length) return '<div class="muted tiny">No containers matched to this stack. Deploy it to create them.</div>';
  return `<div class="table-wrap"><table class="data">
    <thead><tr><th>Container</th><th>Service</th><th>Image</th><th>State</th><th class="right">Uptime</th><th class="right">Restarts</th></tr></thead>
    <tbody>${containers.map((container) => `<tr>
      <td class="mono cell-strong">${esc(container.name)}</td>
      <td class="muted">${esc(container.service || '—')}</td>
      <td class="mono tiny">${esc(container.image)}</td>
      <td>${containerStatePill(container)}${container.oom_killed ? ' <span class="chip bad">OOM</span>' : ''}</td>
      <td class="num">${esc(ui.duration(container.state === 'running' ? container.uptime_s : container.stopped_s))}</td>
      <td class="num">${esc(container.restart_count ?? 0)}</td>
    </tr>`).join('')}</tbody>
  </table></div>`;
}


function profileCard(detail) {
  const profile = detail?.profile;
  if (!profile) return '';
  const safety = profile.safety || {};
  const database = profile.database;
  const data = (safety.data || []).join(', ');
  const dbText = database
    ? `${esc(database.label || 'PostgreSQL')} · <span class="mono">${esc(database.container)}</span>${profile.database_source === 'custom' ? ' · custom' : ' · built-in'}`
    : '<span class="muted">no on-Pi database dump</span>';
  return `<div class="section">
    <div class="section-head"><div class="section-title">Safety &amp; backup</div><div class="section-sub">${profile.custom ? 'workspace profile' : 'built-in defaults'} · used by update planning</div><button class="btn ghost sm right" type="button" data-action="edit-profile">${icon('settings', 12)} Edit</button></div>
    <div class="card"><div class="card-body"><div class="kv">
      <div class="kv-row"><div class="kv-key">Update level</div><div class="kv-val">${safetyBadge(safety.level)}</div></div>
      <div class="kv-row"><div class="kv-key">Before updating</div><div class="kv-val">${esc(safety.backup || 'No backup note.')}</div></div>
      <div class="kv-row"><div class="kv-key">Data to check</div><div class="kv-val">${data ? esc(data) : '<span class="muted">not declared</span>'}</div></div>
      <div class="kv-row"><div class="kv-key">Database dump</div><div class="kv-val">${dbText}</div></div>
    </div></div></div>
  </div>`;
}

function overviewTab(ctx, host, name, detail) {
  const runtime = detail.runtime || {};
  const stack = detail.stack || {};
  const updates = detail.updates || [];
  const unpinned = detail.unpinned || [];
  return `
    ${updates.length ? `<div class="warn-box mb-2">${icon('updates')}<div><strong>${updates.length} update${updates.length === 1 ? '' : 's'} available:</strong> ${updates.map((item) => `${esc(item.service)} ${esc(item.current_display)} → ${esc(item.latest)}`).join(' · ')}</div>
      <button class="btn sm primary" data-go-updates>Review</button></div>` : ''}
    ${unpinned.length ? `<div class="warn-box mb-2">${icon('alert')}<div><strong>Unpinned image(s):</strong> ${unpinned.map((item) => esc(item.service)).join(', ')}. Pin a version before relying on the deployment.</div><button class="btn sm" data-go-updates>Pin</button></div>` : ''}
    <div class="grid cols-2 align-start">
      <div class="card">
        <div class="card-head"><div class="card-title">Pins & releases</div><div class="card-sub">repo intent, upstream, and the running image</div><div class="card-actions"><a class="btn ghost sm" href="#/updates">Updates</a></div></div>
        ${pinsTable(ctx, host, name, detail)}
      </div>
      <div class="card">
        <div class="card-head"><div class="card-title">Stack</div><div class="card-sub">repo → host contract</div></div>
        <div class="card-body"><div class="kv">
          <div class="kv-row"><div class="kv-key">Compose project</div><div class="kv-val mono">${esc(stack.project || name)}</div></div>
          <div class="kv-row"><div class="kv-key">Repo path</div><div class="kv-val mono">${esc(stack.dir || '')}</div></div>
          <div class="kv-row"><div class="kv-key">Compose file</div><div class="kv-val mono">${esc(stack.compose_file || '')}</div></div>
          <div class="kv-row"><div class="kv-key">Services</div><div class="kv-val">${(stack.services || []).map((service) => `<span class="chip">${esc(service.name)}</span>`).join(' ') || '—'}</div></div>
          <div class="kv-row"><div class="kv-key">Runtime</div><div class="kv-val">${runtimePill(runtime.status)} ${syncPill(runtime.sync?.state)}</div></div>
          <div class="kv-row"><div class="kv-key">24h uptime</div><div class="kv-val">${runtime.uptime_pct === null || runtime.uptime_pct === undefined ? '<span class="muted">collecting…</span>' : `<span class="mono">${runtime.uptime_pct}%</span>`}</div></div>
          <div class="kv-row"><div class="kv-key">Git status</div><div class="kv-val">${detail.git?.dirty ? ui.pill('warn', 'uncommitted changes') : ui.pill('ok', 'clean')}</div></div>
        </div></div>
      </div>
    </div>
    ${profileCard(detail)}
    <div class="section">
      <div class="section-head"><div class="section-title">Containers</div><div class="section-sub">${runtime.running || 0}/${runtime.expected || (stack.services || []).length} expected services running</div><div class="right"><button class="btn ghost sm" data-action="logs-jump">${icon('terminal', 12)} Logs</button></div></div>
      <div class="card">${containersTable(runtime)}</div>
    </div>
    <div class="section">
      <div class="section-head"><div class="section-title">24h availability</div><div class="section-sub">from the console sample store</div></div>
      ${ui.uptimeStrip(runtime.history_points || [])}
      <div class="legend"><span><i style="background:var(--accent)"></i>all services up</span><span><i style="background:var(--amber)"></i>degraded</span><span><i style="background:var(--red)"></i>down</span><span><i style="background:var(--line)"></i>unknown</span></div>
    </div>`;
}

function filesTab(ctx, host, name, detail) {
  const files = detail.files || [];
  const selected = getSelectedFile(ctx, host, name);
  const file = files.find((item) => item.path === selected) || files[0];
  const remoteOnly = (detail.remote_files || []).filter((path) => !files.some((item) => item.path === path));
  const hostOnly = remoteOnly.length
    ? `<div class="section">
        <div class="section-head"><div class="section-title">Host-only paths</div><div class="section-sub">${remoteOnly.length} not in the repo</div><button class="btn danger sm right" type="button" data-delete-remote-all>Delete all…</button></div>
        <div class="card"><div class="table-wrap"><table class="data">
          <thead><tr><th>Path on host</th><th class="actions"></th></tr></thead>
          <tbody>${remoteOnly.map((path) => `<tr>
            <td class="mono">${esc(path)}</td>
            <td class="actions"><button class="btn danger sm" type="button" data-delete-remote-path="${esc(path)}">Delete…</button></td>
          </tr>`).join('')}</tbody>
        </table></div></div>
        <div class="hint mt-1">These exist only on the host. Deploy never uploads them, and they are not deleted by a push. Remove stale runtime files here; keep anything a container still writes.</div>
      </div>`
    : '';
  return `<div class="toolbar">
      <span class="muted tiny">Create config files in subfolders with a relative path; parent folders are created automatically.</span>
      <div class="spacer"></div>
      <button class="btn ghost sm" type="button" data-action="new-file">${icon('plus', 12)} New file</button>
    </div>
  <div class="file-layout">
    <div class="file-list">
      <div class="file-list-head">${files.length} file${files.length === 1 ? '' : 's'} in repo</div>
      ${files.map((item) => `<button class="file-item ${item.path === selected ? 'active' : ''}" data-file="${esc(item.path)}">
        ${icon(item.secret ? 'lock' : 'file', 12)}<span class="name">${esc(item.path)}</span><span class="size">${ui.bytes(item.bytes)}</span></button>`).join('')}
    </div>
    <div>
      ${file ? `<div class="editor">
        <div class="editor-bar">
          <span class="editor-path">${esc(file.path)}</span>
          <span class="dirty" id="file-dirty" style="display:none">unsaved</span>
          ${file.secret ? '<span class="secret-note">secret — never commits</span>' : ''}
          <button class="btn ghost sm" data-action="revert-file">Revert</button>
          <button class="btn primary sm" data-action="save-file" disabled>Save</button>
        </div>
        <textarea class="code" id="file-editor" spellcheck="false" data-path="${esc(file.path)}" data-hash="${esc(file.hash)}">${esc(file.content)}</textarea>
      </div>
      <div class="hint mt-1">Deploy pushes all ${files.length} repo file${files.length === 1 ? '' : 's'} in this folder, including <span class="mono">.env</span> and configs. Saving here only writes the repo copy.</div>`
        : '<div class="empty">No files.</div>'}
    </div>
  </div>
  ${hostOnly}`;
}

function foldersTab(ctx, host, name, detail) {
  const remotePath = detail.remote_path || '';
  const localPath = detail.stack?.dir || '';
  const mounts = [];
  for (const container of detail.runtime?.containers || []) {
    for (const mount of container.mounts || []) {
      if (!mount.source || !mount.source.startsWith('/')) continue;
      if (mount.type && mount.type !== 'bind') continue;
      const key = `${mount.source}->${mount.destination}`;
      if (mounts.some((item) => item.key === key)) continue;
      mounts.push({ ...mount, key, container: container.name });
    }
  }
  const canTerminal = !detail.host?.local;
  const terminal = (path) => (canTerminal ? `<button class="btn ghost sm" type="button" data-terminal-path="${esc(path)}">${icon('terminal', 12)} Terminal</button>` : '');
  return `<div class="section">
    <div class="section-head"><div class="section-title">Folders</div><div class="section-sub">where this stack keeps its files</div></div>
    <div class="card"><div class="table-wrap"><table class="data">
      <thead><tr><th>Path</th><th>Kind</th><th>Owner</th><th class="actions"></th></tr></thead>
      <tbody>
        ${remotePath ? `<tr><td class="mono">${esc(remotePath)}</td><td>host stack dir</td><td class="muted">${esc(host)}</td><td class="actions">${terminal(remotePath)}</td></tr>` : ''}
        ${localPath ? `<tr><td class="mono">${esc(localPath)}</td><td>repo dir</td><td class="muted">Mac</td><td class="actions"></td></tr>` : ''}
        ${mounts.map((mount) => `<tr>
          <td class="mono">${esc(mount.source)}</td>
          <td>${esc(mount.type || 'mount')}<span class="sub">→ ${esc(mount.destination || '')}</span></td>
          <td class="muted">${esc(mount.container || '')}</td>
          <td class="actions">${terminal(mount.source)}</td>
        </tr>`).join('')}
      </tbody>
    </table></div></div>
    <div class="hint mt-1">Bind mounts are mutable runtime data. Terminal opens an SSH session on ${esc(host)} at that folder; you can inspect or remove stale files there. Data volumes are never part of a repo snapshot.</div>
  </div>`;
}

function diffTab(ctx, host, name, detail) {
  const plan = getPlan(ctx, host, name);
  return `<div class="toolbar">
      <button class="btn ${plan ? 'ghost' : 'primary'}" data-action="check-diff">${icon('refresh', 12)} ${plan ? 'Re-check diff' : 'Check diff against host'}</button>
      ${plan ? `<span class="muted tiny">checked ${esc(ui.ago(plan.checked_at))} · ${ui.plural(plan.changes?.length || 0, 'file change')}</span>` : '<span class="muted tiny">Read-only: fetches file hashes and compares them with the repo.</span>'}
      <div class="spacer"></div>
      ${plan ? `<button class="btn primary" data-action="deploy-from-plan">${icon('upload', 12)} Deploy now</button>` : ''}
    </div>
    ${plan ? renderPlan(plan) : `<div class="empty">${icon('file', 28)}<strong>No diff loaded</strong><p>Run a diff to see every file that would change on ${esc(host)}. Remote-only files are listed separately and left untouched.</p></div>`}
    ${detail.runtime?.sync?.extra?.length ? `<div class="info-box mt-2">${icon('info')}<div><strong>Host-only files:</strong> <span class="mono">${esc(detail.runtime.sync.extra.join(', '))}</span> — these are not in the repo and will not be deleted.</div></div>` : ''}`;
}

function logsTab(ctx, host, name, detail) {
  const logs = ctx.uiState.stackLogs?.[keyOf(host, name)];
  return `<div class="toolbar">
      <div class="segmented" data-log-tail>
        <button data-tail="100" class="${!logs || logs.tail === 100 ? 'active' : ''}">100</button>
        <button data-tail="300" class="${logs?.tail === 300 ? 'active' : ''}">300</button>
        <button data-tail="1000" class="${logs?.tail === 1000 ? 'active' : ''}">1000</button>
      </div>
      <button class="btn ghost sm" data-action="load-logs" ${logs?.loading ? 'disabled' : ''}>${icon('refresh', 12)} ${logs?.loading ? 'Loading…' : 'Load logs'}</button>
      <span class="muted tiny">docker compose logs — no color, no follow</span>
      <div class="spacer"></div>
      ${logs ? `<button class="btn ghost sm" data-action="copy-logs">${icon('copy', 12)} Copy</button>` : ''}
    </div>
    <div class="log" id="stack-log">${logs ? (logs.text ? ui.esc(logs.text) : '<span class="log-empty">(no output)</span>') : '<span class="log-empty">Load logs to read the last 100 lines from the host.</span>'}</div>`;
}

function snapshotsTab(ctx, host, name, detail) {
  const snapshots = detail.snapshots || [];
  return `<div class="toolbar"><span class="muted tiny">Pre-update snapshots of the repo files, taken by the console. Data volumes are never included.</span></div>
    <div class="card"><div class="table-wrap"><table class="data">
      <thead><tr><th>Snapshot</th><th>Action</th><th>Target</th><th>Files</th><th class="actions"></th></tr></thead>
      <tbody>${snapshots.length ? snapshots.map((snapshot) => `<tr>
        <td class="mono">${esc(snapshot.id)}<span class="sub">${esc(ui.dateTime(snapshot.created))}</span></td>
        <td>${ui.pill('info', snapshot.action || 'update')}</td>
        <td class="muted">${esc(snapshot.target?.service || '')} ${snapshot.target?.value ? `<span class="mono tiny">→ ${esc(snapshot.target.value)}</span>` : ''}</td>
        <td class="mono tiny">${(snapshot.files || []).length}</td>
        <td class="actions"><button class="btn danger sm" data-rollback="${esc(snapshot.id)}">${icon('restart', 12)} Roll back</button></td>
      </tr>`).join('') : '<tr><td colspan="5" class="table-empty">No snapshots yet — one is taken before every console update.</td></tr>'}</tbody>
    </table></div></div>`;
}

function contentHtml(ctx, host, name, detail, stateRow) {
  const runtime = detail?.runtime || stateRow?.runtime || {};
  const stack = detail?.stack || {};
  const files = detail?.files || [];
  const tab = getTab(ctx, host, name);
  const updates = detail?.updates || [];
  const actions = `
    <button class="btn ghost sm" data-action="pull">${icon('download', 12)} Pull & recreate</button>
    <button class="btn ghost sm" data-action="restart">${icon('restart', 12)} Restart</button>
    <button class="btn ghost sm" data-action="down">${icon('power', 12)} Down</button>
    <button class="btn primary sm" data-action="deploy">${icon('upload', 12)} Deploy</button>`;
  const tabs = [
    ['overview', 'Overview'],
    ['files', `Files${files.length ? ` (${files.length})` : ''}`],
    ['folders', 'Folders'],
    ['diff', 'Diff'],
    ['logs', 'Logs'],
    ['snapshots', `Snapshots${detail?.snapshots?.length ? ` (${detail.snapshots.length})` : ''}`],
  ];
  let tabContent = '';
  if (tab === 'files') tabContent = filesTab(ctx, host, name, detail || {});
  else if (tab === 'folders') tabContent = foldersTab(ctx, host, name, detail || {});
  else if (tab === 'diff') tabContent = diffTab(ctx, host, name, detail || {});
  else if (tab === 'logs') tabContent = logsTab(ctx, host, name, detail || {});
  else if (tab === 'snapshots') tabContent = snapshotsTab(ctx, host, name, detail || {});
  else tabContent = overviewTab(ctx, host, name, detail || {});

  let body;
  if (!detail) {
    body = `<div class="empty loading-block">${icon('activity', 28)}<strong>Loading stack detail…</strong><p>Reading ${esc(host)}/${esc(name)} from the repo and the fleet snapshot.</p></div>`;
  } else {
    body = `<div class="tabs" role="tablist" aria-label="Stack sections">${tabs.map(([id, label]) => `<button class="tab ${tab === id ? 'active' : ''}" id="stack-tab-${id}" role="tab" aria-selected="${tab === id}" aria-controls="stack-panel" tabindex="${tab === id ? '0' : '-1'}" data-tab="${id}">${esc(label)}</button>`).join('')}</div>
      <div id="stack-panel" role="tabpanel" aria-labelledby="stack-tab-${tab}">${tabContent}</div>`;
  }
  return `${pageHead({
    title: name,
    subtitle: `<a class="mono host-link" href="#/host/${encodeURIComponent(host)}">${esc(host)}</a>${stack.project ? `<span class="dot">·</span>project <span class="mono">${esc(stack.project)}</span>` : ''}${stack.dir ? `<span class="dot">·</span><span class="mono">${esc(stack.dir)}</span>` : ''}`,
    back: { href: '#/services', label: 'Stacks' },
    actions,
    badges: `${runtimePill(runtime.status)} ${syncPill(runtime.sync?.state)} ${updates.length ? ui.pill('warn', `${updates.length} update${updates.length === 1 ? '' : 's'}`) : ''}`,
  })}
  ${stateBanner(ctx)}
  ${detail?.stack?.parse_error ? `<div class="danger-box mb-2">${icon('alert')}<div><strong>Compose parse error:</strong> ${esc(detail.stack.parse_error)}</div></div>` : ''}
  <div id="stack-body">${body}</div>`;
}

export default {
  render(ctx, params) {
    const [host, name] = params || [];
    const stateRow = (ctx.state?.stacks || []).find((row) => row.host === host && row.name === name);
    const cached = getCache(ctx, host, name);
    return `<div id="stack-page" data-host="${esc(host || '')}" data-name="${esc(name || '')}">${contentHtml(ctx, host, name, cached?.data || null, stateRow)}</div>`;
  },

  mount(root, ctx, params) {
    const [host, name] = params || [];
    const page = root.querySelector('#stack-page');
    if (!page) return;

    const paint = () => {
      const stateRow = (ctx.state?.stacks || []).find((row) => row.host === host && row.name === name);
      const cached = getCache(ctx, host, name);
      page.innerHTML = filterNoticeHtml(contentHtml(ctx, host, name, cached?.data || null, stateRow));
      attach();
    };

    const load = async ({ force = false } = {}) => {
      const cached = getCache(ctx, host, name);
      if (!force && cached?.data) return cached.data;
      try {
        const detail = await ctx.api.get(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}`);
        setCache(ctx, host, name, detail);
        if (document.getElementById('stack-page') === page) paint();
        return detail;
      } catch (error) {
        if (document.getElementById('stack-page') === page) {
          const cachedNow = getCache(ctx, host, name);
          if (!cachedNow) {
            page.innerHTML = `${pageHead({ title: name, subtitle: `Reading ${esc(host)}/${esc(name)}`, back: { href: '#/services', label: 'Stacks' } })}
              <div class="danger-box">${icon('alert')}<div><strong>Could not load the stack.</strong> ${esc(ui.errorText(error))}</div></div>
              <button class="btn mt-2" data-action="retry">Try again</button>`;
            page.querySelector('[data-action="retry"]')?.addEventListener('click', () => { page.innerHTML = '<div class="empty">Retrying…</div>'; load({ force: true }); });
          }
        }
        return null;
      }
    };

    const openNewFile = () => {
      const ref = ui.openModal({
        title: `${host}/${name} · new file`,
        subtitle: 'Relative to this stack; folders are created automatically',
        className: 'wide',
        body: `
          <div class="field"><label for="new-file-path">File path *</label>
            <input id="new-file-path" placeholder="config/prometheus/prometheus.yml" autocomplete="off" spellcheck="false">
            <div class="hint">Use a relative path. <span class="mono">.env</span>, <span class="mono">.env.*</span> and <span class="mono">*.env</span> files are written mode 600; <span class="mono">.git</span> paths are rejected.</div>
          </div>
          <div class="field"><label for="new-file-content">Contents</label>
            <textarea id="new-file-content" class="compose-input" spellcheck="false" placeholder="Paste the file contents here. It is written to the repo only; deploy is a separate step."></textarea>
          </div>
          <div id="new-file-error"></div>`,
        footer: `<span class="note">The file stays in the repo until you deploy it.</span><div class="spacer"></div><button class="btn ghost" data-cancel>Cancel</button><button class="btn primary" type="button" data-create-file>Create file</button>`,
      });
      const pathInput = ref.body.querySelector('#new-file-path');
      const contentInput = ref.body.querySelector('#new-file-content');
      const errorBox = ref.body.querySelector('#new-file-error');
      const createButton = ref.footer.querySelector('[data-create-file]');
      const fail = (message) => {
        errorBox.innerHTML = `<div class="danger-box">${icon('alert')}<div>${esc(message)}</div></div>`;
      };
      const create = async () => {
        const path = pathInput.value.trim();
        const parts = path.split('/');
        if (!path) { fail('Enter a file path.'); pathInput.focus(); return; }
        if (path.startsWith('/') || parts.some((part) => !part || part === '.' || part === '..' || part === '.git' || part.startsWith('.git'))) {
          fail('Use a relative path without empty, "." or ".git" segments.');
          pathInput.focus();
          return;
        }
        errorBox.innerHTML = '';
        createButton.disabled = true;
        const label = createButton.textContent;
        createButton.textContent = 'Creating…';
        try {
          await ctx.api.put(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/file`, {
            path,
            content: contentInput.value,
            create: true,
          });
          setSelectedFile(ctx, host, name, path);
          const cached = getCache(ctx, host, name);
          if (cached) cached.fetchedAt = 0;
          ref.close();
          await load({ force: true });
          await ctx.refreshState({ force: true });
          ui.toast('File created', 'ok', path);
        } catch (error) {
          createButton.disabled = false;
          createButton.textContent = label;
          fail(ui.errorText(error));
        }
      };
      createButton.addEventListener('click', create);
      pathInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); contentInput.focus(); }
      });
      contentInput.addEventListener('keydown', (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); create(); }
      });
      setTimeout(() => pathInput.focus(), 30);
    };

    const openProfileEditor = () => {
      const detail = getCache(ctx, host, name)?.data;
      if (!detail) { ui.toast('Stack detail is still loading', 'warn'); return; }
      const profile = detail.profile || {};
      const safety = profile.safety || {};
      const database = profile.database || null;
      const dbDefaults = database || {
        label: 'PostgreSQL', container: 'postgres', user_key: 'POSTGRES_USER',
        upload_env: 'DATA_DIR', scheduled_subdir: 'backups', scheduled_prefix: 'db-backup-',
        console_subdir: 'console-backups', console_prefix: 'console-db-',
      };
      const field = (id, label, value, hint = '', type = 'text') => `<div class="field"><label for="${esc(id)}">${esc(label)}</label><input id="${esc(id)}" type="${esc(type)}" value="${esc(value)}" autocomplete="off"><div class="hint">${esc(hint)}</div></div>`;
      const ref = ui.openModal({
        title: `${host}/${name} · safety & backup`,
        subtitle: 'These values drive the update warning, auto-update exclusion and the database panel',
        className: 'wide',
        body: `
          <div class="grid cols-2">
            <div>
              <div class="field"><label for="profile-level">Update level</label>
                <select id="profile-level">${['low', 'medium', 'high', 'critical'].map((level) => `<option value="${level}" ${safety.level === level ? 'selected' : ''}>${level}</option>`).join('')}</select>
                <div class="hint">Critical stacks are manual-only; auto-update never touches them.</div>
              </div>
              <div class="field"><label for="profile-data">Data to check before updating <span class="opt">one path per line</span></label><textarea id="profile-data" spellcheck="false" placeholder="/srv/paperless/data&#10;/mnt/raid/photos">${esc((safety.data || []).join('\n'))}</textarea></div>
              <label class="check"><input type="checkbox" id="profile-dns" ${safety.dns_adjacent ? 'checked' : ''}><span><strong>DNS-adjacent.</strong> Keep the change window short and verify resolution afterwards.</span></label>
            </div>
            <div>
              <div class="field"><label for="profile-backup">Backup / pre-update note</label><textarea id="profile-backup" placeholder="Snapshot the data directory before updating.">${esc(safety.backup || '')}</textarea><div class="hint">Shown in the update dialog. It does not run anything.</div></div>
              <label class="check"><input type="checkbox" id="profile-db-enabled" ${database ? 'checked' : ''}><span><strong>PostgreSQL dump on the Pi.</strong> Enables <span class="mono">pg_dumpall</span> through the update dialog. PostgreSQL only.</span></label>
            </div>
          </div>
          <div id="profile-db-fields" ${database ? '' : 'hidden'}>
            <div class="sep-line"></div>
            <div class="grid cols-2">
              <div>${field('profile-db-label', 'Label', dbDefaults.label, 'Shown in the update dialog.')}${field('profile-db-container', 'Container', dbDefaults.container, 'PostgreSQL container name on the host.')}${field('profile-db-user-key', 'User env key', dbDefaults.user_key, 'Key in the stack .env that holds the postgres user.')}${field('profile-db-upload-env', 'Backup base env key', dbDefaults.upload_env, 'Env key whose value is the directory that contains the backup folders.')}</div>
              <div>${field('profile-db-scheduled-subdir', 'Scheduled subfolder', dbDefaults.scheduled_subdir, 'Relative to the base directory.')}${field('profile-db-scheduled-prefix', 'Scheduled prefix', dbDefaults.scheduled_prefix, 'Files matching <prefix>*.sql.gz.')}${field('profile-db-console-subdir', 'Console subfolder', dbDefaults.console_subdir, 'Where fresh console dumps are written.')}${field('profile-db-console-prefix', 'Console prefix', dbDefaults.console_prefix, 'Prefix for fresh dumps.')}</div>
            </div>
            <div class="info-box">${icon('info')}<div>The console only runs <span class="mono">pg_dumpall</span> in the named container and validates the <span class="mono">.sql.gz</span> on the Pi. It never accepts an arbitrary command from this form.</div></div>
          </div>
          <div class="hint mt-2">Saving creates a workspace profile for this stack. Built-in defaults remain available with <strong>Reset</strong>.</div>
        `,
        footer: `<button class="btn ghost" type="button" data-reset-profile>Reset to built-in</button><div class="spacer"></div><button class="btn ghost" data-cancel>Cancel</button><button class="btn primary" type="button" data-save-profile>Save profile</button>`,
      });
      const enabled = ref.body.querySelector('#profile-db-enabled');
      const dbFields = ref.body.querySelector('#profile-db-fields');
      const syncDb = () => { if (dbFields) dbFields.hidden = !enabled.checked; };
      enabled?.addEventListener('change', syncDb);
      syncDb();
      ref.footer.querySelector('[data-cancel]').addEventListener('click', () => ref.close());
      ref.footer.querySelector('[data-save-profile]').addEventListener('click', async (event) => {
        const button = event.currentTarget;
        const value = (id) => ref.body.querySelector(`#${id}`)?.value.trim() || '';
        const profileValue = {
          safety: {
            level: ref.body.querySelector('#profile-level').value,
            data: value('profile-data').split('\n').map((line) => line.trim()).filter(Boolean),
            backup: value('profile-backup'),
            dns_adjacent: ref.body.querySelector('#profile-dns').checked,
          },
          database: enabled.checked ? {
            label: value('profile-db-label'),
            container: value('profile-db-container'),
            user_key: value('profile-db-user-key'),
            upload_env: value('profile-db-upload-env'),
            scheduled_subdir: value('profile-db-scheduled-subdir'),
            scheduled_prefix: value('profile-db-scheduled-prefix'),
            console_subdir: value('profile-db-console-subdir'),
            console_prefix: value('profile-db-console-prefix'),
          } : null,
        };
        button.disabled = true;
        try {
          await ctx.api.post(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/profile`, { profile: profileValue });
          ref.close();
          const cached = getCache(ctx, host, name);
          if (cached) cached.fetchedAt = 0;
          await load({ force: true });
          await ctx.refreshState({ force: true, render: false });
          ui.toast('Safety profile saved', 'ok', `${host}/${name}`);
        } catch (error) {
          button.disabled = false;
          ui.toast('Could not save the safety profile', 'err', ui.errorText(error));
        }
      });
      ref.footer.querySelector('[data-reset-profile]').addEventListener('click', async () => {
        const proceed = await ui.confirmDialog({
          title: 'Reset to built-in?',
          message: 'The workspace profile for this stack is removed. Built-in defaults (if any) apply again.',
          confirmLabel: 'Reset',
          danger: true,
        });
        if (!proceed) return;
        try {
          await ctx.api.post(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/profile`, { reset: true });
          ref.close();
          const cached = getCache(ctx, host, name);
          if (cached) cached.fetchedAt = 0;
          await load({ force: true });
          await ctx.refreshState({ force: true, render: false });
          ui.toast('Safety profile reset', 'ok', `${host}/${name}`);
        } catch (error) {
          ui.toast('Could not reset the profile', 'err', ui.errorText(error));
        }
      });
    };

    const selectedFileContent = () => {
      const detail = getCache(ctx, host, name)?.data;
      const selected = getSelectedFile(ctx, host, name);
      return detail?.files?.find((item) => item.path === selected) || null;
    };

    const saveFile = async (editor, file) => {
      const saveButton = page.querySelector('[data-action="save-file"]');
      const content = editor.value;
      saveButton.disabled = true;
      try {
        await ctx.api.put(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/file`, {
          path: file.path,
          content,
          base_hash: file.hash,
        });
        const cached = getCache(ctx, host, name);
        if (cached) {
          const entry = cached.data.files.find((item) => item.path === file.path);
          if (entry) {
            entry.content = content;
            entry.hash = '';
            entry.bytes = new Blob([content]).size;
          }
          cached.fetchedAt = 0;
        }
        await load({ force: true });
        await ctx.refreshState({ force: true });
        ui.toast('File saved', 'ok', file.path);
      } catch (error) {
        saveButton.disabled = false;
        ui.toast('Save failed', 'err', ui.errorText(error));
      }
    };

    const attach = () => {
      const tabButtons = [...page.querySelectorAll('[data-tab]')];
      const activateTab = (id) => {
        setTab(ctx, host, name, id);
        paint();
        page.querySelector(`[data-tab="${id}"]`)?.focus();
      };
      tabButtons.forEach((button, index) => {
        button.addEventListener('click', () => activateTab(button.dataset.tab));
        button.addEventListener('keydown', (event) => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? tabButtons[0] : event.key === 'End' ? tabButtons[tabButtons.length - 1]
            : tabButtons[(index + (event.key === 'ArrowRight' ? 1 : -1) + tabButtons.length) % tabButtons.length];
          activateTab(next.dataset.tab);
        });
      });
      page.querySelector('[data-action="deploy"]')?.addEventListener('click', () => showDeploy(ctx, host, name, 'up'));
      page.querySelector('[data-action="pull"]')?.addEventListener('click', () => showDeploy(ctx, host, name, 'pull'));
      page.querySelector('[data-action="restart"]')?.addEventListener('click', () => showLifecycle(ctx, host, name, 'restart'));
      page.querySelector('[data-action="down"]')?.addEventListener('click', () => showLifecycle(ctx, host, name, 'down'));
      page.querySelector('[data-action="logs-jump"]')?.addEventListener('click', () => { setTab(ctx, host, name, 'logs'); paint(); });
      page.querySelector('[data-action="edit-profile"]')?.addEventListener('click', openProfileEditor);
      page.querySelector('[data-action="new-file"]')?.addEventListener('click', openNewFile);
      page.querySelectorAll('[data-update-target]').forEach((button) => {
        button.addEventListener('click', () => {
          const detail = getCache(ctx, host, name)?.data;
          const target = (detail?.targets || []).find((item) => item.service === button.dataset.updateTarget);
          if (target) showUpdate(ctx, target);
          else ui.toast('Target changed', 'warn', 'Refresh the stack before updating.');
        });
      });
      page.querySelectorAll('[data-go-updates]').forEach((button) => {
        button.addEventListener('click', () => {
          const detail = getCache(ctx, host, name)?.data;
          const target = (detail?.updates || [])[0];
          if (target) showUpdate(ctx, target);
          else ctx.navigate('#/updates');
        });
      });

      const deleteRemote = async (paths) => {
        if (!paths.length) return;
        const proceed = await ui.confirmDialog({
          title: 'Delete host-only paths?',
          message: 'These files are removed from the host only. The repo and every other stack file are untouched.',
          detail: `<span class="mono">${esc(paths.slice(0, 10).join('\n'))}${paths.length > 10 ? `\n… +${paths.length - 10}` : ''}</span>`,
          confirmLabel: 'Delete',
          danger: true,
          requireText: paths.length > 1 ? name : '',
        });
        if (!proceed) return;
        try {
          await ctx.api.post(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/remote-files/delete`, { paths });
          const cached = getCache(ctx, host, name);
          if (cached?.data?.remote_files) {
            cached.data.remote_files = cached.data.remote_files.filter((path) => !paths.includes(path));
          }
          ui.toast('Deleted from host', 'ok', paths.length === 1 ? paths[0] : `${paths.length} paths`);
          paint();
          ctx.refreshState({ force: true });
          setTimeout(() => { load({ force: true }); ctx.refreshState({ force: true }); }, 2500);
        } catch (error) {
          ui.toast('Could not delete host files', 'err', ui.errorText(error));
        }
      };
      page.querySelectorAll('[data-terminal-path]').forEach((button) => {
        button.addEventListener('click', () => openHostTerminal(ctx, host, button.dataset.terminalPath));
      });
      page.querySelectorAll('[data-delete-remote-path]').forEach((button) => {
        button.addEventListener('click', () => deleteRemote([button.dataset.deleteRemotePath]));
      });
      page.querySelector('[data-delete-remote-all]')?.addEventListener('click', () => {
        deleteRemote([...page.querySelectorAll('[data-delete-remote-path]')].map((button) => button.dataset.deleteRemotePath));
      });

      // file editor
      const editor = page.querySelector('#file-editor');
      const saveButton = page.querySelector('[data-action="save-file"]');
      const dirtyLabel = page.querySelector('#file-dirty');
      if (editor && saveButton) {
        const file = selectedFileContent();
        editor.addEventListener('input', () => {
          const dirty = editor.value !== (file?.content || '');
          saveButton.disabled = !dirty;
          if (dirtyLabel) dirtyLabel.style.display = dirty ? '' : 'none';
        });
        saveButton.addEventListener('click', () => saveFile(editor, file));
        page.querySelector('[data-action="revert-file"]')?.addEventListener('click', () => {
          if (file) {
            editor.value = file.content;
            saveButton.disabled = true;
            if (dirtyLabel) dirtyLabel.style.display = 'none';
          }
        });
        const active = document.activeElement;
        if (active && active.id === 'file-editor') {
          editor.focus();
          editor.setSelectionRange(editor.value.length, editor.value.length);
        }
      }
      page.querySelectorAll('[data-file]').forEach((button) => {
        button.addEventListener('click', async () => {
          const currentEditor = page.querySelector('#file-editor');
          const currentFile = selectedFileContent();
          if (currentEditor && currentFile && currentEditor.value !== currentFile.content) {
            const proceed = await ui.confirmDialog({
              title: 'Discard unsaved changes?',
              message: `${currentFile.path} has unsaved edits. Switching files will discard them.`,
              confirmLabel: 'Discard',
              danger: true,
            });
            if (!proceed) return;
          }
          setSelectedFile(ctx, host, name, button.dataset.file);
          paint();
        });
      });

      // diff
      page.querySelector('[data-action="check-diff"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        const original = button.innerHTML;
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.textContent = 'Checking…';
        try {
          const plan = await ctx.api.get(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/plan`);
          setPlan(ctx, host, name, plan);
          paint();
        } catch (error) {
          button.disabled = false;
          button.removeAttribute('aria-busy');
          button.innerHTML = original;
          ui.toast('Diff failed', 'err', ui.errorText(error));
        }
      });
      page.querySelector('[data-action="deploy-from-plan"]')?.addEventListener('click', () => showDeploy(ctx, host, name, 'up'));

      // logs
      page.querySelector('[data-action="load-logs"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        const tailButton = page.querySelector('[data-log-tail] .active');
        const tail = Number(tailButton?.dataset.tail || 100);
        if (!ctx.uiState.stackLogs) ctx.uiState.stackLogs = {};
        ctx.uiState.stackLogs[keyOf(host, name)] = { loading: true, tail, text: '' };
        button.disabled = true;
        try {
          const result = await ctx.api.get(`/api/stacks/${encodeURIComponent(host)}/${encodeURIComponent(name)}/logs?tail=${tail}`);
          ctx.uiState.stackLogs[keyOf(host, name)] = { loading: false, tail, text: result.text || '' };
          paint();
        } catch (error) {
          ctx.uiState.stackLogs[keyOf(host, name)] = { loading: false, tail, text: `error: ${ui.errorText(error)}` };
          paint();
        }
      });
      page.querySelectorAll('[data-log-tail] button').forEach((button) => {
        button.addEventListener('click', () => {
          const tail = Number(button.dataset.tail);
          const current = ctx.uiState.stackLogs?.[keyOf(host, name)];
          if (current) ctx.uiState.stackLogs[keyOf(host, name)] = { ...current, tail };
          else ctx.uiState.stackLogs = { ...(ctx.uiState.stackLogs || {}), [keyOf(host, name)]: { tail, text: '', loading: false } };
          paint();
        });
      });
      page.querySelector('[data-action="copy-logs"]')?.addEventListener('click', () => {
        const text = ctx.uiState.stackLogs?.[keyOf(host, name)]?.text || '';
        ui.copyText(text);
      });

      // snapshots
      page.querySelectorAll('[data-rollback]').forEach((button) => {
        button.addEventListener('click', () => showRollback(ctx, host, name, button.dataset.rollback));
      });
    };

    // initial content may be a skeleton; attach whichever exists
    attach();
    const cached = getCache(ctx, host, name);
    if (!cached || Date.now() - cached.fetchedAt > 15_000) {
      load();
    } else if (cached?.data) {
      // refresh the rendered cache age marker silently
    }
  },
};
