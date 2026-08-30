# Come si pubblica una versione

Una versione su PyPI **non si sostituisce**. Un numero già caricato resta lì per
sempre: si può solo abbandonarlo (`yank`) e pubblicarne un altro. È l'unica
operazione di questo progetto che non ha un annulla, e questa pagina esiste per
quello — non perché i passi siano difficili.

## Prima di cominciare

- [ ] `.venv/Scripts/python.exe -m pytest -q` — tutti verdi, e **mai rete**.
- [ ] Il `CHANGELOG.md` ha una sezione per questa versione, con la data.
- [ ] `version` in `pyproject.toml` combacia con quella sezione. È l'unica
      fonte: `ecotokens.__version__` la legge da lì, non c'è una seconda copia
      da tenere allineata.
- [ ] Se il CHANGELOG elenca **Rotture**, la minore sale — non la patch. Finché
      la maggiore è 0 una minore può contenerle, ed è scritto in cima al
      CHANGELOG; nasconderle sotto una patch no.
- [ ] **Dopo aver cambiato il numero, rifare `pip install -e .`.**
      `ecotokens.__version__` legge i metadati del pacchetto *installato* e solo
      in mancanza ripiega su `pyproject.toml`: finché non si reinstalla, il
      codice continua a dichiarare la versione precedente. Se ne accorge
      `test_la_versione_e_una_sola`, che per questo va eseguito **dopo** il
      cambio e non prima.

## Costruire e controllare

```bash
rm -rf dist build
.venv/Scripts/python.exe -m build
.venv/Scripts/python.exe -m twine check dist/*
```

`twine check` rifiuta un README che PyPI non saprebbe rendere. Vale la pena
guardarlo: è l'unico controllo che si può fare **prima** invece che dopo, e
dopo non serve più a niente.

## Provarlo come lo proverà chi lo installa

Il passo che si salta più volentieri, ed è quello che ha trovato più problemi.
Un pacchetto che importa nella cartella del progetto può non importare altrove:
un file dimenticato in `[tool.hatch.build.targets.wheel]`, una dipendenza usata
e non dichiarata, un modulo nuovo che nessuno ha incluso.

```bash
python -m venv /tmp/prova && /tmp/prova/Scripts/python.exe -m pip install dist/ecotokens-*.whl
```

Poi, dall'ambiente pulito: `ecotokens diagnosi`, `ecotokens merito`, e
`ecotokens serve` con una richiesta a `/health`. Non `--help`: quello prova che
la firma è valida, non che il corpo giri.

## Pubblicare

**Prima su TestPyPI**, che è il posto dove sbagliare costa zero:

```bash
.venv/Scripts/python.exe -m twine upload --repository testpypi dist/*
```

Poi, solo se l'installazione da TestPyPI funziona:

```bash
.venv/Scripts/python.exe -m twine upload dist/*
```

## Il token, e dove non va messo

L'utente è la stringa letterale `__token__`; la password è un token API di PyPI
(comincia per `pypi-`), non la password dell'account.

Non va in `pyproject.toml`, non va in un file del repository, non va in un
comando che finisce nella cronologia della shell. Le due strade pulite sono
`~/.pypirc` con i permessi ristretti, oppure la variabile `TWINE_PASSWORD`
impostata nella sessione — la stessa regola della chiave Anthropic, per la
stessa ragione: un segreto in un file di configurazione è un segreto che prima
o poi finisce in un backup o in un allegato.

Conviene creare un token **limitato a questo progetto** invece che valido per
tutto l'account. Il primo caricamento non può esserlo (il progetto non esiste
ancora): si usa un token d'account, e subito dopo si sostituisce con uno
ristretto.

## Dopo

```bash
git tag -a v0.3.0 -m "0.3.0"
```

E si verifica che `pip install ecotokens` funzioni da un ambiente pulito, su
una macchina che non è questa se possibile.
