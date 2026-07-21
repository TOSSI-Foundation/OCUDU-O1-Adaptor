# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
PM (performance management) metrics processing for the O1 adapter.

Handles gNB WS metric payloads: flattening, KPI rewriting per TS 28.554,
filtering per PM job, and pushing envelopes to configured stream targets.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import aiohttp

from state import AppState


# 1:1 gNB scalar -> 3GPP KPI rename + rescale (TS 28.554). Aggregated KPIs live in
# _derived_kpis. Note: spec §6.4.2 spells "VirtualResUtilization" as "VirtualResUtilizaiton";
# we emit the corrected form.
_KPI_MAPPINGS: dict[str, tuple[str, float]] = {
    "cu-up.pdcp.dl.average_latency_us": ("DLDelay_gNBCUUP", 0.01),
    "cu-up.pdcp.ul.average_latency_us": ("ULDelay_gNBCUUP", 0.01),
    "app_resource_usage.cpu_usage_percent": ("VirtualResUtilization", 1.0),
}


def _collect_scalars(prefix: str, value, out: list) -> None:
    """Walk a nested dict; append {name: <prefix.path>, value: v} for each numeric leaf.

    Lists and strings are skipped; bool is treated as non-numeric. Names in _KPI_MAPPINGS
    are rewritten and rescaled.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        mapping = _KPI_MAPPINGS.get(prefix)
        if mapping is None:
            out.append({"name": prefix, "value": value})
        else:
            kpi_name, scale = mapping
            out.append({"name": kpi_name, "value": value * scale})
        return
    if isinstance(value, dict):
        for k, v in value.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            _collect_scalars(child, v, out)


def _derived_kpis(data: dict) -> list:
    """Aggregated 3GPP KPIs; entries whose source fields are absent are skipped."""
    out = []

    dl_samples, ul_samples = [], []
    for cell in data.get("cells") or []:
        if not isinstance(cell, dict):
            continue
        for ue in cell.get("ue_list") or []:
            if not isinstance(ue, dict):
                continue
            if isinstance(ue.get("dl_brate"), (int, float)):
                dl_samples.append(ue["dl_brate"])
            if isinstance(ue.get("ul_brate"), (int, float)):
                ul_samples.append(ue["ul_brate"])
    if dl_samples:
        out.append({"name": "DlUeThroughput_Cell", "value": sum(dl_samples) / len(dl_samples) / 1000.0})
    if ul_samples:
        out.append({"name": "UlUeThroughput_Cell", "value": sum(ul_samples) / len(ul_samples) / 1000.0})

    # DLLat_gNB-DU: TS 28.554 §6.3.1.1 restricts this to packets arriving with an empty
    # DL queue. The source field sum_sdu_latency_us accumulates over every SDU, so under
    # load this value drifts above the spec quantity.
    tx = (data.get("rlc_metrics") or {}).get("tx") or {}
    sdus = tx.get("num_sdus")
    if isinstance(sdus, (int, float)) and sdus > 0:
        total_us = tx.get("sum_sdu_latency_us")
        if isinstance(total_us, (int, float)):
            out.append({"name": "DLLat_gNB-DU", "value": (total_us / sdus) * 0.01})
        dropped = tx.get("num_dropped_sdus") or 0
        discarded = tx.get("num_discarded_sdus") or 0
        out.append({"name": "DLRelPSR_Uu", "value": 100.0 * (1.0 - (dropped + discarded) / sdus)})

    rx = (data.get("rlc_metrics") or {}).get("rx") or {}
    pdus = rx.get("num_pdus")
    if isinstance(pdus, (int, float)) and pdus > 0:
        lost = rx.get("num_lost_pdus") or 0
        malformed = rx.get("num_malformed_pdus") or 0
        out.append({"name": "ULRelPSR_Uu", "value": 100.0 * (1.0 - (lost + malformed) / pdus)})

    return out


def _collect_meas_reports(data: dict, out: list) -> None:
    cucp = data.get("cu-cp")
    if not isinstance(cucp, dict):
        return
    reports = cucp.get("meas_reports")
    if not isinstance(reports, list):
        return
    quantities = ("ssb_rsrp_dbm", "ssb_rsrq_db", "ssb_sinr_db", "csi_rsrp_dbm", "csi_rsrq_db", "csi_sinr_db")
    for rep in reports:
        if not isinstance(rep, dict):
            continue
        ue = rep.get("ue")
        for role in ("serving", "best_neigh", "neigh"):
            cells = rep.get(role)
            if not isinstance(cells, list):
                continue
            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                pci = cell.get("pci")
                prefix = f"cu-cp.meas.ue{ue}.{role}.pci{pci}"
                for q in quantities:
                    v = cell.get(q)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        out.append({"name": f"{prefix}.{q}", "value": v})


def _flatten_metrics(data: dict) -> list:
    """Flatten the gNB WS metric payload into a list of {name, value} entries."""
    out = []
    cells = data.get("cells")
    if isinstance(cells, list):
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            cell_id = cell.get("cellId")
            for name, value in cell.items():
                if name == "cellId":
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    out.append({"name": name, "value": value, "cellId": cell_id})
            cell_metrics = cell.get("cell_metrics")
            if isinstance(cell_metrics, dict):
                _collect_scalars("cells.cell_metrics", cell_metrics, out)
            ue_list = cell.get("ue_list")
            if isinstance(ue_list, list):
                for ue in ue_list:
                    if isinstance(ue, dict):
                        _collect_scalars("cells.ue_list", ue, out)

    for top_key in ("rlc_metrics", "app_resource_usage", "buffer_pool", "du_low"):
        block = data.get(top_key)
        if isinstance(block, dict):
            _collect_scalars(top_key, block, out)

    pdcp = (data.get("cu-up") or {}).get("pdcp")
    if isinstance(pdcp, dict):
        _collect_scalars("cu-up.pdcp", pdcp, out)

    mac_dl = (((data.get("du") or {}).get("du_high") or {}).get("mac") or {}).get("dl")
    if isinstance(mac_dl, list):
        for entry in mac_dl:
            if isinstance(entry, dict):
                _collect_scalars("du.du_high.mac.dl", entry, out)

    _collect_meas_reports(data, out)
    out.extend(_derived_kpis(data))
    return out


class PmMetrics:
    """Processes gNB WS metric payloads and pushes PM envelopes to stream targets."""

    def __init__(self, state: AppState, profile: str = "gnb"):
        self._state = state
        self._profile = profile
        self._neighbours = None
        self._last_sent: dict = {}

    async def handle_ws_message(self, msg: str) -> None:
        """Dispatch WS messages by component type, and stream PM envelopes when configured."""
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            logging.error("WS non-JSON message: %s", msg)
            return

        if data.get("cmd"):
            logging.debug("WS command response: %s", data)
            return

        if data.get("neighbours"):
            self._neighbours = data["neighbours"]

        for key in ("cu-cp", "du", "cells"):
            block = data.get(key)
            if block:
                logging.debug("WS %s metrics: %s", key, block)
        # TODO: check for AMF connection status

        active_jobs = {
            job_id: job
            for job_id, job in self._state.pm_jobs.items()
            if job.get("administrativeState") == "UNLOCKED" and job.get("streamTarget")
        }
        if not active_jobs:
            return

        # Per-slice throughput, keyed by the UE S-NSSAI tagged by the RAN. Dynamic: any number
        # of slices that appear in ue_list are aggregated independently.
        slice_tput = self._slice_throughput(data)
        general_metrics = _flatten_metrics(data)
        congestion_cells = self._extract_congestion_cells(data)
        if not slice_tput and not general_metrics and not congestion_cells:
            return

        now = time.monotonic()
        for job_id, job in active_jobs.items():
            period = job.get("granularityPeriod") or 1
            allowed = set(job.get("performanceMetrics") or [])

            for (sst, sd), agg in slice_tput.items():
                slice_metrics = [
                    {"name": "DlUeThroughput_Cell", "value": agg["dl_kbps"]},
                    {"name": "UlUeThroughput_Cell", "value": agg["ul_kbps"]},
                ]
                job_metrics = (
                    [m for m in slice_metrics if m["name"] in allowed] if allowed else slice_metrics
                )
                if job_metrics and self._due((job_id, "slice", sst, sd), period, now):
                    envelope = self._build_envelope(job_id, job, job_metrics, sst=sst, sd=sd)
                    self._state.pm_metrics_queue.put_nowait((job["streamTarget"], envelope))

            job_general = (
                [m for m in general_metrics if m.get("name") in allowed] if allowed else general_metrics
            )
            if job_general or congestion_cells:
                kind = "cells" if congestion_cells else "general"
                if self._due((job_id, kind), period, now):
                    envelope = self._build_envelope(job_id, job, job_general, cells=congestion_cells)
                    self._state.pm_metrics_queue.put_nowait((job["streamTarget"], envelope))

    def _due(self, key, period, now) -> bool:
        if now - self._last_sent.get(key, 0.0) < period:
            return False
        self._last_sent[key] = now
        return True

    @staticmethod
    def _slice_throughput(data: dict) -> dict:
        """Aggregate per-UE throughput by slice S-NSSAI (sst, sd) from the gNB cell metrics.

        Returns {(sst, sd): {"dl_kbps", "ul_kbps", "nof_ues"}}. sd is None when the UE's
        S-NSSAI carries no SD. UEs without an sst tag (older RAN) are skipped.
        """
        slices: dict = {}
        for cell in data.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            for ue in cell.get("ue_list") or []:
                if not isinstance(ue, dict) or ue.get("sst") is None:
                    continue
                key = (ue.get("sst"), ue.get("sd"))
                agg = slices.setdefault(key, {"dl_kbps": 0.0, "ul_kbps": 0.0, "nof_ues": 0})
                dl, ul = ue.get("dl_brate"), ue.get("ul_brate")
                if isinstance(dl, (int, float)) and not isinstance(dl, bool):
                    agg["dl_kbps"] += dl / 1000.0
                if isinstance(ul, (int, float)) and not isinstance(ul, bool):
                    agg["ul_kbps"] += ul / 1000.0
                agg["nof_ues"] += 1
        return slices

    @staticmethod
    def _extract_congestion_cells(data: dict) -> list:
        """Structured per-cell + per-UE view for the congestion rApp; RNTI stays decimal.
        Returns [] for non-cells frames.
        """
        out = []
        for cell in data.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            cm = cell.get("cell_metrics")
            if not isinstance(cm, dict) or cm.get("pci") is None:
                continue
            ues = []
            for ue in cell.get("ue_list") or []:
                if not isinstance(ue, dict) or ue.get("rnti") is None:
                    continue
                ues.append(
                    {
                        "rnti": ue.get("rnti"),
                        "prb_dl": ue.get("tot_pdsch_prbs_used"),
                        "prb_ul": ue.get("tot_pusch_prbs_used"),
                        "thp_dl": ue.get("dl_brate"),
                        "thp_ul": ue.get("ul_brate"),
                        "dl_ok": ue.get("dl_nof_ok"),
                        "dl_nok": ue.get("dl_nof_nok"),
                        "ul_ok": ue.get("ul_nof_ok"),
                        "ul_nok": ue.get("ul_nof_nok"),
                    }
                )
            out.append(
                {
                    "pci": cm.get("pci"),
                    "nof_prbs": cm.get("nof_prbs"),
                    "nof_dl_slots": cm.get("nof_dl_slots"),
                    "nof_ul_slots": cm.get("nof_ul_slots"),
                    "ues": ues,
                }
            )
        return out

    def _build_envelope(self, job_id: str, job: dict, metrics: list, sst=None, sd=None, cells=None) -> dict:
        """Construct a JSON envelope for the PM stream per the agreed schema.

        When sst/sd are provided, the envelope is slice-scoped (measuredObject carries the
        S-NSSAI) so the SMO/rApp can do per-slice assurance.
        """
        measured = {
            "objectType": job.get("nf_key", ""),
            "objectId": job.get("nf_instance_id", "unknown"),
            "plmnId": job.get("plmn_id", ""),
        }
        # 3GPP Distinguished Name keys so the rApp can address the RRMPolicyRatio object over
        # SDNR/RESTCONF (…/ManagedElement=<meId>/GNBDUFunction=<gnbduId>/NRCellDU=<nrCellDuId>/…)
        # without hardcoding or a discovery GET. Omitted when absent (e.g. nrCellDuId on CU jobs).
        for env_key, job_key in (("meId", "me_id"), ("gnbduId", "gnbdu_id"), ("nrCellDuId", "nrcelldu_id")):
            val = job.get(job_key)
            if val is not None:
                measured[env_key] = val
        if sst is not None:
            measured["sst"] = sst
        if sd is not None:
            measured["sd"] = sd
        envelope = {
            "nfType": self._profile,
            "nfInstanceId": job.get("nf_instance_id", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "granularityPeriod": job.get("granularityPeriod", 1),
            "measuredObject": measured,
            "metrics": metrics,
            "jobId": job_id,
        }
        if cells:
            envelope["cells"] = cells
            if self._neighbours:
                envelope["neighbours"] = self._neighbours
        return envelope

    async def run_pusher(self) -> None:
        """Consume pm_metrics_queue and POST each envelope to its configured streamTarget."""
        async with aiohttp.ClientSession() as session:
            while True:
                target, envelope = await self._state.pm_metrics_queue.get()
                try:
                    async with session.post(
                        target,
                        json=envelope,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status >= 400:
                            logging.warning("PM push to %s returned %s", target, resp.status)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logging.warning("PM push to %s failed: %s", target, e)
                finally:
                    self._state.pm_metrics_queue.task_done()
