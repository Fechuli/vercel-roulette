# Vercel Roulette

Apri a caso uno degli oltre 270.000 siti pubblicati su `*.vercel.app`, oppure cercali per nome.

## Da dove vengono i nomi

Un elenco completo dei sottodomini `vercel.app` non esiste. Vercel usa un solo certificato wildcard, quindi i log dei certificati (crt.sh) non mostrano i singoli nomi. Anche il DNS è wildcard: qualsiasi nome risolve, esistente o no.

I nomi vengono quindi dal [web graph di Common Crawl](https://commoncrawl.org/web-graphs), che elenca tutti gli host visitati o linkati durante i crawl. Lo script prende una release ogni circa 3 mesi, da maggio 2023 in poi, e ne ricava 378.000 sottodomini unici. Poi li divide in tre gruppi:

- **progetto**: nomi scelti da chi pubblica, come `portfolio-mario.vercel.app`. Vanno sul sito.
- **anteprima-deploy** e **anteprima-branch**: URL creati da Vercel a ogni deploy, come `progetto-g0n6h8dsd-team` o `progetto-git-main-team`. Restano solo negli export.
- **sospetto**: nomi da kit di phishing (`login`, `wallet`, `trezor`, `ledger`, `verify`…). Sono esclusi dal sito ma segnalati negli export. La regola è `SUSPECT` in [scripts/build_data.py](scripts/build_data.py). Qualche falso positivo è accettato.
- **morto**: siti che non si possono più visitare. [blacklist.txt](blacklist.txt) li elenca con il motivo: `404` (deploy cancellato o home mancante), `410`, `402` (disattivato), `451` (bloccato da Vercel), `vercel-login` (protetto) e `tls` (nome a due livelli, non coperto dal certificato).

## Struttura

```
public/                sito statico, servito da Vercel così com'è
  data/sites.json      elenco compatto letto dalla pagina (generato)
scripts/build_data.py  scarica, unisce, classifica e genera i dati
scripts/check_alive.py controlla quali siti rispondono e aggiorna blacklist.txt
blacklist.txt          siti morti, esclusi a ogni build
cache/                 host grezzi per release ed esiti dei controlli (fuori da git)
exports/               CSV completo e TXT dei progetti (fuori da git)
```

## Aggiornare i dati

Serve Python 3.9 o più recente, senza dipendenze.

```
python -I scripts/build_data.py
```

Lo script riusa le release già in `cache/`. Cerca da solo la release più recente uscita dopo l'ultima in `RELEASES` e la aggiunge. Una volta al trimestre conviene aggiungere quella nuova alla lista. Poi fai commit di `public/data/sites.json` e il push ridistribuisce il sito.

Con `--refresh` riscarica tutto: ci vuole circa un minuto.

## Togliere i siti morti

```
python -I scripts/check_alive.py   # una richiesta HEAD per sito, 5 al secondo
python -I scripts/build_data.py
```

Va piano di proposito. A 33 richieste al secondo la protezione anti-bot di Vercel blocca lo script dopo circa 1.500 siti. Il blocco riguarda il client Python, e il browser continua a funzionare. A 5 al secondo il primo giro su tutta la lista dura circa 15 ore, partendo dai siti visti più di recente. Se Vercel rifiuta comunque le connessioni, lo script si ferma 15 minuti e riparte. Se il blocco arriva dopo che aveva ripreso a lavorare, riparte anche a metà velocità.

Lo script si può interrompere e riprendere. Timeout, 429 ed errori del server non finiscono in blacklist: il giro successivo li riprova.

## In locale

La pagina carica i dati con `fetch`, quindi va servita via HTTP e non aperta come file:

```
python -m http.server 8000 -d public
```
