# Monitor sAERO

Monitor di Aero che gira da solo su GitHub Actions ogni 15 minuti, su tutte le chain dove gira Aero:
Base, OP Mainnet, Ethereum, Arbitrum, Ink, Robinhood Chain e Arc.

Ogni giro:

- legge da ogni chain gli eventi del LeafVoter di Aero (allocazioni delle posizioni sAERO, cooldown, nuovi gauge),
  le fee girate agli allocatori e i programmi di incentivi; le chain si leggono in parallelo e una chain che non
  risponde non blocca le altre. Su Base l'ora di ogni evento viene dal ritmo dei blocchi (uno ogni 2 secondi, uno
  ogni 200 ms dall'aggiornamento Denim del 5 novembre 2026, blocco 53.556.127), controllato a ogni giro sull'ultimo
  blocco letto: se non torna più, il monitor prende gli orari dalla chain e lo scrive nel log;
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
| Calcola sull'APR (48 ore) | La finestra su cui stimare quanto renderà ogni pool: le ultime 48 ore, o le ultime 24, 12, 6 ore o l'ultima ora, con le fee misurate sul gauge in quelle ore portate a 48 ore più gli incentivi programmati; con una finestra corta trend e taglio dei picchi sono spenti |
| Correggi per il giorno della settimana | Porta le fee della finestra ai giorni delle prossime 48 ore con le tendenze di ogni pool (vedi «Tendenze fee»), tra 0,6× e 1,6×; spento di default |
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
  (ogni cambio di allocazione nel pool accredita prima quanto il gauge ha in sospeso fino a lì), ma le fee passano al
  contratto (FeesCollected) solo quando qualcuno fa claim: su un pool tranquillo anche giorni dopo, tutte insieme.
  Per questo il monitor le misura sul gauge a ogni giro: le fee in sospeso adesso (`pendingFees()`), meno quelle del
  giro prima, più quelle raccolte nel frattempo. Da qui gli incassi dell'ultima ora, delle ultime 6, 12 e 24 ore
  (secchi orari), degli ultimi 7 giorni, per pool
  e per giorno. L'APR è calcolato sul valore degli AERO in stake al prezzo attuale.
  Accanto a ogni APR c'è il multiplo della resa di mercato: quanto ha reso ogni voto allocato della posizione
  rispetto alla media di tutti i voti allocati nella stessa finestra (×1 = come il mercato).
- **maturato dall'ultima allocazione**: nella scheda di ogni posizione scrivi quanto mostra l'app di Aero; il tempo
  è quello passato dall'ultima allocazione della posizione (l'ora la esporta il monitor) al momento in cui premi
  Calcola. L'APR, i dollari al giorno e il multiplo del mercato si calcolano subito, senza aspettare i dati del
  monitor, e accanto c'è quanto stima il monitor dalla stessa allocazione (fino a 4 giorni). Il dato resta solo su
  questo dispositivo e si cancella da solo quando la posizione rialloca;
- **in classifica**: dove stanno i tuoi voti tra le posizioni con più di 100k e con più di 10k sAERO allocati
  (Autopilot compreso), per quanto ha reso ogni voto allocato nelle ultime 24 ore e negli ultimi 7 giorni: posto,
  quota delle posizioni che fai meglio, il tuo APR per voto, quello a metà classifica e la soglia del primo 10%. Se
  la tua posizione è più piccola della soglia, dice in che posto sarebbe. Contano le posizioni che incassano o sono
  allocate da almeno metà del periodo; il monitor le mette in `positions.json` (`peers`).

Il primo giro ricostruisce gli ultimi 7 giorni dalle fee già raccolte, solo per le ore in cui ogni posizione aveva già
l'allocazione di adesso. I numeri sono una stima dai dati pubblici, non i claim effettivi del contratto.

Fino al 9 novembre 2026 le fee venivano dai trasferimenti (FeesCollected) e, dove c'era il volume orario, da una
stima volume × fee per dollaro scambiato: sui pool dove si fa claim di rado un solo trasferimento portava fee di
giorni, gonfiando o azzerando le 48 ore e la stima. Al passaggio il monitor ha letto le fee in sospeso di ogni gauge
a ogni ora delle 48 ore prima, dove gli endpoint conservano lo stato vecchio, e ha rifatto con le fee misurate le
ultime 24 ore orarie delle posizioni che non hanno cambiato allocazione; dove lo stato vecchio non c'è, le ore prima
del passaggio restano sui trasferimenti finché escono dalle 48 ore e la variazione delle fee riparte da zero.

## Tendenze fee

