import * as ui from '../ui.js';
import { pageHead, stateBanner } from './common.js';
import { nativePick } from '../native.js';
import { addRoot, bindRootRemoval, openRepoSelection, readRoots, rootsFromSettings, rootsHtml, rootsLabel } from '../repoRoots.js';

const { esc, icon } = ui;

const SETTINGS_SECTIONS = [
  { id: 'appearance', label: 'Appearance' },
  { id: 'collection', label: 'Collection' },
  { id: 'sources', label: 'Sources' },
  { id: 'console', label: 'Console' },
  { id: 'repo-fleet', label: 'Repos' },
  { id: 'backup', label: 'Backup' },
  { id: 'safety', label: 'Safety' },
  { id: 'maintenance', label: 'Maintenance' },
];

function settingsNavHtml() {
  return `<nav class="settings-nav" aria-label="Settings sections">
    <span class="settings-nav-label">Jump to</span>
    ${SETTINGS_SECTIONS.map((section) => `<button class="settings-jump" type="button" data-settings-jump="${section.id}">${section.label}</button>`).join('')}
  </nav>`;
}

function settingsData(ctx) {
  return ctx.uiState.system?.data || null;
}

function repoSettingsHtml(ctx) {
  const settings = ctx.bootstrap?.settings || ctx.state?.settings || {};
  const repoState = ctx.state?.repos || {};
  const gitea = repoState.gitea || {};
  const summary = repoState.summary || {};
  const tokenSet = Boolean(settings.gitea_token_set);
  const appManaged = Boolean(settings.app_managed);
  const tokenSource = String(settings.gitea_token_source || '');
  const sourceLabel = tokenSource === 'macos-app'
    ? 'macOS Keychain'
    : tokenSource === 'environment'
      ? 'environment'
      : tokenSource === 'config'
        ? '.hlc/config.json'
        : '';
  const giteaConfigured = Boolean(settings.gitea_url && settings.gitea_owner);
  const giteaState = !giteaConfigured
    ? '<span class="chip neutral">not configured</span>'
    : gitea.state === 'checking'
      ? '<span class="chip info"><span class="spinner sm"></span> checking</span>'
      : gitea.checked_at
        ? `<span class="chip ${gitea.errors ? 'warn' : 'good'}">${esc(gitea.state || 'checked')}</span><span class="sub">${esc(ui.ago(gitea.checked_at))}</span>`
        : '<span class="chip neutral">not checked</span>';
  const vscode = repoState.vscode || {};
  const roots = rootsFromSettings(settings);
  return `<div class="section settings-section" id="repo-fleet" data-settings-section="repo-fleet">
    <div class="section-head"><div class="section-title">Repos</div><div class="section-sub">local project folders and the Gitea backup</div></div>
    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><div class="card-title">Discovery & mirror</div><div class="card-sub">${ui.plural(summary.total || 0, 'project')} · ${esc(rootsLabel(repoState.roots || roots))}</div></div>
        <div class="card-body">
          <div class="field">
            <label>Project folders</label>
            <div id="repo-roots-list" class="root-list">${rootsHtml(roots)}</div>
            <div class="field-row mt-1">
              <label class="sr-only" for="set-repo-root-manual">Add a project folder path</label>
              <input id="set-repo-root-manual" placeholder="/Users/you/code" autocomplete="off" spellcheck="false">
              <button class="btn ghost sm" type="button" data-action="add-repo-root">Add path</button>
              <button class="btn sm" type="button" data-action="pick-repo-roots">Choose folders…</button>
            </div>
            <div class="hint">Each folder is scanned recursively for git projects. Hidden and dependency/build folders are skipped. Use <strong>Manage projects…</strong> to hide checkouts you do not want in Repos.</div>
          </div>
          <div class="field-row">
            <div class="field"><label for="set-gitea-url">Gitea URL</label><input id="set-gitea-url" value="${esc(settings.gitea_url || '')}" placeholder="https://gitea.example.com" autocomplete="off"><div class="hint">Leave URL and owner empty to disable the Gitea backup.</div></div>
            <div class="field"><label for="set-gitea-owner">Gitea owner</label><input id="set-gitea-owner" value="${esc(settings.gitea_owner || '')}" placeholder="your-username" autocomplete="off"></div>
          </div>
          <div class="field-row">
            <div class="field"><label for="set-gitea-timeout">Network timeout (s)</label><input id="set-gitea-timeout" type="number" min="15" max="900" value="${esc(settings.gitea_timeout_s || 120)}"></div>
            <div class="field"><label for="set-repo-poll">Local scan interval</label><select id="set-repo-poll">${[10, 30, 60, 120].map((value) => `<option value="${value}" ${Number(settings.repo_poll_interval_s) === value ? 'selected' : ''}>${value} seconds</option>`).join('')}</select></div>
          </div>
          <label class="check"><input type="checkbox" id="set-gitea-prune" ${settings.gitea_prune !== false ? 'checked' : ''}><span><strong>Prune Gitea refs.</strong> Delete branches and tags on Gitea that no longer exist locally. This is the exact-mirror mode used by gitea-tools.</span></label>
          <label class="check"><input type="checkbox" id="set-gitea-force" ${settings.gitea_force !== false ? 'checked' : ''}><span><strong>Force-update Gitea refs.</strong> Allow a rewritten local history to replace the Gitea backup. Never applies to GitHub.</span></label>
          <label class="check"><input type="checkbox" id="set-gitea-startup" ${settings.gitea_check_on_start !== false ? 'checked' : ''}><span><strong>Check Gitea automatically.</strong> One ref comparison at startup, plus the affected project after a commit or GitHub push. No timer; <strong>Check Gitea</strong> still works at any time.</span></label>
          <div class="btn-group mt-1"><button class="btn primary" data-action="save-repo-settings">Save project settings</button><button class="btn ghost" type="button" data-action="manage-repos">Manage projects…</button><a class="btn ghost" href="#/repos">Open Projects</a></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><div class="card-title">Gitea credential</div><div class="card-sub">startup, post-action and on-demand ref checks use this token</div></div>
        <div class="card-body">
          <div class="kv mb-2">
            <div class="kv-row"><div class="kv-key">Token</div><div class="kv-val">${tokenSet ? ui.pill('ok', `configured${sourceLabel ? ` · ${sourceLabel}` : ''}`) : ui.pill('neutral', 'not set')}</div></div>
            <div class="kv-row"><div class="kv-key">Last check</div><div class="kv-val">${giteaState}</div></div>
            <div class="kv-row"><div class="kv-key">VS Code CLI</div><div class="kv-val">${vscode.available ? ui.pill('ok', 'detected') : ui.pill('warn', 'not found')}</div></div>
            <div class="kv-row"><div class="kv-key">GitHub push auth</div><div class="kv-val"><span class="muted tiny">uses your git credential helper; no token stored by the console</span></div></div>
          </div>
          ${appManaged
            ? `<div class="info-box">${icon('info')}<div><strong>Managed by the macOS app.</strong> Open <strong>Homelab Console → Settings… (⌘,)</strong> from the menu bar and paste a token. It is stored in the macOS Keychain, never in <span class="mono">.hlc/config.json</span>. Saving restarts the console and checks Gitea automatically.</div></div>
          <div class="btn-group mt-1"><a class="btn primary" href="homelab-console://settings">${icon('key', 12)} Open app Settings…</a><span class="hint">Gitea → Settings → Applications → Generate Token (repository: Read and Write)</span></div>`
            : `<div class="field"><label for="set-gitea-token">Gitea access token</label><input id="set-gitea-token" type="password" placeholder="${tokenSet ? '•••••• configured — enter to replace' : 'token with project write access'}" autocomplete="off"><div class="hint">Stored locally in <span class="mono">.hlc/config.json</span> (mode 600) and never returned by the API. <span class="mono">GITEA_TOKEN</span> / <span class="mono">HLC_GITEA_TOKEN</span> override it.</div></div>
          <div class="btn-group"><button class="btn primary" data-action="save-gitea-token">Save token</button><button class="btn ghost" data-action="clear-gitea-token">Clear token</button></div>`}
          <div class="sep-line"></div>
          <div class="info-box">${icon('info')}<div>Gitea refs are compared at startup, after a commit or GitHub push for that project, and when you press <strong>Check Gitea</strong>. There is no timer, so a Gitea outage cannot slow local status.</div></div>
          <div class="btn-group mt-2"><button class="btn ghost" data-action="check-gitea-settings" ${!giteaConfigured || gitea.state === 'checking' ? 'disabled' : ''}>${icon('git', 12)} Check Gitea now</button><span class="hint">read-only; compares refs, never pushes</span></div>
        </div>
      </div>
    </div>
  </div>`;
}

