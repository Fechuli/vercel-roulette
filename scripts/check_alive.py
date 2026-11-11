"""Controlla quali siti della lista rispondono ancora e mette in blacklist quelli morti.

    python -I scripts/check_alive.py            # 5 richieste al secondo
    python -I scripts/check_alive.py --rate 3   # più piano

Fa una richiesta HEAD alla home di ogni sito in public/data/sites.json, partendo da quelli
visti più di recente, e salva l'esito in cache/status.tsv: si può interrompere e riprendere,
e ogni nuovo giro riprova i siti rimasti in errore. Se Vercel inizia a rifiutare le
connessioni lo script si ferma 15 minuti e riparte a metà velocità; dopo 6 pause si arrende.
Alla fine aggiorna blacklist.txt. Poi rilancia build_data.py.
"""
import argparse
import json
import socket
import ssl
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_DATA = ROOT / 'public' / 'data' / 'sites.json'
STATUS = ROOT / 'cache' / 'status.tsv'
BLACKLIST = ROOT / 'blacklist.txt'
USER_AGENT = 'vercel-roulette-linkcheck/1.0 (+https://github.com/Fechuli/vercel-roulette)'
TIMEOUT = 12
WORKERS = 16
PAUSE_MINUTES = 15
MAX_PAUSES = 6

# Results that mean the site cannot be visited: deleted or missing home (404, 410),
# blocked by Vercel (451), disabled or paused (402), behind Vercel login, or a hostname
# the vercel.app certificate does not cover (tls).
DEAD = {'404', '410', '451', '402', 'tls', 'vercel-login'}
# Results worth trying again on the next run. 'rifiutato' is Vercel closing the
# connection during the handshake, which is how its bot protection says "slow down".
TRANSIENT = {'rifiutato', 'timeout', 'errore', '429', '500', '502', '503', '504'}

CONTEXT = ssl.create_default_context()


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
    except (ssl.SSLEOFError, ConnectionResetError):
        return name, 'rifiutato', ''
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
    for name, code in status.items():
        if code in DEAD:
            entries[name] = code
        elif code not in TRANSIENT:
            entries.pop(name, None)  # a site that came back to life leaves the list
    BLACKLIST.write_text(
        '# Siti esclusi dalla lista: nome<TAB>motivo. Generato da scripts/check_alive.py.\n'
        + ''.join(f'{name}\t{entries[name]}\n' for name in sorted(entries)),
        encoding='utf-8')
    return len(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--rate', type=float, default=5, help='richieste al secondo (default 5)')
    parser.add_argument('--limit', type=int, help='controlla al massimo N siti (per provare)')
    args = parser.parse_args()

    data = json.loads(SITE_DATA.read_text(encoding='utf-8'))
    names, last = data['names'].split('\n'), data['last']
    status = read_status()
    todo = [i for i, n in enumerate(names) if n not in status or status[n] in TRANSIENT]
    todo.sort(key=lambda i: last[i], reverse=True)  # most recently seen first
    todo = [names[i] for i in todo[:args.limit]]
    rate = args.rate
    print(f'Da controllare: {len(todo):,} siti su {len(names):,}, {rate:g} al secondo', flush=True)

    STATUS.parent.mkdir(exist_ok=True)
    counts = Counter()
    recent = deque(maxlen=60)  # True when Vercel refused the connection
    today = date.today().isoformat()
    started = time.monotonic()
    stuck = 0         # consecutive pauses without a single accepted connection
    ok_at_pause = 0

    with open(STATUS, 'a', encoding='utf-8') as out, ThreadPoolExecutor(WORKERS) as pool:
        pending = set()

        def collect(done):
            for future in done:
                name, code, vercel_error = future.result()
                status[name] = code
                counts[code] += 1
                recent.append(code == 'rifiutato')
                out.write(f'{name}\t{code}\t{vercel_error}\t{today}\n')
            total = sum(counts.values())
            if done and total % 1000 < len(done):
                out.flush()
                speed = total / (time.monotonic() - started)
                print(f'{total:,}/{len(todo):,}  morti {sum(counts[c] for c in DEAD):,}  '
                      f'da riprovare {sum(counts[c] for c in TRANSIENT):,}  '
                      f'~{(len(todo) - total) / max(speed, 0.01) / 3600:.1f} h rimaste  '
                      f'{dict(counts.most_common(5))}', flush=True)

        next_slot = time.monotonic()
        for name in todo:
            if len(recent) == recent.maxlen and sum(recent) >= 0.8 * recent.maxlen:
                done, pending = wait(pending)
                collect(done)
                out.flush()
                ok = sum(counts.values()) - counts['rifiutato']
                if ok - ok_at_pause > recent.maxlen:
                    rate, stuck = max(rate / 2, 1), 0  # we were working: our pace triggered it
                else:
                    stuck += 1  # still blocked from before: just wait
                ok_at_pause = ok
                if stuck > MAX_PAUSES:
                    print('Vercel continua a rifiutare le connessioni: mi fermo qui.', flush=True)
                    break
                print(f'Vercel rifiuta le connessioni: pausa di {PAUSE_MINUTES} minuti, '
                      f'poi riparto a {rate:g} al secondo', flush=True)
                time.sleep(PAUSE_MINUTES * 60)
                recent.clear()
                next_slot = time.monotonic()

            delay = next_slot - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_slot = max(next_slot, time.monotonic() - 1) + 1 / rate
            while len(pending) >= WORKERS:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                collect(done)
            pending.add(pool.submit(check, name))
        done, _ = wait(pending)
        collect(done)

    blacklisted = write_blacklist(status)
    left = sum(1 for n in names if status.get(n) in TRANSIENT or n not in status)
    print(f'\nFatto: {dict(counts.most_common())}')
    print(f'blacklist.txt: {blacklisted:,} siti. Ancora da controllare o riprovare: {left:,}.')
    print('Ora rilancia: python -I scripts/build_data.py')


if __name__ == '__main__':
    main()
