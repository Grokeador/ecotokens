"""Configurazione: file TOML + variabili d'ambiente.

Ogni stadio della pipeline si accende e si spegne da qui. I default sono
scelti per essere sicuri: le ottimizzazioni che possono cambiare il contenuto
di una risposta (cache semantica, cambio di modello) sono disattivate.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .pricing import DEFAULT_MODEL

CONFIG_ENV_VAR = "ECOTOKENS_CONFIG"
DEFAULT_CONFIG_NAMES = ("ecotokens.toml",)


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    # Se valorizzata, i client devono presentarla in Authorization: Bearer.
    # Non e' la chiave Anthropic: e' la chiave del gateway.
    api_key: str | None = None
    log_level: str = "info"


class UpstreamSettings(BaseModel):
    """Come il gateway parla con l'API Anthropic."""

    # Lasciare vuoto: l'SDK risolve da solo ANTHROPIC_API_KEY,
    # ANTHROPIC_AUTH_TOKEN o un profilo creato con `ant auth login`.
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = 600.0
    max_retries: int = 2
    default_model: str = DEFAULT_MODEL
    # Applicati quando il client non li specifica.
    default_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    default_max_tokens: int = 16_000
    default_max_tokens_stream: int = 64_000
    adaptive_thinking: bool = True
    thinking_display: Literal["omitted", "summarized"] = "omitted"


class SessionSettings(BaseModel):
    enabled: bool = True
    # Quanti messaggi iniziali normalizzati entrano nel fingerprint.
    fingerprint_depth: int = 3
    # Dopo quanto una sessione inattiva non viene piu' riconosciuta.
    ttl_hours: int = 72


class CachePlannerSettings(BaseModel):
    """Piazzamento automatico dei breakpoint cache_control."""

    enabled: bool = True
    # L'API ne accetta al massimo 4 per richiesta.
    max_breakpoints: int = 4
    # La finestra di lookback e' di 20 blocchi: oltre quella distanza il
    # breakpoint non trova la voce precedente e la cache manca in silenzio.
    intermediate_every_blocks: int = 15
    # Saltare il primo turno sembra prudente e invece costa: il pareggio e' a 2
    # richieste, e il prefisso piu' grosso - prompt di sistema e definizioni dei
    # tool - e' condiviso fra richieste *diverse*, non solo fra i turni della
    # stessa conversazione. La scrittura del primo turno viene quindi riletta
    # dalla richiesta successiva di chiunque abbia lo stesso system prompt.
    # Misurato con `ecotokens optimize`: attivarlo costa il 6% in piu' sul mix
    # standard, e fino al 155% su venti richieste isolate che condividono il
    # system prompt. Da accendere solo se ogni richiesta ha un prefisso unico.
    skip_first_turn: bool = False
    # TTL 1h solo per sessioni lunghe con pause: la scrittura costa 2x e
    # servono almeno 3 richieste per rientrare.
    long_ttl_min_turns: int = 3
    long_ttl_min_gap_seconds: int = 300


