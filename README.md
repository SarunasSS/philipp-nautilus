# Philipp Nautilus

NautilusTrader scaffold for backtesting experiments and live market-data/execution adapters.

## Repository Structure

```text
cli/
├── backtest/            # Run catalog-backed backtests
│   ├── __init__.py      # Shared backtest callback and runner helpers
│   ├── htf_sweep_cisd.py
│   └── subscribe.py
├── catalog.py           # Download Databento bars into the catalog
└── live/                # Run live strategies through configured data providers
    ├── __init__.py      # Shared live callback
    ├── settings.py      # Broker settings
    ├── runner.py        # Adapter registration and node lifecycle
    ├── execute.py       # Case-selected execution adapter tests
    ├── htf_sweep_cisd.py # HTF signals; optional execution instrument enables orders
    └── subscribe.py     # Generic bar subscription command
src/phillip/adapters/tradovate/
├── data.py              # Bars-only Nautilus live data client
├── execution.py         # Account lifecycle, user stream, and reconciliation
├── execution_commands.py # Order command translation
├── execution_parsing.py # Tradovate-to-Nautilus execution transforms
├── execution_reports.py # REST-backed report retrieval
├── providers.py         # Futures instrument discovery
├── http/                # REST authentication and contract metadata
└── websocket/           # Framing, heartbeat, and reconnect handling
strategies/
├── base.py              # Shared live/backtest stratlet lifecycle
├── execute.py           # ExecuteStrategy subscription plus execution tests
├── htf_sweep_cisd.py    # HTF sweep + CISD managed trade strategy
└── subscribe.py         # SubscribeStrategy requests and logs bars
configs/k8s/live/
├── base/
│   ├── strategies/      # Reusable trading workload and narrow control API
│   └── command-and-control/ # Reusable Grafana, Loki, and Alloy resources
└── overlays/alphanet/
    ├── strategies/      # Alphanet strategy configuration
    └── command-and-control/ # Alphanet dashboard ingress and logging config
configs/k8s/tws/
├── base/                # TWS paper desktop, private API/RDP, and storage
└── overlays/alphanet/   # Independent TWS instances, storage, and secret mappings
```

## Setup

This project uses `uv` for environment and dependency management.

```bash
uv venv --python 3.13
uv sync
```

Tradovate authenticates using `TRADOVATE_USERNAME`, `TRADOVATE_PASSWORD`, the key name (`TRADOVATE_APP_ID`), key ID (`TRADOVATE_CID`), and key secret (`TRADOVATE_SEC`) as one complete bundle. `TRADOVATE_APP_VERSION` defaults to `1.0`; `TRADOVATE_DEVICE_ID` is optional. The adapter requests and renews its own session tokens; externally supplied tokens are not accepted. Keep credentials out of source control and logs.

Live startup registers every configured data adapter. There is no provider selector: each strategy selects its instruments with the correct venue.

| Adapter | Data client | Execution client | Example instrument |
| --- | --- | --- | --- |
| Databento | `DATABENTO_API_KEY` | None | `NQZ6.GLBX` |
| Interactive Brokers | Explicit `IB_HOST`; port/client ID default to `7497`/`101` | Also set `IB_ACCOUNT_ID` | `NQZ6.CME` |
| Tradovate | Complete username/password and API-key bundle described above | Registered with the same credentials | `NQZ6.TRADOVATE` |

IB login belongs to TWS, which receives `IB_USERNAME` and `IB_PASSWORD`; the trading process needs its socket endpoint, not a second copy of those credentials. With `IB_HOST` alone, IB supplies data. Adding `IB_ACCOUNT_ID` enables native IB execution. Complete Tradovate credentials register both data and execution clients; absent credentials register neither. Missing Tradovate credentials do not block Databento-only or IB-only runs. Partial credential bundles fail validation.

All configured adapters connect even when they have no instruments to preload, so configure usable credentials/endpoints. Each broker's shared provider loads only its own data/execution instruments. A strategy that requests an execution instrument must have a matching execution adapter for that venue; otherwise startup fails before connecting. Registering an execution client does not itself place orders: `subscribe` never executes, and HTF without an execution instrument is signal-only.
Live settings and options use explicit broker prefixes: `tradovate_username` / `--tradovate-username`, `tradovate_password` / `--tradovate-password`, `tradovate_environment` / `--tradovate-environment`, and `ib_host` / `--ib-host`. Existing `TRADOVATE_*` environment variables retain their names; IB socket environment variables now use `IB_*`.
Set `TRADOVATE_ACCOUNT_ID` when the credentials expose more than one open account. The execution client deliberately refuses to guess which account should receive commands.

