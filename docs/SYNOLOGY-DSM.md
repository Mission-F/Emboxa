# EMBOXA Web su Synology DSM (Container Manager)

Questa guida installa EMBOXA su un NAS Synology copiando **una sola cartella** dal PC e avviandola
da Container Manager. Nessun comando da terminale, nessun SSH.

Il principio: la cartella del progetto contiene sia l'applicazione sia i suoi dati (`data/`).
Copiare la cartella significa portarsi dietro database, archivi email, chiavi di cifratura,
utenti e impostazioni. Spostare l'app in futuro vuol dire copiare di nuovo quella cartella.

**Ti serve:** DSM 7.2 o successivo con il pacchetto **Container Manager** installato dal Centro
pacchetti, e il NAS collegato a internet (serve solo la prima volta, per compilare l'immagine).

---

## 1. Ferma l'app di origine prima di copiare i dati

Salta questo passo solo se stai partendo da zero, senza dati da portare.

Se stai migrando da un'installazione esistente (TrueNAS, un altro Docker, un altro NAS), **ferma
il container prima di copiare la cartella `data/`**.

Non è una formalità. Il database è SQLite e mentre l'app gira tiene un file di journal accanto a
sé (`emboxa-web.db-wal`, che può pesare più di un giga). Copiare quei file mentre vengono scritti
produce una copia incoerente: l'app poi riparte, ma possono mancare gli ultimi backup, o il
database può risultare danneggiato. A container fermo, invece, la copia è pulita.

Quando copi la cartella `data/db/`, portati **tutti e tre** i file se ci sono:

```text
emboxa-web.db
emboxa-web.db-wal
emboxa-web.db-shm
```

---

## 2. Copia la cartella sul NAS

La destinazione consigliata è la cartella condivisa `docker`, che Container Manager crea da solo:

```text
/volume1/docker/emboxa-web
```

Due modi, scegli quello che preferisci:

**Da Finder (consigliato se porti anche gli archivi).** Nel Finder premi `⌘K`, collegati a
`smb://INDIRIZZO-DEL-NAS`, apri la cartella condivisa `docker` e trascinaci dentro l'intera
cartella del progetto. Per diversi giga di archivi è la strada più veloce e più affidabile.

**Da File Station.** Apri File Station nel browser, entra in `docker` e trascina la cartella dal
PC nella finestra. Comodo per la sola applicazione; su cartelle molto grandi e con molti file
l'upload da browser è lento e può interrompersi.

Alla fine sul NAS devi vedere almeno questo:

```text
/volume1/docker/emboxa-web/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── app/
├── docker/
└── data/          ← i tuoi dati, se li stai migrando
```

Se `data/` non c'è, l'app parte comunque e crea un'installazione vuota.

---

## 3. Crea il progetto in Container Manager

1. Apri **Container Manager → Progetto → Crea**.
2. **Nome progetto:** `emboxa-web` (solo minuscole, niente spazi: DSM non accetta altro).
3. **Percorso:** premi *Seleziona* e scegli la cartella che hai appena copiato,
   `/volume1/docker/emboxa-web`.
4. DSM trova da solo il `docker-compose.yml` e propone di usarlo: conferma.
5. **Avanti** fino in fondo e **Fatto**.

Container Manager compila l'immagine e avvia il container. La prima compilazione scarica Python e
le dipendenze: **da 3 a 10 minuti** a seconda del NAS. Le volte successive è quasi immediata.

Non devi modificare il `docker-compose.yml`: ogni valore ha già un default che funziona.

---

## 4. Apri l'app

```text
http://INDIRIZZO-DEL-NAS:49273
```

Se stai migrando, accedi con **gli utenti che avevi già**: sono nel database che hai copiato.
Anche le impostazioni SMTP e Telegram configurate da `/admin` sono nel database e arrivano con lui.

Se invece è un'installazione nuova e vuota, ti serve il primo amministratore: vedi
[Primo amministratore](#primo-amministratore) qui sotto.

---

## 5. Sistema l'indirizzo pubblico

Questo passo non serve per entrare, ma serve perché **i link nelle email funzionino**.

L'app inserisce `PUBLIC_APP_URL` nelle email di verifica e di recupero password, e lo usa per il
redirect di Microsoft OAuth. Il default è `http://localhost:49273`, che vale solo per chi sta
davanti al NAS. Per correggerlo crea un file `.env` nella cartella del progetto (puoi copiare
`.env.example` e rinominarlo da File Station):

```dotenv
PUBLIC_APP_URL=http://192.168.1.50:49273
```

Poi in Container Manager: **Progetto → emboxa-web → Azione → Ricostruisci**.

### Se pubblichi l'app in HTTPS

Con il reverse proxy di DSM (**Pannello di controllo → Portale di accesso → Reverse Proxy**),
imposta nel `.env`:

```dotenv
PUBLIC_APP_URL=https://emboxa.tuodominio.it
COOKIE_SECURE=true
```

`COOKIE_SECURE=true` va messo **solo** con HTTPS reale. Se lo attivi su un semplice `http://`, il
browser scarta il cookie di sessione e il login sembra non funzionare senza dare errori chiari.

---

## Primo amministratore

Solo per installazioni nuove, senza un `data/` esistente. Nel `.env`:

```dotenv
ADMIN_EMAIL=tuo@indirizzo.it
ADMIN_PASSWORD=una-password-lunga-e-unica
```

Ricostruisci il progetto, entra, poi **svuota quei due valori e ricostruisci di nuovo**: l'utente
resta nel database e la password smette di stare in chiaro in un file sul NAS.

---

## Aggiornare l'app

1. Copia sul NAS i file aggiornati dell'applicazione (`app/`, `Dockerfile`, `requirements.txt`,
   `docker/`, `docker-compose.yml`), **senza toccare `data/`**.
2. Container Manager → **Progetto → emboxa-web → Azione → Ricostruisci**.

I dati non vengono toccati: stanno in `data/`, che il container monta e non ricrea mai.

---

## Backup

Tutto ciò che conta sta in `data/`. Per un backup coerente:

1. Container Manager → **Progetto → emboxa-web → Azione → Arresta**.
2. Copia la cartella `data/` dove preferisci (o includila in Hyper Backup).
3. Riavvia il progetto.

`data/secrets/fernet.key` è la chiave con cui sono cifrate le credenziali IMAP salvate:
**se la perdi, quelle credenziali non sono più recuperabili**, nemmeno con il database intatto.

---

## Problemi comuni

**Il container riparte in continuazione, nei log compare `Operation not permitted` su `/data`.**
L'app adotta da sola il proprietario della cartella `data/`, quindi di norma non succede. Se
succede (per esempio con una cartella arrivata da un backup con permessi anomali), forza l'utente
nel `.env` e ricostruisci:

```dotenv
PUID=1026
PGID=100
```

`1026` è il tipico uid del primo utente DSM; lo verifichi con **Pannello di controllo → Utenti**.

**La porta 49273 è occupata.** Cambia porta nel `.env` e ricostruisci:

```dotenv
WEB_PORT=8973
```

Ricordati di aggiornare anche `PUBLIC_APP_URL`.

**Non riesco a raggiungere l'app dalla LAN.** Se il firewall di DSM è attivo, apri la porta in
**Pannello di controllo → Sicurezza → Firewall**.

**La compilazione fallisce.** Serve internet: il NAS deve scaricare l'immagine Python e le
dipendenze. Controlla la connessione e riprova da **Azione → Ricostruisci**.

**L'app parte ma è vuota, i miei utenti non ci sono.** La cartella `data/` non è arrivata, oppure
è arrivata in una posizione sbagliata. Deve stare **dentro** la cartella del progetto, di fianco al
`docker-compose.yml`, e contenere `db/emboxa-web.db`. Verifica da File Station e ricostruisci.

**Voglio vedere cosa sta succedendo.** Container Manager → **Progetto → emboxa-web → Log**. All'
avvio l'app scrive una riga che riassume cosa ha trovato:

```text
EMBOXA: data=/data user=1026:100 database=emboxa-web.db
```
