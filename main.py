"""
Main CLI Entrypoint for powerof3_trading.
Usage:
  python main.py start       - Run the live trading daemon with dashboard
  python main.py dashboard   - View live positions, orders, and P&L snapshot
  python main.py order       - Place a manual discretionary order (interactive or flags)
  python main.py test-alert  - Test Telegram notification broadcast to 3 chat IDs
  python main.py sync-data   - Precompute 14-day Nifty 500 baselines
  python main.py login       - Authenticate with Kotak Neo and show account limits
"""
import sys
import time
import argparse
import logging
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config.settings import load_settings
from engine import TradingEngine
from broker.neo_auth import KotakNeoAuth
from notifier.telegram import TelegramNotifier
from core.models import OrderSide, OrderType, OrderStatus
from core.timeutils import now_ist

console = Console()


def cmd_start(args):
    """Runs the continuous live trading loop."""
    console.print(Panel("[bold green]🚀 STARTING POWER OF 3 TRADING ENGINE[/bold green]\n"
                        "Press [bold yellow]Ctrl+C[/bold yellow] to exit cleanly."))
    settings = load_settings()
    engine = TradingEngine(settings)
    engine.initialize()

    try:
        while True:
            engine.step()
            time.sleep(3)
    except KeyboardInterrupt:
        console.print("\n[bold red]🛑 Trading daemon stopped by user.[/bold red]")


def cmd_dashboard(args):
    """Renders a snapshot of current positions, orders, and P&L."""
    settings = load_settings()
    engine = TradingEngine(settings)
    engine.initialize()
    engine.step()