### Interactive Brokers

The project includes Nautilus 1.228's native IB data and execution adapters. TWS namespace/access provisioning belongs to the `infra` CLI and the `infra-context` runbook, not this repository's application manifests. After infrastructure provisioning, deploy the per-instance TWS resources independently:

```bash
kubectl --context=sarunas-mac@alphanet apply -k configs/k8s/tws/overlays/alphanet
kubectl --context=sarunas-mac@alphanet -n philipp-tws get deployment,externalsecret,pvc
```

Instance `tws-1` is deliberately stopped (`replicas: 0`); applying the manifests does not start it. Google Secret Manager project `oned-works` contains these entries, with Philipp's Viewer and Secret Accessor grants on all six:

| Credential | Instance 1 — populated | Instance 2 — empty, no versions |
| --- | --- | --- |
| IB username | `philipp-tws-1-username` | `philipp-tws-2-username` |
| IB password | `philipp-tws-1-password` | `philipp-tws-2-password` |
| RDP password | `philipp-tws-1-rdp-password` | `philipp-tws-2-rdp-password` |

Instance 2 currently has only secret containers; its Kubernetes workload has not been added. Once instance 1 is deliberately started and Ready, open a local tunnel:

```bash
kubectl --context=sarunas-mac@alphanet -n philipp-tws \
  port-forward --address=127.0.0.1 svc/tws-1 13389:3389 7497:7497
```

Connect an RDP client to `127.0.0.1:13389` as `abc`, using the RDP secret, and complete paper login. A local IB data-only run needs no Tradovate credentials or IB account ID:

```bash
uv run python main.py live \
  --ib-host 127.0.0.1 --ib-port 7497 --ib-client-id 101 \
  subscribe run --bar-type NQZ6.CME-1-MINUTE-LAST-EXTERNAL
```

This requests real-time, all-session data for the dated December 2026 NQ contract. Use the actual contract being traded; `MNQZ6.CME` is the micro contract. Login credentials, CME API entitlements, and any IBKR two-factor authentication must be completed before live verification.

To enable IB execution, also supply `--ib-account-id` / `IB_ACCOUNT_ID` with the intended IB account, then select an execution instrument such as `NQZ6.CME`. TWS must allow API orders and the account must match its authenticated session. No account ID is required for data-only access.

The custom IBKR credential CLI was removed. Use Secret Manager for login credentials. See the [TWS deployment runbook](configs/k8s/tws/overlays/alphanet/README.md) for instance configuration, infrastructure commands, and verification. The subscriber currently selects a `.GLBX` instrument. To use TWS, set `IB_HOST=tws-1.philipp-tws.svc.cluster.local` after authenticating TWS and select an IB venue such as `.CME` in the source ConfigMap.

For an RDP-only local test, omit `7497:7497` from the port-forward command, keep it running, then launch `open -a "Microsoft Remote Desktop"` on this Mac (Windows App on newer installations). Add PC `127.0.0.1:13389`, user `abc`, with no Remote Desktop Gateway. Connect to the Alphanet WireGuard network first. Philipp can use his existing kubeconfig with `-n philipp-tws`; omit this Mac's explicit context. A stopped instance cannot accept port-forwards.

Each TWS instance has editable ConfigMaps for managed settings and one PVC for logs. TWS reads the literal `TimeZone=Etc/UTC` in `jts.ini`; `TZ=Etc/UTC` in `tws.env` sets Linux/desktop time. The init container copies the managed INI file into writable storage with mode `0600` when the pod is created. Service/container restarts preserve TWS's changes; pod recreation reapplies the managed baseline. The configured restart is 23:59 UTC.

