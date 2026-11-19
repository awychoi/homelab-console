import { api } from './api.js';
import * as ui from './ui.js';
import { nativePick } from './native.js';

const { esc, icon, toast, openModal } = ui;

const SLUG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,38}\/[\w.-]{1,100}$/;

function versionPill(candidate) {
  const bits = [];
  if (candidate.version) bits.push(`<span class="pill tiny neutral">v${esc(candidate.version)}</span>`);
  bits.push(candidate.manifest
    ? ui.pill('ok', 'reports versions')
    : ui.pill('neutral', 'no manifest'));
  return bits.join(' ');
}

function choiceHtml(candidate, current) {
  const active = Boolean(current && current.path === candidate.path);
  return `<div class="app-choice${active ? ' selected' : ''}">
    <div class="app-choice-main">
      <div class="app-choice-name"><strong>${esc(candidate.name)}</strong>${versionPill(candidate)}</div>
      <div class="mono tiny">${esc(candidate.path)}</div>
    </div>
    <button class="btn ghost sm" type="button" data-app-use="${esc(candidate.path)}" ${active ? 'disabled' : ''}>${active ? 'In use' : 'Use'}</button>
  </div>`;
}

function dependencyStatus(item) {
  if (!item) return '<span class="chip neutral">not checked yet</span>';
  if (item.status === 'behind') {
    return `<span class="chip warn" title="${esc(item.url || '')}">${esc(item.installed)} → ${esc(item.latest)}</span>`;
  }
  if (item.status === 'current') return `<span class="chip good">${esc(item.installed)} up to date</span>`;
  if (item.status === 'error') {
    return `<span class="chip bad" title="${esc(item.error || '')}">check failed</span>`;
  }
  return `<span class="chip neutral" title="The app does not report which version it bundles">version unknown</span>`;
}

function dependencyRow(item, status) {
  return `<div class="dep-row">
    <div class="dep-main">
      <span class="mono">${esc(item.repo)}</span>
      ${dependencyStatus(status)}
      ${status && status.latest && status.status === 'behind'
        ? `<a class="sub" href="${esc(status.url)}" target="_blank" rel="noopener noreferrer">release notes</a>`
        : ''}
    </div>
    <button class="btn ghost sm icon" type="button" data-dep-remove="${esc(item.repo)}" title="Stop tracking ${esc(item.repo)}" aria-label="Stop tracking ${esc(item.repo)}">${icon('trash', 12)}</button>
  </div>`;
}

function bodyHtml(state) {
  const app = state.app;
  const candidates = state.candidates || [];
  const dependencies = state.dependencies || [];
  return `<div class="section">
    <div class="section-head">
      <div class="section-title">App</div>
      <div class="section-sub">opens next to VS Code and GitHub on the project row</div>
    </div>
    ${app ? `<div class="app-current">
      <div class="app-choice-main">
        <div class="app-choice-name"><strong>${esc(app.name)}</strong>${versionPill(app)}</div>
        <div class="mono tiny">${esc(app.path)}</div>
        ${app.exists ? '' : '<div class="warn-box mt-1">' + icon('alert') + '<div>This bundle is missing or unreadable. Pick another app below.</div></div>'}
      </div>
      <div class="btn-group">
        <button class="btn primary sm" type="button" data-app-launch ${app.exists ? '' : 'disabled'}>${icon('play', 12)} Open app</button>
        <button class="btn ghost sm" type="button" data-app-clear>${icon('x', 12)} Remove</button>
      </div>
    </div>` : `<div class="info-box">${icon('info')}<div>No app is linked yet. The console only launches it and compares versions — building and updating stay inside the app.</div></div>`}
    <div class="field mt-2">
      <label>Bundles found in this project</label>
      ${candidates.length
        ? `<div class="app-choices">${candidates.map((candidate) => choiceHtml(candidate, app)).join('')}</div>`
        : '<div class="hint">No <span class="mono">.app</span> bundle was found. Pick one from disk below.</div>'}
    </div>
    <div class="app-actions">
      <label class="sr-only" for="app-path">App path</label>
      <input id="app-path" placeholder="/Applications/Your App.app" autocomplete="off" spellcheck="false">
      <button class="btn ghost sm" type="button" data-app-use-path>Use path</button>
      <button class="btn sm" type="button" data-app-pick>Choose…</button>
    </div>
  </div>
  <div class="section">
    <div class="section-head">
      <div class="section-title">Upstream dependencies</div>
      <div class="section-sub">newest GitHub release vs what the bundle reports</div>
    </div>
    ${dependencies.length
      ? `<div class="dep-list">${dependencies.map((item) => dependencyRow(item, state.status[item.repo.toLowerCase()])).join('')}</div>`
      : '<div class="hint">Nothing tracked yet. Add the repository whose releases this app bundles, for example <span class="mono">ip7z/7zip</span>.</div>'}
    <div class="app-actions">
      <label class="sr-only" for="dep-slug">Dependency repository</label>
      <input id="dep-slug" placeholder="owner/repository" autocomplete="off" spellcheck="false">
      <button class="btn ghost sm" type="button" data-dep-add>Add</button>
    </div>
    <div class="hint">The installed version comes from <span class="mono">${esc(state.manifestName)}</span> inside the bundle, so a self-update that rewrites it clears the flag by itself. An app without that file shows <em>version unknown</em> rather than a guess.</div>
  </div>`;
}

