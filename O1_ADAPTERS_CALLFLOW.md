# O1 Adapters — Call Flows

> **Component-interaction sequence diagrams (recommended view):** rendered offline as PNGs in
> [docs/callflow/](docs/callflow/) — they show exactly *how components talk to each other*
> (lifelines + ordered messages + protocols):
> - `docs/callflow/ocudu_interaction.png`
> - `docs/callflow/oai_interaction.png`
>
> The flowcharts below give the same information as component/data-flow graphs.

Two independent O1 adapters compared. They share the O1 *purpose* (CM/FM/PM + VES to an SMO)
but invert the NETCONF role and the direction of config flow.

- **OCUDU adapter** (`ocudu_o1_adapter`, Python, srsRAN/OCUDU) — NETCONF **client**, config flows **down** into the gNB.
- **OAI adapter** (`o1_adp_30jan`, C, OpenAirInterface) — **is** the NETCONF server, RAN state flows **up** into the datastore.

> Tip: open this file in the VSCode Markdown preview (`Ctrl+Shift+V`) to see the diagrams rendered.

---

## 1. OCUDU adapter — component / data-flow view

```mermaid
flowchart LR
    SMO["SMO / SDN-R"]
    subgraph ADP["ocudu_o1_adapter (Python, asyncio)"]
        FLASK["Flask health API\n/config-healthy /status /restarted"]
        NCM["netconf_main\n+ ConfigManager"]
        WS["ws_handler"]
        PMP["PmMetrics.run_pusher"]
        RUF["RuForwarder"]
        PTP["ptp_log_monitor\n+ health checker"]
        VES["VesMessages"]
        ALM["AlarmManager"]
        YAML["rendered config.yaml\n(Jinja2)"]
    end
    DU["OCUDU DU\nNETCONF server :830"]
    GNB["OCUDU gNB\nWebSocket :8001"]
    RU["O-RU\nM-plane NETCONF"]
    PTPLOG["ptp4l / phc2sys log"]

    SMO -- "edit config (NETCONF)" --> DU
    DU -- "get-config / notifications" --> NCM
    NCM -- "render" --> YAML
    YAML -- "shared volume" --> GNB
    NCM -- "WS cmd: ssb_set / rrm / quit" --> GNB
    GNB -- "metrics (WS)" --> WS
    WS --> PMP
    PMP -- "HTTP POST PM envelope" --> SMO
    NCM -- "forward config" --> RUF -- "edit-config" --> RU
    PTPLOG --> PTP --> ALM
    NCM & WS & RUF --> ALM
    ALM --> VES -- "VES alarm / registration" --> SMO
    GNB -- "k8s restart via health 400" --> FLASK
```

## 2. OCUDU adapter — startup + runtime sequence

```mermaid
sequenceDiagram
    autonumber
    participant Main as o1_adapter.main
    participant Flask
    participant Orch as orchestrator (asyncio.gather)
    participant NC as netconf_main / ConfigManager
    participant DU as DU NETCONF :830
    participant GNB as gNB WebSocket :8001
    participant SMO

    Main->>Flask: start_flask (daemon thread)
    Main->>Orch: asyncio.run(orchestrator)
    Orch->>NC: netconf_main()
    Orch->>GNB: ws_handler()
    Orch->>SMO: pm_metrics.run_pusher()

    NC->>DU: manager.connect() (ncclient)
    NC->>DU: create_subscription(NETCONF)
    NC->>DU: get_config(running)
    NC->>NC: write_full_config -> Jinja render YAML

    loop on netconf-config-change
        DU-->>NC: notification
        NC->>DU: get_config(running)
        NC->>NC: DeepDiff(last, new)
        alt only runtime-updatable (ssb / RRMPolicyRatio)
            NC->>GNB: WS ssb_set / rrm_policy_ratio_set
            NC->>NC: write_full_config (re-render YAML)
        else full restart needed
            NC->>GNB: WS quit
            NC->>NC: restart_req=true (health -> 400)
            NC->>NC: wait WS reconnect, then re-render
        end
    end

    GNB-->>GNB: connect WS, send metrics_subscribe
    loop metrics
        GNB-->>NC: metric payload (WS)
        NC->>NC: flatten + map to TS 28.554 KPIs
        NC->>SMO: HTTP POST PM envelope -> streamTarget
    end
```

---

## 3. OAI adapter — component / data-flow view

