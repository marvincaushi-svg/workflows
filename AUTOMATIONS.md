# Automazioni MERAVIQA

Questo documento descrive la regia unificata del motore interno `WorkflowOS`. Le regole operative appartengono a un profilo tenant esplicito; il motore MERAVIQA non contiene un'azienda predefinita.

## Principi obbligatori

- Ogni processo ha **un solo obiettivo** e **un solo output**.
- Posta, project management, controllo versione ed eventi interni sono letti solo da collector dedicati.
- La logica segue il livello `MERAVIQA -> Capability -> Provider -> Adapter -> Sistema esterno`.
- Cambiare provider non deve cambiare le regole operative.
- Tutti i lavori sono idempotenti: lo stesso evento non produce due invii o due aggiornamenti.
- Le dipendenze sono esplicite: un flusso successivo resta bloccato finché quello precedente non risulta completato per la stessa commessa.
- Gli errori temporanei vengono ritentati al massimo tre volte, con attesa crescente; poi passano in dead letter e richiedono attenzione.
- Il fuso orario, i ruoli, i provider e le regole linguistiche appartengono al profilo tenant.

## Separazione del tenant

Ogni catalogo dichiara obbligatoriamente:

- un `tenant.id` stabile;
- il nome dell'organizzazione;
- le associazioni fra ruoli di processo e ruoli reali dell'azienda;
- fuso orario, provider e regole di comunicazione;
- automazioni, dipendenze e protezioni.

Il percorso del catalogo è sempre esplicito nei comandi. `tenant.id` entra nelle chiavi di idempotenza, negli slot pianificati e nello stato di completamento, impedendo collisioni fra aziende anche quando commesse e automazioni hanno lo stesso identificatore.

Il repository pubblico contiene soltanto il profilo sanificato `examples/tenants/electrical-contractor/automation.catalog.sanitized.json`. I cataloghi operativi reali, con destinatari e identificativi aziendali, restano fuori dal repository.

## Catalogo sanificato

| Automazione | Trigger | Output | Modalità |
|---|---|---|---|
| `assignment-intake` | email di assegnazione partner verificata | `case_record` | scrittura interna |
| `technical-work-plan` | revisione tecnica con modifiche richieste | `technical_work_plan` | scrittura interna |
| `missing-source-data-draft` | dato sorgente del partner esplicitamente mancante | `missing_source_data_email_draft` | sola bozza |
| `accepted-practices-delivery` | documenti tecnici verificati nel sistema configurato | `accepted_practices_email_delivery` | invio protetto |
| `completion-report-delivery` | RaSi/SiNa firmato e verificato | `signed_safety_report_email_delivery` | invio protetto |
| `daily-operations-brief` | giorni lavorativi alle 07:00 | `daily_operations_brief` | osservazione |
| `automation-health-monitor` | ogni giorno alle 06:50 | `automation_health_report` | osservazione |
| `github-quality-gate` | modifica del repository | `quality_gate_result` | osservazione |

## Responsabilità configurabili

Il catalogo collega i ruoli astratti `assignment_owner`, `available_project_data_provider`, `technical_document_owner` e `grid_operator_manager` ai ruoli reali del tenant. Le automazioni indicano il ruolo astratto responsabile e il control plane lo risolve attraverso il profilo.

`missing-source-data-draft` può preparare una richiesta soltanto quando manca un input sorgente che compete al partner. Non può attribuire al partner i documenti o i controlli tecnici assegnati al tenant.

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

Nel profilo sanificato la lingua predefinita è `it-CH`. La regola di esempio per `operations@example.com` usa `de-CH` con ortografia svizzera, quindi senza `ß`.

La regola può essere verificata con:

```bash
python -m workflowos.cli resolve-message-language \
  --catalog examples/tenants/electrical-contractor/automation.catalog.sanitized.json \
  --recipient operations@example.com
```

## Comandi di controllo

Validare l'intero catalogo:

```bash
python -m workflowos.cli validate-automation-catalog \
  --catalog examples/tenants/electrical-contractor/automation.catalog.sanitized.json
```

Pianificare eventi raccolti dai collector:

```bash
python -m workflowos.cli plan-automation-events \
  --catalog examples/tenants/electrical-contractor/automation.catalog.sanitized.json \
  --events examples/automation/events.sanitized.json \
  --at 2026-08-04T07:15:00+02:00
```

Controllare le automazioni pianificate nel fuso del tenant:

```bash
python -m workflowos.cli plan-automation-schedule \
  --catalog examples/tenants/electrical-contractor/automation.catalog.sanitized.json \
  --at 2026-08-04T07:00:00+02:00
```

Generare il rapporto salute:

```bash
python -m workflowos.cli automation-health \
  --catalog examples/tenants/electrical-contractor/automation.catalog.sanitized.json \
  --at 2026-08-04T07:00:00+02:00
```

Ispezionare l'archivio documentale senza contattare Monday:

```bash
python -m workflowos.cli inspect-document-archive \
  --archive-root /percorso/archivio
```

Registrare la prova che risolve un caricamento Monday incerto:

```bash
python -m workflowos.cli reconcile-monday-upload \
  --archive-root /percorso/archivio \
  --tenant-id tenant-id-del-profilo \
  --case-id case-042 \
  --content-sha256 HASH_SHA256_DEL_PDF \
  --outcome confirmed-not-uploaded \
  --checked-at 2026-08-05T09:00:00+02:00 \
  --checked-by-ref RIFERIMENTO_OPERATORE \
  --evidence-ref-sha256 HASH_SHA256_PROVA
```

Verificare il binding di scrittura Monday senza caricare alcun file:

```bash
python -m workflowos.cli check-monday-upload --item-id ID_ITEM_MONDAY
```

Il caricamento vero e proprio richiede l'interruttore separato `WORKFLOWOS_MONDAY_UPLOAD_ENABLED=true`, in aggiunta al token e al profilo tenant. Come per l'email, la sola configurazione non arma la scrittura.

Eseguire l'unico ritentativo autorizzato (scrive su Monday, richiede l'interruttore):

```bash
python -m workflowos.cli retry-monday-upload \
  --archive-root /percorso/archivio \
  --tenant-id tenant-id-del-profilo \
  --case-id case-042 \
  --content-sha256 HASH_SHA256_DEL_PDF
```

Il ciclo operativo completo è quindi: `inspect-document-archive` indica l'azione, `reconcile-monday-upload` registra la prova, `retry-monday-upload` esegue il singolo ritentativo consentito.

Un caricamento respinto prima della trasmissione non è un caricamento incerto: la voce conserva lo stato precedente, resta pubblicabile e non consuma un eventuale ritentativo autorizzato. Soltanto un esito realmente ignoto passa a `upload_in_doubt` e richiede la riconciliazione con prova.

Entrambi i comandi restano osservativi rispetto ai sistemi esterni: il primo legge soltanto i manifest, il secondo aggiorna lo stato dell'archivio. Il ritentativo del caricamento resta un'azione separata, autorizzata soltanto da una prova `confirmed-not-uploaded` e vincolata all'item e alla colonna registrati al momento dell'archiviazione.

## Stato di attivazione

Il catalogo, la validazione, la pianificazione, le dipendenze, l'idempotenza, i retry, la dead letter, gli orari e le regole linguistiche sono implementati nel repository. Gli adapter esterni continuano a richiedere la rispettiva configurazione e autenticazione; dichiarare un'automazione come `enabled` significa renderla pianificabile, non concederle automaticamente accesso a posta, Monday, GitHub o Hostpoint.
