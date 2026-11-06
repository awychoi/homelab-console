/* Applied before the stylesheet to avoid a flash of the wrong theme. */
(function () {
  var root = document.documentElement;
  var modes = ['dark', 'light', 'system'];
  var injected = root.getAttribute('data-theme-mode') || '';
  var stored = '';
  try { stored = localStorage.getItem('hlc-theme') || ''; } catch (e) { stored = ''; }
  var mode = modes.indexOf(injected) >= 0 ? injected : (modes.indexOf(stored) >= 0 ? stored : 'system');
  var media = window.matchMedia('(prefers-color-scheme: light)');

  function resolve(value) {
    if (value === 'system') return media.matches ? 'light' : 'dark';
    return value === 'light' ? 'light' : 'dark';
  }
  function apply(value) {
    mode = modes.indexOf(value) >= 0 ? value : 'system';
    var theme = resolve(mode);
    root.setAttribute('data-theme', theme);
    root.setAttribute('data-theme-mode', mode);
    try { localStorage.setItem('hlc-theme', mode); } catch (e) { /* private mode */ }
    var meta = document.getElementById('theme-color');
    if (meta) meta.setAttribute('content', theme === 'light' ? '#edf2f9' : '#131b27');
  }

  apply(mode);
  media.addEventListener('change', function () { if (mode === 'system') apply('system'); });
  window.__hlcTheme = { mode: function () { return mode; }, resolve: resolve, apply: apply };
})();
