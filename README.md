# Monitor sAERO

Monitor di Aero che gira da solo su GitHub Actions ogni 15 minuti, su tutte le chain dove gira Aero:
Base, OP Mainnet, Ethereum, Arbitrum, Ink, Robinhood Chain e Arc.

Ogni giro:

- legge da ogni chain gli eventi del LeafVoter di Aero (allocazioni delle posizioni sAERO, cooldown, nuovi gauge),
  le fee girate agli allocatori e i programmi di incentivi; le chain si leggono in parallelo e una chain che non
  risponde non blocca le altre;
- legge l'allocazione totale di ogni pool e com'era 1, 6 e 24 ore fa;
- una volta all'ora chiede a KyberSwap e Velora una quotazione da $1.000 e $10.000 sui pool più allocati e misura
  quanta parte passa dal pool Aero (quota di routing);
- calcola il calendario dei cooldown: quante sAERO escono dal cooldown ora per ora e da quali pool;
- una volta all'ora registra il volume e la liquidità di ogni pool Aero, il volume della stessa coppia su tutti i DEX
  della chain (DexScreener, più i 20 pool più grandi di ogni chain da GeckoTerminal) e il volume DEX di ogni chain
  con la parte di Aero (DefiLlama), quindi la quota di Aero per coppia e per chain; lo storico (orario per qualche
  giorno, poi giornaliero per 4 mesi) serve a costruire più avanti una vera previsione;
- pubblica i dati sul ramo `data` e manda le notifiche sull'app **ntfy**.

## Radar

In cima alla pagina c'è il **Radar sAERO**, la vecchia app, con i dati on-chain del monitor su tutte le chain:
inserisci la tua Allocation Power e il radar divide il 100% tra i pool tenendo conto di quanto la tua allocazione
diluisce ogni pool. Tutte le opzioni della vecchia app:

