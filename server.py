"""
server.py

FastAPI Controller & Web Application for Smart Wizard Inspiration Pipeline.
Implements the Phase 4 Architecture:
- GET /api/templates: Lists all floorplan templates from Azure Blob Storage.
- GET /api/template/rooms: Parses and returns room geometry, areas (m2/sqft), polygons, and styles.
- POST /api/run-inspiration: Runs Step 1 room generation & automated API testing with fallback retry.
- GET /api/history: Queries MySQL records (api_results & api_errors).
- GET /api/logs: Fetches recent log outputs.
- GET /: Serves a complete interactive UI dashboard.
"""

import os
import sys
import json
import time
import copy
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, Query, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Import existing backend modules without modifying them
from azure_blob_service import get_blob_manager, AVAILABLE_CONTAINERS
from create_room_inspiration_payload import (
    identify_room_type,
    calculate_room_area_m2,
    generate_room_json,
    generate_all_room_payloads,
    load_style_rules,
    ALLOWED_USER_STYLE_IDS
)
from test_inspiration_api import (
    test_room_payload,
    get_valid_styles_for_room,
    resolve_mirrored_template_dir,
    automate_floorplan,
    automate_bhk,
    setup_logger,
    DEFAULT_ENDPOINT,
    DEFAULT_API_KEY,
    DEFAULT_RESULTS_DIR,
    DEFAULT_LOG_FILE,
    DEFAULT_RULES_FILE,
    DEFAULT_DB_FILE
)
from inspiration_db import get_db
from visualize_smart_wizard import parse_smart_wizard_json, generate_visualizer_html

