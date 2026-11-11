"""Raccoglie i sottodomini *.vercel.app dal web graph di Common Crawl e genera i dati del sito.

    python -I scripts/build_data.py            # usa la cache e aggiunge l'ultima release, se è uscita
    python -I scripts/build_data.py --refresh  # riscarica anche le release già in cache

Scrive public/data/sites.json, che è l'elenco letto dal sito, e in exports/ l'elenco
completo in CSV più i soli progetti in TXT.
"""
import argparse
import csv
import gzip
import io
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / 'cache'
EXPORTS = ROOT / 'exports'
SITE_DATA = ROOT / 'public' / 'data' / 'sites.json'
BLACKLIST = ROOT / 'blacklist.txt'  # dead sites, written by check_alive.py

BASE = 'https://data.commoncrawl.org/'
GRAPH = BASE + 'projects/hyperlinkgraph/{r}/host/{r}-host-vertices.paths.gz'
PREFIX = 'app.vercel.'  # the graph stores hostnames reversed and sorted
SUFFIX = '.vercel.app'
USER_AGENT = 'vercel-roulette/1.0'
MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

# One release about every 3 months. Each release merges three monthly crawls, and
# these windows do not overlap. Newer releases are found on their own (newest_release).
RELEASES = [
    'cc-main-2023-may-sep-nov',
    'cc-main-2024-feb-apr-may',
    'cc-main-2024-may-jun-jul',
    'cc-main-2024-aug-sep-oct',
    'cc-main-2024-25-nov-dec-jan',
    'cc-main-2025-feb-mar-apr',
    'cc-main-2025-may-jun-jul',
    'cc-main-2025-aug-sep-oct',
    'cc-main-2025-26-nov-dec-jan',
    'cc-main-2026-feb-mar-apr',
    'cc-main-2026-may-jun-jul',
    'cc-main-2026-aug-sep-oct',
]

# Preview URLs that Vercel creates for every deploy:
# <project>-<9 char hash>-<team> and <project>-git-<branch>-<team>.
DEPLOY_HASH = re.compile(r'-(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{9}(-|$)')

# Names that look like phishing kits (fake logins, wallet drainers). They stay in the
# exports, flagged, but are kept off the public site. Some false positives are accepted.
SUSPECT = re.compile(
    r'login|log-in|signin|sign-in|verif|validat|authenti|password|recover|restore|unlock|'
    r'wall?i?et|metamask|exodus|trez[o0]r|ledger|ledgier|ledgger|ladger|coinbase|binance|kucoin|'
    r'airdrop|claim|seed|phrase|dapp|usdt|webmail|mailru|outlook|office-?365|0ffice|paypal|'
    r'banking|secure|helpdesk|support-ticket|2fa|mfa'
)


def period(release):
    """'cc-main-2025-26-nov-dec-jan' -> '2026-01', the last month of the window."""
    parts = release.split('-')[2:]
    years = [p for p in parts if p.isdigit()]
    year = int(years[0]) if len(years) == 1 else int(years[0][:2] + years[1])
    return f'{year}-{MONTHS.index(parts[-1]) + 1:02d}'


def release_ending(year, month):
    """Name of the release whose three-month window ends in the given month."""
    window = '-'.join(MONTHS[(month - 3 + k) % 12] for k in range(3))
    years = str(year) if month >= 3 else f'{year - 1}-{year % 100:02d}'
    return f'cc-main-{years}-{window}'


def get(url, tries=4):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    for attempt in range(tries):
        try:
            return urllib.request.urlopen(request, timeout=120)
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
        except urllib.error.URLError:
            if attempt == tries - 1:
                raise
        time.sleep(5 * 2 ** attempt)


def newest_release():
    """The most recent release published after the last one in RELEASES, if any."""
    year, month = map(int, period(RELEASES[-1]).split('-'))
    found = None
    for _ in range(12):
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        name = release_ending(year, month)
        try:
            get(GRAPH.format(r=name), tries=2).close()
            found = name
        except urllib.error.HTTPError:
            pass
    return found


