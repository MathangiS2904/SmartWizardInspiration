#!/usr/bin/env python3
"""
test_inspiration_api.py

Automates testing of room-level inspiration payloads against the backend endpoint:
http://202.66.172.134:4463/inspiration/engine/generate-inspiration-zealty

Features:
- Validates HTTP response status codes:
  * 200 OK: Saves 3D layout result to output directory (inspiration_results/) as usual.
  * 400 or 404: Extracts error message, logs to SQLite DB, automatically selects another
    valid style for the same room (matching room_type & area range without repeating tried styles),
    updates style_id, and retries up to --max-retries.
  * 500 Server Error: Does NOT retry with another style. Displays exact error in terminal,
    saves error details to SQLite DB, and stops processing that room.
- SQLite database error tracking (stores room, style ID, status code, error message, retry attempt, timestamp).
- Terminal summary table and persistent execution log file.
"""

import os
import sys
import json
import time
import copy
import argparse
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Union
import requests

from inspiration_db import (
    init_db,
    get_db,
    log_api_error,
    log_api_result,
    print_db_errors,
    print_db_results,
    DEFAULT_DB_FILE
)

DEFAULT_ENDPOINT = "http://202.66.172.134:4463/inspiration/engine/generate-inspiration-zealty"
DEFAULT_API_KEY = "bac39574f96bd5fdd5db314a67237dcad064fea9ec8c0ff0587dd753b77201a0"
DEFAULT_PAYLOADS_DIR = "room_level_payloads"
DEFAULT_RESULTS_DIR = "inspiration_results"
DEFAULT_LOG_FILE = "inspiration_api_test.log"
DEFAULT_RULES_FILE = "response_1789025921629.json"
DEFAULT_MAX_RETRIES = 3


def setup_logger(log_file: str) -> logging.Logger:
    """Sets up a logger that outputs to both console and a log file."""
    logger = logging.getLogger("InspirationAPI")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    # Ensure console supports utf-8 safely on Windows
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Console Handler (clean format)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    c_fmt = logging.Formatter("%(message)s")
    ch.setFormatter(c_fmt)
    logger.addHandler(ch)

    # File Handler (timestamped detailed format)
    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fh.setLevel(logging.INFO)
    f_fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(f_fmt)
    logger.addHandler(fh)

    return logger


def get_room_area_m2(payload: Dict[str, Any]) -> float:
    """Extracts or calculates the room area in m2 from payload."""
    room_json = payload.get("room_json", {})

    # 1. From meta or export_info
    area = room_json.get("meta", {}).get("area_m2") or room_json.get("export_info", {}).get("area_m2")
    if area and float(area) > 0:
        return float(area)

    # 2. From layer vertices using shoelace formula
    try:
        from create_room_inspiration_payload import calculate_room_area_m2
        layer = room_json.get("layers", {}).get("layer-1", {})
        areas = layer.get("areas", {})
        vertices = layer.get("vertices", {})
        for _, a_data in areas.items():
            v_ids = a_data.get("vertices", [])
            if v_ids:
                return calculate_room_area_m2(vertices, v_ids)
    except Exception:
        pass

    return 15.0  # Reasonable default fallback if geometry is not resolvable


def get_valid_styles_for_room(
    room_type: str,
    area_m2: float,
    rules_file: str = DEFAULT_RULES_FILE,
    allow_cross_room: bool = True
) -> List[Tuple[str, int, str]]:
    """
    Finds valid public candidate styles for the given area_m2.
    Prioritizes styles matching the nominal room_type first.
    If allow_cross_room is True, also appends styles from other compatible room types
    (e.g., Bathroom, Bedroom, Living Room, Kitchen) whose area rules permit area_m2.
    Returns: List of tuples (room_type, user_style_id, style_name) ordered by preference.
    """
    try:
        from create_room_inspiration_payload import (
            load_style_rules,
            ALLOWED_USER_STYLE_IDS,
            ROOM_TYPE_ALIASES
        )
    except ImportError:
        return []

    styles = load_style_rules(rules_file)

    all_possible_rooms = ["Bathroom", "Bedroom", "Living Room", "Kitchen"]
    ordered_rooms = [room_type]
    if allow_cross_room:
        for r_name in all_possible_rooms:
            if r_name.lower() != room_type.lower() and r_name not in ordered_rooms:
                ordered_rooms.append(r_name)

    candidates: List[Tuple[str, int, str]] = []
    seen = set()

    for rt in ordered_rooms:
        target_norm = ROOM_TYPE_ALIASES.get(rt.lower(), rt.lower())
        for s in styles:
            uid = s.get("user_style_id") or s.get("id")
            if uid not in ALLOWED_USER_STYLE_IDS or not s.get("is_public"):
                continue
            combo = (rt, uid)
            if combo in seen:
                continue

            for r in s.get("rules", []):
                rule_rt = (r.get("room_type") or "").strip()
                rule_norm = ROOM_TYPE_ALIASES.get(rule_rt.lower(), rule_rt.lower())
                if rule_norm == target_norm or rule_rt.lower() == rt.lower():
                    min_a = r.get("min_area_m2", 0)
                    max_a = r.get("max_area_m2", 9999)
                    if min_a <= area_m2 <= max_a:
                        candidates.append((rt, uid, s.get("name", f"Style {uid}")))
                        seen.add(combo)
                        break

    return candidates


