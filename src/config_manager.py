# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""This module provides a ConfigManager class to manage and update the configuration of a gNB / CU / DU."""

import asyncio
import logging
from datetime import datetime, timezone

import ncclient
import xmltodict
from deepdiff import DeepDiff
from jinja2 import Environment, exceptions as jinja2_exceptions, FileSystemLoader

from remote_commands import WsRemoteCommands
from state import AppState

WORKER_SLEEP_INTERVAL = 1  # seconds


# pylint: disable=too-many-instance-attributes,logging-fstring-interpolation,too-many-arguments,too-many-positional-arguments
class ConfigManager:
    """
    ConfigManager is responsible for managing and updating the configuration of a gNB / CU / DU.
    It retrieves the configuration, detects changes, and updates the runtime configuration
    or triggers a full restart if necessary. The updated configuration is written to a file.

    Attributes:
        netconf_manager (NetconfManager): The NETCONF manager instance for retrieving configurations.
        datastore (str): The datastore source for the NETCONF configuration.
        output_filename (str): The filename where the full configuration will be written.
        template_filename (str): The filename of the template used for rendering the configuration file.
    """

    _RUNTIME_UPDATABLE_PARAMS = ["ssb_block_power_dbm", "RRMPolicyRatio", "PerfMetricJob"]
    _FULL_RESTART_TIMEOUT = 30

    def __init__(self, state: AppState, netconf_manager, datastore, output_filename, template_filename, ru_forward_enabled, profile="gnb"):
        # execute the base constructor
        self.netconf_manager = netconf_manager
        self.datastore = datastore
        self.output_filename = output_filename
        self.template_filename = template_filename
        self.last_config = None
        self.state = state
        self._ws = WsRemoteCommands(state.ws_send_queue)  # Inject shared WS send queue
        self._ru_forward_enabled = ru_forward_enabled
        self._render_enabled = profile != "ru"

    async def process_config_update(self):
        """
        Retrieve full config
        """
        raw_xml = self.netconf_manager.get_config(source=self.datastore).data_xml
        raw_config = xmltodict.parse(raw_xml)

        # Create diff between old and new config
        diff = DeepDiff(self.last_config, raw_config, ignore_order=True)
        if not diff:
            logging.debug("No config change detected")
            return

        # RU profile: forward raw config only, skip gNB-shaped diff classification.
        if not self._render_enabled:
            self.write_full_config(raw_config, raw_xml)
            return

        # Check if only runtime updatable parameters changed
        runtime_updatable = True
        logging.debug(f"Config change detected: {diff}")
        try:
            for _, change in diff.items():
                for key, _ in change.items():
                    param_found = False
                    for item in self._RUNTIME_UPDATABLE_PARAMS:
                        if item in key:
                            param_found = True
                            break
                    if not param_found:
                        runtime_updatable = False
                        break

        except (AttributeError, TypeError) as e:
            logging.debug(f"Couldn't determine if parameters can be updated: {e}")
            runtime_updatable = False

        if runtime_updatable:
            logging.debug("Only runtime updatable parameters changed, no need to restart application")
            self.update_runtime_config(raw_config, diff)
            self.write_full_config(raw_config, raw_xml)
        else:
            logging.debug("Full restart needed, sending quit cmd")
            await self._full_restart()
            # _full_restart blocks until WS reconnects; netconf may have changed meanwhile, so refetch.
            self.write_full_config(None)

    async def _full_restart(self):
        if not self._ws.send_quit_command():
            logging.warning("Failed to send quit command")
            self.state.restart_req = True
        else:
            # Mark restart requested immediately for health endpoint consumers.
            self.state.restart_req = True
            logging.info("Waiting for the reboot to complete ..")
            start_time = asyncio.get_running_loop().time()
            while not self.state.session_state.get("ws_connected"):
                if asyncio.get_running_loop().time() - start_time > self._FULL_RESTART_TIMEOUT:
                    logging.warning("Timed out waiting for WebSocket reconnect")
                    self.state.restart_req = True
                    return
                await asyncio.sleep(1)

    def update_runtime_config(self, raw_config, diff):
        """
        Updates the runtime configuration of the DU cell.

        Args:
            raw_config (dict): The raw configuration data.
            diff (DeepDiff): The differences between the old and new configuration.
        """
        if "ssb_block_power_dbm" in str(diff):
            logging.info("Updating SSB power")

            nc_du_cell_config = self.get_du_cell_config(raw_config)

            # Only extract runtime-updateable values
            du_cell_config = []
            for cell in nc_du_cell_config:
                try:
                    nc_cell_extension = cell["attributes"]["ocudu_nrcelldu_extensions"]
                except KeyError as e:
                    logging.warning(f"Couldn't extract OCUDU nrcelldu config extensions: {e}")

                # build DU cell struct to overwrite common cell_cfg fields with individual values
                new_du_cell = {}
                try:
                    for key, value in nc_cell_extension["ocudu_nrcelldu_ssb_extensions"].items():
                        value = int(value) if value.isnumeric() else value
                        new_du_cell[key] = int(value)
                except KeyError as e:
                    logging.warning(f"Couldn't extract OCUDU SSB config extensions: {e}")

                # Extract cell-specific values from standard attributes
                try:
                    new_du_cell["plmn"] = (
                        cell["attributes"]["pLMNInfoList"]["mcc"] + cell["attributes"]["pLMNInfoList"]["mnc"]
                    )
                    new_du_cell["nci"] = int(
                        raw_config["data"]["ManagedElement"]["GNBDUFunction"]["attributes"]["gNBId"]
                        + cell["attributes"]["cellLocalId"]
                    )
                except KeyError as e:
                    logging.warning(f"Couldn't extract PLMN and NCI from GNBDUFunction attributes: {e}")
                du_cell_config.append(new_du_cell)

            # Send SSB update command
            self._ws.send_ssb_command(du_cell_config)

        if "RRMPolicyRatio" in str(diff):
            rrm_policies = self._extract_rrm_policy_ratio_config(raw_config)
            logging.info(f"Updating RRM policy ({len(rrm_policies)} slice(s))")
            # The gNB parses one policy per rrm_policy_ratio_set command and merges it into the
            # matching slice by S-NSSAI, so send one command per slice policy.
            for policy in rrm_policies:
                self._ws.send_rrm_policy_ratio_command(policy)

    def get_du_cell_config(self, raw_config):
        """
        Extracts the full set of configuration parameters for each DU cell from the raw NETCONF configuration.

        Args:
            raw_config (dict): The raw NETCONF configuration data.

        Returns:
            list: A list of configuration parameters for each DU cell.
        """
        nc_du_cell_config = []
        try:
            if isinstance(raw_config["data"]["ManagedElement"]["GNBDUFunction"]["NRCellDU"], list):
                for cell in raw_config["data"]["ManagedElement"]["GNBDUFunction"]["NRCellDU"]:
                    nc_du_cell_config.append(cell)
            else:
                nc_du_cell_config.append(raw_config["data"]["ManagedElement"]["GNBDUFunction"]["NRCellDU"])
        except KeyError as e:
            logging.info(f"Couldn't extract GNBDUFunction/NRCellDU config: {e}")

        for cell in nc_du_cell_config:
            logging.debug(cell)

        return nc_du_cell_config

    # pylint: disable=too-many-locals
    def write_full_config(self, raw_config, raw_xml=None):
        """
        Writes the full configuration to a file based on the provided raw configuration.

        Args:
            raw_config (dict): The raw configuration data.
            raw_xml (str, optional): The raw NETCONF XML payload.
        """
        if raw_config is None:
            if raw_xml is None:
                raw_xml = self.netconf_manager.get_config(source=self.datastore).data_xml
            raw_config = xmltodict.parse(raw_xml)

        logging.debug(f"RAW xml:\n{raw_config}")

        # Store config
        self.last_config = raw_config

        prev_active = self._active_stream_ids(self.state.pm_jobs)
        self.state.pm_jobs = self._extract_perf_metric_jobs(raw_config)
        # Newly-active jobs need a fresh snapshot from the gNB (one-shot on subscribe).
        if self._active_stream_ids(self.state.pm_jobs) - prev_active:
            self._ws.send_metrics_subscribe()

        if not self._render_enabled:
            self._enqueue_ru_forward_update(raw_xml)
            return True

        ofh_cell_config, du_cell_config = self._extract_cells_config(raw_config)
        cell_config = self._extract_cell_config(raw_config, du_cell_config)
        cucp_config = self._extract_cucp_config(raw_config, du_cell_config)
        cuup_config = self._extract_cuup_config(raw_config)

        # Function extensions (DU/CU-CP/CU-UP). testmode/hal/remote_control live only on
        # the DU; log lives on any function (first-wins); pcap entries are merged across
        # all functions; metrics_extensions is now per-function and gets merged
        testmode_config = {"enabled": False}
        log_config = {}
        hal_config = {}
        metrics_config = {}
        remote_control_config = {}
        pcap_config = {}

        managed_element = raw_config.get("data", {}).get("ManagedElement", {})
        for func_key, ext_key in (
            ("GNBDUFunction", "ocudu_gnbdufunction_extensions"),
            ("GNBCUCPFunction", "ocudu_gnbcucpfunction_extensions"),
            ("GNBCUUPFunction", "ocudu_gnbcuupfunction_extensions"),
        ):
            try:
                ext = managed_element[func_key][ext_key]
            except (KeyError, TypeError):
                continue

            testmode = ext.get("ocudu_gnbdufunction_testmode_extensions")
            if testmode is not None:
                testmode_config = testmode
            hal = ext.get("ocudu_hal_extensions")
            if hal is not None:
                hal_config = hal
            remote = ext.get("ocudu_remote_control_extensions")
            if remote is not None:
                remote_control_config = remote

            if not log_config:
                log = ext.get("ocudu_log_extensions")
                if log is not None:
                    log_config = log

            metrics = ext.get("ocudu_metrics_extensions")
            if metrics is not None:
                for key, value in metrics.items():
                    if key in ("periodicity", "layers") and isinstance(value, dict):
                        metrics_config.setdefault(key, {}).update(value)
                    else:
                        metrics_config[key] = value

            pcap = ext.get("ocudu_pcap_extensions") or {}
            pcap_config.update(pcap)

        # Render config file
        try:
            environment = Environment(loader=FileSystemLoader("templates/"))
            template = environment.get_template(self.template_filename)
            content = template.render(
                ofh_cells=ofh_cell_config,
                du_cells=du_cell_config,
                cucp_config=cucp_config,
                cuup_config=cuup_config,
                testmode_config=testmode_config,
                log_config=log_config,
                hal_config=hal_config,
                metrics_config=metrics_config,
                remote_control_config=remote_control_config,
                pcap_config=pcap_config,
                cell_config=cell_config,
            )
        except jinja2_exceptions.UndefinedError as e:
            logging.error(f"Template rendering error: {e}")
            return False

        with open(self.output_filename, mode="w", encoding="utf-8") as message:
            message.write(content)
            logging.info(f"Generating {self.output_filename}")
            logging.debug(f"Generated config:\n{content}")

        self._enqueue_ru_forward_update(raw_xml)

        return True

    def _enqueue_ru_forward_update(self, raw_xml):
        """Queue latest NETCONF config for RU forwarding."""
        if not self._ru_forward_enabled or raw_xml is None:
            return

        dropped = 0
        while not self.state.ru_update_queue.empty():
            try:
                self.state.ru_update_queue.get_nowait()
                self.state.ru_update_queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break

        self.state.ru_update_queue.put_nowait(raw_xml)
        if dropped:
            logging.debug(f"Dropped {dropped} stale RU forwarding update(s)")
        logging.debug("Queued RU forwarding update")

    @staticmethod
    def _active_stream_ids(jobs: dict) -> set:
        return {
            jid for jid, job in jobs.items()
            if job.get("administrativeState") == "UNLOCKED" and job.get("streamTarget")
        }

    @staticmethod
    def _extract_perf_metric_jobs(raw_config) -> dict:
        """Walk ManagedElement/<NF>/PerfMetricJob and return id -> normalised job dict
        (administrativeState, performanceMetrics list, granularityPeriod int, streamTarget,
        nf_key, nf_instance_id, plmn_id)."""
        jobs: dict = {}
        managed_element = raw_config.get("data", {}).get("ManagedElement", {}) or {}
        nf_instance_id = managed_element.get("@id") or managed_element.get("id") or "unknown"

        # 3GPP Distinguished Name of the DU cell, used to address the RRMPolicyRatio over
        # SDNR/RESTCONF (…/ManagedElement=<meId>/GNBDUFunction=<gnbduId>/NRCellDU=<nrCellDuId>).
        # Per-slice throughput is a DU/cell metric and the RRMPolicyRatio lives under NRCellDU,
        # so every PM envelope carries the DU DN regardless of which NF hosts the PerfMetricJob
        # (e.g. the job may sit under GNBCUCPFunction). Sourced from the GNBDUFunction.
        du_gnbdu = managed_element.get("GNBDUFunction") or {}
        if isinstance(du_gnbdu, list):
            du_gnbdu = du_gnbdu[0] if du_gnbdu else {}
        du_gnbdu_id = du_gnbdu.get("@id") or du_gnbdu.get("id")
        du_nrcelldu_id = None
        du_nrcelldu = ConfigManager._ensure_list(du_gnbdu.get("NRCellDU"))
        if du_nrcelldu:
            du_nrcelldu_id = du_nrcelldu[0].get("@id") or du_nrcelldu[0].get("id")

        for nf_key in ("GNBDUFunction", "GNBCUCPFunction", "GNBCUUPFunction"):
            nf = managed_element.get(nf_key)
            if not nf:
                continue
            plmn_attrs = (nf.get("attributes", {}) or {}).get("pLMNId", {}) or {}
            plmn_id = (plmn_attrs.get("mcc") or "") + (plmn_attrs.get("mnc") or "")
            for entry in ConfigManager._ensure_list(nf.get("PerfMetricJob")):
                job_id = entry.get("id")
                if not job_id:
                    continue
                attrs = entry.get("attributes", {}) or {}
                try:
                    granularity = int(attrs.get("granularityPeriod", 1))
                except (TypeError, ValueError):
                    granularity = 1
                jobs[job_id] = {
                    "administrativeState": attrs.get("administrativeState", "LOCKED"),
                    "performanceMetrics": ConfigManager._ensure_list(attrs.get("performanceMetrics")),
                    "granularityPeriod": granularity,
                    "streamTarget": attrs.get("streamTarget"),
                    "nf_key": nf_key,
                    "nf_instance_id": nf_instance_id,
                    "plmn_id": plmn_id,
                    "me_id": nf_instance_id,
                    "gnbdu_id": du_gnbdu_id,
                    "nrcelldu_id": du_nrcelldu_id,
                }
        return jobs

    def _extract_cucp_config(self, raw_config, du_cells=None):
        cucp_config = {}
        try:

            nc_cucp_config = raw_config["data"]["ManagedElement"]["GNBCUCPFunction"]
            logging.debug(nc_cucp_config)

            plmn = nc_cucp_config["attributes"]["pLMNId"]["mcc"] + nc_cucp_config["attributes"]["pLMNId"]["mnc"]

            tac = 7  # Not present in default YANG model it seems
            if du_cells is not None:
                for cell in du_cells:
                    if cell["plmn"] == plmn:
                        tac = cell["tac"]
                        break

            tai_slice_support_list = [{"sst": 1}]  # Default if RRMPolicyRatio is absent
            try:
                nc_rrm_members = nc_cucp_config["RRMPolicyRatio"]["attributes"]["rRMPolicyMemberList"]
                if not isinstance(nc_rrm_members, list):
                    nc_rrm_members = [nc_rrm_members]
                tai_slice_support_list = []
                for member in nc_rrm_members:
                    tai_slice_support_list.append(
                        {"sst": int(member["sst"]), "sd": self._parse_sd(member["sd"])}
                    )
            except (KeyError, ValueError) as e:
                logging.warning(f"Couldn't extract tai_slice_support_list from GNBCUCPFunction RRMPolicyRatio: {e}")

            supported_tracking_areas = [
                {
                    "tac": tac,
                    "plmn_list": [
                        {
                            "plmn": plmn,
                            "tai_slice_support_list": tai_slice_support_list,
                        }
                    ],
                }
            ]

            # Build AMF config subtree
            cucp_config = {
                "amf": {
                    "addrs": nc_cucp_config["EP_NgC"]["attributes"]["remoteAddress"],
                    "bind_addrs": nc_cucp_config["EP_NgC"]["attributes"]["localAddress"]["ipAddress"],
                    "supported_tracking_areas": supported_tracking_areas,
                }
            }

            for ep, key in (("EP_E1", "e1ap"), ("EP_F1C", "f1ap")):
                try:
                    cucp_config[key] = {
                        "bind_addrs": nc_cucp_config[ep]["attributes"]["localAddress"]["ipAddress"]
                    }
                except KeyError:
                    pass
        except KeyError as e:
            logging.warning(f"Couldn't extract CU-CP config: {e}")

        try:
            mobility = self._extract_cucp_mobility_config(raw_config)
            if mobility:
                cucp_config["mobility"] = mobility
        except (KeyError, ValueError, TypeError) as e:
            logging.warning(f"Couldn't extract CU-CP mobility config: {e}")

        return cucp_config

    def _extract_cucp_mobility_config(self, raw_config):
        """Extract cu_cp.mobility from the NETCONF tree.

        Returns a dict mirroring the gNB YAML mobility block, or None if no
        mobility extension data is present.
        """
        try:
            nc_cucp = raw_config["data"]["ManagedElement"]["GNBCUCPFunction"]
        except KeyError:
            return None

        mob_ext = nc_cucp.get("ocudu_gnbcucpfunction_mobility_extensions")
        cucp_attrs = nc_cucp["attributes"]

        # NCI = (gNBId << (36 - gNBIdLength)) | cellLocalId per 3GPP TS 38.300.
        gnb_id = int(cucp_attrs["gNBId"])
        gnb_id_length = int(cucp_attrs["gNBIdLength"])
        cell_id_shift = 36 - gnb_id_length

        nrcellcu_list = self._ensure_list(nc_cucp.get("NRCellCU"))

        nci_by_id = {}
        for nrcellcu in nrcellcu_list:
            local_id = int(nrcellcu["attributes"]["cellLocalId"])
            nci_by_id[str(nrcellcu["id"])] = (gnb_id << cell_id_shift) | local_id

        report_configs = []
        if mob_ext:
            for rc in self._ensure_list(mob_ext.get("report_configs")):
                entry = {}
                for key in (
                    "report_cfg_id",
                    "report_type",
                    "periodic_ho_rsrp_offset_db",
                    "event_triggered_report_type",
                    "meas_trigger_quantity",
                    "meas_trigger_quantity_threshold_db",
                    "meas_trigger_quantity_threshold_2_db",
                    "meas_trigger_quantity_offset_db",
                    "hysteresis_db",
                    "time_to_trigger_ms",
                    "t312",
                    "distance_thresh_from_ref1_km",
                    "distance_thresh_from_ref2_km",
                    "hysteresis_location_km",
                    "ref_location1",
                    "ref_location2",
                    "t1_thres",
                    "duration_s",
                    "report_interval_ms",
                ):
                    if key in rc:
                        value = rc[key]
                        if key == "t1_thres":
                            value = self._t1_thres_to_unix_ms(value)
                        else:
                            value = self._coerce_xml_value(value)
                        entry[key] = value
                if entry:
                    report_configs.append(entry)

        mobility_cells = []
        for nrcellcu in nrcellcu_list:
            nci = nci_by_id.get(str(nrcellcu.get("id")))
            if nci is None:
                continue

            cellcu_mob = nrcellcu.get("attributes", {}).get("ocudu_nrcellcu_mobility_extensions") or {}
            relations = self._ensure_list(nrcellcu.get("NRCellRelation"))

            cell_entry = {"nr_cell_id": f"0x{nci:x}"}
            if "periodic_report_cfg_id" in cellcu_mob:
                cell_entry["periodic_report_cfg_id"] = int(cellcu_mob["periodic_report_cfg_id"])

            ncells = []
            for rel in relations:
                rel_attrs = rel.get("attributes", {})
                # Resolve the adjacentNRCellRef DN (e.g. "...,NRCellCU=nrcellcu2") to the target's NCI.
                dn = rel_attrs.get("adjacentNRCellRef") or ""
                target_nci = None
                for component in str(dn).split(","):
                    stripped = component.strip()
                    if stripped.startswith("NRCellCU="):
                        target_nci = nci_by_id.get(stripped.split("=", 1)[1].strip())
                        break
                if target_nci is None:
                    continue
                ncell = {"nr_cell_id": f"0x{target_nci:x}"}
                refs = rel_attrs.get("ocudu_nrcellrelation_mobility_extensions", {}).get(
                    "report_config_refs"
                )
                if refs is not None:
                    ncell["report_configs"] = [int(r) for r in self._ensure_list(refs)]
                ncells.append(ncell)
            if ncells:
                cell_entry["ncells"] = ncells

            if "periodic_report_cfg_id" in cell_entry or "ncells" in cell_entry:
                mobility_cells.append(cell_entry)

        mobility = {}
        if mob_ext and "trigger_handover_from_measurements" in mob_ext:
            mobility["trigger_handover_from_measurements"] = str(
                mob_ext["trigger_handover_from_measurements"]
            ).lower()
        if mob_ext and "trigger_cho_on_ue_setup" in mob_ext:
            mobility["trigger_cho_on_ue_setup"] = str(mob_ext["trigger_cho_on_ue_setup"]).lower()
        if mob_ext and "cho_timeout_ms" in mob_ext:
            mobility["cho_timeout_ms"] = int(mob_ext["cho_timeout_ms"])
        if mobility_cells:
            mobility["cells"] = mobility_cells
        if report_configs:
            mobility["report_configs"] = report_configs

        return mobility or None

    @staticmethod
    def _t1_thres_to_unix_ms(value):
        """Normalise a t1_thres value to Unix milliseconds as int."""
        # The YANG type accepts either Unix ms or an RFC 3339 timestamp; the gNB only accepts Unix ms.
        text = str(value).strip()
        if text.lstrip("-").isdigit():
            return int(text)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    @staticmethod
    def _coerce_xml_value(value):
        """Convert xmltodict scalars to YAML-friendly Python values."""
        # Recursively walk dicts/lists and turn numeric-looking strings into int/float.
        if isinstance(value, dict):
            return {key: ConfigManager._coerce_xml_value(val) for key, val in value.items()}
        if isinstance(value, list):
            return [ConfigManager._coerce_xml_value(val) for val in value]
        if isinstance(value, str):
            text = value.strip()
            if text.lstrip("-").isdigit():
                return int(text)
            try:
                return float(text)
            except ValueError:
                return value
        return value

    @staticmethod
    def _ensure_list(value):
        """Normalise xmltodict output: missing -> [], single -> [x], list -> list."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def _extract_cuup_config(self, raw_config):
        cuup_config = {}
        try:
            nc_cuup = raw_config["data"]["ManagedElement"]["GNBCUUPFunction"]
        except KeyError:
            return cuup_config

        try:
            attrs = nc_cuup["EP_E1"]["attributes"]
            cuup_config["e1ap"] = {
                "gateways": [
                    {
                        "addrs": attrs["remoteAddress"],
                        "bind_addrs": attrs["localAddress"]["ipAddress"],
                    }
                ]
            }
        except KeyError:
            pass

        for ep, key in (("EP_NgU", "ngu"), ("EP_F1U", "f1u")):
            try:
                bind = nc_cuup[ep]["attributes"]["localAddress"]["ipAddress"]
                cuup_config[key] = {"socket": [{"bind_addr": bind}]}
            except KeyError:
                pass

        return cuup_config

    @staticmethod
    def _parse_sd(sd):
        # 3GPP SD is 3 octets; YANG model encodes it as colon-separated hex bytes
        # (e.g. "ff:ff:ff"). Strip the separators before parsing as a hex integer.
        return int(sd.replace(":", ""), 16)

    @staticmethod
    def _as_list(x):
        # NETCONF/xmltodict collapses a single list entry to a dict; normalize to a list.
        if x is None:
            return []
        return x if isinstance(x, list) else [x]

    def _policy_members(self, attrs):
        # Build the gNB member list from one RRMPolicyRatio's rRMPolicyMemberList,
        # which may be a single dict (one member) or a list of dicts.
        members = []
        for m in self._as_list(attrs.get("rRMPolicyMemberList")):
            members.append(
                {
                    "plmn": m["mcc"] + m["mnc"],
                    "sst": int(m["sst"]),
                    "sd": self._parse_sd(m["sd"]),
                }
            )
        return members

    def _extract_rrm_policy_ratio_config(self, raw_config):
        # Returns a LIST of per-slice policy dicts (one per RRMPolicyRatio). The gNB merges
        # each policy into its matching slice by S-NSSAI, so the caller sends one
        # rrm_policy_ratio_set command per policy (multi-slice support).
        policies = []
        try:
            nrcelldu = raw_config["data"]["ManagedElement"]["GNBDUFunction"]["NRCellDU"]
            for policy in self._as_list(nrcelldu["RRMPolicyRatio"]):
                attrs = policy["attributes"]
                policies.append(
                    {
                        "resourceType": attrs["resourceType"],
                        "rRMPolicyMemberList": self._policy_members(attrs),
                        "min_prb_policy_ratio": int(attrs["rRMPolicyMinRatio"]),
                        "max_prb_policy_ratio": int(attrs["rRMPolicyMaxRatio"]),
                        "dedicated_ratio": int(attrs["rRMPolicyDedicatedRatio"]),
                    }
                )
        except (KeyError, ValueError) as e:
            logging.warning(f"Couldn't extract OCUDU RRM policy config: {e}")

        return policies

    def _extract_cell_config(self, raw_config, du_cells=None):
        cell_cfg = {}
        try:
            nrcelldu = raw_config["data"]["ManagedElement"]["GNBDUFunction"]["NRCellDU"]
            policies = self._as_list(nrcelldu["RRMPolicyRatio"])

            # PLMN/TAC come from the first member of the first policy.
            first_member = self._as_list(policies[0]["attributes"]["rRMPolicyMemberList"])[0]
            plmn = first_member["mcc"] + first_member["mnc"]

            tac = 7  # Not present in default YANG model it seems
            if du_cells is not None:
                for cell in du_cells:
                    if cell["plmn"] == plmn:
                        tac = cell["tac"]
                        break

            # One slicing entry per (policy, member) pair, each carrying its own ratios.
            slicing = []
            for policy in policies:
                attrs = policy["attributes"]
                for m in self._as_list(attrs["rRMPolicyMemberList"]):
                    slicing.append(
                        {
                            "sst": m["sst"],
                            "sd": self._parse_sd(m["sd"]),
                            "sched_cfg": {
                                "min_prb_policy_ratio": attrs["rRMPolicyMinRatio"],
                                "max_prb_policy_ratio": attrs["rRMPolicyMaxRatio"],
                            },
                        }
                    )

            cell_cfg = {
                "tac": tac,
                "plmn": plmn,
                "slicing": slicing,
            }

        except (KeyError, ValueError, IndexError) as e:
            logging.warning(f"Couldn't extract OCUDU RRM policy config: {e}")

        return cell_cfg

    def _extract_cells_config(self, raw_config):  # pylint: disable=too-many-branches
        # Iterate over DU cell and build extract OFH and DU config values
        ofh_cell_config = []
        du_cell_config = []
        for cell in self.get_du_cell_config(raw_config):
            try:
                nc_cell_extension = cell["attributes"]["ocudu_nrcelldu_extensions"]
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU nrcelldu config extensions: {e}")

            # Extract custom ocudu extensions
            try:
                # build OFH cell struct
                new_ofh_cell = {}
                for key, value in nc_cell_extension["ocudu_nrcelldu_ofh_extensions"].items():
                    if "compr_method" in key:
                        value = "bfp" if "BLOCK_FLOATING_POINT" in value else value
                    if "static_compr_hdr" in key:
                        value = "true" if "STATIC" in value else "false"
                    new_ofh_cell[key] = value
                ofh_cell_config.append(new_ofh_cell)
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU OFH config extensions: {e}")

            # build DU cell struct to overwrite common cell_cfg fields with individual values
            new_du_cell = {}

            try:
                ssb_fields = {}
                for key, value in nc_cell_extension["ocudu_nrcelldu_ssb_extensions"].items():
                    ssb_fields[key] = value
                new_du_cell["ssb"] = ssb_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU SSB config extensions: {e}")

            try:
                prach_fields = {}
                for key, value in nc_cell_extension["ocudu_nrcelldu_prach_extensions"].items():
                    prach_fields[key] = value
                new_du_cell["prach"] = prach_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU PRACH config extensions: {e}")

            try:
                tdd_fields = {}
                for key, value in nc_cell_extension["ocudu_nrcelldu_tdd_extensions"].items():
                    tdd_fields[key] = value
                new_du_cell["tdd_ul_dl_cfg"] = tdd_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU TDD config extensions: {e}")

            try:
                pdsch_fields = {}
                for key, value in nc_cell_extension["ocudu_nrcelldu_pdsch_extensions"].items():
                    pdsch_fields[key] = value
                new_du_cell["pdsch"] = pdsch_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU PDSCH config extensions: {e}")

            try:
                pusch_fields = {}
                for key, value in nc_cell_extension["ocudu_nrcelldu_pusch_extensions"].items():
                    pusch_fields[key] = value
                new_du_cell["pusch"] = pusch_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU PUSCH config extensions: {e}")

            try:
                pucch_fields = {}
                for key, value in nc_cell_extension["ocudu_nrcelldu_pucch_extensions"].items():
                    pucch_fields[key] = value
                new_du_cell["pucch"] = pucch_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU PUCCH config extensions: {e}")

            try:
                csi_fields = {}
                for key, value in nc_cell_extension["ocudu_nrcelldu_csi_extensions"].items():
                    csi_fields[key] = value
                new_du_cell["csi"] = csi_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU CSI config extensions: {e}")

            try:
                srs_fields = {}
                for key, value in nc_cell_extension["ocudu_nrcelldu_srs_extensions"].items():
                    srs_fields[key] = value
                new_du_cell["srs"] = srs_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU SRS config extensions: {e}")

            try:
                pdcch_common = nc_cell_extension["ocudu_nrcelldu_pdcch_extensions"]["common"]
                # Emit pdcch.common as a YAML flow mapping so the template's 2-level loop renders correctly.
                new_du_cell["pdcch"] = {"common": "{ss1_n_candidates: " + pdcch_common["ss1_n_candidates"] + "}"}
            except (KeyError, TypeError) as e:
                logging.warning(f"Couldn't extract OCUDU PDCCH config extensions: {e}")

            try:
                for key, value in nc_cell_extension["ocudu_nrcelldu_base_extensions"].items():
                    if "scs" in key:
                        value = "".join(filter(str.isdigit, value))
                    new_du_cell[key] = value
            except KeyError as e:
                logging.warning(f"Couldn't extract OCUDU cell base extensions: {e}")

            # Extract values from standard attributes
            new_du_cell.update(
                {
                    "pci": cell["attributes"]["nRPCI"],
                    "tac": cell["attributes"]["nRTAC"],
                    "dl_arfcn": cell["attributes"]["arfcnDL"],
                    "channel_bandwidth_MHz": cell["attributes"]["bSChannelBwDL"],
                    "plmn": cell["attributes"]["pLMNInfoList"]["mcc"] + cell["attributes"]["pLMNInfoList"]["mnc"],
                    "enabled": cell["attributes"]["administrativeState"] != "LOCKED",
                }
            )
            du_cell_config.append(new_du_cell)
        return ofh_cell_config, du_cell_config

    async def run(self, stop_event: asyncio.Event):
        """Main run loop of the config manager worker."""
        logging.debug("Worker started")
        try:
            # Subscribe to notifications (default NETCONF stream)
            try:
                self.netconf_manager.create_subscription(stream_name="NETCONF", filter=None)
                logging.info("Subscribed to NETCONF notifications")
            except (ncclient.operations.OperationError, ncclient.operations.TimeoutExpiredError) as e:
                logging.warning(f"Failed to subscribe: {e}")

            while not stop_event.is_set():
                if not self.netconf_manager.connected:
                    logging.info("NETCONF session no longer connected (worker)")
                    break

                # Listen for notifications (non-blocking with timeout)
                try:
                    notif = self.netconf_manager.take_notification(timeout=1)
                    if notif is not None:
                        if "netconf-config-change" in notif.notification_xml:
                            logging.debug("netconf-config-change notification received")
                            # Queue event to update config
                            await self.state.app_command_queue.put(notif)

                except ncclient.operations.TimeoutExpiredError:
                    # timeout or transport closed
                    pass

                # Handle commands from main thread
                try:
                    cmd = self.state.app_command_queue.get_nowait()
                except asyncio.QueueEmpty:
                    cmd = None

                if cmd:
                    logging.debug("Processing config update")
                    await self.process_config_update()

                # Periodic heartbeat
                logging.debug("Worker heartbeat")
                await asyncio.sleep(WORKER_SLEEP_INTERVAL)

        finally:
            logging.debug("Worker stopped")
