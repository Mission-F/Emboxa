# Pubblicare Emboxa Web su GitHub

Questa guida riguarda il codice sorgente. Database, archivi email, chiavi e configurazioni reali non devono mai entrare nel repository.

## Cosa caricare

- `.github/` — workflow per costruire l'immagine GHCR;
- `app/` — backend, template e asset statici;
- `deploy/truenas-web.example.yaml` e gli altri esempi non sensibili;
- `docker/`, `docs/`, `scripts/` e `tests/`;
- `.dockerignore`, `.env.example`, `.gitignore`;
- `Dockerfile`, `docker-compose.yml`;
- `README.md`, `SECURITY.md`, `LICENSE.md`;
- `requirements.txt`, `requirements-dev.txt`, `pytest.ini`.

Il file locale `deploy/truenas-web.yaml` non viene caricato: contiene il percorso specifico della NAS. Su GitHub va l'esempio `deploy/truenas-web.example.yaml`.

## Cosa non caricare mai

- `data/`, inclusi `db/`, `archives/`, `exports/`, `imports/` e `secrets/`;
- `.env` o YAML con token, password, email amministratore e chiavi reali;
- `*.db`, `*.sqlite*`, `*.key`, `*.eml`, `*.mailvault`;
- backup di caselle, allegati, log applicativi o export;
- `.venv/`, `__pycache__/`, `.pytest_cache/`, `.DS_Store`;
- token BotFather, chiavi Cloudflare, password IMAP/SMTP o credenziali GitHub.

## Controllo prima del primo push

Esegui dalla cartella `Emboxa-Web`:

```bash
sh scripts/release-check.sh
python -m compileall -q app tests
python -m pytest -q
git status --short --ignored
```

Nell'ultima lista, `data/`, `.env` e `deploy/truenas-web.yaml` devono comparire come ignorati e non tra i file da aggiungere.

## Primo caricamento

Crea su GitHub un repository vuoto, senza README o `.gitignore` generati dal sito. Poi:

```bash
git init
git branch -M main
git add .
git status --short
git commit -m "Initial Emboxa Web release"
git remote add origin https://github.com/Mission-F/Emboxa.git
git push -u origin main
```

Prima di `git commit`, controlla ancora che non appaiano `data/`, `.env`, `deploy/truenas-web.yaml`, database, chiavi o archivi email. Non usare `git add -f` sui file ignorati.

## Immagine container

Il workflow `.github/workflows/container.yml` pubblica automaticamente l'immagine multi-architettura in GitHub Container Registry dopo un push su `main` o un tag `v*`. Il nome sarà:

```text
ghcr.io/mission-f/emboxa-web:latest
```

Se il pacchetto deve essere pubblico, rendilo pubblico nelle impostazioni Packages del repository dopo la prima build.