function backupCard() {
  const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15);
  const defaultPath = `~/Homelab Backups/hlc-backup-${stamp}.zip`;
  return `<div class="section settings-section" id="backup" data-settings-section="backup">
    <div class="section-head"><div class="section-title">Backup &amp; restore</div><div class="section-sub">fleet inventory, stacks and project folders in one archive</div></div>
    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><div class="card-title">Create a backup</div><div class="card-sub">.hlc/ state, logs and tokens are excluded</div></div>
        <div class="card-body">
          <div class="field"><label for="backup-path">Archive path</label><input id="backup-path" value="${esc(defaultPath)}" autocomplete="off" spellcheck="false"><div class="hint">Absolute path or <span class="mono">~/…</span>. Contains <span class="mono">hosts.json</span>, every stack file (including <span class="mono">.env</span> secrets) and non-secret settings, including all project folders.</div></div>
          <div class="btn-group"><button class="btn primary" data-action="create-backup">Create backup</button><button class="btn ghost" data-action="pick-backup-save">Choose file…</button></div>
          <div id="backup-status" class="hint mt-1"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><div class="card-title">Restore a backup</div><div class="card-sub">replaces hosts, stacks and settings</div></div>
        <div class="card-body">
          <div class="field"><label for="restore-path">Archive path</label><input id="restore-path" placeholder="~/Homelab Backups/hlc-backup-….zip" autocomplete="off" spellcheck="false"><div class="hint">Local telemetry and run logs are kept. A pre-restore backup is written to <span class="mono">.hlc/backups/</span> before anything is replaced.</div></div>
          <div class="btn-group"><button class="btn ghost" data-action="pick-backup-open">Choose file…</button><button class="btn danger" data-action="restore-backup">Restore backup</button></div>
          <div id="restore-status" class="hint mt-1"></div>
        </div>
      </div>
    </div>
  </div>`;
}


const SOURCE_KIND_LABELS = {
  dockerhub: 'Docker Hub tags',
  github: 'GitHub releases',
  managed: 'Compatibility-managed',
  unknown: 'No checks',
};

function releaseKindLabel(kind) {
  return SOURCE_KIND_LABELS[kind] || kind || '—';
}

function releaseTarget(source) {
  if (!source) return '—';
  if (source.kind === 'dockerhub') return `Docker Hub · ${source.hub_repo || source.key || '—'}`;
  if (source.kind === 'github') return `GitHub · ${source.github_repo || '—'}`;
  if (source.kind === 'managed') return source.managed_by || 'compatibility-managed';
  return 'not checked';
}

const RELEASE_ORIGIN_LABELS = {
  custom: 'Custom source',
  catalog: 'Built-in catalog',
  auto: 'Auto-detected',
  labels: 'From image labels',
  group: 'Compatibility-managed',
  managed: 'Compatibility-managed',
  unknown: 'Needs a source',
};

function releaseOriginLabel(row) {
  if (row.kind === 'managed' && row.origin !== 'custom') return 'Compatibility-managed';
  return RELEASE_ORIGIN_LABELS[row.origin] || 'Detected';
}

function releaseOriginPill(row) {
  const label = releaseOriginLabel(row);
  const tone = row.origin === 'unknown'
    ? 'warn'
    : row.origin === 'custom'
      ? 'ok'
      : (row.kind === 'managed' || row.origin === 'group')
        ? 'managed'
        : 'neutral';
  return ui.pill(tone, label);
}

function releaseCurrentText(row) {
  if (row.origin === 'unknown') return 'No update source — updates for this image are not checked.';
  if (row.kind === 'dockerhub') return `Docker Hub tags · ${row.hub || row.key}`;
  if (row.kind === 'github') return `GitHub releases · ${row.github || '—'}`;
  if (row.kind === 'managed') return `Compatibility-managed${row.managed ? ` by ${row.managed}` : ''}`;
  return releaseKindLabel(row.kind);
}

function releaseActionLabel(row) {
  if (row.origin === 'custom') return 'Edit source';
  if (row.origin === 'unknown') return row.suggestion ? 'Add GitHub source' : 'Add source';
  return 'Change source';
}

function githubTokenCard(ctx) {
  const settings = ctx.bootstrap?.settings || ctx.state?.settings || {};
  return `<div class="card">
    <div class="card-head"><div class="card-title">GitHub API token</div><div class="card-sub">optional · raises the release-check limit</div></div>
    <div class="card-body">
      <div class="kv mb-2">
        <div class="kv-row"><div class="kv-key">Token</div><div class="kv-val">${settings.github_token_set ? ui.pill('ok', 'configured') : ui.pill('neutral', 'not set')}</div></div>
      </div>
      <div class="field"><label for="set-token">Personal access token</label><input id="set-token" type="password" placeholder="${settings.github_token_set ? '•••••• configured — enter to replace' : 'ghp_…'}" autocomplete="off"><div class="hint">Only used for GitHub release checks. Docker Hub needs no token. Stored locally in <span class="mono">.hlc/config.json</span> (mode 600).</div></div>
      <div class="btn-group"><button class="btn primary" data-action="save-token">Save token</button><button class="btn ghost" data-action="clear-token">Clear token</button></div>
    </div>
  </div>`;
}

