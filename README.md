# Radar sAERO

Monitor di Aero che gira da solo su GitHub Actions ogni 15 minuti.

Ogni giro:

- legge dalla chain Base gli eventi dei contratti Aero: allocazioni delle posizioni sAERO, cooldown, fee girate agli allocatori e nuovi programmi di incentivi;
- legge l'allocazione totale di ogni pool e com'era 1, 6 e 24 ore fa;
- una volta all'ora chiede a KyberSwap e Velora una quotazione da $1.000 e $10.000 sui pool più allocati e misura quanta parte passa dal pool Aero (quota di routing);
- calcola il calendario dei cooldown: quante sAERO escono dal cooldown ora per ora e da quali pool;
- pubblica i dati sul ramo `data` e manda le notifiche sull'app **ntfy**.

## Pagina

`https://wkisai95.github.io/solid-eureka/`, servita da GitHub Pages dal ramo `data`
(Settings → Pages → Deploy from a branch → `data` / root).

Nella sezione «Le tue posizioni» scrivi i numeri delle tue sAERO: restano salvati solo nel browser.

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
| Posizione libera | Finisce il cooldown di una delle tue posizioni (`MY_POSITIONS`) |
| Ondata di cooldown | Nelle prossime `wave_window_h` ore si liberano almeno `wave_min_saero` sAERO di posizioni attive |
| Incentivi in arrivo | Nuovo programma da almeno `incentive_min_usd` dollari |
| Flusso di allocazione | Un pool guadagna o perde almeno `flow_min_pct`% e `flow_min_saero` sAERO in 6 ore |
| Routing | La quota di routing su $10.000 cambia di almeno `routing_change_pts` punti in 24 ore |
| Rendimento anomalo | Un pool sopra `apr_alert_min_alloc` rende almeno `apr_alert_multiple` volte la mediana |
| Parametri del protocollo | Cambia la durata del cooldown o si attivano le riduzioni del cooldown |
| Riepilogo | Ogni mattina dopo le `digest_hour` |

Le soglie si cambiano in `config.json`.

## Esecuzione manuale

Da **Actions → Radar sAERO monitor → Run workflow**. Spunta *Invia una notifica di prova* per controllare ntfy.

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
