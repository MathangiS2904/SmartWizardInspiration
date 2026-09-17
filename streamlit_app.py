"""
Smart Wizard Inspiration Testing Suite - Streamlit Controller
Provides interactive UI for testing room inspiration against Azure Blob templates.
"""

import os
import json
import logging
import streamlit as st
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

# Load existing project modules without modifying them
from azure_blob_service import get_blob_manager
from create_room_inspiration_payload import (
    identify_room_type,
    calculate_room_area_m2,
    generate_all_room_payloads,
    ALLOWED_USER_STYLE_IDS
)
from test_inspiration_api import test_room_payload, DEFAULT_ENDPOINT, DEFAULT_API_KEY, get_valid_styles_for_room
from inspiration_db import get_db, ApiResult, ApiError

load_dotenv()

st.set_page_config(
    page_title="Smart Wizard Inspiration Suite",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling
st.markdown("""
<style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1e293b;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        font-size: 1rem;
        color: #64748b;
        margin-bottom: 1.5rem;
    }
    .metric-card {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 12px 16px;
        margin-bottom: 10px;
    }
    .badge-success {
        background-color: #dcfce7;
        color: #166534;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.8rem;
    }
    .badge-fail {
        background-color: #fee2e2;
        color: #991b1b;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.8rem;
    }
</style>
""", unsafe_allow_html=True)

@st.cache_resource
def get_blob_mgr():
    return get_blob_manager()

@st.cache_resource
def get_database_manager():
    return get_db()

# ----------------- SIDEBAR: AZURE TEMPLATE SELECTOR (Step 2) -----------------
st.sidebar.markdown("### 📁 Azure Floorplan Templates")
blob_mgr = get_blob_mgr()

try:
    all_blobs = blob_mgr.list_floorplans()
except Exception as e:
    st.sidebar.error(f"Failed to list blobs: {e}")
    all_blobs = []

categories = ["ALL", "1BHK", "2BHK", "3BHK", "4BHK", "Villa", "Commercial"]
selected_category = st.sidebar.selectbox("Filter Category", categories, index=0)

if selected_category != "ALL":
    filtered_blobs = [b for b in all_blobs if b.startswith(selected_category)]
else:
    filtered_blobs = all_blobs

st.sidebar.caption(f"Showing {len(filtered_blobs)} of {len(all_blobs)} templates in Azure")

search_term = st.sidebar.text_input("🔍 Search template name", "")
if search_term:
    filtered_blobs = [b for b in filtered_blobs if search_term.lower() in b.lower()]

selected_blob = st.sidebar.selectbox(
    "Choose Template",
    filtered_blobs if filtered_blobs else ["No templates found"],
    index=0
)

# ----------------- MAIN PANEL -----------------
st.markdown("<div class='main-header'>🏠 Smart Wizard Inspiration Testing Suite</div>", unsafe_allow_html=True)
st.markdown("<div class='sub-header'>Phase 4 Controller: Azure Blob Storage ➔ Room Geometry Engine ➔ Remote AI Inspiration API ➔ MySQL Logging</div>", unsafe_allow_html=True)

if not selected_blob or selected_blob == "No templates found":
    st.warning("Please select a valid template from the sidebar.")
    st.stop()

# Load Selected Template Data
col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("📐 Template & Room Inspection (Step 3)")
    with st.spinner("Downloading template from Azure Blob..."):
        try:
            template_data = blob_mgr.download_template_json(selected_blob)
        except Exception as e:
            st.error(f"Error downloading blob: {e}")
            template_data = None

    if template_data:
        import math
        unit = template_data.get("unit", "cm")
        scale_to_meters = 0.01 if unit == "cm" else (0.001 if unit == "mm" else 1.0)
        
        layers = template_data.get("layers", {})
        selected_layer = template_data.get("selectedLayer") or (list(layers.keys())[0] if layers else "layer-1")
        layer = layers.get(selected_layer, {})
        areas = layer.get("areas", {})
        vertices = layer.get("vertices", {})
        lines = layer.get("lines", {})
        holes = layer.get("holes", {})

        # Extract walls
        walls_list = []
        for lid, linfo in lines.items():
            v_ids = linfo.get("vertices", [])
            if len(v_ids) == 2 and v_ids[0] in vertices and v_ids[1] in vertices:
                walls_list.append({
                    "id": lid,
                    "x1": vertices[v_ids[0]].get("x", 0) * scale_to_meters,
                    "y1": vertices[v_ids[0]].get("y", 0) * scale_to_meters,
                    "x2": vertices[v_ids[1]].get("x", 0) * scale_to_meters,
                    "y2": vertices[v_ids[1]].get("y", 0) * scale_to_meters
                })

        # Extract Doors and Windows
        openings_list = []
        doors_cnt = 0
        windows_cnt = 0
        for hid, h in holes.items():
            lid = h.get("line")
            line_info = lines.get(lid, {})
            v_ids = line_info.get("vertices", [])
            hname = h.get("name", "Opening")
            is_door = "door" in hname.lower() or "door" in str(h.get("asset_urls", {})).lower()
            is_window = "window" in hname.lower() or "window" in str(h.get("asset_urls", {})).lower()

            offset = float(h.get("offset", 0.5))
            w_prop = h.get("properties", {}).get("width", {})
            w_raw = w_prop.get("length") if isinstance(w_prop, dict) else 80.0
            w_m = float(w_raw or 80.0) * scale_to_meters

            if len(v_ids) == 2 and v_ids[0] in vertices and v_ids[1] in vertices:
                x0 = vertices[v_ids[0]].get("x", 0) * scale_to_meters
                y0 = vertices[v_ids[0]].get("y", 0) * scale_to_meters
                x1 = vertices[v_ids[1]].get("x", 0) * scale_to_meters
                y1 = vertices[v_ids[1]].get("y", 0) * scale_to_meters
                L = math.hypot(x1 - x0, y1 - y0)
                if L > 0:
                    ux = (x1 - x0) / L
                    uy = (y1 - y0) / L
                    cx = x0 + offset * (x1 - x0)
                    cy = y0 + offset * (y1 - y0)
                    half_w = w_m / 2.0
                    nx = -uy
                    ny = ux

                    op_type = "door" if is_door else ("window" if is_window else "opening")
                    if op_type == "door":
                        doors_cnt += 1
                    elif op_type == "window":
                        windows_cnt += 1

                    openings_list.append({
                        "id": hid,
                        "name": hname,
                        "type": op_type,
                        "width_m": w_m,
                        "cx": cx,
                        "cy": cy,
                        "p0": (cx - half_w * ux, cy - half_w * uy),
                        "p1": (cx + half_w * ux, cy + half_w * uy),
                        "leaf": ((cx - half_w * ux) + w_m * nx, (cy - half_w * uy) + w_m * ny)
                    })

        st.caption(f"**Selected Blob:** `{selected_blob}` | **Unit:** `{unit}` | **Rooms:** `{len(areas)}` | **🚪 Doors:** `{doors_cnt}` | **🪟 Windows:** `{windows_cnt}`")

        parsed_rooms = []
        for aid, ainfo in areas.items():
            rname = ainfo.get("name", f"Area_{aid}")
            vids = ainfo.get("vertices", [])
            norm_type = identify_room_type(rname)
            area_m2 = calculate_room_area_m2(vids, vertices, unit)
            area_sqft = round(area_m2 * 10.7639, 1)

            candidates = get_valid_styles_for_room(norm_type, area_m2=area_m2, allow_cross_room=True)
            rec_style = candidates[0][1] if candidates else None
            fallbacks = sorted(list(set(c[0] for c in candidates if c[0].lower() != norm_type.lower())))

            poly_m = []
            for vid in vids:
                if vid in vertices:
                    poly_m.append((
                        vertices[vid].get("x", 0) * scale_to_meters,
                        vertices[vid].get("y", 0) * scale_to_meters
                    ))

            parsed_rooms.append({
                "area_id": aid,
                "name": rname,
                "room_type": norm_type,
                "area_m2": area_m2,
                "area_sqft": area_sqft,
                "recommended_style": rec_style,
                "fallbacks": fallbacks,
                "polygon": poly_m
            })

        room_df = pd.DataFrame([
            {
                "Room Name": r["name"],
                "Normalized Type": r["room_type"],
                "Area (m²)": r["area_m2"],
                "Area (sqft)": r["area_sqft"],
                "Default Style ID": r["recommended_style"] or "N/A"
            }
            for r in parsed_rooms
        ])
        st.dataframe(room_df, use_container_width=True, hide_index=True)

        # SVG Geometry Visualization with Walls, Doors, and Windows
        st.markdown("##### 🗺️ Architectural Floorplan Preview (Rooms, Doors 🚪, Windows 🪟, Walls)")
        all_x = [pt[0] for r in parsed_rooms for pt in r["polygon"]] + [w["x1"] for w in walls_list] + [w["x2"] for w in walls_list] or [0, 10]
        all_y = [pt[1] for r in parsed_rooms for pt in r["polygon"]] + [w["y1"] for w in walls_list] + [w["y2"] for w in walls_list] or [0, 10]
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
        w = max(1.0, max_x - min_x)
        h = max(1.0, max_y - min_y)
        padding = 0.5
        vb_x = min_x - padding
        vb_y = min_y - padding
        vb_w = w + (padding * 2)
        vb_h = h + (padding * 2)

        colors = ["#3b82f6", "#10b981", "#8b5cf6", "#f59e0b", "#ec4899", "#06b6d4"]
        svg_parts = [
            f'<svg viewBox="{vb_x} {vb_y} {vb_w} {vb_h}" style="width:100%; height:360px; background:#0f172a; border-radius:10px; border:1px solid #334155;">'
        ]

        # 1. Structural Walls
        for wall in walls_list:
            svg_parts.append(f'<line x1="{wall["x1"]}" y1="{wall["y1"]}" x2="{wall["x2"]}" y2="{wall["y2"]}" stroke="#334155" stroke-width="0.08" stroke-linecap="round" />')

        # 2. Rooms
        for i, r in enumerate(parsed_rooms):
            if not r["polygon"]:
                continue
            pts_str = " ".join([f"{pt[0]},{pt[1]}" for pt in r["polygon"]])
            color = colors[i % len(colors)]
            svg_parts.append(f'<polygon points="{pts_str}" fill="{color}" fill-opacity="0.25" stroke="{color}" stroke-width="0.05" />')
            cx = sum([pt[0] for pt in r["polygon"]]) / len(r["polygon"])
            cy = sum([pt[1] for pt in r["polygon"]]) / len(r["polygon"])
            svg_parts.append(f'<text x="{cx}" y="{cy - 0.1}" fill="#ffffff" font-size="0.38" font-weight="bold" text-anchor="middle">{r["name"]}</text>')
            svg_parts.append(f'<text x="{cx}" y="{cy + 0.35}" fill="#94a3b8" font-size="0.28" text-anchor="middle">{r["area_m2"]} m²</text>')

        # 3. Openings (Doors & Windows)
        for o in openings_list:
            if o["type"] == "door":
                p0 = o["p0"]
                p1 = o["p1"]
                leaf = o["leaf"]
                wm = o["width_m"]
                svg_parts.append(f'<line x1="{p0[0]}" y1="{p0[1]}" x2="{p1[0]}" y2="{p1[1]}" stroke="#f59e0b" stroke-width="0.08" stroke-linecap="square" />')
                svg_parts.append(f'<line x1="{p0[0]}" y1="{p0[1]}" x2="{leaf[0]}" y2="{leaf[1]}" stroke="#fbbf24" stroke-width="0.05" stroke-linecap="round" />')
                svg_parts.append(f'<path d="M {leaf[0]} {leaf[1]} A {wm} {wm} 0 0 1 {p1[0]} {p1[1]}" fill="rgba(245, 158, 11, 0.12)" stroke="#f59e0b" stroke-width="0.03" stroke-dasharray="0.05,0.05" />')
                svg_parts.append(f'<text x="{o["cx"]}" y="{o["cy"]}" font-size="0.32" text-anchor="middle" dominant-baseline="central">🚪</text>')
            elif o["type"] == "window":
                p0 = o["p0"]
                p1 = o["p1"]
                svg_parts.append(f'<line x1="{p0[0]}" y1="{p0[1]}" x2="{p1[0]}" y2="{p1[1]}" stroke="#06b6d4" stroke-width="0.1" stroke-linecap="square" />')
                svg_parts.append(f'<line x1="{p0[0]}" y1="{p0[1]}" x2="{p1[0]}" y2="{p1[1]}" stroke="#e0f2fe" stroke-width="0.03" stroke-linecap="square" />')
                svg_parts.append(f'<text x="{o["cx"]}" y="{o["cy"]}" font-size="0.3" text-anchor="middle" dominant-baseline="central">🪟</text>')

        svg_parts.append('</svg>')
        st.markdown("".join(svg_parts), unsafe_allow_html=True)

# ----------------- EXECUTION CONTROLLER (Step 4) -----------------
with col_right:
    st.subheader("⚡ Execution & Remote API Controller (Step 4)")
    st.markdown("Trigger Step 1 payload generator + remote inspiration AI engine with automatic fallback retry.")

    room_options = ["ALL"] + [r["name"] for r in parsed_rooms] if template_data else ["ALL"]
    selected_target_room = st.selectbox("Select Target Room", room_options, index=0)

    exec_col1, exec_col2 = st.columns(2)
    with exec_col1:
        max_retries = st.slider("Max Style Retries", min_value=1, max_value=8, value=4)
    with exec_col2:
        allow_cross_room = st.checkbox("Allow Cross-Room Fallback", value=True, help="If kitchen fails due to mandatory asset clearance, retry as Bathroom or Bedroom")

    run_button = st.button("🚀 Run Inspiration Engine", type="primary", use_container_width=True)

    if run_button and template_data:
        progress_bar = st.progress(0)
        status_text = st.empty()
        log_box = st.empty()

        logs_collected = []
        def log_msg(msg):
            logs_collected.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
            log_box.code("\n".join(logs_collected[-12:]), language="text")

        log_msg(f"Target Template: {selected_blob}")
        log_msg("Step 1: Generating standard inspiration payload...")
        progress_bar.progress(20)

        # Use generate_all_room_payloads or generate_room_json
        DEFAULT_RULES_FILE = os.path.join(os.path.dirname(__file__), "styles_and_rules.json")
        runtime_dir = os.path.join(os.path.dirname(__file__), "runtime_payloads")
        os.makedirs(runtime_dir, exist_ok=True)

        if selected_target_room != "ALL":
            from create_room_inspiration_payload import generate_room_json
            payload = generate_room_json(
                floorplan_data=template_data,
                style_id=237,
                room_type=selected_target_room,
                rules_source=DEFAULT_RULES_FILE
            )
            payload_path = os.path.join(runtime_dir, f"{selected_target_room.lower().replace(' ', '_')}.json")
            with open(payload_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            target_files = [payload_path]
        else:
            generate_all_room_payloads(
                floorplan_data=template_data,
                rules_source=DEFAULT_RULES_FILE,
                output_dir=runtime_dir
            )
            target_files = sorted([os.path.join(runtime_dir, f) for f in os.listdir(runtime_dir) if f.endswith(".json")])

        log_msg(f"Found {len(target_files)} room payload(s) to test.")

        # Test each payload
        success_count = 0
        total_items_placed = 0
        placed_items_list = []

        for idx, pf in enumerate(target_files):
            pct = 40 + int(((idx + 1) / len(target_files)) * 50)
            progress_bar.progress(pct)

            with open(pf, "r", encoding="utf-8") as rf:
                p_data = json.load(rf)

            room_type = p_data.get("room_type", "Unknown")
            style_id = p_data.get("style_id", 0)
            log_msg(f"▶️ Testing '{room_type}' with Style {style_id}...")

            res = test_room_payload(
                payload_path=pf,
                endpoint_url=DEFAULT_ENDPOINT,
                api_key=DEFAULT_API_KEY,
                output_dir=os.path.join(os.path.dirname(__file__), "output_results"),
                max_retries=max_retries,
                allow_cross_room=allow_cross_room
            )

            status_code = res.get("status_code", 0)
            is_success = res.get("success", False)

            if is_success:
                success_count += 1
                items = res.get("placed_items", [])
                total_items_placed += len(items)
                for it in items:
                    placed_items_list.append({
                        "Room": room_type,
                        "Item Name": it.get("item_name") or it.get("name") or "Unknown",
                        "Category": it.get("category", "Furniture"),
                        "Position": str(it.get("position") or it.get("pos", "-")),
                        "Dimensions": str(it.get("dimensions") or it.get("dim", "-"))
                    })
                log_msg(f"🎉 Room '{room_type}' completed: {len(items)} items placed (HTTP 200).")
            else:
                log_msg(f"❌ Room '{room_type}' failed with HTTP {status_code}: {res.get('error') or 'Unknown error'}")

        progress_bar.progress(100)
        status_text.success(f"Execution Completed: {success_count}/{len(target_files)} rooms passed! Total items placed: {total_items_placed}")

        if placed_items_list:
            st.markdown("##### 🛋️ Placed 3D Furniture Items (Step 5)")
            st.dataframe(pd.DataFrame(placed_items_list), use_container_width=True, hide_index=True)

# ----------------- RESULTS VISUALIZATION & MYSQL DASHBOARD (Step 5) -----------------
st.markdown("---")
st.subheader("📊 MySQL Results & Logs Dashboard (Step 5)")

tab_results, tab_errors = st.tabs(["✅ Successful Test Runs (api_results)", "❌ Error & Fallback History (api_errors)"])

db_mgr = get_database_manager()

with tab_results:
    try:
        results = db_mgr.get_all_results(limit=30)
        if results:
            st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
        else:
            st.info("No successful test runs logged in MySQL yet.")
    except Exception as e:
        st.error(f"Error querying api_results: {e}")

with tab_errors:
    try:
        errors = db_mgr.get_all_errors(limit=30)
        if errors:
            st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)
        else:
            st.info("No error records found in MySQL.")
    except Exception as e:
        st.error(f"Error querying api_errors: {e}")

st.caption(f"Smart Wizard Inspiration Testing Suite • Connected to {db_mgr.db_url}")
