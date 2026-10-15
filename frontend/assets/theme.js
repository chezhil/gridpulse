(() => {
  let saved = 'dark';
  try { saved = localStorage.getItem('gridpulse_theme') || 'dark'; } catch {}
  const apply = value => {
    const dark = value !== 'light';
    document.documentElement.classList.toggle('dark', dark);
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      button.textContent = dark ? '☀ Morning' : '☾ Night';
      button.setAttribute('aria-label', dark ? 'Switch to morning theme' : 'Switch to night theme');
    });
    window.dispatchEvent(new CustomEvent('gridpulse-theme', { detail: dark ? 'dark' : 'light' }));
  };
  window.gridpulseTheme = { apply, toggle() {
    const next = document.documentElement.classList.contains('dark') ? 'light' : 'dark';
    try { localStorage.setItem('gridpulse_theme', next); } catch {}
    apply(next);
  }};
  apply(saved);
  document.addEventListener('DOMContentLoaded', () => {
    apply(saved);
    document.querySelectorAll('[data-theme-toggle]').forEach(button => button.addEventListener('click', window.gridpulseTheme.toggle));
  });
  window.addEventListener('storage', event => { if (event.key === 'gridpulse_theme') apply(event.newValue); });
})();
