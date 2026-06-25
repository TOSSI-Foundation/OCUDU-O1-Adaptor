# Fix: NETCONF device YANG bundle won't mount cleanly in OpenDaylight / SDNR

A NETCONF device that works perfectly on its own (netopeer2 / sysrepo / libyang) can still mount
in ODL/SDNR with a pile of `unavailable-capabilities` and whole data subtrees missing. Cause:
**libyang on the device is lenient about YANG revision/import consistency; ODL's yangtools is
strict.** Any import ODL can't satisfy → it drops that module and everything defined in it.

This is the exact fix for the OCUDU stack (3GPP NRM bundle), reproducible for the same class of
issue.

## Symptom
- Device mounts but `connection-status` unusable / `unavailable-capabilities` non-empty in ODL.
- RESTCONF GET on a subtree (e.g. `RRMPolicyRatio`) returns 404 / not exposed.
- The device itself answers `get-config` / `get-schema` fine — only ODL chokes.
- A *different* device with an internally consistent bundle mounts clean on the **same** ODL.

## Root cause (two YANG inconsistencies)
- **(A) phantom `ietf-yang-types@2025-12-22`** — libyang 5 ships it in-memory with no file on
  disk, **missing the `time-with-zone-offset` typedef** that `_3gpp-common-yang-types` imports.
  ODL's `get-schema ietf-yang-types@2025-12-22` fails → cascade of unavailable 3GPP modules.
- **(B) unqualified import** — `_3gpp-nr-nrm-rrmpolicy@2023-09-18` imports `_3gpp-nr-nrm-nrcelldu`
  with **no `revision-date`**; the installed `nrcelldu` is `@2024-05-24`. libyang binds it; ODL
  refuses the unqualified import → `rrmpolicy` (and `RRMPolicyRatio`) stays unavailable.

## Where files live
- Device served YANG (what `get-schema` returns): container `/etc/sysrepo/yang/`
- ODL schema cache (fallback): `onap/onap-sdnc-0:/opt/opendaylight/cache/schema/`
- Both must be corrected; ODL keeps its assembled schema **in memory** → pod restart required.

---

## Fix

### 0. Back up (reversible)
```bash
mkdir -p ~/ocudu_netconf_backup
sudo docker exec ocudu-netconf sysrepoctl -l > ~/ocudu_netconf_backup/modules_before.txt
sudo docker exec ocudu-netconf sysrepocfg --export -d running -f xml > ~/ocudu_netconf_backup/running_before.xml
```

### A. Serve a real `ietf-yang-types@2025-12-22.yang` with `time-with-zone-offset`
Place the corrected file (must contain `typedef time-with-zone-offset`) in **both** locations:
```bash
# device served dir:  /etc/sysrepo/yang/ietf-yang-types@2025-12-22.yang
# ODL cache:          onap/onap-sdnc-0:/opt/opendaylight/cache/schema/ietf-yang-types@2025-12-22.yang
sudo docker exec ocudu-netconf grep -c time-with-zone-offset /etc/sysrepo/yang/ietf-yang-types@2025-12-22.yang   # ->= 1
```

### B. Pin `rrmpolicy`'s `nrcelldu` import to the installed revision
```bash
F="/etc/sysrepo/yang/_3gpp-nr-nrm-rrmpolicy@2023-09-18.yang"
sudo docker exec ocudu-netconf cat "$F" > /tmp/rrmpolicy.yang
python3 - <<'PY'
import re
f="/tmp/rrmpolicy.yang"; s=open(f).read()
if "revision-date 2024-05-24" not in s:
    s=re.sub(r"(import _3gpp-nr-nrm-nrcelldu\s*\{\s*prefix\s+nrcelldu3gpp\s*;)",
             r"\1\n    revision-date 2024-05-24;", s, count=1)
    open(f,"w").write(s); print("patched")
else: print("already patched")
PY
cat /tmp/rrmpolicy.yang | sudo docker exec -i ocudu-netconf sh -c "cat > $F"
```
Resulting import block:
```yang
import _3gpp-nr-nrm-nrcelldu { prefix nrcelldu3gpp; revision-date 2024-05-24; }
```
Confirm the device *serves* the pin (this is what ODL fetches):
```bash
python3 - <<'PY'
from ncclient import manager
m=manager.connect(host='localhost',port=830,username='root',password='root',hostkey_verify=False,allow_agent=False,look_for_keys=False,timeout=10)
print('served pin:', 'revision-date 2024-05-24' in str(m.get_schema('_3gpp-nr-nrm-rrmpolicy',version='2023-09-18'))); m.close_session()
PY
```

### C. Copy the patched `rrmpolicy` into ODL's cache
```bash
scp /tmp/rrmpolicy.yang ubuntu@192.168.4.52:/tmp/rrmpolicy.yang
ssh ubuntu@192.168.4.52 'sudo kubectl cp /tmp/rrmpolicy.yang onap/onap-sdnc-0:/opt/opendaylight/cache/schema/_3gpp-nr-nrm-rrmpolicy@2023-09-18.yang'
```

### D. Restart SDNR (flush ODL's in-memory schema cache) — shared infra, get owner OK
```bash
ssh ubuntu@192.168.4.52 'sudo kubectl delete pod onap-sdnc-0 -n onap --wait=false'
# wait until onap-sdnc-0 is 1/1 Running, then re-mount the device if needed
```

---

## Verify
```bash
AUTH='-u admin:<odl-password>'; BASE='http://192.168.4.52:30267/rests/data/network-topology:network-topology/topology=topology-netconf/node=ocudu-gnb/yang-ext:mount'
curl -s -w '\nHTTP %{http_code}\n' $AUTH "$BASE/_3gpp-common-managed-element:ManagedElement=ran1/_3gpp-nr-nrm-gnbdufunction:GNBDUFunction=du1/_3gpp-nr-nrm-nrcelldu:NRCellDU=nrcelldu1/_3gpp-nr-nrm-rrmpolicy:RRMPolicyRatio=rrm_policy1"
```
Expect **HTTP 200** with the `RRMPolicyRatio` attributes. The only `unavailable-capabilities` left
should be non-3GPP plumbing modules (`netopeer-notifications`, `sysrepo`, `ietf-yang-metadata`,
`ietf-yang-structure-ext`, `yang`, `ietf-origin`, `default`) — harmless, ODL drops them.

## Durability (important)
A/B are **runtime hot-patches** in the running container + ODL cache. They are **lost** if the
NETCONF container is recreated from its image (or the SDNC cache PVC is wiped). Permanent fix:
**bake the two corrected YANG files into the device image** (the YANG-download/build step) so the
served bundle is revision-consistent.

## Generalizing
The bug class is "lenient device libyang vs strict ODL yangtools." To find your own offenders:
diff a working device's mount (`unavailable-capabilities=0`) against the broken one, then for each
unavailable module check (1) every `import` has a `revision-date` that matches an *installed*
revision, and (2) no import resolves to a phantom/in-memory built-in that lacks a needed typedef.
Pin imports and supply a real file for any phantom; recopy to the ODL cache; restart SDNR.