function updateChecksCard(ctx) {
  const release = ctx.state?.release_check || {};
  return `<div class="card">
    <div class="card-head"><div class="card-title">How update checks work</div><div class="card-sub">sources never change the pinned image</div></div>
    <div class="card-body">
      <div class="kv mb-2">
        <div class="kv-row"><div class="kv-key">Last check</div><div class="kv-val">${release.checked_iso ? `${esc(ui.ago(release.checked_at))} · ${ui.plural(release.sources || 0, 'source')}` : 'never'}</div></div>
        <div class="kv-row"><div class="kv-key">Cached sources</div><div class="kv-val mono">${esc(release.sources || 0)}</div></div>
        <div class="kv-row"><div class="kv-key">Errors</div><div class="kv-val">${release.errors ? `<span class="text-warn">${ui.plural(release.errors, 'source')} failed</span>` : '<span class="muted">none</span>'}</div></div>
      </div>
      <div class="info-box">${icon('info')}<div>Docker Hub images are detected automatically. GHCR and other registries need an update source; the console never guesses a tag. Compatibility-managed images are reported but never proposed for an independent update.</div></div>
    </div>
  </div>`;
}

function releaseSourcesHtml(ctx) {
  const settings = ctx.bootstrap?.settings || ctx.state?.settings || {};
  const configured = settings.release_sources || {};
  const configuredRows = Object.entries(configured).sort(([a], [b]) => a.localeCompare(b));
  const configuredKeys = new Set(configuredRows.map(([key]) => key));
  const detected = new Map();
  for (const target of ctx.state?.updates || []) {
    const source = target.source || {};
    const key = source.key || target.image || '';
    if (!key) continue;
    if (!detected.has(key)) {
      detected.set(key, {
        key,
        image: target.image || key,
        kind: source.kind || 'unknown',
        label: source.label || key,
        hub: source.hub_repo || '',
        github: source.github_repo || '',
        suggestion: source.suggested_github_repo || '',
        managed: source.managed_by || '',
        guidance: source.guidance || '',
        origin: configuredKeys.has(key) ? 'custom' : (source.origin || (source.kind === 'managed' ? 'managed' : source.kind === 'unknown' ? 'unknown' : 'catalog')),
        stacks: new Set(),
      });
    }
    const row = detected.get(key);
    if (target.host && target.stack) row.stacks.add(`${target.host}/${target.stack}`);
  }
  const originOrder = { unknown: 0, custom: 1, managed: 2, group: 3, catalog: 4, labels: 5, auto: 6 };
  const allDetected = [...detected.values()]
    .map((row) => ({ ...row, stacks: [...row.stacks] }))
    .sort((a, b) => (originOrder[a.origin] ?? 9) - (originOrder[b.origin] ?? 9) || a.key.localeCompare(b.key));
  const detectedRows = allDetected.slice(0, 40);
  const unmanagedCount = allDetected.filter((row) => row.origin === 'unknown').length;
  const customCount = allDetected.filter((row) => row.origin === 'custom').length;
  const managedCount = allDetected.filter((row) => row.kind === 'managed' || row.origin === 'group').length;
  const configuredTable = configuredRows.length ? `<div class="table-wrap"><table class="data">
    <thead><tr><th>Image</th><th>Type</th><th>Target</th><th class="actions"></th></tr></thead>
    <tbody>${configuredRows.map(([key, source]) => `<tr>
      <td class="mono cell-strong">${esc(key)}</td>
      <td>${esc(releaseKindLabel(source.kind))}</td>
      <td class="muted tiny">${esc(releaseTarget(source))}</td>
      <td class="actions"><div class="btn-group">
        <button class="btn ghost sm" type="button" data-edit-source="${esc(key)}">Edit</button>
        <button class="btn danger sm" type="button" data-remove-source="${esc(key)}">Remove</button>
      </div></td>
    </tr>`).join('')}</tbody>
  </table></div>` : '<div class="empty empty-inline"><strong>No custom sources yet.</strong><p>Cards marked “Needs a source” are the ones to set up.</p></div>';
  const summary = `<div class="source-summary">
    <span class="chip">${ui.plural(allDetected.length, 'image')}</span>
    ${unmanagedCount ? `<span class="chip warn">${unmanagedCount} ${unmanagedCount === 1 ? 'needs' : 'need'} a source</span>` : ''}
    ${customCount ? `<span class="chip good">${customCount} custom</span>` : ''}
    ${managedCount ? `<span class="chip">${managedCount} compatibility-managed</span>` : ''}
    ${allDetected.length > detectedRows.length ? `<span class="chip neutral">showing ${detectedRows.length} of ${allDetected.length}</span>` : ''}
  </div>`;
  const sourceCards = detectedRows.length ? `<div class="source-card-grid">${detectedRows.map((row) => {
    const stacks = row.stacks.slice(0, 4);
    const hiddenStacks = row.stacks.length - stacks.length;
    return `<article class="source-card ${row.origin === 'unknown' ? 'needs-source' : ''}">
      <div class="source-card-top">
        <div class="source-card-identity">
          <div class="source-card-key mono" title="${esc(row.key)}">${esc(row.key)}</div>
          <div class="source-card-label">${esc(row.label)}</div>
          ${row.image && row.image !== row.key ? `<div class="source-card-image mono" title="${esc(row.image)}">${esc(row.image)}</div>` : ''}
        </div>
        ${releaseOriginPill(row)}
      </div>
      <div class="source-card-source">${esc(releaseCurrentText(row))}</div>
      ${row.suggestion ? `<div class="source-card-suggestion">Suggested GitHub source · <span class="mono">${esc(row.suggestion)}</span></div>` : ''}
      ${stacks.length ? `<div class="source-card-stacks">${stacks.map((stack) => `<span class="chip mono">${esc(stack)}</span>`).join('')}${hiddenStacks > 0 ? `<span class="chip">+${hiddenStacks}</span>` : ''}</div>` : ''}
      ${row.guidance ? `<div class="source-card-guidance">${esc(row.guidance)}</div>` : ''}
      <div class="source-card-actions"><button class="btn ${row.origin === 'unknown' ? 'primary' : 'ghost'} sm" type="button" data-fill-source="${esc(row.key)}" title="Open the update source dialog for this image. Nothing is saved until you press Save source.">${esc(releaseActionLabel(row))}</button></div>
    </article>`;
  }).join('')}</div>` : '<div class="empty empty-inline"><strong>No stack images discovered yet.</strong><p>Open Stacks or run a release check, then return here.</p></div>';
  return `<div class="section settings-section" id="update-sources" data-settings-section="sources">
    <div class="section-head"><div class="section-title">Update sources</div><div class="section-sub">where update checks look; this never changes the pinned image</div><button class="btn primary sm right" type="button" data-add-source>${icon('plus', 12)} Add update source</button></div>
    <div class="grid cols-2">
      ${githubTokenCard(ctx)}
      ${updateChecksCard(ctx)}
    </div>
    <div class="card mt-2" id="detected-images">
      <div class="card-head"><div class="card-title">Detected images</div><div class="card-sub">from the current stacks · choose how each image is checked</div></div>
      <div class="card-body">
        ${summary}
        ${sourceCards}
        <div class="hint mt-2">Choose an image’s action to edit its update source. Nothing is saved until you press <strong>Save source</strong> in the dialog. A source never changes the pinned image.</div>
      </div>
    </div>
    ${configuredRows.length ? `<div class="card mt-2">
      <div class="card-head"><div class="card-title">Custom sources</div><div class="card-sub">${ui.plural(configuredRows.length, 'override')} · these win over detection</div></div>
      <div class="card-body">${configuredTable}</div>
    </div>` : '<div class="hint mt-2">No custom sources are saved yet. The cards above show what the console currently checks.</div>'}
  </div>`;

}