class ContextSettings(BaseModel):
    """Gestione della finestra di contesto."""

    enabled: bool = True
    # Rimozione dei vecchi tool result / blocchi di pensiero (beta API).
    # Attenzione: **non e' un'ottimizzazione di costo, e' una difesa contro
    # l'overflow di contesto.** Misurato con `ecotokens ablate`: potare sposta
    # il confine di taglio a ogni turno, quindi cambia il prefisso e distrugge
    # il prompt caching. Sul carico di costruzione di questo progetto la quota
    # di prompt servita da cache crolla dall'89% al 27% e il costo sale del 35%;
    # su un ciclo agentico con risultati molto grossi invece conviene (+10%).
    # Per questo la soglia resta alta: si pota solo quando l'alternativa e'
    # sforare la finestra.
    clear_tool_uses: bool = True
    clear_thinking: bool = False
    # Frazione della finestra del modello oltre la quale si pota.
    trigger_ratio: float = 0.6
    # Oltre questa frazione la potatura non basta e si riassume la parte
    # vecchia della conversazione.
    hard_ratio: float = 0.85
    # Modello usato per riassumere. Il riassunto viene calcolato una volta e
    # poi riusato alla lettera: se cambiasse a ogni turno cambierebbe il
    # prefisso del prompt e la cache mancherebbe sempre.
    summary_model: str = "claude-haiku-4-5"
    local_compaction: bool = True
    # Messaggi recenti che restano sempre integrali, mai riassunti.
    #
    # Attenzione a "ottimizzarlo": misurando, il costo scende in modo monotono
    # man mano che si abbassa (su 50 turni: 4 messaggi costano $1,86, 24 ne
    # costano $3,39). Non e' una scoperta, e' una tautologia - comprimere di
    # piu' costa sempre meno - e il banco non ha nulla da dire sulla qualita'
    # della risposta, che e' esattamente cio' che si perde. Il valore resta 8
    # per fedelta', non per costo: e' un giudizio, non un ottimo misurato.
    # Chi lo abbassa scambia soldi contro dettaglio, e ora sa di farlo.
    keep_recent_messages: int = 8
    # Il punto di taglio avanza a scatti di questa ampiezza invece di seguire
    # la coda della conversazione. E' la regola che rende la compattazione
    # compatibile con la cache: un taglio che si muove a ogni turno produce un
    # riassunto nuovo a ogni turno, quindi un prefisso nuovo a ogni turno.
    # Misurato con `ecotokens compaction`: a inseguimento la compattazione
    # costa il 40% PIU' del non comprimere affatto; a scatti fa risparmiare.
    # Il valore e' il minimo di una curva a U: scatti stretti riassumono di
    # continuo e sprecano cache, scatti larghi tengono troppa cronologia
    # integrale. Provati 2/4/8/12/16/24/32 su conversazioni da 20, 40 e 60
    # turni: 12 e' il migliore o a un soffio dal migliore su tutte e tre.
    recompute_every_messages: int = 12
    # Tetto rigido sul riassunto: questi token si pagano una volta in output e
    # poi a ogni turno in input. Non e' un risparmio misurato - sui carichi di
    # prova il riassunto sta ampiamente sotto il tetto e il limite non morde
    # mai - ma un paracadute contro il riassunto che parte per la tangente.
    summary_max_tokens: int = 600
    # Il riassunto nuovo parte da quello vecchio e legge solo i messaggi
    # aggiunti nel frattempo, invece di rileggere tutta la cronologia.
    incremental_summary: bool = True
    # Ogni blocco della trascrizione data al riassuntore viene troncato a
    # questa lunghezza: per registrare "ha letto il file X" non serve il file.
    # Misurato su 200/400/800/1600/3200: il costo totale cambia pochissimo
    # (0,5% fra gli estremi) ma la spesa del riassuntore si dimezza scendendo.
    # 400 e' il compromesso: -32% di spesa ausiliaria contro 800, e ancora
    # abbastanza testo perche' il riassunto abbia di che lavorare.
    transcript_block_chars: int = 400
    # Sotto questo guadagno lordo la compattazione non si fa: il riassunto
    # costa una chiamata, e comprimere poco non la ripaga.
    min_gain_tokens: int = 2_000


class PromptSettings(BaseModel):
    """Riscrittura del prompt in ingresso.

    L'ordine dei campi e' l'ordine del rischio. Il primo non cambia una parola,
    l'ultimo le cambia tutte.
    """

    enabled: bool = True
    # Senza perdita: spazi ripetuti, righe vuote in eccesso, caratteri
    # invisibili, virgolette tipografiche. Non tocca il testo, solo la sua
    # punteggiatura invisibile. I blocchi di codice recintati restano intatti.
    normalize: bool = True
    # Toglie le formule che introducono un'istruzione senza aggiungerle nulla
    # ("e' importante notare che", "please note that"). Cambia il testo, quindi
    # non e' senza perdita, ma non cambia cosa viene chiesto.
    strip_filler: bool = False
    # Sinonimi piu' corti. Spenta di default per un motivo di metodo: piu' corto
    # in caratteri non vuol dire piu' corto in token, e l'unica autorita' sui
    # token e' `messages.count_tokens`. Vedi `only_verified`.
    substitute: bool = False
    # Applica solo le sostituzioni che il conteggio vero ha confermato. Si
    # popola con `ecotokens substitutions --live`. Spegnerlo significa fidarsi
    # di un'intuizione sul tokenizer, che e' esattamente cio' che questo
    # progetto evita altrove.
    only_verified: bool = True
    # Quali parti del prompt riscrivere. Mai i messaggi assistant (sono parole
    # che il modello ha detto davvero) ne' i tool result (sono dati esterni:
    # riscriverli e' falsificare un'osservazione).
    targets: list[str] = Field(default_factory=lambda: ["system", "user"])
    # Sotto questa lunghezza non si tocca niente: su un testo corto il
    # guadagno e' rumore e il rischio resta intero.
    min_chars: int = 200


class MemorySettings(BaseModel):
    enabled: bool = False
    # Modello economico usato per estrarre i fatti da ricordare.
    extraction_model: str = "claude-haiku-4-5"
    max_facts_injected: int = 8
    max_fact_chars: int = 400


class ExactCacheSettings(BaseModel):
    enabled: bool = True
    ttl_seconds: int = 86_400
    # La chiave si calcola sul testo normalizzato: due richieste che differiscono
    # per uno spazio doppio o una riga vuota sono la stessa domanda, e tenerle su
    # voci diverse significa pagare due volte la stessa risposta. E' l'unica
    # ottimizzazione che vale il prezzo pieno - un hit non sconta la richiesta,
    # la elimina - quindi qui conviene una normalizzazione che altrove sarebbe
    # eccessiva.
    normalize_key: bool = True
    # Non servire da cache le richieste con tool: il risultato dipende da uno
    # stato esterno che la cache non conosce.
    skip_when_tools: bool = True
    max_entries: int = 5_000


