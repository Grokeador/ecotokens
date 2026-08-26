"""Carichi di prova per il banco di misura.

Uno scenario e' una sequenza di richieste in formato OpenAI, cosi' come le
manderebbe un client reale: ogni turno rispedisce l'intera cronologia, perche'
e' esattamente quello che fanno i client OpenAI ed e' la ragione per cui il
prompt caching conta tanto.

Lo scenario ``costruzione`` non e' inventato: legge i file veri di questo
repository e ricostruisce il traffico che un agente di codice produce mentre lo
scrive. E' la misura di quanto EcoTokens avrebbe fatto risparmiare sulla
costruzione di EcoTokens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Prompt di sistema tipico di un agente di codice: lungo, stabile, ripetuto
# identico a ogni turno. E' il candidato ideale al prompt caching.
CODING_SYSTEM = """Sei un assistente di programmazione che lavora su un progetto Python.

Regole di lavoro:
- Leggi i file prima di modificarli, e non inventare percorsi o funzioni.
- Le modifiche devono rispettare lo stile del codice circostante: densita' dei
  commenti, convenzioni di nome, idiomi gia' presenti nel progetto.
- Preferisci riusare le funzioni esistenti invece di scriverne di nuove.
- Quando esegui un comando, spiega in una riga cosa fa e perche' serve.
- Non introdurre dipendenze nuove senza dichiararlo esplicitamente.
- I test devono passare prima di considerare finito un lavoro.
- Se un requisito e' ambiguo, scegli l'interpretazione piu' probabile e
  dichiara l'assunzione, invece di fermarti a chiedere.
- Riporta gli esiti in modo fedele: se qualcosa fallisce, dillo con l'output.

Ambiente:
- Sistema operativo Windows, shell PowerShell e Git Bash disponibili.
- Python 3.13 in un ambiente virtuale nella cartella .venv del progetto.
- Il progetto usa FastAPI, pydantic, SQLite e l'SDK ufficiale anthropic.
- I test girano con pytest e non devono mai richiedere rete.