def fetch(release):
    """Stream the release's host list and keep the app.vercel.* block."""
    with get(GRAPH.format(r=release)) as response:
        parts = gzip.decompress(response.read()).decode().split()
    hosts = []
    for part in parts:
        with get(BASE + part) as response, gzip.GzipFile(fileobj=response) as gz:
            for line in io.TextIOWrapper(gz, encoding='utf-8', errors='replace'):
                host = line.rstrip('\n').split('\t')[-1]
                if host.startswith(PREFIX):
                    hosts.append(host)
                elif host > PREFIX:
                    return hosts  # sorted, so the block is over
    return hosts


def load(release, refresh):
    path = CACHE / f'{release}.txt'
    if refresh or not path.exists():
        hosts = fetch(release)
        CACHE.mkdir(exist_ok=True)
        path.write_text('\n'.join(hosts) + '\n', encoding='utf-8')
    return path.read_text(encoding='utf-8').split()


def read_blacklist():
    if not BLACKLIST.exists():
        return {}
    lines = BLACKLIST.read_text(encoding='utf-8').splitlines()
    return dict(line.split('\t', 1) for line in lines if line and not line.startswith('#'))


def kind(name):
    if '.' in name:
        return 'altro'
    if '-git-' in name:
        return 'anteprima-branch'
    if DEPLOY_HASH.search(name):
        return 'anteprima-deploy'
    return 'progetto'


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--refresh', action='store_true', help='riscarica anche le release già in cache')
    args = parser.parse_args()

    releases = list(RELEASES)
    newest = newest_release()
    if newest:
        print(f'Nuova release trovata: {newest}')
        releases.append(newest)

    seen = {}  # name -> [first period, last period, number of releases]
    for release in releases:
        p = period(release)
        hosts = load(release, args.refresh)
        print(f'{release}: {len(hosts):,} host')
        for reversed_host in hosts:
            name = '.'.join(reversed(reversed_host.split('.')))[:-len(SUFFIX)]
            entry = seen.setdefault(name, [p, p, 0])
            entry[0], entry[1], entry[2] = min(entry[0], p), max(entry[1], p), entry[2] + 1

    blacklist = read_blacklist()
    rows = sorted((name, kind(name), bool(SUSPECT.search(name)), *seen[name]) for name in seen)

    EXPORTS.mkdir(exist_ok=True)
    with open(EXPORTS / 'vercel_app_tutti.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['url', 'tipo', 'sospetto', 'morto', 'prima_vista', 'ultima_vista', 'n_release'])
        for name, k, suspect, first, last, n in rows:
            writer.writerow([f'https://{name}{SUFFIX}', k, 'si' if suspect else '',
                             blacklist.get(name, ''), first, last, n])
    with open(EXPORTS / 'vercel_app_progetti.txt', 'w', encoding='utf-8') as f:
        f.writelines(f'https://{name}{SUFFIX}\n' for name, k, *_ in rows
                     if k == 'progetto' and name not in blacklist)

    periods = sorted({period(r) for r in releases})
    assert len(periods) <= 26, 'the site encodes each period as one letter a-z'
    index = {p: i for i, p in enumerate(periods)}
    public = [(name, last) for name, k, suspect, _, last, _ in rows
              if k in ('progetto', 'altro') and not suspect and name not in blacklist]
    SITE_DATA.parent.mkdir(parents=True, exist_ok=True)
    SITE_DATA.write_text(json.dumps({
        'generated': date.today().isoformat(),
        'periods': periods,
        'names': '\n'.join(name for name, _ in public),
        'last': ''.join(chr(97 + index[last]) for _, last in public),
    }, separators=(',', ':')), encoding='utf-8')

    counts = Counter(k for _, k, *_ in rows)
    print(f'\n{len(rows):,} sottodomini unici: ' + ', '.join(f'{k} {v:,}' for k, v in counts.most_common()))
    print(f'Esclusi dal sito come sospetti: {sum(1 for r in rows if r[2] and r[1] in ("progetto", "altro")):,}')
    print(f'Esclusi perche morti (blacklist.txt): {len(blacklist):,}')
    print(f'Sul sito: {len(public):,} -> {SITE_DATA.relative_to(ROOT)} ({SITE_DATA.stat().st_size / 1e6:.1f} MB)')


if __name__ == '__main__':
    main()