function contentHtml(ctx) {
  const settings = ctx.bootstrap?.settings || ctx.state?.settings || {};
  const app = ctx.bootstrap?.app || {};
  const theme = settings.theme || 'dark';
  const themeChoice = (value, label, description, swatch) => `<button class="theme-choice ${theme === value ? 'active' : ''}" data-theme-choice="${value}" aria-pressed="${theme === value}">
    <span class="theme-swatch ${swatch}"></span>
    <span><strong>${label}</strong><span class="desc">${description}</span></span>
    <span class="check-mark">${icon('check', 14)}</span>
  </button>`;
  const git = ctx.state?.git || {};
  const system = settingsData(ctx);
  const db = system?.storage?.db || ctx.bootstrap?.stats || {};
  const autoEnabled = Boolean(ctx.state?.auto_update_enabled);
  return `${pageHead({
    title: 'Settings',
    subtitle: 'console behaviour, update sources and project hygiene',
  })}
  ${stateBanner(ctx)}
  ${settingsNavHtml()}
  <div class="card mb-2 settings-section" data-settings-section="appearance">
    <div class="card-head"><div class="card-title">Appearance</div><div class="card-sub">blue is the working accent; orange marks updates and warnings</div></div>
    <div class="card-body">
      <div class="theme-grid">
        ${themeChoice('dark', 'Dark', 'Deep slate surfaces, soft white text, blue primary, amber highlights.', 'dark')}
        ${themeChoice('light', 'Light', 'White surfaces, blue primary, orange warnings. Easier in daylight.', 'light')}
        ${themeChoice('system', 'System', 'Follows the macOS appearance and changes live when the OS does.', 'system')}
      </div>
      <div class="hint mt-2">The choice follows this console on the Mac. The quick control in the top bar cycles Dark → Light → System.</div>
    </div>
  </div>
  <div class="card mb-2 settings-section" data-settings-section="collection">
      <div class="card-head"><div class="card-title">Collection & updates</div><div class="card-sub">scoped to this console install</div></div>
      <div class="card-body">
        <div class="field-row">
          <div class="field"><label for="set-poll">Telemetry poll interval</label>
            <select id="set-poll">${[15, 30, 60, 120].map((value) => `<option value="${value}" ${Number(settings.poll_interval_s) === value ? 'selected' : ''}>${value} seconds</option>`).join('')}</select>
          </div>
          <div class="field"><label for="set-ttl">Release check cache</label>
            <select id="set-ttl">${[[1800, '30 minutes'], [3600, '1 hour'], [21600, '6 hours'], [43200, '12 hours'], [86400, '24 hours']].map(([value, label]) => `<option value="${value}" ${Number(settings.release_ttl_s) === value ? 'selected' : ''}>${label}</option>`).join('')}</select>
          </div>
        </div>
        <div class="field-row">
          <div class="field"><label for="set-history">History retention (days)</label><input id="set-history" type="number" min="1" max="365" value="${esc(settings.history_days)}"></div>
          <div class="field"><label for="set-health">Health verification timeout (s)</label><input id="set-health" type="number" min="30" max="600" value="${esc(settings.health_timeout_s)}"></div>
        </div>
        <div class="field"><label for="set-root">Remote stack root</label><input id="set-root" value="${esc(settings.remote_root || '/opt/stacks')}"><div class="hint">Where the console pushes stack directories on every host. Changing this does not move existing stacks.</div></div>
        <label class="check"><input type="checkbox" id="set-start-check" ${settings.check_releases_on_start ? 'checked' : ''}><span>Check upstream releases when the console starts.</span></label>
        <label class="check"><input type="checkbox" id="set-auto" ${autoEnabled ? 'checked' : ''}><span><strong>Enable automatic updates.</strong> Stacks with an “auto” policy update when the console sees a new stable release. Critical stacks are always excluded; every run still verifies health and rolls back compose files on failure.</span></label>
        <div class="btn-group mt-1"><button class="btn primary" data-action="save-settings">Save settings</button><span class="hint">Applies to this console immediately.</span></div>
      </div>
    </div>

  ${releaseSourcesHtml(ctx)}

  <div class="grid cols-2 section settings-section" id="console-info" data-settings-section="console">
    <div class="card">
      <div class="card-head"><div class="card-title">Workspace repository</div><div class="card-sub">desired state stays in git</div>${git.dirty ? `<div class="card-actions"><button class="btn ghost sm" data-open-commit>${icon('git', 12)} Review &amp; commit</button></div>` : ''}</div>
      <div class="card-body"><div class="kv">
        <div class="kv-row"><div class="kv-key">Branch</div><div class="kv-val mono">${esc(git.branch || '—')}</div></div>
        <div class="kv-row"><div class="kv-key">Last commit</div><div class="kv-val mono tiny">${esc(git.last_commit || '—')}</div></div>
        <div class="kv-row"><div class="kv-key">Working tree</div><div class="kv-val">${git.dirty ? ui.pill('warn', `${git.entries.length} modified`) : ui.pill('ok', 'clean')}</div></div>
        <div class="kv-row"><div class="kv-key">Repo</div><div class="kv-val mono tiny">${esc(app.repo || '')}</div></div>
        <div class="kv-row"><div class="kv-key">State dir</div><div class="kv-val mono tiny">${esc(app.state_dir || '')}</div></div>
      </div>
      ${git.dirty ? `<div class="sep-line"></div><div class="diff">${git.entries.map((entry) => `<div class="diff-line"><span class="marker">${esc(entry.code)}</span><span class="mono">${esc(entry.path)}</span></div>`).join('')}</div>` : ''}
      </div>
    </div>
    <div class="card">
      <div class="card-head"><div class="card-title">Console install</div><div class="card-sub">local process and storage</div></div>
      <div class="card-body"><div class="kv">
        <div class="kv-row"><div class="kv-key">Version</div><div class="kv-val mono">${esc(app.version || '')}</div></div>
        <div class="kv-row"><div class="kv-key">Listening</div><div class="kv-val mono">127.0.0.1:${esc(app.port || '')}</div></div>
        <div class="kv-row"><div class="kv-key">Python</div><div class="kv-val mono">${esc(app.python || '')}</div></div>
        <div class="kv-row"><div class="kv-key">History rows</div><div class="kv-val mono">${esc(db.host_samples || 0)} host · ${esc(db.stack_samples || 0)} stack</div></div>
        <div class="kv-row"><div class="kv-key">Events / runs</div><div class="kv-val mono">${esc(db.events || 0)} / ${esc(db.runs || 0)}</div></div>
        ${system ? `<div class="kv-row"><div class="kv-key">State size</div><div class="kv-val mono">${ui.bytes(system.storage.state_bytes)} · ${ui.plural(system.storage.snapshots, 'snapshot')}</div></div>` : ''}
        <div class="kv-row"><div class="kv-key">Snapshot dir</div><div class="kv-val mono tiny">${esc((app.state_dir || '') + '/snapshots')}</div></div>
      </div></div>
    </div>
  </div>

  ${repoSettingsHtml(ctx)}

  ${backupCard()}

  <div class="section settings-section" id="safety-model" data-settings-section="safety">
    <div class="section-head"><div class="section-title">Safety model</div><div class="section-sub">why this is not a Portainer clone</div></div>
    <div class="card"><div class="card-body">
      <div class="grid cols-3">
        <div><div class="mini-label">Source of truth</div><p class="policy-note">The repo is desired state. The console writes compose files here first, then streams a tar to the configured remote stack root. Nothing is hand-edited on a Pi.</p></div>
        <div><div class="mini-label">Gates</div><p class="policy-note">Overwrites require a reviewed diff hash. Added files flow; extra host files are reported and left untouched. Down/restart never push files.</p></div>
        <div><div class="mini-label">Updates</div><p class="policy-note">Pins change only on request. The console snapshots repo files, recreates affected services, verifies health, and restores the previous compose on failure. Databases are never dumped automatically.</p></div>
      </div>
    </div></div>
  </div>

  <div class="section settings-section" id="maintenance" data-settings-section="maintenance">
    <div class="card danger-zone">
      <div class="card-head"><div class="card-title">Maintenance</div><div class="card-sub">destructive, local only</div></div>
      <div class="card-body flex items-center gap-2 wrap">
        <button class="btn danger" data-action="clear-history">Clear telemetry history</button>
        <span class="muted tiny">Deletes host/stack samples from the SQLite store. Events, run logs and snapshots are kept. The repo and Pis are untouched.</span>
      </div>
    </div>
  </div>`;
}

