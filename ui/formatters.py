"""
Rich UI table formatters, metric panels, and color styling.
"""
from typing import List
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from core.models import Position, Order, Trade, ScreenerCandidate, PositionSide, OrderStatus


def format_metrics_panel(
    capital: float,
    realized_pnl: float,
    unrealized_pnl: float,
    open_positions_count: int,
    total_charges: float,
    trading_mode: str,
    time_str: str,
    phase_str: str = ""
) -> Panel:
    """Generates the top header status and account NAV panel."""
    total_pnl = realized_pnl + unrealized_pnl
    nav = capital + total_pnl

    pnl_color = "bold green" if total_pnl >= 0 else "bold red"
    sign = "+" if total_pnl >= 0 else ""

    text = Text()
    text.append(f"⚡ Mode: ", style="bold cyan")
    mode_style = "bold magenta" if trading_mode == "PAPER" else "bold green"
    text.append(f"[{trading_mode}]  ", style=mode_style)
    text.append(f"🕒 Time: {time_str} IST  ", style="bold yellow")
    if phase_str:
        text.append(f"📍 Phase: {phase_str}  ", style="bold cyan")
    text.append(f"💼 Open Positions: {open_positions_count}\n", style="bold white")

    text.append(f"💰 Account NAV: ₹{nav:,.2f}  ", style="bold white")
    text.append(f"💵 Realized: ₹{realized_pnl:,.2f}  ", style="green" if realized_pnl >= 0 else "red")
    text.append(f"📈 Unrealized: ₹{unrealized_pnl:,.2f}  ", style="green" if unrealized_pnl >= 0 else "red")
    text.append(f"📊 Net Day P&L: {sign}₹{total_pnl:,.2f}  ", style=pnl_color)
    text.append(f"🧾 Charges: ₹{total_charges:.2f}", style="dim")

    return Panel(
        text,
        title="[bold blue]🚀 POWER OF 3 TRADING ENGINE[/bold blue]",
        border_style="bright_blue",
        box=box.ROUNDED
    )


def format_positions_table(positions: List[Position]) -> Table:
    """Generates formatted table for live intraday positions."""
    table = Table(
        title="[bold green]📊 ACTIVE INTRADAY POSITIONS[/bold green]",
        box=box.SIMPLE_HEAVY,
        expand=True
    )
    table.add_column("Symbol", style="bold white")
    table.add_column("Side", justify="center")
    table.add_column("Qty", justify="right")
    table.add_column("Entry Price", justify="right")
    table.add_column("LTP", justify="right")
    table.add_column("Stop Loss", justify="right")
    table.add_column("Trailing SL", justify="right")
    table.add_column("Unrealized P&L", justify="right")

    if not positions:
        table.add_row("-", "-", "-", "-", "-", "-", "-", "[dim]No active positions[/dim]")
        return table

    for p in positions:
        side_style = "bold green" if p.side == PositionSide.LONG else "bold red"
        pnl_style = "bold green" if p.unrealized_pnl >= 0 else "bold red"
        pnl_sign = "+" if p.unrealized_pnl >= 0 else ""

        table.add_row(
            p.symbol,
            f"[{side_style}]{p.side.value}[/{side_style}]",
            str(p.quantity),
            f"₹{p.entry_price:.2f}",
            f"₹{p.current_price:.2f}",
            f"₹{p.stop_loss:.2f}" if p.stop_loss else "-",
            f"₹{p.trailing_stop:.2f}" if p.trailing_stop else "-",
            f"[{pnl_style}]{pnl_sign}₹{p.unrealized_pnl:,.2f}[/{pnl_style}]"
        )
    return table


def format_orders_table(orders: List[Order]) -> Table:
    """Generates formatted table for today's orders."""
    table = Table(
        title="[bold cyan]📋 TODAY'S ORDER BOOK[/bold cyan]",
        box=box.SIMPLE_HEAVY,
        expand=True
    )
    table.add_column("Time", style="dim")
    table.add_column("Order ID", style="bold cyan")
    table.add_column("Symbol", style="bold white")
    table.add_column("Side", justify="center")
    table.add_column("Qty", justify="right")
    table.add_column("Avg Price", justify="right")
    table.add_column("Status", justify="center")
    table.add_column("Tag", style="dim")

    if not orders:
        table.add_row("-", "-", "-", "-", "-", "-", "[dim]No orders placed today[/dim]", "-")
        return table

    for o in orders[-8:][::-1]:  # Show latest 8
        side_style = "green" if o.side.value == "BUY" else "red"
        status_styles = {
            OrderStatus.FILLED: "bold green",
            OrderStatus.SUBMITTED: "bold yellow",
            OrderStatus.PENDING: "yellow",
            OrderStatus.REJECTED: "bold red",
            OrderStatus.CANCELLED: "dim"
        }
        st_style = status_styles.get(o.status, "white")

        table.add_row(
            o.created_at.strftime("%H:%M:%S"),
            o.order_id,
            o.symbol,
            f"[{side_style}]{o.side.value}[/{side_style}]",
            str(o.quantity),
            f"₹{o.average_price:.2f}" if o.average_price > 0 else "MKT",
            f"[{st_style}]{o.status.value}[/{st_style}]",
            o.tag
        )
    return table


def format_screener_table(candidates: List[ScreenerCandidate]) -> Table:
    """Generates table of 09:20 IST screener stocks in play."""
    table = Table(
        title="[bold yellow]🎯 MORNING STOCKS IN PLAY (RVOL ORB)[/bold yellow]",
        box=box.SIMPLE_HEAVY,
        expand=True
    )
    table.add_column("Rank", justify="center", style="bold")
    table.add_column("Symbol", style="bold white")
    table.add_column("Bias", justify="center")
    table.add_column("LTP", justify="right", style="bold cyan")
    table.add_column("Trigger Price", justify="right")
    table.add_column("RVOL", justify="right", style="bold yellow")
    table.add_column("14d ATR", justify="right")
    table.add_column("5m Open Range", justify="right")

    if not candidates:
        table.add_row("-", "-", "-", "-", "-", "-", "-", "[dim]Screener runs at 09:20 IST[/dim]")
        return table

    for i, c in enumerate(candidates, 1):
        bias_style = "bold green" if c.direction == "LONG" else "bold red"
        ltp_val = getattr(c, "current_ltp", 0.0)
        if ltp_val <= 0:
            ltp_val = c.opening_close

        table.add_row(
            str(i),
            c.symbol,
            f"[{bias_style}]{c.direction}[/{bias_style}]",
            f"₹{ltp_val:.2f}",
            f"₹{c.trigger_price:.2f}",
            f"{c.rvol:.1f}x",
            f"₹{c.atr_14d:.2f}",
            f"₹{c.opening_low:.2f} - ₹{c.opening_high:.2f}"
        )
    return table
