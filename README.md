# WorkflowOS

**WorkflowOS esiste per rendere l'impossibile possibile.**

WorkflowOS governa decisioni, processi, persone, AI e dati attraverso un sistema modulare, verificabile e adattabile, rendendo possibile ciò che oggi è troppo complesso da gestire.

A&F Elektro è il primo tenant pilota, non un vincolo del prodotto. Gli adapter e i
profili operativi devono poter essere configurati per aziende diverse senza modificare
il motore di verifica. Il trasporto Gmail accetta quindi account, nome aziendale e
ruolo del tenant da configurazione, mantenendo obbligatoria la corrispondenza tra
account autenticato e mittente. Le regole A&F/SB descritte sotto appartengono al
profilo pilota e non definiscono universalmente WorkflowOS.

Questo repository contiene il primo percorso eseguibile, deliberatamente piccolo:

```text
email di assegnazione SB
→ creazione Case
→ normalizzazione dei dati progettuali disponibili
→ stato assegnazione blocked oppure ready
→ presa in carico tecnica A&F
→ produzione TAG, IA, schema e dimensionamento
→ gestione diretta del gestore di rete da parte di A&F
→ controllo tecnico changes_required, rejected oppure approved
→ registro verificabile di eventi e decisioni
```

Non è ancora un gestionale completo e non dichiara un impianto tecnicamente o normativamente approvato. Lo stato `ready` vale esclusivamente per lo scope `assignment_intake_only`.

Il controllo tecnico successivo è separato dalla presa in carico della commessa. Nel pilota restituisce `changes_required`: un file ricevuto da SB e denominato come dimensionamento è stato verificato come piano di copertura da 18,80 kWp. È un dato progettuale disponibile, non un dimensionamento prodotto da A&F e non trasferisce a SB la responsabilità di produrre TAG, IA, schema o dimensionamento. Restano richiesti dodici controlli tecnici espliciti; nessuna loro assenza viene trasformata automaticamente in una non conformità o in un'approvazione.

Una decisione `changes_required` viene trasformata in un piano operativo sanificato con flussi A&F per progettazione tecnica, coordinamento diretto con il gestore e verifiche finali. Quando TAG e IA sono accettati, A&F carica su Monday TAG, IA e schema; ogni caricamento produce una notifica idempotente. WorkflowOS confronta i tre documenti con la commessa Monday e tra loro: cliente, indirizzo completo, riferimento di progetto e ogni altro campo comune osservabile devono coincidere. Soltanto dopo questo controllo, l'accettazione delle pratiche, la verifica della versione più recente e la risoluzione certa del destinatario SB viene richiesto automaticamente l'invio dell'email con i tre allegati. Qualsiasi dato mancante o discordante blocca l'invio e richiede verifica A&F.

Dopo l'ultimazione dell'installazione viene applicato lo stesso controllo al RaSi/SiNa. Il documento è trattato come un unico rapporto di sicurezza nelle diverse denominazioni linguistiche: deve essere completo, nella versione più recente e firmato dal professionista autorizzato. Solo dopo la corrispondenza con la commessa Monday viene richiesto automaticamente l'invio a SB. Vengono creati soltanto i flussi necessari e le dipendenze impediscono di anticipare verifiche o consegne. Nessun controllo tecnico genera automaticamente una richiesta a SB.

Il modulo `workflowos.automation` collega questi controlli agli eventi delle colonne file di Monday. L'associazione tra ID delle colonne e tipo di documento è configurabile e non contiene identificativi della bacheca nel repository pubblico. Il runtime parte in modalità `test`: costruisce la richiesta email completa ma non chiama mai l'adapter di invio. La modalità `live` richiede sia `mode=live` sia l'interruttore separato `allow_external_email=true`; senza entrambi l'invio viene rifiutato. Gli eventi duplicati, le colonne estranee e le commesse provenienti da una bacheca diversa non possono generare email.

L'adapter `workflowos.hostpoint_email` collega il mittente aziendale A&F a Hostpoint tramite SMTP STARTTLS. Accetta esclusivamente `Marvin.Caushi@elektro-af.ch` come utente e mittente, verifica nuovamente l'hash SHA-256 di ogni allegato scaricato da Monday e registra la consegna soltanto dopo l'accettazione del server SMTP. Il provider è fissato a `asmtp.mail.hostpoint.ch:587`; la password viene letta dall'ambiente e non deve essere inserita nel repository.

