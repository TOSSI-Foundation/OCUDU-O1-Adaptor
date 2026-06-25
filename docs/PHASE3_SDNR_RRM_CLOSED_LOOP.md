# Phase 3 — RRM Closed Loop over SDNR (the "Option A / Option 1" YANG fix)

> **Status: WORKING — proven end-to-end on 2026-06-20.**
> The `ransliceassurance` rApp can now **read and write** `RRMPolicyRatio` on the OCUDU
> (srsRAN) gNB **through SDNR/RESTCONF**, and the change propagates all the way down to the
> running gNB. This document explains exactly what was broken, why, how it was fixed, how to
> reproduce it, how to verify it, and — critically — the **durability flag** (the fix is a
> runtime hot-patch that must be baked into the image to survive a container rebuild).

---

## 0. TL;DR (read this first)

The OCUDU NETCONF server (`ocudu_netconf`) shipped a **YANG bundle that is internally
revision-inconsistent**. The device's own YANG library (libyang) is *lenient* and tolerated it,
so the gNB / adapter / sysrepo all worked. But **OpenDaylight (ODL/yangtools), which SDNR is
built on, is *strict*** — it refuses to build a schema context it cannot fully resolve, so the
OCUDU node mounted with a pile of `unavailable-capabilities` and the `RRMPolicyRatio` subtree was
**not reachable** over RESTCONF. That killed the SDNR-based RRM closed loop (Option A).

Two surgical YANG corrections + one SDNR restart fixed it:

1. **`ietf-yang-types@2025-12-22`** was a libyang-5 *phantom* built-in missing the
   `time-with-zone-offset` typedef that `_3gpp-common-yang-types` imports. → merged the typedef in.
2. **`_3gpp-nr-nrm-rrmpolicy@2023-09-18`** imported `_3gpp-nr-nrm-nrcelldu` **unqualified**, and
   ODL would not bind that to the installed `nrcelldu@2024-05-24`. → added an explicit
   `revision-date 2024-05-24;` pin to the import.
3. Restart `onap-sdnc-0` to flush ODL's in-memory schema cache so it re-reads the corrected files.

Result: the `ocudu-gnb` mount goes `connected`, the 3GPP tree resolves, and:

```
GET  RRMPolicyRatio via SDNR  -> HTTP 200 (real attributes)
PATCH rRMPolicyDedicatedRatio -> HTTP 200, writes through to ocudu_netconf, adapter reacts.
```