def cmd_order(args):
    """Places a manual discretionary order with confirmation and status reporting."""
    settings = load_settings()
    engine = TradingEngine(settings)
    engine.initialize()

    console.print(Panel(
        f"[bold cyan]🛒 MANUAL ORDER PLACEMENT[/bold cyan]\n"
        f"Mode: [bold magenta]{settings.app.trading_mode}[/bold magenta]",
        border_style="cyan"
    ))

    # 1. Resolve Symbol
    symbol = getattr(args, "symbol", None)
    if not symbol:
        symbol = console.input("[bold yellow]Enter Trading Symbol (e.g. RELIANCE, TCS): [/bold yellow]").strip().upper()
    symbol = symbol.replace("-EQ", "").strip().upper()

    if not symbol:
        console.print("[bold red]❌ Error: Symbol cannot be empty.[/bold red]")
        return

    # 2. Resolve Side
    side_str = getattr(args, "side", None)
    if not side_str:
        side_in = console.input("[bold yellow]Transaction Type: [1] BUY (Long)  [2] SELL (Short) [Default: 1]: [/bold yellow]").strip()
        side_str = "SELL" if side_in == "2" else "BUY"
    side = OrderSide.BUY if side_str.upper() == "BUY" else OrderSide.SELL

    # 3. Resolve Order Type & Price
    ord_type_str = getattr(args, "type", None)
    if not ord_type_str:
        ord_type_in = console.input("[bold yellow]Order Type: [1] MARKET  [2] LIMIT [Default: 1]: [/bold yellow]").strip()
        ord_type_str = "LMT" if ord_type_in == "2" else "MKT"
    order_type = OrderType.LIMIT if ord_type_str.upper() in ("LMT", "LIMIT") else OrderType.MARKET

    raw_price = getattr(args, "price", None)
    price = float(raw_price) if raw_price is not None else 0.0
    if order_type == OrderType.LIMIT and price <= 0:
        price_in = console.input("[bold yellow]Enter Limit Price (INR): [/bold yellow]").strip()
        try:
            price = float(price_in)
        except ValueError:
            console.print("[bold red]❌ Invalid price.[/bold red]")
            return

    # 4. Resolve Quantity
    raw_qty = getattr(args, "qty", None)
    qty = int(raw_qty) if raw_qty is not None else 0
    if qty <= 0:
        qty_in = console.input("[bold yellow]Enter Quantity (Shares): [/bold yellow]").strip()
        try:
            qty = int(qty_in)
        except ValueError:
            console.print("[bold red]❌ Invalid quantity.[/bold red]")
            return

    # 5. Optional Stop Loss and Target
    sl_price = getattr(args, "sl", None)
    if sl_price is None and not getattr(args, "no_prompt", False):
        sl_in = console.input("[bold yellow]Stop Loss Price (Optional, press Enter to skip): [/bold yellow]").strip()
        sl_price = float(sl_in) if sl_in else None

    target_price = getattr(args, "target", None)
    if target_price is None and not getattr(args, "no_prompt", False):
        target_in = console.input("[bold yellow]Target Price (Optional, press Enter to skip): [/bold yellow]").strip()
        target_price = float(target_in) if target_in else None

    # Fetch live LTP for confirmation
    ltp = engine.market_data.get_ltp(symbol)
    if ltp <= 0 and engine.neo_client:
        quotes = engine.market_data.fetch_quotes_batch([symbol])
        q = quotes.get(symbol)
        ltp = q.last_price if q else 0.0

    # Summary confirmation table
    conf_table = Table(title="[bold yellow]⚠️ ORDER CONFIRMATION[/bold yellow]", border_style="yellow")
    conf_table.add_column("Field", style="bold white")
    conf_table.add_column("Value", style="bold cyan")
    conf_table.add_row("Symbol", symbol)
    if ltp > 0:
        conf_table.add_row("Current LTP", f"₹{ltp:.2f}")
    conf_table.add_row("Action", f"[bold green]{side.value}[/bold green]" if side == OrderSide.BUY else f"[bold red]{side.value}[/bold red]")
    conf_table.add_row("Order Type", order_type.value)
    conf_table.add_row("Quantity", str(qty))
    conf_table.add_row("Price", f"₹{price:.2f}" if order_type == OrderType.LIMIT else "MARKET")
    if ltp > 0:
        approx_val = (price if order_type == OrderType.LIMIT else ltp) * qty
        conf_table.add_row("Approx Value", f"₹{approx_val:,.2f}")
    if sl_price:
        conf_table.add_row("Stop Loss", f"₹{sl_price:.2f}")
    if target_price:
        conf_table.add_row("Target", f"₹{target_price:.2f}")
    conf_table.add_row("Product", "MIS (Intraday)")

    console.print(conf_table)

    confirm = console.input("\n[bold yellow]Type 'y' to confirm and place order [y/n]: [/bold yellow]").strip().lower()
    if confirm not in ("y", "yes"):
        console.print("[bold red]🚫 Order cancelled by user.[/bold red]")
        return

    console.print(f"[bold cyan]Submitting {side.value} order for {qty} {symbol}...[/bold cyan]")
    order = engine.broker.place_order(
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=order_type,
        price=price,
        stop_loss=sl_price,
        target=target_price,
        tag="MANUAL"
    )

    if order and order.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED):
        console.print(Panel(
            f"[bold green]✅ ORDER PLACED SUCCESSFULLY![/bold green]\n\n"
            f"Order ID: [bold white]{order.order_id}[/bold white]\n"
            f"Symbol: [bold white]{order.symbol}[/bold white]\n"
            f"Side: [bold white]{order.side.value}[/bold white]\n"
            f"Quantity: [bold white]{order.quantity}[/bold white]\n"
            f"Avg Price: [bold white]₹{order.average_price:.2f}[/bold white]\n"
            f"Status: [bold green]{order.status.value}[/bold green]",
            border_style="green"
        ))
        if engine.notifier:
            engine.notifier.notify_trade_entry(order, sl_price, target_price)
    else:
        err_msg = order.message if order else "Unknown error"
        console.print(Panel(f"[bold red]❌ ORDER REJECTED: {err_msg}[/bold red]", border_style="red"))


