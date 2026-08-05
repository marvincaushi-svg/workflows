# MERAVIQA

**MERAVIQA esiste per rendere l'impossibile possibile.**

MERAVIQA governa decisioni, processi, persone, AI e dati attraverso un sistema modulare, verificabile e adattabile, rendendo possibile ciò che oggi è troppo complesso da gestire. `WorkflowOS` resta il nome tecnico interno del motore e del pacchetto Python per preservare la compatibilità.

A&F Elektro è il primo tenant pilota, non un vincolo del prodotto. Gli adapter e i
profili operativi devono poter essere configurati per aziende diverse senza modificare
il motore di verifica. Il trasporto Gmail accetta quindi account, nome aziendale e
ruolo del tenant da configurazione, mantenendo obbligatoria la corrispondenza tra
account autenticato e mittente. Le regole A&F/SB descritte sotto appartengono al
profilo pilota e non definiscono universalmente WorkflowOS.

I PDF ricevuti dai partner possono essere archiviati in entrambe le destinazioni:
MERAVIQA crea, per ciascun tenant, una cartella con il nome sanificato della
commessa e conserva il file con scrittura atomica; lo stesso PDF viene pubblicato
nella colonna documentale dell'item Monday associato. Il checksum SHA-256 blocca
duplicati e sostituzioni silenziose. Se Monday non conferma il caricamento, il PDF
resta disponibile in MERAVIQA con stato `pending` e non viene dichiarato caricato.

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

Il raccoglitore `workflowos.monday_assets` interroga Monday esclusivamente con una query in sola lettura. La richiesta GraphQL autenticata usa l'endpoint fisso `https://api.monday.com/v2` e rifiuta ogni redirect, impedendo che il token venga inoltrato a un'altra destinazione. Bacheca e colonne provengono dal profilo del tenant; un file viene accettato soltanto se appartiene esattamente all'item, alla colonna e all'asset indicati. Più file nella stessa colonna richiedono l'ID dell'asset ricevuto dall'evento, così la selezione non viene mai dedotta dal nome. Il raccoglitore ammette solo PDF, host configurati e file fino a 10 MB; controlla anche dimensione dichiarata, dimensione scaricata e firma `%PDF-`. Ogni redirect del file viene validato prima di aprire la connessione successiva, quindi non può deviare verso protocolli, porte o host non autorizzati. Gli URL temporanei Monday non vengono conservati nello stato: l'adapter riceve un locator stabile e risolve nuovamente l'asset prima dell'invio, mentre l'hash SHA-256 finale resta verificato dall'adapter email.

Il pipeline `workflowos.monday_pipeline` è il percorso applicativo consigliato: collega direttamente il raccoglitore in sola lettura, il controllo documentale, lo stato atomico e l'adapter email. Verifica che bacheca e colonne del collector coincidano con la configurazione del tenant e usa un lock per serializzare gli eventi della stessa commessa. Prima di interrogare Monday o scaricare un allegato controlla che sorgente, bacheca, item e `case_id` coincidano con lo stato persistente; un evento associato alla commessa sbagliata viene quindi bloccato senza leggere il relativo documento. Ogni risultato di verifica deve indicare commessa, tipo di documento e hash SHA-256 del PDF esaminato; se uno dei tre non coincide con l'asset Monday appena risolto, il documento viene bloccato. Prima di un invio live registra un outbox persistente; se la connessione cade dopo l'avvio SMTP, lo stato diventa `delivery_in_doubt` e MERAVIQA blocca ogni ritentativo automatico. La riconciliazione richiede una prova con hash, operatore e timestamp: una consegna confermata completa lo stato senza reinviare, mentre un mancato invio confermato abilita un solo ritentativo esplicito usando la richiesta persistita e ricontrollata.

L'outbox può essere ispezionato senza mostrare destinatario, corpo o allegati. La riconciliazione registra soltanto riferimenti non sensibili e non apre connessioni verso Monday o SMTP:

```bash
python -m workflowos.cli inspect-email-outbox --state /percorso/stato.json

python -m workflowos.cli reconcile-email-delivery \
  --state /percorso/stato.json \
  --idempotency-key HASH_SHA256 \
  --outcome confirmed-not-sent \
  --checked-at 2026-08-05T01:30:00+02:00 \
  --checked-by-ref RIFERIMENTO_OPERATORE \
  --evidence-ref-sha256 HASH_SHA256_PROVA
```

Per `confirmed-sent` è obbligatorio anche `--message-ref-sha256`. Il comando aggiorna lo stato ma non invia email: l'eventuale retry resta un'azione distinta, esplicita e protetta dall'adapter configurato.

Lo stesso principio vale per l'archivio documentale. Un caricamento Monday rimasto incerto non viene mai ritentato da solo: l'ispezione elenca i PDF che attendono un esito leggendo unicamente i manifest, senza aprire i file, senza contattare Monday e senza mostrare cartella della commessa, nome del file o identificativi Monday.

