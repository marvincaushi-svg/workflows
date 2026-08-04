# Automazioni WorkflowOS

Questo documento descrive la regia unificata delle automazioni A&F senza fondere responsabilità o accessi ai sistemi esterni.

## Principi obbligatori

- Ogni processo ha **un solo obiettivo** e **un solo output**.
- Gmail/posta, Monday, GitHub e gli eventi interni sono letti solo da collector dedicati.
- La logica segue il livello `WorkflowOS -> Capability -> Provider -> Adapter -> Sistema esterno`.
- Cambiare provider non deve cambiare le regole operative.
- Tutti i lavori sono idempotenti: lo stesso evento non produce due invii o due aggiornamenti.
- Le dipendenze sono esplicite: un flusso successivo resta bloccato finché quello precedente non risulta completato per la stessa commessa.
- Gli errori temporanei vengono ritentati al massimo tre volte, con attesa crescente; poi passano in dead letter e richiedono attenzione.
- Il fuso orario operativo è `Europe/Zurich`.

## Catalogo attuale

| Automazione | Trigger | Output | Modalità |
|---|---|---|---|
| `sb-assignment-intake` | email di assegnazione SB verificata | `case_record` | scrittura interna |
| `af-technical-work-plan` | revisione tecnica con modifiche richieste | `af_work_plan` | scrittura interna |
| `missing-source-data-draft` | dato sorgente SB esplicitamente mancante | `missing_source_data_email_draft` | sola bozza |
| `accepted-practices-delivery` | TAG, IA e schema verificati su Monday | `accepted_practices_email_delivery` | invio protetto |
| `completion-report-delivery` | RaSi/SiNa firmato e verificato | `signed_safety_report_email_delivery` | invio protetto |
| `daily-operations-brief` | giorni lavorativi alle 07:00 | `daily_operations_brief` | osservazione |
| `automation-health-monitor` | ogni giorno alle 06:50 | `automation_health_report` | osservazione |
| `github-quality-gate` | modifica del repository | `quality_gate_result` | osservazione |

## Responsabilità preservate

SB Energetica assegna la commessa e fornisce soltanto i dati progettuali disponibili. A&F Elektro produce TAG, IA, schema unifilare e dimensionamento e gestisce direttamente il gestore di rete.

`missing-source-data-draft` può preparare una richiesta soltanto quando manca un input sorgente che compete a SB. Non può chiedere a SB di produrre TAG, IA, schema, dimensionamento o altri controlli tecnici A&F.

## Sicurezza degli invii

Il catalogo non abilita da solo l'invio reale. Le due automazioni di consegna richiedono almeno:

- destinatario verificato;
- chiave di idempotenza;
- interruttore live del provider;
- corrispondenza dell'identità dei documenti con la commessa;
- ultima versione dei documenti;
- hash degli allegati;
- accettazione delle pratiche oppure ultimazione e firma professionale, secondo il flusso.

Restano inoltre validi i tre interruttori Hostpoint già presenti nel runtime: modalità live, autorizzazione all'email esterna e `WORKFLOWOS_SMTP_LIVE_ENABLED=true`.

## Lingua delle comunicazioni

La lingua predefinita è `it-CH`. Le comunicazioni dirette a `info@elektro-af.ch` usano `de-CH` con ortografia svizzera, quindi senza `ß`.

La regola può essere verificata con:

```bash
python -m workflowos.cli resolve-message-language \
  --catalog automation.catalog.json \
  --recipient info@elektro-af.ch
```

## Comandi di controllo

Validare l'intero catalogo:

```bash
python -m workflowos.cli validate-automation-catalog \
  --catalog automation.catalog.json
```

Pianificare eventi raccolti dai collector:

```bash
python -m workflowos.cli plan-automation-events \
  --catalog automation.catalog.json \
  --events examples/automation/events.sanitized.json \
  --at 2026-08-04T07:15:00+02:00
```

Controllare le automazioni pianificate nel fuso di Zurigo:

```bash
python -m workflowos.cli plan-automation-schedule \
  --catalog automation.catalog.json \
  --at 2026-08-04T07:00:00+02:00
```

Generare il rapporto salute:

```bash
python -m workflowos.cli automation-health \
  --catalog automation.catalog.json \
  --at 2026-08-04T07:00:00+02:00
```

## Stato di attivazione

Il catalogo, la validazione, la pianificazione, le dipendenze, l'idempotenza, i retry, la dead letter, gli orari e le regole linguistiche sono implementati nel repository. Gli adapter esterni continuano a richiedere la rispettiva configurazione e autenticazione; dichiarare un'automazione come `enabled` significa renderla pianificabile, non concederle automaticamente accesso a Gmail, Monday, GitHub o Hostpoint.
