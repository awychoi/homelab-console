import * as ui from '../ui.js';
import {
  pageHead, statTile, emptyState, stateBanner, runtimePill, syncPill, updateBadge,
  tempTone, memTone, diskTone, loadTone, toneClass, bar, throttleChips, tailscaleChip, stackHref, containerStatePill,
} from './common.js';
import { showEditHost, showRemoveHost } from '../flows.js';

const { esc, icon } = ui;

export default {
  render(ctx, params) {
    const alias = params?.[0];
    const host = (ctx.state?.hosts || []).find((item) => item.alias === alias);
    if (!host) {
      return `${pageHead({ title: alias || 'Host', subtitle: 'Not in the fleet snapshot', back: { href: '#/fleet', label: 'Fleet' } })}
        ${emptyState('hosts', 'Host not found', 'It may have been removed, or the fleet snapshot is still warming up.')}`;
    }
    const summary = host.summary || {};
    const data = host.data || {};
    const stacks = (ctx.state?.stacks || []).filter((row) => row.host === alias);
    const temp = summary.temp_c;
    const load = (summary.load || [])[0];
    const loadPct = load !== undefined && data.cpu_count ? Math.min(100, (Number(load) / data.cpu_count) * 100) : null;
    const containers = data.containers || [];
    const disks = data.disks || [];
    const tailscale = summary.tailscale || {};
    const throttle = summary.throttled || {};
    const rootDisk = disks.find((disk) => disk.mountpoint === '/');
    const uptimePct = host.history?.uptime_pct;
    const actions = `
      <button class="btn ghost sm" data-action="test-host">${icon('wifi', 12)} Test SSH</button>
      <button class="btn ghost sm" data-action="edit-host">${icon('edit', 12)} Edit</button>
      <button class="btn ghost sm" data-action="copy-ssh">${icon('copy', 12)} Copy ssh</button>
      <span class="action-sep"></span>
      <button class="btn danger sm" data-action="remove-host">${icon('trash', 12)} Remove</button>`;

    const throttleFlags = [...(throttle.now || []).map((flag) => ({ flag, now: true })), ...(throttle.since_boot || []).map((flag) => ({ flag, now: false }))];
    const uptimePoints = host.history_points || [];

    return `
      ${pageHead({
        title: alias,
        subtitle: `${esc(host.address)}${host.port !== 22 ? `:${host.port}` : ''} ${summary.hostname && summary.hostname !== alias ? `· <span class="mono">${esc(summary.hostname)}</span>` : ''}${host.role ? ` · ${esc(host.role)}` : host.notes ? ` · ${esc(host.notes)}` : ''}`,
        back: { href: '#/fleet', label: 'Fleet' },
        actions,
        badges: host.online ? (host.status === 'degraded' ? ui.pill('warn', 'degraded') : ui.pill('ok', 'online')) : ui.pill('bad', 'offline'),
      })}
      ${stateBanner(ctx)}
      ${!host.online ? `<div class="danger-box mb-2">${icon('alert')}<div><strong>SSH probe failed.</strong> ${esc(host.error || 'The host did not answer.')} ${host.history?.last_ts ? `Last seen ${esc(ui.ago(host.history.last_ts))}.` : ''}</div></div>` : ''}
      <div class="grid cols-4">
        ${statTile({ label: 'Uptime', value: summary.uptime || '—', sub: host.online ? `24h availability ${uptimePct === null || uptimePct === undefined ? 'collecting…' : `${uptimePct}%`}` : 'last seen ' + ui.ago(host.collected_at), iconName: 'clock', toneName: host.online ? 'ok' : 'warn' })}
        ${statTile({ label: 'CPU temperature', value: temp === null || temp === undefined ? '—' : `${Number(temp).toFixed(1)}°C`, sub: `${esc(summary.model || data.model || '')}`, iconName: 'thermo', toneName: tempTone(temp) || 'ok', valueTone: toneClass(tempTone(temp)) })}
        ${statTile({ label: 'Memory', value: ui.pct(summary.mem_pct), sub: `${ui.bytes(summary.mem_used)} of ${ui.bytes(summary.mem_total)} used`, iconName: 'memory', toneName: memTone(summary.mem_pct) || 'ok', valueTone: toneClass(memTone(summary.mem_pct)) })}
        ${statTile({ label: 'Root disk', value: ui.pct(summary.disk_pct), sub: rootDisk?.size ? `${esc(rootDisk.used)} of ${esc(rootDisk.size)}` : 'no root mount', iconName: 'disk', toneName: diskTone(summary.disk_pct) || 'ok', valueTone: toneClass(diskTone(summary.disk_pct)) })}
      </div>

      <div class="grid cols-2 section">
        <div class="card">
          <div class="card-head"><div class="card-title">24h telemetry</div><div class="card-sub">temperature and reachability</div><div class="card-actions"><a class="btn ghost sm" href="#/activity">Activity</a></div></div>
          <div class="card-body">
            <div class="mini-label">CPU temperature · last 24h</div>
            ${ui.sparkline(uptimePoints, 'temp_c', { height: 46, color: 'var(--accent)', label: `CPU temperature for ${alias}, last 24 hours` }) || '<div class="muted tiny">collecting samples…</div>'}
            <div class="mini-label mt-2">Host reachability</div>
            ${ui.uptimeStrip(uptimePoints.map((point) => ({ ...point, status: point.reachable ? 'up' : 'down' })), 'status', `${alias} reachability, last 24 hours`)}
            <div class="legend"><span><i style="background:var(--accent)"></i>reachable</span><span><i style="background:var(--red)"></i>offline</span><span class="mono">${uptimePoints.length} samples</span></div>
            <div class="sep-line"></div>
            <div class="bars">
              ${bar(summary.mem_pct, 'mem', memTone(summary.mem_pct))}
              ${bar(summary.disk_pct, 'disk', diskTone(summary.disk_pct))}
              ${bar(loadPct, 'cpu', loadTone(loadPct))}
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><div class="card-title">Power & throttling</div><div class="card-sub">vcgencmd get_throttled</div></div>
          <div class="card-body">
            ${throttle.supported === false ? `<div class="info-box">${icon('info')}<div>No <span class="mono">vcgencmd</span> on this host (not a Raspberry Pi or firmware tools absent).</div></div>`
              : `<div class="flex items-center gap-2 mb-2">
                  <span class="mono throttle-raw ${throttle.ok === false ? 'text-bad' : 'text-ok'}">${esc(throttle.raw || '0x0')}</span>
                  ${throttle.ok === false ? ui.pill('bad', 'throttling now') : ui.pill('ok', 'clean')}
                </div>
                <div class="chips">
                  ${throttleFlags.length ? throttleFlags.map((item) => `<span class="chip ${item.now ? 'bad' : 'warn'}">${esc(item.flag)}${item.now ? ' · now' : ' · since boot'}</span>`).join('') : '<span class="chip good">no under-voltage / thermal flags</span>'}
                </div>
                <div class="hint mt-2">0x0 means no under-voltage, frequency capping, throttling or soft temperature limit has occurred since boot.</div>`}
            <div class="sep-line"></div>
            <div class="kv">
              <div class="kv-row"><div class="kv-key">Power state</div><div class="kv-val">${throttle.ok === false ? ui.pill('bad', 'degraded power') : ui.pill('ok', 'stable')}</div></div>
              <div class="kv-row"><div class="kv-key">NTP synced</div><div class="kv-val">${summary.ntp_synced === true ? ui.pill('ok', 'yes') : summary.ntp_synced === false ? ui.pill('warn', 'no') : '<span class="muted">unknown</span>'}</div></div>
              <div class="kv-row"><div class="kv-key">Reboot needed</div><div class="kv-val">${summary.reboot_required ? ui.pill('warn', 'yes') : '<span class="muted">no</span>'}</div></div>
              <div class="kv-row"><div class="kv-key">Failed units</div><div class="kv-val mono">${esc((summary.failed_units || []).join(', ') || 'none')}</div></div>
            </div>
          </div>
        </div>
      </div>

      <div class="section">
        <div class="section-head"><div class="section-title">Containers</div><div class="section-sub">${ui.plural(containers.length, 'container')} on this host</div><div class="right"><span class="chips">${throttleChips(summary)} ${tailscaleChip(summary)}</span></div></div>
        <div class="card"><div class="table-wrap"><table class="data">
          <thead><tr><th>Container</th><th>Service</th><th>Image</th><th>State</th><th class="right">Uptime</th><th class="right">Restarts</th><th>Digest</th></tr></thead>
          <tbody>${containers.length ? containers.map((container) => `<tr>
            <td class="mono cell-strong">${esc(container.name)}</td>
            <td class="muted">${esc(container.service || container.project || '—')}</td>
            <td><span class="mono image-text">${esc(container.image)}</span></td>
            <td>${containerStatePill(container)}${container.oom_killed ? ' <span class="chip bad">OOM</span>' : ''}</td>
            <td class="num">${esc(container.state === 'running' ? ui.duration(container.uptime_s) : ui.duration(container.stopped_s))}</td>
            <td class="num">${esc(container.restart_count ?? 0)}</td>
            <td class="mono dim digest-text">${esc(ui.shortSha(container.digest, 12) || '—')}</td>
          </tr>`).join('') : '<tr><td colspan="7" class="table-empty">No containers reported.</td></tr>'}</tbody>
        </table></div></div>
      </div>

      <div class="section">
        <div class="section-head"><div class="section-title">Stacks on ${esc(alias)}</div><div class="section-sub">repo-managed services</div><div class="right"><a class="btn ghost sm" href="#/services">All services</a></div></div>
        <div class="card"><div class="table-wrap"><table class="data">
          <thead><tr><th>Stack</th><th>Status</th><th>Sync</th><th>Services</th><th>Updates</th><th class="actions"></th></tr></thead>
          <tbody>${stacks.length ? stacks.map((row) => `<tr class="clickable" data-stack="${esc(row.name)}">
            <td class="main-cell"><a class="stack-title" href="${stackHref(row.host, row.name)}">${esc(row.name)}</a>${row.parse_error ? `<span class="sub text-bad">${esc(row.parse_error)}</span>` : ''}</td>
            <td>${runtimePill(row.runtime?.status)}</td>
            <td>${syncPill(row.runtime?.sync?.state)}</td>
            <td class="muted">${row.services?.length || 0}</td>
            <td>${updateBadge(row) || '<span class="muted tiny">none</span>'}</td>
            <td class="actions"><a class="btn ghost sm" href="${stackHref(row.host, row.name)}">Open</a></td>
          </tr>`).join('') : '<tr><td colspan="6" class="table-empty">No stacks for this host.</td></tr>'}</tbody>
        </table></div></div>
      </div>

      <div class="grid cols-2 section">
        <div class="card">
          <div class="card-head"><div class="card-title">Storage</div><div class="card-sub">df, tmpfs excluded</div></div>
          <div class="table-wrap"><table class="data"><thead><tr><th>Mount</th><th class="right">Used</th><th class="right">Size</th><th class="right">Use</th><th>FS</th></tr></thead>
            <tbody>${disks.length ? disks.map((disk) => `<tr><td class="mono">${esc(disk.mountpoint)}</td><td class="num">${esc(disk.used)}</td><td class="num">${esc(disk.size)}</td><td class="num ${toneClass(diskTone(disk.percent))}">${esc(disk.percent)}%</td><td class="muted mono">${esc(disk.filesystem_type || '')}</td></tr>`).join('') : '<tr><td colspan="5" class="table-empty">No mounts.</td></tr>'}</tbody>
          </table></div>
        </div>
        <div class="card">
          <div class="card-head"><div class="card-title">System</div><div class="card-sub">software and substrate</div></div>
          <div class="card-body"><div class="kv">
            <div class="kv-row"><div class="kv-key">Model</div><div class="kv-val">${esc(summary.model || '—')}</div></div>
            <div class="kv-row"><div class="kv-key">OS / kernel</div><div class="kv-val">${esc(summary.os || '—')}<br><span class="mono tiny">${esc(summary.kernel || '')} · ${esc(summary.arch || '')}</span></div></div>
            <div class="kv-row"><div class="kv-key">Docker / Compose</div><div class="kv-val mono">${esc(summary.docker_version || '—')} · ${esc(summary.compose_version || '—')}</div></div>
            <div class="kv-row"><div class="kv-key">Tailscale</div><div class="kv-val">${tailscale.installed ? `${esc(tailscale.hostname || '')} · <span class="mono">${esc(tailscale.ipv4 || '')}</span> · v${esc(tailscale.version || '')}` : '<span class="muted">not installed</span>'}</div></div>
            <div class="kv-row"><div class="kv-key">RAID</div><div class="kv-val">${(data.raid || []).length ? data.raid.map((raid) => `<span class="mono">${esc(raid.name)} ${esc(raid.flags || '')}</span>`).join('<br>') : '<span class="muted">none</span>'}</div></div>
            <div class="kv-row"><div class="kv-key">Listening ports</div><div class="kv-val mono tiny">${esc((data.listeners || []).slice(0, 14).join(', '))}${(data.listeners || []).length > 14 ? ` +${data.listeners.length - 14}` : ''}</div></div>
            <div class="kv-row"><div class="kv-key">SSH target</div><div class="kv-val mono">${esc(host.user)}@${esc(host.address)}:${esc(host.port)}</div></div>
            <div class="kv-row"><div class="kv-key">Key</div><div class="kv-val mono tiny">${esc(host.key || 'agent / ~/.ssh/config')}</div></div>
          </div></div>
        </div>
      </div>`;
  },

  mount(root, ctx, params) {
    const alias = params?.[0];
    const host = (ctx.state?.hosts || []).find((item) => item.alias === alias);
    if (!host) return;
    root.querySelector('[data-action="test-host"]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await ctx.api.post(`/api/hosts/${encodeURIComponent(alias)}/test`, {});
        const ok = result.result?.ok;
        ui.toast(ok ? 'SSH works' : 'SSH failed', ok ? 'ok' : 'warn', ok ? `${result.result.hostname} · ${result.result.latency_ms} ms` : result.result?.error);
        if (ok) ctx.refreshState({ force: true });
      } catch (error) {
        ui.toast('Test failed', 'err', ui.errorText(error));
      } finally {
        button.disabled = false;
      }
    });
    root.querySelector('[data-action="edit-host"]')?.addEventListener('click', () => showEditHost(ctx, host));
    root.querySelector('[data-action="remove-host"]')?.addEventListener('click', () => showRemoveHost(ctx, host));
    root.querySelector('[data-action="copy-ssh"]')?.addEventListener('click', () => ui.copyText(`ssh ${alias}`));
    root.querySelectorAll('[data-stack]').forEach((row) => {
      row.addEventListener('click', (event) => {
        if (event.target.closest('a,button')) return;
        ctx.navigate(stackHref(alias, row.dataset.stack));
      });
    });
  },
};
