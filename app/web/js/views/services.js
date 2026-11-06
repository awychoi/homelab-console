import * as ui from '../ui.js';
import { pageHead, stateBanner, runtimePill, syncPill, stackHref } from './common.js';
import { showDeploy, showNewStack } from '../flows.js';

const { esc, icon } = ui;

function initFilter(ctx) {
  if (!ctx.uiState.servicesFilter) ctx.uiState.servicesFilter = { host: 'all', q: '' };
  return ctx.uiState.servicesFilter;
}

function layoutWarningsHtml(ctx) {
  const warnings = ctx.state?.layout_warnings || [];
  if (!warnings.length) return '';
  const shown = warnings.slice(0, 8);
  const reason = (kind) => kind === 'compose-in-host'
    ? 'compose file is directly in the host folder'
    : kind === 'compose-in-stacks-root'
      ? 'compose file is directly in stacks/'
      : 'stack directory is nested too deep';
  return `<div class="warn-box mb-2">${icon('alert')}<div>
    <strong>Ignored nested stack layout.</strong> The console only manages compose files at
    <span class="mono">stacks/&lt;host&gt;/&lt;stack&gt;/&lt;compose-file&gt;</span>. These files are on disk but are not stacks:
    <ul class="note-list mt-1">${shown.map((warning) => `<li><span class="mono">${esc(warning.path)}</span> — ${esc(reason(warning.kind))}</li>`).join('')}</ul>
    ${warnings.length > shown.length ? `<span class="tiny">…and ${warnings.length - shown.length} more.</span>` : ''}
    <span class="tiny">Move each one to the expected path, or keep it as a file inside a valid stack.</span>
  </div></div>`;
}

function visibleStacks(ctx) {
  const filter = initFilter(ctx);
  const query = filter.q.trim().toLowerCase();
  return (ctx.state?.stacks || [])
    .filter((row) => filter.host === 'all' || row.host === filter.host)
    .filter((row) => {
      if (!query) return true;
      const haystack = [row.name, row.host, row.project, ...(row.services || []).map((service) => `${service.name} ${service.image}`)].join(' ').toLowerCase();
      return haystack.includes(query);
    });
}

function rowHtml(row) {
  const runtime = row.runtime || {};
  const services = row.services || [];
  const images = services.filter((service) => service.image).slice(0, 2);
  const more = services.length - images.length;
  const updates = row.updates || [];
  const unpinned = row.unpinned || [];
  const imageParts = images.map((service) => {
    const target = (row.targets || []).find((item) => item.service === service.name || (item.services_affected || []).includes(service.name));
    const resolved = service.variable && target?.current_display && target.current_display !== '(not set)'
      ? `${service.repo}:${target.current_display}`
      : service.image;
    return { text: resolved, title: service.image };
  });
  const imageCell = imageParts.length
    ? imageParts.map((part) => `<span class="mono image-text" title="${esc(part.title)}">${esc(part.text)}</span>`).join('<br>')
    : '<span class="muted tiny">build only</span>';
  const compactImages = imageParts.length
    ? `${imageParts.map((part) => part.text).join(' · ')}${more > 0 ? ` · +${more} more` : ''}`
    : '';
  return `<tr class="clickable" data-stack-row data-host-name="${esc(row.host)}" data-stack-name="${esc(row.name)}">
    <td class="main-cell">
      <a class="stack-title" href="${stackHref(row.host, row.name)}">${esc(row.name)}</a>
      ${row.parse_error ? `<span class="sub text-bad">${esc(row.parse_error)}</span>` : `<span class="sub">${services.length} service${services.length === 1 ? '' : 's'}${row.project && row.project !== row.name ? ` · project ${esc(row.project)}` : ''}</span>`}
      ${compactImages ? `<span class="stack-images-compact mono">${esc(compactImages)}</span>` : ''}
    </td>
    <td data-label="Host"><div class="cell-value"><a class="mono host-link" href="#/host/${encodeURIComponent(row.host)}">${esc(row.host)}</a></div></td>
    <td data-label="Status"><div class="cell-value">${runtimePill(runtime.status)}</div></td>
    <td data-label="Sync"><div class="cell-value">${syncPill(runtime.sync?.state)}${runtime.uptime_pct !== null && runtime.uptime_pct !== undefined ? `<span class="sub">${runtime.uptime_pct}% 24h</span>` : ''}</div></td>
    <td class="col-images" data-label="Images"><div class="cell-value">
      ${imageCell}
      ${more > 0 ? `<span class="sub">+${more} more</span>` : ''}
    </div></td>
    <td data-label="Updates"><div class="cell-value">
      ${updates.length ? `<span class="chip warn">${icon('updates', 11)} ${updates.length} update${updates.length === 1 ? '' : 's'}</span>` : ''}
      ${unpinned.length ? `<span class="chip warn" title="no immutable tag">${unpinned.length} unpinned</span>` : ''}
      ${!updates.length && !unpinned.length ? '<span class="muted tiny">current</span>' : ''}
    </div></td>
    <td class="actions services-actions">
      <button class="btn ghost sm" data-deploy-host="${esc(row.host)}" data-deploy-name="${esc(row.name)}">${icon('upload', 12)} Deploy</button>
      <a class="btn ghost sm" href="${stackHref(row.host, row.name)}">Open</a>
    </td>
  </tr>`;
}

