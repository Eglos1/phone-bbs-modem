#!/usr/bin/env python3
"""
MUD Terminal UI
===============
Interactive terminal that connects to mud_client.py via file-based I/O.
Displays MUD output in real-time and sends commands when you type.

Local commands (prefixed with /):
    /status     Show client status
    /triggers   Show loaded triggers
    /clear      Clear screen
    /history    Show command history
    /script     Load and run a command script
    /alias      Add a quick alias
    /save       Save command history
    /quit       Disconnect and exit

Usage:
    python3 terminal/ui.py                    # interactive mode
    python3 terminal/ui.py --triggers         # with trigger engine
    python3 terminal/ui.py --script cmds.txt  # run a script then go interactive

Requires mud_client.py to be running separately (or start it for you).
"""

import sys
import os
import time
import select
import signal
import threading
import readline  # command line editing
import atexit

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─── File paths ──────────────────────────────────────────────────────────────
OUTPUT_FILE = "mud_output.txt"
CMDS_FILE   = "mud_cmds.txt"
STATE_FILE  = "mud_state.txt"
STATUS_FILE = "mud_status.txt"
HISTORY_FILE = ".mud_history"
SCRIPT_DIR  = "scripts"


class MUDTerminal:
    """
    Interactive terminal UI for the MUD client.
    Reads mud_output.txt, writes to mud_cmds.txt.
    """

    def __init__(self, use_triggers=False):
        self.running = True
        self.use_triggers = use_triggers
        self.trigger_engine = None
        self.cmd_history = []
        self.last_output_pos = 0
        self.scripts = {}

        # Load command history
        self._load_history()

        # Optional trigger engine
        if use_triggers:
            try:
                from mud_triggers import TriggerEngine
                self.trigger_engine = TriggerEngine()
                cfg = "mud_triggers.cfg"
                if os.path.exists(cfg):
                    self.trigger_engine.load_config(cfg)
                self.trigger_engine.start()
            except ImportError:
                print("[!] mud_triggers.py not found, triggers disabled")

    def _load_history(self):
        """Load readline history."""
        try:
            if os.path.exists(HISTORY_FILE):
                readline.read_history_file(HISTORY_FILE)
                readline.set_history_length(1000)
        except:
            pass

    def _save_history(self):
        """Save readline history."""
        try:
            readline.write_history_file(HISTORY_FILE)
        except:
            pass

    # ─── Output Display ──────────────────────────────────────────────

    def read_new_output(self):
        """Read new content from mud_output.txt since last check."""
        try:
            if not os.path.exists(OUTPUT_FILE):
                return ""

            with open(OUTPUT_FILE, "r", errors="replace") as f:
                f.seek(self.last_output_pos)
                new_text = f.read()
                self.last_output_pos = f.tell()

            return new_text
        except:
            return ""

    def display_output(self, text):
        """Display MUD output to the terminal."""
        if not text:
            return
        sys.stdout.write(text)
        sys.stdout.flush()

    # ─── Command Sending ─────────────────────────────────────────────

    def send_command(self, command):
        """Send a command to mud_cmds.txt."""
        # Expand aliases from trigger engine
        expanded = command
        if self.trigger_engine:
            expanded = self.trigger_engine.expand_aliases(command)

        try:
            with open(CMDS_FILE, "a") as f:
                f.write(expanded + "\n")
                f.flush()
            self.cmd_history.append(command)
        except Exception as e:
            print(f"[!] Send error: {e}")

    # ─── Local Commands ──────────────────────────────────────────────

    def handle_local_command(self, line):
        """Handle /commands that are local to the terminal."""
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/quit" or cmd == "/exit" or cmd == "/q":
            self.running = False
            return True

        elif cmd == "/clear" or cmd == "/cls":
            os.system("clear" if os.name != "nt" else "cls")
            return True

        elif cmd == "/status" or cmd == "/st":
            self._show_status()
            return True

        elif cmd == "/triggers" or cmd == "/tr":
            self._show_triggers()
            return True

        elif cmd == "/history" or cmd == "/h":
            self._show_history()
            return True

        elif cmd == "/script" or cmd == "/sc":
            self._run_script(arg)
            return True

        elif cmd == "/alias" or cmd == "/al":
            self._add_alias(arg)
            return True

        elif cmd == "/save":
            self._save_history()
            print("[History saved]")
            return True

        elif cmd == "/help" or cmd == "/?":
            self._show_help()
            return True

        elif cmd == "/clear-output":
            try:
                with open(OUTPUT_FILE, "w") as f:
                    f.write("")
                self.last_output_pos = 0
                print("[Output cleared]")
            except:
                pass
            return True

        elif cmd == "/reconnect":
            # Signal mud_client to reconnect by writing status
            try:
                with open(STATUS_FILE, "w") as f:
                    f.write("state=reconnect_requested\n")
                print("[Reconnect requested]")
            except:
                pass
            return True

        return False

    def _show_status(self):
        """Show client connection status."""
        try:
            if os.path.exists(STATUS_FILE):
                with open(STATUS_FILE, "r") as f:
                    content = f.read()
                print(content)
            else:
                print("[No status file - is mud_client.py running?]")
        except:
            print("[Error reading status]")

        # Show trigger engine stats
        if self.trigger_engine:
            print()
            print(self.trigger_engine.status())

    def _show_triggers(self):
        """Show loaded triggers."""
        if not self.trigger_engine:
            print("[Trigger engine not loaded]")
            print("Start with --triggers to enable")
            return
        print(self.trigger_engine.status())

    def _show_history(self):
        """Show command history."""
        if not self.cmd_history:
            print("[No commands sent yet]")
            return
        recent = self.cmd_history[-30:]
        for i, cmd in enumerate(recent):
            print(f"  {i+1:3d}. {cmd}")
        if len(self.cmd_history) > 30:
            print(f"  ... ({len(self.cmd_history) - 30} more)")

    def _run_script(self, filename):
        """Load and run a command script."""
        if not filename:
            # List available scripts
            self._list_scripts()
            return

        filepath = filename
        if not os.path.exists(filepath) and os.path.exists(os.path.join(SCRIPT_DIR, filename)):
            filepath = os.path.join(SCRIPT_DIR, filename)

        if not os.path.exists(filepath):
            print(f"[Script not found: {filename}]")
            return

        print(f"[Running script: {filepath}]")
        try:
            with open(filepath, "r") as f:
                lines = f.readlines()

            sent = 0
            delay = 0.5  # default delay between commands

            for line in lines:
                line = line.rstrip()
                if not line or line.startswith("#"):
                    continue

                # Handle special directives
                if line.lower().startswith("delay "):
                    try:
                        delay = float(line.split(None, 1)[1])
                    except:
                        pass
                    continue

                if line.lower().startswith("wait "):
                    try:
                        wait_time = float(line.split(None, 1)[1])
                        print(f"  [wait {wait_time}s]")
                        time.sleep(wait_time)
                    except:
                        pass
                    continue

                # Send command
                self.send_command(line)
                sent += 1
                print(f"  > {line}")
                time.sleep(delay)

            print(f"[Script done: {sent} commands sent]")
        except Exception as e:
            print(f"[Script error: {e}]")

    def _list_scripts(self):
        """List available scripts."""
        if not os.path.exists(SCRIPT_DIR):
            print(f"[No scripts directory. Create {SCRIPT_DIR}/ and add .txt files]")
            return

        scripts = [f for f in os.listdir(SCRIPT_DIR)
                   if f.endswith(('.txt', '.script', '.cmd'))]
        if not scripts:
            print(f"[No scripts in {SCRIPT_DIR}/]")
            return

        print(f"Available scripts in {SCRIPT_DIR}/:")
        for s in sorted(scripts):
            print(f"  {s}")

    def _add_alias(self, arg):
        """Add a quick alias."""
        if not self.trigger_engine:
            print("[Trigger engine not loaded]")
            return
        if not arg or "|" not in arg:
            print("Usage: /alias <short> | <expanded>")
            return
        parts = arg.split("|", 1)
        short = parts[0].strip()
        expanded = parts[1].strip()
        self.trigger_engine.add_alias(short, expanded)
        print(f"[Alias added: {short} → {expanded}]")

    def _show_help(self):
        """Show help."""
        print("""
Local Commands:
  /status, /st      Show client status
  /triggers, /tr    Show loaded triggers
  /history, /h      Show command history
  /script [file]    Run a script (or list scripts)
  /alias s | cmd    Add an alias
  /clear, /cls      Clear screen
  /clear-output     Clear mud_output.txt
  /reconnect        Request mud_client reconnect
  /save             Save command history
  /quit, /exit      Exit terminal
  /help, /?         This help

Any other text is sent as a MUD command.
""")

    # ─── Main Loop ───────────────────────────────────────────────────

    def run(self):
        """Main terminal loop."""
        # Initialize output file if needed
        if not os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, "w") as f:
                f.write("")
        if not os.path.exists(CMDS_FILE):
            with open(CMDS_FILE, "w") as f:
                f.write("")

        # Start from end of current output
        try:
            with open(OUTPUT_FILE, "r") as f:
                f.seek(0, 2)
                self.last_output_pos = f.tell()
        except:
            pass

        print("=" * 60)
        print("  MUD Terminal")
        print("=" * 60)
        print(f"  Output: {OUTPUT_FILE}")
        print(f"  Commands: {CMDS_FILE}")
        print(f"  Triggers: {'ON' if self.trigger_engine else 'OFF'}")
        print()
        print("  Type /help for local commands")
        print("  Type /quit to exit")
        print("=" * 60)
        print()

        # Set up readline
        readline.parse_and_bind("tab: complete")
        readline.set_completer(self._completer)

        try:
            while self.running:
                # Check for new MUD output
                new_text = self.read_new_output()
                if new_text:
                    # Move cursor to start of line, clear it, print output,
                    # then re-show prompt
                    self.display_output(new_text)

                # Check stdin (non-blocking)
                try:
                    r, _, _ = select.select([sys.stdin], [], [], 0.2)
                    if r:
                        try:
                            line = input().strip()
                        except EOFError:
                            break

                        if not line:
                            continue

                        if line.startswith("/"):
                            if self.handle_local_command(line):
                                if not self.running:
                                    break
                                continue
                        else:
                            self.send_command(line)
                except (EOFError, KeyboardInterrupt):
                    break

        except Exception as e:
            print(f"\n[!] Error: {e}")
        finally:
            self.running = False
            if self.trigger_engine:
                self.trigger_engine.stop()
            self._save_history()
            print("\nDisconnected.")

    def _completer(self, text, state):
        """Tab completion for local commands and MUD commands."""
        commands = [
            "/status", "/triggers", "/history", "/script", "/alias",
            "/clear", "/clear-output", "/reconnect", "/save", "/help", "/quit",
            "/exit", "/cls", "/st", "/tr", "/h", "/sc", "/al", "/q",
        ]
        if text.startswith("/"):
            matches = [c for c in commands if c.startswith(text)]
        else:
            # Could add MUD command completion here
            matches = []
        return matches[state] if state < len(matches) else None


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="MUD Terminal UI — interactive interface for mud_client.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Requires mud_client.py to be running separately.

Examples:
  python3 terminal/ui.py                    # basic interactive mode
  python3 terminal/ui.py --triggers         # with trigger engine
  python3 terminal/ui.py --script cmds.txt  # run script then interactive
"""
    )
    parser.add_argument("--triggers", action="store_true",
                        help="Enable trigger/alias engine")
    parser.add_argument("--script", metavar="FILE",
                        help="Run a command script first, then go interactive")

    args = parser.parse_args()

    # Validate files exist
    if not os.path.exists(OUTPUT_FILE):
        print(f"[!] {OUTPUT_FILE} not found.")
        print(f"[!] Is mud_client.py running?")
        print(f"[!] Creating empty output file...")
        with open(OUTPUT_FILE, "w") as f:
            f.write("")

    term = MUDTerminal(use_triggers=args.triggers)

    # Run script first if requested
    if args.script:
        term._run_script(args.script)
        print()

    # Run interactive
    term.run()


if __name__ == "__main__":
    main()