def resolve_mirrored_template_dir(
    blob_or_path: str,
    base_output_dir: str = DEFAULT_RESULTS_DIR
) -> Tuple[str, str, str]:
    """
    Resolves the mirrored Azure folder hierarchy for a floorplan.
    Returns: (full_output_dir, category, template_filename)

    Examples:
    - '1BHK/Balanced_Single_Living/1bhk_Southface_422.3Sqft.json'
      -> ('inspiration_results/1BHK/Balanced_Single_Living/1bhk_Southface_422.3Sqft', '1BHK', '1bhk_Southface_422.3Sqft.json')
    - '1bhk_south_square_853.2sqft.json'
      -> ('inspiration_results/1BHK/1bhk_south_square_853.2sqft', '1BHK', '1bhk_south_square_853.2sqft.json')
    - '2BHK/2-bhk/2BHK_SouthFace_825.8sqft.json'
      -> ('inspiration_results/2BHK/2-bhk/2BHK_SouthFace_825.8sqft', '2BHK', '2BHK_SouthFace_825.8sqft.json')
    """
    clean_p = blob_or_path.replace("\\", "/").strip("/")
    if clean_p.startswith("cloud_templates/"):
        clean_p = clean_p[len("cloud_templates/"):]
    elif clean_p.startswith("./"):
        clean_p = clean_p[2:]

    parts = [pt for pt in clean_p.split("/") if pt]
    filename = parts[-1] if parts else "floorplan.json"
    stem = os.path.splitext(filename)[0]
    # If stem ends with .json (e.g. .json.json in some azure blobs)
    if stem.lower().endswith(".json"):
        stem = os.path.splitext(stem)[0]

    category = "Custom"
    for pt in parts:
        pt_upper = pt.upper()
        for k in ("1BHK", "2BHK", "3BHK", "4BHK", "5BHK"):
            if k in pt_upper:
                category = k
                break
        if category != "Custom":
            break

    if category == "Custom":
        fn_upper = filename.upper()
        for k in ("1BHK", "2BHK", "3BHK", "4BHK", "5BHK"):
            if k in fn_upper:
                category = k
                break

    if len(parts) > 1:
        # Preserves category/subfolder/stem
        rel_subdirs = parts[:-1] + [stem]
    else:
        if category != "Custom":
            rel_subdirs = [category, stem]
        else:
            rel_subdirs = [stem]

    full_output_dir = os.path.join(base_output_dir, *rel_subdirs)
    return full_output_dir, category, filename


