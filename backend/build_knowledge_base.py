"""
Pulls alarm/fault data from GitHub repos and builds a unified knowledge base JSON.
Run once to populate data/knowledge_base.json, or on a schedule to refresh.

Sources:
  - generacclean/generac-home-error-catalog  (self_test, configuration, device_alarm YAMLs)
  - neurio/pwrinverter  (DataStoreAlarms.xml + ExtendedDescriptions/*.md)
  - neurio/pwrbmu      (DataStoreAlarms.xml + ExtendedDescriptions/*.md)
  - neurio/reef        (headend-events.json — GMS/PLCHE/Manta alarms)
"""

import os, json, base64, re, subprocess, sys
from pathlib import Path

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OUT_PATH = Path(__file__).parent.parent / "data" / "knowledge_base.json"

# ── helpers ──────────────────────────────────────────────────────────────────

def gh_api(path: str) -> dict | list | None:
    env = {**os.environ, "GITHUB_TOKEN": GITHUB_TOKEN}
    result = subprocess.run(
        ["gh", "api", path],
        capture_output=True, text=True, env=env
    )
    if result.returncode != 0:
        print(f"  WARN: gh api {path} failed: {result.stderr[:100]}", file=sys.stderr)
        return None
    return json.loads(result.stdout)

def gh_file_content(repo: str, file_path: str) -> str | None:
    data = gh_api(f"repos/{repo}/contents/{file_path}")
    if not data or "content" not in data:
        return None
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

def gh_tree(repo: str) -> list[str]:
    data = gh_api(f"repos/{repo}/git/trees/HEAD?recursive=1")
    if not data:
        return []
    return [item["path"] for item in data.get("tree", []) if item["type"] == "blob"]


# ── parsers ──────────────────────────────────────────────────────────────────

def parse_yaml_entry(content: str, file_path: str) -> dict | None:
    """Parse a catalog YAML entry into a normalized dict (no external deps)."""
    # Simple line-by-line YAML parser sufficient for this flat structure
    entry = {"source_file": file_path, "id": "", "engineering": {}, "product": {}}
    section = None
    eng_ts, prod_ts, int_ts = [], [], []
    current_step_list = None
    buffer = []

    def flush_buffer(target: dict, key: str):
        if buffer:
            target[key] = " ".join(" ".join(buffer).split())
            buffer.clear()

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()

        if stripped.startswith("#"):
            continue

        # Section detection
        if line == "engineering:":
            section = "engineering"
            continue
        if line == "product:":
            section = "product"
            continue
        if stripped.startswith("troubleshooting:") and section == "engineering":
            current_step_list = eng_ts
            continue
        if stripped.startswith("internal_troubleshooting:") and section == "product":
            current_step_list = int_ts
            continue
        if stripped.startswith("troubleshooting:") and section == "product":
            current_step_list = prod_ts
            continue

        # Top-level id
        m = re.match(r'^id:\s*(.+)', line)
        if m:
            entry["id"] = m.group(1).strip()
            continue

        # Step items
        if current_step_list is not None:
            m = re.match(r'\s*-\s*step:\s*(.*)', line)
            if m:
                current_step_list.append(m.group(1).strip())
                continue
            # continuation of previous step
            if stripped and not re.match(r'\w+:', stripped):
                if current_step_list:
                    current_step_list[-1] += " " + stripped
                continue

        # Key: value pairs
        m = re.match(r'\s{2}(\w+):\s*(.*)', line)
        if m and section:
            key, val = m.group(1), m.group(2).strip()
            val = val.strip('"\'')
            current_step_list = None
            target = entry[section] if section in entry else {}
            if val in ("null", "~", ""):
                target[key] = None
            elif val in ("true", "True"):
                target[key] = True
            elif val in ("false", "False"):
                target[key] = False
            elif val.startswith(">"):
                buffer.clear()
                entry["_buffer_target"] = (section, key)
            else:
                target[key] = val
                entry[section] = target
            continue

        # Multi-line scalar continuation
        if "_buffer_target" in entry and stripped:
            buffer.append(stripped)
        elif "_buffer_target" in entry and not stripped:
            sec, key = entry.pop("_buffer_target")
            entry[sec][key] = " ".join(" ".join(buffer).split())
            buffer.clear()

    if "_buffer_target" in entry:
        sec, key = entry.pop("_buffer_target")
        entry[sec][key] = " ".join(" ".join(buffer).split())
        buffer.clear()

    if eng_ts:
        entry["engineering"]["troubleshooting"] = eng_ts
    if prod_ts:
        entry["product"]["troubleshooting"] = prod_ts
    if int_ts:
        entry["product"]["internal_troubleshooting"] = int_ts

    return entry if entry.get("id") or entry.get("engineering") else None


