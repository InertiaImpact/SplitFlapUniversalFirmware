#!/usr/bin/env python3
"""
provision.py — Split-Flap Module Provisioning Tool
====================================================
Listens on the RS-485 bus for unprovisioned modules advertising their
ATtiny1616 serial numbers, then lets you:

  • Optionally home the module so you can physically identify which tile
    it is (the reel will spin to the blank/home position).
  • Assign a numeric bus ID (0–254) and persist it to the module's EEPROM.
  • De-provision one specific module (by ID) or all modules at once,
    returning them to the unprovisioned/advertising state.

Usage:
    python3 provision.py [--port /dev/ttyUSB0] [--baud 9600]

Defaults:
    --port  /dev/ttyUSB0
    --baud  9600

While the tool is running, press:
    d  →  open the de-provision menu
    q  →  quit

RS-485 message protocol (from firmware):
    Advertisement  :  mXadv:<serialNumber>\\n        (module → Pi)
    Home by SN     :  mXH<serialNumber>\\n            (Pi → module)
    Assign ID by SN:  mXI<serialNumber>:<id>\\n       (Pi → module)
    Ack            :  mXack:<serialNumber>:<id>\\n    (module → Pi)
    Reset (single) :  m<id>R\\n                       (Pi → module)
    Reset (all)    :  m*R\\n                          (Pi → module)
"""

import argparse
import sys
import time
import threading
import queue
import select
import termios
import tty

try:
    import serial
except ImportError:
    print("pyserial is required.  Install it with:  pip install pyserial")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# ANSI colour helpers (gracefully degrade on non-ANSI terminals)
# ─────────────────────────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
DIM    = "\033[2m"
MAGENTA = "\033[95m"


def c(code: str, text: str) -> str:
    """Wrap text in an ANSI escape code, but only when stdout is a real TTY."""
    if sys.stdout.isatty():
        return f"{code}{text}{RESET}"
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Serial helpers
# ─────────────────────────────────────────────────────────────────────────────
ACK_TIMEOUT = 2.0   # seconds to wait for mXack after an assign command


def send(ser: serial.Serial, msg: str) -> None:
    """Transmit a message over the RS-485 bus (appends \\n if missing)."""
    if not msg.endswith("\n"):
        msg += "\n"
    ser.write(msg.encode("ascii"))
    ser.flush()


def home_by_serial(ser: serial.Serial, sn: str) -> None:
    """Send the H provisioning command to home the module with this serial."""
    send(ser, f"mXH{sn}")


def assign_id(ser: serial.Serial, sn: str, new_id: int) -> None:
    """Send the I provisioning command to assign a bus ID."""
    send(ser, f"mXI{sn}:{new_id}")


def reset_module(ser: serial.Serial, module_id: int) -> None:
    """Send the R command to de-provision a single module by its bus ID."""
    send(ser, f"m{module_id}R")


def reset_all_modules(ser: serial.Serial) -> None:
    """Broadcast the R command to de-provision every module on the bus."""
    send(ser, "m*R")


# ─────────────────────────────────────────────────────────────────────────────
# Non-blocking single-keypress reader (raw terminal mode)
# ─────────────────────────────────────────────────────────────────────────────

class RawKeyReader:
    """
    Puts the terminal into raw mode so we can detect single keypresses
    (e.g. 'd' to open de-provision menu, 'q' to quit) without the user
    having to press Enter.  Restores normal mode around blocking prompts.
    """

    def __init__(self):
        self._old_settings = None
        self._active = False

    def enter_raw(self):
        if sys.stdin.isatty() and not self._active:
            self._old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            self._active = True

    def leave_raw(self):
        if self._active and self._old_settings is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old_settings)
            self._active = False

    def read_key(self, timeout: float = 0.2) -> str | None:
        """
        Return the next keypress character, or None if nothing arrived within
        `timeout` seconds.  Must be called while in raw mode.
        """
        if not sys.stdin.isatty():
            time.sleep(timeout)
            return None
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            return sys.stdin.read(1)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Background reader thread
# ─────────────────────────────────────────────────────────────────────────────