Formato delle risposte:
- Vai al punto, senza preamboli e senza riepiloghi di cio' che stai per fare.
- Cita i file come percorso relativo, con eventuale numero di riga.
- Il codice va in blocchi con il linguaggio dichiarato.
"""

# Definizioni di tool tipiche di un agente di codice. Renderizzano in posizione
# zero del prompt, prima di system e messages: sono la parte piu' stabile in
# assoluto, e anche quella che un ordinamento instabile rovinerebbe.
CODING_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Legge un file dal disco e ne restituisce il contenuto con i numeri di riga.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Percorso assoluto del file"},
                    "offset": {"type": "integer", "description": "Riga di partenza"},
                    "limit": {"type": "integer", "description": "Numero di righe da leggere"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Scrive un file sul disco, sovrascrivendolo se esiste gia'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Sostituisce una stringa esatta dentro un file esistente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Esegue un comando di shell e restituisce output ed exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_ms": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Cerca un'espressione regolare nei file del progetto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string"},
                    "output_mode": {"type": "string", "enum": ["content", "files", "count"]},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Elenca i file che corrispondono a un pattern glob.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Esegue la suite di test e restituisce il riepilogo.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "verbose": {"type": "boolean"}},
                "required": [],
            },
        },
    },
]


@dataclass
class Scenario:
    """Una sequenza di richieste da inviare al gateway, in ordine."""

    name: str
    description: str
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.requests)


def _request(
    messages: list[dict[str, Any]],
    *,
    model: str = "claude-opus-5",
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        payload["tools"] = tools
    return payload


def scenario_chat(system_words: int = 900, turns: int = 8) -> Scenario:
    """Conversazione lunga con un prompt di sistema grande e stabile.

    E' il caso piu' comune e anche il piu' favorevole al prompt caching: il
    prefisso cresce a ogni turno e viene rispedito identico.
    """
    system = {"role": "system", "content": "Istruzione operativa dettagliata. " * system_words}
    domande = [
        "Spiegami come funziona la gestione della memoria in Python.",
        "E il garbage collector come interviene sui cicli?",
        "Quanto costa in pratica un riferimento circolare?",
        "Come lo diagnostico su un processo in produzione?",
        "Quali strumenti mi consigli per profilarlo?",
        "E se il problema fosse invece la frammentazione?",
        "Mi fai un esempio concreto di codice problematico?",
        "Come lo riscriveresti per evitarlo?",
    ][:turns]

    scenario = Scenario(
        name="chat",
        description=f"Conversazione di {turns} turni con system prompt grande e stabile",
    )
    storia: list[dict[str, Any]] = [system]
    for domanda in domande:
        storia = storia + [{"role": "user", "content": domanda}]
        scenario.requests.append(_request(list(storia)))
        storia = storia + [{"role": "assistant", "content": "Risposta di prova."}]
    return scenario


def scenario_agente(turns: int = 6, tool_per_turno: int = 6) -> Scenario:
    """Ciclo agentico con molte chiamate di tool in parallelo.

    In questo scenario i risultati dei tool diventano rapidamente la voce di
    spesa piu' grossa del prompt: e' il caso in cui la potatura del contesto e
    i breakpoint intermedi hanno senso.
    """
    scenario = Scenario(
        name="agente",
        description=f"Ciclo agentico: {turns} turni da {tool_per_turno} tool ciascuno",
    )
    storia: list[dict[str, Any]] = [
        {"role": "system", "content": CODING_SYSTEM},
        {"role": "user", "content": "Analizza il progetto e correggi i test che falliscono."},
    ]
    scenario.requests.append(_request(list(storia), tools=CODING_TOOLS))

    for turno in range(turns):
        chiamate = [
            {
                "id": f"call_{turno}_{indice}",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": f"src/modulo_{turno}_{indice}.py"}),
                },
            }
            for indice in range(tool_per_turno)
        ]
        storia.append({"role": "assistant", "tool_calls": chiamate})
        for indice in range(tool_per_turno):
            storia.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{turno}_{indice}",
                    # Un file letto: il tipo di contenuto che gonfia il prompt.
                    "content": f"riga di codice numero {indice} " * 120,
                }
            )
        storia.append({"role": "user", "content": "Continua."})
        scenario.requests.append(_request(list(storia), tools=CODING_TOOLS))
    return scenario


def scenario_ripetitivo(uniche: int = 4, ripetizioni: int = 3) -> Scenario:
    """Domande frequenti: poche richieste diverse, ripetute molte volte.

    Il caso in cui la cache esatta lavora al massimo: e' tipico di un'app che
    espone un assistente a molti utenti che chiedono le stesse cose.
    """
    scenario = Scenario(
        name="ripetitivo",
        description=f"{uniche} domande distinte ripetute {ripetizioni} volte",
    )
    system = {"role": "system", "content": "Sei l'assistente di supporto del prodotto. " * 300}
    domande = [
        "Come reimposto la password?",
        "Quali metodi di pagamento accettate?",
        "Come disdico l'abbonamento?",
        "Entro quanto tempo posso chiedere un rimborso?",
    ][:uniche]

    for _ in range(ripetizioni):
        for domanda in domande:
            scenario.requests.append(
                _request([system, {"role": "user", "content": domanda}])
            )
    return scenario


def scenario_costruzione(project_root: Path, max_files: int = 14) -> Scenario:
    """Il carico reale: costruire EcoTokens.

    Ricostruisce il traffico che un agente di codice produce scrivendo questo
    progetto, usando i file veri come contesto. Ogni turno rispedisce la
    cronologia e il contenuto dei file gia' letti, che e' esattamente il motivo
    per cui una sessione di programmazione assistita costa cosi' tanto.
    """
    sorgenti = _project_sources(project_root, max_files)
    scenario = Scenario(
        name="costruzione",
        description=f"Costruzione di EcoTokens: {len(sorgenti)} file reali letti e modificati",
    )
    if not sorgenti:
        return scenario

    storia: list[dict[str, Any]] = [
        {"role": "system", "content": CODING_SYSTEM},
        {
            "role": "user",
            "content": (
                "Costruisci un gateway locale compatibile con l'API OpenAI che "
                "inoltri le richieste a Claude riducendo il consumo di token. "
                "Procedi un modulo alla volta e verifica con i test."
            ),
        },
    ]
    scenario.requests.append(_request(list(storia), tools=CODING_TOOLS))

    for indice, (percorso, contenuto) in enumerate(sorgenti):
        # Il modello chiede di leggere il file, riceve il contenuto, poi scrive.
        storia.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": f"read_{indice}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": percorso}),
                        },
                    }
                ],
            }
        )
        storia.append({"role": "tool", "tool_call_id": f"read_{indice}", "content": contenuto})
        storia.append({"role": "user", "content": f"Ora completa {percorso} e passa al prossimo."})
        scenario.requests.append(_request(list(storia), tools=CODING_TOOLS))

    # Chiusura tipica: esecuzione dei test e correzione.
    storia.append(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "tests",
                    "type": "function",
                    "function": {"name": "run_tests", "arguments": "{}"},
                }
            ],
        }
    )
    storia.append(
        {"role": "tool", "tool_call_id": "tests", "content": "58 passed in 13.13s"}
    )
    storia.append({"role": "user", "content": "Bene. Aggiorna il README e chiudi."})
    scenario.requests.append(_request(list(storia), tools=CODING_TOOLS))
    return scenario


def _project_sources(project_root: Path, max_files: int) -> list[tuple[str, str]]:
    """Legge i sorgenti veri del progetto, dal piu' grande al piu' piccolo."""
    candidati: list[tuple[int, str, str]] = []
    for percorso in sorted(project_root.glob("ecotokens/**/*.py")):
        if percorso.name == "__init__.py":
            continue
        try:
            contenuto = percorso.read_text(encoding="utf-8")
        except OSError:
            continue
        relativo = percorso.relative_to(project_root).as_posix()
        candidati.append((len(contenuto), relativo, contenuto))

    candidati.sort(reverse=True)
    return [(percorso, contenuto) for _, percorso, contenuto in candidati[:max_files]]


def all_scenarios(project_root: Path | None = None) -> list[Scenario]:
    """Il set completo di carichi del banco di misura."""
    root = project_root or Path.cwd()
    return [
        scenario_chat(),
        scenario_agente(),
        scenario_ripetitivo(),
        scenario_costruzione(root),
    ]


def scenarios_by_name(names: Iterable[str], project_root: Path | None = None) -> list[Scenario]:
    disponibili = {scenario.name: scenario for scenario in all_scenarios(project_root)}
    selezionati = []
    for name in names:
        if name not in disponibili:
            raise ValueError(
                f"Scenario sconosciuto: {name}. Disponibili: {', '.join(disponibili)}"
            )
        selezionati.append(disponibili[name])
    return selezionati