Per ogni pool con almeno 50.000 voti (i 160 più votati) il monitor legge da GeckoTerminal le candele orarie delle
ultime sei settimane circa: volume e prezzo. Le fee seguono il volume, quindi la forma settimanale del volume è
quella delle fee; le fee misurate sul gauge vengono tenute giorno per giorno e dicono quanto vale un giorno tipico
in dollari. Dieci pool a ogni giro (limite dell'API gratuita), ognuno riletto due volte al giorno.

- **Per giorno della settimana** (UTC): il giorno tipico di ogni giorno rispetto al giorno medio del pool (1,0×).
  I giorni eccezionali contano al massimo 3 volte la mediana, così un evento non passa per un'abitudine.
- **Fasce di sei ore**: 28 blocchi della settimana, dai soli giorni normali. Con questi la pagina confronta le
  prossime 48 ore con le ore su cui è misurato un APR.
- **Eventi (catalyst)**: i giorni con almeno 2,5 volte il volume mediano del pool, con il movimento del prezzo, se
  si è mosso tutto il mercato (almeno un quarto dei pool in picco, o volume totale 1,6 volte il solito; con le
  variazioni di ETH e BTC) o solo quel pool, e se in quei giorni è partito un programma di incentivi.
- **Radar, «Correggi per il giorno della settimana»** (spento di default): porta le fee della finestra scelta ai
  giorni delle prossime 48 ore (tra 0,6× e 1,6×), per i pool con almeno 14 giorni di storia.

- **Backtest delle previsioni**: sulla storia oraria di ogni pool il monitor rifà, ogni sei ore, undici previsioni
  delle 48 ore successive (ultime 6, 12, 24, 48, 96 ore, mediana di quattro giorni, medie pesate, combinazioni),
  ognuna anche corretta per il giorno della settimana con le sole fasce note fino a quel momento, e misura di quanto
  sbagliano (errore tipico: media di |ln(previsto/reale)|). Nel radar, «Previsione più affidabile (backtest)» usa
  quella che sbaglia meno, applicata alle fee misurate ora per ora e ripulita dal suo scarto medio; per ogni pool
  suggerito mostra di quanto ha sbagliato su quel pool. Una rete misurata da meno di quattro giorni usa la migliore
  tra le previsioni che leggono solo 48 ore.

Con cinque o sei campioni per giorno della settimana sono tendenze, non certezze: accanto a ogni giorno c'è il
numero di settimane viste.

## Stocks onchain

Le azioni tokenizzate su Robinhood Chain (NVDA, TSLA, SPY…: i token dei pool Aero il cui simbolo è un ticker USA,
tranne U che lì è una stablecoin) e
su Base (NVDAc, TSLAc…: i token azionari con la «c» finale, 8 decimali e indirizzo che inizia con `0xb2` e venti zeri;
più i fondi WisdomTree `wt…`). Una volta per ogni ora (al primo giro dell'ora, ad almeno 40 minuti dalla lettura
prima) il monitor legge per ognuno, da DexScreener, tutti i suoi pool sui DEX
della rete: volume e operazioni dell'ultima ora (acquisti e vendite), prezzo, variazione del prezzo, liquidità e
volume delle 24 ore; dalla chain legge la supply (`totalSupply`), che da un'ora all'altra dice quante azioni sono state
portate sulla chain o tolte. Tiene 7 giorni di storia oraria in `state.json.gz` e scrive `stocks.json` sul ramo
`data` (72 ore per rete, 48 per titolo).

Nella pagina, sotto il Radar, scegli il periodo (1, 2, 6 o 12 ore): per ogni rete il volume del periodo con la
variazione sulle ore subito prima e sulle stesse ore di ieri, le operazioni, la supply portata sulla chain o tolta e il
grafico delle ultime 72 ore con il periodo in evidenza e la fascia della borsa di New York aperta; poi la tabella dei
titoli della rete scelta per lo stesso periodo (prezzo e sua variazione, volume e sua variazione, operazioni, con la
quota di acquisti per l'ultima ora, supply, volume 24 ore, barre delle ultime 24 ore), con ricerca. Le ore senza
lettura contano alla media delle altre del periodo. In `config.json` `"stocks": {"add": [...], "skip": [...]}` aggiunge o toglie token (chiavi `chain:indirizzo`).

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

Per ora la pagina non mostra Le 10 posizioni più grandi, Simulazione (con il tasto che le carica le opzioni del
Radar), Ondate di cooldown, Posizioni grandi in uscita, Pool, Chain e Avvisi recenti: il monitor continua a
raccoglierne tutti i dati e gli avvisi continuano ad arrivare. La riga in fondo alla pagina le rimostra su quel
dispositivo; per toglierle dall'elenco basta togliere il loro nome da `HIDDEN` in `site/index.html`.

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
