import * as ui from '../ui.js';
import { openHostTerminal } from '../terminal.js';
import {
  pageHead, statTile, emptyState, stateBanner,
  tempTone, memTone, diskTone, loadTone, toneClass, bar, throttleChips, tailscaleChip, hostHref, stackHref,
} from './common.js';

const { esc, icon } = ui;

function hostCard(host) {
  const summary = host.summary || {};
  const offline = !host.online;
  const temp = summary.temp_c;
  const tempLabel = temp === null || temp === undefined ? '—' : `${Number(temp).toFixed(1)}°C`;
  const load = (summary.load || [])[0];
  const loadPct = load !== undefined && host.data?.cpu_count ? Math.min(100, (Number(load) / host.data.cpu_count) * 100) : null;
  const memPct = summary.mem_pct;
  const diskPct = summary.disk_pct;
  const sparks = ui.sparkline(host.history_points || [], 'temp_c', {
    height: 26, color: 'var(--accent)', label: `CPU temperature for ${host.alias}, last 24 hours`,
  });
  const updateCount = summary.updates || 0;
  const badCount = summary.containers_bad || 0;
  return `<div class="card host-card ${offline ? 'offline' : ''}">
    <div class="host-top">
      <div class="status-dot ${offline ? 'bad' : host.status === 'degraded' ? 'warn' : 'ok'}"></div>
      <div class="host-id">
        <div class="host-name"><a href="${hostHref(host.alias)}">${esc(host.alias)}</a>${host.local ? '<span class="pill tiny neutral">local</span>' : ''}</div>
        <div class="host-meta">${esc(host.address)}${host.port && host.port !== 22 ? `:${host.port}` : ''}${summary.hostname && summary.hostname !== host.alias ? ` · ${esc(summary.hostname)}` : ''}</div>
        <div class="host-role">${esc(host.role || host.notes || summary.model || 'no role set')}</div>
      </div>
      <div class="host-actions">
        <a class="btn ghost sm" href="${hostHref(host.alias)}">Open ${icon('chevron', 11)}</a>
      </div>
    </div>
    ${offline ? `<div class="host-stats"><div class="host-stat"><div class="k">Status</div><div class="v text-bad value-sm">unreachable</div></div>
      <div class="host-stat"><div class="k">Last seen</div><div class="v value-sm">${esc(ui.ago(host.history?.last_ts))}</div></div>
      <div class="host-stat"><div class="k">Containers</div><div class="v">${summary.containers_running || 0}<small>/${summary.containers_total || 0}</small></div></div></div>
      <div class="host-foot"><div class="host-tags"><span class="tiny text-bad">${esc(host.error || 'SSH unreachable')}</span></div></div>`
    : `<div class="host-stats">
        <div class="host-stat"><div class="k">CPU temp</div><div class="v ${toneClass(tempTone(temp))}">${tempLabel}</div></div>
        <div class="host-stat"><div class="k">Uptime</div><div class="v value-sm">${esc(summary.uptime || '—')}</div></div>
        <div class="host-stat"><div class="k">Containers</div><div class="v">${summary.containers_running || 0}<small>/${summary.containers_total || 0}</small></div></div>
      </div>
      ${sparks ? `<div class="host-spark">${sparks}</div>` : ''}
      <div class="bars">
        ${bar(memPct, 'mem', memTone(memPct))}
        ${bar(diskPct, 'disk', diskTone(diskPct))}
        ${bar(loadPct, 'cpu', loadTone(loadPct))}
      </div>
      <div class="host-foot">
        <div class="host-tags">
          ${throttleChips(summary)}
          ${summary.reboot_required ? '<span class="chip warn">reboot required</span>' : ''}
          ${badCount ? `<span class="chip bad">${badCount} bad container${badCount === 1 ? '' : 's'}</span>` : ''}
          ${updateCount ? `<a class="chip warn" href="#/updates">${icon('updates', 11)} ${updateCount} update${updateCount === 1 ? '' : 's'}</a>` : ''}
          ${tailscaleChip(summary)}
        </div>
        <span class="host-collected tiny dim mono">${esc(ui.ago(host.collected_at))}</span>
      </div>`}
  </div>`;
}

