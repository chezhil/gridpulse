/* Shared vector symbols for the live Canvas topology and Cytoscape Lab. */
(() => {
  const paths = {
    Tower: [
      'M0 -28 L-9 -15 L-17 26 M0 -28 L9 -15 L17 26',
      'M-27 -13 L-9 -18 H9 L27 -13 M-27 -13 H27',
      'M-27 1 L-11 -3 H11 L27 1 M-27 1 H27',
      'M-9 -14 L10 1 M9 -14 L-10 1 M-12 2 L16 25 M12 2 L-16 25',
      'M-21 26 H21',
      'M-24 -12 V-8 M24 -12 V-8 M-24 2 V6 M24 2 V6',
      'M-28 -8 H-20 V-2 H-28 Z M20 -8 H28 V-2 H20 Z M-28 6 H-20 V12 H-28 Z M20 6 H28 V12 H20 Z'
    ],
    HydroPlant: [
      'M-24 18 V-11 H24 V18 Z M-24 -5 H24',
      'M0 -17 V-11 M-9 -17 H9',
      'M0 -4 A10 10 0 1 0 0 16 A10 10 0 1 0 0 -4',
      'M0 -4 V16 M-10 6 H10'
    ],
    Substation: [
      'M-23 20 V-16 H23 V20 Z M-16 -22 V-16 M16 -22 V-16',
      'M-9 -9 H9 V13 H-9 Z M-4 -4 H4 M-4 2 H4 M-4 8 H4',
      'M-29 8 H-23 M23 8 H29'
    ],
    BESS: [
      'M-23 -16 H20 V16 H-23 Z M20 -6 H26 V6 H20',
      'M-16 -10 V10 M-8 -10 V10 M0 -10 V10 M8 -10 V10'
    ],
    HOSPITAL: [
      'M-20 -20 H20 V20 H-20 Z M-5 -13 H5 V-5 H13 V5 H5 V13 H-5 V5 H-13 V-5 H-5 Z'
    ],
    RESIDENTIAL: [
      'M-25 -1 L0 -22 L25 -1 M-19 -5 V20 H19 V-5 M-5 20 V6 H5 V20'
    ],
    COMMERCIAL: [
      'M-23 20 V-8 H-6 V20 M-6 20 V-20 H22 V20 Z',
      'M-17 -1 H-13 M-17 7 H-13 M2 -12 H7 M13 -12 H18 M2 -3 H7 M13 -3 H18 M2 6 H7 M13 6 H18'
    ],
    INDUSTRIAL: [
      'M-26 20 V-7 L-11 1 L1 -7 L12 1 L23 -7 V20 Z M13 -7 V-23 H20 V-9',
      'M-18 11 H-13 M-5 11 H0 M8 11 H13'
    ],
    Consumer: ['M0 -21 A21 21 0 1 0 0 21 A21 21 0 1 0 0 -21 M-9 0 H9 M0 -9 V9']
  };
  const aliases = {buses:'Tower',substations:'Substation',generators:'HydroPlant',loads:'Consumer',batteries:'BESS'};
  const iconPaths = kind => paths[aliases[kind] || kind] || paths.Consumer;
  const safeColor = color => /^#[0-9a-f]{3,8}$/i.test(color) ? color : '#38bdf8';
  const cache = new Map();
  function url(kind, color) {
    const key = `${kind}:${color}`;
    if (cache.has(key)) return cache.get(key);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" fill="none"><g transform="translate(32 32)" stroke="${safeColor(color)}" stroke-width="${kind === 'Tower' ? '2.5' : '3.2'}" stroke-linecap="round" stroke-linejoin="round">${iconPaths(kind).map(d => `<path d="${d}"/>`).join('')}</g></svg>`;
    const value = `data:image/svg+xml,${encodeURIComponent(svg)}`;
    cache.set(key, value);
    return value;
  }
  function draw(ctx, kind, color, size = 38) {
    ctx.save();
    ctx.scale(size / 64, size / 64);
    ctx.strokeStyle = safeColor(color);
    ctx.lineWidth = kind === 'Tower' ? 2.5 : 3.2;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    iconPaths(kind).forEach(d => ctx.stroke(new Path2D(d)));
    ctx.restore();
  }
  function cytoscapeStyle(kind, color, width = 34, height = 34) {
    return {shape:'round-rectangle',width,height,'background-color':'#0d2238','border-width':1.2,'border-color':safeColor(color),
      'background-image':url(kind,color),'background-fit':'contain','background-width':'100%','background-height':'100%','background-position-x':'50%','background-position-y':'50%'};
  }
  window.GridPulseIcons = {draw,url,cytoscapeStyle};
})();
