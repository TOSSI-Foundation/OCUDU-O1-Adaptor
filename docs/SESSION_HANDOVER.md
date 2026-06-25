# OCUDU ↔ rApp (ransliceassurance) — Session Handover

> Purpose: full context for a fresh chat to continue the OCUDU O1 + SDNR RRM-closed-loop work
> without re-deriving anything. Read this top to bottom. Companion docs:
> `docs/PHASE3_SDNR_RRM_CLOSED_LOOP.md` (the SDNR/YANG fix in depth) and
> `O1_ADAPTERS_CALLFLOW.md` (component call flows). Date of this handover: 2026-06-21.

---

## 0. TL;DR — where we are right now

The goal is a **RAN slice-assurance closed loop**: the `ransliceassurance` rApp watches per-slice
throughput and, when a slice exceeds a threshold, changes that slice's PRB allocation on the
OCUDU (srsRAN) gNB — driven **through SDNR (Option A)**, not direct NETCONF (Option D).

**Proven & done:**
- ✅ Full SDNR RRM loop proven with a **synthetic** PM (rApp decision → SDNR RESTCONF PATCH of
  `RRMPolicyRatio` → `ocudu_netconf` → adapter `rrm_policy_ratio_set` → gNB).
- ✅ **Real** per-slice PM pipeline confirmed flowing (gNB tags S-NSSAI → adapter aggregates →
  rApp ingests).
- ✅ Code committed: adapter `73c7641` (branch `main`), rApp `435fe65` (branch `ocudu-support`).
- ✅ **Data-plane (user-plane) blocker FIXED** this session — see §6. The single most important
  fix: `cu_cp.amf.bind_addrs: 0.0.0.0 → 192.168.70.129` in the gNB config.
- ✅ Multi-slice: new multi-slice core up, gNB advertises `sd=1`/`sd=2`, `sd=1` UE attaches +
  gets a PDU session + IP + **data forwards both ways**.
- ✅ **REAL-TRAFFIC CLOSED LOOP FIRED (end-to-end, incl. the RAN).** A UDP DL flood gave slice
  `sd=1` ~17 Mbps → adapter per-slice PM → rApp `17 > threshold 1.0` → SDNR PATCH
  `rRMPolicyDedicatedRatio 15→25` → adapter `Updating RRM policy` → gNB
  `DU reconfiguration procedure for RRM policy update completed` + `Slice Reconfig: cell=0`.
  Then the rApp correctly holds at `already elevated (25.00)`. **Important nuance:** UE throughput
  did NOT increase — `dedicated ratio` is a PRB *reservation* that only matters under contention;
  with one slice + one UE there's nothing to contend, so no visible throughput shift. To *show* a
  throughput effect you need a **2-slice contended demo** (`sd=1`+`sd=2` both loaded), which needs
  the adapter multi-policy enhancement.

**Remaining (all optional now — the loop is proven):** see §7. (1) Wire the rApp threshold to
config + revert the demo value (`1.0`); (2) commit the uncommitted changes (gNB `config_o1.yaml`
incl. the `bind_addrs` fix; rApp threshold change); (3) adapter multi-policy (render
`RRMPolicyRatio` as a list) → enables the 2-slice contention demo; (4) SDNR durability fix.

---

## 1. Topology / VMs (SSH password is `ubuntu` for all three)