`configMapGenerator` produces ordinary ConfigMaps from the settings/script files. It adds a content hash to their names and updates Deployment references, so applying a changed source restarts a running instance. Explicit ConfigMap YAML is also valid; stable names would require a manual restart or pod-template checksum for these startup settings. See [Kustomize's generator documentation](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/#generating-resources).

TWS writes native diagnostic logs beside generated session files, so its working directory remains writable on the log PVC. Managed settings live in ConfigMaps; generated TWS state also persists alongside logs. The small `start-session.sh` wrapper only tees service stdout/stderr to the PVC and runs the original image launcher; it does not change settings. Console output and `/var/log` use the same PVC, while desktop scratch data, `/tmp`, and `/dev/shm` are ephemeral. See the runbook for exact paths and retention limits. Add independent instances under `configs/k8s/tws/overlays/alphanet/instances/<i>` with separate credentials, Services, selectors, and PVCs; keep one replica per authenticated session.

## Run

Download NQ front-month continuous 1-minute bars from Databento into the local catalog:

```bash
uv run python main.py catalog bars download --start 2026-01-01 --end 2026-06-01
```

Run the subscribe-bar backtest over the first half of 2026:

```bash
uv run python main.py backtest \
  --start 2026-01-01 \
  --end 2026-06-01 \
  subscribe run
```

Run the HTF sweep + CISD strategy. It logs `Short_signal` / `long_signal` when the CISD condition confirms, then manages the entry, stop-loss, and take-profit lifecycle:

```bash
uv run python main.py backtest \
  --start 2026-01-01 \
  --end 2026-06-01 \
  htf-sweep-cisd run \
  --htf-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  --ltf-bar-type NQ.c.0.GLBX-1-MINUTE-LAST-EXTERNAL \
  --entry-order-type LIMIT \
  --entry-limit-offset 0.0 \
  --trade-notional 1000000 \
  --stop-order-type MARKET \
  --stop-loss-distance-ratio 1.0
```

`--entry-limit-offset` and `--stop-limit-offset` are direct ratios, so `0.001` means `0.1%`. `--trade-notional` defaults to `1000`; the NQ example above uses `1000000` so contract sizing produces filled futures orders in the local backtest. Signal cooldown defaults to one HTF period and can be overridden with `--signal-cooldown-seconds`. The HTF sweep backtest uses hedging mode so concurrent entries retain separate positions.

Each backtest exports Nautilus order, order-fill, fill, position, and account reports as strategy-prefixed CSV files under `data/results/`. It also creates two interactive, self-contained HTML files:

- `<strategy>-tearsheet.html` contains run information, performance statistics, equity, drawdown, periodic returns, return distribution, and rolling Sharpe charts.
- `<strategy>-bars-with-fills.html` contains candlesticks with buy and sell fill markers.

The bars-with-fills chart uses the selected catalog bar type and retains its latest 10,000 bars by default. Set `--chart-bar-type` to chart another bar type cached during the run, such as the HTF composite bars produced by the HTF Sweep/CISD strategy. Composite inputs are resolved to the standard bar type under which Nautilus caches the generated bars. Set `--chart-bar-limit` to review a larger or smaller window, or pass `--no-visualize` to skip HTML generation:

```bash
uv run python main.py backtest \
  --start 2026-01-01 \
  --end 2026-06-01 \
  --chart-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  --chart-bar-limit 150000 \
  htf-sweep-cisd run \
  --htf-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  --ltf-bar-type NQ.c.0.GLBX-1-MINUTE-LAST-EXTERNAL \
  --trade-notional 1000000
```

Run the same generic subscribe strategy against live Tradovate bars:

```bash
uv run python main.py live \
  --tradovate-environment demo \
  subscribe run \
  --bar-type NQU6.TRADOVATE-1-MINUTE-LAST-EXTERNAL
```

The live adapter currently supports external LAST bars only. The example contract expires, so replace `NQU6` with a currently listed contract when necessary. Tradovate API-key **Market Data: Read Only** permission and an ordinary display-data subscription do not by themselves prove that CME non-display API data is enabled; `Symbol is inaccessible` for valid CME symbols must be resolved with Tradovate support.

Run an active execution-adapter test using Databento bars and a separate Tradovate execution instrument:

```bash
uv run python main.py live \
  --tradovate-environment demo \
  execute run \
  --bar-type MNQU6.GLBX-1-MINUTE-LAST-EXTERNAL \
  --instrument-id MNQU6.TRADOVATE \
  --case entry-exit \
  --quantity 1
```

This command subscribes to Databento bars, submits a Tradovate market buy, waits for its fill, and then submits a Tradovate market sell for the filled quantity. Omit `--instrument-id` to trade the instrument contained in `--bar-type`. It places real orders in the selected Tradovate environment. Wait for the `completed` log before stopping the node; an interruption between fills requires checking and flattening the account manually. The currently supported case is `entry-exit`.

Databento replaces only the market-data source. A Tradovate `401 Access is denied` response from `/order/placeorder` still means the authenticated Tradovate API user does not have permission to submit that order; verify order-write access and the selected demo account in the API-key configuration.

Run the generic subscribe strategy through Nautilus's built-in Databento adapter. The strategy first requests the instrument definition, then subscribes to its live one-minute bars:

```bash
uv run python main.py live \
  subscribe run \
  --bar-type MNQU6.GLBX-1-MINUTE-LAST-EXTERNAL
```

Nautilus receives explicit venue routes for the strategy instruments: dataset venues such as `GLBX` use Databento, IB futures exchange venues such as `CME` use TWS, and `TRADOVATE` uses the custom adapter. The Databento client retains the dataset venue so IDs match the catalog. Replace dated contracts at rollover. Internally aggregated bars are not emitted when no source updates arrive; a timer-generated timestamp alone does not prove a fresh feed.

At live-node startup, the CLI derives the selected instrument ID from `--bar-type` and preloads its latest exact-match definition from Databento's recent historical range. This makes price precision available before the strategy subscribes and also works while the current session is empty, such as on weekends. The subscribe strategy uses the preloaded cache directly and falls back to `request_instrument()` only when no cached definition exists.

The custom provider addresses the observed weekend startup failure in pinned Nautilus 1.228: the standard provider requests live definition replay and can wait indefinitely when no first definition arrives. The replacement requests only the selected contracts over seven days ending at historical availability, keeps the newest definition for each exact ID, and fails explicitly if any are missing. It adds a historical metadata request; it does not replace the live market-data feed or prove that fresh bars are available. See [upstream provider implementation](https://github.com/nautechsystems/nautilus_trader/blob/v1.228.0/nautilus_trader/adapters/databento/providers.py).

Tradovate factories construct and cache one instrument provider for matching client/configuration inputs. Loading occurs asynchronously in each client's `_connect()` through `await provider.initialize()`, before instruments are published to the node cache. Nautilus serializes initialization with a lock and skips loading once complete, so data and execution clients can share it without duplicate loads. Synchronous factory construction and cache lookup perform no instrument-loading I/O.

The Databento API key must have a live `GLBX.MDP3` license. Historical access alone is
not sufficient: Nautilus can resolve the delayed historical instrument definition and
log `Subscribed bars`, while the live gateway still sends no records. Databento's
official client reports this state explicitly as
`A live data license is required to access GLBX.MDP3`.

Run HTF sweep + CISD with Databento bars and a separate Tradovate execution
instrument:

```bash
uv run python main.py live \
  --tradovate-environment demo \
  htf-sweep-cisd run \
  --htf-bar-type MNQZ6.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  --ltf-bar-type MNQZ6.GLBX-1-MINUTE-LAST-EXTERNAL \
  --execution-instrument-id MNQZ6.TRADOVATE \
  --entry-order-type MARKET \
  --trade-quantity 1
```

The bar and execution instruments must use the same symbol. The requested execution venue must have a configured adapter: `.TRADOVATE` requires Tradovate credentials, while `.CME` requires `IB_HOST` and `IB_ACCOUNT_ID`. No broker is required solely because the run is live.

For an HTF live run that only logs signals, leave `EXECUTION_INSTRUMENT_ID` unset (or empty in the dashboard) and omit the flag:

```bash
uv run python main.py live htf-sweep-cisd run \
  --htf-bar-type MNQZ6.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  --ltf-bar-type MNQZ6.GLBX-1-MINUTE-LAST-EXTERNAL
```

This needs only the configured data provider. It produces signals without creating orders or trade instances, even if an execution adapter is registered. Backtests keep their execution behavior.
Executing live runs use an explicit whole-contract quantity so the requested
minimum remains one MNQ contract as the futures price changes.
Live limit entries use a 15-minute GTD expiry by default; change it with
`--entry-order-expire-minutes`. Executing live strategies skip new entries while one managed trade is active, including for Tradovate's netted positions. Use an
account where no other process trades the same instrument. After execution
reconciliation, startup refuses any pre-existing open order or position for
the execution instrument rather than adopting or flattening unknown exposure.
The strategy's stop-loss and take-profit are independent orders, with sibling cancellation managed by the strategy. For Tradovate, the adapter does not transmit Nautilus `reduce_only`; after
one exit fills, the strategy detects it and cancels the sibling. Configure exit
levels with enough separation for that application-side cancellation model.

## Alphanet deployment

Live strategies and command/control use `philipp-trading-dev`. Their independently renderable bases and overlays are under `configs/k8s/live/{base,overlays/alphanet}/{strategies,command-and-control}`. TWS is independent under `configs/k8s/tws/{base,overlays/alphanet}` in `philipp-tws`; its root desktop startup requires a different pod policy from the restricted trading namespace. `infra` provisions that namespace and access through CLI commands documented in `infra-context`. The application overlay contains no Namespace or RBAC resources.

HTF sweep + CISD is disabled with `replicas: 0`. The generic subscriber remains at one replica. Its single source of configuration is `configs/k8s/live/base/strategies/subscribe-config.yaml`, currently `MNQZ6.GLBX-5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL`. HTF defaults are in the neighboring `htf-sweep-cisd-config.yaml`. Change these source ConfigMaps to retain settings across deployments.

Deployment arguments contain only the command path, such as `live subscribe run`. Typer reads runtime values from environment variables populated by ConfigMaps and Secrets, including `TRADER_ID`, `LOG_LEVEL`, `BAR_TYPE`, and the HTF parameters. Command-line flags remain available for manual runs. The same value is no longer repeated in both `env` and `args`.

Alphanet patches contain only actual environment differences: registry pull credentials, published image names, ingress, and storage. Named strategic merge YAML identifies its target by kind/name; duplicate ConfigMap overrides and inline `patches.target` operations were removed. The one-time unprefixed migration completed in August, so its deployment overlay was removed. The old `philipp-loki-data` claim remains a separate rollback backup; this cleanup does not delete it.

### Application image

The three application containers and the local image alias are named `philipp-trading-dev`. The Alphanet strategy overlay replaces `image: philipp-trading-dev:latest` with `europe-west1-docker.pkg.dev/oned-works/oned/philipp-trading-dev:latest`. This is built from this repository's `Dockerfile`, which installs the Python package and NautilusTrader dependencies. The TWS desktop uses the separate upstream `ghcr.io/gnzsnz/tws-rdesktop:stable` image.

Render the overlay before applying; base YAML alone retains the local image alias. Publish a compatible application image before applying manifest changes that depend on new CLI/environment handling. `latest` is floating and uses `imagePullPolicy: Always`; record the resolved digest after rollout.

### Dashboard, logs, and controls

Grafana is exposed at [the dev dashboard](https://dashboard.dev.philipp-trading.apps.1d.works) through `nginx`, with a `letsencrypt-prod` certificate. Namespace-scoped Alloy collects logs/events and sends them to Loki. Loki has a 2 GiB ReadWriteOnce PVC and seven-day retention; storage limits can be reached before retention catches up.

Grafana's database is ephemeral. Git provisions the dashboards/data source, and the bootstrap companion recreates the managed `philipp` Viewer account after a pod replacement. UI-created dashboards/users do not survive replacement unless provisioned. Rotate the managed Viewer password by adding a Secret Manager version and rolling Grafana.

The `Strategies / Strategy control` dashboard combines runtime configuration, rollout readiness, error/activity statistics, and logs. Business Forms sends requests through the server-side Infinity data source to the internal FastAPI control service. The bearer token stays server-side. A registry allowlists each ConfigMap/Deployment and its editable fields; `resourceVersion` rejects stale form updates. Saving updates the mapped ConfigMap and annotates that Deployment to restart it. There is no scaling endpoint, so editing HTF configuration does not activate its zero-replica workload.

The controller's Role permits get/update only on listed ConfigMaps and get/patch only on listed Deployments. Its NetworkPolicy admits the API only from Grafana. TWS has its own NetworkPolicy because its API socket has no per-client authentication: only designated strategy pods can connect, and RDP uses an authorized port-forward. Anyone allowed to create/relabel pods within the permitted scope remains trusted.

Any signed-in Grafana user who can access the control dashboard can submit its form; per-user control authorization is not implemented. A direct ConfigMap edit does not restart the process. Applying the strategy Kustomization restores source values, so copy long-lived dashboard changes back into the source ConfigMap.

To add a strategy:

1. Create its ConfigMap, Deployment, and stable `app.kubernetes.io/name` label.
2. Add its schema to `configs/k8s/live/base/strategies/config/strategy-registry.json`.
3. Add those exact resource names to the controller Role.
4. Render both stacks and verify selector, status, logs, and a no-op form update.

The error counter includes ERROR/CRITICAL lines, tracebacks, fatal errors, and panics. Warnings are excluded. Browser/form failures are outside strategy logs. Pod readiness and internally generated bars do not establish broker connectivity or fresh data: verify upstream timestamps and updates. The September 10 check found unchanged zero-volume subscriber bars, with the last positive-volume bar on September 3; changing the adapter registration does not itself prove the source issue is resolved.

If multiple log streams report `failed to create fsnotify watcher: too many open files` while process descriptor counts are low, inspect node `fs.inotify.max_user_instances`; repair that host limit rather than filtering logs. CPU/memory/restart metrics require a separate metrics stack; current control statistics come from Kubernetes rollout status and Loki.

### Secrets and broker boundaries

`trading-env` holds only `DATABENTO_API_KEY`; `tradovate-env` belongs to the stopped HTF strategy. Missing Tradovate secrets do not prevent the subscriber's data credential from synchronizing. IB login remains in TWS; set a strategy's `IB_HOST=tws-1.philipp-tws.svc.cluster.local` only after TWS is authenticated, and set `IB_ACCOUNT_ID` only when IB execution is wanted. The subscriber/HTF pod labels permit that private API connection but do not enable an adapter by themselves.

Command/control uses these source secrets in GCP project `oned-works`:

- `philipp-trading-dev-grafana-admin-password`
- `philipp-trading-dev-grafana-philipp-password`
- `philipp-trading-dev-strategy-control-token`

The seven Tradovate entries have prefix `philipp-trading-dev-tradovate-` and suffixes `username`, `password`, `app-id`, `cid`, `sec`, `app-version`, and `device-id`. ExternalSecrets references are in `configs/k8s/live/base/strategies/tradovate-external-secret.yaml`. This dedicated account leaves `TRADOVATE_ACCOUNT_ID` unset; startup accepts exactly one open account and rejects ambiguity.

Before any future HTF activation, verify its instrument preload, reconciled flat account, fresh data, and compatible image. The strategy's independent stop/take-profit orders and application-side sibling cancellation remain the accepted execution model. The TWS [runbook](configs/k8s/tws/overlays/alphanet/README.md) lists its separate login and RDP secrets and desktop-access procedure.

### Render, deploy, and inspect

```bash
kubectl kustomize configs/k8s/live/overlays/alphanet/strategies
kubectl kustomize configs/k8s/live/overlays/alphanet/command-and-control
kubectl kustomize configs/k8s/tws/overlays/alphanet

kubectl --context=sarunas-mac@alphanet apply -k configs/k8s/live/overlays/alphanet
kubectl --context=sarunas-mac@alphanet apply -k configs/k8s/tws/overlays/alphanet
kubectl --context=sarunas-mac@alphanet -n philipp-trading-dev get pvc,externalsecret,pods,ingress,certificate
```

The parent live overlay deploys both strategy and command/control stacks; either child can be applied independently. TWS is always a separate apply. After changes, verify secret synchronization, storage, TLS, workload readiness, and source-data freshness.

Expected RBAC boundaries: Alloy can list pods in `philipp-trading-dev`, not `default`; the strategy controller can update `subscribe-config` and patch `subscribe`, not arbitrary resources. Through `infra`, Philipp's existing `user-philipp` identity has view/log access and `pods/portforward` creation in `philipp-tws`. Kubernetes Secret reads, pod exec, workload modification, and scaling remain unavailable. Secret Manager separately grants `philipplieske33@gmail.com` read access to all three `philipp-tws-1-*` credentials.
