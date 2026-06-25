# NETCONF device YANG bundle won't mount cleanly in OpenDaylight / SDNR

A NETCONF device that works on its own (netopeer2 / sysrepo / libyang) can still mount in ODL/SDNR
with `unavailable-capabilities` and whole data subtrees missing.

**Why:** the device's libyang is **lenient** about YANG revision/import consistency; ODL's
yangtools is **strict**. Any `import` ODL cannot satisfy → it drops that module and everything
defined in it.

## Symptom
- Device mounts but shows non-empty `unavailable-capabilities`; affected subtrees not exposed.
- RESTCONF GET on the subtree returns 404 / not found.
- The device answers `get-config` / `get-schema` fine — only ODL chokes.
- A *different* device with an internally consistent bundle mounts clean on the **same** ODL.

## Root cause (two typical inconsistencies)
- **Phantom built-in module** — libyang ships some IETF built-ins (e.g. `ietf-yang-types`)
  *in memory, no file on disk*, and a stripped copy can be **missing a typedef** another module
  imports. ODL's `get-schema` for that module fails → a cascade of unavailable modules.
- **Unqualified import across mixed revisions** — a module imports another **without a
  `revision-date`**, while the installed dependency is a different revision. libyang binds it; ODL
  refuses → the importing module (and its data) stays unavailable.

## Variables (fill in for your environment)
```bash
NETCONF_CTR=<netconf-container>          # the netopeer2/sysrepo container (or host)
SERVED=/etc/sysrepo/yang                 # device served-YANG dir (what get-schema returns)
SDNR_HOST=<sdnr-host>                     # host running ODL/SDNR
SDNC_POD=<sdnc-pod>; SDNR_NS=<namespace> # ODL pod + k8s namespace
ODL_CACHE=/opt/opendaylight/cache/schema # ODL on-disk schema cache
ODL_AUTH='-u <user>:<password>'
RESTCONF="http://$SDNR_HOST:<restconf-port>/rests/data"
```

## Fix

### A. Supply a real file for the phantom module (with the missing typedef)
Put a real `ietf-yang-types@<rev>.yang` (containing the needed typedef, e.g. `time-with-zone-offset`)
in **both** the served dir and the ODL cache:
```bash
# device served dir:
sudo docker exec -i $NETCONF_CTR sh -c "cat > $SERVED/ietf-yang-types@<rev>.yang" < ietf-yang-types@<rev>.yang
sudo docker exec $NETCONF_CTR grep -c time-with-zone-offset $SERVED/ietf-yang-types@<rev>.yang   # >= 1
# ODL cache:
scp ietf-yang-types@<rev>.yang ubuntu@$SDNR_HOST:/tmp/
ssh ubuntu@$SDNR_HOST "sudo kubectl cp /tmp/ietf-yang-types@<rev>.yang $SDNR_NS/$SDNC_POD:$ODL_CACHE/ietf-yang-types@<rev>.yang"
```

### B. Pin the unqualified import to the installed revision
```bash
F="$SERVED/<importing-module>@<rev>.yang"
sudo docker exec $NETCONF_CTR cat "$F" > /tmp/mod.yang
python3 - <<'PY'
import re
f="/tmp/mod.yang"; s=open(f).read()
if "revision-date <installed-rev>" not in s:
    s=re.sub(r"(import <dependency-module>\s*\{\s*prefix\s+<p>\s*;)",
             r"\1\n    revision-date <installed-rev>;", s, count=1)
    open(f,"w").write(s); print("patched")
else: print("already patched")
PY
cat /tmp/mod.yang | sudo docker exec -i $NETCONF_CTR sh -c "cat > $F"
```
Resulting import block:
```yang
import <dependency-module> { prefix <p>; revision-date <installed-rev>; }
```

### C. Confirm the device serves the corrected module (this is what ODL fetches)
```bash
python3 - <<'PY'
from ncclient import manager
m=manager.connect(host='localhost',port=830,username='root',password='root',
                  hostkey_verify=False,allow_agent=False,look_for_keys=False,timeout=10)
print('served pin present:',
      'revision-date <installed-rev>' in str(m.get_schema('<importing-module>',version='<rev>')))
m.close_session()
PY
```

### D. Copy the patched module into ODL's cache, then restart SDNR
```bash
scp /tmp/mod.yang ubuntu@$SDNR_HOST:/tmp/mod.yang
ssh ubuntu@$SDNR_HOST "sudo kubectl cp /tmp/mod.yang $SDNR_NS/$SDNC_POD:$ODL_CACHE/<importing-module>@<rev>.yang"
# ODL caches schema in memory; the pod must restart to re-read the files (shared infra — coordinate):
ssh ubuntu@$SDNR_HOST "sudo kubectl delete pod $SDNC_POD -n $SDNR_NS --wait=false"
# wait until the pod is 1/1 Running, then re-mount the device if needed.
```

## Verify
```bash
curl -s -w '\nHTTP %{http_code}\n' $ODL_AUTH \
  "$RESTCONF/network-topology:network-topology/topology=topology-netconf/node=<device>/yang-ext:mount/<path-to-subtree>"
```
Expect **HTTP 200** with the real attributes. The only `unavailable-capabilities` left should be
non-functional plumbing modules (NETCONF-server / datastore helpers) — none carry your data.

## Durability
The file edits live only in the running device + ODL cache — **lost** if the NETCONF device is
recreated from its image or the ODL cache is wiped. Permanent fix: **bake the corrected,
revision-consistent YANG files into the device image** (its YANG download/build step), so every
served module pins an installed revision and no phantom built-in lacks a required typedef.
