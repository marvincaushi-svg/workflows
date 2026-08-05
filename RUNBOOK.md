# Runbook: test reale di MERAVIQA

Questo documento porta il motore dal test locale all'uso su sistemi veri, in fasi
separate. **Ogni fase è verificabile da sola e nessuna fase arma la successiva.**
Fermati alla fase che ti basta: le fasi 0–2 non scrivono nulla da nessuna parte.

Prima di cominciare, due avvertenze oneste:

- **L'adapter di caricamento Monday non è mai stato eseguito contro l'API reale.**
  È scritto sul contratto documentato da Monday e coperto da test con transport
  simulati, ma la prima richiesta vera partirà durante la fase 3. Trattala come
  un collaudo, non come un'operazione di routine.
- **Dalla fase 3 in poi si scrive su sistemi di produzione.** Usa una commessa o
  un item di prova, non una commessa reale, finché il collaudo non è concluso.

---

## Fase 0 — Verifica locale (nessuna credenziale)

```bash
python -m unittest discover -s tests -v
```

Atteso: tutti i test verdi. Richiede Python 3.11+ e nessuna dipendenza esterna.

Poi valida il catalogo del tenant:

```bash
python -m workflowos.cli validate-automation-catalog --catalog IL_TUO_CATALOGO.json
```

Atteso: `"ok": true` con tenant, fuso orario e conteggio automazioni corretti.
Se qui qualcosa non torna, tutte le fasi successive useranno regole sbagliate.

---

## Fase 1 — Percorso documentale a secco (nessuna credenziale)

Archivia un PDF **senza pubblicarlo**. Nessuna rete viene toccata.

```bash
python -m workflowos.cli archive-document \
  --archive-root /percorso/archivio \
  --tenant-id IL_TUO_TENANT_ID \
  --case-id case-100 \
  --case-name "Nome Commessa" \
  --monday-item-id ID_ITEM \
  --monday-column-id ID_COLONNA \
  --document-type tag_grid_connection_application \
  --pdf /percorso/documento.pdf
```

Atteso: `"status": "archived"`, `"monday_status": "pending"`,
`"monday_uploaded": false`. Il PDF è ora su disco in
`/percorso/archivio/IL_TUO_TENANT_ID/Nome Commessa/` con il suo manifest.

Controlla l'arretrato:

```bash
python -m workflowos.cli inspect-document-archive --archive-root /percorso/archivio
```

Atteso: una voce con `"action_required": "publish_monday_upload"`. L'ispezione
non mostra mai cartelle o nomi di file: prendi nota di `content_sha256`, ti
serve nei comandi successivi.

---

## Fase 2 — Monday in sola lettura (serve il token)

Configura l'ambiente **senza** l'interruttore di scrittura:

```bash
export MONDAY_API_TOKEN='...'                 # dal secret store, mai nel repo
export WORKFLOWOS_TENANT_ID='IL_TUO_TENANT_ID'
export WORKFLOWOS_MONDAY_BOARD_ID='ID_BACHECA'
export WORKFLOWOS_MONDAY_DOCUMENT_COLUMNS='{"ID_COL_TAG":"tag_grid_connection_application","ID_COL_IA":"installation_notice_ia","ID_COL_SCHEMA":"single_line_diagram","ID_COL_SINA":"safety_report_rasi_sina"}'
export WORKFLOWOS_MONDAY_UPLOAD_ENABLED=false
```

Verifica il binding di scrittura senza caricare nulla:

```bash
python -m workflowos.cli check-monday-upload --item-id ID_ITEM
```

Atteso: `"status": "ok"`, `"uploaded": false` e `board_id` uguale alla bacheca
configurata. Questo comando esegue **una sola query in sola lettura**.

Errori utili a questo punto:

| Messaggio | Significato |
|---|---|
| `Monday item belongs to another board` | l'item non è nella bacheca configurata |
| `Monday API returned errors` | token non valido o senza permessi |
| `Monday column is not configured for documents` | mappatura colonne incompleta |

**Non proseguire finché questa fase non è pulita.** È l'unico controllo che
valida token, bacheca e colonne senza conseguenze.

---

## Fase 3 — Primo caricamento reale su Monday

Arma l'interruttore, in una shell dedicata:

```bash
export WORKFLOWOS_MONDAY_UPLOAD_ENABLED=true
```

Pubblica il PDF già archiviato nella fase 1 ripetendo lo stesso comando con
`--publish-to-monday`. L'archiviazione è idempotente sul checksum: il file non
viene riscritto, viene solo tentata la pubblicazione della voce `pending`.

```bash
python -m workflowos.cli archive-document \
  --archive-root /percorso/archivio \
  --tenant-id IL_TUO_TENANT_ID \
  --case-id case-100 \
  --case-name "Nome Commessa" \
  --monday-item-id ID_ITEM \
  --monday-column-id ID_COLONNA \
  --document-type tag_grid_connection_application \
  --pdf /percorso/documento.pdf \
  --publish-to-monday
```

Tre esiti possibili, tutti previsti:

- **`"monday_uploaded": true`** — il file è sull'item. Verificalo anche
  nell'interfaccia Monday: è il collaudo vero dell'adapter.
