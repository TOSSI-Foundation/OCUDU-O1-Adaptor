# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module provides a class `WsRemoteCommands` to handle remote commands via WebSocket.
"""

import json
from typing import Any, Dict


# pylint: disable=too-few-public-methods
class WsRemoteCommands:
    """
    A class to handle remote commands via WebSocket.

    Attributes:
        ws_send_queue (queue): The WebSocket send queue.
    """

    def __init__(self, ws_send_queue=None):
        self.send_queue = ws_send_queue

    def send_ssb_command(self, ssb_cell_config) -> Any:
        """
        Sends an SSB (Synchronization Signal Block) configuration command.

        Args:
            ssb_cell_config (list): A list of cell configurations for the SSB.
        """
        return self._send_ws_message(
            {
                "cmd": "ssb_set",
                "cells": ssb_cell_config,
            }
        )

    def send_rrm_policy_ratio_command(self, rrm_policy_ratio_config) -> Any:
        """
        Sends an RRM (Radio Resource Management) policy ratio configuration command.

        Args:
            rrm_policy_ratio_config (list): A list of RRM policy ratio configurations.
        """
        return self._send_ws_message(
            {
                "cmd": "rrm_policy_ratio_set",
                "policies": rrm_policy_ratio_config,
            }
        )

    def send_handover_command(self, handover_config) -> Any:
        """
        Sends a handover command (serving_pci, rnti, target_pci, target_plmn, target_tac) for a single UE.
        """
        return self._send_ws_message(
            {
                "cmd": "ho",
                **handover_config,
            }
        )

    def send_quit_command(self) -> Any:
        """
        Sends a quit command to the server and waits for connection close.

        Returns:
            bool: True if quit command was successful and connection closed properly,
        """
        return self._send_ws_message({"cmd": "quit"})

    def send_metrics_subscribe(self) -> Any:
        """Request the gNB to (re-)emit its metric snapshot."""
        return self._send_ws_message({"cmd": "metrics_subscribe"})

    def _send_ws_message(self, command: Dict) -> Any:
        # Put quit command on WS queue and return
        self.send_queue.put_nowait(json.dumps(command))
        return True