def test_room_payload(
    payload_path: str,
    endpoint_url: str,
    api_key: str,
    output_dir: str,
    logger: logging.Logger,
    max_retries: int = DEFAULT_MAX_RETRIES,
    db_path: str = DEFAULT_DB_FILE,
    rules_file: str = DEFAULT_RULES_FILE,
    allow_cross_room: bool = True,
    timeout: int = 60,
    input_url: Optional[str] = None,
    input_path: Optional[str] = None,
    template_name: Optional[str] = None,
    payload_data: Optional[Dict[str, Any]] = None,
    save_to_disk: bool = False
) -> Dict[str, Any]:
    """
    Sends a room payload to the inspiration endpoint.
    - Supports in-memory testing without local disk file creation when save_to_disk=False.
    """
    file_name = os.path.basename(payload_path) if isinstance(payload_path, str) else "room_payload.json"
    if not input_path:
        input_path = payload_path
    if not input_url:
        input_url = endpoint_url

    logger.info(f"\n" + "=" * 70)
    logger.info(f"[*] Testing Room Payload: {file_name}")
    logger.info(f"    - Input Path: {input_path}")
    logger.info(f"    - Input URL : {input_url}")

    # 1. Load payload (from memory or file)
    if payload_data is not None:
        payload = copy.deepcopy(payload_data)
    else:
        with open(payload_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

    meta = payload.get("room_json", {}).get("meta", {})
    initial_room_type = payload.get("room_type", "Unknown")
    current_room_type = initial_room_type
    initial_style_id = payload.get("style_id", "Unknown")
    current_style_id = initial_style_id
    area_m2 = get_room_area_m2(payload)
    apply_flags = {k: v for k, v in payload.items() if k.startswith("apply_")}

    smart_wizard_room_name = (
        meta.get("smart_wizard_room_name") or
        payload.get("smart_wizard_room_name") or
        initial_room_type
    )
    smart_wizard_nominal_type = (
        meta.get("smart_wizard_nominal_type") or
        payload.get("smart_wizard_nominal_type") or
        initial_room_type
    )
    smart_wizard_area_id = (
        meta.get("smart_wizard_area_id") or
        meta.get("area_id") or
        payload.get("area_id") or
        ""
    )

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json"
    }

    # Find candidate styles (nominal first, then other compatible room types)
    candidate_styles = get_valid_styles_for_room(
        room_type=initial_room_type,
        area_m2=area_m2,
        rules_file=rules_file,
        allow_cross_room=allow_cross_room
    )

    def lookup_style_name(style_id: Any, rt: str) -> str:
        for cand_rt, cand_uid, cand_name in candidate_styles:
            if cand_uid == style_id and cand_rt.lower() == rt.lower():
                return cand_name
        for cand_rt, cand_uid, cand_name in candidate_styles:
            if cand_uid == style_id:
                return cand_name
        return f"Style {style_id}"

    logger.info(f"    - Smart Wizard Room   : '{smart_wizard_room_name}' (Nominal: '{smart_wizard_nominal_type}', Area ID: {smart_wizard_area_id})")
    logger.info(f"    - Initial Style ID    : {initial_style_id} ('{lookup_style_name(initial_style_id, initial_room_type)}')")
    logger.info(f"    - Calculated Area     : {area_m2:.2f} m2")
    logger.info(f"    - Max Retries         : {max_retries}")
    logger.info(f"    - Cross-Room Fallback : {'Enabled' if allow_cross_room else 'Disabled'}")
    logger.info(f"    - Flags               : {apply_flags}")

    tried_combos = set()
    failed_room_types = set()
    if isinstance(initial_style_id, int):
        tried_combos.add((current_room_type, initial_style_id))

    retry_attempt = 0

    while True:
        payload["room_type"] = current_room_type
        payload["style_id"] = current_style_id

        if retry_attempt > 0:
            logger.info(f"[*] Sending POST request (Retry #{retry_attempt}) [Room Type: '{current_room_type}', Style ID: {current_style_id}] to: {endpoint_url} ...")
        else:
            logger.info(f"[*] Sending POST request [Room Type: '{current_room_type}', Style ID: {current_style_id}] to: {endpoint_url} ...")

        start_time = time.time()
        try:
            response = requests.post(endpoint_url, headers=headers, json=payload, timeout=timeout)
            latency = time.time() - start_time
            status_code = response.status_code
        except requests.exceptions.RequestException as e:
            latency = time.time() - start_time
            err_msg = str(e)
            logger.error(f"[!] [NETWORK ERROR] Connection error after {latency:.2f}s: {err_msg}")
            log_api_error(
                room=f"{file_name} [{smart_wizard_room_name} -> {current_room_type}]",
                style_id=current_style_id if isinstance(current_style_id, int) else None,
                status_code=0,
                error_message=err_msg,
                retry_attempt=retry_attempt,
                db_path=db_path,
                input_url=input_url,
                input_path=input_path,
                template_name=template_name
            )
            return {
                "file": file_name,
                "payload_path": payload_path,
                "smart_wizard_room_name": smart_wizard_room_name,
                "smart_wizard_nominal_type": smart_wizard_nominal_type,
                "smart_wizard_area_id": smart_wizard_area_id,
                "applied_room_type": current_room_type,
                "applied_style_id": current_style_id,
                "applied_style_name": lookup_style_name(current_style_id, current_room_type),
                "room_type_switched": (current_room_type.lower() != smart_wizard_nominal_type.lower()),
                "status": "ERROR",
                "status_code": 0,
                "latency": latency,
                "items_count": 0,
                "retries_used": retry_attempt,
                "input_url": input_url,
                "input_path": input_path,
                "error": err_msg
            }

        # =================================================================
        # CASE 1: 200 OK -> Save result as usual
        # =================================================================
        if status_code == 200:
            applied_style_name = lookup_style_name(current_style_id, current_room_type)
            mapping_text = f"Smart Wizard '{smart_wizard_room_name}' -> Inspiration '{current_room_type}' (Style #{current_style_id}: '{applied_style_name}')"

            if current_room_type != smart_wizard_nominal_type:
                status_desc = f"200 OK (Validated via Cross-Room Type '{current_room_type}', Retry #{retry_attempt})"
                logger.info(f"[+] [SUCCESS] Status {status_desc} received in {latency:.2f}s (Style ID: {current_style_id})")
                logger.info(f"    ℹ Floorplan room geometry verified successfully using fallback Room Type: '{current_room_type}' (Nominal: '{smart_wizard_nominal_type}')")
            else:
                status_desc = f"200 OK (Retry #{retry_attempt})" if retry_attempt > 0 else "200 OK"
                logger.info(f"[+] [SUCCESS] Status {status_desc} received in {latency:.2f}s (Style ID: {current_style_id})")

            logger.info(f"[+] [ROOM MAPPING] {mapping_text}")

            res_data = response.json()

            # Extract placed items and metrics
            layer = res_data.get("layers", {}).get("layer-1", {})
            placed_items = layer.get("items", {})
            items_count = len(placed_items)
            walls_count = len(layer.get("lines", {}))
            holes_count = len(layer.get("holes", {}))

            logger.info(f"    - Total Placed 3D Furniture Items: {items_count}")
            logger.info(f"    - Enclosing Walls Count          : {walls_count}")
            logger.info(f"    - Window / Door Openings Count   : {holes_count}")

            if items_count > 0:
                logger.info("    - Placed Items Breakdown:")
                for idx, (item_id, item) in enumerate(placed_items.items(), 1):
                    item_name = item.get("name", "Unnamed")
                    x = round(item.get("x", 0), 2)
                    y = round(item.get("y", 0), 2)
                    rot = round(item.get("rotation", 0), 2)
                    logger.info(f"      {idx:2d}. {item_name:28} at (x: {x:8.2f}, y: {y:8.2f}, rot: {rot} rad)")

            out_path = None
            if save_to_disk:
                os.makedirs(output_dir, exist_ok=True)
                base_name = os.path.splitext(file_name)[0]
                clean_base = base_name[:-8] if base_name.endswith("_payload") else base_name
                out_path = os.path.join(output_dir, f"{clean_base}_inspiration.json")
                with open(out_path, "w", encoding="utf-8") as out_f:
                    json.dump(res_data, out_f, indent=2)
                logger.info(f"[+] Inspiration result saved to: {out_path}")

                try:
                    with open(payload_path, "w", encoding="utf-8") as pf:
                        json.dump(payload, pf, indent=2)
                except Exception as pe:
                    logger.warning(f"[!] Could not update successful payload {payload_path}: {pe}")

            # Record successful run to database
            succ_reason = f"{mapping_text}. Placed {items_count} 3D items."
            if retry_attempt > 0:
                succ_reason += f" (after {retry_attempt} retries)"

            log_api_result(
                room=f"{file_name} [{smart_wizard_room_name} -> {current_room_type}]",
                room_type=current_room_type,
                style_id=current_style_id if isinstance(current_style_id, int) else None,
                status="SUCCESS",
                status_code=200,
                latency=latency,
                items_count=items_count,
                retries_used=retry_attempt,
                saved_to=out_path,
                db_path=db_path,
                reason=succ_reason,
                input_url=input_url,
                input_path=input_path,
                template_name=template_name
            )

            return {
                "file": file_name,
                "payload_path": payload_path,
                "smart_wizard_room_name": smart_wizard_room_name,
                "smart_wizard_nominal_type": smart_wizard_nominal_type,
                "smart_wizard_area_id": smart_wizard_area_id,
                "applied_room_type": current_room_type,
                "applied_style_id": current_style_id,
                "applied_style_name": applied_style_name,
                "room_type_switched": (current_room_type.lower() != smart_wizard_nominal_type.lower()),
                "status": "SUCCESS",
                "status_code": 200,
                "latency": latency,
                "items_count": items_count,
                "retries_used": retry_attempt,
                "saved_to": out_path,
                "input_url": input_url,
                "input_path": input_path,
                "placed_items": placed_items
            }

        # =================================================================
        # CASE 2: 500 SERVER ERROR -> Do NOT retry, display exact error,
        # save to database, stop processing that room
        # =================================================================
        elif status_code >= 500:
            exact_error = response.text
            applied_style_name = lookup_style_name(current_style_id, current_room_type)
            mapping_text = f"Smart Wizard '{smart_wizard_room_name}' -> Inspiration '{current_room_type}' (Style #{current_style_id}: '{applied_style_name}')"

            logger.error(f"[!] [500 SERVER ERROR] HTTP {status_code} received in {latency:.2f}s for '{file_name}' with Style ID {current_style_id}")
            logger.error(f"    Exact Error Details: {exact_error}")
            logger.info(f"[*] [ROOM MAPPING] {mapping_text}")

            # Save to database
            db_row = log_api_error(
                room=f"{file_name} [{smart_wizard_room_name} -> {current_room_type}]",
                style_id=current_style_id if isinstance(current_style_id, int) else None,
                status_code=status_code,
                error_message=exact_error,
                retry_attempt=retry_attempt,
                db_path=db_path,
                input_url=input_url,
                input_path=input_path,
                template_name=template_name
            )
            logger.error(f"[+] [DATABASE] Stored 500 server error into database (Record #{db_row})")
            logger.warning(f"[*] Stopping processing for room '{file_name}' on 500 error (will NOT retry with another style).")

            log_api_result(
                room=f"{file_name} [{smart_wizard_room_name} -> {current_room_type}]",
                room_type=current_room_type,
                style_id=current_style_id if isinstance(current_style_id, int) else None,
                status="SERVER_ERROR",
                status_code=status_code,
                latency=latency,
                items_count=0,
                retries_used=retry_attempt,
                saved_to=None,
                db_path=db_path,
                reason=f"{mapping_text}. 500 Server Error: {exact_error[:180]}",
                input_url=input_url,
                input_path=input_path,
                template_name=template_name
            )

            return {
                "file": file_name,
                "payload_path": payload_path,
                "smart_wizard_room_name": smart_wizard_room_name,
                "smart_wizard_nominal_type": smart_wizard_nominal_type,
                "smart_wizard_area_id": smart_wizard_area_id,
                "applied_room_type": current_room_type,
                "applied_style_id": current_style_id,
                "applied_style_name": applied_style_name,
                "room_type_switched": (current_room_type.lower() != smart_wizard_nominal_type.lower()),
                "status": "SERVER_ERROR",
                "status_code": status_code,
                "latency": latency,
                "items_count": 0,
                "retries_used": retry_attempt,
                "input_url": input_url,
                "input_path": input_path,
                "error": exact_error[:300]
            }

        # =================================================================
        # CASE 3: 400 BAD REQUEST or 404 NOT FOUND ->
        # Get error message, select another valid style for same room,
        # or switch to another valid room type (e.g. Bathroom, Bedroom),
        # update room_type and style_id, retry API (do not use same combo again)
        # =================================================================
        elif status_code in (400, 404):
            err_msg = response.text
            try:
                err_json = response.json()
                if "detail" in err_json:
                    err_msg = err_json["detail"]
            except Exception:
                pass

            applied_style_name = lookup_style_name(current_style_id, current_room_type)
            mapping_text = f"Smart Wizard '{smart_wizard_room_name}' -> Inspiration '{current_room_type}' (Style #{current_style_id}: '{applied_style_name}')"

            logger.warning(f"[!] [FAILED {status_code}] Style ID {current_style_id} for '{current_room_type}' failed in {latency:.2f}s")
            logger.warning(f"    Error Message: {err_msg}")
            logger.info(f"[*] [ROOM MAPPING] {mapping_text}")

            # Save error to database
            db_row = log_api_error(
                room=f"{file_name} [{smart_wizard_room_name} -> {current_room_type}]",
                style_id=current_style_id if isinstance(current_style_id, int) else None,
                status_code=status_code,
                error_message=err_msg,
                retry_attempt=retry_attempt,
                db_path=db_path,
                input_url=input_url,
                input_path=input_path,
                template_name=template_name
            )
            logger.info(f"[+] [DATABASE] Stored error into database (Record #{db_row})")

            # Check if mandatory asset placement failed or room type is incompatible with this geometry
            must_switch_room_type = (
                "Could not place primary mandatory asset" in err_msg or
                "may be too small or invalid" in err_msg or
                "not compatible with this style's requirements" in err_msg
            )
            if must_switch_room_type and allow_cross_room:
                failed_room_types.add(current_room_type.lower())
                logger.info(f"[*] Room type '{current_room_type}' cannot fit mandatory assets for this geometry. Switching to an alternate room type...")

            # Check if retry limit reached
            if retry_attempt >= max_retries:
                logger.error(f"[!] Reached maximum retry limit ({max_retries}) for '{file_name}'. Stopping.")
                log_api_result(
                    room=f"{file_name} [{smart_wizard_room_name} -> {current_room_type}]",
                    room_type=current_room_type,
                    style_id=current_style_id if isinstance(current_style_id, int) else None,
                    status="FAILED",
                    status_code=status_code,
                    latency=latency,
                    items_count=0,
                    retries_used=retry_attempt,
                    saved_to=None,
                    db_path=db_path,
                    reason=f"{mapping_text}. Max retries ({max_retries}) reached: {err_msg[:180]}",
                    input_url=input_url,
                    input_path=input_path,
                    template_name=template_name
                )
                return {
                    "file": file_name,
                    "payload_path": payload_path,
                    "smart_wizard_room_name": smart_wizard_room_name,
                    "smart_wizard_nominal_type": smart_wizard_nominal_type,
                    "smart_wizard_area_id": smart_wizard_area_id,
                    "applied_room_type": current_room_type,
                    "applied_style_id": current_style_id,
                    "applied_style_name": applied_style_name,
                    "room_type_switched": (current_room_type.lower() != smart_wizard_nominal_type.lower()),
                    "status": "FAILED",
                    "status_code": status_code,
                    "latency": latency,
                    "items_count": 0,
                    "retries_used": retry_attempt,
                    "input_url": input_url,
                    "input_path": input_path,
                    "error": f"Max retries ({max_retries}) reached. Last error: {err_msg[:200]}"
                }

            # Select next candidate (prioritize another room type if mandatory assets failed)
            next_candidate = None
            if allow_cross_room and must_switch_room_type:
                for cand_rt, cand_uid, cand_name in candidate_styles:
                    if cand_rt.lower() not in failed_room_types and (cand_rt, cand_uid) not in tried_combos:
                        next_candidate = (cand_rt, cand_uid, cand_name)
                        break

            # Fallback to any untried candidate across available options
            if not next_candidate:
                for cand_rt, cand_uid, cand_name in candidate_styles:
                    if (cand_rt, cand_uid) not in tried_combos:
                        next_candidate = (cand_rt, cand_uid, cand_name)
                        break

            if not next_candidate:
                logger.error(f"[!] No further candidate styles or room types available for area {area_m2:.2f} m2. Retries exhausted.")
                log_api_result(
                    room=f"{file_name} [{smart_wizard_room_name} -> {current_room_type}]",
                    room_type=current_room_type,
                    style_id=current_style_id if isinstance(current_style_id, int) else None,
                    status="FAILED",
                    status_code=status_code,
                    latency=latency,
                    items_count=0,
                    retries_used=retry_attempt,
                    saved_to=None,
                    db_path=db_path,
                    reason=f"{mapping_text}. Candidate options exhausted: {err_msg[:180]}",
                    input_url=input_url,
                    input_path=input_path,
                    template_name=template_name
                )
                return {
                    "file": file_name,
                    "payload_path": payload_path,
                    "smart_wizard_room_name": smart_wizard_room_name,
                    "smart_wizard_nominal_type": smart_wizard_nominal_type,
                    "smart_wizard_area_id": smart_wizard_area_id,
                    "applied_room_type": current_room_type,
                    "applied_style_id": current_style_id,
                    "applied_style_name": applied_style_name,
                    "room_type_switched": (current_room_type.lower() != smart_wizard_nominal_type.lower()),
                    "status": "FAILED",
                    "status_code": status_code,
                    "latency": latency,
                    "items_count": 0,
                    "retries_used": retry_attempt,
                    "input_url": input_url,
                    "input_path": input_path,
                    "error": f"All candidate options exhausted. Last error: {err_msg[:200]}"
                }

            # Advance to next candidate
            tried_combos.add((next_candidate[0], next_candidate[1]))
            switched_type = (next_candidate[0] != current_room_type)
            current_room_type = next_candidate[0]
            current_style_id = next_candidate[1]
            next_name = next_candidate[2]
            retry_attempt += 1

            payload["room_type"] = current_room_type
            payload["style_id"] = current_style_id

            if switched_type:
                logger.info(
                    f"\n[*] [RETRY #{retry_attempt}/{max_retries}] 🔄 Switched Room Type to '{current_room_type}' "
                    f"with Style ID {current_style_id} ('{next_name}') to test geometry validity..."
                )
            else:
                logger.info(
                    f"\n[*] [RETRY #{retry_attempt}/{max_retries}] Selected alternative style ID {current_style_id} ('{next_name}') for '{current_room_type}'"
                )

            logger.info(f"[*] Retrying API with payload (Room Type: '{current_room_type}', Style ID: {current_style_id})...")
            continue

        # =================================================================
        # CASE 4: Any other unexpected HTTP status
        # =================================================================
        else:
            err_msg = response.text
            applied_style_name = lookup_style_name(current_style_id, current_room_type)
            logger.error(f"[!] [UNEXPECTED STATUS {status_code}] Response body: {err_msg[:400]}")
            log_api_error(
                room=f"{file_name} [{smart_wizard_room_name} -> {current_room_type}]",
                style_id=current_style_id if isinstance(current_style_id, int) else None,
                status_code=status_code,
                error_message=err_msg,
                retry_attempt=retry_attempt,
                db_path=db_path,
                input_url=input_url,
                input_path=input_path
            )
            return {
                "file": file_name,
                "payload_path": payload_path,
                "smart_wizard_room_name": smart_wizard_room_name,
                "smart_wizard_nominal_type": smart_wizard_nominal_type,
                "smart_wizard_area_id": smart_wizard_area_id,
                "applied_room_type": current_room_type,
                "applied_style_id": current_style_id,
                "applied_style_name": applied_style_name,
                "room_type_switched": (current_room_type.lower() != smart_wizard_nominal_type.lower()),
                "status": "FAILED",
                "status_code": status_code,
                "latency": latency,
                "items_count": 0,
                "retries_used": retry_attempt,
                "input_url": input_url,
                "input_path": input_path,
                "error": err_msg[:300]
            }