class SemanticCacheSettings(BaseModel):
    """Disattivata di default, e per una ragione.

    Servire una risposta "abbastanza simile" e' un rischio di correttezza, non
    un'ottimizzazione neutra: due domande vicine nello spazio degli embedding
    possono avere risposte giuste diverse.
    """

    enabled: bool = False
    model_name: str = "BAAI/bge-small-en-v1.5"
    similarity_threshold: float = 0.97
    ttl_seconds: int = 3_600
    max_candidates: int = 200


class RouterSettings(BaseModel):
    enabled: bool = True
    # Livello 1: abbassa l'effort sulle richieste semplici. Non invalida la
    # cache e non cambia modello: e' il risparmio sicuro.
    effort_downshift: bool = True
    simple_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # Cosa fare quando la domanda e' semplice ma il modello potrebbe dover
    # scegliere un tool. Prima qui c'era un rifiuto in blocco, e costava caro:
    # misurato, i turni con tool sono il 45% del traffico e comprendono il
    # carico di costruzione, che da solo vale il 61% della spesa.
    #
    # Togliere del tutto il veto varrebbe l'11,4% del costo totale. Non e' il
    # valore predefinito, e la ragione va detta: il banco modella la
    # *lunghezza* della risposta in funzione dell'effort, non la sua
    # *qualita'*. Un effort basso su un turno agentico puo' produrre la
    # chiamata sbagliata, e un tentativo in piu' costa piu' di quanto l'effort
    # abbia risparmiato. Quel rischio qui non e' misurabile.
    #
    # "medium" prende la parte sicura del premio (2,6% misurato) lasciando al
    # modello un margine di ragionamento. "low" prende tutto l'11,4% e scommette
    # sulla qualita'. "off" ripristina il rifiuto in blocco.
    effort_with_tools: Literal["off", "low", "medium", "high"] = "medium"
    # Si misura la domanda, non l'intero prompt. La difficolta' sta in cio' che
    # viene chiesto: un system prompt da 5000 token non rende difficile un
    # "che ore sono". Misurare il prompt intero significava non abbassare mai
    # l'effort, perche' qualsiasi system prompt reale supera la soglia.
    simple_max_question_tokens: int = 120
    # Livello 2: cambio di modello. Le cache sono legate al modello, quindi
    # cambiarlo a meta' conversazione azzera il prompt caching.
    model_downgrade: bool = False
    # Se attivo, il modello si sceglie una volta per sessione e non cambia.
    model_locked_per_session: bool = True
    downgrade_target: str = "claude-haiku-4-5"


class BudgetSettings(BaseModel):
    enabled: bool = False
    daily_usd: float = 5.0
    monthly_usd: float = 100.0
    # Preventivo esatto con count_tokens prima di inviare. Costa una chiamata
    # in piu' (non fatturata) ma evita sforamenti.
    precount: bool = False


class StorageSettings(BaseModel):
    path: str = "ecotokens.db"
    # Conserva il testo dei messaggi. Disattivare per non tenere a disco il
    # contenuto delle conversazioni: le statistiche continuano a funzionare.
    store_message_content: bool = True


class Settings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    upstream: UpstreamSettings = Field(default_factory=UpstreamSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    cache_planner: CachePlannerSettings = Field(default_factory=CachePlannerSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    prompt: PromptSettings = Field(default_factory=PromptSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    exact_cache: ExactCacheSettings = Field(default_factory=ExactCacheSettings)
    semantic_cache: SemanticCacheSettings = Field(default_factory=SemanticCacheSettings)
    router: RouterSettings = Field(default_factory=RouterSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)


def _find_config(explicit: str | Path | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    for name in DEFAULT_CONFIG_NAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate
    return None


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Applica le variabili ECOTOKENS_<SEZIONE>__<CAMPO>."""
    for raw_key, value in os.environ.items():
        if not raw_key.startswith("ECOTOKENS_") or "__" not in raw_key:
            continue
        remainder = raw_key[len("ECOTOKENS_") :]
        section, _, field = remainder.partition("__")
        section, field = section.lower(), field.lower()
        if section not in Settings.model_fields:
            continue
        data.setdefault(section, {})[field] = value
    return data


def load_settings(path: str | Path | None = None) -> Settings:
    """Carica la configurazione da TOML, poi applica gli override d'ambiente."""
    data: dict[str, Any] = {}
    config_path = _find_config(path)
    if config_path is not None:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    data = _apply_env_overrides(data)
    return Settings.model_validate(data)
