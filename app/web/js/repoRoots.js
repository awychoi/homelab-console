import * as ui from './ui.js';

const { esc } = ui;

export function rootsFromSettings(settings) {
  const list = settings?.repo_roots;
  if (Array.isArray(list)) return list.map(String).filter(Boolean);
  return settings?.repo_root ? [String(settings.repo_root)] : [];
}

export function rootsHtml(roots) {
  if (!roots.length) return '<div class="muted tiny">No folders yet.</div>';
  return roots.map((root) => `<div class="root-item" data-root-path="${esc(root)}">
    <span class="mono" title="${esc(root)}">${esc(root)}</span>
    <button class="btn ghost sm root-remove" type="button" data-remove-root aria-label="Remove ${esc(root)}" title="Remove">×</button>
  </div>`).join('');
}

export function readRoots(container) {
  return [...container.querySelectorAll('[data-root-path]')].map((node) => node.dataset.rootPath).filter(Boolean);
}

export function writeRoots(container, roots) {
  container.innerHTML = rootsHtml(roots);
}

export function addRoot(container, value) {
  const path = String(value || '').trim();
  if (!path) return false;
  const roots = readRoots(container);
  if (roots.includes(path)) return false;
  roots.push(path);
  writeRoots(container, roots);
  return true;
}

export function bindRootRemoval(container) {
  container.addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-root]');
    if (!button) return;
    const item = button.closest('[data-root-path]');
    if (!item) return;
    item.remove();
    if (!container.querySelector('[data-root-path]')) {
      container.innerHTML = '<div class="muted tiny">No folders yet.</div>';
    }
  });
}

export function rootsLabel(roots) {
  if (!roots.length) return 'not configured';
  if (roots.length === 1) return roots[0];
  return `${roots.length} folders`;
}

export async function openRepoSelection(ctx) {
  let payload;
  try {
    payload = await ctx.api.get('/api/repos/discovered');
  } catch (error) {
    ui.toast('Could not scan project folders', 'err', ui.errorText(error));
    return;
  }
  const roots = payload.roots || [];
  const repos = payload.repos || [];
  const errors = payload.errors || [];
  const byRoot = new Map(roots.map((root) => [root, []]));
  for (const repo of repos) {
    if (!byRoot.has(repo.root)) byRoot.set(repo.root, []);
    byRoot.get(repo.root).push(repo);
  }
  const groups = [...byRoot.entries()].map(([root, entries]) => {
    const rows = entries.length
      ? entries.map((repo) => `<label class="check repo-choice">
          <input type="checkbox" data-repo-choice data-root="${esc(repo.root)}" data-rel="${esc(repo.rel)}" ${repo.selected ? 'checked' : ''}>
          <span><strong>${esc(repo.name)}</strong>${repo.root_repo ? ' <span class="pill tiny neutral">selected folder</span>' : ''}
            <span class="sub mono">${esc(repo.rel)}</span></span>
        </label>`).join('')
      : '<div class="muted tiny">No git projects found here.</div>';
    return `<div class="section">
      <div class="section-head"><div class="section-title mono">${esc(root)}</div><div class="section-sub">${entries.length} ${entries.length === 1 ? 'project' : 'projects'}</div></div>
      <div class="card"><div class="card-body repo-choice-list">${rows}</div></div>
    </div>`;
  }).join('');
  const body = `
    <div class="hint">Each folder is scanned recursively. Hidden folders, dependency/build folders, and nested projects inside a project are skipped. Unchecked projects stay on disk but disappear from Repos.</div>
    ${errors.length ? `<div class="warn-box mt-2">${ui.icon('alert')}<div>${errors.map((error) => esc(error)).join('<br>')}</div></div>` : ''}
    ${groups || '<div class="empty mini"><strong>No project folders configured.</strong><p>Add one in Settings first.</p></div>'}
    <div id="repo-selection-error" class="mt-2"></div>`;
  const ref = ui.openModal({
    title: 'Manage projects',
    subtitle: 'Choose which discovered git checkouts Repos should manage',
    className: 'wide',
    body,
    footer: `<button class="btn ghost sm" type="button" data-select-all>Select all</button><button class="btn ghost sm" type="button" data-select-none>Select none</button><div class="spacer"></div><button class="btn ghost" data-cancel>Cancel</button><button class="btn primary" type="button" data-save-selection ${repos.length ? '' : 'disabled'}>Save selection</button>`,
  });
  const boxes = () => [...ref.body.querySelectorAll('[data-repo-choice]')];
  ref.footer.querySelector('[data-select-all]')?.addEventListener('click', () => {
    for (const box of boxes()) box.checked = true;
  });
  ref.footer.querySelector('[data-select-none]')?.addEventListener('click', () => {
    for (const box of boxes()) box.checked = false;
  });
  ref.footer.querySelector('[data-cancel]')?.addEventListener('click', ref.close);
  const save = ref.footer.querySelector('[data-save-selection]');
  save?.addEventListener('click', async () => {
    const exclusions = {};
    for (const root of byRoot.keys()) {
      const excluded = boxes()
        .filter((box) => box.dataset.root === root && !box.checked)
        .map((box) => box.dataset.rel);
      if (excluded.length) exclusions[root] = excluded;
    }
    const errorBox = ref.body.querySelector('#repo-selection-error');
    if (errorBox) errorBox.innerHTML = '';
    save.disabled = true;
    const label = save.textContent;
    save.textContent = 'Saving…';
    try {
      await ctx.api.post('/api/repos/selection', { exclusions });
      ref.close();
      await ctx.refreshState({ force: true });
      ui.toast('Project selection saved', 'ok');
    } catch (error) {
      save.disabled = false;
      save.textContent = label;
      if (errorBox) errorBox.innerHTML = `<div class="danger-box">${esc(ui.errorText(error))}</div>`;
    }
  });
}