def run_automation(
    payloads_dir: str = DEFAULT_PAYLOADS_DIR,
    target_file: Optional[str] = None,
    endpoint_url: str = DEFAULT_ENDPOINT,
    api_key: str = DEFAULT_API_KEY,
    output_dir: str = DEFAULT_RESULTS_DIR,
    log_file: str = DEFAULT_LOG_FILE,
    max_retries: int = DEFAULT_MAX_RETRIES,
    db_path: str = DEFAULT_DB_FILE,
    rules_file: str = DEFAULT_RULES_FILE,
    allow_cross_room: bool = True,
    input_url: Optional[str] = None,
    input_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Runs automated testing on room payloads against the inspiration endpoint.
    """
    init_db(db_path)
    logger = setup_logger(log_file)

    db_mgr = get_db(db_path)
    db_label = "MySQL" if db_mgr.is_mysql else "SQLite"

    logger.info("=" * 75)
    logger.info("       SMART WIZARD INSPIRATION ENGINE - AUTOMATED API TEST")
    logger.info("=" * 75)
    logger.info(f"Target Endpoint : {endpoint_url}")
    masked_key = api_key[:8] + "..." + api_key[-8:] if len(api_key) > 16 else "***"
    logger.info(f"API Key         : {masked_key}")
    logger.info(f"Max Retries     : {max_retries} attempts per room")
    logger.info(f"Cross-Room Test : {'Enabled (will try alternate room types if geometry fails)' if allow_cross_room else 'Disabled'}")
    logger.info(f"Database ({db_label}) : {db_mgr.db_url}")
    logger.info(f"Execution Time  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Log Output File : {log_file}")
    logger.info("=" * 75)

    # Determine list of files to test
    if target_file:
        if not os.path.exists(target_file):
            logger.error(f"Target file not found: {target_file}")
            return []
        files_to_test = [target_file]
    else:
        if not os.path.exists(payloads_dir):
            logger.error(f"Payloads directory not found: {payloads_dir}")
            return []
        files = sorted(os.listdir(payloads_dir))
        files_to_test = [os.path.join(payloads_dir, f) for f in files if f.endswith(".json")]

    if not files_to_test:
        logger.warning(f"No JSON payloads found in: {payloads_dir}")
        return []

    logger.info(f"[*] Found {len(files_to_test)} room payload(s) to test.\n")

    summary_results = []
    total_start = time.time()

    for p_path in files_to_test:
        res = test_room_payload(
            payload_path=p_path,
            endpoint_url=endpoint_url,
            api_key=api_key,
            output_dir=output_dir,
            logger=logger,
            max_retries=max_retries,
            db_path=db_path,
            rules_file=rules_file,
            allow_cross_room=allow_cross_room,
            input_url=input_url,
            input_path=input_path or p_path
        )
        summary_results.append(res)

    total_duration = time.time() - total_start

    # Print Summary Table
    logger.info("\n" + "=" * 115)
    logger.info(f"{'TEST EXECUTION SUMMARY':^115}")
    logger.info("=" * 115)
    logger.info(f"{'Smart Wizard Room':<18} | {'Applied Inspiration':<20} | {'Style':<18} | {'Status':<12} | {'Retries':<8} | {'Latency':>8} | {'Placed Items':>12}")
    logger.info("-" * 115)

    success_count = 0
    server_error_count = 0
    failed_count = 0

    for r in summary_results:
        sw_name = r.get("smart_wizard_room_name") or r.get("file", "unknown")
        app_type = r.get("applied_room_type") or r.get("room_type", "unknown")
        s_id = r.get("applied_style_id") or r.get("style_id", "-")
        s_name = r.get("applied_style_name", "")
        style_disp = f"#{s_id} {s_name}"[:18]
        status = r.get("status", "FAILED")
        status_code = r.get("status_code", 0)
        retries = r.get("retries_used", 0)
        latency_str = f"{r.get('latency', 0.0):>6.2f}s"
        items_cnt = f"{r.get('items_count', 0)} items"

        if status == "SUCCESS":
            status_tag = "200 OK"
            success_count += 1
        elif status == "SERVER_ERROR":
            status_tag = f"ERR {status_code}"
            server_error_count += 1
            failed_count += 1
        else:
            status_tag = f"ERR {status_code}" if status_code else "FAILED"
            failed_count += 1

        logger.info(
            f"{sw_name:<18} | {app_type:<20} | {style_disp:<18} | {status_tag:<12} | {retries:<8} | {latency_str:>8} | {items_cnt:>12}"
        )

    logger.info("=" * 115)
    logger.info(f"Total Tested: {len(summary_results)} | Passed: {success_count} | Failed: {failed_count} (Server 500: {server_error_count})")
    logger.info(f"Total Execution Duration: {total_duration:.2f}s")
    logger.info(f"All full inspiration results saved in: {output_dir}/")
    logger.info(f"Detailed run log saved in: {log_file}")
    logger.info(f"Database ({db_label}) Persistence: {db_mgr.db_url}")
    logger.info("=" * 115 + "\n")

    return summary_results


def automate_floorplan(
    floorplan_source: Union[str, Dict[str, Any]],
    output_base_dir: str = DEFAULT_RESULTS_DIR,
    endpoint_url: str = DEFAULT_ENDPOINT,
    api_key: str = DEFAULT_API_KEY,
    rules_file: str = DEFAULT_RULES_FILE,
    max_retries: int = DEFAULT_MAX_RETRIES,
    allow_cross_room: bool = True,
    db_path: str = DEFAULT_DB_FILE,
    log_file: str = DEFAULT_LOG_FILE,
    logger: Optional[logging.Logger] = None,
    custom_template_name: Optional[str] = None,
    container: Optional[str] = None,
    save_to_disk: bool = False
) -> Dict[str, Any]:
    """
    Automates inspiration generation for an entire floorplan.
    If save_to_disk is True, creates local folders and saves JSON results.
    Otherwise, processes everything in-memory without saving local files.
    """
    init_db(db_path)
    logger = logger or setup_logger(log_file)

    from azure_blob_service import get_blob_manager
    from create_room_inspiration_payload import generate_all_room_payloads

    manager = get_blob_manager(container)
    if isinstance(floorplan_source, dict):
        fp_data = floorplan_source
        resolved_blob = custom_template_name or fp_data.get("name") or "uploaded_floorplan.json"
        if not resolved_blob.lower().endswith(".json"):
            resolved_blob += ".json"
        blob_url = None
    elif os.path.isfile(floorplan_source):
        with open(floorplan_source, "r", encoding="utf-8") as f:
            fp_data = json.load(f)
        resolved_blob = custom_template_name or os.path.basename(floorplan_source)
        blob_url = None
    else:
        resolved_blob = manager.find_blob(floorplan_source)
        blob_url = manager.get_blob_url(resolved_blob)
        fp_data = manager.get_floorplan_json(resolved_blob)

    target_dir, category, template_name = resolve_mirrored_template_dir(resolved_blob, output_base_dir)

    if save_to_disk:
        os.makedirs(target_dir, exist_ok=True)
        orig_fp_path = os.path.join(target_dir, "original_floorplan.json")
        with open(orig_fp_path, "w", encoding="utf-8") as f:
            json.dump(fp_data, f, indent=2)

    logger.info("=" * 85)
    logger.info(f"[*] AUTOMATING FLOORPLAN: {resolved_blob}")
    logger.info(f"    - BHK Category : {category}")
    logger.info(f"    - Azure Path   : {resolved_blob}")
    logger.info(f"    - Storage Mode : {'Disk Files' if save_to_disk else 'In-Memory Only'}")
    logger.info("=" * 85)

    # 2. Generate room-level payloads
    payloads_dict = generate_all_room_payloads(
        floorplan_data=fp_data,
        rules_source=rules_file,
        output_dir=target_dir if save_to_disk else None
    )

    # 3. Test each room payload in-memory or from disk
    room_results = []
    for aid, p_data in payloads_dict.items():
        rm_slug = p_data.get("room_type", "room").lower().replace(" ", "_")
        p_file = os.path.join(target_dir, f"{rm_slug}.json") if save_to_disk else f"{rm_slug}.json"
        res = test_room_payload(
            payload_path=p_file,
            payload_data=p_data,
            endpoint_url=endpoint_url,
            api_key=api_key,
            output_dir=target_dir,
            logger=logger,
            max_retries=max_retries,
            db_path=db_path,
            rules_file=rules_file,
            allow_cross_room=allow_cross_room,
            input_url=blob_url,
            input_path=resolved_blob,
            template_name=custom_template_name or resolved_blob,
            save_to_disk=save_to_disk
        )
        if save_to_disk and res.get("status_code") != 200:
            if os.path.exists(p_file):
                try:
                    os.remove(p_file)
                except Exception:
                    pass
        room_results.append(res)

    # 4. Build Room Inspiration Mapping
    room_mappings = []
    for r in room_results:
        sw_name = r.get("smart_wizard_room_name", "unknown")
        app_type = r.get("applied_room_type", "unknown")
        nom_type = r.get("smart_wizard_nominal_type", "unknown")
        s_id = r.get("applied_style_id")
        s_name = r.get("applied_style_name", "")
        switched = r.get("room_type_switched", False)
        is_success = (r.get("status") == "SUCCESS")

        room_mappings.append({
            "smart_wizard_room_name": sw_name,
            "smart_wizard_area_id": r.get("smart_wizard_area_id", ""),
            "smart_wizard_nominal_type": nom_type,
            "applied_inspiration_room_type": app_type,
            "applied_style_id": s_id,
            "applied_style_name": s_name,
            "room_type_switched": switched,
            "status": r.get("status", "FAILED"),
            "status_code": r.get("status_code", 0),
            "items_count": r.get("items_count", 0),
            "latency": r.get("latency", 0.0),
            "retries_used": r.get("retries_used", 0),
            "payload_file": os.path.basename(r.get("payload_path") or r.get("file") or "") if is_success else None,
            "result_file": os.path.basename(r.get("saved_to") or "") if r.get("saved_to") else None,
            "error": r.get("error"),
            "mapping_summary": (
                f"Smart Wizard room '{sw_name}' -> Applied Inspiration '{app_type}' "
                f"(Style #{s_id}: {s_name})"
                + (" [Cross-Room Fallback]" if switched else "")
                + (" [Inspiration Succeeded]" if is_success else " [Inspiration Failed - Excluded]")
            )
        })

    has_500_error = any(
        r.get("status") == "SERVER_ERROR" or (isinstance(r.get("status_code"), int) and r.get("status_code") >= 500)
        for r in room_results
    )
    overall_status = "FAILED" if has_500_error else "SUCCESS"
    overall_message = (
        "Floorplan processing failed due to internal server error(s) in one or more rooms."
        if has_500_error else
        "Floorplan processing completed successfully. All rooms evaluated."
    )

    formatted_rooms = []
    for r in room_results:
        st_code = r.get("status_code", 0)
        st_val = r.get("status")
        if st_code >= 500 or st_val == "SERVER_ERROR":
            rm_status = "FAILED"
            rm_msg = f"Internal Server Error: {r.get('error') or '500 Server Error'}"
        else:
            rm_status = "SUCCESS"
            if st_code == 200:
                rm_msg = "3D layout generated successfully."
            else:
                rm_msg = r.get("error") or "Room geometry evaluated. Handled style constraint validation."
        
        rm_dict = {
            "room_name": r.get("smart_wizard_room_name") or "Unknown Room",
            "status": rm_status,
        }
        if rm_msg:
            rm_dict["message"] = rm_msg
        formatted_rooms.append(rm_dict)

    mapping_payload = {
        "status": overall_status,
        "message": overall_message,
        "floorplan": template_name,
        "azure_blob_path": resolved_blob,
        "category": category,
        "target_folder": os.path.abspath(target_dir) if save_to_disk else None,
        "relative_folder": target_dir if save_to_disk else None,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_rooms": len(room_mappings),
        "passed_rooms": sum(1 for r in formatted_rooms if r["status"] == "SUCCESS"),
        "failed_rooms": sum(1 for r in formatted_rooms if r["status"] == "FAILED"),
        "rooms": formatted_rooms,
        "room_mappings": room_mappings
    }

    if save_to_disk:
        mapping_path = os.path.join(target_dir, "inspiration_mapping.json")
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(mapping_payload, f, indent=2)
        logger.info(f"[+] Complete inspiration mapping saved to: {mapping_path}")

    # Print Table
    logger.info("\n" + "=" * 105)
    logger.info(f" ROOM INSPIRATION MAPPING: {template_name} ({category})")
    if save_to_disk:
        logger.info(f" Saved Folder: {target_dir}")
    logger.info("=" * 105)
    logger.info(f"{'Smart Wizard Room':<18} | {'Nominal':<12} | {'Applied Inspiration':<20} | {'Style':<18} | {'Status':<10} | {'Items':>6}")
    logger.info("-" * 105)
    for m in room_mappings:
        s_tag = f"#{m['applied_style_id']} {m['applied_style_name']}"[:18]
        status_disp = "200 OK" if m["status"] == "SUCCESS" else f"ERR {m['status_code']}"
        logger.info(
            f"{m['smart_wizard_room_name']:<18} | {m['smart_wizard_nominal_type']:<12} | {m['applied_inspiration_room_type']:<20} | {s_tag:<18} | {status_disp:<10} | {m['items_count']:>6}"
        )
    logger.info("=" * 105 + "\n")

    return mapping_payload


def automate_bhk(
    category: str = "1BHK",
    limit: Optional[int] = None,
    output_base_dir: str = DEFAULT_RESULTS_DIR,
    endpoint_url: str = DEFAULT_ENDPOINT,
    api_key: str = DEFAULT_API_KEY,
    rules_file: str = DEFAULT_RULES_FILE,
    max_retries: int = DEFAULT_MAX_RETRIES,
    allow_cross_room: bool = True,
    db_path: str = DEFAULT_DB_FILE,
    log_file: str = DEFAULT_LOG_FILE,
    container: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Automates all floorplans under a specific BHK category (e.g. 1BHK, 2BHK) from Azure:
    - Lists matching templates from Azure Blob Storage.
    - Creates mirrored folder structure following Azure for each template.
    - Runs room generation and inspiration testing for every template.
    - Generates room mapping summaries and stores every JSON in each template's folder.
    """
    logger = setup_logger(log_file)
    from azure_blob_service import get_blob_manager
    manager = get_blob_manager(container)

    logger.info("=" * 85)
    logger.info(f"       STARTING AUTOMATION FOR CATEGORY: {category.upper()} (Container: {manager.container_name})")
    logger.info("=" * 85)

    templates = manager.list_floorplans(category=category)
    if not templates:
        logger.warning(f"No templates found in container '{manager.container_name}' matching category '{category}'.")
        return []

    if limit and limit > 0:
        templates = templates[:limit]

    logger.info(f"[*] Found {len(templates)} floorplan template(s) to process for {category.upper()}.\n")

    all_floorplan_results = []
    for idx, t in enumerate(templates, 1):
        blob_path = t["name"]
        logger.info(f"\n>> [{idx}/{len(templates)}] Processing: {blob_path}")
        try:
            res = automate_floorplan(
                floorplan_source=blob_path,
                output_base_dir=output_base_dir,
                endpoint_url=endpoint_url,
                api_key=api_key,
                rules_file=rules_file,
                max_retries=max_retries,
                allow_cross_room=allow_cross_room,
                db_path=db_path,
                log_file=log_file,
                logger=logger,
                container=container
            )
            all_floorplan_results.append(res)
        except Exception as e:
            logger.error(f"[!] Error automating {blob_path}: {e}")

    logger.info("\n" + "=" * 90)
    logger.info(f" CATEGORY {category.upper()} AUTOMATION COMPLETE: {len(all_floorplan_results)}/{len(templates)} templates processed")
    logger.info(f" Results stored under: {output_base_dir}/{category}/")
    logger.info("=" * 90 + "\n")

    return all_floorplan_results


def main():
    parser = argparse.ArgumentParser(
        description="Automated API Testing for Smart Wizard Room Inspiration Engine with Fallback & Error DB"
    )
    parser.add_argument(
        "--bhk",
        help="Automate all floorplans in a BHK category from Azure (e.g. '1BHK', '2BHK', '3BHK', '4BHK')"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional limit on the number of templates to automate for --bhk"
    )
    parser.add_argument(
        "-b", "--blob",
        help="Automate a specific Azure Blob template (e.g. '1BHK/Balanced_Single_Living/1bhk_Southface_422.3Sqft.json')"
    )
    parser.add_argument(
        "-d", "--input-dir",
        default=DEFAULT_PAYLOADS_DIR,
        help=f"Directory containing room payload JSON files (default: '{DEFAULT_PAYLOADS_DIR}')"
    )
    parser.add_argument(
        "-f", "--file",
        help="Test a single room payload JSON file or automate a full floorplan JSON file"
    )
    parser.add_argument(
        "-u", "--url",
        default=DEFAULT_ENDPOINT,
        help=f"Inspiration endpoint URL (default: '{DEFAULT_ENDPOINT}')"
    )
    parser.add_argument(
        "-k", "--api-key",
        default=DEFAULT_API_KEY,
        help="x-api-key authentication token"
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=DEFAULT_RESULTS_DIR,
        help=f"Directory to save generated inspiration JSON results (default: '{DEFAULT_RESULTS_DIR}')"
    )
    parser.add_argument(
        "-l", "--log-file",
        default=DEFAULT_LOG_FILE,
        help=f"File to save persistent test logs (default: '{DEFAULT_LOG_FILE}')"
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Max alternate styles to retry on 400/404 errors (default: {DEFAULT_MAX_RETRIES})"
    )
    parser.add_argument(
        "--db-file",
        default=DEFAULT_DB_FILE,
        help=f"SQLite database file to store API errors (default: '{DEFAULT_DB_FILE}')"
    )
    parser.add_argument(
        "--rules",
        default=DEFAULT_RULES_FILE,
        help=f"Style rules JSON file path (default: '{DEFAULT_RULES_FILE}')"
    )
    parser.add_argument(
        "--no-cross-room",
        action="store_true",
        help="Disable cross-room-type fallback when styles fail"
    )
    parser.add_argument(
        "--view-db",
        action="store_true",
        help="Display all errors stored in the database and exit"
    )

    args = parser.parse_args()

    if args.view_db:
        print_db_errors(db_url=args.db_file)
        print_db_results(db_url=args.db_file)
        return

    # 1. Automate an entire BHK Category from Azure
    if args.bhk:
        automate_bhk(
            category=args.bhk,
            limit=args.limit,
            output_base_dir=args.output_dir,
            endpoint_url=args.url,
            api_key=args.api_key,
            rules_file=args.rules,
            max_retries=args.max_retries,
            allow_cross_room=not args.no_cross_room,
            db_path=args.db_file,
            log_file=args.log_file
        )
        return

    # 2. Automate a specific Azure Blob Template
    if args.blob:
        automate_floorplan(
            floorplan_source=args.blob,
            output_base_dir=args.output_dir,
            endpoint_url=args.url,
            api_key=args.api_key,
            rules_file=args.rules,
            max_retries=args.max_retries,
            allow_cross_room=not args.no_cross_room,
            db_path=args.db_file,
            log_file=args.log_file
        )
        return

    # 3. If -f is given, check if it is a full floorplan JSON or a single room payload
    if args.file and os.path.isfile(args.file):
        try:
            with open(args.file, "r", encoding="utf-8") as f_check:
                check_data = json.load(f_check)
            if "layers" in check_data and "room_json" not in check_data:
                # Full floorplan file -> automate floorplan
                automate_floorplan(
                    floorplan_source=args.file,
                    output_base_dir=args.output_dir,
                    endpoint_url=args.url,
                    api_key=args.api_key,
                    rules_file=args.rules,
                    max_retries=args.max_retries,
                    allow_cross_room=not args.no_cross_room,
                    db_path=args.db_file,
                    log_file=args.log_file
                )
                return
        except Exception:
            pass

    # 4. Standard Room Payload Automation
    run_automation(
        payloads_dir=args.input_dir,
        target_file=args.file,
        endpoint_url=args.url,
        api_key=args.api_key,
        output_dir=args.output_dir,
        log_file=args.log_file,
        max_retries=args.max_retries,
        db_path=args.db_file,
        rules_file=args.rules,
        allow_cross_room=not args.no_cross_room
    )


if __name__ == "__main__":
    main()