| VM | Role | Runs |
|----|------|------|
| **192.168.4.50** | OCUDU stack (we operate here; this doc's commands run on 4.50) | srsRAN `gnb` (ZMQ), Python `o1_adapter`, `ocudu-netconf` container, OAI 5G core (docker), OAI UE `nr-uesoftmodem` |
| **192.168.4.51** | OAI **reference** stack | OAI gNB + its own `o1_adapter` (the *consistent* YANG bundle we used as a control to prove the SDNR YANG issue) |
| **192.168.4.52** | SMO / rApp | ONAP **SDNR** (`onap-sdnc-0`, OpenDaylight, RESTCONF NodePort **30267**); the `ransliceassurance` rApp (k8s pod, namespace `nonrtric`) |

Reach 4.51/4.52 from 4.50 with: `sshpass -p ubuntu ssh -o StrictHostKeyChecking=no ubuntu@192.168.4.5X`.

---

## 2. Directories & key files

### On 4.50 (OCUDU host)
- **O1 adapter:** `/home/ubuntu/sushant/ocudu_o1_adapter` — branch `main`, our commit `73c7641`.
  - `src/config_manager.py` — renders gNB config from netconf; parses `RRMPolicyRatio`,
    `PerfMetricJob`; **`_extract_perf_metric_jobs`** now adds the DU DN (`me_id`/`gnbdu_id`/
    `nrcelldu_id`). `_extract_rrm_policy_ratio_config` / `_extract_cell_config` are STILL
    single-policy/single-member (the multi-policy TODO).
  - `src/pm_metrics.py` — gNB WS metrics → per-slice aggregation (`_slice_throughput`, dynamic for
    N slices) → envelope (`_build_envelope`, emits `meId`/`gnbduId`/`nrCellDuId` + `sst`/`sd`) →
    HTTP POST to `streamTarget`.
  - `src/remote_commands.py` — WS commands to gNB: `rrm_policy_ratio_set`, `ssb_set`, etc.
  - `src/o1_adapter.py` — entrypoint. `docs/` has this handover + the SDNR doc + call-flows.
- **RAN (srsRAN/OCUDU):** `/home/ubuntu/sushant/ocudu` — branch `dev`, **root-owned** (use `sudo`),
  gNB commit `f96df2fb1c`.
  - Binary + cwd: `/home/ubuntu/sushant/ocudu/build/apps/gnb/gnb` (it logs to **`gnb.log` in that
    cwd**, NOT stdout; `all_level: debug` so it's huge).
  - Config the gNB actually uses: **`/home/ubuntu/sushant/ocudu/configs/config_o1.yaml`** (an O1
    copy; the user's original `config.yaml` is left untouched). Backups I made:
    `config_o1.yaml.bak.preslice`, `.bak.pretuning`.
  - Phase-2 RAN patch (per-UE S-NSSAI in scheduler metrics) is built into this gNB — the adapter
    relies on it for per-slice PM.
- **ocudu_netconf:** `/home/ubuntu/sushant/ocudu_netconf`. Container name **`ocudu-netconf`**
  (netopeer2 **2.8.2** + sysrepo **4.5.4** + libyang/yanglint **5.4.9**). NETCONF on
  `localhost:830`, user/pass **`root`/`root`**. Served YANG files: `/etc/sysrepo/yang/` inside the
  container (this is what `get-schema` returns to ODL). **Restarting/recreating this container
  re-seeds the datastore** (see §8 gotchas).
- **OAI cores (two!):**
  - `/home/ubuntu/sushant/oai-cn5g` — the OLD single-slice core.
  - **`/home/ubuntu/oai-cn5g`** — the **NEW multi-slice core** (THE one to use). Compose:
    `/home/ubuntu/oai-cn5g/docker-compose.yaml`. Has 4× SMF (`oai-smf`..`oai-smf4`) + 4× UPF
    (`oai-upf`..`oai-upf4`) + amf/ausf/udm/udr/nrf/mysql/ext-dn (15 containers). AMF at
    `192.168.70.132`, UPF (sd=1) at `192.168.70.134`, ext-dn at `192.168.70.135`. The host's IP on
    this docker net (`oai-cn5g` bridge) is **`192.168.70.129`**.
- **OAI RAN/UE source:** `/home/ubuntu/sushant/openairinterface5g`. UE binary:
  `cmake_targets/nr-uesoftmodem`. UE cap file: `targets/PROJECTS/GENERIC-NR-5GC/CONF/uecap_ports1.xml`.
- **Python venv (ncclient etc.):** `/home/ubuntu/sushant/o1_venv/bin/python`.
- **Backups:** `/home/ubuntu/ocudu_netconf_backup/` (modules_before.txt, running_before.xml).

### On 4.52 (SMO/rApp)
- **rApp:** `/home/ubuntu/shivam/ransliceassurance` — branch `ocudu-support`, our commit `435fe65`.
  - Active rApp = **`newrapp/`**: `main.go`, `pm_ingest.go` (`/pm` HTTP handler), `sdnr/client.go`
    (the 3GPP SDNR client — rewritten by us), `policy/manager.go` (decision logic + threshold),
    `types/types.go`, `config/loader.go`, **`config/real_env_config.yaml`** (the live config; its
    `node_id` and `thresholds` matter), `k8s-deployment.yaml`, and the prebuilt binary
    `ransliceassurance`.
  - `icsversion/` is the OLD upstream O-RAN-SC version (hello-world YANG model) — reference only.
- **Go:** `1.23.2`, available **in the login shell** on 4.52 (`bash -lc 'go version'`). None on 4.50.
- **SDNR / ODL:** RESTCONF base `http://192.168.4.52:30267/rests`, auth
  **`admin` / `Kp8bJ4SXszM0WXlhak3eHlcse2gAw84vaoGGmJvUy2U`**. Pod:
  `sudo kubectl -n onap onap-sdnc-0 -c sdnc`. ODL schema cache: `/opt/opendaylight/cache/schema/`.
  ODL version 5.0.7 (sal-netconf-connector).
- **rApp pod:** namespace `nonrtric`, `deployment/ransliceassurance`. Service is NodePort
  `8080:30888`. Pod runs `alpine:latest` with a **hostPath** mount of the `newrapp/` dir at `/app`
  and execs `./ransliceassurance`. We added **`hostPort: 8080`** so the adapter's PM (which targets
  `4.52:8080`) reaches the live pod.

---

## 3. The architecture & the closed loop

```
   ── PM (UP direction) ──────────────────────────────────────────────
   gNB --WS metrics(:8001)--> o1_adapter --HTTP POST /pm--> rApp(:8080)
        (per-UE S-NSSAI)        (per-slice aggregate)        (decide)

   ── RRM / CM (DOWN direction) ──────────────────────────────────────
   rApp --RESTCONF PATCH RRMPolicyRatio--> SDNR/ODL(:30267)
        --NETCONF edit-config--> ocudu_netconf(:830)
        --netconf-config-change--> o1_adapter
        --WS rrm_policy_ratio_set(:8001)--> gNB (re-allocates slice PRBs)
```

- **Option A = SDNR** (what we built/use). **Option D** = rApp speaking NETCONF straight to the
  device (not used).
- The single knob is the standard 3GPP **`RRMPolicyRatio`** (`_3gpp-nr-nrm-rrmpolicy`), which
  augments `NRCellDU`. Its `rRMPolicyDedicatedRatio`/`Min`/`Max` set the PRB share for a slice
  (S-NSSAI) identified by `rRMPolicyMemberList`.
- The device DN is `ManagedElement=ran1 / GNBDUFunction=du1 / NRCellDU=nrcelldu1 /
  RRMPolicyRatio=rrm_policy1`. The SDNR mount node is **`ocudu-gnb`** (host 192.168.4.50, port 830).

### RRMPolicyRatio RESTCONF URL (copy-paste; run from 4.52)
```
http://192.168.4.52:30267/rests/data/network-topology:network-topology/topology=topology-netconf/node=ocudu-gnb/yang-ext:mount/_3gpp-common-managed-element:ManagedElement=ran1/_3gpp-nr-nrm-gnbdufunction:GNBDUFunction=du1/_3gpp-nr-nrm-nrcelldu:NRCellDU=nrcelldu1/_3gpp-nr-nrm-rrmpolicy:RRMPolicyRatio=rrm_policy1
```
GET it directly; to LIST policies the rApp GETs the **NRCellDU parent** (ODL rejects a keyless list
GET), i.e. drop the trailing `/RRMPolicyRatio=...`.

---

## 4. What we changed (the diffs)

### Adapter (commit `73c7641`, files `config_manager.py` + `pm_metrics.py`)
Carry the 3GPP **cell DN** in the PM envelope so the rApp can address `RRMPolicyRatio` over SDNR
without hardcoding/discovery. `_extract_perf_metric_jobs` sources the **DU** DN (`ran1/du1/
nrcelldu1`) even when the `PerfMetricJob` sits under `GNBCUCPFunction` (per-slice throughput is a
DU metric, and `RRMPolicyRatio` lives under `NRCellDU`); `_build_envelope` emits
`meId`/`gnbduId`/`nrCellDuId` in `measuredObject`.

### rApp (commit `435fe65`, branch `ocudu-support`)
- `sdnr/client.go` — **rewritten** from the O-RAN-SC hello-world model to the 3GPP model: GET the
  `NRCellDU` parent and read its `RRMPolicyRatio` children, **match the policy by S-NSSAI**
  (`sst`/`sd`), and **PATCH** `rRMPolicyDedicatedRatio` with `Content-Type: application/yang-data+json`
  (the exact shape proven to return HTTP 200). DN passed in via a `DN{MeId,GnbduId,NrCellDuId}` struct.
- `pm_ingest.go` / `types.go` — capture `meId`/`gnbduId`/`nrCellDuId` + S-NSSAI from the PM envelope.
- `policy/manager.go` — thread the DN + `sst`/`sd` into the SDNR read/write.
- `main.go` — `discoverPolicies` is now a no-op (discovery is PM-driven; DN carried per-PM).
- `config/real_env_config.yaml` — `node_id: ocudu-gnb`; `k8s-deployment.yaml` — `NODE_ID=ocudu-gnb`
  + **`hostPort: 8080`**.
- **Build/deploy on 4.52:** edit `newrapp/*`, then `bash -lc 'cd /home/ubuntu/shivam/
  ransliceassurance/newrapp && go build -o ransliceassurance .'`, then
  `sudo kubectl rollout restart deployment/ransliceassurance -n nonrtric`.

---

## 5. The SDNR / YANG fix (Phase 3 — see `docs/PHASE3_SDNR_RRM_CLOSED_LOOP.md` for full depth)
ODL/yangtools (strict) refused to mount OCUDU because its YANG bundle is revision-inconsistent
(libyang on the device is lenient and tolerated it). Two runtime hot-patches + an SDNC restart made
`RRMPolicyRatio` reachable/writable over SDNR:
1. `ietf-yang-types@2025-12-22` — add the missing `time-with-zone-offset` typedef.
2. `_3gpp-nr-nrm-rrmpolicy@2023-09-18` — add `revision-date 2024-05-24;` to its unqualified
   `nrcelldu` import.
Applied to BOTH the container `/etc/sysrepo/yang/` and the ODL cache, then restart `onap-sdnc-0`.
**Durability gap:** these are runtime hot-patches; lost if the `ocudu-netconf` container is recreated
or the SDNC cache PVC is wiped. **Durable fix = bake consistency into the `ocudu_netconf` image.**
A *separate* module, `netopeer-notifications@2026-01-05`, has a **mandatory cross-module augment
(`session-type`)** that yangtools also rejects — it's why the mount can get stuck `connecting` after
a fresh SDNC start (the mount still serves data once built; a clean rebuild may not recover). That
fix (drop the `mandatory`) is still pending = the "durability" todo.

---

## 6. ⭐ The data-plane fix (the big one this session)

**Symptom:** UE registered fine and got an IP, but **no user-plane data forwarded** — every ping/
iperf/speedtest failed and the OAI UPF spammed `pfcp_session_look_up_pack_in_access failed PDR id 1`.
The gNB GTP-U log showed `local_addr=0.0.0.0`.

**Root cause:** the gNB was binding its **N3/NG-U GTP-U to `0.0.0.0`**, so the OAI UPF couldn't
associate the gNB's uplink and dropped it. (My initial "srsRAN uses `teid=0x1`, it's a TEID bug"
guess was WRONG — a red herring.)

**FIX:** in `config_o1.yaml`, set
```yaml
cu_cp:
  amf:
    bind_addrs: 192.168.70.129    # was 0.0.0.0 — use the host's real IP on the core net
```
`192.168.70.129` is what `ip route get 192.168.70.132` returns as the source on this host. After
this, the gNB GTP-U binds `local_addr=192.168.70.129`, the UPF forwards, ping works both ways
(ext-dn + internet), `failed-PDR = 0`. This delta came from the user's **working CU/DU-split
config** (which sets explicit `amf.bind_addrs` + F1 binds). It is NOT the core and NOT a TEID bug.

**Also applied** (from the same working config, for ZMQ throughput/stability):
`expert_phy.allow_request_on_empty_uplink_slot: true`, `prach` (`preamble_trans_max: 200`,
`total_nof_ra_preambles: 64`), `pdcch.common.ss1_n_candidates: [0,0,2,1,0]`, `nof_antennas_dl/ul: 1`,
and slice `sched_cfg` (`min/max_prb_policy_ratio`). **Gains MUST stay `0`/`0`** — this OCUDU build
(`f96df2fb1c`) hard-errors on non-zero ZMQ gains (`Channel gain must be <= 0.0 dB`); the user's
server runs a different srsRAN build that accepts `80`/`40` (a build difference, not functional).

---

## 7. Multi-slice status & the live loop — ✅ FIRED (steps 1–5 done this session)

> The 5 steps below were completed and the **real-traffic loop fired** (see §0). They are kept here
> as the record of how it was done. The DU `RRMPolicyRatio` member is now `sd=00:00:01`, the
> adapter is connected, the rApp threshold is `1.0` (demo value, hardcoded — wire/revert it), and a
> UDP DL flood (`ext-dn → UE`, `iperf3 -u`) drove it. **Enhancements (multi-policy, durability)
> remain.** What follows are the exact steps + the heads-up on throughput.

The earlier `sd=1` UE was **rejected at the core** (`S-NSSAI is not matched for SMF profile`) because
the old core only knew `sst=1` (no SD). The user deployed the **multi-slice core** + we edited the
gNB config:
- **CU:** `cu_cp.amf.supported_tracking_areas[].plmn_list[].tai_slice_support_list` += `{sst:1,sd:1}`,
  `{sst:1,sd:2}`.
- **DU:** `cell_cfg.slicing` += `{sst:1,sd:1}`, `{sst:1,sd:2}` (with `sched_cfg`).
→ `sd=1` now registers, gets a PDU session + IP, and **data forwards** (after §6).

**To complete the live closed loop:**
1. **Align the netconf `RRMPolicyRatio` member to `sd=1`.** Currently `sd=ff:ff:ff` (no-SD
   sentinel). The UE is now `sst=1/sd=1`, so the rApp's S-NSSAI match (`sst=1,sd=1` vs member
   `sd=0xffffff`) would **MISS** and the loop wouldn't find the policy. Edit-config the member to
   `sd=00:00:01`. (For full multi-slice, also add a second `RRMPolicyRatio` for `sd=2`.)
