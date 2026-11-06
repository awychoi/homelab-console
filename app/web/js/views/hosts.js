import * as ui from '../ui.js';
import { pageHead, stateBanner, emptyState, hostHref } from './common.js';
import { showAddHost, showEditHost, showRemoveHost } from '../flows.js';

const { esc, icon } = ui;

function hostCard(ctx, host) {
  const fleet = (ctx.state?.hosts || []).find((item) => item.alias === host.alias) || {};
  const stacks = (ctx.state?.stacks || []).filter((row) => row.host === host.alias);
  const ssh = host.ssh_config || {};
  const key = host.key_info;
  const online = fleet.online;
  return `<div class="card">
    <div class="host-top">
      <span class="status-dot ${online ? (fleet.status === 'degraded' ? 'warn' : 'ok') : 'bad'}"></span>
      <div>
        <div class="host-name"><a href="${hostHref(host.alias)}">${esc(host.alias)}</a>
          ${host.local ? '<span class="pill tiny neutral">local</span>' : ''}
          ${host.tags?.map((tag) => `<span class="chip">${esc(tag)}</span>`).join('') || ''}
        </div>
        <div class="host-meta">${host.local ? 'controller workstation' : `${esc(host.user)}@${esc(host.address)}:${esc(host.port)}`}</div>
        <div class="host-role">${esc(host.role || host.notes || (online ? 'reachable' : 'not reachable'))}</div>
      </div>
      <div class="host-actions">
        <button class="btn ghost sm icon" type="button" data-test="${esc(host.alias)}" title="Test SSH" aria-label="Test SSH for ${esc(host.alias)}">${icon('wifi', 13)}</button>
        <button class="btn ghost sm icon" type="button" data-edit="${esc(host.alias)}" title="Edit host" aria-label="Edit ${esc(host.alias)}">${icon('edit', 13)}</button>
        <button class="btn ghost sm icon" type="button" data-remove="${esc(host.alias)}" title="Remove host" aria-label="Remove ${esc(host.alias)}">${icon('trash', 13)}</button>
      </div>
    </div>
    <div class="card-body flush-top">
      <div class="kv">
        <div class="kv-row"><div class="kv-key">Fleet status</div><div class="kv-val">${online ? (fleet.status === 'degraded' ? ui.pill('warn', 'degraded') : ui.pill('ok', 'online')) : ui.pill('bad', 'offline')} ${fleet.error ? `<span class="tiny muted">${esc(fleet.error.slice(0, 90))}</span>` : ''}</div></div>
        <div class="kv-row"><div class="kv-key">Stacks</div><div class="kv-val">${stacks.length ? stacks.map((row) => `<a class="chip" href="#/stack/${encodeURIComponent(host.alias)}/${encodeURIComponent(row.name)}">${esc(row.name)}</a>`).join(' ') : '<span class="muted">none in repo</span>'}</div></div>
        <div class="kv-row"><div class="kv-key">ssh config</div><div class="kv-val mono tiny">${ssh.hostname ? `HostName ${esc(ssh.hostname)} · User ${esc(ssh.user || '')}${ssh.port ? ` · Port ${esc(ssh.port)}` : ''}` : '<span class="muted">no stanza found</span>'}</div></div>
        <div class="kv-row"><div class="kv-key">Identity</div><div class="kv-val mono tiny">${key ? `${esc(key.type)} · ${esc(key.fingerprint)}` : host.key ? esc(host.key) : ctx.bootstrap?.ssh?.default_key ? `default · ${esc(ctx.bootstrap.ssh.default_key.fingerprint)}` : '<span class="muted">agent / default</span>'}</div></div>
      </div>
    </div>
    <div class="host-foot">
      <div class="host-tags">
        ${online ? `<span class="chip good">${esc(fleet.summary?.hostname || host.alias)}</span><span class="chip">${esc(fleet.summary?.uptime || '—')} up</span>${fleet.summary?.temp_c !== null && fleet.summary?.temp_c !== undefined ? `<span class="chip">${fleet.summary.temp_c.toFixed(1)}°C</span>` : ''}` : '<span class="chip bad">unreachable</span>'}
      </div>
      <div class="host-foot-actions">
        <button class="btn ghost sm icon" type="button" data-copy-ssh="${esc(host.alias)}" aria-label="Copy SSH command for ${esc(host.alias)}" title="Copy ssh ${esc(host.alias)}">${icon('copy', 12)}</button>
        <a class="btn ghost sm" href="${hostHref(host.alias)}">Open ${icon('chevron', 11)}</a>
      </div>
    </div>
  </div>`;
}

