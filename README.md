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
| Escludi i picchi di volume | Toglie i pool le cui fee sono salite più del mercato oltre la tolleranza |
| Minimo e massimo per pool | Quota minima e massima di ogni pool, in percentuale |
| Reti, pool e ricerche scartati | Le reti spente, i pool senza spunta e le ricerche salvate restano fuori |

Ci sono anche «Usa questa allocazione» con il confronto tra fee attese ed effettive, il cooldown di 47 ore, la
media del mercato, i movimenti di allocazione (1, 6, 24 e 48 ore) e la tabella dei pool con ricerca e ordinamento.

**Carica queste opzioni nelle simulazioni** manda le opzioni al monitor (avvia un giro con il token GitHub della
pagina): da quel giro in poi tutti i metodi della simulazione le seguono. Restano valide finché non ne carichi altre
e sono in `tracker.json` (`options`).

## Simulazione

Ogni giro (circa ogni 15 minuti) calcola con cinque metodi l'allocazione migliore per una posizione di
`tracker.size` sAERO (235.000 di default) tra i pool di tutte le chain, con al massimo 20 pool per chain come il
LeafVoter, la salva e la segue per tutto il cooldown con le fee e gli incentivi che arrivano davvero, diluiti
dall'allocazione reale degli altri:

| Metodo | Come stima la resa delle prossime 47 ore |
|---|---|
| Ultime 48 ore | Fee delle ultime 48 ore più gli incentivi programmati |
| Tendenza 12 ore | Metà peso alle ultime 48 ore, metà alle ultime 12 |
| Prudente | Come il primo, ma prevede che gli altri allocatori arrivino sui pool che rendono molto più della media |
| Chain nuove | Come il primo, ma solo sulle coppie più grosse (per volume su tutti i DEX) delle chain fuori da Base e OP dove Aero ha meno del 15% del volume DEX, e solo dove Aero ha meno della metà del volume della coppia |
| Radar | Il metodo del Radar con il trend e la concorrenza caricati dalla pagina |

Tutti i metodi rispettano le opzioni caricate dal Radar: massimo e minimo per pool, reti, pool e ricerche scartati,
picchi di volume. A ogni giro il monitor misura quanto hanno incassato tutte le allocazioni salvate e ne fa una
classifica: dollari al giorno nelle ultime 24 ore, negli ultimi 7 giorni e dall'inizio, con il confronto tra
incassato e previsto. Ogni metodo ha anche una posizione virtuale che si sposta solo a fine cooldown. I risultati
sono in `tracker.json` sul ramo `data` e nella pagina. La raccolta non si azzera mai da sola né da un tasto.

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
| Riepilogo | Ogni mattina dopo le `digest_hour` |

Le soglie, le chain e i loro RPC pubblici, e le regole del metodo «Chain nuove» (`strategies.nuove`) si cambiano in
`config.json`.

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