```mermaid
flowchart LR
    SMO["SMO / rApp"]
    subgraph STACK["o1_adp_30jan (C)"]
        NP["netopeer2 (NETCONF server :830)"]
        SR["sysrepo datastore\n(full 3GPP YANG)"]
        subgraph ADP["gnb-adapter binary"]
            MAIN["main loop (sleep 1s)"]
            NCD["netconf_data\n(sr_set_item / edit cb)"]
            RRM["rrm_policy\n(seed + telnet push)"]
            PMD["pm_data\n(15-min XML)"]
            VES["ves (libcurl)"]
            ALM["alarms"]
        end
        FTP["vsftpd /ftp"]
    end
    OAI["OAI nr-softmodem\nTelnet :9091"]

    SMO -- "get/edit config (NETCONF)" --> NP --> SR
    SR <--> NCD
    OAI -- "o1 stats (telnet poll)" --> MAIN
    MAIN -- "parse JSON, diff" --> NCD -- "sr_set_item_str" --> SR
    SMO -- "edit RRMPolicyRatio" --> NP --> NCD
    NCD -- "edit callback" --> RRM -- "telnet_update_rrm_policy" --> OAI
    RRM -- "seed 8 policies @startup" --> SR
    MAIN --> PMD -- "write measData XML" --> FTP
    FTP -. "FTP pull" .-> SMO
    PMD --> VES
    MAIN --> ALM --> VES
    VES -- "VES pnfReg / heartbeat / fileReady / alarm" --> SMO
```

## 4. OAI adapter — startup + runtime sequence

```mermaid
sequenceDiagram
    autonumber
    participant Main as main()
    participant Telnet
    participant SR as sysrepo
    participant OAI as OAI softmodem (telnet)
    participant SMO
    participant FTP as /ftp (vsftpd)

    Note over Main: init order
    Main->>Telnet: telnet_local_init + register alarm cbs
    Main->>SR: netconf_init / netconf_data_init
    Main->>SR: rrm_policy_init_sysrepo (seed 8 policies)
    Main->>SR: rrm_policy_subscribe_changes
    Main->>Main: pm_data_init / ves_init / alarms_init / oai_init

    loop main loop every 1s
        Main->>OAI: telnet "o1 stats\n"
        OAI-->>Main: JSON stats
        Main->>Main: oai_data_parse_json (cJSON)
        Main->>Main: oai_data_feed -> diff vs cached
        alt gnbId/name changed
            Main->>SR: netconf_data_update_full
        else BWP/nrcelldu changed
            Main->>SR: update_bwp_dl / update_bwp_ul / update_nrcelldu
        end
        Main->>Main: alarms_loop / pm_data_loop / ves_loop
        opt 15-min window elapsed
            Main->>FTP: write 3GPP measData XML
            Main->>SMO: VES fileReady
        end
        opt heartbeat interval
            Main->>SMO: VES heartbeat
        end
    end

    Note over SMO,OAI: closed-loop RRM control
    SMO->>SR: edit RRMPolicyRatio (NETCONF)
    SR-->>Main: netconf_data_edit_callback
    Main->>SR: fetch min/ded/max ratios
    Main->>OAI: telnet_update_rrm_policy(sst,sd,min,ded,max)
```

---

## 5. Side-by-side — the key inversion

```mermaid
flowchart TB
    subgraph OCUDU["OCUDU adapter (Python)"]
        direction TB
        S1["SMO"] -- NETCONF --> D1["DU NETCONF server"]
        D1 -- "adapter READS (ncclient client)" --> A1["adapter"]
        A1 -- "renders YAML / WS cmd DOWN" --> G1["gNB"]
        G1 -- "metrics WS UP" --> A1 -- "HTTP PM" --> S1
    end
    subgraph OAI["OAI adapter (C)"]
        direction TB
        G2["OAI softmodem"] -- "telnet stats UP" --> A2["adapter"]
        A2 -- "sr_set_item UP" --> N2["sysrepo + netopeer2 (adapter HOSTS server)"]
        N2 -- NETCONF --> S2["SMO"]
        S2 -- "edit RRM DOWN" --> N2 -- "telnet push DOWN" --> G2
    end
```

| Axis | OCUDU (Python) | OAI (C) |
|------|----------------|---------|
| NETCONF role | client (reads DU) | server (hosts netopeer2+sysrepo) |
| Config direction | NETCONF → gNB (down) | RAN → NETCONF (up); SMO edits come down |
| Southbound to RAN | WebSocket + NETCONF | Telnet (`o1 stats`) |
| Concurrency | asyncio tasks | single-thread `sleep(1)` poll loop |
| PM delivery | JSON over HTTP (stream) | 3GPP XML files over FTP + VES fileReady |
| Closed-loop control | WS `ssb_set` / `rrm_policy_ratio_set` | sysrepo edit cb → telnet RRM push |
| Extras | O-RU M-plane, PTP monitor, profiles | full 3GPP YANG, RRM seeding, VES heartbeat |
```