function keysPanel(ctx) {
  const ssh = ctx.bootstrap?.ssh || {};
  const keys = ssh.keys || [];
  return `<div class="card">
    <div class="card-head"><div class="card-title">SSH identities</div><div class="card-sub">${esc(ssh.dir || '~/.ssh')}</div><div class="card-actions"><button class="btn ghost sm" data-generate-key>${icon('plus', 12)} Create dedicated key</button></div></div>
    <div class="table-wrap"><table class="data">
      <thead><tr><th>Key</th><th>Type</th><th>Fingerprint</th><th class="actions"></th></tr></thead>
      <tbody>${keys.length ? keys.map((key) => `<tr>
        <td class="mono tiny">${esc(key.private.replace(/^.*\//, ''))}${key.private === ssh.default_key?.private ? ' <span class="pill tiny ok">default</span>' : ''}</td>
        <td>${esc(key.type)}</td>
        <td class="mono tiny">${esc(key.fingerprint)}</td>
        <td class="actions"><button class="btn ghost sm" type="button" data-copy-key="${esc(key.public)}" aria-label="Copy public key ${esc(key.private.replace(/^.*\//, ''))}">${icon('copy', 12)} Public key</button></td>
      </tr>`).join('') : '<tr><td colspan="4" class="table-empty">No keys found. Create one to start onboarding.</td></tr>'}</tbody>
    </table></div>
    <div class="card-body info-body"><div class="info-box">${icon('info')}<div>Saving a public key on a Pi is a one-time trust step. The console generates ed25519 keys, verifies the host fingerprint before the first login, and never stores a password.</div></div></div>
  </div>`;
}

export default {
  render(ctx) {
    const hosts = ctx.bootstrap?.hosts || [];
    return `${pageHead({
      title: 'Hosts',
      subtitle: `${ui.plural(hosts.filter((host) => !host.local).length, 'SSH endpoint')}<span class="dot">·</span>keys and inventory live on this Mac`,
      actions: `<button class="btn primary sm" data-add-host>${icon('plus', 12)} Add host</button>`,
    })}
    ${stateBanner(ctx)}
    ${hosts.length ? `<div class="host-grid">${hosts.map((host) => hostCard(ctx, host)).join('')}</div>` : emptyState('hosts', 'No hosts yet', 'Add your first Pi to let the console collect telemetry, deploy stacks and check releases.',
      '<button class="btn primary mt-2" data-add-host>Add host</button>')}
    <div class="section">
      <div class="section-head"><div class="section-title">How onboarding works</div><div class="section-sub">the four gates</div></div>
      <div class="grid cols-4 gates">
        <div class="card"><div class="card-body"><div class="mini-label">1 · address</div><p class="policy-note">Alias, hostname or IP, SSH user and port. The alias becomes the stack directory name.</p></div></div>
        <div class="card"><div class="card-body"><div class="mini-label">2 · fingerprint</div><p class="policy-note">The host key is fetched with <span class="mono">ssh-keyscan</span> and shown for verification before trust.</p></div></div>
        <div class="card"><div class="card-body"><div class="mini-label">3 · key</div><p class="policy-note">Use your ed25519 key or create a dedicated one. Install it with a one-time password, or paste the manual command.</p></div></div>
        <div class="card"><div class="card-body"><div class="mini-label">4 · test</div><p class="policy-note">The console proves key auth with a side-effect-free SSH probe. Deploy and monitoring reuse it.</p></div></div>
      </div>
    </div>
    <div class="section">${keysPanel(ctx)}</div>`;
  },
  mount(root, ctx) {
    const byAlias = (alias) => (ctx.bootstrap?.hosts || []).find((host) => host.alias === alias);
    root.querySelectorAll('[data-add-host]').forEach((button) => button.addEventListener('click', () => showAddHost(ctx)));
    root.querySelectorAll('[data-edit]').forEach((button) => button.addEventListener('click', () => {
      const host = byAlias(button.dataset.edit);
      if (host) showEditHost(ctx, host);
    }));
    root.querySelectorAll('[data-remove]').forEach((button) => button.addEventListener('click', () => {
      const host = byAlias(button.dataset.remove);
      if (host) showRemoveHost(ctx, host);
    }));
    root.querySelectorAll('[data-test]').forEach((button) => button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const result = await ctx.api.post(`/api/hosts/${encodeURIComponent(button.dataset.test)}/test`, {});
        if (result.result?.ok) {
          ui.toast('SSH works', 'ok', `${button.dataset.test} · ${result.result.hostname} · ${result.result.latency_ms} ms`);
          ctx.refreshState({ force: true });
        } else {
          ui.toast('SSH failed', 'warn', result.result?.error || '');
        }
      } catch (error) {
        ui.toast('Test failed', 'err', ui.errorText(error));
      } finally {
        button.disabled = false;
      }
    }));
    root.querySelectorAll('[data-copy-ssh]').forEach((button) => button.addEventListener('click', () => ui.copyText(`ssh ${button.dataset.copySsh}`)));
    root.querySelectorAll('[data-copy-key]').forEach((button) => button.addEventListener('click', () => ui.copyText(button.dataset.copyKey)));
    root.querySelectorAll('[data-generate-key]').forEach((button) => button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const result = await ctx.api.post('/api/ssh/generate', { name: 'homelab_console_ed25519' });
        await ctx.reloadBootstrap();
        ui.toast('ed25519 key created', 'ok', result.key.fingerprint);
      } catch (error) {
        ui.toast('Could not create key', 'err', ui.errorText(error));
      } finally {
        button.disabled = false;
      }
    }));
  },
};
