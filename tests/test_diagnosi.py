"""Test di `ecotokens diagnosi`.

Il comando esiste perche' quasi tutti i modi di configurare male questo
gateway **non danno errore**: si manifestano come un registro vuoto, una
memoria che non trova niente, una cache che non si forma. Un comando che
promette di scoprirli e ne lascia passare uno e' peggio di nessun comando,
perche' produce la convinzione che sia tutto a posto.

La proprieta' che conta piu' di tutte sta nel primo test: **non stampa mai il
valore di una credenziale.** Un output di diagnosi finisce incollato nelle
segnalazioni di errore, nei forum e nelle chat, ed e' esattamente il posto in
cui una chiave non deve trovarsi.
"""

from __future__ import annotations

import pytest

from ecotokens.config import Settings
from ecotokens.diagnosi import AVVISO, GRAVE, OK, esegui


# --- la proprieta' che viene prima di tutte --------------------------------


def test_non_stampa_mai_il_valore_di_una_credenziale(monkeypatch):
    segreto = "sk-ant-non-deve-comparire-da-nessuna-parte"
    monkeypatch.setenv("ANTHROPIC_API_KEY", segreto)

    settings = Settings()
    settings.upstream.api_key = segreto
    settings.server.api_key = "chiave-del-gateway-anche-questa-segreta"

    testo = " ".join(
        f"{e.nome} {e.dettaglio} {e.rimedio}" for e in esegui(settings).esiti
    )
    assert segreto not in testo
    assert "chiave-del-gateway" not in testo
    # Deve pero' dire **da dove** arriva, o non serve a niente.
    assert "configurazione" in testo.lower()


def test_una_chiave_nel_file_di_configurazione_e_un_problema_grave(monkeypatch):
    """E' il posto da cui tutto il progetto passa il tempo a tenerle fuori: un
    file di configurazione finisce in un repository, in un backup o in un
    allegato."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings()
    settings.upstream.api_key = "sk-ant-qualcosa"

    voce = _voce(esegui(settings), "Credenziali Anthropic")
    assert voce.stato == GRAVE
    assert "ANTHROPIC_API_KEY" in voce.rimedio


CHIAVE_FINTA = "sk-ant-api03-finta-ma-lunga-come-una-vera"


def test_la_chiave_nell_ambiente_va_bene_e_viene_riconosciuta(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", CHIAVE_FINTA)
    voce = _voce(esegui(Settings()), "Credenziali Anthropic")
    assert voce.stato == OK
    assert "ANTHROPIC_API_KEY" in voce.dettaglio


# --- la variabile c'e', il contenuto no ------------------------------------
#
# Non un caso di scuola: succede davvero. `Read-Host -AsSecureString` non
# accetta `Ctrl+V` in molte console, e l'utente preme Invio su un campo dove
# non e' entrato niente. La variabile viene creata lo stesso, e il controllo
# precedente - "la variabile esiste?" - diceva OK su un carattere solo.


@pytest.mark.parametrize(
    "valore, atteso",
    [
        ("v", "un carattere"),
        ("sk-ant", "6 caratteri"),
        (f'"{CHIAVE_FINTA}"', "virgolette"),
        (f"'{CHIAVE_FINTA}'", "virgolette"),
        (f"{CHIAVE_FINTA}\n", "spazi"),
        (f"  {CHIAVE_FINTA}", "spazi"),
    ],
)
def test_una_credenziale_di_forma_impossibile_e_grave(monkeypatch, valore, atteso):
    monkeypatch.setenv("ANTHROPIC_API_KEY", valore)
    voce = _voce(esegui(Settings()), "Credenziali Anthropic")
    assert voce.stato == GRAVE, voce.dettaglio
    assert atteso in voce.dettaglio
    assert voce.rimedio


def test_la_forma_sbagliata_non_fa_trapelare_il_valore(monkeypatch):
    """Il controllo nuovo e' codice nuovo che tocca una credenziale: la
    proprieta' del primo test deve reggere anche sul percorso di errore."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", f'"{CHIAVE_FINTA}"')
    testo = " ".join(
        f"{e.nome} {e.dettaglio} {e.rimedio}" for e in esegui(Settings()).esiti
    )
    assert CHIAVE_FINTA not in testo