def reader_thread(ser: serial.Serial, line_q: queue.Queue,
                  stop_event: threading.Event) -> None:
    """
    Read lines from the serial port and push them onto line_q.
    Runs in its own daemon thread so the main loop can block on user
    input without missing incoming advertisements.
    """
    buf = b""
    while not stop_event.is_set():
        try:
            chunk = ser.read(ser.in_waiting or 1)
        except serial.SerialException:
            break
        if chunk:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                decoded = line.decode("ascii", errors="replace").strip()
                if decoded:
                    line_q.put(decoded)
        else:
            time.sleep(0.01)


# ─────────────────────────────────────────────────────────────────────────────
# Provisioning session
# ─────────────────────────────────────────────────────────────────────────────

def provision_module(ser: serial.Serial, sn: str,
                     line_q: queue.Queue,
                     registry: dict,
                     key_reader: RawKeyReader) -> None:
    """
    Interactive session to identify and assign an ID to one module.

    Steps:
      1. Ask if the user wants to home the module to physically locate it.
      2. Ask for a numeric ID (or 's' to skip).
      3. Send the assign command and wait for acknowledgement.
    """
    # Switch to normal (cooked) terminal mode so input() works properly.
    key_reader.leave_raw()

    print()
    print("─" * 60)
    print(c(BOLD + CYAN, "  New module detected"))
    print(f"  Serial : {c(YELLOW, sn)}")
    print("─" * 60)

    # ── Step 1: optional home ─────────────────────────────────────────────
    while True:
        try:
            ans = input(c(BOLD, "  Home this module now to identify it? [y/N] ")).strip().lower()
        except EOFError:
            ans = ""
        if ans in ("y", "yes"):
            print(f"  {c(DIM, 'Sending home command...')}", end="", flush=True)
            home_by_serial(ser, sn)
            print(c(GREEN, " sent."))
            print(f"  {c(DIM, 'The reel will spin to the blank/home position.')}")
            break
        elif ans in ("n", "no", ""):
            break
        else:
            print(f"  {c(RED, 'Please enter y or n.')}")

    # ── Step 2: assign ID ─────────────────────────────────────────────────
    while True:
        try:
            raw = input(
                c(BOLD, "  Enter bus ID to assign (0–254), or 's' to skip: ")
            ).strip().lower()
        except EOFError:
            raw = "s"

        if raw in ("s", "skip"):
            print(f"  {c(DIM, 'Skipped — module will continue advertising.')}")
            key_reader.enter_raw()
            return

        try:
            new_id = int(raw)
        except ValueError:
            print(f"  {c(RED, 'Invalid input — enter a number between 0 and 254, or s to skip.')}")
            continue

        if not (0 <= new_id <= 254):
            print(f"  {c(RED, 'ID must be between 0 and 254.')}")
            continue

        if new_id in registry:
            existing_sn = registry[new_id]
            print(f"  {c(RED, f'ID {new_id} is already assigned to serial {existing_sn} this session. Choose another.')}")
            continue

        # Confirm
        try:
            confirm = input(
                c(BOLD, f"  Assign ID {c(YELLOW, str(new_id))} to serial {c(YELLOW, sn)}? [Y/n] ")
            ).strip().lower()
        except EOFError:
            confirm = "y"

        if confirm in ("n", "no"):
            continue

        # ── Step 3: send assign and wait for ack ──────────────────────────
        print(f"  {c(DIM, 'Sending assign command...')}", end="", flush=True)
        assign_id(ser, sn, new_id)
        print(c(GREEN, " sent."))
        print(f"  {c(DIM, f'Waiting for acknowledgement (up to {ACK_TIMEOUT:.0f}s)...')}", end="", flush=True)

        deadline = time.monotonic() + ACK_TIMEOUT
        acked = False
        while time.monotonic() < deadline:
            try:
                line = line_q.get(timeout=0.1)
            except queue.Empty:
                continue

            if line.startswith("mXack:"):
                parts = line[len("mXack:"):].split(":")
                if len(parts) == 2 and parts[0].upper() == sn.upper():
                    confirmed_id = parts[1].strip()
                    print(c(GREEN, f" acknowledged (ID {confirmed_id})."))
                    registry[new_id] = sn
                    acked = True
                    break
                else:
                    line_q.put(line)
            elif line.startswith("mXadv:"):
                line_q.put(line)

        if not acked:
            print(c(YELLOW, " no ack received."))
            print(f"  {c(DIM, 'The command may still have succeeded. Verify with a dump (m<id>d) if needed.')}")
            registry[new_id] = sn  # Record anyway to prevent duplicate suggestions

        break

    print(c(GREEN + BOLD, "  Done."))
    print()

    key_reader.enter_raw()
    _print_hotkey_hint()


