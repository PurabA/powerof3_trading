"""
Interactive Terminal Dashboard and CLI interface.
"""
import time
from typing import Optional, List
from rich.console import Console
from rich.layout import Layout
from rich.live import Live

from ui.formatters import (
    format_metrics_panel,
    format_positions_table,
    format_orders_table,
    format_screener_table
)
from core.timeutils import now_ist


class TerminalDashboard:
    """
    Renders terminal dashboard using Rich components.
    """
    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()

    def print_snapshot(
        self,
        capital: float,
        realized_pnl: float,
        open_positions: list,
        orders: list,
        candidates: list,
        charges: float,
        trading_mode: str,
        phase_str: str = ""
    ):
        """Prints a complete formatted terminal dashboard snapshot."""
        unrealized = sum(p.unrealized_pnl for p in open_positions)
        time_str = now_ist().strftime("%H:%M:%S")

        self.console.clear()
        
        # 1. Header Metrics Panel
        header = format_metrics_panel(
            capital=capital,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized,
            open_positions_count=len(open_positions),
            total_charges=charges,
            trading_mode=trading_mode,
            time_str=time_str,
            phase_str=phase_str
        )
        self.console.print(header)

        # 2. Screener Candidates Table
        if candidates:
            self.console.print(format_screener_table(candidates))

        # 3. Active Positions Table
        self.console.print(format_positions_table(open_positions))

        # 4. Orders Table
        self.console.print(format_orders_table(orders))
