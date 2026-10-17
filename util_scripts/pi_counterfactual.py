#!/usr/bin/env python3
"""Import a WCL pull and diagnose paired PI/no-PI simulations. No ranking scores."""

import argparse
import base64
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

PI = 10060
ENDPOINT = "https://www.warcraftlogs.com/api/v2/client"
METADATA_QUERY = """
query Pull($code: String!, $fights: [Int]) {
  reportData { report(code: $code) {
    code startTime revision zone { id name }
    fights(fightIDs: $fights) {
      id name startTime endTime encounterID difficulty kill
      friendlyPlayers friendlySpecs keystoneLevel keystoneTime
    }
    masterData { logVersion gameVersion lang actors { id name type subType petOwner } }
  } }
}
"""
EVENTS_QUERY = """
query Events($code: String!, $fights: [Int], $start: Float!, $end: Float!) {
  reportData { report(code: $code) {
    events(fightIDs: $fights, startTime: $start, endTime: $end,
           limit: 10000, includeResources: true, useAbilityIDs: false) {
      data nextPageTimestamp
    }
  } }
}
"""


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value


def client_access_token(path):
    credentials = read_json(path)
    client_id, client_secret = credentials["client_id"], credentials["client_secret"]
    if not all(isinstance(value, str) and value for value in (client_id, client_secret)):
        raise ValueError("Client credential file requires nonempty client_id and client_secret strings")
    authorization = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = urllib.request.Request(
        "https://www.warcraftlogs.com/oauth/token", data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {authorization}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Warcraft Logs OAuth returned HTTP {exc.code}") from None
    token = result.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("Warcraft Logs OAuth response is missing an access token")
    return token


def graphql(query, variables, token=None):
    token = token or os.environ.get("WCL_ACCESS_TOKEN")
    if not token:
        raise ValueError("Set WCL_ACCESS_TOKEN to a Warcraft Logs client API access token")
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Warcraft Logs returned HTTP {exc.code}") from None
    if result.get("errors"):
        raise ValueError("Warcraft Logs GraphQL request failed; no capture was written")
    report = result.get("data", {}).get("reportData", {}).get("report")
    if report is None:
        raise ValueError("Warcraft Logs report is unavailable")
    return report


def capture(code, fight_id, request=graphql):
    if not re.fullmatch(r"[A-Za-z0-9]{16}", code):
        raise ValueError("Report code must contain 16 letters or digits")
    variables = {"code": code, "fights": [fight_id]}
    metadata = request(METADATA_QUERY, variables)
    fights = [f for f in metadata["fights"] if f["id"] == fight_id]
    if len(fights) != 1:
        raise ValueError("Requested fight was not found")
    fight = fights[0]
    cursor = number(fight["startTime"], "fight start")
    end = number(fight["endTime"], "fight end")
    if end <= cursor:
        raise ValueError("Fight must have positive duration")
    pages = []
    while True:
        page = request(EVENTS_QUERY, dict(variables, start=cursor, end=end))["events"]
        if not isinstance(page.get("data"), list):
            raise ValueError("Missing event page data")
        pages.append(page)
        next_time = page.get("nextPageTimestamp")
        if next_time is None:
            break
        next_time = number(next_time, "pagination cursor")
        if not cursor < next_time <= end:
            raise ValueError("Event pagination did not advance within the fight")
        cursor = next_time
    return {
        "schema": "simc.pi.capture.v1",
        "report": {key: metadata[key] for key in ("code", "startTime", "revision", "zone") if key in metadata},
        "master_data": {key: metadata["masterData"][key] for key in ("logVersion", "gameVersion", "lang")
                        if key in metadata["masterData"]},
        "fight": fight,
        "actors": metadata["masterData"]["actors"],
        "pages": pages,
    }


def ability(event):
    # API v2 with useAbilityIDs=false, or a raw event containing its ability object.
    value = event.get("abilityGameID", event.get("ability", {}).get("guid"))
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("Event is missing its game spell ID")
    return value


def owned_actors(actors, player_id):
    owners = {a["id"]: a.get("petOwner") for a in actors}
    result = {player_id}
    while True:
        added = {actor for actor, owner in owners.items() if owner in result} - result
        if not added:
            return result
        result.update(added)


def actor_state_observations(events, player_id, start):
    buffs, snapshots = [], []
    buff_types = {"applybuff", "refreshbuff", "removebuff", "applybuffstack", "removebuffstack"}
    stat_keys = ("hitPoints", "maxHitPoints", "attackPower", "spellPower", "armor", "absorb",
                 "versatility", "avoidance", "x", "y", "facing", "mapID")
    for event in events:
        time_ms = event["timestamp"] - start
        if event["type"] in buff_types and event.get("targetID") == player_id:
            buffs.append({"time_ms": time_ms, "type": event["type"], "spell_id": ability(event),
                          "source_id": event.get("sourceID"), "stacks": event.get("stack")})
        resource_actor = event.get("resourceActor")
        owns_snapshot = ((resource_actor == 1 and event.get("sourceID") == player_id) or
                         (resource_actor == 2 and event.get("targetID") == player_id))
        if owns_snapshot and event.get("classResources"):
            snapshots.append({"time_ms": time_ms, "event_type": event["type"],
                              "resources": event["classResources"],
                              "stats": {key: event[key] for key in stat_keys if key in event}})
    return {"buff_events": buffs, "resource_snapshots": snapshots}


def summarize(bundle, player_id):
    if bundle.get("schema") != "simc.pi.capture.v1":
        raise ValueError("Unsupported capture schema")
    fight = bundle["fight"]
    start = number(fight["startTime"], "fight start")
    end = number(fight["endTime"], "fight end")
    if end <= start:
        raise ValueError("Fight must have positive duration")
    actors = bundle["actors"]
    players = [a for a in actors if a["id"] == player_id and a["type"] == "Player"]
    if len(players) != 1:
        raise ValueError("Select exactly one player actor ID")
    pages = bundle["pages"]
    if not pages or pages[-1].get("nextPageTimestamp") is not None:
        raise ValueError("Capture is incomplete: final event page is missing")
    cursor = start
    events = []
    for index, page in enumerate(pages):
        if not isinstance(page.get("data"), list):
            raise ValueError("Missing event page data")
        for event in page["data"]:
            timestamp = number(event["timestamp"], "event timestamp")
            if not start <= timestamp <= end:
                raise ValueError("Event lies outside the captured fight")
            events.append(event)
        next_time = page.get("nextPageTimestamp")
        if index < len(pages) - 1:
            if next_time is None or not cursor < number(next_time, "pagination cursor") <= end:
                raise ValueError("Capture has an invalid pagination chain")
            cursor = next_time
    events.sort(key=lambda e: e["timestamp"])
    owned = owned_actors(actors, player_id)
    windows, casts, damage = [], [], []
    combatants = [e for e in events if e["type"] == "combatantinfo" and e.get("sourceID") == player_id]
    inherited_pi = any(e["timestamp"] == start and any(a.get("ability") == PI for a in e.get("auras", []))
                       for e in combatants)
    active = start if inherited_pi else None
    for event in events:
        kind = event["type"]
        time = (event["timestamp"] - start) / 1000
        if kind in {"applybuff", "refreshbuff", "removebuff"} and event.get("targetID") == player_id:
            if ability(event) == PI:
                if kind == "applybuff":
                    if active is not None and not (inherited_pi and event["timestamp"] == start):
                        raise ValueError("PI applied while already active; inspect the event capture")
                    inherited_pi = False
                    active = event["timestamp"]
                elif kind == "refreshbuff":
                    if active is None:
                        raise ValueError("PI refresh lacks its application; capture is insufficient")
                elif active is None:
                    raise ValueError("PI removal lacks its application; capture is insufficient")
                else:
                    if event["timestamp"] > active:
                        windows.append({"start": (active - start) / 1000,
                                        "duration": (event["timestamp"] - active) / 1000, "end_censored": False})
                    active = None
        if kind in {"begincast", "cast"} and event.get("sourceID") == player_id:
            casts.append({"time": time, "spell_id": ability(event), "type": kind,
                          "target_id": event.get("targetID"),
                          "resource_actor": event.get("resourceActor"),
                          "resources": event.get("classResources", []) if event.get("resourceActor") == 1 else []})
        if kind == "damage" and event.get("sourceID") in owned:
            amount = number(event.get("amount"), "damage amount")
            if amount < 0:
                raise ValueError("Damage amount must be nonnegative")
            damage.append({"time": time, "spell_id": ability(event), "amount": amount,
                           "source_id": event["sourceID"], "target_id": event.get("targetID")})
    duration = (end - start) / 1000
    if active is not None and active < end:
        windows.append({"start": (active - start) / 1000, "duration": (end - active) / 1000, "end_censored": True})
    if not combatants:
        raise ValueError("Player combatantinfo is missing; gear/talent reconstruction is unavailable")
    if not casts or not damage:
        raise ValueError("Player casts and damage are required")
    return {
        "schema": "simc.pi.observation.v1",
        "report": bundle["report"], "fight": fight, "player": players[0],
        "duration": duration, "power_infusion_windows": windows,
        "combatantinfo": combatants, "casts": casts, "damage": damage,
        "initial_auras": combatants[0].get("auras", []),
        "master_data": bundle.get("master_data", {}),
        **actor_state_observations(events, player_id, start),
        "owned_actor_ids": sorted(owned),
        "observed_damage": sum(e["amount"] for e in damage),
        "scope": "fixed recorded encounter timeline",
        "ranking_eligible": False,
    }


def mean(value):
    if isinstance(value, dict):
        value = value["mean"]
    return number(value, "simulation mean")


def report_player(report):
    players = report["sim"]["players"]
    if len(players) != 1 or "priest" in players[0].get("specialization", "").lower():
        raise ValueError("Prototype requires one non-priest player; priest cast costs need separate modeling")
    return players[0]


def diagnose(observation, with_pi, without_pi):
    if observation.get("schema") != "simc.pi.observation.v1":
        raise ValueError("Unsupported observation schema")
    baseline, alternate = report_player(with_pi), report_player(without_pi)
    for key in ("name", "specialization", "talents", "gear", "level", "race", "dbc"):
        if baseline.get(key) != alternate.get(key):
            raise ValueError(f"Simulation actors differ: {key}")
    for player in (baseline, alternate):
        if abs(mean(player["collected_data"]["fight_length"]) - observation["duration"]) > 0.001:
            raise ValueError("Simulation duration differs from the recorded encounter")
    expected_uptime = 100 * sum(w["duration"] for w in observation["power_infusion_windows"]) / observation["duration"]
    def pi_uptime(player):
        return sum(number(b["uptime"], "PI uptime") for b in player.get("buffs", []) + player.get("buffs_constant", [])
                   if b["name"] == "power_infusion")
    if abs(pi_uptime(baseline) - expected_uptime) > 0.01 or pi_uptime(alternate) != 0:
        raise ValueError("Simulation PI uptime does not match the requested intervention")
    duration = observation["duration"]
    observed = observation["observed_damage"]
    baseline_damage = mean(baseline["collected_data"]["compound_dmg"])
    alternate_damage = mean(alternate["collected_data"]["compound_dmg"])
    casts = Counter(str(e["spell_id"]) for e in observation["casts"] if e["type"] == "cast")
    cast_counts = Counter()
    for stat in baseline.get("stats", []):
        if stat.get("id") and stat.get("num_executes"):
            cast_counts[str(stat["id"])] += mean(stat["num_executes"])
    return {
        "schema": "simc.pi.diagnostic.v1",
        "status": "diagnostic_only", "ranking_eligible": False,
        "scope": observation["scope"],
        "observed_damage": observed,
        "model_with_pi_damage": baseline_damage,
        "model_without_pi_damage": alternate_damage,
        "model_damage_difference": baseline_damage - alternate_damage,
        "baseline_relative_damage_error": (baseline_damage - observed) / observed if observed else None,
        "model_without_pi_dps": alternate_damage / duration,
        "observed_cast_counts": dict(casts),
        "model_cast_counts": dict(cast_counts),
        "blockers": [
            "Player decision policy has not been inferred and validated on held-out log events.",
            "Encounter reconstruction and gear/talent/patch agreement require verification.",
            "Policy uncertainty and no-PI control-pull validation are not yet implemented.",
        ],
    }


def run_pair(observation, profile, binary, out, iterations):
    if observation.get("schema") != "simc.pi.observation.v1":
        raise ValueError("Unsupported observation schema")
    if iterations < 2:
        raise ValueError("Use at least two iterations")
    if observation["player"].get("subType", "").lower() == "priest":
        raise ValueError("Priest counterfactuals require separate cast-cost modeling")
    profile, binary = Path(profile).resolve(strict=True), Path(binary).resolve(strict=True)
    if any(c in str(out.resolve()) for c in (",", "\n", "\r")):
        raise ValueError("Output path cannot contain commas or newlines")
    out.mkdir(parents=True, exist_ok=False)
    windows = "/".join(f'{w["start"]:.3f}:{w["duration"]:.3f}'
                       for w in observation["power_infusion_windows"])
    common = [str(binary), str(profile), "fixed_time=1", "vary_combat_length=0",
              f'max_time={observation["duration"]:.3f}', f"iterations={iterations}",
              "threads=1", "seed=739391", "external_buffs.power_infusion=",
              "external_buffs.power_infusion_windows=", "report_details=1", "statistics_level=3"]
    manifest = {"profile": str(profile), "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
                "binary": str(binary), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "iterations": iterations, "commands": {}, "ranking_eligible": False}
    write_json(out / "observation.json", observation)
    for label, disabled, schedule in (("with_pi", int(not windows), windows), ("without_pi", 1, "")):
        command = common + [f"external_buffs.disable_power_infusion={disabled}",
                            f"external_buffs.power_infusion_windows={schedule}",
                            f"json={out.resolve() / (label + '.json')},version=2.0.0"]
        manifest["commands"][label] = command
        write_json(out / "manifest.json", manifest)
        with (out / (label + ".log")).open("w") as log:
            result = subprocess.run(command, cwd=profile.parent, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            raise ValueError(f"{label} simulation failed; see {out / (label + '.log')}")
        report_player(read_json(out / (label + ".json")))
    result = diagnose(observation, read_json(out / "with_pi.json"), read_json(out / "without_pi.json"))
    write_json(out / "diagnostic.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("capture", help="Capture every event page for one fight; requires WCL_ACCESS_TOKEN")
    fetch.add_argument("report_code")
    fetch.add_argument("fight", type=int)
    fetch.add_argument("output", type=Path)
    fetch.add_argument("--credentials", type=Path,
                       help="Private JSON client_id/client_secret file; access token stays in memory")
    summary = commands.add_parser("summarize", help="Extract one player's observed PI windows, casts, and pet damage")
    summary.add_argument("capture", type=Path)
    summary.add_argument("player", type=int)
    summary.add_argument("output", type=Path)
    run = commands.add_parser("run", help="Run an explicit candidate profile with/without PI; diagnostic only")
    run.add_argument("observation", type=Path)
    run.add_argument("profile", type=Path, help="Explicit candidate player/encounter policy, not an inferred log replay")
    run.add_argument("--simc", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            if args.output.exists():
                raise ValueError("Capture output already exists")
            if args.credentials:
                token = client_access_token(args.credentials)
                request = lambda query, variables: graphql(query, variables, token)
                bundle = capture(args.report_code, args.fight, request)
            else:
                bundle = capture(args.report_code, args.fight)
            write_json(args.output, bundle)
        elif args.command == "summarize":
            if args.output.exists():
                raise ValueError("Observation output already exists")
            write_json(args.output, summarize(read_json(args.capture), args.player))
        else:
            result = run_pair(read_json(args.observation), args.profile, args.simc, args.output, args.iterations)
            print(json.dumps({"status": result["status"], "ranking_eligible": False,
                              "diagnostic": str(args.output / "diagnostic.json")}))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