# ─────────────────────────────────────────────────────────────────────────────
# De-provisioning menu
# ─────────────────────────────────────────────────────────────────────────────

def deprovision_menu(ser: serial.Serial, registry: dict,
                     seen_serials: set,
                     key_reader: RawKeyReader) -> None:
    """
    Interactive menu for de-provisioning one or all modules.

    De-provisioning sends the firmware's 'R' command, which erases the stored
    bus ID and causes the module to resume advertising.  Calibration data is
    preserved on the module.

    The session registry is updated so de-provisioned IDs become available
    for re-assignment and the module's serial is removed from seen_serials so
    the provisioning dialog will re-open when it starts advertising again.
    """
    key_reader.leave_raw()

    print()
    print("─" * 60)
    print(c(BOLD + MAGENTA, "  De-provision modules"))
    print("─" * 60)

    # Show which IDs are known this session
    if registry:
        print(c(BOLD, "  IDs provisioned this session:"))
        for mid, sn in sorted(registry.items()):
            print(f"    {c(YELLOW, str(mid).rjust(3))}  →  {c(DIM, sn)}")
    else:
        print(c(DIM, "  No modules provisioned in this session."))
        print(c(DIM, "  You can still de-provision by entering a known ID manually."))

    print()
    print(c(DIM, "  Options:"))
    print(c(DIM, "    Enter a module ID (0–254) to de-provision that module"))
    print(c(DIM, "    Enter 'all' to de-provision every module on the bus"))
    print(c(DIM, "    Enter 's' to go back"))
    print()

    while True:
        try:
            raw = input(c(BOLD, "  Choice: ")).strip().lower()
        except EOFError:
            raw = "s"

        if raw in ("s", "skip", "back", ""):
            print(f"  {c(DIM, 'Cancelled.')}")
            break

        # ── De-provision ALL ──────────────────────────────────────────────
        if raw == "all":
            try:
                confirm = input(
                    c(BOLD + RED, "  WARNING: This will de-provision EVERY module on the bus. Continue? [y/N] ")
                ).strip().lower()
            except EOFError:
                confirm = "n"

            if confirm not in ("y", "yes"):
                print(f"  {c(DIM, 'Aborted.')}")
                continue

            print(f"  {c(DIM, 'Sending broadcast reset...')}", end="", flush=True)
            reset_all_modules(ser)
            print(c(GREEN, " sent."))
            print(f"  {c(DIM, 'All modules will erase their IDs and begin advertising again.')}")

            # Clear the full session registry and re-open seen serials
            # so each module gets a fresh provisioning dialog when it reappears.
            registry.clear()
            seen_serials.clear()

            print(c(GREEN + BOLD, "  All modules de-provisioned."))
            break

        # ── De-provision ONE ──────────────────────────────────────────────
        try:
            target_id = int(raw)
        except ValueError:
            print(f"  {c(RED, 'Invalid input — enter a module ID, all, or s to go back.')}")
            continue

        if not (0 <= target_id <= 254):
            print(f"  {c(RED, 'ID must be between 0 and 254.')}")
            continue

        known_sn = registry.get(target_id, None)
        sn_hint  = f"  (serial: {c(DIM, known_sn)})" if known_sn else "  (serial: unknown — not seen this session)"

        try:
            confirm = input(
                c(BOLD, f"  De-provision module {c(YELLOW, str(target_id))}{sn_hint}? [Y/n] ")
            ).strip().lower()
        except EOFError:
            confirm = "y"

        if confirm in ("n", "no"):
            continue

        print(f"  {c(DIM, f'Sending reset to module {target_id}...')}", end="", flush=True)
        reset_module(ser, target_id)
        print(c(GREEN, " sent."))
        print(f"  {c(DIM, 'Module will erase its ID and begin advertising again shortly.')}")

        # Remove from registry so the ID is free and the module gets re-prompted
        # when its advertisement arrives.
        if target_id in registry:
            freed_sn = registry.pop(target_id)
            seen_serials.discard(freed_sn)

        print(c(GREEN + BOLD, f"  Module {target_id} de-provisioned."))
        break

    print()
    key_reader.enter_raw()
    _print_hotkey_hint()