2. **Restart the adapter** so it reconnects to the fresh gNB WS and real per-slice PM flows.
3. **Lower the rApp threshold.** It is a **hardcoded const** `THROUGHPUT_THRESHOLD = 700.0` in
   `policy/manager.go` (used by `NewManager`). `SetThreshold()` exists but is **not** wired to
   config/API, and `real_env_config.yaml: thresholds.throughput` is **not read** by the manager. So
   lowering it = a small code change (set the const ~1.0 for ZMQ) + `go build` + rollout. ZMQ rfsim
   here gives only ~a few Mbps at ~200 ms latency, so 700 Mbps is unreachable.
4. **Generate sustained traffic** over the slice. `iperf3 -B <oaitun ip>` fails with `Bad file
   descriptor` (bind quirk over the tun); `iperf` v2 isn't installed. Use a `curl`/`wget` download
   via the tunnel, or fix the iperf bind. Even modest traffic (>~1 Mbps) will exceed a lowered
   threshold.
5. **Watch the loop:** `traffic → adapter per-slice PM → rApp decision (>threshold) → SDNR PATCH →
   adapter "Updating RRM policy" → gNB`.

**Enhancements (separate):**
- **Adapter multi-policy:** `config_manager.py` `_extract_rrm_policy_ratio_config` /
  `_extract_cell_config` still read a SINGLE `RRMPolicyRatio` + SINGLE member. For real N-slice they
  must loop over a list of `RRMPolicyRatio` and produce a `slicing` list. (rApp side already matches
  a list by S-NSSAI; CU-CP path already iterates members for `tai_slice_support_list`.)