export async function showRepoApps(ctx, repo) {
  let data;
  try {
    data = await api.get(`/api/repos/${encodeURIComponent(repo.id)}/apps`);
  } catch (error) {
    toast('Could not read app details', 'err', ui.errorText(error));
    return;
  }
  const status = {};
  for (const item of repo.deps?.items || []) status[item.repo.toLowerCase()] = item;
  const state = {
    app: data.app || null,
    candidates: data.candidates || [],
    dependencies: (repo.deps?.items || []).map((item) => ({ repo: item.repo })),
    status,
    manifestName: data.manifest_name || 'homelab-app.json',
  };

  const ref = openModal({
    title: 'App and dependencies',
    subtitle: repo.rel,
    className: 'wide',
    body: bodyHtml(state),
    footer: `<div class="spacer"></div>
      <button class="btn ghost" type="button" data-deps-check ${state.dependencies.length ? '' : 'disabled'}>${icon('refresh', 12)} Check now</button>
      <button class="btn primary" type="button" data-done>Done</button>`,
  });

  const repaint = () => {
    ref.body.innerHTML = bodyHtml(state);
    ref.footer.querySelector('[data-deps-check]').disabled = !state.dependencies.length;
  };

  const refresh = async () => {
    try {
      await ctx.refreshState({ force: true });
    } catch {
      /* the modal keeps working with local state */
    }
  };

  const applyApp = async (path) => {
    try {
      const result = await api.post(`/api/repos/${encodeURIComponent(repo.id)}/app`, { path });
      state.app = result.app || null;
      if (state.app) toast('App linked', 'ok', `${state.app.name} · ${repo.rel}`);
      else toast('App unlinked', 'ok', repo.rel);
      repaint();
      await refresh();
    } catch (error) {
      toast('Could not save the app', 'err', ui.errorText(error));
    }
  };

  const saveDependencies = async (dependencies) => {
    try {
      const result = await api.post(
        `/api/repos/${encodeURIComponent(repo.id)}/dependencies`,
        { dependencies },
      );
      state.dependencies = result.dependencies || [];
      repaint();
      await refresh();
      if (state.dependencies.length) {
        toast('Dependency saved', 'ok', 'The check runs in the background.');
      }
    } catch (error) {
      toast('Could not save dependencies', 'err', ui.errorText(error));
    }
  };

  ref.body.addEventListener('click', async (event) => {
    const useButton = event.target.closest('[data-app-use]');
    if (useButton) return applyApp(useButton.dataset.appUse);

    if (event.target.closest('[data-app-pick]')) {
      const picked = await nativePick({ directories: true, multiple: false });
      if (picked === null) {
        toast('Type the bundle path instead', 'info');
        return;
      }
      if (picked && picked[0]) applyApp(picked[0]);
      return;
    }

    if (event.target.closest('[data-app-use-path]')) {
      const input = ref.body.querySelector('#app-path');
      const value = (input?.value || '').trim();
      if (!value) {
        input?.focus();
        return;
      }
      return applyApp(value);
    }

    if (event.target.closest('[data-app-launch]')) {
      try {
        await api.post(`/api/repos/${encodeURIComponent(repo.id)}/app/open`);
        toast('Opening app', 'ok', state.app?.name || repo.rel);
      } catch (error) {
        toast('Could not open the app', 'err', ui.errorText(error));
      }
      return;
    }

    if (event.target.closest('[data-app-clear]')) {
      return applyApp('');
    }

    const removeButton = event.target.closest('[data-dep-remove]');
    if (removeButton) {
      const slug = removeButton.dataset.depRemove;
      return saveDependencies(state.dependencies.filter((item) => item.repo !== slug));
    }

    if (event.target.closest('[data-dep-add]')) {
      const input = ref.body.querySelector('#dep-slug');
      const value = (input?.value || '').trim();
      if (!SLUG_RE.test(value)) {
        toast('Use owner/repository', 'warn', 'For example ip7z/7zip');
        input?.focus();
        return;
      }
      if (state.dependencies.some((item) => item.repo.toLowerCase() === value.toLowerCase())) {
        toast('Already tracked', 'info', value);
        return;
      }
      if (input) input.value = '';
      return saveDependencies([...state.dependencies, { repo: value }]);
    }

    if (event.target.closest('[data-deps-check]')) {
      try {
        await api.post('/api/repos/dependencies/check');
        toast('Checking upstream releases', 'ok', 'The row and this list update when it finishes.');
        setTimeout(() => ctx.refreshState({ force: true }), 1500);
      } catch (error) {
        toast('Could not start the check', 'err', ui.errorText(error));
      }
    }
  });

  ref.body.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    if (event.target.id === 'dep-slug') {
      event.preventDefault();
      ref.body.querySelector('[data-dep-add]')?.click();
    } else if (event.target.id === 'app-path') {
      event.preventDefault();
      ref.body.querySelector('[data-app-use-path]')?.click();
    }
  });

  ref.footer.querySelector('[data-done]').addEventListener('click', ref.close);
}