| Opzione | Cosa fa |
|---|---|
| Allocation Power, prezzo AERO | La tua posizione (resta solo sul dispositivo); il prezzo serve solo per l'APR |
| Peso del trend fee | Corregge le fee delle ultime 48 ore della loro variazione rispetto alle 48 prima |
| Concorrenza attesa | Nessun pool rende più del multiplo scelto della media del mercato (o «voti fermi») |
| Taglia i picchi di volume | Nei pool le cui fee sono salite più del mercato oltre la tolleranza conta le fee delle 48 ore prima, mosse come il mercato; gli incentivi programmati restano |
| APR 1, 6, 12, 18, 24 ore (pool suggeriti) | APR con la tua quota, dalle fee per dollaro scambiato delle ultime 48 ore applicate al volume orario di DexScreener, più gli incentivi; avviso se un'ora fa almeno il 25% del volume delle 24 ore o le ultime 6 ore almeno il 55%. L'ora è l'ultima fotografia oraria del volume: è la finestra più rumorosa e il filtro dell'APR instabile non la usa |
| Cerca un pool | Sopra l'allocazione suggerita: ogni pool del radar che corrisponde (token, coppia, rete o tipo) con voti e variazione in 6 ore, rewards, APR per voto a 1, 6, 12, 18, 24 e 48 ore (con la tua quota se è suggerito), se il radar lo suggerisce o perché no, la quota dell'Autopilot e i picchi; ✕ lo esclude, ↺ lo riprende |
| Scarta i pool con APR instabile (acceso) | Scarta i pool in cui l'APR di 6, 12, 18 o 24 ore è oltre lo scarto (2 = più del doppio o meno della metà) da quello delle 48 ore mosso come il mercato; i pool senza volume orario restano, con una nota |
| Nota «Voti in uscita» (pool suggeriti) | Se nelle ultime 6 ore sono usciti voti dal pool e, già al netto dei tuoi, a parità di rewards il rendimento atteso supera di almeno il 5% quello che ha reso ogni voto nelle 6 ore: voti usciti, quota del totale e aumento atteso |
| Posizioni da riallocare (con l'indirizzo in «I tuoi sAERO») | Le posizioni spuntate escono dai loro pool quando le sposti: il radar toglie i loro voti attuali e valuta quei pool con l'APR senza di te; usa la loro Allocation Power. Sotto i suggeriti: i pool da cui esci, con l'APR senza i tuoi voti e il motivo per cui non sono scelti |
| Minimo e massimo per pool | Quota minima e massima di ogni pool, in percentuale |
| Reti, pool e ricerche scartati | Le reti spente, i pool senza spunta e le ricerche salvate restano fuori |

Ci sono anche «Usa questa allocazione» con il confronto tra fee attese ed effettive, il cooldown di 47 ore, la
media del mercato, i movimenti di allocazione (1, 6, 24 e 48 ore) e la tabella dei pool con ricerca e ordinamento.

### Voti dell'Autopilot

Sotto l'allocazione suggerita c'è la finestra **Voti dell'Autopilot**. L'Autopilot di Aero è fatto di contratti
*relay*: ognuno possiede una sola posizione sAERO e la alloca per tutti i suoi depositanti, con lo stesso cooldown di
47 ore delle altre. Il monitor li trova sulla chain (chiede `relayType()` e `relayConfig()` ai proprietari delle
posizioni più grandi) e per ognuno mostra i voti, il conto alla rovescia alla fine del cooldown (al secondo), i pool
in cui è con la sua quota del pool e quanto renderebbe in più chi resta se uscisse, e gli ultimi spostamenti. Nei
pool suggeriti in cui l'Autopilot ha almeno il 20% dei voti c'è una nota con la sua quota e quando può riallocare.
La finestra si chiude dal suo titolo e la pagina se lo ricorda, come il pannello delle 10 posizioni più grandi.

### Le 10 posizioni più grandi

A ogni giro il monitor legge l'Allocation Power di 1.500 posizioni (tutte in circa 14 giri) e aggiorna le 40 più
grandi e le 40 più allocate: numero, AERO in stake, lock, quota di tutti i voti, proprietario, cooldown, pool e
ultima allocazione. La **Aerodrome Foundation** non ha una posizione sua: il 24 ottobre ha depositato le sue nel relay
«Autopilot Internal 004» (#10426), lo dicono la [pagina security della documentazione di
Aerodrome](https://github.com/aerodrome-finance/docs/blob/main/content/security.mdx) (multisig «Aerodrome Foundation
and Incentives», `0xBDE0…EA5a`) e i depositi sulla chain. I nomi degli indirizzi noti sono in `config.json`
(`labels`).

**Carica queste opzioni nella simulazione** manda le opzioni al monitor (avvia un giro con il token GitHub della
pagina): da quel giro in poi la simulazione le segue. Restano valide finché non ne carichi altre
e sono in `tracker.json` (`options`).

## I tuoi sAERO

In cima alla pagina scrivi l'indirizzo del tuo wallet: resta solo sul dispositivo. La pagina legge da Base quali sAERO
possiede (numero, AERO in stake, scadenza o perpetuo, Allocation Power, da quando è tuo) e prende dal monitor:

- **allocazione** di ogni posizione su ogni rete, con la quota del pool e l'APR del pool nelle ultime 48 ore;
- **cooldown** di ogni rete;
- **APR reale**: a ogni giro il monitor divide le fee e gli incentivi di ogni pool tra tutte le posizioni
  allocate, in proporzione alla loro quota. Il contratto di Aero accredita le fee ai voti allocati mentre maturano
  (ogni cambio di allocazione nel pool accredita prima quanto è maturato fino a lì), ma le fee arrivano al contratto
  solo quando qualcuno le raccoglie, su alcune reti ogni qualche ora: dove il volume orario del pool è noto il monitor
  le stima mentre maturano (volume × fee per dollaro scambiato nelle ultime 48 ore) e i trasferimenti aggiungono solo
  quello che la stima non aveva contato. Da qui gli incassi dell'ultima ora, delle ultime 6, 12 e 24 ore
  (secchi orari), degli ultimi 7 giorni, per pool
  e per giorno. L'APR è calcolato sul valore degli AERO in stake al prezzo attuale.
  Accanto a ogni APR c'è il multiplo della resa di mercato: quanto ha reso ogni voto allocato della posizione
  rispetto alla media di tutti i voti allocati nella stessa finestra (×1 = come il mercato).
- **maturato dal cambio di allocazione**: nella scheda di ogni posizione scrivi quanto mostra l'app di Aero e in
  quante ore (la pagina propone le ore dal cambio di allocazione): l'APR, i dollari al giorno e il multiplo del
  mercato si calcolano subito, senza aspettare i dati del monitor, e accanto c'è la stima del monitor per lo stesso
  periodo. Il dato resta solo su questo dispositivo e si cancella da solo quando la posizione rialloca;
- **in classifica**: dove stanno i tuoi voti tra le posizioni con più di 100k e con più di 10k sAERO allocati
  (Autopilot compreso), per quanto ha reso ogni voto allocato nelle ultime 24 ore e negli ultimi 7 giorni: posto,
  quota delle posizioni che fai meglio, il tuo APR per voto, quello a metà classifica e la soglia del primo 10%. Se
  la tua posizione è più piccola della soglia, dice in che posto sarebbe. Contano le posizioni che incassano o sono
  allocate da almeno metà del periodo; il monitor le mette in `positions.json` (`peers`).

Il primo giro ricostruisce gli ultimi 7 giorni dalle fee già raccolte, solo per le ore in cui ogni posizione aveva già
l'allocazione di adesso. I numeri sono una stima dai dati pubblici, non i claim effettivi del contratto.

## Simulazione

Ogni giro (circa ogni 15 minuti) calcola con il metodo del Radar l'allocazione migliore per una posizione di
`tracker.size` sAERO (235.000 di default) tra i pool di tutte le chain, con al massimo 20 pool per chain come il
LeafVoter, la salva e la segue per tutto il cooldown con le fee e gli incentivi che arrivano davvero, diluiti
dall'allocazione reale degli altri. Segue le opzioni caricate dal Radar: trend, concorrenza, massimo e minimo per
pool, reti, pool e ricerche scartati, picchi di volume.

A ogni giro il monitor misura quanto hanno incassato tutte le allocazioni salvate: dollari al giorno nelle ultime 24
ore, negli ultimi 7 giorni e dall'inizio, con il confronto tra incassato e previsto. C'è anche una posizione virtuale
che si sposta solo a fine cooldown. I risultati sono in `tracker.json` sul ramo `data` e nella pagina. La raccolta non
si azzera mai da sola né da un tasto.

## Pagina e app

`https://wkisai95.github.io/solid-eureka/`, servita da GitHub Pages dal ramo `data`
(Settings → Pages → Deploy from a branch → `data` / root). L'app Android **Monitor sAERO** carica la stessa pagina
dal ramo `data`, quindi non va aggiornata quando cambia la pagina.

Nella sezione «Le tue posizioni» scrivi i numeri delle tue sAERO: restano salvati solo nel dispositivo. Le sezioni
lunghe si aprono e si chiudono toccando il titolo, e la pagina ricorda quali hai lasciato aperte.

«Raccogli i dati adesso» avvia subito un giro. La prima volta chiede un token GitHub *fine-grained* limitato a
questo repository con il solo permesso **Actions: Read and write**; il token resta salvato solo nel dispositivo.

## Giri automatici

Il workflow parte ogni 15 minuti con lo schedule di GitHub (ai minuti 7, 22, 37 e 52). Lo schedule di GitHub è
«best effort» e sui repository nuovi a volte non parte per ore. Per avere comunque un giro ogni 15 minuti circa,
crea una volta l'ambiente di riserva:

Settings → Environments → New environment → nome `attesa` → spunta **Wait timer** → `14` minuti →
Save protection rules.

Da quel momento ogni giro ne prepara un altro che parte dopo la pausa dell'ambiente (senza occupare macchine durante
l'attesa), e un avvio manuale fa ripartire il ciclo se si è interrotto. Lo schedule di GitHub resta attivo: i giri
automatici troppo vicini a un altro (meno di 8 minuti) non rifanno la raccolta.

## Dati privati (Settings → Secrets and variables → Actions)

Il repository è pubblico, quindi i dati personali stanno solo nei secret, mai nei file:

| Secret | Contenuto |
|---|---|
| `NTFY_TOPIC` | Nome dell'argomento ntfy a cui sei iscritto. Senza, il monitor gira ma non manda notifiche. |
| `MY_POSITIONS` | Facoltativo. Numeri delle tue posizioni sAERO separati da virgola, per gli avvisi di fine cooldown. |

Gli avvisi sulle tue posizioni arrivano solo su ntfy e non compaiono nei dati pubblici.

## Avvisi

| Avviso | Quando |
|---|---|
| Posizione libera | Finisce il cooldown di una delle tue posizioni (`MY_POSITIONS`) su una chain |
| Ondata di cooldown | Nelle prossime `wave_window_h` ore si liberano almeno `wave_min_saero` sAERO di posizioni attive |
| Incentivi in arrivo | Nuovo programma da almeno `incentive_min_usd` dollari |
| Flusso di allocazione | Un pool guadagna o perde almeno `flow_min_pct`% e `flow_min_saero` sAERO in 6 ore |
| Routing | La quota di routing su $10.000 cambia di almeno `routing_change_pts` punti in 24 ore |
| Rendimento anomalo | Un pool sopra `apr_alert_min_alloc` rende almeno `apr_alert_multiple` volte la mediana |
| Parametri del protocollo | Cambia la durata del cooldown o si attivano le riduzioni del cooldown su una chain |
| Autopilot o balena riallocano | Un relay dell'Autopilot o una delle 10 posizioni più grandi sposta almeno `autopilot_move_min_saero` voti: da quali pool esce (con quanto prende in più chi resta) e dove entra |
| Cooldown dell'Autopilot | Mancano `autopilot_heads_up_min` minuti alla fine del cooldown di un relay da almeno 5 milioni di voti |
| Riepilogo | Ogni mattina dopo le `digest_hour` |

Le soglie, le chain e i loro RPC pubblici si cambiano in `config.json`.

## Esecuzione manuale

Dalla pagina con «Raccogli i dati adesso», oppure da **Actions → Monitor sAERO → Run workflow**
(*Invia una notifica di prova* controlla ntfy).

In locale:

```bash
pip install -r requirements.txt
NTFY_TOPIC=... python -m monitor.main --out out
```

## Fonti

- Contratti e indirizzi: [Dromos-Labs/metadex-public](https://github.com/Dromos-Labs/metadex-public)
- Prezzi: DexScreener
- Volumi: DexScreener, GeckoTerminal, DefiLlama
- Quote: KyberSwap, Velora (ParaSwap)

Non è una consulenza finanziaria.
