# OCUDU O1 Adapter

## Overview

Python application acting as adapter between CU/DU towards the Service Management and Orchestration (SMO).
The application currently supports:
* Configuration Management (CM),
* Fault Management (FM).

Performance Managment (PM) is currently implemented through a Json-based metrics service exposed over the
Websocket interface of OCUDU.

In a Kubernetes deployment the O1-Adapter is supposed to be running as a sidecar container next to the e.g.
OCUDU DU container to control the config file generation/provisioning and livecycle of the Pod through a shared volume and REST-based API, respectively.

## Installation

Using `requirements.txt` or on Ubuntu with `sudo apt-get install python3-ncclient python3-flask python3-xmltodict python3-websockets python3-deepdiff`

Within a python virtual environment, install the requirements with `pip install -r requirements.txt`

## Operation

Upon start the application attempts to connect to a Config datastore over SSH/netconf. If it succeeds an initial configuration file will be genereated using the `running` datastore.

## Manual Execution

Make sure a Netconf server is running an can be reached over `localhost` on port `830` (those defaults can be changed over the command line). Run the app in one console:

`$ python3 src/o1_adapter`

The application will listen on all available network interfaces including `localhost`.

In another console use `curl` to check the config healthiness:

`$ curl -i http://localhost:5000/config-healthy`

It should report the config to be healthy.

Make a config modifcation to the `running` datastore and rerun the command. It should now report `Error code 400` config unhealthy. This status can be used to trigger a restart of the DU container application.

To reset the health status send the following command to the server:

`$ curl -H 'Content-Type: application/json' -d '{ "restarted": True}' -X POST http://localhost:5000/restarted`

An example Docker and k8s integration is provided.

### Adapter component profiles

The adapter ships templates for the following gNB split components: `gnb.yaml`, `cu.yaml`, `cucp.yaml`, `cuup.yaml` and `du.yaml`. Select one with `--profile`, e.g.:

```
$ python3 src/o1_adapter.py --profile cucp
```

`--profile` defaults to `gnb` and selects `<profile>.yaml` as the rendering template. Pass `--template <file>` to override the template explicitly. The special value `--profile ru` skips YAML rendering entirely and only forwards the raw NETCONF config downstream.

### RU controller within O1 Adapter

With the option `--ru_forward` the O1 Adapter automatically forwards configuration updates from the NETCONF server of the DU to the NETCONF server of the RU. In this case it has to be ensured, that two NETCONF servers are reachable. One represents the NETCONF server on the DU and the other NETCONF server runs on the RU. Then the forwarding can be achieved by the following command:

```
$ python3 src/o1_adapter.py --netconf_host <DU-IP-ADDRESS> --netconf_username <DU-NETCONF-USERNAME> --netconf_password <DU-USER-PASSWORD> --ru_forward --ru_netconf_host <RU-IP-ADDRESS> --ru_netconf_username <RU-NETCONF-USERNAME> --ru_netconf_password <RU-USER-PASSWORD>
```

## RU controller

The RU controller is a stand-alone application to configure an O-RU over Mplane.
Taking an example RU that exposes it's Netconf interface over `10.10.0.100` using `admin/admin` as login credentials for example, we can retrieve the current RU config with:

```
$ ./ru_controller.py --host=10.10.0.100 -u admin -p admin -d running --get_config
```

We can activate the carrier with:

```
$ ./ru_controller.py --host=10.10.0.100 -u admin -p admin -d running --tx_gain=26.0 --activate_carriers --carrier_state ACTIVE
```

Run a full RU configuration with:

```
$ ./ru_controller.py --host=10.10.0.100 -u admin -p admin -d running --set_full_config --ru_mac_addr=00:a0:0a:01:a4:42 --vlan=127 --du_mac_addr=9c:69:b4:66:cd:48 --iq_bitwidth=9  --compression_type=STATIC --rf_bandwidth_hz=100000000 --dl_arfcn=649980 --dl_freq=3749700000 --tx_gain=39 --ul_arfcn=649980 --ul_freq=3749700000 --carrier_state ACTIVE
```

Note: Full configuration has only been verified for a subset of configuration, e.g. with TDD 100MHz, PRACH format B4.

## Network Slice Assurance

This fork extends the base O1 adapter to drive a **RAN slice-assurance closed loop** between the
OCUDU gNB and an SMO (ONAP SDNR). The main additions:

* **SDNR RRM closed loop** — the `ransliceassurance` rApp reads and writes `RRMPolicyRatio`
  (TS 28.541) on the gNB through SDNR/RESTCONF; the adapter translates the resulting NETCONF
  config change into the gNB's `rrm_policy_ratio_set` WebSocket command. See
  [docs/PHASE3_SDNR_RRM_CLOSED_LOOP.md](docs/PHASE3_SDNR_RRM_CLOSED_LOOP.md).

* **Multi-slice RRM** — `config_manager` now renders `RRMPolicyRatio` **per slice**: it extracts a
  list of per-slice policies and sends one `rrm_policy_ratio_set` command per slice, since the gNB
  parses one policy per command and merges it into the matching slice by S-NSSAI. This lets the
  rApp arbitrate PRBs across multiple contending slices (e.g. boost a premium slice's
  `rRMPolicyMinRatio` under contention).

* **Per-slice PM** — performance metrics are aggregated per `(SST, SD)` so the SMO/rApp can act on
  per-slice throughput, and the PM envelope carries the 3GPP cell DN for SDNR correlation.

* **Documentation** — see [docs/](docs/):
  * [STACK_RUNBOOK.md](docs/STACK_RUNBOOK.md) — bring up core + gNB + UEs + netconf + adapter + rApp.
  * [SDNR_YANG_COMPAT_FIX.md](docs/SDNR_YANG_COMPAT_FIX.md) /
    [ODL_SDNR_NETCONF_YANG_COMPAT.md](docs/ODL_SDNR_NETCONF_YANG_COMPAT.md) — making the device's
    YANG bundle mount cleanly in OpenDaylight/SDNR (specific + general).
  * [O1_ADAPTERS_CALLFLOW.md](O1_ADAPTERS_CALLFLOW.md) — component call flows (with diagrams under
    [docs/callflow/](docs/callflow/)).
  * [SESSION_HANDOVER.md](docs/SESSION_HANDOVER.md) — running context for the integration work.

## License

This project is licensed under the BSD 3-Clause Open MPI variant License – see the [LICENSE](./LICENSE) file for details.