def test_non_si_pretende_di_conoscere_il_formato(monkeypatch):
    """Il controllo distingue «non e' arrivato niente» da «non conosco questo
    formato», e solo la prima e' un problema. Convalidare davvero una chiave e'
    lavoro del server: da qui si puo' solo con `verifica --live`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "un-formato-che-non-conosciamo-ma-lungo")
    assert _voce(esegui(Settings()), "Credenziali Anthropic").stato == OK


# --- ogni esito deve poter essere usato -----------------------------------


def _voce(diagnosi, nome):
    return next(e for e in diagnosi.esiti if e.nome == nome)


def test_ogni_esito_che_non_va_dice_cosa_fare():
    """Un controllo che dice solo "non va" sposta il problema senza risolverlo:
    chi lo legge sa di avere un guaio e non sa da dove cominciare."""
    settings = Settings()
    settings.server.host = "0.0.0.0"
    settings.semantic_cache.enabled = True
    settings.storage.path = ":memory:"

    for voce in esegui(settings).esiti:
        if voce.stato != OK:
            assert len(voce.rimedio) > 30, voce.nome


def test_il_codice_di_uscita_distingue_i_tre_casi(monkeypatch):
    """Serve a metterlo davanti a `serve` in uno script di avvio: senza tre
    valori distinti non si puo' decidere se fermarsi o solo annotare."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", CHIAVE_FINTA)

    sano = Settings()
    sano.storage.path = "ecotokens.db"
    assert esegui(sano).codice_uscita in (0, 1)

    rotto = Settings()
    rotto.server.host = "0.0.0.0"  # esposto senza chiave del gateway
    assert esegui(rotto).codice_uscita == 2


# --- i singoli controlli ---------------------------------------------------


def test_esporsi_senza_chiave_e_grave_esporsi_con_chiave_e_solo_un_avviso():
    """La porta inoltra con la chiave Anthropic dell'utente: non e' un
    servizio che espone dei dati, e' uno che espone una carta di credito."""
    aperto = Settings()
    aperto.server.host = "0.0.0.0"
    assert _voce(esegui(aperto), "Esposizione").stato == GRAVE

    protetto = Settings()
    protetto.server.host = "0.0.0.0"
    protetto.server.api_key = "una-frase-lunga"
    voce = _voce(esegui(protetto), "Esposizione")
    assert voce.stato == AVVISO
    assert "TLS" in voce.rimedio


def test_la_cache_semantica_accesa_senza_la_sua_libreria_e_grave():
    """E' il guasto silenzioso tipico: accesa nella configurazione, ferma nei
    fatti, e nessun errore da nessuna parte."""
    pytest.importorskip
    settings = Settings()
    settings.semantic_cache.enabled = True
    voce = _voce(esegui(settings), "Cache semantica")
    try:
        import fastembed  # noqa: F401

        assert voce.stato == OK
    except ImportError:
        assert voce.stato == GRAVE
        assert "semantic" in voce.rimedio


def test_un_registro_in_memoria_e_un_avviso_non_un_errore():
    """Va benissimo per provare, e non va bene per misurare: la differenza
    merita un avviso, non un rifiuto."""
    settings = Settings()
    settings.storage.path = ":memory:"
    assert _voce(esegui(settings), "Registro").stato == AVVISO


def test_una_cartella_non_scrivibile_viene_vista(tmp_path, monkeypatch):
    settings = Settings()
    settings.storage.path = str(tmp_path / "sotto" / "eco.db")

    def vietato(*args, **kwargs):
        raise OSError("permesso negato")

    monkeypatch.setattr("pathlib.Path.mkdir", vietato)
    voce = _voce(esegui(settings), "Registro")
    assert voce.stato == GRAVE
    assert "non misura" in voce.rimedio


def test_gli_stadi_elencati_sono_quelli_veri():
    """Non l'intenzione della configurazione: cio' che verrebbe montato."""
    settings = Settings(profilo="prudente")
    settings.memory.enabled = False
    voce = _voce(esegui(settings), "Stadi attivi")
    assert "ledger" in voce.dettaglio
    assert "memory" in voce.dettaglio.split("spenti:")[-1]
