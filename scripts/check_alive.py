"""Controlla quali siti della lista rispondono ancora e mette in blacklist quelli morti.

    python -I scripts/check_alive.py             # controlla i siti mai controllati
    python -I scripts/check_alive.py --retry     # ricontrolla timeout ed errori temporanei
    python -I scripts/check_alive.py --rate 30   # richieste al secondo (default 40)

Fa una richiesta HEAD alla home di ogni sito in public/data/sites.json e salva l'esito in
cache/status.tsv, quindi si può interrompere e riprendere. Alla fine aggiorna blacklist.txt,
che build_data.py usa per togliere i siti dalla lista. Poi rilancia build_data.py.
"""
import argparse
import json
import socket
import ssl
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_DATA = ROOT / 'public' / 'data' / 'sites.json'
STATUS = ROOT / 'cache' / 'status.tsv'
BLACKLIST = ROOT / 'blacklist.txt'
USER_AGENT = 'vercel-roulette-linkcheck/1.0'
TIMEOUT = 12

# Results that mean the site cannot be visited: deleted or missing home (404, 410),
# blocked by Vercel (451), disabled or paused (402), behind Vercel login, or a hostname
# the vercel.app certificate does not cover (tls).
DEAD = {'404', '410', '451', '402', 'tls', 'vercel-login'}
# Results worth trying again later.
TRANSIENT = {'timeout', 'errore', '429', '500', '502', '503', '504'}


def check(name):
    host = f'{name}.vercel.app'
    request = (f'HEAD / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {USER_AGENT}\r\n'
               'Accept: text/html\r\nConnection: close\r\n\r\n').encode()
    head = b''
    try:
        with socket.create_connection((host, 443), timeout=TIMEOUT) as raw:
            with CONTEXT.wrap_socket(raw, server_hostname=host) as conn:
                conn.sendall(request)
                while b'\r\n\r\n' not in head and len(head) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    head += chunk
    except ssl.SSLCertVerificationError:
        return name, 'tls', ''
    except TimeoutError:
        return name, 'timeout', ''
    except OSError:
        return name, 'errore', ''

    lines = head.split(b'\r\n')
    parts = lines[0].split()
    if len(parts) < 2:
        return name, 'errore', ''
    code = parts[1].decode(errors='replace')
    vercel_error, sso = '', False
    for line in lines[1:]:
        key, _, value = line.partition(b':')
        key = key.strip().lower()
        if key == b'x-vercel-error':
            vercel_error = value.strip().decode(errors='replace')
        elif key == b'set-cookie' and b'_vercel_sso_nonce' in value:
            sso = True
    if code in ('401', '403') and sso:
        code = 'vercel-login'
    return name, code, vercel_error


def read_status():
    """Latest result per name."""
    status = {}
    if STATUS.exists():
        for line in STATUS.read_text(encoding='utf-8').splitlines():
            name, code, *_ = line.split('\t')
            status[name] = code
    return status


def write_blacklist(status):
    entries = {}
    if BLACKLIST.exists():
        for line in BLACKLIST.read_text(encoding='utf-8').splitlines():
            if line and not line.startswith('#'):
                name, _, reason = line.partition('\t')
                entries[name] = reason
    entries.update({name: code for name, code in status.items() if code in DEAD})
    for name, code in status.items():  # a site that came back to life leaves the list
        if code not in DEAD and code not in TRANSIENT:
            entries.pop(name, None)
    BLACKLIST.write_text(
        '# Siti esclusi dalla lista: nome<TAB>motivo. Generato da scripts/check_alive.py.\n'
        + ''.join(f'{name}\t{entries[name]}\n' for name in sorted(entries)),
        encoding='utf-8')
    return len(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--retry', action='store_true', help='ricontrolla timeout ed errori temporanei')
    parser.add_argument('--rate', type=float, default=40, help='richieste al secondo (default 40)')
    parser.add_argument('--limit', type=int, help='controlla al massimo N siti (per provare)')
    args = parser.parse_args()

    names = json.loads(SITE_DATA.read_text(encoding='utf-8'))['names'].split('\n')
    status = read_status()
    if args.retry:
        todo = [n for n in names if status.get(n) in TRANSIENT]
    else:
        todo = [n for n in names if n not in status]
    if args.limit:
        todo = todo[:args.limit]
    print(f'Da controllare: {len(todo):,} siti su {len(names):,}, {args.rate:g} al secondo', flush=True)

    STATUS.parent.mkdir(exist_ok=True)
    counts = Counter()
    today = date.today().isoformat()
    interval = 1 / args.rate
    started = time.monotonic()
    with open(STATUS, 'a', encoding='utf-8') as out, ThreadPoolExecutor(max_workers=64) as pool:
        pending = set()

        def collect(done):
            for future in done:
                name, code, vercel_error = future.result()
                status[name] = code
                counts[code] += 1
                out.write(f'{name}\t{code}\t{vercel_error}\t{today}\n')
            total = sum(counts.values())
            if done and total % 2000 < len(done):
                out.flush()
                elapsed = time.monotonic() - started
                dead = sum(counts[c] for c in DEAD)
                left = (len(todo) - total) / max(total / elapsed, 0.1)
                print(f'{total:,}/{len(todo):,}  morti {dead:,}  '
                      f'transitori {sum(counts[c] for c in TRANSIENT):,}  '
                      f'~{left / 60:.0f} min rimasti  {dict(counts.most_common(6))}', flush=True)

        for i, name in enumerate(todo):
            delay = started + i * interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            while len(pending) >= 64:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                collect(done)
            pending.add(pool.submit(check, name))
        done, _ = wait(pending)
        collect(done)

    blacklisted = write_blacklist(status)
    print(f'\nFatto: {dict(counts.most_common())}')
    print(f'blacklist.txt: {blacklisted:,} siti. Ora rilancia: python -I scripts/build_data.py')


CONTEXT = ssl.create_default_context()

if __name__ == '__main__':
    sys.exit(main())