- **SDNR durability:** the `netopeer-notifications` `mandatory`-augment fix + ideally bake the §5
  YANG fixes into the `ocudu_netconf` image.

---

## 8. Gotchas / hard-won lessons (don't relearn these)

- **Run the adapter at `INFO`, not `DEBUG`** — DEBUG floods the captured pipe and the process blocks.
- **Killing processes:** use `sudo pkill -x gnb` and `sudo pkill -x nr-uesoftmodem` (exact `comm`,
  won't match your own shell). Kill the adapter by its python PID
  (`ps -eo pid,comm,args | awk '/o1_adapter\.py/ && $2 ~ /python/ {print $1}'`). NEVER `pkill -f`
  a pattern that your own command line contains.
- **ZMQ pairs the gNB↔UE at start.** If you restart/swap the UE without restarting the gNB, ZMQ
  desyncs and the UE gets stuck in RACH (`nof_ues=0`, no PRACH). Always restart the gNB when
  (re)starting the UE.
- **ZMQ gains must be `<= 0`** for this OCUDU build (`tx_gain: 0`, `rx_gain: 0`).
- **Restarting the `ocudu-netconf` container RE-SEEDS the datastore** → the `PerfMetricJob`
  `streamTarget` reverts to `http://ocudu-mock-smo:9560/json` and the metric to `DRB.MaxActiveUeDl`.
  Re-apply via NETCONF `edit-config` (it's under `GNBCUCPFunction=cucp1/PerfMetricJob=defaulttrace`):
  set `administrativeState UNLOCKED`, `streamTarget http://192.168.4.52:8080/pm`, and add
  `performanceMetrics` `DlUeThroughput_Cell` + `UlUeThroughput_Cell`. `<config>` needs the base
  namespace `xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"`.
- **The gNB uses `config_o1.yaml`** (`-c`), NOT the adapter's generated `/tmp/adapter_o1_config.yaml`.
  The `config_o1.yaml` dir is **root-owned** → edit with `sudo` (the editor tool can't write it).
- **gNB log timestamps are ~2 h behind wall-clock** (UTC offset) — don't be fooled into thinking the
  log is stale.
- **The rApp's `node_id` comes from `real_env_config.yaml`**, which **overrides** the `NODE_ID` env
  var (config-file path takes precedence in `config/loader.go`). Set it there.
- **rApp on host `:8080`:** the pod is NOT hostNetwork; we added `hostPort: 8080`. Historically a
  stale **host-run** `./ransliceassurance` process squatted `:8080` — kill any such orphan (it's a
  user-session process, `ppid=1`, not k8s) so the pod owns the port. NodePort `30888` always routes
  to the live pod regardless.
- **`snap` docker can't `docker cp` to host paths** — use `docker exec -i ... sh -c 'cat > file'`.
- **No Go on 4.50**; Go is on **4.52 login shell** (`bash -lc`). The rApp pod just execs the host
  binary from the hostPath mount, so: build on 4.52 → rollout restart.
- **Shared-infra guardrail:** changes to SDNR/SDNC (mount delete/create, pod restarts) and `kubectl`
  deploys are gated and need explicit user OK each time.

---

## 9. Exact run commands (on 4.50 unless noted)

```bash
# Core (multi-slice) — bring up / down
sudo docker compose -f /home/ubuntu/oai-cn5g/docker-compose.yaml up -d
sudo docker compose -f /home/ubuntu/oai-cn5g/docker-compose.yaml down

# gNB (logs to ./gnb.log in the cwd)
sudo bash -c 'cd /home/ubuntu/sushant/ocudu/build/apps/gnb && exec ./gnb -c /home/ubuntu/sushant/ocudu/configs/config_o1.yaml'

# Adapter (INFO; from the adapter repo dir)
cd /home/ubuntu/sushant/ocudu_o1_adapter && PYTHONUNBUFFERED=1 /home/ubuntu/sushant/o1_venv/bin/python -u src/o1_adapter.py \
  --netconf_host localhost --netconf_port 830 --profile gnb --ws_host localhost --ws_port 8001 \
  -c /tmp/adapter_o1_config.yaml --loglevel INFO

# UE (sd=1, multi-slice); run from cmake_targets, needs root for oaitun
sudo bash -c 'cd /home/ubuntu/sushant/openairinterface5g/cmake_targets && exec ./nr-uesoftmodem \
  -r 106 --numerology 1 --band 78 -C 3489420000 \
  --uicc0.imsi 001010000000002 --uicc0.pdu_sessions.[0].nssai_sst 1 \
  --uicc0.pdu_sessions.[0].nssai_sd 1 --uicc0.pdu_sessions.[0].dnn oai \
  --zmq.[0].tx_channels tcp://127.0.0.1:4557 --zmq.[0].rx_channels tcp://127.0.0.1:4556 \
  --device.name oai_zmqdevif --ssb 42 \
  --uecap_file ../targets/PROJECTS/GENERIC-NR-5GC/CONF/uecap_ports1.xml'

# Startup order: core (healthy) -> gNB (N2/AMF up) -> adapter -> UE.

# Verify UE data path
ip -br addr show oaitun_ue1                       # UE IP (10.0.0.x)
sudo ping -I oaitun_ue1 -c3 192.168.70.135        # ext-dn
for u in oai-upf oai-upf2 oai-upf3 oai-upf4; do echo "$u $(sudo docker logs $u --since=60s 2>&1 | grep -c 'failed PDR')"; done

# SDNR mount status + RRMPolicyRatio (from 4.50, ssh to 4.52, or curl 4.52 directly)
AUTH='-u admin:Kp8bJ4SXszM0WXlhak3eHlcse2gAw84vaoGGmJvUy2U'
curl -s $AUTH "http://192.168.4.52:30267/rests/data/network-topology:network-topology/topology=topology-netconf/node=ocudu-gnb?content=nonconfig" | python3 -m json.tool | grep connection-status

# rApp build + deploy (on 4.52)
ssh ubuntu@192.168.4.52 'bash -lc "cd /home/ubuntu/shivam/ransliceassurance/newrapp && go build -o ransliceassurance . && sudo kubectl rollout restart deployment/ransliceassurance -n nonrtric"'
ssh ubuntu@192.168.4.52 'sudo kubectl logs -n nonrtric -l app=ransliceassurance --tail=30'
```

---

## 10. Component versions
- gNB: OCUDU srsRAN, commit **`f96df2fb1c`** (rejects non-zero ZMQ gains).
- UE: OAI `nr-uesoftmodem`, branch `develop` hash `26efcc4989` (May 2026).
- `ocudu-netconf`: netopeer2 2.8.2, sysrepo 4.5.4, libyang/yanglint 5.4.9.
- ODL/SDNR: 5.0.7. rApp: Go 1.23.2 build, runs in `alpine:latest` via `libgcompat`.
- Core: OAI cn5g, multi-slice variant at `/home/ubuntu/oai-cn5g`.

---

## 11. Persisted memory (auto-loaded each session)
`/home/ubuntu/.claude/projects/-home-ubuntu-sushant-ocudu-o1-adapter/memory/`:
`working-principles.md`, `ocudu-rapp-integration.md`, `phase3-sdnr-working.md`,
`multislice-and-dataplane.md` (the data-path fix + multi-slice plan). Keep these updated.

---

*Authored at the end of the session where we (a) proved the SDNR RRM loop + real PM pipeline,
(b) committed adapter `73c7641` and rApp `435fe65`, (c) brought up the multi-slice core, and
(d) FIXED the user-plane with `amf.bind_addrs`. The next chat's job: finish §7 (align the
`RRMPolicyRatio` member to sd=1, lower the rApp threshold, push traffic, watch the live loop).*
