#!/usr/bin/env python3
"""Import public Warcraft Logs UI exports as evidence, without an API token."""

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

CLASSES = {"deathknight", "demonhunter", "druid", "evoker", "hunter", "mage",
           "monk", "paladin", "priest", "rogue", "shaman", "warlock", "warrior"}


def milliseconds(value):
    match = re.fullmatch(r"(-?)(\d+):([0-5]\d)\.(\d{3})", value)
    if not match:
        raise ValueError(f"Expected a WCL mm:ss.sss timestamp, got {value!r}")
    sign, minutes, seconds, fraction = match.groups()
    result = (int(minutes) * 60 + int(seconds)) * 1000 + int(fraction)
    return -result if sign else result


def read_events(paths, allow_empty=False):
    events, sources = [], []
    previous = None
    for path in paths:
        raw = Path(path).read_bytes()
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not {"Time", "Type", "Ability", "Source → Target"} <= set(reader.fieldnames or []):
            raise ValueError("Expected an English Warcraft Logs events CSV export")
        count = 0
        for row in reader:
            timestamp = milliseconds(row["Time"])
            if previous is not None and timestamp < previous:
                raise ValueError("Event pages must be ordered and must not overlap")
            previous = timestamp
            events.append({"time_ms": timestamp, "display": row})
            count += 1
        if count == 0 and not allow_empty:
            raise ValueError("Event exports must contain data rows")
        sources.append({"file": Path(path).name, "sha256": hashlib.sha256(raw).hexdigest(),
                        "rows": count})
    return events, sources


def pi_windows(events, player):
    active = None
    windows = []
    for event in events:
        row, time = event["display"], event["time_ms"]
        if row["Ability"] != "Power Infusion":
            raise ValueError("PI export contains another ability; select only Power Infusion")
        if row["Source → Target"].rsplit(" → ", 1)[-1] != player:
            raise ValueError("PI export contains a different recipient")
        if row["Type"] == "Apply Buff":
            if active is not None:
                raise ValueError("PI applied while already active")
            active = time
        elif row["Type"] == "Refresh Buff":
            if active is None:
                raise ValueError("PI refresh is missing its application")
        elif row["Type"] == "Remove Buff":
            if active is None:
                raise ValueError("PI removal is missing its application")
            if time > max(0, active):
                windows.append({"start": max(0, active) / 1000,
                                "duration": (time - max(0, active)) / 1000})
            active = None
        else:
            raise ValueError(f"Unsupported PI event type: {row['Type']}")
    if active is not None:
        raise ValueError("PI removal is missing; a CSV export alone does not establish fight end")
    return windows


def import_exports(report_url, player, profile, pi_paths, cast_paths):
    url = urlparse(report_url)
    match = re.fullmatch(r"/reports/([A-Za-z0-9]{16})", url.path.rstrip("/"))
    query = parse_qs(url.query)
    if url.scheme != "https" or url.hostname not in {"www.warcraftlogs.com", "warcraftlogs.com"} or not match:
        raise ValueError("Use the public Warcraft Logs report URL for the selected player")
    try:
        fight, actor = int(query["fight"][0]), int(query["source"][0])
    except (KeyError, ValueError):
        raise ValueError("Report URL must include numeric fight and source actor IDs") from None
    if fight <= 0 or actor <= 0:
        raise ValueError("Fight and source actor IDs must be positive")
    profile_text = Path(profile).read_text()
    fields, declarations = {}, []
    for line in profile_text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip().strip('"')
            if key.strip() in CLASSES:
                declarations.append(key.strip())
    player_classes = CLASSES & fields.keys()
    if len(declarations) != 1 or len(player_classes) != 1 or fields[next(iter(player_classes))] != player:
        raise ValueError("Profile must contain the selected player and exactly one class")
    for required in ("level", "spec", "talents"):
        if not fields.get(required):
            raise ValueError(f"Logged profile is missing {required}")
    buffs, buff_sources = read_events(pi_paths, allow_empty=True)
    casts, cast_sources = read_events(cast_paths)
    for event in casts:
        row = event["display"]
        if row["Type"] not in {"Cast", "Begin Cast"}:
            raise ValueError("Cast export contains another event type")
        # WCL combines the name, displayed cast duration and cancellation status in one CSV cell.
        name = re.sub(r" (?:\d+(?:\.\d+)? sec|Canceled)$", "", row["Ability"])
        event["ability_name"] = name
        event["canceled"] = row["Ability"].endswith(" Canceled")
    windows = pi_windows(buffs, player)
    gaps = [
        "CSV has display names rather than spell IDs and full actor/resource/buff state.",
        "CSV pages alone cannot establish complete event coverage or exact fight duration.",
        "Historical game patch and player decision policy have not been reconstructed or validated.",
    ]
    if not fields.get("race"):
        gaps.append("Warcraft Logs character export omits race; do not silently assume a racial ability.")
    return {
        "schema": "simc.pi.log_exports.v1", "ranking_eligible": False,
        "report_url": report_url, "report_code": match[1], "fight_id": fight,
        "actor_id": actor, "player": player,
        "profile": {"text": profile_text, "sha256": hashlib.sha256(profile_text.encode()).hexdigest()},
        "pi_sources": buff_sources, "cast_sources": cast_sources,
        "power_infusion_windows": windows, "pi_events": buffs, "cast_events": casts,
        "with_pi_options": [f"external_buffs.disable_power_infusion={int(not windows)}", "external_buffs.power_infusion=",
                            "external_buffs.power_infusion_windows=" + "/".join(
                                f'{w["start"]:.3f}:{w["duration"]:.3f}' for w in windows)],
        "without_pi_options": ["external_buffs.disable_power_infusion=1"],
        "data_gaps": gaps,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-url", required=True)
    parser.add_argument("--player", required=True)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--pi-events", required=True, nargs="+", type=Path)
    parser.add_argument("--casts", required=True, nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = import_exports(args.report_url, args.player, args.profile, args.pi_events, args.casts)
        with args.output.open("x") as output:
            json.dump(result, output, indent=2, allow_nan=False)
            output.write("\n")
        print(json.dumps({"output": str(args.output), "player": args.player,
                          "pi_windows": len(result["power_infusion_windows"]),
                          "cast_rows": len(result["cast_events"]), "ranking_eligible": False}))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