function rowsHtml(ctx) {
  const rows = visibleStacks(ctx);
  const filter = initFilter(ctx);
  if (!rows.length) {
    const filtered = filter.q.trim() || filter.host !== 'all';
    return `<tr><td colspan="7"><div class="empty empty-inline">
      <strong>No stacks match</strong><p>${filtered ? 'Clear the filters to see the whole repo again.' : 'Create a stack to get started.'}</p>
      <div class="btn-group mt-1">
        ${filtered ? `<button class="btn primary" data-clear-filters>Clear filters</button>` : `<button class="btn primary" data-new-stack>${icon('plus', 12)} New stack</button>`}
        ${filtered ? `<button class="btn ghost" data-new-stack>${icon('plus', 12)} New stack</button>` : ''}
      </div>
    </div></td></tr>`;
  }
  return rows.map(rowHtml).join('');
}

function remoteHtml(ctx) {
  const rows = ctx.state?.remote_stacks || [];
  const remoteRoot = String(ctx.bootstrap?.settings?.remote_root || '/opt/stacks').replace(/\/+$/, '');
  const nonEmpty = rows.filter((row) => !row.empty);
  const empty = rows.filter((row) => row.empty);
  if (!nonEmpty.length && !empty.length) return '';
  return `<div class="section">
    <div class="section-head"><div class="section-title">On hosts, not in the repo</div><div class="section-sub">unmanaged directories under ${esc(remoteRoot)} — pushes never delete them</div></div>
    <div class="card"><div class="table-wrap"><table class="data">
      <thead><tr><th>Host</th><th>Directory</th><th>Files</th><th>State</th></tr></thead>
      <tbody>${rows.map((row) => `<tr>
        <td class="mono">${esc(row.host)}</td>
        <td class="mono">${esc(row.name)}</td>
        <td class="muted mono tiny">${esc((row.files || []).slice(0, 4).join(', ') || '—')}${(row.files || []).length > 4 ? ` +${row.files.length - 4}` : ''}</td>
        <td>${row.empty ? '<span class="chip warn">empty directory</span>' : '<span class="chip">unmanaged</span>'}</td>
      </tr>`).join('')}</tbody>
    </table></div></div>
  </div>`;
}

