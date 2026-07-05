"""Genome registry: the menu of entry-signal and exit-style modules genome
evolution can choose between, plus genome load/validation.

Nothing here ever writes new logic — it only recombines and dispatches to
modules a human wrote and tested in src/modules/. Adding a new choice means
adding a function there and one line to a registry dict below.
"""
import json
import pathlib

from src.modules import entries, exits

ROOT = pathlib.Path(__file__).resolve().parent.parent

ENTRY_SIGNALS = {
    "ema_pullback": entries.ema_pullback,
    "breakout": entries.breakout,
    "mean_reversion": entries.mean_reversion,
}

EXIT_STYLES = {
    "atr_trail_half": exits.atr_trail_half,
    "fixed_r_multiple": exits.fixed_r_multiple,
}

DEFAULT_GENOME = {"entry_signal": "ema_pullback", "exit_style": "atr_trail_half"}


def validate_genome(genome: dict) -> None:
    """Schema check used before any genome is dispatched to."""
    if genome.get("entry_signal") not in ENTRY_SIGNALS:
        raise ValueError(f"genome invalid: unknown entry_signal '{genome.get('entry_signal')}'")
    if genome.get("exit_style") not in EXIT_STYLES:
        raise ValueError(f"genome invalid: unknown exit_style '{genome.get('exit_style')}'")


def load_genome(genome_id: str) -> dict:
    path = ROOT / "config" / "genomes" / f"{genome_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no such genome: config/genomes/{genome_id}.json")
    genome = json.loads(path.read_text())
    validate_genome(genome)
    return genome


def list_genomes() -> list[str]:
    return sorted(p.stem for p in (ROOT / "config" / "genomes").glob("*.json"))


def all_combinations() -> list[dict]:
    """Every possible (entry_signal, exit_style) pair — the full menu genome
    evolution screens. Not every combination needs a saved config/genomes/
    file; new ones are only written to disk once they win."""
    return [{"entry_signal": e, "exit_style": x}
            for e in ENTRY_SIGNALS for x in EXIT_STYLES]


def assemble(genome: dict | None):
    """Returns (entry_fn, exit_fn) for a genome dict, or the baseline pair
    if genome is None — preserves today's exact behavior when omitted."""
    genome = genome or DEFAULT_GENOME
    validate_genome(genome)
    return ENTRY_SIGNALS[genome["entry_signal"]], EXIT_STYLES[genome["exit_style"]]