def parse_extended_md(content: str, alarm_name: str) -> dict:
    """Parse INV/BMU ExtendedDescriptions/*.md into a normalized dict."""
    sections = {"Hardware": "", "Trigger": "", "Thresholds": "", "Troubleshooting": []}
    current = None
    steps = []
    text_buf = []

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current == "Troubleshooting":
                sections["Troubleshooting"] = steps
            elif current and text_buf:
                sections[current] = " ".join(text_buf).strip()
            current = stripped[3:].strip()
            text_buf = []
            steps = []
        elif current == "Troubleshooting":
            m = re.match(r'\d+\.\s+(.*)', stripped)
            if m:
                steps.append(m.group(1))
            elif stripped and not stripped.startswith("#"):
                steps.append(stripped)
        elif current:
            if stripped:
                text_buf.append(stripped)

    if current == "Troubleshooting":
        sections["Troubleshooting"] = steps
    elif current and text_buf:
        sections[current] = " ".join(text_buf).strip()

    return {
        "id": f"device_alarm.fw.{alarm_name.lower()}",
        "alarm_name": alarm_name,
        "source": "firmware_extended_desc",
        "engineering": {
            "trigger": sections.get("Trigger", ""),
            "threshold": sections.get("Thresholds", ""),
            "hardware": sections.get("Hardware", ""),
            "troubleshooting": sections.get("Troubleshooting", []),
        }
    }


def parse_headend_events(content: str) -> list[dict]:
    """Parse reef headend-events.json into normalized alarm entries."""
    try:
        data = json.loads(content)
    except Exception:
        return []
    entries = []
    for ev in data.get("Events", []):
        alarm = ev.get("Alarm", {})
        cond = ev.get("Condition", {})
        entries.append({
            "id": f"device_alarm.gms.{alarm.get('Name', '').lower()}",
            "alarm_name": alarm.get("Name", ""),
            "device": "GMS/MANTA",
            "source": "headend_events",
            "engineering": {
                "description": alarm.get("Description", ""),
                "trigger": ev.get("Failure", ""),
                "alarm_type": alarm.get("Type", ""),
                "alarm_id": alarm.get("ID", ""),
                "condition_type": cond.get("Type", ""),
                "threshold": cond.get("Threshold"),
                "activation_time_s": cond.get("Activation_time_s"),
            }
        })
    return entries


# ── masterlist data (from Theresa's spreadsheet — pasted inline) ─────────────
# This will be supplemented/replaced by SharePoint/Power Automate later.
# Fields: device, alarm_name, friendly_name, alarm_code, description,
#         corrective_action, internal_notes, severity, alarm_type,
#         visible_support, visible_installer, visible_homeowner

