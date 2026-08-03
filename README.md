# WorkflowOS

**WorkflowOS esiste per rendere l'impossibile possibile.**

WorkflowOS governa decisioni, processi, persone, AI e dati attraverso un sistema modulare, verificabile e adattabile, rendendo possibile ciò che oggi è troppo complesso da gestire.

Questo repository contiene il primo percorso eseguibile, deliberatamente piccolo:

```text
email di assegnazione
→ creazione Case
→ normalizzazione delle prove
→ checklist del Blueprint
→ stato blocked oppure ready
→ registro verificabile di eventi e decisioni
```

Non è ancora un gestionale completo e non dichiara un impianto tecnicamente o normativamente approvato. Lo stato `ready` vale esclusivamente per lo scope `document_intake_only`.

Il controllo tecnico successivo è separato dalla completezza documentale. Nel pilota restituisce `changes_required`: il file denominato come dimensionamento è stato verificato come piano di copertura da 18,80 kWp e non contiene schema unifilare, dimensionamento dei cavi o protezioni. Restano richiesti nove controlli elettrici espliciti; nessuna loro assenza viene trasformata automaticamente in una non conformità o in un'approvazione.

## Risultato del pilota

La fixture pubblica deriva da una vera email operativa, ma è sanificata: identità, oggetto, corpo, nomi dei file, cliente e indirizzo non sono nel repository. Dopo il controllo del contenuto dei sette PDF originali, il percorso restituisce `ready` per lo scope limitato `document_intake_only`. La verifica ha confermato:

- layout finale firmato e datato;
- piano di copertura con 40 moduli e fermaneve;
- distinta materiali di 21 posizioni;
- due stringhe da 20 moduli, report di progetto, dimensionamento e relazione di montaggio.

I documenti originali non sono pubblicati. La fixture registra soltanto classificazioni sanificate basate sulla verifica del contenuto. I test dimostrano sia il passaggio `blocked → ready`, sia il blocco esplicito quando firma o contenuto non sono verificati.

## Esecuzione

Richiede Python 3.11 o successivo e non ha dipendenze runtime esterne.

```bash
python -m workflowos.cli run \
  --process process.schema.yaml \
  --email examples/pilot/assignment.email.sanitized.json \
  --case-id pilot-pv-001 \
  --audit /tmp/workflowos-audit.jsonl \
  --at 2026-08-03T10:00:00Z

python -m workflowos.cli verify-audit \
  --audit /tmp/workflowos-audit.jsonl
```

## Test

```bash
python -m unittest discover -s tests -v
```

I test coprono:

- email reale sanificata trasformata in Case;
- regola email-first;
- identificazione deterministica dei dati mancanti;
- percorso `blocked` e percorso `ready`;
- assenza di campi cliente inventati;
- rilevamento della manomissione del registro hash-chained;
- esecuzione CLI end-to-end.

## Confini delle responsabilità

- `normalize_email_evidence`: acquisisce e normalizza soltanto le prove osservate; non le collega e non le completa.
- `build_case_from_email`: collega le prove normalizzate al Case.
- `evaluate_case`: applica soltanto il Blueprint e decide `blocked`/`ready`.
- `audit`: registra eventi e decisioni in una catena SHA-256 verificabile.

Nuovi agenti, provider, adapter o moduli restano fuori dal MVP finché questo percorso non è stabile.

## Struttura

- `process.schema.yaml`: Blueprint eseguibile e checklist.
- `examples/pilot/`: evidenza email sanificata.
- `workflowos/`: motore deterministico e audit log.
- `tests/`: test automatici unitari ed end-to-end.
- `.github/workflows/test.yml`: verifica continua su GitHub.
- `workflowos/technical_review.py`: decisione tecnica `approved`, `changes_required` o `rejected`, sempre soggetta alla firma del professionista autorizzato.

## Privacy

Email originali, documenti dei clienti e fixture non sanificate devono restare in `private/` o `examples/private/`, entrambi esclusi da Git. Il repository pubblico conserva soltanto riferimenti hash e dati minimi necessari alla prova.

## Licenza

MIT.