app = FastAPI(
    title="Smart Wizard Inspiration API & Dashboard",
    description="Frontend controller and REST API for testing floorplans from Azure Blob Storage",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = setup_logger(DEFAULT_LOG_FILE)


def print_term_log(action: str, detail: str, level: str = "INFO"):
    """Prints a clear, formatted log statement directly to the terminal."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    symbols = {"INFO": "ℹ️", "SUCCESS": "✅", "WARN": "⚠️", "ERROR": "❌", "RUN": "🚀"}
    sym = symbols.get(level, "🔹")
    print(f"[{now_str}] [{level}] {sym} [{action}] {detail}", flush=True)


# =====================================================================
# Request Models
# =====================================================================
class RunInspirationRequest(BaseModel):
    wizard_name: Optional[str] = None
    blob: Optional[str] = None
    container: Optional[str] = None
    floorplan: Optional[Dict[str, Any]] = None
    file_path: Optional[str] = None
    room_type: Optional[str] = None
    style_id: Optional[int] = None
    max_retries: int = 3
    allow_cross_room: bool = True
    output_dir: str = DEFAULT_RESULTS_DIR

    class Config:
        extra = "allow"


class AutomateBhkRequest(BaseModel):
    category: str = "1BHK"
    container: Optional[str] = None
    limit: Optional[int] = None
    max_retries: int = 3
    allow_cross_room: bool = True
    output_dir: str = DEFAULT_RESULTS_DIR


# =====================================================================
# Step 1: Backend API Wrapper Endpoints
# =====================================================================

@app.get("/api/containers")
def get_containers():
    """
    GET /api/containers
    Returns the list of configured Azure Blob containers and the active default.
    """
    return {
        "containers": AVAILABLE_CONTAINERS,
        "default": AVAILABLE_CONTAINERS[0] if AVAILABLE_CONTAINERS else "prod-smartwizardtemplates"
    }


@app.get("/api/templates")
def get_templates(
    category: Optional[str] = None,
    search: Optional[str] = None,
    container: Optional[str] = None
):
    """
    GET /api/templates
    Calls manager.list_floorplans() from azure_blob_service.py for the specified container.
    """
    print_term_log("GET /api/templates", f"Listing templates (Container: '{container or 'default'}', Category: '{category or 'ALL'}', Search: '{search or 'NONE'}')")
    try:
        manager = get_blob_manager(container)
        templates = manager.list_floorplans(category=category, keyword=search)

        # Extract unique categories
        all_blobs = manager.list_floorplans()
        categories = sorted(list(set(t["category"] for t in all_blobs)))

        print_term_log("GET /api/templates", f"Found {len(templates)} matching template(s) in container '{manager.container_name}'", "SUCCESS")
        return {
            "total": len(templates),
            "container": manager.container_name,
            "categories": categories,
            "templates": templates
        }
    except Exception as e:
        print_term_log("GET /api/templates", f"Error fetching templates: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/template/rooms")
def get_template_rooms(
    blob: str = Query(..., description="Blob name, path, or search identifier"),
    container: Optional[str] = Query(None, description="Azure blob container name")
):
    """
    GET /api/template/rooms?blob=...&container=...
    Parses and returns room types, areas (m2 & sqft), polygons, and style options from the selected blob.
    """
    print_term_log("GET /api/template/rooms", f"Fetching room geometry for blob: '{blob}' (Container: '{container or 'default'}')")
    try:
        if os.path.exists(blob):
            with open(blob, "r", encoding="utf-8") as f:
                fp_data = json.load(f)
            resolved_blob = os.path.basename(blob)
        else:
            manager = get_blob_manager(container)
            resolved_blob = manager.find_blob(blob)
            fp_data = manager.get_floorplan_json(resolved_blob)

        # Parse with Smart Wizard visualizer engine (handles mm vs cm auto-detection & Shoelace)
        viz_data = parse_smart_wizard_json(fp_data)

        # Augment rooms with style recommendations and fallback types
        rooms_list = []
        for r in viz_data["rooms"]:
            aid = r["id"]
            raw_name = r["name"]
            norm_type, _ = identify_room_type({"name": raw_name, "id": aid})
            area_m2 = r["area_m2"]
            area_sqft = r["area_sqft"]

            # Retrieve candidate styles for this area
            candidates = get_valid_styles_for_room(
                room_type=norm_type,
                area_m2=area_m2,
                rules_file=DEFAULT_RULES_FILE,
                allow_cross_room=True
            )

            rec_style = None
            for cand_rt, cand_uid, cand_name in candidates:
                if cand_rt.lower() == norm_type.lower():
                    rec_style = {"room_type": cand_rt, "style_id": cand_uid, "name": cand_name}
                    break
            if not rec_style and candidates:
                rec_style = {"room_type": candidates[0][0], "style_id": candidates[0][1], "name": candidates[0][2]}

            fallback_rts = sorted(list(set(c[0] for c in candidates if c[0].lower() != norm_type.lower())))

            rooms_list.append({
                "area_id": aid,
                "name": raw_name,
                "room_type": norm_type,
                "area_m2": area_m2,
                "area_sqft": area_sqft,
                "recommended_style": rec_style,
                "fallback_room_types": fallback_rts,
                "total_compatible_styles": len(candidates),
                "polygon": r["polygon"],
                "center": r.get("center", {"x": 0, "y": 0}),
                "vertices_count": r.get("vertices_count", len(r.get("polygon", [])))
            })

        room_summary = ", ".join([f"{r['name']} ({r['area_m2']} m²)" for r in rooms_list])
        print_term_log(
            "GET /api/template/rooms",
            f"Parsed {len(rooms_list)} room(s), {viz_data['doors_count']} door(s), {viz_data['windows_count']} window(s) from '{resolved_blob}': {room_summary}",
            "SUCCESS"
        )

        return {
            "blob": resolved_blob,
            "unit": viz_data["unit"],
            "direction": viz_data["direction"],
            "direction_angle": viz_data["direction_angle"],
            "total_area_m2": viz_data["total_area_m2"],
            "total_area_sqft": viz_data["total_area_sqft"],
            "rooms_count": len(rooms_list),
            "doors_count": viz_data["doors_count"],
            "windows_count": viz_data["windows_count"],
            "walls_count": viz_data["walls_count"],
            "rooms": rooms_list,
            "walls": viz_data["walls"],
            "openings": viz_data["openings"]
        }
    except Exception as e:
        print_term_log("GET /api/template/rooms", f"Error parsing rooms: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/visualizer", response_class=HTMLResponse)
def visualizer_view(blob: Optional[str] = None, container: Optional[str] = None):
    """
    GET /visualizer?blob=...&container=...
    Directly renders the standalone Smart Wizard 2D CAD visualizer powered by visualize_smart_wizard.py.
    """
    try:
        raw_data = None
        title = "Smart Wizard Floorplan Visualizer"
        if blob:
            if os.path.exists(blob):
                with open(blob, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                title = f"Smart Wizard: {os.path.basename(blob)}"
            else:
                manager = get_blob_manager(container)
                resolved = manager.find_blob(blob)
                raw_data = manager.get_floorplan_json(resolved)
                title = f"Smart Wizard: {os.path.basename(resolved)}"
        else:
            sample_file = "1bhk_south_square_853.2sqft.json"
            if os.path.exists(sample_file):
                with open(sample_file, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                title = f"Smart Wizard: {sample_file}"
            else:
                raw_data = {"unit": "cm", "direction": "N", "layers": {}}

        parsed = parse_smart_wizard_json(raw_data)
        html_code = generate_visualizer_html(parsed, title=title)
        return HTMLResponse(content=html_code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Visualizer generation failed: {e}")


@app.get("/api/visualizer-data")
def visualizer_data(blob: Optional[str] = None, container: Optional[str] = None):
    """
    GET /api/visualizer-data?blob=...&container=...
    Returns parsed Smart Wizard floorplan visualization data structure.
    """
    try:
        if blob:
            if os.path.exists(blob):
                with open(blob, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
            else:
                manager = get_blob_manager(container)
                resolved = manager.find_blob(blob)
                raw_data = manager.get_floorplan_json(resolved)
        else:
            sample_file = "1bhk_south_square_853.2sqft.json"
            if os.path.exists(sample_file):
                with open(sample_file, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
            else:
                raw_data = {"unit": "cm", "direction": "N", "layers": {}}
        return parse_smart_wizard_json(raw_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/run-inspiration")
async def run_inspiration(
    request: Request,
    wizard_name: Optional[str] = Form(None, description="Optional Smart Wizard floorplan template name (WizardName)"),
    file: Optional[UploadFile] = File(None, description="Directly upload Smart Wizard floorplan JSON file (multipart/form-data)")
):
    """
    POST /api/run-inspiration
    Directly tests a floorplan with all default flags:
    - Supports:
      1) wizard_name input form field / query parameter (WizardName)
      2) Direct file upload in Swagger UI / multipart form data ('file' parameter)
      3) Direct Smart Wizard floorplan JSON in body (e.g. { "layers": ... })
      4) Wrapped JSON: { "floorplan": { ... } }
      5) Azure Blob reference: { "blob": "1BHK/..." }
      6) Local file path: { "file_path": "path/to/floorplan.json" }
    - All flags (room_type=None for ALL rooms, max_retries=3, allow_cross_room=True, output_dir=DEFAULT_RESULTS_DIR)
      use built-in defaults automatically without needing to be specified explicitly.
    """
    content_type = request.headers.get("content-type", "").lower()
    fp_data = None
    custom_name = None
    blob_source = None
    container = None
    file_path = None
    room_type = None
    style_id = None
    max_retries = 3
    allow_cross_room = True
    output_dir = DEFAULT_RESULTS_DIR

    # 1. Check if file was uploaded via Swagger UI / multipart form data
    if file and file.filename:
        content = await file.read()
        fp_data = json.loads(content.decode("utf-8"))
        custom_name = file.filename

    if "multipart/form-data" in content_type:
        form = await request.form()
        uploaded_file = form.get("file") or form.get("floorplan") or form.get("upload")
        if uploaded_file and hasattr(uploaded_file, "read") and fp_data is None:
            content = await uploaded_file.read()
            fp_data = json.loads(content.decode("utf-8"))
            custom_name = getattr(uploaded_file, "filename", None)
        
        form_wiz = form.get("wizard_name") or form.get("wizardName") or form.get("WizardName")
        if form_wiz:
            wizard_name = str(form_wiz)
        if "container" in form and form["container"]:
            container = str(form["container"])
        if "room_type" in form and form["room_type"]:
            room_type = str(form["room_type"])
        if "style_id" in form and form["style_id"]:
            try:
                style_id = int(form["style_id"])
            except Exception:
                pass
        if "max_retries" in form and form["max_retries"]:
            try:
                max_retries = int(form["max_retries"])
            except Exception:
                pass
        if "allow_cross_room" in form:
            allow_cross_room = str(form["allow_cross_room"]).lower() in ("true", "1", "yes")
        if "output_dir" in form and form["output_dir"]:
            output_dir = str(form["output_dir"])
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}

        if isinstance(body, dict):
            body_wiz = body.get("wizard_name") or body.get("wizardName") or body.get("WizardName")
            if body_wiz:
                wizard_name = str(body_wiz)

            # Check if body IS directly the floorplan JSON
            if "layers" in body or "processed_house_json" in body or "house_json" in body:
                fp_data = body
                custom_name = body.get("name") or "uploaded_floorplan.json"
            elif "floorplan" in body and isinstance(body["floorplan"], dict):
                fp_data = body["floorplan"]
                custom_name = body.get("name") or fp_data.get("name") or "uploaded_floorplan.json"
            elif "blob" in body and body["blob"]:
                blob_source = body["blob"]
            elif "file_path" in body and body["file_path"]:
                file_path = body["file_path"]

            container = body.get("container")
            room_type = body.get("room_type")
            style_id = body.get("style_id")
            if "max_retries" in body and body["max_retries"] is not None:
                try:
                    max_retries = int(body["max_retries"])
                except Exception:
                    pass
            if "allow_cross_room" in body and body["allow_cross_room"] is not None:
                allow_cross_room = bool(body["allow_cross_room"])
            if "output_dir" in body and body["output_dir"]:
                output_dir = str(body["output_dir"])

    if wizard_name:
        custom_name = str(wizard_name).strip()
        if not custom_name.lower().endswith(".json"):
            custom_name += ".json"

    # Resolve floorplan data
    blob_url = None
    if fp_data is not None:
        resolved_blob = custom_name or fp_data.get("name") or "uploaded_floorplan.json"
        if not resolved_blob.lower().endswith(".json"):
            resolved_blob += ".json"
    elif blob_source:
        manager = get_blob_manager(container)
        resolved_blob = manager.find_blob(blob_source)
        blob_url = manager.get_blob_url(resolved_blob)
        fp_data = manager.get_floorplan_json(resolved_blob)
    elif file_path and os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            fp_data = json.load(f)
        resolved_blob = custom_name or os.path.basename(file_path)
    else:
        raise HTTPException(
            status_code=400,
            detail="No floorplan provided. Send raw floorplan JSON in the request body, upload a file, or specify 'blob'."
        )

    print_term_log(
        "POST /api/run-inspiration",
        f"Execution Request: Source='{resolved_blob}', Container='{container or 'default'}', Room='{room_type or 'ALL'}', MaxRetries={max_retries}, CrossRoom={allow_cross_room}",
        "RUN"
    )

    try:
        target_dir, category, template_name = resolve_mirrored_template_dir(resolved_blob, output_dir)
        results = []

        if room_type:
            # Single Room Test Flow (In-Memory)
            print_term_log("POST /api/run-inspiration", f"Generating Step 1 payload in-memory for room '{room_type}'...")
            payload = generate_room_json(
                floorplan_data=fp_data,
                style_id=style_id or 237,
                room_type=room_type,
                rules_source=DEFAULT_RULES_FILE
            )

            print_term_log("POST /api/run-inspiration", f"Sending payload to Inspiration Engine API...")
            res = test_room_payload(
                payload_path=f"{room_type.lower().replace(' ', '_')}.json",
                payload_data=payload,
                endpoint_url=DEFAULT_ENDPOINT,
                api_key=DEFAULT_API_KEY,
                output_dir=target_dir,
                logger=logger,
                max_retries=max_retries,
                allow_cross_room=allow_cross_room,
                input_url=blob_url,
                input_path=resolved_blob,
                template_name=wizard_name or template_name or resolved_blob,
                save_to_disk=False
            )
            results.append(res)
        else:
            # All Rooms Test Flow via automate_floorplan logic (In-Memory)
            print_term_log("POST /api/run-inspiration", f"Automating all rooms for '{resolved_blob}' in-memory...")
            floorplan_summary = automate_floorplan(
                floorplan_source=fp_data,
                custom_template_name=wizard_name or resolved_blob,
                output_base_dir=output_dir,
                endpoint_url=DEFAULT_ENDPOINT,
                api_key=DEFAULT_API_KEY,
                rules_file=DEFAULT_RULES_FILE,
                max_retries=max_retries,
                allow_cross_room=allow_cross_room,
                db_path=DEFAULT_DB_FILE,
                log_file=DEFAULT_LOG_FILE,
                logger=logger,
                container=container,
                save_to_disk=False
            )
            room_mappings = floorplan_summary.get("room_mappings", [])

            results = []
            for m in room_mappings:
                results.append({
                    "file": m.get("payload_file"),
                    "smart_wizard_room_name": m.get("smart_wizard_room_name"),
                    "smart_wizard_nominal_type": m.get("smart_wizard_nominal_type"),
                    "smart_wizard_area_id": m.get("smart_wizard_area_id"),
                    "applied_room_type": m.get("applied_inspiration_room_type"),
                    "room_type": m.get("applied_inspiration_room_type"),
                    "style_id": m.get("applied_style_id"),
                    "applied_style_id": m.get("applied_style_id"),
                    "applied_style_name": m.get("applied_style_name"),
                    "room_type_switched": m.get("room_type_switched", False),
                    "status": m.get("status"),
                    "status_code": m.get("status_code"),
                    "latency": m.get("latency"),
                    "retries_used": m.get("retries_used"),
                    "saved_to": None,
                    "target_folder": None,
                    "placed_items": [],
                    "error": m.get("error")
                })

            has_500_error = any(
                m.get("status") == "SERVER_ERROR" or (isinstance(m.get("status_code"), int) and m.get("status_code") >= 500)
                for m in room_mappings
            )
            overall_status = "FAILED" if has_500_error else "SUCCESS"
            overall_message = (
                "Floorplan processing failed due to internal server error(s) in one or more rooms."
                if has_500_error else
                "Floorplan processing completed successfully. All rooms evaluated."
            )

            formatted_rooms = []
            for m in room_mappings:
                st_code = m.get("status_code", 0)
                st_val = m.get("status")
                if st_code >= 500 or st_val == "SERVER_ERROR":
                    rm_status = "FAILED"
                    rm_msg = f"Internal Server Error: {m.get('error') or '500 Server Error'}"
                else:
                    rm_status = "SUCCESS"
                    if st_code == 200:
                        rm_msg = "3D layout generated successfully."
                    else:
                        rm_msg = m.get("error") or "Room geometry evaluated. Handled style constraint validation."
                
                rm_dict = {
                    "room_name": m.get("smart_wizard_room_name") or m.get("room_type") or "Unknown Room",
                    "status": rm_status,
                }
                if rm_msg:
                    rm_dict["message"] = rm_msg
                formatted_rooms.append(rm_dict)

            passed_count = sum(1 for r in formatted_rooms if r["status"] == "SUCCESS")
            failed_count = sum(1 for r in formatted_rooms if r["status"] == "FAILED")

            return {
                "status": overall_status,
                "message": overall_message,
                "success": not has_500_error,
                "total_tested": len(formatted_rooms),
                "passed": passed_count,
                "failed": failed_count,
                "rooms": formatted_rooms
            }

        # Format single-room flow response in-memory
        room_mappings = [{
            "smart_wizard_room_name": r.get("smart_wizard_room_name", room_type),
            "smart_wizard_area_id": r.get("smart_wizard_area_id", ""),
            "smart_wizard_nominal_type": r.get("smart_wizard_nominal_type", room_type),
            "applied_inspiration_room_type": r.get("applied_room_type", r.get("room_type")),
            "applied_style_id": r.get("applied_style_id", r.get("style_id")),
            "applied_style_name": r.get("applied_style_name", ""),
            "room_type_switched": r.get("room_type_switched", False),
            "status": r.get("status", "FAILED"),
            "status_code": r.get("status_code", 0),
            "items_count": r.get("items_count", 0),
            "latency": r.get("latency", 0.0),
            "retries_used": r.get("retries_used", 0),
            "error": r.get("error"),
            "mapping_summary": f"Smart Wizard '{r.get('smart_wizard_room_name')}' -> Applied Inspiration '{r.get('applied_room_type')}'"
        } for r in results]

        has_500_error = any(
            r.get("status") == "SERVER_ERROR" or (isinstance(r.get("status_code"), int) and r.get("status_code") >= 500)
            for r in results
        )
        overall_status = "FAILED" if has_500_error else "SUCCESS"
        overall_message = (
            "Floorplan processing failed due to internal server error(s) in one or more rooms."
            if has_500_error else
            "Floorplan processing completed successfully. All rooms evaluated."
        )

        formatted_rooms = []
        for r in results:
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
                "room_name": r.get("smart_wizard_room_name") or room_type or "Unknown Room",
                "status": rm_status,
            }
            if rm_msg:
                rm_dict["message"] = rm_msg
            formatted_rooms.append(rm_dict)

        passed_count = sum(1 for r in formatted_rooms if r["status"] == "SUCCESS")
        failed_count = sum(1 for r in formatted_rooms if r["status"] == "FAILED")

        print_term_log(
            "POST /api/run-inspiration",
            f"Execution Completed (In-Memory): Status={overall_status}. {passed_count}/{len(results)} room(s) passed.",
            "SUCCESS" if overall_status == "SUCCESS" else "WARN"
        )

        return {
            "status": overall_status,
            "message": overall_message,
            "success": not has_500_error,
            "total_tested": len(formatted_rooms),
            "passed": passed_count,
            "failed": failed_count,
            "rooms": formatted_rooms
        }

    except Exception as e:
        print_term_log("POST /api/run-inspiration", f"Error during inspiration execution: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/automate-bhk")
def api_automate_bhk(req: AutomateBhkRequest):
    """
    POST /api/automate-bhk
    Automates all or top-N floorplans in a given BHK category from Azure Blob Storage.
    Creates folder structure following Azure, saves all JSONs, and generates room inspiration mappings.
    """
    print_term_log(
        "POST /api/automate-bhk",
        f"Automating Category '{req.category}' (Limit={req.limit or 'ALL'}, MaxRetries={req.max_retries})",
        "RUN"
    )
    try:
        results = automate_bhk(
            category=req.category,
            limit=req.limit,
            output_base_dir=req.output_dir,
            endpoint_url=DEFAULT_ENDPOINT,
            api_key=DEFAULT_API_KEY,
            rules_file=DEFAULT_RULES_FILE,
            max_retries=req.max_retries,
            allow_cross_room=req.allow_cross_room,
            db_path=DEFAULT_DB_FILE,
            log_file=DEFAULT_LOG_FILE,
            container=req.container
        )
        return {
            "success": True,
            "category": req.category,
            "total_floorplans": len(results),
            "floorplans": results
        }
    except Exception as e:
        print_term_log("POST /api/automate-bhk", f"Error in BHK automation: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/history")
def get_history(limit: int = 50):
    """
    GET /api/history
    Queries MySQL records from inspiration_db.py (api_results & api_errors).
    """
    print_term_log("GET /api/history", f"Querying latest {limit} records from MySQL database...")
    try:
        db_mgr = get_db()
        errors = db_mgr.get_all_errors(limit=limit)
        results = db_mgr.get_all_results(limit=limit)
        db_engine = "MySQL" if db_mgr.is_mysql else "SQLite"

        print_term_log("GET /api/history", f"Retrieved {len(results)} test runs and {len(errors)} errors from {db_engine}", "SUCCESS")
        return {
            "engine": db_engine,
            "db_url": db_mgr.db_url,
            "total_results": len(results),
            "total_errors": len(errors),
            "results": results,
            "errors": errors
        }
    except Exception as e:
        print_term_log("GET /api/history", f"Error querying database: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/logs")
def get_logs(lines: int = 60):
    """Returns recent log lines from inspiration_api_test.log."""
    if not os.path.exists(DEFAULT_LOG_FILE):
        return {"logs": []}
    try:
        with open(DEFAULT_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return {"logs": all_lines[-lines:]}
    except Exception as e:
        return {"logs": [f"Error reading log file: {e}"]}


# =====================================================================
# Complete Interactive Frontend Dashboard (HTML/CSS/JS)
# Implements Steps 2, 3, 4, 5 directly in the browser!
# =====================================================================
@app.get("/", response_class=HTMLResponse)
def index_page():
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Smart Wizard Inspiration Engine - Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #0b0f19;
            --bg-card: rgba(18, 24, 38, 0.75);
            --bg-card-hover: rgba(26, 34, 52, 0.85);
            --border: rgba(255, 255, 255, 0.08);
            --primary: #4f46e5;
            --primary-glow: rgba(79, 70, 229, 0.35);
            --primary-light: #818cf8;
            --accent: #06b6d4;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
            --text: #f3f4f6;
            --text-muted: #9ca3af;
            --font-main: 'Plus Jakarta Sans', sans-serif;
            --font-code: 'JetBrains Mono', monospace;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg-base);
            color: var(--text);
            font-family: var(--font-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            background-image: 
                radial-gradient(circle at 15% 10%, rgba(79, 70, 229, 0.12) 0%, transparent 45%),
                radial-gradient(circle at 85% 90%, rgba(6, 182, 212, 0.10) 0%, transparent 45%);
            background-attachment: fixed;
        }

        header {
            padding: 1.25rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            backdrop-filter: blur(12px);
            background: rgba(11, 15, 25, 0.8);
            position: sticky;
            top: 0;
            z-index: 50;
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        .brand-icon {
            width: 38px;
            height: 38px;
            border-radius: 10px;
            background: linear-gradient(135deg, var(--primary), var(--accent));
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.25rem;
            box-shadow: 0 0 15px var(--primary-glow);
        }
        .brand-text h1 { font-size: 1.15rem; font-weight: 700; letter-spacing: -0.02em; }
        .brand-text span { font-size: 0.75rem; color: var(--text-muted); }

        .db-badge {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.25);
            padding: 0.4rem 0.85rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            color: #34d399;
            font-weight: 600;
        }
        .db-pulse {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #34d399;
            box-shadow: 0 0 8px #34d399;
            animation: pulse 2s infinite;
        }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

        main {
            flex: 1;
            padding: 1.5rem 2rem;
            display: grid;
            grid-template-columns: 360px 1fr;
            gap: 1.5rem;
            max-width: 1750px;
            margin: 0 auto;
            width: 100%;
        }

        .card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 1.25rem;
            backdrop-filter: blur(16px);
            display: flex;
            flex-direction: column;
            gap: 1rem;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 0.75rem;
            border-bottom: 1px solid var(--border);
        }
        .card-title {
            font-size: 0.95rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .card-counter {
            font-size: 0.75rem;
            padding: 0.2rem 0.55rem;
            background: rgba(255, 255, 255, 0.08);
            border-radius: 999px;
            color: var(--text-muted);
        }

        /* Template Selector */
        .category-tabs {
            display: flex;
            gap: 0.4rem;
            flex-wrap: wrap;
        }
        .cat-btn {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border);
            color: var(--text-muted);
            padding: 0.35rem 0.7rem;
            border-radius: 8px;
            font-size: 0.75rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        .cat-btn:hover { background: rgba(255, 255, 255, 0.1); color: var(--text); }
        .cat-btn.active {
            background: var(--primary);
            border-color: var(--primary-light);
            color: white;
            box-shadow: 0 0 10px var(--primary-glow);
        }

        .search-box {
            position: relative;
        }
        .search-input {
            width: 100%;
            padding: 0.6rem 0.85rem;
            background: rgba(0, 0, 0, 0.35);
            border: 1px solid var(--border);
            border-radius: 10px;
            color: var(--text);
            font-size: 0.8rem;
            outline: none;
            transition: border 0.2s;
        }
        .search-input:focus { border-color: var(--primary-light); }

        .template-list {
            max-height: 600px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            padding-right: 0.25rem;
        }
        .template-list::-webkit-scrollbar { width: 6px; }
        .template-list::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.15); border-radius: 3px; }

        .tpl-item {
            padding: 0.85rem;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border);
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .tpl-item:hover {
            background: var(--bg-card-hover);
            border-color: rgba(255, 255, 255, 0.2);
            transform: translateY(-1px);
        }
        .tpl-item.selected {
            background: rgba(79, 70, 229, 0.15);
            border-color: var(--primary-light);
            box-shadow: 0 0 12px var(--primary-glow);
        }
        .tpl-name {
            font-size: 0.82rem;
            font-weight: 600;
            margin-bottom: 0.3rem;
            word-break: break-word;
        }
        .tpl-meta {
            display: flex;
            justify-content: space-between;
            font-size: 0.7rem;
            color: var(--text-muted);
        }

        /* Right Panel Layout */
        .workspace {
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }

        /* Room Geometry Inspector */
        .rooms-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 1rem;
        }
        .room-card {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1rem;
            display: flex;
            flex-direction: column;
            gap: 0.65rem;
            transition: all 0.2s;
        }
        .room-card:hover { border-color: rgba(255, 255, 255, 0.2); }
        .room-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .room-name { font-size: 0.95rem; font-weight: 700; color: #e0e7ff; }
        .room-area {
            background: rgba(6, 182, 212, 0.15);
            color: var(--accent);
            padding: 0.2rem 0.5rem;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 700;
            font-family: var(--font-code);
        }
        .style-pill {
            background: rgba(79, 70, 229, 0.2);
            border: 1px solid rgba(79, 70, 229, 0.4);
            color: #c7d2fe;
            padding: 0.35rem 0.6rem;
            border-radius: 8px;
            font-size: 0.75rem;
            display: flex;
            justify-content: space-between;
        }
        .fallback-tags {
            display: flex;
            gap: 0.35rem;
            flex-wrap: wrap;
            align-items: center;
            font-size: 0.7rem;
            color: var(--text-muted);
        }
        .tag {
            background: rgba(255, 255, 255, 0.06);
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
            color: #cbd5e1;
        }

        .room-card.selected {
            border-color: var(--accent);
            box-shadow: 0 0 16px rgba(6, 182, 212, 0.35);
            background: rgba(6, 182, 212, 0.08);
            transform: translateY(-2px);
        }

        /* Canvas & Polygon Viewer */
        .preview-box {
            background: #090d16;
            border: 1px solid var(--border);
            border-radius: 14px;
            height: 380px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            position: relative;
            cursor: grab;
            user-select: none;
        }
        .preview-box:active { cursor: grabbing; }

        .preview-floating-toolbar {
            position: absolute;
            top: 12px;
            left: 12px;
            display: flex;
            gap: 0.35rem;
            background: rgba(15, 23, 42, 0.88);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border);
            padding: 0.35rem 0.5rem;
            border-radius: 10px;
            z-index: 10;
        }
        .preview-tool-btn {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #94a3b8;
            padding: 0.25rem 0.55rem;
            border-radius: 6px;
            font-size: 0.72rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s;
        }
        .preview-tool-btn:hover { background: rgba(255, 255, 255, 0.12); color: white; }
        .preview-tool-btn.active {
            background: rgba(79, 70, 229, 0.35);
            border-color: #6366f1;
            color: #ffffff;
            box-shadow: 0 0 8px var(--primary-glow);
        }

        .preview-compass {
            position: absolute;
            top: 12px;
            right: 12px;
            width: 38px;
            height: 38px;
            background: rgba(15, 23, 42, 0.88);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.75rem;
            font-weight: 800;
            color: #ef4444;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
            z-index: 10;
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .preview-zoom-controls {
            position: absolute;
            bottom: 12px;
            right: 12px;
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
            background: rgba(15, 23, 42, 0.88);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border);
            padding: 0.3rem;
            border-radius: 8px;
            z-index: 10;
        }
        .preview-zoom-btn {
            width: 28px;
            height: 28px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #cbd5e1;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            font-size: 0.85rem;
            font-weight: 700;
            transition: all 0.15s;
        }
        .preview-zoom-btn:hover {
            background: rgba(255, 255, 255, 0.18);
            color: white;
        }

        .nav-btn-visualizer {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            background: rgba(99, 102, 241, 0.15);
            border: 1px solid rgba(99, 102, 241, 0.35);
            color: #c7d2fe;
            padding: 0.42rem 0.95rem;
            border-radius: 9999px;
            font-size: 0.78rem;
            font-weight: 700;
            text-decoration: none;
            transition: all 0.2s;
            cursor: pointer;
        }
        .nav-btn-visualizer:hover {
            background: var(--primary);
            color: white;
            box-shadow: 0 0 15px var(--primary-glow);
            transform: translateY(-1px);
        }

        .btn-open-visualizer {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.25), rgba(6, 182, 212, 0.25));
            border: 1px solid rgba(99, 102, 241, 0.45);
            color: #e0e7ff;
            padding: 0.38rem 0.85rem;
            border-radius: 8px;
            font-size: 0.78rem;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 0.4rem;
            transition: all 0.2s;
        }
        .btn-open-visualizer:hover {
            background: linear-gradient(135deg, #4f46e5, #06b6d4);
            color: white;
            box-shadow: 0 0 15px var(--primary-glow);
            transform: translateY(-1px);
        }

        /* Modal for 2D Visualizer */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            background: rgba(0, 0, 0, 0.85);
            backdrop-filter: blur(8px);
            z-index: 9999;
            display: none;
            align-items: center;
            justify-content: center;
            padding: 1.5rem;
        }
        .modal-overlay.active { display: flex; }
        .modal-window {
            background: #090d16;
            border: 1px solid rgba(255, 255, 255, 0.15);
            border-radius: 16px;
            width: 95vw;
            height: 92vh;
            display: flex;
            flex-direction: column;
            box-shadow: 0 25px 60px rgba(0, 0, 0, 0.7);
            overflow: hidden;
        }
        .modal-top-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.85rem 1.5rem;
            background: #0f172a;
            border-bottom: 1px solid var(--border);
        }
        .modal-title-wrap {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        .modal-title {
            font-size: 1rem;
            font-weight: 700;
            color: #ffffff;
        }
        .modal-iframe {
            flex: 1;
            width: 100%;
            border: none;
            background: #090d16;
        }
        .modal-close-btn {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.15);
            color: #94a3b8;
            width: 32px;
            height: 32px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.1rem;
            cursor: pointer;
            transition: all 0.2s;
        }
        .modal-close-btn:hover {
            background: #ef4444;
            color: white;
            border-color: #ef4444;
        }

        svg.floorplan-svg {
            width: 100%;
            height: 100%;
        }

        /* Control Panel */
        .controls-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border);
            padding: 0.85rem 1.25rem;
            border-radius: 12px;
            flex-wrap: wrap;
        }
        .controls-group {
            display: flex;
            align-items: center;
            gap: 1.25rem;
        }
        .ctrl-item {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.8rem;
            color: var(--text-muted);
        }
        .btn-run {
            background: linear-gradient(135deg, var(--primary), #6366f1);
            color: white;
            border: none;
            padding: 0.7rem 1.4rem;
            border-radius: 10px;
            font-size: 0.85rem;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            transition: all 0.2s;
            box-shadow: 0 4px 14px var(--primary-glow);
        }
        .btn-run:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px var(--primary-glow);
        }
        .btn-run:disabled { opacity: 0.5; cursor: not-allowed; }

        /* Results & Table */
        .tabs-header {
            display: flex;
            gap: 1rem;
            border-bottom: 1px solid var(--border);
            padding-bottom: 0.5rem;
        }
        .tab-nav {
            font-size: 0.85rem;
            font-weight: 700;
            color: var(--text-muted);
            cursor: pointer;
            padding: 0.3rem 0;
            position: relative;
        }
        .tab-nav.active { color: var(--text); }
        .tab-nav.active::after {
            content: '';
            position: absolute;
            bottom: -0.55rem;
            left: 0;
            right: 0;
            height: 2px;
            background: var(--primary-light);
        }

        .data-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8rem;
            margin-top: 0.5rem;
        }
        .data-table th, .data-table td {
            padding: 0.65rem 0.85rem;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }
        .data-table th {
            color: var(--text-muted);
            font-weight: 600;
            background: rgba(0, 0, 0, 0.2);
        }
        .data-table tr:hover { background: rgba(255, 255, 255, 0.02); }

        .badge-status {
            display: inline-flex;
            align-items: center;
            padding: 0.2rem 0.55rem;
            border-radius: 6px;
            font-size: 0.72rem;
            font-weight: 700;
        }
        .badge-200 { background: rgba(16, 185, 129, 0.15); color: #34d399; }
        .badge-400 { background: rgba(245, 158, 11, 0.15); color: #fbbf24; }
        .badge-500 { background: rgba(239, 68, 68, 0.15); color: #f87171; }

        /* Terminal Console */
        .terminal-box {
            background: #06090e;
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 12px;
            padding: 0.85rem;
            font-family: var(--font-code);
            font-size: 0.75rem;
            line-height: 1.5;
            color: #a7f3d0;
            max-height: 180px;
            overflow-y: auto;
            white-space: pre-wrap;
        }

        /* SVG Legend & Openings */
        .svg-legend {
            position: absolute;
            top: 10px;
            left: 14px;
            display: flex;
            gap: 0.85rem;
            align-items: center;
            font-size: 0.73rem;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(8px);
            border: 1px solid var(--border);
            padding: 0.35rem 0.75rem;
            border-radius: 8px;
            z-index: 10;
        }
        .legend-item {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            color: var(--text-muted);
            font-weight: 600;
        }
        .legend-dot {
            width: 9px;
            height: 9px;
            border-radius: 2px;
        }

        /* DB Sub-Tabs & Reason Column */
        .sub-filter-bar {
            display: flex;
            gap: 0.6rem;
            margin-bottom: 0.75rem;
        }
        .sub-filter-btn {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border);
            color: var(--text-muted);
            padding: 0.35rem 0.75rem;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s;
        }
        .sub-filter-btn:hover { background: rgba(255, 255, 255, 0.1); color: white; }
        .sub-filter-btn.active {
            background: rgba(79, 70, 229, 0.3);
            border-color: var(--primary);
            color: #ffffff;
            box-shadow: 0 0 10px var(--primary-glow);
        }
        .reason-box {
            background: rgba(255, 255, 255, 0.03);
            border-left: 3px solid var(--accent);
            padding: 0.35rem 0.6rem;
            border-radius: 4px;
            font-size: 0.73rem;
            line-height: 1.35;
            color: #cbd5e1;
            max-width: 320px;
            word-break: break-word;
        }
        .reason-box.error {
            border-left-color: var(--danger);
            background: rgba(239, 68, 68, 0.08);
            color: #fca5a5;
        }
        .reason-box.success {
            border-left-color: #10b981;
            background: rgba(16, 185, 129, 0.08);
            color: #a7f3d0;
        }
    </style>
</head>
<body>

    <header>
        <div class="brand">
            <div class="brand-icon">✨</div>
            <div class="brand-text">
                <h1>Smart Wizard Inspiration Dashboard</h1>
                <span>Step 1 Room JSON & AI Inspiration Testing Suite</span>
            </div>
        </div>
        <div style="display: flex; align-items: center; gap: 0.85rem;">
            <button class="nav-btn-visualizer" onclick="openVisualizerModal()" title="Open Smart Wizard 2D Visualizer">
                <span>📐 Smart Wizard 2D Visualizer</span>
            </button>
            <div class="db-badge" id="dbStatus">
                <div class="db-pulse"></div>
                <span id="dbName">Connecting to MySQL...</span>
            </div>
        </div>
    </header>

    <main>
        <!-- Step 2: Azure Template Selector Panel -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">📁 Azure Floorplans</div>
                <div class="card-counter" id="tplCount">0 templates</div>
            </div>

            <!-- Azure Container Switcher -->
            <div style="display: flex; flex-direction: column; gap: 0.35rem; padding: 0.65rem 0.85rem; border: 1px solid var(--border); background: rgba(0, 0, 0, 0.22); border-radius: 10px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-size: 0.72rem; font-weight: 700; color: var(--accent); display: flex; align-items: center; gap: 0.35rem; text-transform: uppercase; letter-spacing: 0.05em;">
                        ☁️ Storage Container
                    </span>
                    <span id="containerBadge" style="font-size: 0.65rem; color: var(--text-muted); font-family: var(--font-code);">prod-smartwizardtemplates</span>
                </div>
                <select id="containerSelect" onchange="switchContainer(this.value)" style="width: 100%; background: rgba(18, 24, 38, 0.95); color: #fff; border: 1px solid rgba(79, 70, 229, 0.5); border-radius: 6px; padding: 0.42rem 0.65rem; font-size: 0.78rem; font-family: var(--font-code); font-weight: 600; cursor: pointer; outline: none; transition: border-color 0.2s;">
                    <option value="prod-smartwizardtemplates" selected>prod-smartwizardtemplates (Primary)</option>
                    <option value="dev-smartwizardtemplates">dev-smartwizardtemplates</option>
                </select>
            </div>

            <div class="category-tabs" id="catTabs">
                <button class="cat-btn active" onclick="filterCategory('')">All</button>
                <button class="cat-btn" onclick="filterCategory('1BHK')">1BHK</button>
                <button class="cat-btn" onclick="filterCategory('2BHK')">2BHK</button>
                <button class="cat-btn" onclick="filterCategory('3BHK')">3BHK</button>
                <button class="cat-btn" onclick="filterCategory('4BHK')">4BHK</button>
            </div>

            <div class="search-box">
                <input type="text" class="search-input" id="searchTpl" placeholder="Search templates (e.g. southface, 853sqft)..." oninput="searchTemplates()">
            </div>

            <!-- Direct Floorplan JSON Upload (No Blob/Flags Required) -->
            <div style="padding: 0.65rem 0.85rem; border: 1px solid var(--border); background: rgba(79, 70, 229, 0.08); border-radius: 8px; margin: 0 1rem 0.6rem 1rem;">
                <div style="font-size: 0.78rem; font-weight: 700; color: var(--primary-light); margin-bottom: 0.35rem; display: flex; align-items: center; justify-content: space-between;">
                    <span>📤 Direct JSON Upload</span>
                    <span style="font-size: 0.65rem; color: var(--text-muted);">No blob/flags needed</span>
                </div>
                <label for="directJsonUpload" style="display: block; text-align: center; padding: 0.55rem; border: 1.5px dashed var(--primary); border-radius: 6px; cursor: pointer; font-size: 0.74rem; background: rgba(18, 24, 38, 0.6); color: var(--text); transition: all 0.2s ease;">
                    <span id="uploadLabelText">📁 Click to Upload Floorplan JSON</span>
                    <input type="file" id="directJsonUpload" accept=".json" style="display: none;" onchange="handleDirectJsonUpload(event)">
                </label>
            </div>

            <div class="template-list" id="templateList">
                <div style="padding: 2rem; text-align: center; color: var(--text-muted); font-size: 0.8rem;">Loading templates from Azure Blob Storage...</div>
            </div>
        </div>

        <!-- Right Workspace: Geometry Preview, Controller & Results -->
        <div class="workspace">
            
            <!-- Step 3: Room Geometry & Style Preview -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <span>📐 Room Geometry & Floorplan Preview</span>
                    </div>
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <span style="font-size: 0.8rem; color: var(--text-muted);" id="activeTplName">Select a template on the left</span>
                        <button class="btn-open-visualizer" onclick="openVisualizerModal()" title="Open dedicated 2D CAD floorplan visualizer with zoom, pan, walls, doors, windows & JSON tree">
                            <span>🔍 Open Full 2D Visualizer</span>
                        </button>
                        <a id="btnExtVisualizer" href="/visualizer" target="_blank" class="preview-zoom-btn" title="Open in New Tab" style="text-decoration:none;">↗</a>
                    </div>
                </div>

                <div class="preview-box" id="svgContainer">
                    <div class="preview-floating-toolbar" id="previewToolbar" style="display: none;">
                        <button class="preview-tool-btn active" id="pBtnRooms" onclick="togglePreviewLayer('rooms')">🟦 Rooms</button>
                        <button class="preview-tool-btn active" id="pBtnWalls" onclick="togglePreviewLayer('walls')">🧱 Walls</button>
                        <button class="preview-tool-btn active" id="pBtnDoors" onclick="togglePreviewLayer('doors')">🚪 Doors</button>
                        <button class="preview-tool-btn active" id="pBtnWindows" onclick="togglePreviewLayer('windows')">🪟 Windows</button>
                        <button class="preview-tool-btn" id="pBtnDims" onclick="togglePreviewLayer('dims')">📏 Dims</button>
                    </div>

                    <div class="preview-compass" id="previewCompass" title="North Orientation" style="display: none;">
                        <span>N ↑</span>
                    </div>

                    <div class="preview-zoom-controls" id="previewZoomControls" style="display: none;">
                        <button class="preview-zoom-btn" onclick="previewZoomIn()" title="Zoom In">+</button>
                        <button class="preview-zoom-btn" onclick="previewZoomOut()" title="Zoom Out">−</button>
                        <button class="preview-zoom-btn" onclick="previewResetView()" title="Fit to Center">⛶</button>
                    </div>

                    <svg id="previewSvg" class="floorplan-svg" style="display: none;">
                        <g id="previewTransformGroup">
                            <g id="pWallsGroup"></g>
                            <g id="pRoomsGroup"></g>
                            <g id="pOpeningsGroup"></g>
                            <g id="pDimsGroup" style="display: none;"></g>
                        </g>
                    </svg>

                    <div id="previewPlaceholder" style="color: var(--text-muted); font-size: 0.85rem;">
                        Click any template to inspect floorplan polygon boundaries
                    </div>
                </div>

                <div class="rooms-grid" id="roomsGrid">
                    <!-- Populated dynamically with rooms, areas, and fallback types -->
                </div>

                <!-- Step 4: Execution Controller -->
                <div class="controls-bar">
                    <div class="controls-group">
                        <label class="ctrl-item">
                            <input type="checkbox" id="chkCrossRoom" checked>
                            <span>Enable Cross-Room Fallback (Kitchen ➔ Bathroom on collision)</span>
                        </label>
                        <div class="ctrl-item">
                            <span>Max Retries:</span>
                            <select id="selRetries" style="background: rgba(0,0,0,0.4); color: white; border: 1px solid var(--border); border-radius: 6px; padding: 0.2rem 0.4rem;">
                                <option value="1">1</option>
                                <option value="2">2</option>
                                <option value="3" selected>3</option>
                                <option value="5">5</option>
                            </select>
                        </div>
                    </div>
                    <div style="display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap;">
                        <button class="btn-run" id="btnRun" onclick="runInspirationTest()" disabled>
                            <span>🚀 Run Selected Floorplan</span>
                        </button>
                        <button class="btn-run" id="btnAutomateBhk" onclick="runBhkBatchAutomation()" style="background: linear-gradient(135deg, #059669, #10b981);">
                            <span>⚡ Automate Full BHK Category</span>
                        </button>
                    </div>
                </div>
            </div>

            <!-- Step 5: Results Visualization & MySQL Dashboard -->
            <div class="card">
                <div class="tabs-header">
                    <div class="tab-nav active" onclick="switchTab('resultsTab', this)">🎯 Placed 3D Furniture & Room Mappings</div>
                    <div class="tab-nav" onclick="switchTab('mysqlTab', this)">🗄️ MySQL Database History</div>
                    <div class="tab-nav" onclick="switchTab('logsTab', this)">📜 Live Terminal Logs</div>
                </div>

                <!-- Tab 1: Placed Items & Room Inspiration Mapping -->
                <div id="resultsTab">
                    <table class="data-table" id="furnitureTable">
                        <thead>
                            <tr>
                                <th>Smart Wizard Room</th>
                                <th>Nominal</th>
                                <th>Applied Inspiration</th>
                                <th>Style</th>
                                <th>Status</th>
                                <th>Retries</th>
                                <th>Latency</th>
                                <th>Saved Folder</th>
                                <th>Placed 3D Items Details</th>
                            </tr>
                        </thead>
                        <tbody id="furnitureBody">
                            <tr><td colspan="9" style="text-align: center; color: var(--text-muted);">Run an inspiration test above to visualize placed 3D furniture models and room mappings.</td></tr>
                        </tbody>
                    </table>
                </div>

                <!-- Tab 2: MySQL History -->
                <div id="mysqlTab" style="display: none;">
                    <div class="sub-filter-bar">
                        <button class="sub-filter-btn active" id="btnShowResults" onclick="showDbSubTab('results')">📊 All Executions (api_results)</button>
                        <button class="sub-filter-btn" id="btnShowErrors" onclick="showDbSubTab('errors')">⚠️ Errors & Retries (api_errors)</button>
                    </div>

                    <!-- Sub-view 1: Executions with Reason, Input URL & Path -->
                    <div id="dbResultsView">
                        <table class="data-table" id="dbResultsTable">
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>Timestamp</th>
                                    <th>Room</th>
                                    <th>Type</th>
                                    <th>Style</th>
                                    <th>Status</th>
                                    <th>Latency</th>
                                    <th>Items</th>
                                    <th>Input Path</th>
                                    <th>Input URL</th>
                                    <th>Reason / Execution Details</th>
                                </tr>
                            </thead>
                            <tbody id="dbResultsBody">
                                <tr><td colspan="11" style="text-align: center; color: var(--text-muted);">Loading database records from MySQL...</td></tr>
                            </tbody>
                        </table>
                    </div>

                    <!-- Sub-view 2: Errors with Reason, Input URL & Path -->
                    <div id="dbErrorsView" style="display: none;">
                        <table class="data-table" id="dbErrorsTable">
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>Timestamp</th>
                                    <th>Room</th>
                                    <th>Style ID</th>
                                    <th>HTTP Status</th>
                                    <th>Attempt #</th>
                                    <th>Input Path</th>
                                    <th>Input URL</th>
                                    <th>Failure Reason (error_message)</th>
                                </tr>
                            </thead>
                            <tbody id="dbErrorsBody">
                                <tr><td colspan="9" style="text-align: center; color: var(--text-muted);">Loading error records from MySQL...</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- Tab 3: Terminal Logs -->
                <div id="logsTab" style="display: none;">
                    <div class="terminal-box" id="terminalBox">Connecting to real-time execution log stream...</div>
                </div>
            </div>

        </div>
    </main>

    <!-- Dedicated 2D CAD Visualizer Modal -->
    <div class="modal-overlay" id="visualizerModal">
        <div class="modal-window">
            <div class="modal-top-bar">
                <div class="modal-title-wrap">
                    <span style="font-size: 1.25rem;">📐</span>
                    <div>
                        <div class="modal-title" id="modalTplTitle">Smart Wizard 2D CAD Visualizer</div>
                        <div style="font-size: 0.72rem; color: var(--text-muted);" id="modalTplSubtitle">Interactive Architectural Floorplan Inspector</div>
                    </div>
                </div>
                <div style="display: flex; align-items: center; gap: 0.65rem;">
                    <a id="modalNewTabLink" href="/visualizer" target="_blank" class="sub-filter-btn" style="text-decoration:none;" title="Open in dedicated browser window">
                        <span>↗ Open in New Window</span>
                    </a>
                    <button class="modal-close-btn" onclick="closeVisualizerModal()" title="Close (Esc)">✕</button>
                </div>
            </div>
            <iframe id="visualizerIframe" class="modal-iframe" src="/visualizer"></iframe>
        </div>
    </div>

    <script>
        let allTemplates = [];
        let selectedBlob = "";
        let currentCategory = "";
        let selectedContainer = "prod-smartwizardtemplates";

        // Interactive Preview Pan & Zoom State
        let pScale = 1.0;
        let pPanX = 0;
        let pPanY = 0;
        let pIsPanning = false;
        let pStartX = 0;
        let pStartY = 0;
        const pVisibility = { rooms: true, walls: true, doors: true, windows: true, dims: false };

        // On Page Load
        window.addEventListener('DOMContentLoaded', () => {
            initPreviewPanZoom();
            loadTemplates();
            loadHistory();
            startLogPolling();
        });

        // Switch Container
        async function switchContainer(containerName) {
            selectedContainer = containerName;
            selectedBlob = "";
            currentCategory = "";
            document.getElementById('searchTpl').value = "";
            const badge = document.getElementById('containerBadge');
            if (badge) badge.innerText = containerName;
            document.getElementById('templateList').innerHTML = `<div style="padding: 2rem; text-align: center; color: var(--accent); font-size: 0.8rem;">Loading templates from container <strong>${containerName}</strong>...</div>`;
            document.getElementById('activeTplName').innerText = `Container: ${containerName} (Select a template)`;
            await loadTemplates();
        }

        // 1. Fetch Templates from Azure Blob Storage
        async function loadTemplates() {
            try {
                const res = await fetch(`/api/templates?container=${encodeURIComponent(selectedContainer)}`);
                const data = await res.json();
                allTemplates = data.templates;
                document.getElementById('tplCount').innerText = `${allTemplates.length} templates`;
                
                // Update dynamic categories from server response
                if (data.categories && data.categories.length > 0) {
                    renderCategoryTabs(data.categories);
                }

                renderTemplates(allTemplates);
                
                // Auto-select first template for convenience
                if (allTemplates.length > 0) {
                    selectTemplate(allTemplates[0].name);
                } else {
                    document.getElementById('roomsGrid').innerHTML = '<div style="color: var(--warning); padding: 1rem;">No templates found in this container.</div>';
                    document.getElementById('activeTplName').innerText = 'No templates in this container';
                    document.getElementById('svgContainer').innerHTML = '<div style="color: var(--text-muted); text-align: center; padding-top: 35%;">No floorplan to display</div>';
                }
            } catch (err) {
                console.error(err);
                document.getElementById('templateList').innerHTML = `<div style="color: var(--danger); padding: 1rem;">Failed to load Azure templates: ${err.message}</div>`;
            }
        }

        function renderCategoryTabs(categories) {
            const catTabs = document.getElementById('catTabs');
            if (!catTabs) return;
            const primaryCats = ['1BHK', '2BHK', '3BHK', '4BHK'];
            const otherCats = categories.filter(c => !primaryCats.includes(c)).sort();
            const displayCats = [...primaryCats.filter(c => categories.includes(c)), ...otherCats];
            
            let html = `<button class="cat-btn ${currentCategory === '' ? 'active' : ''}" onclick="filterCategory('')">All</button>`;
            displayCats.forEach(c => {
                html += `<button class="cat-btn ${currentCategory === c ? 'active' : ''}" onclick="filterCategory('${c}')">${c}</button>`;
            });
            catTabs.innerHTML = html;
        }

        function filterCategory(cat) {
            currentCategory = cat;
            document.querySelectorAll('.cat-btn').forEach(btn => {
                btn.classList.toggle('active', btn.innerText.toLowerCase() === (cat || 'all').toLowerCase());
            });
            applyFilters();
        }

        function searchTemplates() {
            applyFilters();
        }

        function applyFilters() {
            const query = document.getElementById('searchTpl').value.toLowerCase();
            const filtered = allTemplates.filter(t => {
                const matchCat = !currentCategory || t.category.toLowerCase() === currentCategory.toLowerCase();
                const matchQuery = !query || t.name.toLowerCase().includes(query);
                return matchCat && matchQuery;
            });
            renderTemplates(filtered);
        }

        function renderTemplates(list) {
            const container = document.getElementById('templateList');
            if (list.length === 0) {
                container.innerHTML = '<div style="padding: 2rem; text-align: center; color: var(--text-muted); font-size: 0.8rem;">No templates found matching filters.</div>';
                return;
            }
            container.innerHTML = list.map(t => `
                <div class="tpl-item ${t.name === selectedBlob ? 'selected' : ''}" onclick="selectTemplate('${t.name}')">
                    <div class="tpl-name">${t.filename}</div>
                    <div class="tpl-meta">
                        <span>🏷️ ${t.category}</span>
                        <span>📦 ${t.size_kb} KB</span>
                    </div>
                </div>
            `).join('');
        }

        // 2. Select Template & Load Room Geometry
        async function selectTemplate(blobName) {
            selectedBlob = blobName;
            document.getElementById('activeTplName').innerText = blobName;
            const extLink = document.getElementById('btnExtVisualizer');
            if (extLink) extLink.href = `/visualizer?blob=${encodeURIComponent(blobName)}&container=${encodeURIComponent(selectedContainer)}`;

            document.querySelectorAll('.tpl-item').forEach(el => {
                el.classList.toggle('selected', el.querySelector('.tpl-name').innerText === blobName.split('/').pop());
            });

            document.getElementById('btnRun').disabled = true;
            document.getElementById('roomsGrid').innerHTML = '<div style="grid-column: 1/-1; color: var(--text-muted); padding: 1rem;">Parsing room geometry & Shoelace areas from Azure Blob...</div>';

            try {
                const res = await fetch(`/api/template/rooms?blob=${encodeURIComponent(blobName)}&container=${encodeURIComponent(selectedContainer)}`);
                const data = await res.json();
                renderRooms(data.rooms);
                drawFloorplanSVG(data);
                document.getElementById('btnRun').disabled = false;
            } catch (err) {
                console.error(err);
                document.getElementById('roomsGrid').innerHTML = `<div style="color: var(--danger); padding: 1rem;">Error parsing room geometry: ${err.message}</div>`;
            }
        }

        // Render Room Geometry Cards
        function renderRooms(rooms) {
            const container = document.getElementById('roomsGrid');
            if (!rooms || rooms.length === 0) {
                container.innerHTML = '<div style="color: var(--warning); padding: 1rem;">No valid room areas found in this template.</div>';
                return;
            }

            container.innerHTML = rooms.map(r => `
                <div class="room-card" id="card_room_${r.area_id}" onclick="highlightRoomOnPreview('${r.area_id}')" style="cursor: pointer;" title="Click to highlight on 2D floorplan">
                    <div class="room-top">
                        <span class="room-name">${r.name} <small style="color: var(--text-muted); font-weight: 400;">(${r.room_type})</small></span>
                        <span class="room-area">${r.area_m2} m² (${r.area_sqft} sqft)</span>
                    </div>
                    <div class="style-pill">
                        <span>⭐ Rec. Style:</span>
                        <strong style="color: white;">${r.recommended_style ? r.recommended_style.name + ' (' + r.recommended_style.style_id + ')' : 'Default'}</strong>
                    </div>
                    <div class="fallback-tags">
                        <span>Fallback Types:</span>
                        ${r.fallback_room_types.map(rt => `<span class="tag">${rt}</span>`).join('') || '<span class="tag">None</span>'}
                    </div>
                </div>
            `).join('');
        }

        // Interactive Pan & Zoom for Inline Preview
        function initPreviewPanZoom() {
            const box = document.getElementById('svgContainer');
            box.addEventListener('mousedown', (e) => {
                if (e.target.closest('.preview-floating-toolbar') || e.target.closest('.preview-zoom-controls') || e.target.closest('.preview-compass')) return;
                pIsPanning = true;
                pStartX = e.clientX - pPanX;
                pStartY = e.clientY - pPanY;
            });
            window.addEventListener('mousemove', (e) => {
                if (!pIsPanning) return;
                pPanX = e.clientX - pStartX;
                pPanY = e.clientY - pStartY;
                updatePreviewTransform();
            });
            window.addEventListener('mouseup', () => { pIsPanning = false; });
            box.addEventListener('wheel', (e) => {
                e.preventDefault();
                const factor = e.deltaY < 0 ? 1.15 : 0.87;
                pScale = Math.max(0.2, Math.min(6.0, pScale * factor));
                updatePreviewTransform();
            }, { passive: false });
        }

        function updatePreviewTransform() {
            const grp = document.getElementById('previewTransformGroup');
            if (grp) grp.setAttribute('transform', `translate(${pPanX}, ${pPanY}) scale(${pScale})`);
        }

        function previewZoomIn() {
            pScale = Math.min(pScale * 1.25, 6.0);
            updatePreviewTransform();
        }
        function previewZoomOut() {
            pScale = Math.max(pScale / 1.25, 0.2);
            updatePreviewTransform();
        }
        function previewResetView() {
            pScale = 1.0;
            pPanX = 0;
            pPanY = 0;
            updatePreviewTransform();
        }

        function togglePreviewLayer(type) {
            pVisibility[type] = !pVisibility[type];
            const btn = document.getElementById('pBtn' + type.charAt(0).toUpperCase() + type.slice(1));
            if (btn) btn.classList.toggle('active', pVisibility[type]);
            if (type === 'rooms') document.getElementById('pRoomsGroup').style.display = pVisibility.rooms ? 'inline' : 'none';
            if (type === 'walls') document.getElementById('pWallsGroup').style.display = pVisibility.walls ? 'inline' : 'none';
            if (type === 'doors') document.querySelectorAll('.door-elem').forEach(el => el.style.display = pVisibility.doors ? 'inline' : 'none');
            if (type === 'windows') document.querySelectorAll('.window-elem').forEach(el => el.style.display = pVisibility.windows ? 'inline' : 'none');
            if (type === 'dims') document.getElementById('pDimsGroup').style.display = pVisibility.dims ? 'inline' : 'none';
        }

        // Room Highlight synchronization between Cards & SVG
        function highlightRoomOnPreview(roomId) {
            document.querySelectorAll('.room-card').forEach(c => c.classList.remove('selected'));
            const card = document.getElementById('card_room_' + roomId);
            if (card) {
                card.classList.add('selected');
                card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
            document.querySelectorAll('#pRoomsGroup polygon').forEach(p => p.setAttribute('fill-opacity', '0.22'));
            const poly = document.querySelector('#preview_poly_' + roomId);
            if (poly) {
                poly.setAttribute('fill-opacity', '0.70');
            }
        }

        // Modal Controls
        function openVisualizerModal() {
            if (!selectedBlob) return;
            document.getElementById('modalTplTitle').innerText = 'Smart Wizard: ' + selectedBlob.split('/').pop();
            document.getElementById('modalTplSubtitle').innerText = selectedBlob;
            const iframe = document.getElementById('visualizerIframe');
            iframe.src = `/visualizer?blob=${encodeURIComponent(selectedBlob)}&container=${encodeURIComponent(selectedContainer)}`;
            document.getElementById('modalNewTabLink').href = `/visualizer?blob=${encodeURIComponent(selectedBlob)}&container=${encodeURIComponent(selectedContainer)}`;
            document.getElementById('visualizerModal').classList.add('active');
        }

        function closeVisualizerModal() {
            document.getElementById('visualizerModal').classList.remove('active');
        }

        window.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeVisualizerModal();
        });

        // Draw Interactive SVG Room Polygons with Walls, Doors, and Windows
        function drawFloorplanSVG(data) {
            const svg = document.getElementById('previewSvg');
            const placeholder = document.getElementById('previewPlaceholder');
            const toolbar = document.getElementById('previewToolbar');
            const zoomCtrls = document.getElementById('previewZoomControls');
            const compass = document.getElementById('previewCompass');

            const rooms = data.rooms || [];
            const walls = data.walls || [];
            const openings = data.openings || [];

            if (!rooms || rooms.length === 0) {
                placeholder.style.display = 'block';
                placeholder.innerText = 'No valid room geometry found in this template';
                svg.style.display = 'none';
                toolbar.style.display = 'none';
                zoomCtrls.style.display = 'none';
                compass.style.display = 'none';
                return;
            }

            placeholder.style.display = 'none';
            svg.style.display = 'block';
            toolbar.style.display = 'flex';
            zoomCtrls.style.display = 'flex';
            compass.style.display = 'flex';
            compass.style.transform = `rotate(${data.direction_angle || 0}deg)`;

            // Update button counters
            document.getElementById('pBtnDoors').innerText = `🚪 Doors (${data.doors_count || 0})`;
            document.getElementById('pBtnWindows').innerText = `🪟 Windows (${data.windows_count || 0})`;

            let allPoints = [];
            rooms.forEach(r => (r.polygon || []).forEach(p => allPoints.push(p)));
            walls.forEach(w => {
                allPoints.push({ x: w.x1, y: w.y1 });
                allPoints.push({ x: w.x2, y: w.y2 });
            });
            if (allPoints.length === 0) {
                openings.forEach(o => allPoints.push({ x: o.cx, y: o.cy }));
            }

            if (allPoints.length === 0) return;

            const minX = Math.min(...allPoints.map(p => p.x));
            const maxX = Math.max(...allPoints.map(p => p.x));
            const minY = Math.min(...allPoints.map(p => p.y));
            const maxY = Math.max(...allPoints.map(p => p.y));

            const width = Math.max(maxX - minX, 100);
            const height = Math.max(maxY - minY, 100);
            const pad = 40;

            svg.setAttribute('viewBox', `${minX - pad} ${minY - pad} ${width + pad * 2} ${height + pad * 2}`);

            const colors = ['#6366f1', '#06b6d4', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6'];

            // 1. Structural Walls
            document.getElementById('pWallsGroup').innerHTML = walls.map(w => `
                <line x1="${w.x1}" y1="${w.y1}" x2="${w.x2}" y2="${w.y2}" stroke="#334155" stroke-width="${Math.max(w.thickness_cm ? w.thickness_cm * 0.4 : 5, 4)}" stroke-linecap="round">
                    <title>Wall: ${w.length_m || ''} m</title>
                </line>
            `).join('');

            // 2. Room Polygons
            document.getElementById('pRoomsGroup').innerHTML = rooms.map((r, i) => {
                const pts = (r.polygon || []).map(p => `${p.x},${p.y}`).join(' ');
                const c = colors[i % colors.length];
                const cx = (r.polygon || []).reduce((acc, p) => acc + p.x, 0) / (r.polygon.length || 1);
                const cy = (r.polygon || []).reduce((acc, p) => acc + p.y, 0) / (r.polygon.length || 1);
                return `
                    <g class="room-poly-group" onclick="highlightRoomOnPreview('${r.area_id}')" style="cursor: pointer;">
                        <polygon id="preview_poly_${r.area_id}" points="${pts}" fill="${c}" fill-opacity="0.22" stroke="${c}" stroke-width="2.5" />
                        <text x="${cx}" y="${cy - 7}" fill="#ffffff" font-size="15" font-weight="700" text-anchor="middle" pointer-events="none">${r.name}</text>
                        <text x="${cx}" y="${cy + 13}" fill="#94a3b8" font-size="11" font-weight="600" text-anchor="middle" pointer-events="none">${r.area_m2} m² (${r.area_sqft} sqft)</text>
                    </g>
                `;
            }).join('');

            // 3. Openings (Doors & Windows)
            document.getElementById('pOpeningsGroup').innerHTML = openings.map(o => {
                if (o.type === 'door') {
                    const jamb = `<line x1="${o.p0.x}" y1="${o.p0.y}" x2="${o.p1.x}" y2="${o.p1.y}" stroke="#f59e0b" stroke-width="5" stroke-linecap="square" />`;
                    const leaf = `<line x1="${o.p0.x}" y1="${o.p0.y}" x2="${o.leaf.x}" y2="${o.leaf.y}" stroke="#fbbf24" stroke-width="2.6" stroke-linecap="round" />`;
                    const swingArc = `<path d="M ${o.leaf.x} ${o.leaf.y} A ${o.width} ${o.width} 0 0 1 ${o.p1.x} ${o.p1.y}" fill="rgba(245, 158, 11, 0.12)" stroke="#f59e0b" stroke-width="1.6" stroke-dasharray="3,3" />`;
                    const badge = `<text x="${o.cx}" y="${o.cy}" fill="#fde68a" font-size="12" text-anchor="middle" dominant-baseline="central">🚪</text><title>${o.name} (${o.width} cm)</title>`;
                    return `<g class="door-elem">${swingArc}${jamb}${leaf}${badge}</g>`;
                } else if (o.type === 'window') {
                    const frame = `<line x1="${o.p0.x}" y1="${o.p0.y}" x2="${o.p1.x}" y2="${o.p1.y}" stroke="#06b6d4" stroke-width="6" stroke-linecap="square" />`;
                    const glass = `<line x1="${o.p0.x}" y1="${o.p0.y}" x2="${o.p1.x}" y2="${o.p1.y}" stroke="#e0f2fe" stroke-width="2" stroke-linecap="square" />`;
                    const badge = `<text x="${o.cx}" y="${o.cy}" fill="#bae6fd" font-size="11" text-anchor="middle" dominant-baseline="central">🪟</text><title>${o.name} (${o.width} cm)</title>`;
                    return `<g class="window-elem">${frame}${glass}${badge}</g>`;
                }
                return '';
            }).join('');

            // Reset zoom/pan position
            pScale = 1.0;
            pPanX = 0;
            pPanY = 0;
            updatePreviewTransform();
        }

        // 2B. Handle Direct Floorplan JSON Upload (No Blob/Flags Needed)
        async function handleDirectJsonUpload(event) {
            const file = event.target.files[0];
            if (!file) return;

            const btn = document.getElementById('btnRun');
            btn.disabled = true;
            btn.innerHTML = '<span>⏳ Uploading & Running Inspiration...</span>';

            const tbody = document.getElementById('furnitureBody');
            tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--accent); padding: 1.5rem;">Uploading <strong>${file.name}</strong> directly and generating inspirations with all default flags...</td></tr>`;

            try {
                const text = await file.text();
                const jsonContent = JSON.parse(text);

                // Update active template label in UI
                document.getElementById('activeTplName').innerText = `Uploaded: ${file.name}`;
                const lbl = document.getElementById('uploadLabelText');
                if (lbl) lbl.innerText = `📄 ${file.name}`;

                // Send raw JSON directly to POST /api/run-inspiration without any flags!
                const res = await fetch('/api/run-inspiration', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(jsonContent)
                });
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(data.detail || 'Server returned an error');
                }
                renderExecutionResults(data.results);
                loadHistory();
            } catch (err) {
                console.error(err);
                tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--danger);">Upload & Execution error: ${err.message}</td></tr>`;
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<span>🚀 Run Selected Floorplan</span>';
                event.target.value = '';
            }
        }

        // 3. Trigger Pipeline Execution (POST /api/run-inspiration)
        async function runInspirationTest() {
            const btn = document.getElementById('btnRun');
            const crossRoom = document.getElementById('chkCrossRoom').checked;
            const retries = parseInt(document.getElementById('selRetries').value, 10);

            btn.disabled = true;
            btn.innerHTML = '<span>⏳ Processing Pipeline & API...</span>';

            const tbody = document.getElementById('furnitureBody');
            tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: var(--accent); padding: 1.5rem;">Dispatching payload(s) to remote Inspiration Engine with mirrored Azure folders & retry enabled...</td></tr>';

            try {
                const res = await fetch('/api/run-inspiration', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        blob: selectedBlob,
                        container: selectedContainer,
                        max_retries: retries,
                        allow_cross_room: crossRoom
                    })
                });
                const data = await res.json();
                renderExecutionResults(data.results);
                loadHistory(); // Refresh MySQL history tab
            } catch (err) {
                console.error(err);
                tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--danger);">Execution error: ${err.message}</td></tr>`;
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<span>🚀 Run Selected Floorplan</span>';
            }
        }

        // 3B. Batch Automate All Floorplans in a BHK Category (POST /api/automate-bhk)
        async function runBhkBatchAutomation() {
            const cat = currentCategory || "1BHK";
            const btn = document.getElementById('btnAutomateBhk');
            const crossRoom = document.getElementById('chkCrossRoom').checked;
            const retries = parseInt(document.getElementById('selRetries').value, 10);

            const limitStr = prompt(`Enter template limit for ${cat} automation (leave blank or 0 for all templates):`, "3");
            if (limitStr === null) return;
            const limit = parseInt(limitStr, 10) || 0;

            btn.disabled = true;
            btn.innerHTML = `<span>⏳ Automating ${cat}...</span>`;

            const tbody = document.getElementById('furnitureBody');
            tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--accent); padding: 1.5rem;">Batch automating ${cat} floorplans from Azure container '${selectedContainer}'. Creating mirrored folders & JSON mappings...</td></tr>`;

            try {
                const res = await fetch('/api/automate-bhk', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        category: cat,
                        container: selectedContainer,
                        limit: limit > 0 ? limit : null,
                        max_retries: retries,
                        allow_cross_room: crossRoom
                    })
                });
                const data = await res.json();
                let flattenedResults = [];
                (data.floorplans || []).forEach(fp => {
                    (fp.room_mappings || []).forEach(rm => {
                        flattenedResults.push({
                            file: rm.payload_file,
                            smart_wizard_room_name: rm.smart_wizard_room_name,
                            smart_wizard_nominal_type: rm.smart_wizard_nominal_type,
                            applied_room_type: rm.applied_inspiration_room_type,
                            room_type: rm.applied_inspiration_room_type,
                            applied_style_id: rm.applied_style_id,
                            style_id: rm.applied_style_id,
                            applied_style_name: rm.applied_style_name,
                            room_type_switched: rm.room_type_switched,
                            status: rm.status,
                            status_code: rm.status_code,
                            retries_used: rm.retries_used,
                            latency: rm.latency,
                            target_folder: fp.relative_folder || fp.target_folder,
                            placed_items: []
                        });
                    });
                });
                renderExecutionResults(flattenedResults);
                loadHistory();
                alert(`✅ Completed ${cat} automation! Processed ${data.total_floorplans} floorplan(s). Folders created and JSONs saved.`);
            } catch (err) {
                console.error(err);
                tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--danger);">BHK Automation Error: ${err.message}</td></tr>`;
            } finally {
                btn.disabled = false;
                btn.innerHTML = `<span>⚡ Automate Full BHK Category</span>`;
            }
        }

        // Render Placed 3D Furniture Items & Room Mappings Table
        function renderExecutionResults(results) {
            const tbody = document.getElementById('furnitureBody');
            if (!results || results.length === 0) {
                tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: var(--text-muted);">No results returned.</td></tr>';
                return;
            }

            tbody.innerHTML = results.map(r => {
                const badgeClass = r.status_code === 200 ? 'badge-200' : (r.status_code >= 500 ? 'badge-500' : 'badge-400');
                const itemsList = r.placed_items && r.placed_items.length > 0 
                    ? r.placed_items.map(it => `• <strong>${it.name}</strong> at (x: ${it.x}, y: ${it.y}, rot: ${it.rotation} rad)`).join('<br>')
                    : (r.error ? `<span style="color: var(--danger);">${r.error}</span>` : (r.status_code === 200 ? `${r.items_count || 0} items placed` : '0 items placed'));

                const swName = r.smart_wizard_room_name || r.file || 'unknown';
                const nomType = r.smart_wizard_nominal_type || r.room_type || '-';
                const appType = r.applied_room_type || r.room_type || '-';
                const switchedBadge = r.room_type_switched ? `<span class="badge-status" style="background:rgba(245,158,11,0.25); color:#f59e0b; font-size:0.65rem; margin-left:0.25rem;">Fallback</span>` : '';
                const styleDisp = r.applied_style_name ? `#${r.applied_style_id || r.style_id} (${r.applied_style_name})` : `#${r.style_id}`;
                const folderDisp = r.target_folder ? `<span style="font-family: var(--font-code); font-size:0.7rem; color: #38bdf8;" title="${r.target_folder}">${r.target_folder.replace(/\\\\/g, '/').split('/').slice(-3).join('/')}</span>` : '-';

                return `
                    <tr>
                        <td><strong>${swName}</strong></td>
                        <td>${nomType}</td>
                        <td><strong>${appType}</strong>${switchedBadge}</td>
                        <td><span class="tag">${styleDisp}</span></td>
                        <td><span class="badge-status ${badgeClass}">${r.status_code || r.status}</span></td>
                        <td>${r.retries_used || 0}</td>
                        <td>${r.latency ? r.latency.toFixed(2) + 's' : '-'}</td>
                        <td>${folderDisp}</td>
                        <td style="font-family: var(--font-code); font-size: 0.72rem; line-height: 1.4;">${itemsList}</td>
                    </tr>
                `;
            }).join('');
        }

        // 4. Query MySQL History & Render Executions & Errors with Reasons
        let dbHistoryCache = { results: [], errors: [] };

        async function loadHistory() {
            try {
                const res = await fetch('/api/history?limit=50');
                const data = await res.json();
                dbHistoryCache = data;
                
                document.getElementById('dbName').innerText = `${data.engine} (${data.total_results} runs, ${data.total_errors} errors)`;
                
                renderDbResults(data.results || []);
                renderDbErrors(data.errors || []);
            } catch (err) {
                console.error(err);
                document.getElementById('dbName').innerText = 'Database Connection Offline';
            }
        }

        function showDbSubTab(tab) {
            document.getElementById('btnShowResults').classList.toggle('active', tab === 'results');
            document.getElementById('btnShowErrors').classList.toggle('active', tab === 'errors');
            document.getElementById('dbResultsView').style.display = tab === 'results' ? 'block' : 'none';
            document.getElementById('dbErrorsView').style.display = tab === 'errors' ? 'block' : 'none';
        }

        function renderDbResults(results) {
            const tbody = document.getElementById('dbResultsBody');
            if (!results || results.length === 0) {
                tbody.innerHTML = '<tr><td colspan="11" style="text-align: center; color: var(--text-muted);">No execution records currently stored in MySQL.</td></tr>';
                return;
            }

            tbody.innerHTML = results.map(r => `
                <tr>
                    <td>#${r.id}</td>
                    <td style="font-family: var(--font-code); font-size: 0.72rem;">${r.timestamp}</td>
                    <td><strong>${r.room}</strong></td>
                    <td>${r.room_type || '-'}</td>
                    <td><span class="tag">#${r.style_id || '-'}</span></td>
                    <td><span class="badge-status ${r.status_code === 200 ? 'badge-200' : 'badge-400'}">${r.status || r.status_code}</span></td>
                    <td>${r.latency ? r.latency.toFixed(2) + 's' : '-'}</td>
                    <td>${r.items_count || 0} items</td>
                    <td><span style="font-family: var(--font-code); font-size: 0.72rem; color: #94a3b8;" title="${r.input_path || ''}">${r.input_path ? r.input_path.split('/').pop() : '-'}</span></td>
                    <td>
                        ${r.input_url ? `<a href="${r.input_url}" target="_blank" class="tag" style="text-decoration:none; display:inline-flex; align-items:center; gap:0.25rem; font-size:0.7rem;" title="${r.input_url}">🔗 Blob URL</a>` : '-'}
                    </td>
                    <td>
                        <div class="reason-box ${r.status_code === 200 ? 'success' : 'error'}">
                            ${r.reason || (r.status_code === 200 ? 'Completed successfully' : 'Failed with HTTP ' + r.status_code)}
                        </div>
                    </td>
                </tr>
            `).join('');
        }

        function renderDbErrors(errors) {
            const tbody = document.getElementById('dbErrorsBody');
            if (!errors || errors.length === 0) {
                tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: var(--text-muted);">No error records logged in MySQL.</td></tr>';
                return;
            }

            tbody.innerHTML = errors.map(e => `
                <tr>
                    <td>#${e.id}</td>
                    <td style="font-family: var(--font-code); font-size: 0.72rem;">${e.timestamp}</td>
                    <td><strong>${e.room}</strong></td>
                    <td><span class="tag">#${e.style_id || '-'}</span></td>
                    <td><span class="badge-status ${e.status_code >= 500 ? 'badge-500' : 'badge-400'}">HTTP ${e.status_code}</span></td>
                    <td>Attempt #${e.retry_attempt || 0}</td>
                    <td><span style="font-family: var(--font-code); font-size: 0.72rem; color: #94a3b8;" title="${e.input_path || ''}">${e.input_path ? e.input_path.split('/').pop() : '-'}</span></td>
                    <td>
                        ${e.input_url ? `<a href="${e.input_url}" target="_blank" class="tag" style="text-decoration:none; display:inline-flex; align-items:center; gap:0.25rem; font-size:0.7rem;" title="${e.input_url}">🔗 Blob URL</a>` : '-'}
                    </td>
                    <td>
                        <div class="reason-box error">
                            ${e.reason || e.error_message || 'Unknown error message'}
                        </div>
                    </td>
                </tr>
            `).join('');
        }

        // 5. Real-Time Log Polling
        async function startLogPolling() {
            setInterval(async () => {
                try {
                    const res = await fetch('/api/logs?lines=30');
                    const data = await res.json();
                    const box = document.getElementById('terminalBox');
                    box.innerText = data.logs.join('');
                    box.scrollTop = box.scrollHeight;
                } catch (e) {}
            }, 3000);
        }

        function switchTab(tabId, el) {
            document.querySelectorAll('.tab-nav').forEach(t => t.classList.remove('active'));
            el.classList.add('active');
            document.getElementById('resultsTab').style.display = tabId === 'resultsTab' ? 'block' : 'none';
            document.getElementById('mysqlTab').style.display = tabId === 'mysqlTab' ? 'block' : 'none';
            document.getElementById('logsTab').style.display = tabId === 'logsTab' ? 'block' : 'none';
        }
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print("=" * 80)
    print("       SMART WIZARD INSPIRATION ENGINE - FRONTEND & API SERVER")
    print("=" * 80)
    print(f"[*] API Base URL      : http://localhost:{port}")
    print(f"[*] Interactive UI    : http://localhost:{port}/")
    print(f"[*] Documentation     : http://localhost:{port}/docs")
    print(f"[*] Azure Containers  : prod-smartwizardtemplates, dev-smartwizardtemplates")
    print(f"[*] Remote Inspiration: {DEFAULT_ENDPOINT}")
    print("=" * 80)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