MASTERLIST = [
    {"device":"INV","alarm_name":"ALARM_OVER_CURRENT_IAC1","friendly_name":"AC L1 Overcurrent Detected","alarm_code":"","description":"AC Current exceeded maximum allowable operating limit on L1.","corrective_action":"AC Line Overcurrent - Typically during transitioning events or surge events, common during backup mode. Clear alarm and check loads. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: AC Overcurrent (L1)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_OVER_CURRENT_IAC2","friendly_name":"AC L2 Overcurrent Detected","alarm_code":"","description":"AC Current exceeded maximum allowable operating limit on L2.","corrective_action":"AC Line Overcurrent - Typically during transitioning events or surge events, common during backup mode. Clear alarm and check loads. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: AC Overcurrent (L2)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_OVER_CURRENT_IACN","friendly_name":"AC Neutral Overcurrent Detected","alarm_code":"","description":"AC Current exceeded maximum allowable operating limit on Neutral.","corrective_action":"AC Line Overcurrent - Typically during transitioning events or surge events, common during backup mode. Clear alarm and check loads. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: AC Overcurrent (N)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_OVER_VOLTAGE_GRID_SIDE_VAC1","friendly_name":"L1 Grid Overvoltage Detected","alarm_code":"","description":"Grid Voltage exceeded maximum allowable operating limit on L1.","corrective_action":"Check grid voltage levels; Verify grid profile settings are correct for location. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Grid Overvoltage (L1)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_OVER_VOLTAGE_GRID_SIDE_VAC2","friendly_name":"L2 Grid Overvoltage Detected","alarm_code":"","description":"Grid Voltage exceeded maximum allowable operating limit on L2.","corrective_action":"Check grid voltage levels; Verify grid profile settings are correct for location. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Grid Overvoltage (L2)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_OVER_CURRENT_IREBP","friendly_name":"DC Wiring Overcurrent Detected (Positive)","alarm_code":"","description":"DC Current exceeded maximum allowable operating limit (Positive).","corrective_action":"Inspect connected loads, wiring, and connectors for overload or short-circuit conditions. Verify current draw is within specification and inspect for damaged components. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Wiring Overcurrent (Positive)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_OVER_CURRENT_IREBN","friendly_name":"DC Wiring Overcurrent Detected (Negative)","alarm_code":"","description":"DC Current exceeded maximum allowable operating limit (Negative).","corrective_action":"Inspect connected loads, wiring, and connectors for overload or short-circuit conditions. Verify current draw is within specification and inspect for damaged components. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Wiring Overcurrent (Negative)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_OVER_VOLTAGE_VBAT","friendly_name":"Battery Overvoltage Detected","alarm_code":"","description":"Battery Voltage exceeded maximum allowable operating limit.","corrective_action":"Verify input/source voltage, inspect power regulation circuitry, check for transient conditions, and restore voltage to specified operating range. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Battery Overvoltage","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_OVER_VOLTAGE_DCP","friendly_name":"DC Bus Overvoltage Detected (Positive)","alarm_code":"","description":"DC Voltage exceeded maximum allowable operating limit (Positive).","corrective_action":"Verify input/source voltage, inspect power regulation circuitry, check for transient conditions, and restore voltage to specified operating range. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Bus Overvoltage (Positive)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_OVER_VOLTAGE_DCN","friendly_name":"DC Bus Overvoltage Detected (Negative)","alarm_code":"","description":"DC Voltage exceeded maximum allowable operating limit (Negative).","corrective_action":"Verify input/source voltage, inspect power regulation circuitry, check for transient conditions, and restore voltage to specified operating range. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Bus Overvoltage (Negative)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_CPU1_HEARTBEAT","friendly_name":"CPU Communication Failure","alarm_code":"","description":"Communication Lost.","corrective_action":"CPU communication failure; Power cycle the inverter. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: CPU1 Heartbeat Failure","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":False,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_BSTOP","friendly_name":"Battery Stop Activated","alarm_code":"","description":"Battery protection system commanded stop to prevent damage.","corrective_action":"Battery voltage checks measure module voltages, check status lights. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Battery Stop. All purpose alarm that protects the batteries from any harm.","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_DCBUS_UNDER_VOLTAGE","friendly_name":"Undervoltage Detected","alarm_code":"","description":"DC Voltage dropped below minimum allowable operating limit.","corrective_action":"Check if BMU is enabled and providing power to DC Bus; Verify 400 VDC bus is operational. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Bus Undervoltage","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_GROUND_FAULT_REBUS","friendly_name":"DC Wiring Ground Fault","alarm_code":"","description":"Ground fault detected on DC wiring.","corrective_action":"Check for ground faults in wiring; Inspect insulation on DC Bus or battery connections; Verify proper grounding. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Wiring Ground Fault","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_UNINTENTIONAL_ISLANDING","friendly_name":"Unintentional Islanding Detected","alarm_code":"","description":"The system is receiving remote control messages while it is not in a mode that allows external control.","corrective_action":"Check grid connection status and grid relay. Check CAN termination and connections. Ensure that system is properly commissioned. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Unintentional Islanding Detected. Verify the unit is actually in AUTO mode (INV and BMU 1018 = 1)","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":False,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_TCI_PACKET_TIMEOUT","friendly_name":"Remote Communication Timeout","alarm_code":"","description":"Expected periodic updates from another system stopped arriving within the allowed time window.","corrective_action":"Check CAN termination and connections. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: TCI Packet Timeout. TCI packets come from Manta via CAN; Verify Target Control Mode from Manta; Check for CAN overload.","severity":"High (Severity 2)","alarm_type":"Fault","visible_support":True,"visible_installer":False,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_PFAIL","friendly_name":"Control Power Failure","alarm_code":"","description":"Internal control power supply is not operating correctly.","corrective_action":"Verify power source operation, inspect wiring and connectors, and restore stable input power. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Power Fail","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_REBUS_MISWIRED","friendly_name":"Wiring Configuration Error","alarm_code":"","description":"DC Wiring polarity or wiring detected as incorrect.","corrective_action":"Verify DC Wiring from INV to BMU is correct. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Wiring Miswired","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_REBUS_OVER_CURRENT","friendly_name":"Overcurrent Detected","alarm_code":"","description":"DC Current exceeded maximum allowable operating limit.","corrective_action":"Verify DC Wiring from INV to BMU is correct. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Wiring Overcurrent","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_SDS_ESTOP","friendly_name":"Emergency Stop Activated","alarm_code":"","description":"Smart Disconnect Switch commanded system stop.","corrective_action":"Check if eStop is activated or miswired. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: SDS Emergency Stop","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_REBUS_IMBALANCE","friendly_name":"DC Bus Imbalance","alarm_code":"","description":"DC Wiring Imbalance fault detected.","corrective_action":"Check DC Wiring. Ensure wiring is landed properly. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Wiring Imbalance","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_CAN_VERSION_MISMATCH","friendly_name":"Device Version Mismatch","alarm_code":"","description":"Communication failure detected due to incompatible device firmware or protocol versions.","corrective_action":"Verify all connected devices are using compatible firmware versions. Restart the system after updating devices if required. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: CAN Version Mismatch. Perform FW update on all devices.","severity":"High (Severity 2)","alarm_type":"Fault","visible_support":True,"visible_installer":False,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_MANTA_HEARTBEAT_TIMEOUT","friendly_name":"Communication Timeout","alarm_code":"","description":"Communication with the SDS was lost.","corrective_action":"Check if there are any offline devices (LEDs off, not communicating). Check communication lines. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: MANTA Heartbeat Timeout","severity":"High (Severity 2)","alarm_type":"Fault","visible_support":True,"visible_installer":False,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_AC1_RELAY_FAILED_OPEN","friendly_name":"L1 Relay Failed Open","alarm_code":"","description":"Relay did not open when commanded.","corrective_action":"Power down the system safely. Inspect relay wiring and terminal connections. Verify relay operation and contact movement. Check for welded, stuck, or damaged relay contacts. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: AC Relay 1 Failed Open. Possible causes: welded contacts, relay coil failure, driver circuit fault.","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_AC1_RELAY_FAILED_CLOSED","friendly_name":"L1 Relay Failed Closed","alarm_code":"","description":"Relay did not close when commanded.","corrective_action":"Power down the system safely. Inspect relay wiring and terminal connections. Verify relay operation and contact movement. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: AC Relay 1 Failed Closed","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_AC2_RELAY_FAILED_OPEN","friendly_name":"L2 Relay Failed Open","alarm_code":"","description":"Relay did not open when commanded.","corrective_action":"Power down the system safely. Inspect relay wiring and terminal connections. Verify relay operation and contact movement. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: AC Relay 2 Failed Open","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_AC2_RELAY_FAILED_CLOSED","friendly_name":"L2 Relay Failed Closed","alarm_code":"","description":"Relay did not close when commanded.","corrective_action":"Power down the system safely. Inspect relay wiring and terminal connections. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: AC Relay 2 Failed Closed","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_CHASSIS_OVER_VOLTAGE","friendly_name":"Chassis Voltage Too High","alarm_code":"","description":"Voltage between the system and chassis ground exceeded the allowed limit.","corrective_action":"Power down the system safely. Inspect DC wiring for damaged insulation or loose connections. Check for shorts between DC lines and chassis ground. Verify chassis grounding connections are secure. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Chassis Overvoltage. Possible insulation failure, leakage current, grounding issue, or short to chassis.","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":False,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_GROUND_FAULT_TEST_FAILURE","friendly_name":"Ground Fault Detected","alarm_code":"","description":"Ground Fault Detected.","corrective_action":"If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Ground Fault Test Failure","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_SEQUENCER_TIMEOUT","friendly_name":"Startup Sequencer Timeout Detected","alarm_code":"","description":"Startup or operating sequence did not complete within the expected time.","corrective_action":"Check CAN termination and connections. Power cycle the inverter. Ensure inverter is enabled. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Sequencer Timeout","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"INV","alarm_name":"ALARM_SEQUENCER_FAILURE","friendly_name":"Startup Sequencer Failure Detected","alarm_code":"","description":"System startup or control sequence failed to complete successfully.","corrective_action":"Check CAN termination and connections. Power cycle the inverter. Ensure inverter is enabled. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Sequencer Failure","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    # BMU alarms
    {"device":"BMU","alarm_name":"ALARM_OVER_CURRENT_IREBP","friendly_name":"Positive DC Wiring Over Current","alarm_code":"","description":"Positive DC wiring current exceeded the allowed limit.","corrective_action":"Power down the system safely. Inspect DC wiring and connected equipment for overloads or short circuits before restoring power. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Wiring Overcurrent (Neutral)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"BMU","alarm_name":"ALARM_OVER_CURRENT_IREBN","friendly_name":"Negative DC Wiring Over Current","alarm_code":"","description":"Negative DC wiring current exceeded the allowed limit.","corrective_action":"Power down the system safely. Inspect DC wiring and connected equipment for overloads or short circuits before restoring power. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Wiring Overcurrent (Negative)","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"BMU","alarm_name":"ALARM_OVER_CURRENT_IBAT","friendly_name":"Battery Overcurrent Detected","alarm_code":"","description":"Battery current exceeded the allowed charging or discharging limit.","corrective_action":"Reduce system load and verify battery connections are secure. Inspect battery wiring and connected equipment for overload conditions. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Battery Overcurrent","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"BMU","alarm_name":"ALARM_OVER_VOLTAGE_VBAT","friendly_name":"Battery Overvoltage Detected","alarm_code":"","description":"Battery voltage exceeded the allowed limit.","corrective_action":"Reduce system load and verify battery connections are secure. Inspect battery wiring and connected equipment for overload conditions. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Battery Overvoltage","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":False,"visible_homeowner":False},
    {"device":"BMU","alarm_name":"ALARM_BSTOP","friendly_name":"Battery Stop Activated","alarm_code":"","description":"Battery protection system commanded stop to prevent damage.","corrective_action":"Battery voltage checks, measure module voltages, check status lights. Verify the battery system is operating normally and inspect for active battery faults or protection events. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Battery Stop","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"BMU","alarm_name":"ALARM_DCBUS_UNDER_VOLTAGE","friendly_name":"Undervoltage Detected","alarm_code":"","description":"DC Bus voltage is below the allowed operating limit.","corrective_action":"Verify the input power source and DC bus voltage are within specification. Inspect wiring and power connections for loose or damaged connections. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Bus Undervoltage","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"BMU","alarm_name":"ALARM_GROUND_FAULT_BATTERY","friendly_name":"Battery Ground Fault","alarm_code":"","description":"Ground Fault Detected.","corrective_action":"Power down the system safely. Inspect battery wiring and connectors for damaged insulation, moisture, or shorts to chassis ground before restoring power. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Battery Ground Fault","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"BMU","alarm_name":"ALARM_GROUND_FAULT_REBUS","friendly_name":"DC Wiring Ground Fault","alarm_code":"","description":"Ground Fault Detected.","corrective_action":"Power down the system safely. Inspect DC wiring and connectors for damaged insulation, moisture, or shorts to chassis ground before restoring power. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Wiring Ground Fault","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"BMU","alarm_name":"ALARM_BATTERY_COMMS","friendly_name":"Battery Communication Error","alarm_code":"","description":"Communication Lost.","corrective_action":"Inspect the module communication port at the battery management unit and confirm that all CAT5 connectors are intact and undamaged. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Battery Communication Fault","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"BMU","alarm_name":"ALARM_PFAIL","friendly_name":"Power Failure Detected","alarm_code":"","description":"Input supply dropped below operational threshold or was lost.","corrective_action":"Verify input power is present and stable. Inspect input wiring, breakers, and power connections before restarting the system. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: Power Fail","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"BMU","alarm_name":"ALARM_REBUS_MISWIRED","friendly_name":"Wiring Configuration Error","alarm_code":"","description":"DC Wiring polarity or wiring detected as incorrect.","corrective_action":"Power down the system safely. Verify DC wiring polarity and confirm all wiring connections match the installation requirements before restoring power. If behavior persists, contact Technical Support.","internal_notes":"Technical Name: DC Wiring Miswired","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    # GMS alarms (from spreadsheet)
    {"device":"GMS","alarm_name":"FAULT_24V_OVERVOLTAGE","friendly_name":"24V Supply Overvoltage Detected","alarm_code":"0xb024","description":"The GMS has detected overvoltage on the 24V supply.","corrective_action":"Verify input/source voltage.","internal_notes":"","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"FAULT_24V_UNDERVOLTAGE","friendly_name":"24V Supply Undervoltage Detected","alarm_code":"0xb025","description":"The GMS has detected undervoltage on the 24V supply.","corrective_action":"Verify input/source voltage.","internal_notes":"","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"FAULT_PWRTALK_OVERVOLTAGE","friendly_name":"Overvoltage Detected","alarm_code":"0xb026","description":"The GMS has detected overvoltage on the 24V PWRTalk.","corrective_action":"Verify input/source voltage.","internal_notes":"","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"WARN_GRID_CT_L1_BAD","friendly_name":"Grid CT L1 Error","alarm_code":"0xa0a2","description":"The GMS has detected an error with the L1 grid CT.","corrective_action":"If warning persists, contact a dealer.","internal_notes":"","severity":"Medium (Severity 3)","alarm_type":"Warn","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"WARN_GRID_CT_L2_BAD","friendly_name":"Grid CT L2 Error","alarm_code":"0xa0a3","description":"The GMS has detected an error with the L2 grid CT.","corrective_action":"If warning persists, contact a dealer.","internal_notes":"","severity":"Medium (Severity 3)","alarm_type":"Warn","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"FAULT_GRID_RELAY_CLOSE_FAILURE","friendly_name":"Grid Relay Stuck Closed","alarm_code":"0xB04F","description":"The GMS grid relay is out of sequence and is stuck closed.","corrective_action":"If behavior persists, contact Technical Support.","internal_notes":"","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"FAULT_GRID_RELAY_OPEN_FAILURE","friendly_name":"Grid Relay Stuck Open","alarm_code":"0xB050","description":"The GMS grid relay is out of sequence and is stuck open.","corrective_action":"If behavior persists, contact Technical Support.","internal_notes":"","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"FAULT_GEN_RELAY_CLOSE_FAILURE","friendly_name":"Gen Relay Stuck Closed","alarm_code":"0xB051","description":"The GMS generator relay is out of sequence and is stuck closed.","corrective_action":"If behavior persists, contact Technical Support.","internal_notes":"","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"FAULT_GEN_RELAY_OPEN_FAILURE","friendly_name":"Gen Relay Stuck Open","alarm_code":"0xB052","description":"The GMS generator relay is out of sequence and is stuck open.","corrective_action":"If behavior persists, contact Technical Support.","internal_notes":"","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"FAULT_CPU_LOAD_FAILURE","friendly_name":"CPU Load Exceeds Threshold","alarm_code":"0xB055","description":"The GMS encountered an error. Device will reboot.","corrective_action":"If behavior persists, contact Technical Support.","internal_notes":"","severity":"Critical (Severity 1)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"WARN_TEMPERATURE_A_MEAS_OO_BOUNDS","friendly_name":"Internal Temperature A Out of Bounds","alarm_code":"0xa09e","description":"The GMS has detected the internal temperature A measurements are out of expected range.","corrective_action":"Inspect cooling systems.","internal_notes":"","severity":"Medium (Severity 3)","alarm_type":"Warn","visible_support":True,"visible_installer":True,"visible_homeowner":False},
    {"device":"GMS","alarm_name":"FAULT_STARTUP_DELAY","friendly_name":"Startup Delay","alarm_code":"0xb035","description":"The GMS is in startup delay.","corrective_action":"If behavior persists, contact Technical Support.","internal_notes":"","severity":"Medium (Severity 3)","alarm_type":"Fault","visible_support":True,"visible_installer":True,"visible_homeowner":False},
]


# ── main build ────────────────────────────────────────────────────────────────

def build():
    kb = {"version": "1.0", "sources": [], "entries": []}
    entry_map: dict[str, dict] = {}  # alarm_name → entry

    # 1. Seed from masterlist
    print("Loading masterlist...")
    for row in MASTERLIST:
        key = f"{row['device']}:{row['alarm_name']}"
        entry_map[key] = {
            "id": f"device_alarm.{row['device'].lower()}.{row['alarm_name'].lower()}",
            "alarm_name": row["alarm_name"],
            "device": row["device"],
            "friendly_name": row["friendly_name"],
            "alarm_code": row["alarm_code"],
            "severity": row["severity"],
            "alarm_type": row["alarm_type"],
            "visibility": {
                "support": row["visible_support"],
                "installer": row["visible_installer"],
                "homeowner": row["visible_homeowner"],
            },
            "engineering": {
                "description": row["description"],
                "internal_notes": row["internal_notes"],
            },
            "product": {
                "corrective_action": row["corrective_action"],
            },
            "sources": ["masterlist"],
        }
    print(f"  {len(entry_map)} masterlist entries loaded")
    kb["sources"].append("masterlist")

    # 2. Pull catalog YAMLs (self_test + configuration)
    print("Pulling error catalog YAMLs...")
    all_paths = gh_tree("generacclean/generac-home-error-catalog")
    catalog_paths = [p for p in all_paths if p.startswith("catalog/") and p.endswith(".yaml")]
    print(f"  Found {len(catalog_paths)} catalog YAML files")
    for path in catalog_paths:
        content = gh_file_content("generacclean/generac-home-error-catalog", path)
        if not content:
            continue
        parsed = parse_yaml_entry(content, path)
        if parsed:
            cid = parsed.get("id", path)
            parsed["sources"] = ["error_catalog"]
            entry_map[cid] = parsed
    kb["sources"].append("error_catalog")

    # 3. Pull INV extended descriptions
    print("Pulling INV extended descriptions...")
    inv_paths = [p for p in gh_tree("neurio/pwrinverter")
                 if "ExtendedDescriptions" in p and p.endswith(".md")]
    print(f"  Found {len(inv_paths)} INV extended description files")
    for path in inv_paths:
        alarm_name = Path(path).stem
        content = gh_file_content("neurio/pwrinverter", path)
        if not content:
            continue
        parsed = parse_extended_md(content, alarm_name)
        key = f"INV:{alarm_name}"
        if key in entry_map:
            # Merge engineering details into existing masterlist entry
            entry_map[key]["engineering"].update({
                k: v for k, v in parsed["engineering"].items() if v
            })
            entry_map[key]["sources"].append("inv_extended_desc")
        else:
            parsed["device"] = "INV"
            parsed["sources"] = ["inv_extended_desc"]
            entry_map[key] = parsed
    kb["sources"].append("inv_extended_desc")

    # 4. Pull BMU extended descriptions
    print("Pulling BMU extended descriptions...")
    bmu_paths = [p for p in gh_tree("neurio/pwrbmu")
                 if "ExtendedDescriptions" in p and p.endswith(".md")]
    print(f"  Found {len(bmu_paths)} BMU extended description files")
    for path in bmu_paths:
        alarm_name = Path(path).stem
        content = gh_file_content("neurio/pwrbmu", path)
        if not content:
            continue
        parsed = parse_extended_md(content, alarm_name)
        key = f"BMU:{alarm_name}"
        if key in entry_map:
            entry_map[key]["engineering"].update({
                k: v for k, v in parsed["engineering"].items() if v
            })
            entry_map[key]["sources"].append("bmu_extended_desc")
        else:
            parsed["device"] = "BMU"
            parsed["sources"] = ["bmu_extended_desc"]
            entry_map[key] = parsed
    kb["sources"].append("bmu_extended_desc")

    # 5. Pull GMS/Manta headend events from reef
    print("Pulling GMS/Manta headend events...")
    content = gh_file_content("neurio/reef", "source/application/headend/event-manager/headend-events.json")
    if content:
        events = parse_headend_events(content)
        print(f"  Found {len(events)} GMS/Manta alarm events")
        for ev in events:
            key = f"GMS:{ev['alarm_name']}"
            if key in entry_map:
                entry_map[key]["engineering"].update(ev.get("engineering", {}))
                entry_map[key]["sources"].append("reef_headend")
            else:
                ev["sources"] = ["reef_headend"]
                entry_map[key] = ev
    kb["sources"].append("reef_headend")

    kb["entries"] = list(entry_map.values())
    print(f"\nTotal entries: {len(kb['entries'])}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(kb, f, indent=2)
    print(f"Written to {OUT_PATH}")


if __name__ == "__main__":
    build()