- **`"monday_status": "pending"` con `refused_attempts` aumentato** — rifiuto
  *prima* della trasmissione: nulla è partito. Leggi `last_refusal_code`
  nell'ispezione, correggi la configurazione e ripeti. Non serve alcuna prova.
- **`"monday_status": "upload_in_doubt"`** — esito ignoto: la connessione è
  caduta a trasmissione avviata. Vai alla fase 4. **Non ripetere il comando**:
  il motore lo impedisce apposta, perché un secondo tentativo cieco potrebbe
  duplicare il file.

---

## Fase 4 — Riconciliazione di un caricamento incerto

Guarda su Monday se il file è arrivato, poi registra ciò che hai osservato.
Servono un riferimento non sensibile dell'operatore e l'hash di una prova
(per esempio lo screenshot o l'export che hai conservato).

```bash
# calcola l'hash della tua prova
sha256sum /percorso/prova.png
```

Se il file **è** su Monday:

```bash
python -m workflowos.cli reconcile-monday-upload \
  --archive-root /percorso/archivio \
  --tenant-id IL_TUO_TENANT_ID --case-id case-100 \
  --content-sha256 HASH_DEL_PDF \
  --outcome confirmed-uploaded \
  --checked-at 2026-08-05T09:00:00+02:00 \
  --checked-by-ref RIFERIMENTO_OPERATORE \
  --evidence-ref-sha256 HASH_DELLA_PROVA
```

Se il file **non** è su Monday, usa `--outcome confirmed-not-uploaded`: la voce
passa a `retry_authorized` e sblocca **un solo** ritentativo:

```bash
python -m workflowos.cli retry-monday-upload \
  --archive-root /percorso/archivio \
  --tenant-id IL_TUO_TENANT_ID --case-id case-100 \
  --content-sha256 HASH_DEL_PDF
```

Questo comando scrive su Monday e richiede quindi
`WORKFLOWOS_MONDAY_UPLOAD_ENABLED=true`. Se anche il ritentativo resta incerto,
la voce torna in `upload_in_doubt` e serve una nuova riconciliazione.

---

## Fase 5 — Email (tre interruttori indipendenti)

L'invio reale richiede **tutte e tre** le condizioni: `mode=live`,
`allow_external_email=true` e `WORKFLOWOS_SMTP_LIVE_ENABLED=true`.

Prima autenticati senza inviare nulla (il comando manda solo `NOOP`):

```bash
export WORKFLOWOS_SMTP_USERNAME='...'
export WORKFLOWOS_SMTP_PASSWORD='...'      # dal secret store
export WORKFLOWOS_SMTP_LIVE_ENABLED=false
python -m workflowos.cli check-hostpoint-smtp
```

Solo dopo un esito pulito, e solo se vuoi provare un invio, imposta
`WORKFLOWOS_SMTP_LIVE_ENABLED=true` ed esegui l'auto-test, che invia una sola
email alla casella del tenant e non accetta un destinatario diverso:

```bash
python -m workflowos.cli send-hostpoint-self-test
```

Se una consegna resta incerta, il percorso è lo stesso dell'archivio:

```bash
python -m workflowos.cli inspect-email-outbox --state /percorso/stato.json
python -m workflowos.cli reconcile-email-delivery --state /percorso/stato.json ...
```

---

## Fase 6 — Regia quotidiana

Registra l'esito di ogni lavoro pianificato: è ciò che sblocca le dipendenze,
programma i ritentativi e alimenta il monitoraggio.

```bash
python -m workflowos.cli record-automation-outcome \
  --catalog IL_TUO_CATALOGO.json \
  --state /percorso/stato-runtime.json \
  --work-item /percorso/work-item.json \
  --outcome succeeded \
  --at 2026-08-05T09:00:00+02:00
```

Con `--outcome failed` è obbligatorio `--error-code`: il motore programma un
ritentativo con attesa crescente e, esaurito il numero massimo di tentativi,
sposta il lavoro in dead letter.

Il brief del mattino riassume tutto:

```bash
python -m workflowos.cli daily-operations-brief \
  --catalog IL_TUO_CATALOGO.json \
  --state /percorso/stato-runtime.json \
  --archive-root /percorso/archivio \
  --at 2026-08-05T07:00:00+02:00
```

`status: attention_required` significa che c'è un fallimento in dead letter
oppure un documento che richiede una persona. Una dipendenza non ancora
soddisfatta **non** alza lo stato: è il normale ordine dei flussi.

---

## Come fermare tutto

Nessun comando è pianificato o automatico: il motore agisce solo quando lo
invochi. Per disarmare le scritture esterne:

```bash
export WORKFLOWOS_MONDAY_UPLOAD_ENABLED=false
export WORKFLOWOS_SMTP_LIVE_ENABLED=false
```

I comandi di ispezione, il brief e il rapporto salute continuano a funzionare:
sono in sola lettura e non richiedono alcun interruttore.

---

## Cosa conservare fuori dal repository

Token, password, cataloghi operativi con destinatari e identificativi reali,
PDF dei clienti e stati runtime. Il repository pubblico contiene solo il
profilo sanificato. `private/` ed `examples/private/` sono esclusi da Git.