```bash
python -m workflowos.cli inspect-document-archive \
  --archive-root /percorso/archivio \
  --tenant-id tenant-id-del-profilo

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

MERAVIQA distingue due esiti che prima venivano confusi. Un adapter può dichiarare un rifiuto soltanto finché è certo che nemmeno un byte del PDF abbia lasciato il processo: colonna non configurata, item appartenente a un'altra bacheca, contenuto non valido, nome file non sicuro. In quel caso la voce conserva lo stato precedente e resta pubblicabile, `upload_attempts` non aumenta e viene registrato un `refused_attempts` con il codice del rifiuto: nessuna prova umana è necessaria per sbloccare un documento che non è mai partito. Un'autorizzazione al ritentativo già concessa non viene consumata da un rifiuto. Dal momento in cui il payload viene consegnato al transport l'esito è invece incerto, l'errore è ordinario e la voce passa a `upload_in_doubt`, che continua a richiedere riconciliazione con prova. Il ramo del rifiuto è per costruzione irraggiungibile dopo l'inizio della trasmissione.

Ogni voce indica l'azione richiesta: `publish_monday_upload`, `reconcile_monday_upload`, `retry_monday_upload` oppure `none`. L'unico ritentativo consentito si esegue con un comando distinto:

```bash
python -m workflowos.cli retry-monday-upload \
  --archive-root /percorso/archivio \
  --tenant-id tenant-id-del-profilo \
  --case-id case-042 \
  --content-sha256 HASH_SHA256_DEL_PDF
```

A differenza dell'ispezione e della riconciliazione, questo comando scrive davvero su Monday e richiede quindi `WORKFLOWOS_MONDAY_UPLOAD_ENABLED=true`. Resta comunque subordinato all'autorizzazione già registrata: senza una prova `confirmed-not-uploaded` la voce non è in `retry_authorized` e il comando si rifiuta. Il documento viene ricostruito dal manifest, quindi il ritentativo non può essere indirizzato a un altro item, e il publisher deve appartenere allo stesso tenant della commessa. Un rifiuto prima della trasmissione conserva l'autorizzazione; un esito nuovamente incerto riporta la voce in `upload_in_doubt` e richiede una nuova riconciliazione. La riconciliazione ricostruisce il legame del documento dal manifest e dal PDF archiviato, quindi non può essere dirottata verso un altro item o un'altra colonna Monday; viene eseguita senza adapter di pubblicazione e non apre alcuna connessione. `confirmed-uploaded` chiude la voce, `confirmed-not-uploaded` autorizza un solo ritentativo esplicito, che resta un'azione distinta con l'adapter Monday configurato.

L'adapter `workflowos.monday_uploads` è l'unico componente autorizzato a scrivere un file su Monday ed è disattivato finché `WORKFLOWOS_MONDAY_UPLOAD_ENABLED=true` non viene impostato esplicitamente. Riusa bacheca e colonne del profilo tenant: rifiuta una colonna non configurata prima di qualsiasi richiesta e, prima di trasmettere un solo byte, verifica con una query in sola lettura che l'item appartenga alla bacheca attesa, così un PDF non può finire nella commessa di un altro tenant. Item e colonna raggiungono la mutation già vincolati a cifre e a un identificatore alfanumerico configurato, quindi non possono introdurre sintassi GraphQL. L'endpoint file resta fissato a `https://api.monday.com/v2/file` e ogni redirect della richiesta autenticata viene rifiutato. La conferma restituita dichiara ciò che Monday ha effettivamente accettato — l'id dell'asset creato e, quando riportata, la dimensione memorizzata confrontata con quella trasmessa; l'hash presente nella conferma è il digest dei byte inviati, perché Monday non restituisce un checksum lato server.

Il binding può essere verificato senza caricare nulla:

```bash
python -m workflowos.cli check-monday-upload --item-id ID_ITEM_MONDAY
```

Il comando esegue soltanto la query di verifica: non richiede l'interruttore di caricamento e non scrive su Monday.

L'adapter `workflowos.hostpoint_email` collega un account aziendale Hostpoint tramite SMTP STARTTLS. Account, mittente, nome aziendale e ruolo provengono dalla configurazione del tenant; il mittente deve coincidere con l'account autenticato. L'adapter verifica nuovamente l'hash SHA-256 di ogni allegato scaricato da Monday, blocca duplicati e invii oltre 20 MB e registra la consegna soltanto dopo l'accettazione del server SMTP. Il provider resta fissato a `asmtp.mail.hostpoint.ch:587`; la password viene letta dall'ambiente e non deve essere inserita nel repository. A&F è soltanto il profilo pilota predefinito.

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
- raccoglitore Monday in sola lettura con associazione esatta item/colonna/asset e senza persistenza degli URL temporanei;
- legame obbligatorio della verifica a commessa, tipo documento e hash SHA-256 del PDF Monday esaminato;
- interruttore esplicito per l'email reale e blocco delle duplicazioni.
- adapter SMTP Hostpoint configurabile per tenant, con STARTTLS, identità autenticata coincidente, controllo hash, duplicati e limite allegati di 20 MB.
- adapter Gmail configurabile per tenant, con mittente autenticato coincidente, firma aziendale e ruolo verificato.
- ispezione dell'archivio in sola lettura e riconciliazione dei caricamenti Monday incerti, con legame documentale ricostruito dal manifest e nessun accesso a Monday.
- caricamento Monday protetto da interruttore esplicito, con verifica della bacheca prima dell'invio, rifiuto dei redirect, blocco delle colonne non configurate e controllo della dimensione memorizzata.
- distinzione fra mancata trasmissione certa ed esito ignoto: la prima conserva lo stato e non consuma un ritentativo autorizzato, la seconda continua a richiedere riconciliazione con prova.

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