### Configurazione Hostpoint locale

Copiare `.env.example` in `.env.local` e compilare la password fuori da Git. Prima del test controllato lasciare:

```text
WORKFLOWOS_SMTP_LIVE_ENABLED=false
```

L'invio reale richiede tre autorizzazioni indipendenti: `mode=live`, `allow_external_email=true` e `WORKFLOWOS_SMTP_LIVE_ENABLED=true`. La sola configurazione SMTP non è quindi sufficiente ad attivare email esterne.

La GitHub Action manuale `Hostpoint SMTP connection test` usa il secret `HOSTPOINT_SMTP_PASSWORD` per autenticarsi e inviare soltanto il comando SMTP `NOOP`. Non costruisce messaggi e non invia email. Lo stesso controllo può essere eseguito nel runtime configurato con:

```bash
python -m workflowos.cli check-hostpoint-smtp
```

La GitHub Action manuale `Hostpoint SMTP self-email test` invia una sola email
senza allegati da e verso `Marvin.Caushi@elektro-af.ch`. Il destinatario non è
configurabile e il comando richiede `WORKFLOWOS_SMTP_LIVE_ENABLED=true` oltre al
secret Hostpoint:

```bash
python -m workflowos.cli send-hostpoint-self-test
```

## Risultato del pilota

La fixture pubblica deriva da una vera email operativa, ma è sanificata: identità, oggetto, corpo, nomi dei file, cliente e indirizzo non sono nel repository. La commessa risulta `ready` per lo scope limitato `assignment_intake_only`; soltanto l'email di assegnazione è obbligatoria. Gli allegati e gli altri fatti progettuali sono registrati come dati disponibili forniti da SB e non come deliverable obbligatori di SB. La verifica degli allegati ha confermato:

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

python -m workflowos.cli create-af-work-plan \
  --review examples/pilot/technical-review.sanitized.json \
  --case-id pilot-pv-001 \
  --at 2026-08-03T13:00:00Z
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
- blocco dell'email per cliente, indirizzo o dato tecnico discordante;
- invio automatico idempotente dopo il controllo documentale;
- invio del RaSi/SiNa firmato soltanto dopo l'ultimazione dell'impianto.
- runtime Monday in modalità test senza invii esterni;
- interruttore esplicito per l'email reale e blocco delle duplicazioni.
- adapter SMTP Hostpoint con STARTTLS, verifica del mittente A&F e controllo hash degli allegati.
- adapter Gmail configurabile per tenant, con mittente autenticato coincidente, firma aziendale e ruolo verificato.

## Confini delle responsabilità

- **SB Energetica** assegna la commessa e fornisce esclusivamente i dati progettuali già disponibili. Tali dati sono input facoltativi: la loro presenza, assenza o denominazione non rende SB responsabile dei documenti tecnici.
- **A&F Elektro** produce TAG, IA, schema unifilare e dimensionamento, esegue le verifiche tecniche collegate e gestisce direttamente il gestore di rete.
- Ogni flusso generato da `changes_required` viene assegnato ad `af_elektro`; il destinatario non è selezionabile da CLI e non può essere spostato su SB.
- Il flusso del gestore indica `grid_operator` come controparte esterna e `af_elektro` come gestore diretto della relazione.
- Dopo l'accettazione delle pratiche, A&F carica TAG, IA e schema su Monday; le notifiche avviano il confronto documentale e l'email a SB viene richiesta automaticamente soltanto quando tutti i controlli coincidono.
- Dopo l'ultimazione, A&F carica il RaSi/SiNa firmato su Monday; anche questo documento viene confrontato con la commessa prima dell'invio automatico a SB.
- Il destinatario SB deve essere risolto e verificato nella commessa; non viene mai dedotto da un nome ambiguo o da una comunicazione storica.
- Una richiesta a SB può nascere soltanto da un dato progettuale sorgente esplicitamente mancante, mai dall'assenza di TAG, IA, schema, dimensionamento o altri controlli tecnici.
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