def cmd_test_alert(args):
    """Sends a test alert to configured Telegram chat IDs."""
    settings = load_settings()
    notifier = TelegramNotifier(settings.telegram)

    if not notifier.bot_token or not notifier.chat_ids:
        console.print("[bold red]❌ Error: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_IDS not configured in .env[/bold red]")
        return

    console.print(f"📡 Sending test notification to {len(notifier.chat_ids)} chat IDs...")
    msg = (
        "🔔 *POWER OF 3 — TEST NOTIFICATION* 🔔\n\n"
        "✅ Bot connected successfully!\n"
        f"🕒 Server Time (IST): `{now_ist().strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"⚡ Mode: `{settings.app.trading_mode}`\n"
        f"🎯 Strategy: `{settings.strategy.top_n_stocks} Stock RVOL ORB`"
    )
    success = notifier.send_broadcast(msg)
    if success:
        console.print("[bold green]✅ Test notification delivered successfully![/bold green]")
    else:
        console.print("[bold red]❌ Failed to deliver test notification. Check bot token and chat IDs.[/bold red]")


def cmd_sync_data(args):
    """Precomputes 14-day baselines for Nifty 500 stocks."""
    settings = load_settings()
    engine = TradingEngine(settings)
    engine.initialize()
    console.print("[bold cyan]🔄 Precomputing 14-day baselines for Nifty 500...[/bold cyan]")
    engine.run_pre_market_prep()
    console.print(f"[bold green]✅ Completed baselines for {len(engine.historical_data.stock_baselines)} stocks.[/bold green]")


def cmd_login(args):
    """Tests Kotak Neo login and displays account limits."""
    settings = load_settings()
    console.print("[bold cyan]🔐 Authenticating with Kotak Neo API...[/bold cyan]")
    auth = KotakNeoAuth(settings.neo)
    client = auth.get_authenticated_client(force_login=True)

    if client:
        try:
            limits = client.limits()
            console.print("[bold green]✅ Authentication Successful![/bold green]")
            console.print(limits)
        except Exception as e:
            console.print(f"[bold yellow]Authenticated, but limits query returned: {e}[/bold yellow]")
    else:
        console.print("[bold red]❌ Authentication failed. Check your credentials in .env[/bold red]")


def main():
    parser = argparse.ArgumentParser(description="PowerOf3 Trading System CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("start", help="Start the trading engine daemon")
    subparsers.add_parser("dashboard", help="Display current trading dashboard")
    
    order_parser = subparsers.add_parser("order", help="Place a manual discretionary order")
    order_parser.add_argument("--symbol", type=str, help="Trading symbol (e.g. RELIANCE)")
    order_parser.add_argument("--side", type=str, choices=["BUY", "SELL"], help="BUY or SELL")
    order_parser.add_argument("--qty", type=int, help="Number of shares")
    order_parser.add_argument("--type", type=str, choices=["MKT", "LMT"], help="Order type")
    order_parser.add_argument("--price", type=float, default=0.0, help="Limit price")
    order_parser.add_argument("--sl", type=float, default=None, help="Stop loss price")
    order_parser.add_argument("--target", type=float, default=None, help="Target price")
    order_parser.add_argument("--no-prompt", action="store_true", help="Skip interactive prompts")

    subparsers.add_parser("test-alert", help="Send test alert to Telegram")
    subparsers.add_parser("sync-data", help="Precompute 14-day baselines")
    subparsers.add_parser("login", help="Authenticate with Kotak Neo")

    args = parser.parse_args()

    # Default logging config
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    if args.command == "start":
        cmd_start(args)
    elif args.command == "dashboard":
        cmd_dashboard(args)
    elif args.command == "order":
        cmd_order(args)
    elif args.command == "test-alert":
        cmd_test_alert(args)
    elif args.command == "sync-data":
        cmd_sync_data(args)
    elif args.command == "login":
        cmd_login(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
