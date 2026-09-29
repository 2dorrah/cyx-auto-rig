#!/usr/bin/env python3
"""
CYTHANX ORGANISM — AUTOMATED TERMUX XMRIG SUPERVISOR
=====================================================

This is a Termux-friendly Python controller that keeps the CYTHANX /
CYTHANX ORGANISM state engine alongside a real XMRig process.

CYTHANX pipeline:
    BLAKE3 -> SHA-256d -> Scrypt -> BLAKE3 commitment

JOATT:
    BLAKE3 -> SHA-256d -> PBKDF2-HMAC-SHA512 -> Scrypt -> BLAKE3

ORGANISM:
    perception -> entropy -> adaptation -> DYNO-256 state

XMRig remains responsible for the actual pool mining algorithm.  The
CYTHANX organism is a supervisory/state layer; it is not claimed to be
a native XMRig hashing algorithm.

AUTOMATION:
  * starts XMRig
  * continuously runs CYTHANX organism cycles
  * writes live state to cythanx_state.json
  * restarts XMRig after an unexpected exit
  * handles Ctrl-C / SIGTERM cleanly
  * supports command-line arguments and environment variables
  * can run entirely from Termux

Environment variables:
  CYTHANX_XMRIG       path to xmrig
  CYTHANX_POOL        pool URL:PORT
  CYTHANX_USER        wallet/worker
  CYTHANX_PASS        pool password
  CYTHANX_ALGO        XMRig algorithm (default rx/0)
  CYTHANX_THREADS     optional thread count
  CYTHANX_STATE       state-file path

Example:
  export CYTHANX_POOL='pool.example:3333'
  export CYTHANX_USER='WALLET.WORKER'
  python cythanx_xmrig_termux.py --auto

Or:
  python cythanx_xmrig_termux.py --auto \
      --xmrig ./xmrig \
      --pool pool.example:3333 \
      --user WALLET.WORKER
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NETWORK = "CYTHAN-MAINNET"
CHAIN_ID = "CYTHAN-04-BED-BTCADDR"
VECTOR_KEY = b"CYTHAN-MAINNET-V1"

SCRYPT_N = 1024
SCRYPT_R = 1
SCRYPT_P = 1
SCRYPT_DKLEN = 32
PBKDF2_ROUNDS = 2048

DEFAULT_ALGO = "rx/0"
DEFAULT_STATE_FILE = "cythanx_state.json"
DEFAULT_INTERVAL = 10.0
DEFAULT_RESTART_DELAY = 5.0


# ---------------------------------------------------------------------------
# Optional BLAKE3 dependency
# ---------------------------------------------------------------------------

try:
    from blake3 import blake3
except ImportError:
    blake3 = None


def require_blake3() -> None:
    if blake3 is None:
        raise RuntimeError(
            "Python package 'blake3' is required.\n"
            "Install it in Termux with:\n"
            "  python -m pip install blake3"
        )


# ---------------------------------------------------------------------------
# CYTHANX cryptographic/state primitives
# ---------------------------------------------------------------------------

def blake3_256(data: bytes) -> bytes:
    require_blake3()
    return blake3(data).digest(length=32)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def double_sha256(data: bytes) -> bytes:
    return sha256(sha256(data))


def scrypt256(data: bytes, salt: Optional[bytes] = None) -> bytes:
    if salt is None:
        salt = data
    return hashlib.scrypt(
        password=data,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def pbkdf2_sha512(data: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha512",
        data,
        salt,
        PBKDF2_ROUNDS,
        64,
    )


def joaat(data: bytes) -> int:
    h = 0
    for byte in data:
        h = (h + byte) & 0xFFFFFFFF
        h = (h + ((h << 10) & 0xFFFFFFFF)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + ((h << 3) & 0xFFFFFFFF)) & 0xFFFFFFFF
    h ^= h >> 11
    h = (h + ((h << 15) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return h & 0xFFFFFFFF


def cythan_vector() -> tuple[bytes, int]:
    vector = blake3_256(
        b"CYTHAN|VECTOR|"
        + CHAIN_ID.encode()
        + b"|"
        + VECTOR_KEY
    )
    return vector, joaat(vector)


def cythanize(payload: bytes, previous: bytes = b"\x00" * 32) -> dict:
    vector, checksum = cythan_vector()
    checksum_bytes = checksum.to_bytes(4, "big")

    state = blake3_256(
        b"CYTHAN|STATE|"
        + payload
        + previous
        + vector
        + checksum_bytes
    )

    sha_state = double_sha256(b"CYTHAN|SHA256|" + state)
    scrypt_state = scrypt256(b"CYTHAN|SCRYPT|" + sha_state)

    commitment = blake3_256(
        b"CYTHAN|COMMITMENT|"
        + vector
        + checksum_bytes
        + state
        + sha_state
        + scrypt_state
    )

    return {
        "vector": vector.hex(),
        "joaat": f"{checksum:08x}",
        "state": state.hex(),
        "sha256": sha_state.hex(),
        "scrypt": scrypt_state.hex(),
        "commitment": commitment.hex(),
    }


# ---------------------------------------------------------------------------
# CYTHANX ORGANISM
# ---------------------------------------------------------------------------

def organism_enigma(data: bytes) -> bytes:
    def rotor(buf: bytes, value: int) -> bytes:
        out = bytearray(len(buf))
        for i, byte in enumerate(buf):
            x = (byte + value + i * 17) & 0xFF
            x ^= (value * 29 + i * 7) & 0xFF
            out[i] = ((x << 3) | (x >> 5)) & 0xFF
        return bytes(out)

    x = rotor(data, 11)
    x = rotor(x, 37)
    x = rotor(x, 73)
    x = bytes(255 - b for b in x)
    x = rotor(x, 73)
    x = rotor(x, 37)
    return rotor(x, 11)


def organism_joatt(data: bytes, previous: bytes = b"") -> dict:
    root = blake3_256(data + previous)
    tree = double_sha256(root + data)
    stretched = pbkdf2_sha512(tree, b"CYTHANX|JOATT|2048")
    memory = scrypt256(stretched[:32], b"CYTHANX|MEMORY")
    mix = blake3_256(root + tree + memory)

    return {
        "root": root.hex(),
        "sha256d": tree.hex(),
        "memory": memory.hex(),
        "state256": mix.hex(),
        "identifier64": mix[:8].hex(),
        "adler32": f"{zlib.adler32(mix) & 0xFFFFFFFF:08x}",
    }


def organism_cythanize(data: bytes, domain: bytes = b"VECTOR") -> dict:
    vector = blake3_256(b"CYTHANX|" + domain + b"|" + data)
    digest = double_sha256(vector)
    work = scrypt256(digest, b"CYTHANX|VECTOR|SCRYPT")
    commitment = blake3_256(vector + digest + work)

    return {
        "vector": vector.hex(),
        "sha256d": digest.hex(),
        "scrypt": work.hex(),
        "commitment": commitment.hex(),
    }


def organism_transmogrify(data: bytes, rounds: int = 3) -> bytes:
    x = data
    for i in range(rounds):
        x = organism_enigma(x)
        x = blake3_256(
            b"CYTHANX|TRANSMOGRIFY|"
            + bytes([i & 0xFF])
            + x
        )
        x = scrypt256(x, f"round-{i}".encode())
    return x


@dataclass
class OrganismState:
    energy: float = 1.0
    entropy: float = 0.0
    health: float = 1.0
    generation: int = 0

    def perceive(self, payload: bytes) -> float:
        x = int.from_bytes(
            blake3_256(payload)[:8],
            "big",
        ) / 2**64
        self.entropy = -math.log2(max(x, 2**-64)) / 64.0
        return x

    def adapt(self, signal: bytes) -> None:
        x = self.perceive(signal)
        learning = 0.02 + 0.08 * x

        self.energy = max(
            0.0,
            min(2.0, self.energy * (0.98 + learning)),
        )

        self.health = max(
            0.0,
            min(
                1.0,
                0.97 * self.health
                + 0.03 * (1.0 - abs(x - 0.5)),
            ),
        )

        self.generation += 1


def run_organism(seed: str = "CYTHANX", rounds: int = 8) -> dict:
    if not 1 <= rounds <= 1000:
        raise ValueError("rounds must be between 1 and 1000")

    seed_bytes = seed.encode("utf-8")

    # 80-byte deterministic header/state seed.
    header = (
        b"CYTHANX-HEADER"
        + hashlib.sha256(seed_bytes).digest()
        + blake3_256(seed_bytes)
        + int(time.time()).to_bytes(4, "big")
        + b"\x00" * 24
    )[:80].ljust(80, b"\x00")

    state = blake3_256(
        b"CYTHANX|GENESIS|" + seed_bytes
    )

    organism = OrganismState()
    previous = b""
    history = []

    for i in range(rounds):
        jo = organism_joatt(
            organism_enigma(header + state),
            previous,
        )

        vector = organism_cythanize(
            bytes.fromhex(jo["state256"]),
            b"ORGANISM",
        )

        transformed = organism_transmogrify(
            bytes.fromhex(vector["commitment"]),
            3 + (i % 3),
        )

        aux128 = blake3_256(
            b"CYTHANX|AUX128|" + transformed
        )[:16]

        state = blake3_256(
            b"CYTHANX|DYNO256|"
            + state
            + aux128
            + i.to_bytes(8, "big")
        )

        organism.adapt(aux128)

        final = blake3_256(
            b"CYTHANX|IMPOSSIBLE|"
            + header
            + state
            + bytes.fromhex(vector["commitment"])
            + organism.generation.to_bytes(8, "big")
        )

        previous = bytes.fromhex(jo["state256"])

        sample = int.from_bytes(
            state[:4],
            "big",
        ) / 0xFFFFFFFF

        history.append({
            "round": i,
            "joatt_id": jo["identifier64"],
            "dyno_difficulty": round(
                1 + 15 * sample,
                8,
            ),
            "organism_energy": round(
                organism.energy,
                8,
            ),
            "organism_health": round(
                organism.health,
                8,
            ),
            "final_state": final.hex(),
        })

        header = blake3_256(
            header + state + final
        )

    return {
        "name": "CYTHANX ORGANISM",
        "network": NETWORK,
        "chain_id": CHAIN_ID,
        "seed": seed,
        "rounds": rounds,
        "final_dyno_state": state.hex(),
        "organism": asdict(organism),
        "history": history,
    }


def organism_self_test() -> dict:
    result = run_organism(
        "CYTHANX-SELF-TEST",
        2,
    )

    assert len(
        bytes.fromhex(result["final_dyno_state"])
    ) == 32

    assert result["organism"]["generation"] == 2

    return {
        "ok": True,
        "generation": result["organism"]["generation"],
        "final_dyno_state": result["final_dyno_state"],
    }


# ---------------------------------------------------------------------------
# AUTOMATION STATE
# ---------------------------------------------------------------------------

class Controller:
    def __init__(
        self,
        state_file: Path,
        seed: str,
        rounds: int,
        interval: float,
    ):
        self.state_file = state_file
        self.seed = seed
        self.rounds = rounds
        self.interval = max(1.0, interval)

        self.stop = threading.Event()
        self.lock = threading.Lock()

        self.cycles = 0
        self.restarts = 0
        self.last_result: Optional[dict] = None
        self.started_at = time.time()

    def save_state(self, event: str = "organism_cycle") -> None:
        result = self.last_result or {}

        payload = {
            "controller": "CYTHANX-TERMUX-AUTO",
            "network": NETWORK,
            "event": event,
            "timestamp": int(time.time()),
            "uptime_seconds": round(
                time.time() - self.started_at,
                3,
            ),
            "cycles": self.cycles,
            "restarts": self.restarts,
            "organism": result.get(
                "organism",
                {},
            ),
            "final_dyno_state": result.get(
                "final_dyno_state",
                "",
            ),
            "last_history": (
                result.get("history", [])[-1]
                if result.get("history")
                else {}
            ),
        }

        tmp = self.state_file.with_suffix(
            self.state_file.suffix + ".tmp"
        )

        try:
            tmp.write_text(
                json.dumps(
                    payload,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.state_file)
        except OSError as exc:
            print(
                f"[CYTHANX] state write failed: {exc}",
                file=sys.stderr,
            )

    def organism_loop(self) -> None:
        while not self.stop.is_set():
            try:
                result = run_organism(
                    self.seed,
                    self.rounds,
                )

                with self.lock:
                    self.last_result = result
                    self.cycles += 1

                self.save_state()

                org = result["organism"]
                state = result["final_dyno_state"]

                print(
                    "[CYTHANX] "
                    f"cycle={self.cycles} "
                    f"generation={org['generation']} "
                    f"energy={org['energy']:.6f} "
                    f"health={org['health']:.6f} "
                    f"entropy={org['entropy']:.6f} "
                    f"state={state[:24]}..."
                )

            except Exception as exc:
                print(
                    f"[CYTHANX] organism error: {exc}",
                    file=sys.stderr,
                )

            self.stop.wait(self.interval)

    def signal_stop(self, signum, _frame) -> None:
        print(
            f"\n[CYTHANX] signal {signum}; shutting down..."
        )
        self.stop.set()


# ---------------------------------------------------------------------------
# XMRIG PROCESS CONTROL
# ---------------------------------------------------------------------------

def resolve_xmrig(requested: Optional[str]) -> str:
    candidate = (
        requested
        or os.environ.get("CYTHANX_XMRIG")
        or "xmrig"
    )

    found = shutil.which(candidate)

    if found:
        return found

    direct = Path(candidate).expanduser()

    if direct.is_file():
        if not os.access(direct, os.X_OK):
            raise RuntimeError(
                f"XMRig exists but is not executable: {direct}\n"
                f"Run: chmod +x {direct}"
            )
        return str(direct)

    raise FileNotFoundError(
        "XMRig was not found.\n"
        "Put xmrig on PATH or use --xmrig /path/to/xmrig."
    )


def build_xmrig_command(
    args: argparse.Namespace,
    xmrig: str,
) -> list[str]:
    pool = args.pool or os.environ.get("CYTHANX_POOL")
    user = args.user or os.environ.get("CYTHANX_USER")
    password = (
        args.password
        if args.password is not None
        else os.environ.get("CYTHANX_PASS", "x")
    )
    algo = (
        args.algo
        or os.environ.get(
            "CYTHANX_ALGO",
            DEFAULT_ALGO,
        )
    )

    if not pool:
        raise ValueError(
            "No pool supplied. Use --pool POOL:PORT "
            "or CYTHANX_POOL."
        )

    if not user:
        raise ValueError(
            "No wallet/worker supplied. Use --user WALLET.WORKER "
            "or CYTHANX_USER."
        )

    cmd = [
        xmrig,
        "--algo",
        algo,
        "--url",
        pool,
        "--user",
        user,
        "--pass",
        password or "x",
        "--keepalive",
    ]

    if args.tls:
        cmd.append("--tls")

    if args.threads:
        cmd += [
            "--threads",
            str(args.threads),
        ]

    if args.config:
        cmd += [
            "--config",
            args.config,
        ]

    return cmd


def run_xmrig_once(
    controller: Controller,
    args: argparse.Namespace,
) -> int:
    xmrig = resolve_xmrig(args.xmrig)
    command = build_xmrig_command(
        args,
        xmrig,
    )

    printable = []
    for item in command:
        if (
            item == args.password
            and item
        ):
            printable.append("***")
        else:
            printable.append(item)

    print("[XMRIG] starting:")
    print(" ".join(printable))

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
    )

    while not controller.stop.is_set():
        code = process.poll()

        if code is not None:
            return int(code)

        time.sleep(0.5)

    # Clean shutdown.
    try:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    except ProcessLookupError:
        pass

    return 0


def automated_supervisor(
    controller: Controller,
    args: argparse.Namespace,
) -> int:
    monitor = threading.Thread(
        target=controller.organism_loop,
        daemon=True,
        name="cythanx-organism",
    )
    monitor.start()

    while not controller.stop.is_set():
        try:
            exit_code = run_xmrig_once(
                controller,
                args,
            )
        except Exception as exc:
            print(
                f"[XMRIG] launch error: {exc}",
                file=sys.stderr,
            )
            exit_code = 127

        controller.save_state(
            event=f"xmrig_exit_{exit_code}"
        )

        if controller.stop.is_set():
            break

        controller.restarts += 1

        if (
            args.max_restarts >= 0
            and controller.restarts > args.max_restarts
        ):
            print(
                "[XMRIG] restart limit reached; stopping."
            )
            break

        print(
            f"[XMRIG] exited with code {exit_code}; "
            f"restart {controller.restarts} "
            f"in {args.restart_delay:.1f}s"
        )

        if controller.stop.wait(
            max(0.0, args.restart_delay)
        ):
            break

    controller.stop.set()
    controller.save_state(
        event="shutdown"
    )

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def env_or(value: Optional[str], name: str, default=None):
    if value is not None:
        return value
    return os.environ.get(name, default)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "CYTHANX ORGANISM automated Termux XMRig supervisor"
        )
    )

    parser.add_argument(
        "--auto",
        action="store_true",
        help="run organism + XMRig with automatic restart",
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
    )

    parser.add_argument(
        "--organism",
        action="store_true",
        help="run one CYTHANX organism calculation",
    )

    parser.add_argument(
        "--seed",
        default=os.environ.get(
            "CYTHANX_SEED",
            "CYTHANX",
        ),
    )

    parser.add_argument(
        "--rounds",
        type=int,
        default=int(
            os.environ.get(
                "CYTHANX_ROUNDS",
                "8",
            )
        ),
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=float(
            os.environ.get(
                "CYTHANX_INTERVAL",
                str(DEFAULT_INTERVAL),
            )
        ),
    )

    parser.add_argument(
        "--state-file",
        default=os.environ.get(
            "CYTHANX_STATE",
            DEFAULT_STATE_FILE,
        ),
    )

    parser.add_argument(
        "--xmrig",
        default=None,
        help="XMRig executable",
    )

    parser.add_argument(
        "--pool",
        default=None,
        help="pool host:port",
    )

    parser.add_argument(
        "--user",
        default=None,
        help="wallet or wallet.worker",
    )

    parser.add_argument(
        "--password",
        default=None,
        help="pool password; default x",
    )

    parser.add_argument(
        "--algo",
        default=None,
        help=f"XMRig algorithm; default {DEFAULT_ALGO}",
    )

    parser.add_argument(
        "--tls",
        action="store_true",
    )

    parser.add_argument(
        "--threads",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--config",
        default=None,
    )

    parser.add_argument(
        "--restart-delay",
        type=float,
        default=float(
            os.environ.get(
                "CYTHANX_RESTART_DELAY",
                str(DEFAULT_RESTART_DELAY),
            )
        ),
    )

    parser.add_argument(
        "--max-restarts",
        type=int,
        default=int(
            os.environ.get(
                "CYTHANX_MAX_RESTARTS",
                "-1",
            )
        ),
        help="-1 means unlimited",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        require_blake3()

        if args.rounds < 1 or args.rounds > 1000:
            raise ValueError(
                "--rounds must be between 1 and 1000"
            )

        if args.self_test:
            print(
                json.dumps(
                    organism_self_test(),
                    indent=2,
                )
            )
            return 0

        if args.organism:
            print(
                json.dumps(
                    run_organism(
                        args.seed,
                        args.rounds,
                    ),
                    indent=2,
                )
            )
            return 0

        if not args.auto:
            print(
                "CYTHANX ready.\n\n"
                "Quick tests:\n"
                "  python cythanx_xmrig_termux.py --self-test\n"
                "  python cythanx_xmrig_termux.py --organism\n\n"
                "Automated mode:\n"
                "  python cythanx_xmrig_termux.py --auto "
                "--pool POOL:PORT --user WALLET.WORKER"
            )
            return 0

        controller = Controller(
            state_file=Path(args.state_file),
            seed=args.seed,
            rounds=args.rounds,
            interval=args.interval,
        )

        signal.signal(
            signal.SIGINT,
            controller.signal_stop,
        )

        if hasattr(signal, "SIGTERM"):
            signal.signal(
                signal.SIGTERM,
                controller.signal_stop,
            )

        return automated_supervisor(
            controller,
            args,
        )

    except KeyboardInterrupt:
        print("\n[CYTHANX] interrupted")
        return 130

    except Exception as exc:
        print(
            f"[ERROR] {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