export default {
  render(ctx) {
    return `<div id="settings-page">${contentHtml(ctx)}</div>`;
  },
  mount(root, ctx) {
    const page = root.querySelector('#settings-page');
    if (!page) return;
    if (ctx.uiState.settingsNavCleanup) ctx.uiState.settingsNavCleanup();
    const paint = () => { page.innerHTML = contentHtml(ctx); attach(); };

    // The page is long; keep the section chips in sync while the operator scrolls.
    const setupSettingsNav = () => {
      if (ctx.uiState.settingsNavCleanup) ctx.uiState.settingsNavCleanup();
      const nav = page.querySelector('.settings-nav');
      const sections = [...page.querySelectorAll('[data-settings-section]')];
      if (!nav || !sections.length) return;
      const buttons = [...nav.querySelectorAll('[data-settings-jump]')];
      let frame = 0;
      let currentId = '';
      let pinnedId = '';

      const setActive = (id, { reveal = false } = {}) => {
        if (!id || id === currentId) return;
        currentId = id;
        for (const button of buttons) {
          const active = button.dataset.settingsJump === id;
          button.classList.toggle('active', active);
          if (active) button.setAttribute('aria-current', 'location');
          else button.removeAttribute('aria-current');
        }
        if (reveal) {
          const button = nav.querySelector(`[data-settings-jump="${id}"]`);
          if (!button) return;
          const navRect = nav.getBoundingClientRect();
          const buttonRect = button.getBoundingClientRect();
          if (buttonRect.left < navRect.left + 12) nav.scrollLeft -= navRect.left + 12 - buttonRect.left;
          else if (buttonRect.right > navRect.right - 12) nav.scrollLeft += buttonRect.right - navRect.right + 12;
        }
      };

      const sync = () => {
        if (!page.isConnected) { cleanup(); return; }
        const line = nav.getBoundingClientRect().bottom + 18;
        let active = sections[0].dataset.settingsSection;
        for (const section of sections) {
          if (section.getBoundingClientRect().top <= line) active = section.dataset.settingsSection;
        }
        // Clicking a chip pins it. The last sections cannot reach the scrollspy
        // line at the bottom of the page, so position alone would keep an
        // earlier chip highlighted. Real user scrolling releases the pin.
        setActive(pinnedId || active, { reveal: true });
      };

      const schedule = () => {
        if (frame) return;
        frame = requestAnimationFrame(() => { frame = 0; sync(); });
      };
      const releasePin = () => {
        if (!pinnedId) return;
        pinnedId = '';
        schedule();
      };

      const cleanup = () => {
        window.removeEventListener('scroll', schedule);
        window.removeEventListener('resize', schedule);
        window.removeEventListener('wheel', releasePin);
        window.removeEventListener('touchstart', releasePin);
        window.removeEventListener('keydown', releasePin);
        if (ctx.uiState.settingsNavCleanup === cleanup) ctx.uiState.settingsNavCleanup = null;
      };

      for (const button of buttons) {
        button.addEventListener('click', () => {
          const id = button.dataset.settingsJump;
          const target = page.querySelector(`[data-settings-section="${id}"]`);
          pinnedId = id;
          setActive(id, { reveal: true });
          // Instant jump keeps the highlighted chip and the visible section in
          // sync; a long smooth scroll leaves the wrong chip active mid-flight.
          target?.scrollIntoView({ behavior: 'auto', block: 'start' });
        });
      }
      window.addEventListener('scroll', schedule, { passive: true });
      window.addEventListener('resize', schedule);
      window.addEventListener('wheel', releasePin, { passive: true });
      window.addEventListener('touchstart', releasePin, { passive: true });
      window.addEventListener('keydown', releasePin);
      ctx.uiState.settingsNavCleanup = cleanup;
      sync();
    };

    const loadSystem = async () => {
      try {
        const data = await ctx.api.get('/api/system');
        if (!ctx.uiState.system) ctx.uiState.system = {};
        ctx.uiState.system.data = data;
        const focused = document.activeElement;
        if (!ui.modalOpen() && !(focused && page.contains(focused) && focused.matches('input, textarea, select'))) {
          paint();
        }
      } catch { /* optional */ }
    };

    const sourceDataFor = (key, configuredSource = null) => {
      const target = (ctx.state?.updates || []).find((item) => item.source?.key === key || item.image === key);
      const source = target?.source || {};
      const custom = configuredSource || (ctx.bootstrap?.settings?.release_sources || {})[key] || null;
      if (custom) {
        const base = {
          key,
          kind: custom.kind || 'dockerhub',
          hub: custom.hub_repo || '',
          github: custom.github_repo || '',
          managed: custom.managed_by || '',
          origin: 'custom',
        };
        return {
          ...base,
          label: custom.label || '',
          calendar: Boolean(custom.calendar_tags),
          guidance: custom.guidance || '',
          suggestion: source.suggested_github_repo || '',
          currentText: releaseCurrentText(base),
        };
      }
      const base = {
        key,
        kind: source.kind || 'unknown',
        hub: source.hub_repo || '',
        github: source.github_repo || '',
        managed: source.managed_by || '',
        origin: source.origin || (source.kind === 'managed' ? 'managed' : source.kind === 'unknown' ? 'unknown' : 'catalog'),
      };
      return {
        ...base,
        label: source.label || '',
        calendar: Boolean(source.calendar_tags),
        guidance: source.guidance || '',
        suggestion: source.suggested_github_repo || '',
        currentText: source.key ? releaseCurrentText(base) : '',
      };
    };

    const openSourceEditor = (data) => {
      const key = String(data.key || '').trim();
      const fixed = Boolean(key);
      const custom = data.origin === 'custom';
      const initialKind = data.kind === 'unknown' && !custom && data.suggestion
        ? 'github'
        : (data.kind || 'dockerhub');
      const title = custom ? 'Edit update source' : (data.origin === 'unknown' || data.origin === 'new') ? 'Add update source' : 'Change update source';
      const outcome = custom ? 'Saving updates this custom source.' : (data.origin === 'unknown' || data.origin === 'new') ? 'Saving tells the console where to check this image.' : 'Saving creates a custom override for this image.';
      const ref = ui.openModal({
        title,
        subtitle: fixed ? key : 'For an image that is not in the current stacks',
        className: 'wide',
        body: `
          <div class="info-box mb-2">${icon('info')}<div><strong>An update source only changes where release checks look.</strong> It does not pull the image, change its pin in compose, or deploy anything. ${outcome}</div></div>
          ${fixed ? `<div class="kv mb-2">
            <div class="kv-row"><div class="kv-key">Image</div><div class="kv-val mono">${esc(key)}</div></div>
            <div class="kv-row"><div class="kv-key">Current source</div><div class="kv-val">${esc(data.currentText || '—')}</div></div>
          </div>` : ''}
          <div class="field"><label for="source-repo">Image repository *</label><input id="source-repo" value="${esc(key)}" placeholder="ghcr.io/owner/app" autocomplete="off" spellcheck="false" ${fixed ? 'readonly' : ''}><div class="hint">${fixed ? 'The repository is fixed for this image. Use Add update source for another image.' : 'Registry path without the tag, e.g. linuxserver/jellyfin or ghcr.io/paperless-ngx/paperless-ngx.'}</div></div>
          <div class="field-row">
            <div class="field"><label for="source-kind">Source type</label><select id="source-kind">${Object.entries(SOURCE_KIND_LABELS).map(([value, label]) => `<option value="${value}" ${value === initialKind ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select></div>
            <div class="field"><label for="source-label">Display label <span class="opt">optional</span></label><input id="source-label" value="${esc(data.label || '')}" placeholder="Paperless" autocomplete="off"></div>
          </div>
          <div class="field" id="source-hub-field"><label for="source-hub">Docker Hub repository</label><input id="source-hub" value="${esc(data.hub || '')}" placeholder="linuxserver/jellyfin" autocomplete="off" spellcheck="false"><div class="hint">Owner/name on Docker Hub. Use <span class="mono">library/name</span> for official images.</div></div>
          <div class="field" id="source-github-field"><label for="source-github">GitHub repository</label><input id="source-github" value="${esc(data.github || data.suggestion || '')}" placeholder="owner/repository" autocomplete="off" spellcheck="false"><div class="hint">Release/tag source, not the image registry.</div></div>
          <div class="field" id="source-managed-field"><label for="source-managed">Managed by <span class="opt">optional</span></label><input id="source-managed" value="${esc(data.managed || '')}" placeholder="parent stack compatibility matrix" autocomplete="off"></div>
          <label class="check" id="source-calendar-field"><input type="checkbox" id="source-calendar" ${data.calendar ? 'checked' : ''}><span>This project uses calendar tags (YYYY.MM.DD), like SearXNG.</span></label>
          <div class="field"><label for="source-guidance">Update guidance <span class="opt">optional</span></label><textarea id="source-guidance" class="env-input" placeholder="What to back up or verify before updating this service.">${esc(data.guidance || '')}</textarea></div>
          <div id="source-editor-error"></div>
        `,
        footer: `<span class="note">Custom sources win over the catalog and auto-detection.</span><div class="spacer"></div><button class="btn ghost" type="button" data-cancel>Cancel</button><button class="btn primary" type="button" data-save-source>Save source</button>`,
      });
      const input = (id) => ref.body.querySelector(`#${id}`);
      const kindInput = input('source-kind');
      const errorBox = input('source-editor-error');
      const syncFields = () => {
        const kind = kindInput.value;
        input('source-hub-field').hidden = kind !== 'dockerhub';
        input('source-github-field').hidden = kind !== 'github';
        input('source-managed-field').hidden = kind !== 'managed';
        input('source-calendar-field').hidden = kind !== 'dockerhub';
      };
      kindInput.addEventListener('change', syncFields);
      syncFields();
      ref.footer.querySelector('[data-cancel]').addEventListener('click', ref.close);
      const save = ref.footer.querySelector('[data-save-source]');
      save.addEventListener('click', async () => {
        const repo = (fixed ? key : input('source-repo').value.trim());
        if (!repo) {
          errorBox.innerHTML = `<div class="danger-box">${icon('alert')}<div>Enter the image repository first.</div></div>`;
          return;
        }
        const kind = kindInput.value;
        const source = { kind };
        if (kind === 'dockerhub') {
          source.hub_repo = input('source-hub').value.trim();
          source.calendar_tags = input('source-calendar').checked;
        }
        if (kind === 'github') source.github_repo = input('source-github').value.trim();
        if (kind === 'managed') source.managed_by = input('source-managed').value.trim();
        source.label = input('source-label').value.trim();
        source.guidance = input('source-guidance').value.trim();
        errorBox.innerHTML = '';
        save.disabled = true;
        const label = save.textContent;
        save.textContent = 'Saving…';
        try {
          await ctx.api.post('/api/release-sources', { repo, source });
          ref.close();
          await ctx.reloadBootstrap();
          await ctx.refreshState({ force: true, render: false });
          paint();
          ui.toast('Update source saved', 'ok', `${repo} — checking releases`);
        } catch (error) {
          save.disabled = false;
          save.textContent = label;
          errorBox.innerHTML = `<div class="danger-box">${icon('alert')}<div>${esc(ui.errorText(error))}</div></div>`;
        }
      });
      if (!fixed) setTimeout(() => input('source-repo').focus(), 30);
    };

    const attach = () => {
      page.querySelectorAll('[data-theme-choice]').forEach((button) => {
        button.addEventListener('click', async () => {
          const mode = button.dataset.themeChoice;
          await ctx.setTheme(mode);
          page.querySelectorAll('[data-theme-choice]').forEach((node) => {
            const active = node.dataset.themeChoice === mode;
            node.classList.toggle('active', active);
            node.setAttribute('aria-pressed', String(active));
          });
          ui.toast(`Theme: ${mode[0].toUpperCase() + mode.slice(1)}`, 'ok', mode === 'system' ? 'Following the macOS appearance.' : '');
        });
      });
      page.querySelector('[data-action="save-settings"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        const auto = page.querySelector('#set-auto').checked;
        const autoWas = Boolean(ctx.state?.auto_update_enabled);
        if (auto && !autoWas) {
          const proceed = await ui.confirmDialog({
            title: 'Enable automatic updates?',
            message: 'Stacks with the “auto” policy will update themselves when a new stable release is detected, then verify health and roll back compose on failure. Critical stacks, including stacks with a custom safety profile, are always excluded.',
            confirmLabel: 'Enable automatic updates',
            danger: true,
          });
          if (!proceed) return;
        }
        button.disabled = true;
        try {
          await ctx.api.post('/api/settings', {
            poll_interval_s: Number(page.querySelector('#set-poll').value),
            release_ttl_s: Number(page.querySelector('#set-ttl').value),
            history_days: Number(page.querySelector('#set-history').value),
            health_timeout_s: Number(page.querySelector('#set-health').value),
            remote_root: page.querySelector('#set-root').value.trim(),
            check_releases_on_start: page.querySelector('#set-start-check').checked,
            auto_update_enabled: auto,
          });
          await ctx.reloadBootstrap();
          await ctx.refreshState({ force: true });
          ui.toast('Settings saved', 'ok');
        } catch (error) {
          ui.toast('Could not save settings', 'err', ui.errorText(error));
        } finally {
          button.disabled = false;
        }
      });
      page.querySelector('[data-action="save-token"]')?.addEventListener('click', async (event) => {
        const value = page.querySelector('#set-token').value.trim();
        if (!value) { ui.toast('Enter a token first', 'warn'); return; }
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await ctx.api.post('/api/settings', { github_token: value });
          page.querySelector('#set-token').value = '';
          await ctx.reloadBootstrap();
          paint();
          ui.toast('GitHub token saved', 'ok');
        } catch (error) {
          ui.toast('Could not save token', 'err', ui.errorText(error));
        } finally {
          button.disabled = false;
        }
      });
      page.querySelector('[data-action="clear-token"]')?.addEventListener('click', async () => {
        const proceed = await ui.confirmDialog({ title: 'Clear GitHub token?', message: 'Release checks fall back to the anonymous GitHub limit.', confirmLabel: 'Clear token', danger: true });
        if (!proceed) return;
        try {
          await ctx.api.post('/api/settings', { clear_github_token: true });
          await ctx.reloadBootstrap();
          paint();
          ui.toast('Token cleared', 'ok');
        } catch (error) {
          ui.toast('Could not clear token', 'err', ui.errorText(error));
        }
      });
      const rootsList = page.querySelector('#repo-roots-list');
      if (rootsList) {
        bindRootRemoval(rootsList);
        page.querySelector('[data-action="add-repo-root"]')?.addEventListener('click', () => {
          const input = page.querySelector('#set-repo-root-manual');
          if (addRoot(rootsList, input?.value || '')) input.value = '';
        });
        page.querySelector('#set-repo-root-manual')?.addEventListener('keydown', (event) => {
          if (event.key !== 'Enter') return;
          event.preventDefault();
          const input = page.querySelector('#set-repo-root-manual');
          if (addRoot(rootsList, input?.value || '')) input.value = '';
        });
        page.querySelector('[data-action="pick-repo-roots"]')?.addEventListener('click', async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          try {
            const picked = await nativePick({ directories: true, multiple: true });
            if (picked === null) {
              page.querySelector('#set-repo-root-manual')?.focus();
              ui.toast('Type a folder path, then Add path', 'info');
            } else if (!picked.length) {
              // cancelled
            } else {
              for (const path of picked) addRoot(rootsList, path);
            }
          } finally {
            button.disabled = false;
          }
        });
      }
      page.querySelector('[data-action="save-repo-settings"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const roots = readRoots(page.querySelector('#repo-roots-list'));
          await ctx.api.post('/api/settings', {
            repo_roots: roots,
            repo_poll_interval_s: Number(page.querySelector('#set-repo-poll').value),
            gitea_url: page.querySelector('#set-gitea-url').value.trim(),
            gitea_owner: page.querySelector('#set-gitea-owner').value.trim(),
            gitea_timeout_s: Number(page.querySelector('#set-gitea-timeout').value),
            gitea_prune: page.querySelector('#set-gitea-prune').checked,
            gitea_force: page.querySelector('#set-gitea-force').checked,
            gitea_check_on_start: page.querySelector('#set-gitea-startup').checked,
          });
          await ctx.reloadBootstrap();
          await ctx.refreshState({ force: true });
          ui.toast('Project settings saved', 'ok');
        } catch (error) {
          ui.toast('Could not save project settings', 'err', ui.errorText(error));
        } finally {
          button.disabled = false;
        }
      });
      page.querySelector('[data-action="manage-repos"]')?.addEventListener('click', () => openRepoSelection(ctx));
      page.querySelector('[data-action="save-gitea-token"]')?.addEventListener('click', async (event) => {        const value = page.querySelector('#set-gitea-token').value.trim();
        if (!value) { ui.toast('Enter a Gitea token first', 'warn'); return; }
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await ctx.api.post('/api/settings', { gitea_token: value });
          page.querySelector('#set-gitea-token').value = '';
          await ctx.reloadBootstrap();
          await ctx.refreshState({ force: true });
          ui.toast('Gitea token saved', 'ok', 'Run Check Gitea to test it.');
        } catch (error) {
          ui.toast('Could not save Gitea token', 'err', ui.errorText(error));
        } finally {
          button.disabled = false;
        }
      });
      page.querySelector('[data-action="clear-gitea-token"]')?.addEventListener('click', async () => {
        const proceed = await ui.confirmDialog({
          title: 'Clear the Gitea token?',
          message: 'Ref checks and backups fall back to the git credential helper. If that cannot answer in the background, Gitea actions will report an auth error until a token is set again.',
          confirmLabel: 'Clear token',
          danger: true,
        });
        if (!proceed) return;
        try {
          await ctx.api.post('/api/settings', { clear_gitea_token: true });
          await ctx.reloadBootstrap();
          await ctx.refreshState({ force: true });
          ui.toast('Gitea token cleared', 'ok');
        } catch (error) {
          ui.toast('Could not clear Gitea token', 'err', ui.errorText(error));
        }
      });
      page.querySelector('[data-action="check-gitea-settings"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await ctx.api.post('/api/repos/gitea/check');
          ui.toast('Checking Gitea refs', 'ok', 'Open Projects to watch the column fill.');
          setTimeout(() => ctx.refreshState({ force: true }), 1200);
          setTimeout(() => ctx.refreshState({ force: true }), 4000);
        } catch (error) {
          button.disabled = false;
          ui.toast('Could not start the Gitea check', 'err', ui.errorText(error));
        }
      });
      page.querySelector('[data-add-source]')?.addEventListener('click', () => openSourceEditor({ key: '', kind: 'dockerhub', origin: 'new' }));
      page.querySelectorAll('[data-edit-source]').forEach((button) => {
        button.addEventListener('click', () => {
          const key = button.dataset.editSource;
          const configured = (ctx.bootstrap?.settings?.release_sources || {})[key] || {};
          openSourceEditor(sourceDataFor(key, configured));
        });
      });
      page.querySelectorAll('[data-fill-source]').forEach((button) => {
        button.addEventListener('click', () => {
          openSourceEditor(sourceDataFor(button.dataset.fillSource));
        });
      });
      page.querySelectorAll('[data-remove-source]').forEach((button) => {
        button.addEventListener('click', async () => {
          const key = button.dataset.removeSource;
          const proceed = await ui.confirmDialog({
            title: 'Remove this update source?',
            message: 'The image falls back to the built-in catalog or auto-detection.',
            detail: `<span class="mono">${esc(key)}</span>`,
            confirmLabel: 'Remove',
            danger: true,
          });
          if (!proceed) return;
          try {
            await ctx.api.post('/api/release-sources', { repo: key, delete: true });
            await ctx.reloadBootstrap();
            await ctx.refreshState({ force: true, render: false });
            paint();
            ui.toast('Update source removed', 'ok', key);
          } catch (error) {
            ui.toast('Could not remove the source', 'err', ui.errorText(error));
          }
        });
      });

      const backupStatus = page.querySelector('#backup-status');
      page.querySelector('[data-action="pick-backup-save"]')?.addEventListener('click', async () => {
        const input = page.querySelector('#backup-path');
        const picked = await nativePick({ mode: 'save', extensions: ['zip'], filename: input?.value.split('/').pop() || 'hlc-backup.zip' });
        if (picked?.length) input.value = picked[0];
        else if (picked === null) input?.focus();
      });
      page.querySelector('[data-action="pick-backup-open"]')?.addEventListener('click', async () => {
        const input = page.querySelector('#restore-path');
        const picked = await nativePick({ mode: 'open', extensions: ['zip'] });
        if (picked?.length) input.value = picked[0];
        else if (picked === null) input?.focus();
      });
      page.querySelector('[data-action="create-backup"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        const path = page.querySelector('#backup-path')?.value.trim() || '';
        button.disabled = true;
        if (backupStatus) backupStatus.textContent = 'Writing backup…';
        try {
          const result = await ctx.api.post('/api/backup', { path });
          if (backupStatus) backupStatus.innerHTML = `Backup written to <span class="mono">${esc(result.path)}</span> · ${ui.bytes(result.bytes)}`;
          ui.toast('Backup created', 'ok', result.path);
        } catch (error) {
          if (backupStatus) backupStatus.textContent = ui.errorText(error);
          ui.toast('Could not create the backup', 'err', ui.errorText(error));
        } finally {
          button.disabled = false;
        }
      });
      page.querySelector('[data-action="restore-backup"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        const path = page.querySelector('#restore-path')?.value.trim() || '';
        const status = page.querySelector('#restore-status');
        if (!path) { if (status) status.textContent = 'Choose a backup file first.'; return; }
        const proceed = await ui.confirmDialog({
          title: 'Restore this backup?',
          message: 'Hosts, stacks and settings will be replaced. A pre-restore backup is written first; local telemetry and logs stay.',
          detail: `<span class="mono">${esc(path)}</span>`,
          confirmLabel: 'Restore',
          danger: true,
        });
        if (!proceed) return;
        button.disabled = true;
        if (status) status.textContent = 'Restoring…';
        try {
          const result = await ctx.api.post('/api/backup/restore', { path });
          if (status) status.innerHTML = `Restored ${result.stacks} stacks and ${result.hosts} hosts. Pre-restore backup: <span class="mono">${esc(result.pre_restore_backup)}</span>`;
          ui.toast('Backup restored', 'ok', 'Reloading the console data.');
          await ctx.reloadBootstrap();
          await ctx.refreshState({ force: true });
          ctx.navigate('#/fleet');
        } catch (error) {
          if (status) status.textContent = ui.errorText(error);
          ui.toast('Could not restore the backup', 'err', ui.errorText(error));
        } finally {
          button.disabled = false;
        }
      });
      page.querySelector('[data-action="clear-history"]')?.addEventListener('click', async () => {
        const proceed = await ui.confirmDialog({
          title: 'Clear telemetry history?',
          message: 'Host and stack samples are deleted. Uptime charts restart from zero; events and run logs stay.',
          confirmLabel: 'Clear history',
          danger: true,
        });
        if (!proceed) return;
        try {
          await ctx.api.post('/api/maintenance/clear-history', {});
          await ctx.refreshState({ force: true });
          ui.toast('History cleared', 'ok');
        } catch (error) {
          ui.toast('Could not clear history', 'err', ui.errorText(error));
        }
      });
      setupSettingsNav();
    };

    const focusRequestedSection = () => {
      const id = ctx.uiState.settingsScrollTo;
      if (!id) return;
      delete ctx.uiState.settingsScrollTo;
      const target = document.getElementById(id);
      if (!target) return;
      requestAnimationFrame(() => {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        target.querySelector('button, input, select, textarea')?.focus({ preventScroll: true });
      });
    };

    attach();
    if (!settingsData(ctx)) loadSystem().finally(focusRequestedSection);
    else focusRequestedSection();
  },
};
