# EcoTokens in un contenitore.
#
# NON VERIFICATO: la macchina su cui questo file e' stato scritto non ha
# Docker, quindi non e' mai stato costruito ne' eseguito. Il progetto non
# dichiara come funzionante cio' che non ha misurato, e questo vale anche per
# un Dockerfile. I passi sono gli stessi dell'installazione da sorgente, che
# invece e' verificata (ruota costruita e installata in un ambiente pulito),
# ma la traduzione in immagine e' da provare:
#
#     docker build -t ecotokens .
#     docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... ecotokens
#
# Due scelte che val la pena spiegare, perche' non sono ovvie.

FROM python:3.13-slim AS build

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY ecotokens ./ecotokens

# La ruota si costruisce qui e si installa nell'immagine finale: cosi' hatchling
# e i suoi resti non finiscono nell'immagine che gira.
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist


FROM python:3.13-slim

# Utente non privilegiato. Il gateway inoltra all'API con la chiave
# dell'utente: se qualcuno riesce a farlo uscire dal processo, e' meglio che si
# trovi in un contenitore senza root.
RUN useradd --create-home --uid 10001 ecotokens

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Il database sta in un volume dichiarato, non nella cartella di lavoro. Fuori
# dal contenitore il percorso predefinito e' relativo, quindi il file nasce
# dove si e' lanciato il comando: dentro un servizio la cartella di lavoro e'
# arbitraria, e i consumi finirebbero ogni volta in un posto diverso - cioe'
# la pagina dei numeri sarebbe vuota senza che si capisca perche'.
ENV ECOTOKENS_STORAGE__PATH=/dati/ecotokens.db
RUN mkdir -p /dati && chown ecotokens:ecotokens /dati
VOLUME ["/dati"]

USER ecotokens
EXPOSE 8000

# 0.0.0.0 dentro il contenitore e' obbligato - l'alternativa e' che nessuno lo
# raggiunga - e il gateway si rifiuta di partire cosi' senza una chiave sua.
# Non e' un fastidio da aggirare: chi pubblica la porta la sta rendendo
# raggiungibile davvero, ed e' esattamente il caso in cui quel controllo serve.
ENV ECOTOKENS_SERVER__HOST=0.0.0.0

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

ENTRYPOINT ["ecotokens"]
CMD ["serve"]