**⚠️ THE FLAG:** both YANG edits live only in the **running container** (`/etc/sysrepo/yang`) and
the **ODL cache** (`/opt/opendaylight/cache/schema`). They are **lost if the `ocudu_netconf`
container is recreated from its image** (and the ODL copies are lost if the SDNC cache PVC is
wiped). See [§9 — The Durability Flag](#9-the-durability-flag-must-read).

---

## 1. What "the closed loop" is and why it matters

The whole point of the integration is a **RAN intelligent control loop**:

```
  gNB  --PM (per-slice throughput)-->  O1 adapter  --HTTP /pm-->  rApp
                                                                    |
                                          rApp decides a slice is starved
                                                                    |
   gNB <--rrm_policy_ratio_set (WS)-- O1 adapter <--netconf-config-change-- ocudu_netconf
                                                                    ^
                                                                    | NETCONF edit-config
                                                                  ODL (SDNR)
                                                                    ^
                                                                    | RESTCONF PATCH
                                                                  rApp
```

- The **PM half** (gNB → adapter → rApp) was already working from Phase 1/2.
- The **control half** (rApp → … → gNB) is what Phase 3 delivers. We chose **Option A
  (SDNR/RESTCONF)** over Option D (rApp talks NETCONF straight to `ocudu_netconf`) because
  SDNR is the production-realistic SMO path: the rApp speaks RESTCONF to ODL, ODL speaks
  NETCONF to the device. That realism is exactly what forced us to satisfy ODL's strict parser.

The single knob the loop turns is **`RRMPolicyRatio`** — the standard 3GPP
`_3gpp-nr-nrm-rrmpolicy` NRM object that augments `NRCellDU`. Its
`rRMPolicyDedicatedRatio` / `rRMPolicyMinRatio` / `rRMPolicyMaxRatio` set the PRB share a slice
(S-NSSAI) gets. The adapter watches this object and pushes `rrm_policy_ratio_set` to the gNB.

---

## 2. Environment / topology (as tested)

| VM | Role | Key bits |
|----|------|----------|
| **192.168.4.50** | OCUDU stack | srsRAN gNB (ZMQ build), `o1_adapter` (Python), `ocudu_netconf` container, OAI 5G core, OAI UE |
| **192.168.4.51** | OAI reference stack | OAI gNB + its own `o1_adapter` (the *consistent* bundle we compared against) |
| **192.168.4.52** | SMO / rApp | ONAP **SDNR** (`onap-sdnc-0`, OpenDaylight), the `ransliceassurance` rApp |

SSH to all three: `ubuntu@<ip>`, password `ubuntu`.

**`ocudu_netconf` container (on 4.50):**
- netopeer2-server **2.8.2**, libsysrepo **v4.5.4**, libyang / yanglint **5.4.9**.
- NETCONF endpoint: `localhost:830`, user `root` / pass `root`.
- Served YANG files: `/etc/sysrepo/yang/` (this is what `get-schema` serves to ODL).
- Serves 3GPP Rel-18 NRM + 22 custom `ocudu-*` YANG modules that augment
  `ManagedElement` / `GNBDUFunction` / `NRCellDU`.
- Config-only datastore (no operational state from the RAN flows back through it).

**SDNR / ODL (on 4.52):**
- RESTCONF base URL: `http://192.168.4.52:30267/rests` (NodePort 30267).
- Auth used in this work: `-u admin:Kp8bJ4SXszM0WXlhak3eHlcse2gAw84vaoGGmJvUy2U`.
- Pod / container: `kubectl -n onap onap-sdnc-0 -c sdnc`.
- ODL schema cache (filesystem): `/opt/opendaylight/cache/schema/`.
- Our mounted device node name: **`ocudu-gnb`**.

**O1 adapter (on 4.50):** repo `/home/ubuntu/sushant/ocudu_o1_adapter`. It is a NETCONF
**client** of `ocudu_netconf` (it subscribes to `netconf-config-change`), and drives the gNB
over a WebSocket on `:8001` with commands like `rrm_policy_ratio_set` / `ssb_set`.

---

## 3. Why it was broken — strict vs lenient YANG

YANG modules import each other. A consumer of the schema must assemble a **schema context**:
every module + every revision every import asks for. There are two philosophies:

- **libyang (on the device — netopeer2/sysrepo): lenient.** If an import is slightly off (a
  missing typedef, an unqualified import that *almost* resolves), libyang shrugs and carries on.
  So the gNB, the adapter, and sysrepo were all perfectly happy. From inside OCUDU, nothing looked
  wrong.

- **yangtools (inside OpenDaylight / SDNR): strict.** ODL pulls every module from the device
  (via `get-schema`, falling back to a filesystem cache) and rebuilds the **entire** schema
  context itself. If even one import can't be satisfied, ODL marks the offending modules as
  **`unavailable-capabilities`** and refuses to expose their data tree. `RRMPolicyRatio` lives
  under exactly such an unresolved branch → not reachable over RESTCONF.

So the bug was invisible on the device and only manifested at the SMO. That is why we burned so
much time: every "it works here" on OCUDU was true, and irrelevant to ODL.

### The two concrete inconsistencies

**(A) The phantom `ietf-yang-types@2025-12-22`.**
libyang 5 ships some IETF built-ins *in-memory* with a synthetic revision date
(`2025-12-22`) and **no file on disk**. That in-memory copy was **missing the
`time-with-zone-offset` typedef**. But `_3gpp-common-yang-types@2025-11-06` (which nearly the
whole 3GPP tree depends on) imports `time-with-zone-offset` from `ietf-yang-types`. On the device
libyang papered over it; ODL could not (`get-schema ietf-yang-types@2025-12-22` → the server tried
to open a file that didn't exist → fail → cascade of unavailable 3GPP modules).

**(B) The `rrmpolicy` ↔ `nrcelldu` revision mismatch.**
The 3GPP MnS snapshot OCUDU pulled is itself mixed-era:
- `_3gpp-nr-nrm-rrmpolicy` is revision **2023-09-18**
- `_3gpp-nr-nrm-nrcelldu` is revision **2024-05-24** (newer)

`rrmpolicy.yang` imports `nrcelldu` with **no `revision-date`** (unqualified). libyang resolves
the unqualified import to the only installed `nrcelldu` (2024-05-24) and moves on. ODL/yangtools,
however, left `_3gpp-nr-nrm-rrmpolicy` in the unavailable set with an **unsatisfied import of
`_3gpp-nr-nrm-nrcelldu`** — it would not bind the unqualified import to the available revision.
Because `RRMPolicyRatio` is defined in `rrmpolicy`, this single unresolved module is what
ultimately kept the RRM knob off-limits.

---

## 4. How we proved the root cause (the OAI control experiment)

Rather than guess, we used the **OAI stack on 4.51 as a control**. OAI runs effectively the same
SMO/SDNR/rApp machinery, but its `o1_adapter` ships an **internally consistent** YANG bundle
(an older, coherent ~2023-09-18 3GPP snapshot, on libyang2, no phantom built-ins, `rrmpolicy`
pinning a `nrcelldu` revision that *is* installed).

When the OAI gNB was started, its node in SDNR went **`connection-status=connected`,
`unavailable-capabilities=0`** and its RRM tree was fully reachable. OCUDU's node, with the same
ODL, did not. Same SMO, same rApp, same ODL — **the only differing variable is the device's YANG
bundle consistency.** That conclusively pinned the blame on OCUDU's YANG packaging, not on ODL,
SDNR, the mount procedure, or the rApp.

---

## 5. The fix — step by step (reproducible)

> All commands assume you are on a host that can reach the `ocudu-netconf` container (4.50) and
> can `ssh ubuntu@192.168.4.52` (the SDNR host). Adjust the python venv path
> (`/home/ubuntu/sushant/o1_venv`) and the ODL auth token to your environment.

### Step 0 — Back up first (so it's reversible)

```bash
mkdir -p /home/ubuntu/ocudu_netconf_backup
sudo docker exec ocudu-netconf sysrepoctl -l            > /home/ubuntu/ocudu_netconf_backup/modules_before.txt
sudo docker exec ocudu-netconf sysrepocfg --export -d running -f xml \
                                                        > /home/ubuntu/ocudu_netconf_backup/running_before.xml
```

### Step 1 — Fix `ietf-yang-types` (add `time-with-zone-offset`)

The device must *serve* a real `ietf-yang-types@2025-12-22.yang` file (the phantom has no file),
containing the `time-with-zone-offset` typedef that `_3gpp-common-yang-types` needs. Place the
corrected file both in the container's served dir and in ODL's cache:

```bash
# container (so get-schema can serve it):
#   /etc/sysrepo/yang/ietf-yang-types@2025-12-22.yang   (must contain typedef time-with-zone-offset)
# ODL cache (so ODL resolves even if get-schema is skipped):
#   onap/onap-sdnc-0:/opt/opendaylight/cache/schema/ietf-yang-types@2025-12-22.yang
```

(Verify: `grep -c time-with-zone-offset /etc/sysrepo/yang/ietf-yang-types@2025-12-22.yang` → ≥1.)

### Step 2 — Pin `rrmpolicy`'s `nrcelldu` import to the installed revision

This is the key edit. We add an explicit `revision-date` to the unqualified import so ODL has an
unambiguous instruction.

```bash
F="/etc/sysrepo/yang/_3gpp-nr-nrm-rrmpolicy@2023-09-18.yang"
sudo docker exec ocudu-netconf cat "$F" > /tmp/rrmpolicy.yang

python3 - <<'PY'
import re
f="/tmp/rrmpolicy.yang"; s=open(f).read()
if "revision-date 2024-05-24" not in s:
    s = re.sub(r"(import _3gpp-nr-nrm-nrcelldu\s*\{\s*prefix\s+nrcelldu3gpp\s*;)",
               r"\1\n    revision-date 2024-05-24;", s, count=1)
    open(f,"w").write(s); print("patched")
else:
    print("already patched")
PY

# write the patched file back into the container's served dir:
cat /tmp/rrmpolicy.yang | sudo docker exec -i ocudu-netconf sh -c "cat > $F"
```

The import block should now read:

```yang
  import _3gpp-nr-nrm-nrcelldu {
    prefix nrcelldu3gpp;
    revision-date 2024-05-24;
  }
```

Verify the device actually *serves* the pinned version (this is what ODL fetches):

```bash
/home/ubuntu/sushant/o1_venv/bin/python - <<'PY'
from ncclient import manager
m=manager.connect(host='localhost',port=830,username='root',password='root',
                  hostkey_verify=False,allow_agent=False,look_for_keys=False,timeout=10)
s=str(m.get_schema('_3gpp-nr-nrm-rrmpolicy',version='2023-09-18'))
print('served pin present:', 'revision-date 2024-05-24' in s)   # -> True
m.close_session()
PY
```

### Step 3 — Push the patched `rrmpolicy` into ODL's cache

```bash
scp /tmp/rrmpolicy.yang ubuntu@192.168.4.52:/tmp/rrmpolicy.yang
ssh ubuntu@192.168.4.52 '
  sudo kubectl cp /tmp/rrmpolicy.yang \
    onap/onap-sdnc-0:/opt/opendaylight/cache/schema/_3gpp-nr-nrm-rrmpolicy@2023-09-18.yang
  sudo kubectl exec -n onap onap-sdnc-0 -c sdnc -- \
    grep -c "revision-date 2024-05-24" \
    /opt/opendaylight/cache/schema/_3gpp-nr-nrm-rrmpolicy@2023-09-18.yang   # -> 1
'
```

### Step 4 — Restart SDNR to flush ODL's in-memory schema cache

ODL caches the assembled schema context **in memory for the life of the JVM**. Editing files on
disk is not enough; the pod must restart to re-read them. (Shared infra — get the owner's OK.)

```bash
ssh ubuntu@192.168.4.52 'sudo kubectl delete pod onap-sdnc-0 -n onap --wait=false'
# then poll until 1/1 Running:
ssh ubuntu@192.168.4.52 'for i in $(seq 1 32); do
  kubectl get pod onap-sdnc-0 -n onap --no-headers | grep -q "1/1 *Running" && break; sleep 5; done'
```

---

## 6. Verification (what "working" looked like)

After the restart, ODL re-reads the corrected cache, the `ocudu-gnb` mount comes up
`connected`, and the 3GPP tree resolves. The only `unavailable-capabilities` left are
**irrelevant non-3GPP modules** — ODL drops them and keeps going:

```
netopeer-notifications, sysrepo, ietf-yang-metadata,
ietf-yang-structure-ext, yang, ietf-origin, default
```

None of these touch the RRM tree. (They are netconf-server/datastore plumbing modules, not part
of the 3GPP NRM.)

### 6.1 Read works

```bash
RRM="http://192.168.4.52:30267/rests/data/network-topology:network-topology/topology=topology-netconf/node=ocudu-gnb/yang-ext:mount/_3gpp-common-managed-element:ManagedElement=ran1/_3gpp-nr-nrm-gnbdufunction:GNBDUFunction=du1/_3gpp-nr-nrm-nrcelldu:NRCellDU=nrcelldu1/_3gpp-nr-nrm-rrmpolicy:RRMPolicyRatio=rrm_policy1"
AUTH='-u admin:Kp8bJ4SXszM0WXlhak3eHlcse2gAw84vaoGGmJvUy2U'
curl -s -w '\nHTTP %{http_code}\n' $AUTH "$RRM"
```

→ `HTTP 200`:

```json
{"_3gpp-nr-nrm-rrmpolicy:RRMPolicyRatio":[{"id":"rrm_policy1","attributes":{
  "resourceType":"PRB",
  "rRMPolicyMemberList":[{"mcc":"001","mnc":"01","sd":"ff:ff:ff","sst":1}],
  "rRMPolicyMaxRatio":100,"rRMPolicyMinRatio":0,"rRMPolicyDedicatedRatio":25}}]}
```

### 6.2 Write works AND writes through to the device

```bash
curl -s -o /dev/null -w 'PATCH -> HTTP %{http_code}\n' $AUTH -X PATCH \
  -H 'Content-Type: application/yang-data+json' "$RRM" \
  -d '{"_3gpp-nr-nrm-rrmpolicy:RRMPolicyRatio":[{"id":"rrm_policy1","attributes":{"rRMPolicyDedicatedRatio":30}}]}'
# PATCH -> HTTP 200
```

Confirm it actually landed in `ocudu_netconf`'s running datastore (not just ODL's cache) —
read the device directly with ncclient:

```
ocudu_netconf running rRMPolicyDedicatedRatio = 30   ✅
```

### 6.3 The adapter reacts (control half closes)

The adapter is subscribed to `netconf-config-change`. When ODL's edit-config hits the device, the
adapter log shows:

```
[INFO] Updating RRM policy
[INFO] Generating /tmp/adapter_o1_config.yaml
```

i.e. it picked up the new ratio and re-rendered the gNB config, which it then pushes to the gNB
via the `rrm_policy_ratio_set` WebSocket command. (Earlier, a direct edit had already produced a
visible `DU reconfiguration procedure` on the gNB, proving the adapter→gNB hop.)

> Harmless noise in the adapter log: lines like `Couldn't extract OCUDU SRS/PDCCH/CSI config
> extensions: 'ocudu_nrcelldu_*_extensions'`. The adapter probes for optional custom SRS
> extension fields; when a config doesn't carry them it warns and continues. Not related to RRM.

### 6.4 Full chain, confirmed

```
rApp → RESTCONF PATCH (200) → ODL → NETCONF edit-config → ocudu_netconf running (value changed)
     → netconf-config-change → adapter ("Updating RRM policy") → gNB (rrm_policy_ratio_set)
```

---

## 7. Why each piece of the fix was necessary (mental model)

- **`get-schema` is the source of truth ODL fetches.** Editing
  `/etc/sysrepo/yang/<module>@<rev>.yang` changes what `get-schema` returns. That's why the
  rrmpolicy edit goes there (not a sysrepo module reinstall).
- **The ODL cache is the fallback ODL trusts.** If `get-schema` is unavailable or partial, ODL
  reads `/opt/opendaylight/cache/schema/`. We update *both* so ODL resolves regardless of path.
- **An explicit `revision-date` is unambiguous.** ODL's refusal to bind the unqualified
  `nrcelldu` import disappears once we say *exactly* which revision to use — and that revision
  (2024-05-24) is the one installed, so it resolves.
- **The restart is mandatory.** ODL's schema context is built once and cached in the JVM. No
  amount of on-disk editing takes effect until the pod restarts.

---

## 8. Reversibility (asked and answered: YES)

Everything done here is reversible and **non-destructive to user/config data**:

| Change | How to undo |
|--------|-------------|
| `ietf-yang-types@2025-12-22.yang` added/edited in container | remove/restore the file; or recreate the container from its image |
| `_3gpp-nr-nrm-rrmpolicy@2023-09-18.yang` import pin in container | restore original (no `revision-date`); or recreate container |
| Same two files in ODL cache | delete them from `/opt/opendaylight/cache/schema/` |
| `ocudu-gnb` SDNR mount | RESTCONF `DELETE` on the topology node |
| Any change to the `ocudu_netconf` repo/image | `git revert` + rebuild |

Backup taken before the work: `/home/ubuntu/ocudu_netconf_backup/`
(`modules_before.txt` = `sysrepoctl -l` snapshot; `running_before.xml` = full running datastore).
Ultimate revert for the device = **recreate the `ocudu_netconf` container from its unchanged
image** (it reloads its seed config).

---

## 9. THE DURABILITY FLAG (must read)

> **This is the single most important caveat. Read it before you rely on the closed loop.**

The two YANG corrections are **runtime hot-patches**. They exist only in:

1. the **running `ocudu_netconf` container** at `/etc/sysrepo/yang/…`, and
2. the **ODL schema cache** at `/opt/opendaylight/cache/schema/…`.

They are **NOT in any image or repo.** Concretely:

- **If the `ocudu_netconf` container is recreated/redeployed from its image**, the served YANG
  reverts to the broken (inconsistent) bundle. The mount breaks again on the next SDNR restart.
- **If the SDNC pod's schema-cache PVC is wiped/recreated**, the corrected cache files are lost.
  (A plain pod restart keeps the PVC, so the cache survives a restart — but not a re-provision.)
- A plain process restart of the gNB/adapter does **not** affect this (the YANG lives in the
  netconf container, not the gNB).

**So today's working state is correct but fragile.** Treat it as a validated proof, not a durable
deployment, until the persistent fix below is applied.

### Making it durable (the real "Option 1 proper" = fix the image)

The permanent fix is to bake consistency into the `ocudu_netconf` **repo/image** so the served
bundle is revision-consistent out of the box (exactly why OAI on 4.51 just works). In the
`ocudu_netconf` build (its `setup_*.sh` / `download_yang_models.sh` / the set of `.yang` files
installed into sysrepo):

1. Ship an `ietf-yang-types` that actually contains `time-with-zone-offset` (a real file, not the
   libyang-5 phantom), and make the 3GPP common-types import resolve against it.
2. Make `rrmpolicy` and `nrcelldu` (and the rest of the interdependent 3GPP NRM modules)
   revision-consistent — either install all from one coherent 3GPP snapshot, or carry a
   `rrmpolicy` whose `nrcelldu` import is pinned to the installed `nrcelldu@2024-05-24`.
3. Re-verify the 22 custom `ocudu-*` extension modules still augment the chosen snapshot cleanly.

After rebuilding the image, the on-device hot-patches in §5 are no longer needed; the ODL cache
copies become redundant (ODL will `get-schema` a correct bundle). This is git-revertable and is
the change that should ultimately go upstream to SRS.

---

## 10. Troubleshooting / gotchas (hard-won)

- **Run the adapter at `INFO`, not `DEBUG`.** At DEBUG it floods the captured-output pipe and the
  process blocks on write (looks like a hang).
- **Never `pkill -f "gnb -c"`** from a shell whose own command line contains that pattern — you'll
  kill your own shell. Kill by PID.
- **`docker cp` under snap can silently fail** to write host paths (returns 0, no file). Use
  `docker exec -i … sh -c 'cat > file'` to write into the container and `docker exec … cat >
  hostfile` to read out.
- **ncclient `edit-config` needs the base namespace** on `<config>`:
  `<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">…</config>`, else "Missing XML
  namespace".
- **ZMQ gNB requires `tx_gain`/`rx_gain` ≤ 0** ("Channel gain must be <= 0.0 dB").
- **Editing YANG on disk does nothing until the SDNC pod restarts** (in-memory schema cache).
- **`unavailable-capabilities` being non-zero is not automatically failure** — check *which*
  modules. Non-3GPP plumbing modules (sysrepo, yang, ietf-origin, …) being unavailable is fine;
  the RRM tree only needs the 3GPP modules to resolve.

---

## 11. Key file/URL reference

| Thing | Value |
|-------|-------|
| ocudu_netconf NETCONF | `localhost:830` on 4.50, `root`/`root` |
| Served YANG (device) | `/etc/sysrepo/yang/` in the `ocudu-netconf` container |
| ODL RESTCONF base | `http://192.168.4.52:30267/rests` |
| ODL auth (this env) | `admin` / `Kp8bJ4SXszM0WXlhak3eHlcse2gAw84vaoGGmJvUy2U` |
| SDNR pod/container | `kubectl -n onap onap-sdnc-0 -c sdnc` |
| ODL schema cache | `/opt/opendaylight/cache/schema/` |
| Mount node name | `ocudu-gnb` |
| Patched file 1 | `ietf-yang-types@2025-12-22.yang` (add `time-with-zone-offset`) |
| Patched file 2 | `_3gpp-nr-nrm-rrmpolicy@2023-09-18.yang` (pin nrcelldu `revision-date 2024-05-24`) |
| Backup | `/home/ubuntu/ocudu_netconf_backup/` |
| Adapter repo | `/home/ubuntu/sushant/ocudu_o1_adapter` |
| RRM object path | `…/NRCellDU=nrcelldu1/_3gpp-nr-nrm-rrmpolicy:RRMPolicyRatio=rrm_policy1` |

### RRMPolicyRatio RESTCONF URL (copy-paste)

```
http://192.168.4.52:30267/rests/data/network-topology:network-topology/topology=topology-netconf/node=ocudu-gnb/yang-ext:mount/_3gpp-common-managed-element:ManagedElement=ran1/_3gpp-nr-nrm-gnbdufunction:GNBDUFunction=du1/_3gpp-nr-nrm-nrcelldu:NRCellDU=nrcelldu1/_3gpp-nr-nrm-rrmpolicy:RRMPolicyRatio=rrm_policy1
```

---

## 12. Component versions (for the record)

- netopeer2-server **2.8.2**, libsysrepo **v4.5.4** (SO v8.4.8), libyang/yanglint **5.4.9**
- OCUDU RAN = srsRAN_Project (ZMQ build), OAI 5G core, OAI nr-uesoftmodem UE
- SDNR = ONAP SDNC / OpenDaylight (RESTCONF on NodePort 30267)
- Reference (consistent) stack = OAI on 192.168.4.51

---

*Authored 2026-06-20 after the Phase 3 SDNR closed loop was proven end-to-end. If you are reading
this because the OCUDU node fell out of SDNR again — check §9 first: the container or SDNC cache
was almost certainly re-provisioned and the hot-patch was lost. Re-apply §5, or (better) bake the
durable fix from §9 into the `ocudu_netconf` image.*