function attentionItems(ctx) {
  const state = ctx.state;
  const items = [];
  const remoteRoot = String(ctx.bootstrap?.settings?.remote_root || '/opt/stacks').replace(/\/+$/, '');
  for (const host of state.hosts || []) {
    if (!host.online) items.push({ tone: 'bad', title: `${host.alias} unreachable`, detail: host.error || 'SSH failed', href: hostHref(host.alias) });
    else if (host.problems?.length) items.push({ tone: 'warn', title: `${host.alias} degraded`, detail: host.problems.join(' · '), href: hostHref(host.alias) });
  }
  for (const exception of state.exceptions || []) {
    items.push({
      tone: exception.state === 'running' ? 'warn' : 'bad',
      title: `${exception.host} · ${exception.container || exception.service}`,
      detail: `${exception.state}${exception.health ? ` / ${exception.health}` : ''}${exception.oom_killed ? ' · OOM-killed' : ''}`,
      href: exception.stack && exception.stack !== 'unmanaged' ? stackHref(exception.host, exception.stack) : '#/services',
    });
  }
  for (const key of state.summary?.drift || []) {
    const [host, name] = key.split('/');
    items.push({ tone: 'warn', title: `${key} drift`, detail: 'repo files differ from the host', href: stackHref(host, name) });
  }
  for (const key of state.summary?.remote_extra || []) {
    const [host, name] = key.split('/');
    items.push({
      tone: 'info',
      title: `${key} extra files`,
      detail: 'host-only paths, left untouched by deploy',
      href: stackHref(host, name),
      tab: 'files',
    });
  }
  for (const key of state.summary?.remote_empty_dirs || []) {
    const [host, name] = key.split('/');
    items.push({
      tone: 'info',
      title: `${key} empty dir`,
      detail: 'empty directory on the host; open a terminal to inspect or remove it',
      href: null,
      terminal: { host, path: `${remoteRoot}/${name}` },
    });
  }
  for (const entry of state.updates || []) {
    if (entry.status !== 'check-failed' || !entry.error) continue;
    items.push({ tone: 'warn', title: `${entry.host}/${entry.stack} release check failed`, detail: entry.error, href: '#/updates' });
  }
  return items;
}

function attention(ctx, items) {
  if (!items.length) {
    return `<div class="card"><div class="card-body attention-empty">
      <span class="status-dot ok"></span><span class="muted">No exceptions, drift or unreachable hosts right now.</span>
    </div></div>`;
  }
  const visible = items.slice(0, 12);
  return `<div class="card">
    <ul class="attention-list">
      ${visible.map((item) => {
        const stackKey = item.href ? item.href.replace(/^#\/stack\//, '').split('/').map(decodeURIComponent).join('/') : '';
        const tabAttrs = item.tab ? `data-stack-tab="${esc(item.tab)}" data-stack-key="${esc(stackKey)}"` : '';
        return `<li class="attention-item ${item.href ? 'clickable' : ''}" ${item.href ? `data-href="${esc(item.href)}"` : ''} ${tabAttrs}>
        <span class="status-dot ${item.tone === 'bad' ? 'bad' : item.tone === 'warn' ? 'warn' : ''}"></span>
        <div class="attention-text">
          <span class="attention-title">${esc(item.title)}</span>
          <span class="attention-detail">${esc(item.detail)}</span>
        </div>
        ${item.terminal ? `<button class="btn ghost sm attention-open" type="button" data-terminal-host="${esc(item.terminal.host)}" data-terminal-path="${esc(item.terminal.path)}">Terminal</button>` : ''}
        ${item.href ? `<a class="btn ghost sm attention-open" href="${esc(item.href)}" ${tabAttrs}>Open</a>` : ''}
      </li>`;
      }).join('')}
    </ul>
    ${items.length > 12 ? `<div class="card-foot muted tiny">+${items.length - 12} more in Stacks and Updates</div>` : ''}
  </div>`;
}

