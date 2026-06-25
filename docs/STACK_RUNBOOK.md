# OCUDU O1 + rApp + 2-UE/2-slice runbook (working setup, 2026-06-22)

Hosts: core/gNB/adapter/netconf/UEs on the **lab host**; SDNR + rApp on **192.168.4.52** (ssh pass `ubuntu`).
Anchor `cd` paths use `/home/ubuntu/...` (works as root or ubuntu; `~` breaks when you're root).

Already-running infra (keep these): OAI core (`docker compose`), `ocudu-netconf` container, rApp k8s deploy (scaled 0).
Startup order: **core → netconf → gNB → adapter → rApp → UEs**.

---

## 0. Verify infra is up
```bash
sudo docker ps --format '{{.Names}}' | grep -E 'oai-amf|ocudu-netconf'
sudo ss -ltn | grep ':830 '    # netconf datastore listening
```
If the core is down:  `cd /home/ubuntu/oai-cn5g && sudo docker compose up -d`
If netconf is down:   `sudo docker run -d --name ocudu-netconf -p 830:830 ocudu-netconf/ocudu-netconf:latest --config gnb`
  └─ then it loads EMPTY (seed schema-skew). Re-seed it: see §A below.

## 1. gNB (srsRAN, sd=1 + sd=3) — own terminal, logs on screen
```bash
cd /home/ubuntu/sushant/ocudu/build/apps/gnb
sudo ./gnb -c ../../../configs/config_o1.yaml
# wait for: "Connected to AMF" + "DU started successfully" + ZMQ on :4556
```

## 2. O1 adapter — own terminal, logs on screen
```bash
cd /home/ubuntu/sushant/ocudu_o1_adapter
source /home/ubuntu/sushant/o1_venv/bin/activate
python -u src/o1_adapter.py --netconf_host localhost --netconf_port 830 --profile gnb \
  --ws_host localhost --ws_port 8001 -c /tmp/adapter_o1_config.yaml --loglevel INFO
# wait for: "Connected to WebSocket server" + "Connected to NETCONF server" + "Subscribed"
```

## 3. Push RRM baseline (min=0 on both slices) to the fresh gNB
The adapter only pushes on a datastore *change*, so nudge sd=3's min to force it:
```bash
/home/ubuntu/sushant/o1_venv/bin/python - <<'PY'
from ncclient import manager; import time
def setmin(m,v):
    cfg=f'''<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><ManagedElement xmlns="urn:3gpp:sa5:_3gpp-common-managed-element"><id>ran1</id>
      <GNBDUFunction xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-gnbdufunction"><id>du1</id><NRCellDU xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-nrcelldu"><id>nrcelldu1</id>
        <RRMPolicyRatio xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-rrmpolicy"><id>rrm_policy2</id><attributes><rRMPolicyMaxRatio>100</rRMPolicyMaxRatio><rRMPolicyMinRatio>{v}</rRMPolicyMinRatio><rRMPolicyDedicatedRatio>0</rRMPolicyDedicatedRatio></attributes></RRMPolicyRatio>
      </NRCellDU></GNBDUFunction></ManagedElement></config>'''
    return 'ok' in str(m.edit_config(target='running',config=cfg))
m=manager.connect(host='localhost',port=830,username='root',password='root',hostkey_verify=False,allow_agent=False,look_for_keys=False,timeout=15)
setmin(m,1); time.sleep(3); print("min=0 pushed:",setmin(m,0)); m.close_session()
PY
# verify on gNB:  sudo grep -c "RRM policy update completed" /home/ubuntu/sushant/ocudu/build/apps/gnb/gnb.log
```

## 4. rApp (on 192.168.4.52)
```bash
ssh ubuntu@192.168.4.52 'sudo kubectl scale deployment/ransliceassurance -n nonrtric --replicas=1'
ssh ubuntu@192.168.4.52 'sudo kubectl logs -n nonrtric -l app=ransliceassurance -f'
# build after editing Go src:  cd ~/shivam/ransliceassurance/newrapp && go build -o ransliceassurance . && sudo kubectl rollout restart deployment/ransliceassurance -n nonrtric
```

## 5. Two UEs (sd=1 + sd=3) via the ZMQ proxy — own terminal
```bash
cd /home/ubuntu/jishan/openairinterface5g/multi_ue_sim
sudo bash run_2ue_2slice.sh
# watch attach (look for: oaitun_ue1 ... IPv4):
tail -f logs/ue1_2slice.log    # UE1 sd=1 -> 10.0.0.x
tail -f logs/ue2_2slice.log    # UE2 sd=3 -> 10.0.3.x
tail -f logs/proxy_2slice.log
# stop UEs+proxy+namespaces:
sudo bash run_2ue_2slice.sh --stop
```
Notes: UE2 = sd=3 (IMSI ...003 is SM-subscribed to sd3). CPU pins set for 12 cores (proxy=11/UE1=9-10/UE2=7-8). Needs `pyzmq`+`numpy` (already installed).

## 6. Check / measure
```bash
# UEs registered:        sudo docker logs --tail 30 oai-amf | grep 5GMM-REGISTERED
# UE IPs:                for u in 1 2; do sudo ip netns exec ue$u ip -4 -o addr show oaitun_ue1; done
# UE data path:          sudo ip netns exec ue1 ping -I oaitun_ue1 -c3 8.8.8.8
# gNB per-UE scheduling: sudo grep 'rnti=0x460' /home/ubuntu/sushant/ocudu/build/apps/gnb/gnb.log | tail
# datastore RRM read-back:
/home/ubuntu/sushant/o1_venv/bin/python -c "from ncclient import manager;import re;m=manager.connect(host='localhost',port=830,username='root',password='root',hostkey_verify=False,allow_agent=False,look_for_keys=False);x=str(m.get_config(source='running'));m.close_session();print([ (a,b) for a,b in re.findall(r'<sd>(00:00:0[13])</sd>.*?<rRMPolicyMinRatio>(\d+)</rRMPolicyMinRatio>',x,re.S)])"
```
Traffic over the radio: each UE's default route is the veth (host bypass), so route a target via `oaitun` first:
```bash
sudo ip netns exec ue1 ip route add 192.168.70.135 dev oaitun_ue1   # ext-dn via the radio
sudo ip netns exec ue1 ping -c3 192.168.70.135
# NB: iperf3 control socket fails over this link; the rfsim link RLFs under saturating DL.
```

## Stop everything (lab host)
```bash
cd /home/ubuntu/jishan/openairinterface5g/multi_ue_sim && sudo bash run_2ue_2slice.sh --stop
sudo pkill -9 -x gnb ; pkill -9 -f o1_adapter.py
ssh ubuntu@192.168.4.52 'sudo kubectl scale deployment/ransliceassurance -n nonrtric --replicas=0'
# core + netconf containers stay up; to stop: cd /home/ubuntu/oai-cn5g && sudo docker compose down ; sudo docker rm -f ocudu-netconf
```

---

## §A. Re-seed the netconf datastore (only if it loaded empty after a container restart)
The image's baked seed is schema-skewed (`<band>` not in YANG) so a fresh container loads EMPTY. The HOST source is band-free — load it:
```bash
sudo docker cp /home/ubuntu/sushant/ocudu_netconf/configs/config_gnb.xml ocudu-netconf:/root/cfg.xml
sudo docker exec ocudu-netconf sysrepocfg --edit /root/cfg.xml --datastore running -f xml
```
Then set the two slices (sd=1, sd=3) and point PM at the rApp:
```bash
/home/ubuntu/sushant/o1_venv/bin/python - <<'PY'
from ncclient import manager
def pol(pid,sd,mn): return f'<RRMPolicyRatio xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-rrmpolicy" xmlns:xc="urn:ietf:params:xml:ns:netconf:base:1.0" xc:operation="replace"><id>{pid}</id><attributes><resourceType>PRB</resourceType><rRMPolicyMemberList><mcc>001</mcc><mnc>01</mnc><sst>1</sst><sd>{sd}</sd></rRMPolicyMemberList><rRMPolicyMaxRatio>100</rRMPolicyMaxRatio><rRMPolicyMinRatio>{mn}</rRMPolicyMinRatio><rRMPolicyDedicatedRatio>0</rRMPolicyDedicatedRatio></attributes></RRMPolicyRatio>'
rrm=f'<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" xmlns:xc="urn:ietf:params:xml:ns:netconf:base:1.0"><ManagedElement xmlns="urn:3gpp:sa5:_3gpp-common-managed-element"><id>ran1</id><GNBDUFunction xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-gnbdufunction"><id>du1</id><NRCellDU xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-nrcelldu"><id>nrcelldu1</id>{pol("rrm_policy1","00:00:01",0)}{pol("rrm_policy2","00:00:03",0)}</NRCellDU></GNBDUFunction></ManagedElement></config>'
# PerfMetricJob: NOTE no explicit xmlns on PerfMetricJob (inherits GNBCUCPFunction ns)
pm='<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" xmlns:xc="urn:ietf:params:xml:ns:netconf:base:1.0"><ManagedElement xmlns="urn:3gpp:sa5:_3gpp-common-managed-element"><id>ran1</id><GNBCUCPFunction xmlns="urn:3gpp:sa5:_3gpp-nr-nrm-gnbcucpfunction"><id>cucp1</id><PerfMetricJob xc:operation="replace"><id>defaulttrace</id><attributes><administrativeState>UNLOCKED</administrativeState><performanceMetrics>DlUeThroughput_Cell</performanceMetrics><performanceMetrics>UlUeThroughput_Cell</performanceMetrics><granularityPeriod>1</granularityPeriod><streamTarget>http://192.168.4.52:8080/pm</streamTarget></attributes></PerfMetricJob></GNBCUCPFunction></ManagedElement></config>'
m=manager.connect(host='localhost',port=830,username='root',password='root',hostkey_verify=False,allow_agent=False,look_for_keys=False,timeout=15)
print("RRM:", 'ok' in str(m.edit_config(target='running',config=rrm)))
print("PM->rApp:", 'ok' in str(m.edit_config(target='running',config=pm))); m.close_session()
PY
```
