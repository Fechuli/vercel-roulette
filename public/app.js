const MESI = ['gen', 'feb', 'mar', 'apr', 'mag', 'giu', 'lug', 'ago', 'set', 'ott', 'nov', 'dic'];
const PAGE = 200;

const $ = id => document.getElementById(id);
const fmt = p => { const [y, m] = p.split('-'); return MESI[+m - 1] + ' ' + y; };
const url = name => 'https://' + name + '.vercel.app';
const store = {
  get(key, fallback) {
    try { const v = JSON.parse(localStorage.getItem(key)); return v === null ? fallback : v; } catch (e) { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {} },
};

let names = [], last = '', periods = [];
let opened = new Set(store.get('vr_opened', []));
let hist = store.get('vr_hist', []);
let matches = [], shown = 0;

const periodOf = i => last.charCodeAt(i) - 97;

function filter() {
  const q = $('q').value.trim().toLowerCase();
  const min = +$('since').value;
  matches = [];
  for (let i = 0; i < names.length; i++) {
    if (periodOf(i) < min) continue;
    if (q && !names[i].includes(q)) continue;
    matches.push(i);
  }
  shown = 0;
  $('list').innerHTML = '';
  $('count').textContent = matches.length.toLocaleString('it-IT') + ' siti';
  more();
}

function link(name) {
  const a = document.createElement('a');
  a.href = url(name);
  a.target = '_blank';
  a.rel = 'noopener noreferrer nofollow';
  a.textContent = name;
  return a;
}

function item(i) {
  const li = document.createElement('li');
  const a = link(names[i]);
  if (opened.has(names[i])) a.className = 'vis';
  const when = document.createElement('span');
  when.textContent = fmt(periods[periodOf(i)]);
  a.append(when);
  a.addEventListener('click', () => { a.className = 'vis'; mark(names[i]); });
  li.append(a);
  return li;
}

function more() {
  const frag = document.createDocumentFragment();
  const end = Math.min(shown + PAGE, matches.length);
  for (; shown < end; shown++) frag.append(item(matches[shown]));
  $('list').append(frag);
  $('more').hidden = shown >= matches.length;
}

function mark(name) {
  opened.add(name);
  hist = [name].concat(hist.filter(x => x !== name)).slice(0, 40);
  store.set('vr_opened', Array.from(opened).slice(-20000));
  store.set('vr_hist', hist);
  drawHist();
}

function drawHist() {
  $('recent').hidden = !hist.length;
  $('hist').replaceChildren(...hist.map(name => {
    const li = document.createElement('li');
    li.append(link(name));
    return li;
  }));
}

// Sites already opened are never picked again.
function random() {
  const pool = matches.filter(i => !opened.has(names[i]));
  if (!pool.length) { $('count').textContent = 'Hai già aperto tutti i siti con questi filtri'; return; }
  const name = names[pool[Math.floor(Math.random() * pool.length)]];
  window.open(url(name), '_blank', 'noopener,noreferrer');
  mark(name);
}

async function init() {
  try {
    const res = await fetch('/data/sites.json');
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    names = data.names.split('\n');
    last = data.last;
    periods = data.periods;
    $('total').textContent = names.length.toLocaleString('it-IT');
    $('range').textContent = ` (${periods[0].slice(0, 4)}–${periods[periods.length - 1].slice(0, 4)})`;
    $('generated').textContent = ', aggiornati al ' + new Date(data.generated).toLocaleDateString('it-IT', { day: 'numeric', month: 'long', year: 'numeric' });
  } catch (e) {
    $('count').textContent = "Non riesco a caricare l'elenco. Ricarica la pagina.";
    return;
  }

  const since = $('since');
  periods.forEach((p, i) => since.add(new Option(i === 0 ? 'qualsiasi data' : fmt(p), i)));
  const savedSince = store.get('vr_since', null);
  const saved = savedSince === 'any' ? 0 : periods.indexOf(savedSince);
  since.value = saved === -1 ? periods.length - 1 : saved;

  let timer;
  $('q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(filter, 150); });
  since.addEventListener('change', () => { store.set('vr_since', since.value === '0' ? 'any' : periods[+since.value]); filter(); });
  $('rnd').addEventListener('click', random);
  $('more').addEventListener('click', more);
  document.addEventListener('keydown', e => {
    if (e.ctrlKey || e.metaKey || e.altKey || e.key.toLowerCase() !== 'r') return;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
    random();
  });

  for (const id of ['q', 'rnd', 'since']) $(id).disabled = false;
  drawHist();
  filter();
  $('q').focus();
}

init();