# ─────────────────────────────────────────────────────────────────────────────
# Misc UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _print_hotkey_hint() -> None:
    print(c(DIM, "  Listening…  Press d to de-provision  |  q to quit"))


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision split-flap modules via RS-485."
    )
    parser.add_argument(
        "--port", default="/dev/ttyUSB0",
        help="Serial port for the RS-485 adapter (default: /dev/ttyUSB0)"
    )
    parser.add_argument(
        "--baud", type=int, default=9600,
        help="Baud rate (default: 9600 — must match firmware)"
    )
    args = parser.parse_args()

    # ── Open serial port ─────────────────────────────────────────────────────
    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.1,
        )
    except serial.SerialException as exc:
        print(c(RED, f"Could not open {args.port}: {exc}"))
        sys.exit(1)

    print()
    print(c(BOLD, "  Split-Flap Module Provisioner"))
    print(c(DIM,  f"  Port: {args.port}  Baud: {args.baud}"))
    print()
    print("  Listening for unprovisioned modules advertising on the bus.")
    print(c(DIM,  "  Modules advertise every 10 seconds."))
    print()
    _print_hotkey_hint()
    print()

    # ── Start background reader ───────────────────────────────────────────────
    line_q: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    t = threading.Thread(
        target=reader_thread,
        args=(ser, line_q, stop_event),
        daemon=True,
    )
    t.start()

    # Session state
    # registry  : {bus_id: serial_string}   — modules provisioned this session
    # seen_serials : {serial_string}         — serials already handled (prevents re-prompt)
    registry:      dict = {}
    seen_serials:  set  = set()

    key_reader = RawKeyReader()
    key_reader.enter_raw()

    # ── Main loop ─────────────────────────────────────────────────────────────
    try:
        while True:

            # ── Check for keypress ────────────────────────────────────────
            key = key_reader.read_key(timeout=0.05)
            if key:
                if key.lower() == "q":
                    print()
                    print(c(DIM, "  Quitting."))
                    break
                elif key.lower() == "d":
                    print()
                    deprovision_menu(ser, registry, seen_serials, key_reader)

            # ── Drain the serial queue ────────────────────────────────────
            try:
                line = line_q.get_nowait()
            except queue.Empty:
                continue

            # ── Advertisement: mXadv:<serialNumber> ──────────────────────
            if line.startswith("mXadv:"):
                sn = line[len("mXadv:"):].strip().upper()

                if not sn:
                    continue

                if sn in seen_serials:
                    # Already handled — just a reminder pulse.
                    print(c(DIM, f"\r  [ {sn} still advertising — not yet provisioned ]"))
                    _print_hotkey_hint()
                    continue

                seen_serials.add(sn)
                provision_module(ser, sn, line_q, registry, key_reader)

            # ── Late ack (arrived outside a provisioning window) ──────────
            elif line.startswith("mXack:"):
                parts = line[len("mXack:"):].split(":")
                if len(parts) == 2:
                    ack_sn = parts[0].upper()
                    ack_id = parts[1].strip()
                    print(c(DIM, f"\r  [ late ack: serial {ack_sn} → ID {ack_id} ]"))
                    _print_hotkey_hint()

            # ── Everything else is bus traffic from provisioned modules ────
            # (silently ignored)

    except KeyboardInterrupt:
        print()
        print(c(DIM, "  Interrupted — exiting."))
    finally:
        key_reader.leave_raw()
        stop_event.set()
        ser.close()


if __name__ == "__main__":
    main()
