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
- pubblica i dati sul ramo `data` e manda le notifiche sull'app **ntfy**.

## Simulazione

Ogni giro calcola l'allocazione migliore per una posizione di `tracker.size` sAERO (235.000 di default) tra i pool
di tutte le chain, con al massimo 20 pool per chain come il LeafVoter, e segue ogni consiglio per tutto il cooldown
con le fee e gli incentivi che arrivano davvero, diluiti dall'allocazione reale degli altri:

| Metodo | Come stima la resa delle prossime 47 ore |
|---|---|
| Ultime 48 ore | Fee delle ultime 48 ore più gli incentivi programmati |
| Tendenza 12 ore | Metà peso alle ultime 48 ore, metà alle ultime 12 |
| Prudente | Come il primo, ma prevede che gli altri allocatori arrivino sui pool che rendono molto più della media |

Ogni metodo ha anche una posizione virtuale che si sposta solo a fine cooldown. I risultati sono in
`tracker.json` sul ramo `data` e nella pagina. Dalla pagina, «Nuova raccolta» ricomincia da zero, anche con un'altra
dimensione.

## Pagina e app

`https://wkisai95.github.io/solid-eureka/`, servita da GitHub Pages dal ramo `data`
(Settings → Pages → Deploy from a branch → `data` / root). L'app Android **Monitor sAERO** carica la stessa pagina
dal ramo `data`, quindi non va aggiornata quando cambia la pagina.

Nella sezione «Le tue posizioni» scrivi i numeri delle tue sAERO: restano salvati solo nel dispositivo.

«Raccogli i dati adesso» avvia subito un giro. La prima volta chiede un token GitHub *fine-grained* limitato a
questo repository con il solo permesso **Actions: Read and write**; il token resta salvato solo nel dispositivo.

## Giri automatici

Il workflow parte ogni 15 minuti con lo schedule di GitHub (ai minuti 7, 22, 37 e 52). Lo schedule di GitHub è
«best effort» e sui repository nuovi a volte non parte per ore. Per avere comunque un giro ogni 15 minuti circa,
crea una volta l'ambiente di riserva:

Settings → Environments → New environment → nome `attesa` → spunta **Wait timer** → `12` minuti →
Save protection rules.

Da quel momento, se lo schedule non ha girato nell'ultima mezz'ora, ogni giro ne prepara un altro che parte dopo la
pausa dell'ambiente (senza occupare macchine durante l'attesa). Se lo schedule riprende, i giri di riserva si
fermano da soli. I giri automatici troppo vicini a un altro (meno di 8 minuti) non rifanno la raccolta.

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

Le soglie, le chain e i loro RPC pubblici si cambiano in `config.json`.

## Esecuzione manuale

Dalla pagina con «Raccogli i dati adesso», oppure da **Actions → Monitor sAERO → Run workflow**
(*Invia una notifica di prova* controlla ntfy, *Ricomincia la simulazione da zero* fa ripartire la simulazione).

In locale:

```bash
pip install -r requirements.txt
NTFY_TOPIC=... python -m monitor.main --out out
```

## Fonti

- Contratti e indirizzi: [Dromos-Labs/metadex-public](https://github.com/Dromos-Labs/metadex-public)
- Prezzi: DexScreener
- Quote: KyberSwap, Velora (ParaSwap)

Non è una consulenza finanziaria.
