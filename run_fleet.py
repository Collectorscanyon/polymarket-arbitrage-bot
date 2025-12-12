"""Multi-Wallet Fleet Launcher.

Spawns and manages multiple bot instances, each with its own configuration.
Each bot runs with a separate .env file for different wallet/strategy settings.

Usage:
    python run_fleet.py              # Start all enabled wallets
    python run_fleet.py --status     # Show status of all wallets
    python run_fleet.py --stop       # Stop all running bots
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# Fleet configuration file
FLEET_CONFIG = Path(__file__).parent / "wallets.yaml"


@dataclass
class WalletConfig:
    """Configuration for a single wallet/bot instance."""
    name: str
    env_file: str
    description: str = ""
    enabled: bool = True


@dataclass
class BotProcess:
    """Tracks a running bot process."""
    wallet: WalletConfig
    process: subprocess.Popen
    start_time: float
    pid: int


class FleetManager:
    """Manages multiple bot processes."""

    def __init__(self, config_path: Path = FLEET_CONFIG):
        self.config_path = config_path
        self.wallets: list[WalletConfig] = []
        self.processes: dict[str, BotProcess] = {}
        self._load_config()

    def _load_config(self):
        """Load wallet configurations from YAML file."""
        if not self.config_path.exists():
            print(f"Fleet config not found: {self.config_path}")
            print("Creating default wallets.yaml...")
            self._create_default_config()
            return

        with open(self.config_path) as f:
            data = yaml.safe_load(f)

        self.wallets = []
        for w in data.get("wallets", []):
            self.wallets.append(WalletConfig(
                name=w.get("name", "Unnamed"),
                env_file=w.get("env_file", ".env"),
                description=w.get("description", ""),
                enabled=w.get("enabled", True),
            ))

        print(f"Loaded {len(self.wallets)} wallet configurations")

    def _create_default_config(self):
        """Create a default wallets.yaml file."""
        default_config = """# Multi-Wallet Fleet Configuration
wallets:
  - name: "Default"
    env_file: ".env"
    description: "Default bot configuration"
    enabled: true
"""
        with open(self.config_path, "w") as f:
            f.write(default_config)
        self._load_config()

    def start_wallet(self, wallet: WalletConfig) -> Optional[BotProcess]:
        """Start a single bot instance for a wallet."""
        if wallet.name in self.processes:
            print(f"[{wallet.name}] Already running (PID {self.processes[wallet.name].pid})")
            return None

        env_path = Path(__file__).parent / wallet.env_file
        if not env_path.exists():
            print(f"[{wallet.name}] ⚠️ Env file not found: {env_path}")
            print(f"[{wallet.name}] Skipping (create {wallet.env_file} to enable)")
            return None

        # Build environment with custom .env file
        env = os.environ.copy()
        
        # Load variables from the wallet's .env file
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip()

        # Set a unique identifier for this wallet
        env["FLEET_WALLET_NAME"] = wallet.name

        print(f"[{wallet.name}] Starting bot with {wallet.env_file}...")

        # Spawn the bot process
        python_cmd = sys.executable
        proc = subprocess.Popen(
            [python_cmd, "-m", "bot.main"],
            cwd=Path(__file__).parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        bot_proc = BotProcess(
            wallet=wallet,
            process=proc,
            start_time=time.time(),
            pid=proc.pid,
        )

        self.processes[wallet.name] = bot_proc
        print(f"[{wallet.name}] STARTED (PID {proc.pid})")

        return bot_proc

    def start_all(self):
        """Start all enabled wallets."""
        enabled = [w for w in self.wallets if w.enabled]
        print(f"\nStarting {len(enabled)} enabled wallet(s)...\n")

        for wallet in enabled:
            self.start_wallet(wallet)
            time.sleep(1)  # Stagger starts

        print(f"\nFLEET STARTED with {len(self.processes)} bot(s)")

    def stop_wallet(self, name: str):
        """Stop a specific wallet's bot."""
        if name not in self.processes:
            print(f"[{name}] Not running")
            return

        proc = self.processes[name]
        print(f"[{name}] Stopping (PID {proc.pid})...")

        try:
            proc.process.terminate()
            proc.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.process.kill()

        del self.processes[name]
        print(f"[{name}] STOPPED")

    def stop_all(self):
        """Stop all running bots."""
        if not self.processes:
            print("No bots running")
            return

        print(f"\nStopping {len(self.processes)} bot(s)...\n")

        for name in list(self.processes.keys()):
            self.stop_wallet(name)

        print("\nALL BOTS STOPPED")

    def status(self):
        """Print status of all wallets."""
        print("\n" + "=" * 60)
        print("FLEET STATUS")
        print("=" * 60)

        for wallet in self.wallets:
            status = "RUNNING" if wallet.name in self.processes else "STOPPED"
            enabled = "YES" if wallet.enabled else "NO"

            if wallet.name in self.processes:
                proc = self.processes[wallet.name]
                uptime = int(time.time() - proc.start_time)
                uptime_str = f"{uptime // 60}m {uptime % 60}s"
                status = f"RUNNING (PID {proc.pid}, uptime: {uptime_str})"

            print(f"\n[{wallet.name}]")
            print(f"  Status:      {status}")
            print(f"  Enabled:     {enabled}")
            print(f"  Env file:    {wallet.env_file}")
            print(f"  Description: {wallet.description}")

        print("\n" + "=" * 60)

    def monitor(self):
        """Monitor running bots and restart if they crash."""
        print("\nMonitoring fleet (Ctrl+C to stop)...\n")

        try:
            while True:
                for name, proc in list(self.processes.items()):
                    retcode = proc.process.poll()
                    if retcode is not None:
                        print(f"[{name}] ⚠️ Crashed with code {retcode}")
                        del self.processes[name]

                        # Auto-restart
                        wallet = next((w for w in self.wallets if w.name == name), None)
                        if wallet and wallet.enabled:
                            print(f"[{name}] Restarting...")
                            time.sleep(2)
                            self.start_wallet(wallet)

                time.sleep(5)

        except KeyboardInterrupt:
            print("\n\nShutting down fleet...")
            self.stop_all()


def main():
    parser = argparse.ArgumentParser(description="Multi-Wallet Fleet Launcher")
    parser.add_argument("--status", action="store_true", help="Show status of all wallets")
    parser.add_argument("--stop", action="store_true", help="Stop all running bots")
    parser.add_argument("--wallet", type=str, help="Start/stop a specific wallet by name")
    args = parser.parse_args()

    manager = FleetManager()

    if args.status:
        manager.status()
    elif args.stop:
        manager.stop_all()
    elif args.wallet:
        wallet = next((w for w in manager.wallets if w.name == args.wallet), None)
        if wallet:
            manager.start_wallet(wallet)
            manager.monitor()
        else:
            print(f"Wallet not found: {args.wallet}")
    else:
        manager.start_all()
        manager.monitor()


if __name__ == "__main__":
    main()