export default {
  render(ctx) {
    const state = ctx.state;
    const summary = state.summary || {};
    if (state.warming_up) {
      return `${pageHead({ title: 'Fleet', subtitle: 'Collecting telemetry from every host…' })}
        ${emptyState('activity', 'Warming up', 'The first SSH collection is in flight. This usually takes a few seconds.')}`;
    }
    if (!state.hosts?.length) {
      return `${pageHead({ title: 'Fleet', subtitle: 'No hosts configured yet', actions: '<a class="btn primary" href="#/hosts">Add your first Pi</a>' })}
        ${emptyState('hosts', 'No hosts yet', 'Point the console at a Pi: it guides you through fingerprint verification, ed25519 key install and connection testing.',
          '<a class="btn primary mt-2" href="#/hosts">Open host onboarding</a>')}`;
    }
    const hosts = state.hosts;
    const tempValues = hosts.map((host) => host.summary?.temp_c).filter((value) => value !== null && value !== undefined);
    const hottest = tempValues.length ? Math.max(...tempValues) : null;
    const hottestHost = hottest === null ? null : hosts.find((host) => host.summary?.temp_c === hottest);
    const attentionList = attentionItems(ctx);
    const containerWord = summary.containers_running === 1 ? 'container' : 'containers';
    const updateWord = summary.updates_available === 1 ? 'update' : 'updates';
    const actions = `
      <a class="btn ghost sm" href="#/services">${icon('services', 12)} Stacks</a>`;
    return `
      ${pageHead({
        title: 'Fleet',
        subtitle: `<strong>${summary.hosts_online}/${summary.hosts_total}</strong> nodes online<span class="dot">·</span><strong>${summary.containers_running}</strong> ${containerWord} running<span class="dot">·</span><strong>${summary.updates_available || 0}</strong> ${updateWord} available`,
        actions,
        badges: summary.hosts_offline?.length ? ui.pill('bad', `${summary.hosts_offline.length} offline`) : ui.pill('ok', 'all reachable'),
      })}
      ${stateBanner(ctx)}
      <div class="grid cols-4 mb-2">
        ${statTile({ label: 'Hosts online', value: `${summary.hosts_online}<small> / ${summary.hosts_total}</small>`, sub: summary.hosts_offline?.length ? `${esc(summary.hosts_offline.join(', '))} unreachable` : 'all SSH probes answered', iconName: 'hosts', toneName: summary.hosts_offline?.length ? 'bad' : 'ok', valueTone: summary.hosts_offline?.length ? 'text-bad' : '' })}
        ${statTile({ label: 'Containers', value: `${summary.containers_running}<small> / ${summary.containers_total}</small>`, sub: summary.exceptions ? ui.plural(summary.exceptions, 'exception') : 'all running', iconName: 'box', toneName: summary.exceptions ? 'warn' : 'ok', valueTone: summary.exceptions ? 'text-warn' : '' })}
        ${statTile({ label: 'Updates available', value: String(summary.updates_available || 0), sub: summary.updates_available ? `${ui.plural(summary.updates_available, 'stable release')} ready to review` : 'pins are current', iconName: 'updates', toneName: summary.updates_available ? 'warn' : 'ok', valueTone: summary.updates_available ? 'text-warn' : '' })}
        ${statTile({ label: 'Hottest Pi', value: hottest === null ? '—' : `${hottest.toFixed(1)}°C`, sub: hottest === null ? 'no temperature samples yet' : tempTone(hottest) ? `${esc(hottestHost?.alias || 'a host')} is above the warm threshold` : 'all Pis within the safe range', iconName: 'thermo', toneName: tempTone(hottest) || 'ok', valueTone: toneClass(tempTone(hottest)) })}
      </div>
      <div class="host-grid">${hosts.map(hostCard).join('')}</div>
      <div class="section">
        <div class="section-head"><div class="section-title">Attention &amp; notes${attentionList.length ? ` <span class="count">${attentionList.length}</span>` : ''}</div><div class="section-sub">health, drift and unmanaged host state</div></div>
        ${attention(ctx, attentionList)}
      </div>`;
  },
  mount(root, ctx) {
    root.querySelectorAll('[data-stack-tab]').forEach((node) => {
      node.addEventListener('click', () => {
        if (!ctx.uiState.stackTabs) ctx.uiState.stackTabs = {};
        ctx.uiState.stackTabs[node.dataset.stackKey] = node.dataset.stackTab;
      });
    });
    root.querySelectorAll('[data-terminal-host]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        openHostTerminal(ctx, button.dataset.terminalHost, button.dataset.terminalPath);
      });
    });
    root.querySelectorAll('[data-href]').forEach((row) => {
      row.addEventListener('click', (event) => {
        if (event.target.closest('a,button')) return;
        ctx.navigate(row.dataset.href);
      });
    });
  },
};