function contentHtml(ctx) {
  const filter = initFilter(ctx);
  const hosts = ctx.bootstrap?.hosts || ctx.state?.hosts || [];
  const allRows = ctx.state?.stacks || [];
  const summary = ctx.state?.summary || {};
  const counts = {};
  for (const row of allRows) counts[row.host] = (counts[row.host] || 0) + 1;
  const drift = (summary.drift || []).length;
  return `${pageHead({
    title: 'Stacks',
    subtitle: `<strong>${allRows.length}</strong> stacks<span class="dot">·</span><strong>${summary.updates_available || 0}</strong> update${summary.updates_available === 1 ? '' : 's'} pending<span class="dot">·</span>${drift ? `${drift} drift` : 'no drift'}`,
    actions: `<button class="btn primary sm" data-new-stack>${icon('plus', 12)} New stack</button>`,
  })}
  ${stateBanner(ctx)}
  ${layoutWarningsHtml(ctx)}
  <div class="toolbar">
    <div class="segmented" data-host-filter aria-label="Filter stacks by host">
      <button type="button" class="${filter.host === 'all' ? 'active' : ''}" data-host="all" aria-pressed="${filter.host === 'all'}">All hosts <span class="mono">${allRows.length}</span></button>
      ${hosts.filter((host) => !host.local || counts[host.alias]).map((host) => `<button type="button" class="${filter.host === host.alias ? 'active' : ''}" data-host="${esc(host.alias)}" aria-pressed="${filter.host === host.alias}">${esc(host.alias)} <span class="mono">${counts[host.alias] || 0}</span></button>`).join('')}
    </div>
    <div class="spacer"></div>
    <div class="search">${icon('search')}<input id="service-search" type="search" placeholder="Filter stacks or services…" aria-label="Filter stacks or services" value="${esc(filter.q)}" autocomplete="off" spellcheck="false"></div>
  </div>
  <div class="card"><div class="table-wrap"><table class="data services-table">
    <caption class="sr-only">Stacks managed by this workspace</caption>
    <thead><tr><th>Stack</th><th>Host</th><th>Status</th><th>Sync</th><th class="col-images">Images</th><th>Updates</th><th class="actions"></th></tr></thead>
    <tbody id="service-rows">${rowsHtml(ctx)}</tbody>
  </table></div></div>
  ${remoteHtml(ctx)}`;
}

function bindRows(root, ctx) {
  root.querySelectorAll('[data-stack-row]').forEach((row) => {
    row.addEventListener('click', (event) => {
      if (event.target.closest('a,button')) return;
      ctx.navigate(stackHref(row.dataset.hostName, row.dataset.stackName));
    });
  });
  root.querySelectorAll('[data-deploy-host]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      showDeploy(ctx, button.dataset.deployHost, button.dataset.deployName, 'up');
    });
  });
}

export default {
  render(ctx) {
    return contentHtml(ctx);
  },
  mount(root, ctx) {
    if (!ctx.state?.stacks) return;
    const filter = initFilter(ctx);
    const tbody = root.querySelector('#service-rows');
    const search = root.querySelector('#service-search');

    const syncHostButtons = () => {
      root.querySelectorAll('[data-host-filter] button').forEach((node) => {
        const active = node.dataset.host === filter.host;
        node.classList.toggle('active', active);
        node.setAttribute('aria-pressed', String(active));
      });
    };

    const bindNewStack = (scope = root) => {
      scope.querySelectorAll('[data-new-stack]').forEach((button) => {
        button.addEventListener('click', () => showNewStack(ctx));
      });
    };

    const renderRows = () => {
      tbody.innerHTML = rowsHtml(ctx);
      bindRows(root, ctx);
      bindNewStack(tbody);
      root.querySelectorAll('[data-clear-filters]').forEach((button) => {
        button.addEventListener('click', () => {
          filter.host = 'all';
          filter.q = '';
          if (search) search.value = '';
          syncHostButtons();
          renderRows();
          search?.focus();
        });
      });
    };

    bindNewStack();
    renderRows();

    root.querySelectorAll('[data-host-filter] button').forEach((button) => {
      button.addEventListener('click', () => {
        filter.host = button.dataset.host;
        syncHostButtons();
        renderRows();
      });
    });

    search?.addEventListener('input', () => {
      filter.q = search.value;
      renderRows();
    });
    search?.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && search.value) {
        event.preventDefault();
        filter.q = '';
        search.value = '';
        renderRows();
      }
    });

    const active = document.activeElement;
    if (active && active.id === 'service-search') {
      active.focus();
      active.setSelectionRange(active.value.length, active.value.length);
    }
  },
};
