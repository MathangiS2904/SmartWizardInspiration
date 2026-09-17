"""
visualize_smart_wizard.py

Standalone JSON Visualizer for Smart Wizard Floorplan Inputs.
Renders 2D CAD-grade architectural floorplan with:
- Rooms & Calculated Area (m² / sqft)
- Structural Walls & Dimension Lengths
- Doors with Opening Leaves & Swing Arcs 🚪
- Windows with Double-Pane Glass Frames 🪟
- North Compass Orientation 🧭
- Interactive Pan/Zoom, Visibility Toggles, Room Inspector, and JSON Tree

Usage:
  # 1. Visualize a local JSON file:
  python visualize_smart_wizard.py -f 1bhk_south_square_853.2sqft.json

  # 2. Visualize an Azure Blob floorplan template:
  python visualize_smart_wizard.py -b 1BHK/Comfort_Focused_Living/1bhk_south_rect_818.9sqft.json

  # 3. Launch interactive web visualizer with drag-and-drop:
  python visualize_smart_wizard.py --serve

  # 4. Export static standalone HTML:
  python visualize_smart_wizard.py -f 1bhk_south_square_853.2sqft.json -o floorplan_preview.html
"""

import os
import sys
import json
import math
import argparse
import webbrowser
import http.server
import socketserver
import threading
from typing import Dict, Any, List, Optional
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


# Enable UTF-8 encoding in Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def calculate_polygon_area_shoelace(coords: List[tuple]) -> float:
    """Calculates the area of a polygon using the Shoelace formula."""
    n = len(coords)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += coords[i][0] * coords[j][1]
        area -= coords[j][0] * coords[i][1]
    return abs(area) / 2.0


def parse_smart_wizard_json(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parses a raw Smart Wizard floorplan JSON structure into an architectural
    visualization model with calculated dimensions, area (m2/sqft), walls,
    doors, windows, and orientation.
    """
    unit = str(data.get("unit", "")).lower()

    layers = data.get("layers", {})
    selected_layer_id = data.get("selectedLayer") or (list(layers.keys())[0] if layers else "layer-1")
    layer = layers.get(selected_layer_id, {})

    vertices = layer.get("vertices", {})
    lines = layer.get("lines", {})
    holes = layer.get("holes", {})
    areas = layer.get("areas", {})

    # Auto-detect millimeters vs centimeters
    is_mm = unit == "mm"
    if not is_mm and unit != "cm":
        for lid, linfo in lines.items():
            props = linfo.get("properties", {})
            thick = props.get("thickness", {}).get("length", 0) if isinstance(props.get("thickness"), dict) else 0
            height = props.get("height", {}).get("length", 0) if isinstance(props.get("height"), dict) else 0
            if thick > 50 or height > 1000:
                is_mm = True
                break

    scale_to_cm = 0.1 if is_mm else 1.0
    scale_to_m = 0.001 if is_mm else 0.01
    unit = "mm" if is_mm else "cm"

    direction = data.get("direction", "N")
    direction_angle = data.get("directionangle", 0)

    # 1. Parse Vertices
    parsed_vertices = {}
    for vid, v in vertices.items():
        parsed_vertices[vid] = {
            "id": vid,
            "x": float(v.get("x", 0)),
            "y": float(v.get("y", 0))
        }

    # 2. Parse Walls (Lines)
    parsed_walls = []
    for lid, linfo in lines.items():
        vids = linfo.get("vertices", [])
        if len(vids) == 2 and vids[0] in parsed_vertices and vids[1] in parsed_vertices:
            v0 = parsed_vertices[vids[0]]
            v1 = parsed_vertices[vids[1]]
            length_cm = math.hypot(v1["x"] - v0["x"], v1["y"] - v0["y"]) * scale_to_cm
            length_m = length_cm / 100.0

            thickness_prop = linfo.get("properties", {}).get("thickness", {})
            thickness_cm = float(thickness_prop.get("length", 15.0)) if isinstance(thickness_prop, dict) else 15.0

            parsed_walls.append({
                "id": lid,
                "v0": v0,
                "v1": v1,
                "x1": round(v0["x"], 1),
                "y1": round(v0["y"], 1),
                "x2": round(v1["x"], 1),
                "y2": round(v1["y"], 1),
                "length_cm": round(length_cm, 1),
                "length_m": round(length_m, 2),
                "thickness_cm": round(thickness_cm, 1),
                "visible": linfo.get("visible", True)
            })

    # 3. Parse Openings (Doors & Windows)
    parsed_openings = []
    doors_count = 0
    windows_count = 0

    for hid, h in holes.items():
        lid = h.get("line")
        linfo = lines.get(lid, {})
        vids = linfo.get("vertices", [])
        hname = h.get("name", "Opening")

        is_door = "door" in hname.lower() or "door" in str(h.get("asset_urls", {})).lower()
        is_window = "window" in hname.lower() or "window" in str(h.get("asset_urls", {})).lower()

        offset = float(h.get("offset", 0.5))
        w_prop = h.get("properties", {}).get("width", {})
        w_raw = w_prop.get("length") if isinstance(w_prop, dict) else 80.0
        width_val = float(w_raw or 80.0)

        if len(vids) == 2 and vids[0] in parsed_vertices and vids[1] in parsed_vertices:
            v0 = parsed_vertices[vids[0]]
            v1 = parsed_vertices[vids[1]]
            x0, y0 = v0["x"], v0["y"]
            x1, y1 = v1["x"], v1["y"]
            L = math.hypot(x1 - x0, y1 - y0)
            if L > 0:
                offset_ratio = (offset / L) if offset > 1.0 else offset
                offset_ratio = min(max(offset_ratio, 0.0), 1.0)
                ux = (x1 - x0) / L
                uy = (y1 - y0) / L
                cx = x0 + offset_ratio * (x1 - x0)
                cy = y0 + offset_ratio * (y1 - y0)
                half_w = width_val / 2.0
                nx = -uy
                ny = ux

                op_type = "door" if is_door else ("window" if is_window else "opening")
                if op_type == "door":
                    doors_count += 1
                elif op_type == "window":
                    windows_count += 1

                parsed_openings.append({
                    "id": hid,
                    "name": hname,
                    "type": op_type,
                    "width": round(width_val, 1),
                    "cx": round(cx, 1),
                    "cy": round(cy, 1),
                    "p0": {"x": round(cx - half_w * ux, 1), "y": round(cy - half_w * uy, 1)},
                    "p1": {"x": round(cx + half_w * ux, 1), "y": round(cy + half_w * uy, 1)},
                    "leaf": {"x": round((cx - half_w * ux) + width_val * nx, 1), "y": round((cy - half_w * uy) + width_val * ny, 1)},
                    "line_id": lid
                })

    # 4. Parse Rooms (Areas)
    parsed_rooms = []
    total_m2 = 0.0

    for aid, a in areas.items():
        rname = a.get("name", f"Room_{aid}")
        v_ids = a.get("vertices", [])
        poly_m = []
        poly_raw = []

        for vid in v_ids:
            if vid in parsed_vertices:
                pv = parsed_vertices[vid]
                poly_raw.append({"id": vid, "x": round(pv["x"], 1), "y": round(pv["y"], 1)})
                poly_m.append((pv["x"] * scale_to_m, pv["y"] * scale_to_m))

        area_m2 = round(calculate_polygon_area_shoelace(poly_m), 2)
        area_sqft = round(area_m2 * 10.7639, 1)
        total_m2 += area_m2

        # Center point
        if poly_raw:
            cx = round(sum(p["x"] for p in poly_raw) / len(poly_raw), 1)
            cy = round(sum(p["y"] for p in poly_raw) / len(poly_raw), 1)
        else:
            cx, cy = 0.0, 0.0

        parsed_rooms.append({
            "id": aid,
            "name": rname,
            "area_m2": area_m2,
            "area_sqft": area_sqft,
            "vertices_count": len(v_ids),
            "polygon": poly_raw,
            "center": {"x": cx, "y": cy}
        })

    total_sqft = round(total_m2 * 10.7639, 1)

    return {
        "unit": unit,
        "direction": direction,
        "direction_angle": direction_angle,
        "total_area_m2": round(total_m2, 2),
        "total_area_sqft": total_sqft,
        "rooms_count": len(parsed_rooms),
        "walls_count": len(parsed_walls),
        "doors_count": doors_count,
        "windows_count": windows_count,
        "vertices_count": len(parsed_vertices),
        "rooms": parsed_rooms,
        "walls": parsed_walls,
        "openings": parsed_openings,
        "raw_json": data
    }


def generate_visualizer_html(parsed_data: Dict[str, Any], title: str = "Smart Wizard Floorplan Visualizer") -> str:
    """Generates a rich, standalone interactive HTML visualizer with zoom/pan and inspector."""
    json_str = json.dumps(parsed_data, indent=2)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-base: #090d16;
            --bg-card: #0f172a;
            --bg-card-hover: #1e293b;
            --border: rgba(255, 255, 255, 0.08);
            --primary: #4f46e5;
            --primary-light: #818cf8;
            --primary-glow: rgba(99, 102, 241, 0.25);
            --accent: #06b6d4;
            --amber: #f59e0b;
            --success: #10b981;
            --danger: #ef4444;
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --font-main: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            --font-code: 'JetBrains Mono', monospace;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            background: var(--bg-base);
            color: var(--text);
            font-family: var(--font-main);
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }}

        /* Header */
        header {{
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(12px);
            border-bottom: 1px solid var(--border);
            padding: 0.75rem 1.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            z-index: 100;
        }}

        .brand {{
            display: flex;
            align-items: center;
            gap: 0.85rem;
        }}
        .brand-icon {{
            width: 38px;
            height: 38px;
            border-radius: 10px;
            background: linear-gradient(135deg, var(--primary), var(--accent));
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.25rem;
            box-shadow: 0 0 16px var(--primary-glow);
        }}
        .brand-title {{
            font-size: 1.05rem;
            font-weight: 800;
            background: linear-gradient(135deg, #ffffff, #cbd5e1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        .brand-subtitle {{
            font-size: 0.72rem;
            color: var(--text-muted);
        }}

        .header-meta {{
            display: flex;
            align-items: center;
            gap: 1rem;
        }}
        .metric-badge {{
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border);
            padding: 0.35rem 0.75rem;
            border-radius: 8px;
            font-size: 0.75rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        .metric-badge strong {{
            color: #ffffff;
            font-family: var(--font-code);
        }}

        /* Main Workspace */
        .workspace {{
            display: flex;
            flex: 1;
            height: calc(100vh - 65px);
            overflow: hidden;
        }}

        /* Left / Center: Interactive Canvas */
        .canvas-container {{
            flex: 1;
            position: relative;
            background: radial-gradient(circle at center, #131d33 0%, #090d16 100%);
            overflow: hidden;
            user-select: none;
        }}

        svg.floorplan-viewport {{
            width: 100%;
            height: 100%;
            cursor: grab;
        }}
        svg.floorplan-viewport:active {{
            cursor: grabbing;
        }}

        /* Floating Toolbar (Controls & Toggles) */
        .floating-toolbar {{
            position: absolute;
            top: 16px;
            left: 16px;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(10px);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 0.5rem;
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
            z-index: 20;
            box-shadow: 0 8px 32px rgba(0,0,0,0.5);
        }}

        .toolbar-group {{
            display: flex;
            gap: 0.4rem;
        }}

        .tool-btn {{
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 0.4rem 0.65rem;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            transition: all 0.2s;
        }}
        .tool-btn:hover {{
            background: rgba(255, 255, 255, 0.12);
            color: white;
        }}
        .tool-btn.active {{
            background: rgba(99, 102, 241, 0.3);
            border-color: var(--primary);
            color: #ffffff;
        }}

        /* Zoom Controls */
        .zoom-controls {{
            position: absolute;
            bottom: 20px;
            left: 20px;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(10px);
            border: 1px solid var(--border);
            border-radius: 10px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            z-index: 20;
        }}
        .zoom-btn {{
            background: transparent;
            border: none;
            color: white;
            width: 36px;
            height: 36px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            font-size: 1.1rem;
            transition: background 0.2s;
        }}
        .zoom-btn:hover {{
            background: rgba(255, 255, 255, 0.1);
        }}
        .zoom-btn:not(:last-child) {{
            border-bottom: 1px solid var(--border);
        }}

        /* Compass */
        .compass-rose {{
            position: absolute;
            top: 20px;
            right: 20px;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(8px);
            border: 1px solid var(--border);
            border-radius: 50%;
            width: 50px;
            height: 50px;
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 20;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }}

        /* Right Panel: Inspector & Data Tree */
        .inspector-panel {{
            width: 400px;
            background: var(--bg-card);
            border-left: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            z-index: 50;
        }}

        .tabs-nav {{
            display: flex;
            border-bottom: 1px solid var(--border);
            background: rgba(0, 0, 0, 0.2);
        }}
        .tab-btn {{
            flex: 1;
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 0.85rem;
            font-size: 0.8rem;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s;
            position: relative;
        }}
        .tab-btn.active {{
            color: var(--text);
            background: rgba(255, 255, 255, 0.03);
        }}
        .tab-btn.active::after {{
            content: '';
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: var(--primary);
        }}

        .tab-content {{
            flex: 1;
            padding: 1.25rem;
            overflow-y: auto;
        }}

        .section-title {{
            font-size: 0.85rem;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.75rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .room-item-card {{
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 0.75rem;
            margin-bottom: 0.5rem;
            cursor: pointer;
            transition: all 0.2s;
        }}
        .room-item-card:hover, .room-item-card.selected {{
            border-color: var(--primary-light);
            background: rgba(99, 102, 241, 0.08);
        }}
        .room-header-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-weight: 700;
            font-size: 0.85rem;
        }}
        .room-area-tag {{
            font-family: var(--font-code);
            font-size: 0.72rem;
            background: rgba(6, 182, 212, 0.15);
            color: var(--accent);
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
        }}

        .json-pre {{
            background: #06090e;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1rem;
            font-family: var(--font-code);
            font-size: 0.72rem;
            line-height: 1.45;
            color: #38bdf8;
            overflow: auto;
            max-height: calc(100vh - 160px);
            white-space: pre-wrap;
        }}

        /* Drag and Drop Zone Overlay */
        .dropzone-overlay {{
            position: absolute;
            inset: 0;
            background: rgba(15, 23, 42, 0.9);
            backdrop-filter: blur(10px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            border: 3px dashed var(--primary);
        }}
        .dropzone-overlay.active {{
            display: flex;
        }}
        .dropzone-text {{
            font-size: 1.5rem;
            font-weight: 700;
            color: white;
            text-align: center;
        }}
    </style>
</head>
<body>

    <header>
        <div class="brand">
            <div class="brand-icon">🏠</div>
            <div>
                <div class="brand-title">{title}</div>
                <div class="brand-subtitle">Smart Wizard CAD Floorplan Geometry & Element Inspector</div>
            </div>
        </div>

        <div class="header-meta">
            <div class="metric-badge">
                <span>📐 Total Area:</span>
                <strong><span id="metaAreaM2">--</span> m² (<span id="metaAreaSqft">--</span> sqft)</strong>
            </div>
            <div class="metric-badge">
                <span>Unit:</span>
                <strong><span id="metaUnit">--</span></strong>
            </div>
            <label class="tool-btn" style="cursor: pointer;">
                <span>📂 Open JSON</span>
                <input type="file" id="fileInput" accept=".json" style="display: none;" onchange="handleFileSelect(event)">
            </label>
        </div>
    </header>

    <div class="workspace">
        
        <!-- Floorplan Canvas Viewport -->
        <div class="canvas-container" id="canvasBox">
            
            <!-- Floating Toggles -->
            <div class="floating-toolbar">
                <div class="toolbar-group">
                    <button class="tool-btn active" id="btnTogRooms" onclick="toggleVisibility('rooms')">🟦 Rooms</button>
                    <button class="tool-btn active" id="btnTogWalls" onclick="toggleVisibility('walls')">🧱 Walls</button>
                    <button class="tool-btn active" id="btnTogDoors" onclick="toggleVisibility('doors')">🚪 Doors (<span id="cntDoors">0</span>)</button>
                    <button class="tool-btn active" id="btnTogWindows" onclick="toggleVisibility('windows')">🪟 Windows (<span id="cntWindows">0</span>)</button>
                    <button class="tool-btn" id="btnTogDims" onclick="toggleVisibility('dims')">📏 Dimensions</button>
                </div>
            </div>

            <!-- Compass Rose -->
            <div class="compass-rose" id="compassElem" title="North Orientation">
                <span style="color: #ef4444; font-weight: 800; font-size: 1.1rem;">N ↑</span>
            </div>

            <!-- Zoom & Pan Controls -->
            <div class="zoom-controls">
                <button class="zoom-btn" onclick="zoomIn()" title="Zoom In">+</button>
                <button class="zoom-btn" onclick="zoomOut()" title="Zoom Out">−</button>
                <button class="zoom-btn" onclick="resetView()" title="Fit to Center">⛶</button>
            </div>

            <!-- SVG Rendering Surface -->
            <svg id="svgViewport" class="floorplan-viewport">
                <g id="transformGroup">
                    <g id="wallsGroup"></g>
                    <g id="roomsGroup"></g>
                    <g id="openingsGroup"></g>
                    <g id="dimsGroup" style="display: none;"></g>
                </g>
            </svg>

            <!-- Dropzone Overlay -->
            <div class="dropzone-overlay" id="dropOverlay">
                <div class="dropzone-text">
                    📥 Drop Smart Wizard JSON Floorplan Here to Visualize
                </div>
            </div>
        </div>

        <!-- Right Side Panel -->
        <div class="inspector-panel">
            <div class="tabs-nav">
                <button class="tab-btn active" onclick="switchInspectorTab('roomsTab', this)">🏠 Rooms (<span id="cntRooms">0</span>)</button>
                <button class="tab-btn" onclick="switchInspectorTab('jsonTab', this)">📜 Raw JSON</button>
            </div>

            <!-- Tab 1: Rooms Inspector -->
            <div class="tab-content" id="roomsTab">
                <div class="section-title">
                    <span>Floorplan Rooms</span>
                    <span style="font-size: 0.72rem; color: var(--accent);">Click to Highlight</span>
                </div>
                <div id="roomsListContainer">
                    <!-- Populated dynamically -->
                </div>
            </div>

            <!-- Tab 2: Raw JSON Tree -->
            <div class="tab-content" id="jsonTab" style="display: none;">
                <pre class="json-pre" id="jsonTreeBox"></pre>
            </div>
        </div>

    </div>

    <script>
        // Initial Parsed Data embedded from Python
        let floorplanData = {json_str};

        // Canvas Zoom & Pan Transform State
        let scale = 1.0;
        let panX = 0;
        let panY = 0;
        let isPanning = false;
        let startX = 0;
        let startY = 0;

        const visibility = {{
            rooms: true,
            walls: true,
            doors: true,
            windows: true,
            dims: false
        }};

        window.addEventListener('DOMContentLoaded', () => {{
            setupDragAndDrop();
            setupPanZoomEvents();
            renderFloorplan(floorplanData);
        }});

        function renderFloorplan(data) {{
            floorplanData = data;
            
            // Header stats
            document.getElementById('metaAreaM2').innerText = data.total_area_m2 || 0;
            document.getElementById('metaAreaSqft').innerText = data.total_area_sqft || 0;
            document.getElementById('metaUnit').innerText = (data.unit || 'cm').toUpperCase();
            document.getElementById('cntDoors').innerText = data.doors_count || 0;
            document.getElementById('cntWindows').innerText = data.windows_count || 0;
            document.getElementById('cntRooms').innerText = data.rooms_count || 0;

            // Rotate compass rose according to direction angle
            const angle = data.direction_angle || 0;
            document.getElementById('compassElem').style.transform = `rotate(${{angle}}deg)`;

            // Render right side room cards
            renderRoomCards(data.rooms || []);

            // Render raw JSON in inspector
            document.getElementById('jsonTreeBox').innerText = JSON.stringify(data.raw_json, null, 2);

            // Compute Bounding Box
            const walls = data.walls || [];
            const rooms = data.rooms || [];
            const openings = data.openings || [];

            let allPoints = [];
            rooms.forEach(r => r.polygon.forEach(p => allPoints.push(p)));
            walls.forEach(w => {{
                allPoints.push({{ x: w.x1, y: w.y1 }});
                allPoints.push({{ x: w.x2, y: w.y2 }});
            }});
            if (allPoints.length === 0) {{
                openings.forEach(o => allPoints.push({{ x: o.cx, y: o.cy }}));
            }}

            if (allPoints.length === 0) return;

            const minX = Math.min(...allPoints.map(p => p.x));
            const maxX = Math.max(...allPoints.map(p => p.x));
            const minY = Math.min(...allPoints.map(p => p.y));
            const maxY = Math.max(...allPoints.map(p => p.y));

            const bboxWidth = Math.max(maxX - minX, 100);
            const bboxHeight = Math.max(maxY - minY, 100);

            // Render Structural Walls
            const wallsGroup = document.getElementById('wallsGroup');
            wallsGroup.innerHTML = walls.map(w => `
                <line x1="${{w.x1}}" y1="${{w.y1}}" x2="${{w.x2}}" y2="${{w.y2}}" stroke="#475569" stroke-width="7" stroke-linecap="round" />
            `).join('');

            // Render Rooms
            const colors = ['#6366f1', '#06b6d4', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6'];
            const roomsGroup = document.getElementById('roomsGroup');
            roomsGroup.innerHTML = rooms.map((r, i) => {{
                const pts = r.polygon.map(p => `${{p.x}},${{p.y}}`).join(' ');
                const c = colors[i % colors.length];
                const cx = r.center.x;
                const cy = r.center.y;
                return `
                    <g class="room-polygon-g" id="poly_room_${{r.id}}" onclick="selectRoomCard('${{r.id}}')">
                        <polygon points="${{pts}}" fill="${{c}}" fill-opacity="0.25" stroke="${{c}}" stroke-width="2.5" />
                        <text x="${{cx}}" y="${{cy - 8}}" fill="#ffffff" font-size="18" font-weight="700" text-anchor="middle">${{r.name}}</text>
                        <text x="${{cx}}" y="${{cy + 14}}" fill="#94a3b8" font-size="13" font-weight="600" text-anchor="middle">${{r.area_m2}} m² (${{r.area_sqft}} sqft)</text>
                    </g>
                `;
            }}).join('');

            // Render Openings (Doors & Windows)
            const openingsGroup = document.getElementById('openingsGroup');
            openingsGroup.innerHTML = openings.map(o => {{
                if (o.type === 'door') {{
                    const jamb = `<line x1="${{o.p0.x}}" y1="${{o.p0.y}}" x2="${{o.p1.x}}" y2="${{o.p1.y}}" stroke="#f59e0b" stroke-width="6" stroke-linecap="square" />`;
                    const leaf = `<line x1="${{o.p0.x}}" y1="${{o.p0.y}}" x2="${{o.leaf.x}}" y2="${{o.leaf.y}}" stroke="#fbbf24" stroke-width="3.5" stroke-linecap="round" />`;
                    const swingArc = `<path d="M ${{o.leaf.x}} ${{o.leaf.y}} A ${{o.width}} ${{o.width}} 0 0 1 ${{o.p1.x}} ${{o.p1.y}}" fill="rgba(245, 158, 11, 0.12)" stroke="#f59e0b" stroke-width="1.8" stroke-dasharray="3,3" />`;
                    const badge = `<text x="${{o.cx}}" y="${{o.cy}}" font-size="14" text-anchor="middle" dominant-baseline="central">🚪</text><title>${{o.name}} (${{o.width}} cm)</title>`;
                    return `<g class="door-elem">${{swingArc}}${{jamb}}${{leaf}}${{badge}}</g>`;
                }} else if (o.type === 'window') {{
                    const frame = `<line x1="${{o.p0.x}}" y1="${{o.p0.y}}" x2="${{o.p1.x}}" y2="${{o.p1.y}}" stroke="#06b6d4" stroke-width="7" stroke-linecap="square" />`;
                    const glass = `<line x1="${{o.p0.x}}" y1="${{o.p0.y}}" x2="${{o.p1.x}}" y2="${{o.p1.y}}" stroke="#e0f2fe" stroke-width="2" stroke-linecap="square" />`;
                    const badge = `<text x="${{o.cx}}" y="${{o.cy}}" font-size="13" text-anchor="middle" dominant-baseline="central">🪟</text><title>${{o.name}} (${{o.width}} cm)</title>`;
                    return `<g class="window-elem">${{frame}}${{glass}}${{badge}}</g>`;
                }}
                return '';
            }}).join('');

            // Render Dimensions (Wall Lengths)
            const dimsGroup = document.getElementById('dimsGroup');
            dimsGroup.innerHTML = walls.map(w => {{
                const mx = (w.x1 + w.x2) / 2;
                const my = (w.y1 + w.y2) / 2;
                return `
                    <g>
                        <rect x="${{mx - 22}}" y="${{my - 9}}" width="44" height="18" fill="rgba(15, 23, 42, 0.85)" rx="4" />
                        <text x="${{mx}}" y="${{my + 4}}" fill="#cbd5e1" font-size="10" font-weight="600" text-anchor="middle">${{w.length_m}}m</text>
                    </g>
                `;
            }}).join('');

            // Auto-fit to viewport
            fitToBox(minX, minY, bboxWidth, bboxHeight);
        }}

        function fitToBox(minX, minY, w, h) {{
            const svg = document.getElementById('svgViewport');
            const svgWidth = svg.clientWidth || 800;
            const svgHeight = svg.clientHeight || 600;

            const pad = 60;
            const scaleX = svgWidth / (w + pad * 2);
            const scaleY = svgHeight / (h + pad * 2);
            scale = Math.min(scaleX, scaleY, 1.8);

            const centerX = minX + w / 2;
            const centerY = minY + h / 2;

            panX = (svgWidth / 2) - (centerX * scale);
            panY = (svgHeight / 2) - (centerY * scale);

            updateTransform();
        }}

        function updateTransform() {{
            const g = document.getElementById('transformGroup');
            g.setAttribute('transform', `translate(${{panX}}, ${{panY}}) scale(${{scale}})`);
        }}

        // Pan & Zoom Events
        function setupPanZoomEvents() {{
            const svg = document.getElementById('svgViewport');

            svg.addEventListener('mousedown', (e) => {{
                isPanning = true;
                startX = e.clientX - panX;
                startY = e.clientY - panY;
            }});

            window.addEventListener('mousemove', (e) => {{
                if (!isPanning) return;
                panX = e.clientX - startX;
                panY = e.clientY - startY;
                updateTransform();
            }});

            window.addEventListener('mouseup', () => {{
                isPanning = false;
            }});

            svg.addEventListener('wheel', (e) => {{
                e.preventDefault();
                const zoomFactor = 1.15;
                const mouseX = e.clientX;
                const mouseY = e.clientY;

                const prevScale = scale;
                if (e.deltaY < 0) {{
                    scale = Math.min(scale * zoomFactor, 8.0);
                }} else {{
                    scale = Math.max(scale / zoomFactor, 0.15);
                }}

                panX = mouseX - (mouseX - panX) * (scale / prevScale);
                panY = mouseY - (mouseY - panY) * (scale / prevScale);

                updateTransform();
            }});
        }}

        function zoomIn() {{
            scale = Math.min(scale * 1.25, 8.0);
            updateTransform();
        }}
        function zoomOut() {{
            scale = Math.max(scale / 1.25, 0.15);
            updateTransform();
        }}
        function resetView() {{
            renderFloorplan(floorplanData);
        }}

        // Visibility Toggles
        function toggleVisibility(type) {{
            visibility[type] = !visibility[type];
            const btn = document.getElementById('btnTog' + type.charAt(0).toUpperCase() + type.slice(1));
            if (btn) btn.classList.toggle('active', visibility[type]);

            if (type === 'rooms') document.getElementById('roomsGroup').style.display = visibility.rooms ? 'inline' : 'none';
            if (type === 'walls') document.getElementById('wallsGroup').style.display = visibility.walls ? 'inline' : 'none';
            if (type === 'doors') {{
                document.querySelectorAll('.door-elem').forEach(el => el.style.display = visibility.doors ? 'inline' : 'none');
            }}
            if (type === 'windows') {{
                document.querySelectorAll('.window-elem').forEach(el => el.style.display = visibility.windows ? 'inline' : 'none');
            }}
            if (type === 'dims') document.getElementById('dimsGroup').style.display = visibility.dims ? 'inline' : 'none';
        }}

        // Right Inspector Rooms List
        function renderRoomCards(rooms) {{
            const container = document.getElementById('roomsListContainer');
            container.innerHTML = rooms.map(r => `
                <div class="room-item-card" id="card_room_${{r.id}}" onclick="selectRoomCard('${{r.id}}')">
                    <div class="room-header-row">
                        <span>${{r.name}}</span>
                        <span class="room-area-tag">${{r.area_m2}} m²</span>
                    </div>
                    <div style="font-size: 0.72rem; color: var(--text-muted); margin-top: 0.25rem;">
                        ${{r.area_sqft}} sqft • ${{r.vertices_count}} vertices
                    </div>
                </div>
            `).join('');
        }}

        function selectRoomCard(roomId) {{
            document.querySelectorAll('.room-item-card').forEach(c => c.classList.remove('selected'));
            const card = document.getElementById('card_room_' + roomId);
            if (card) {{
                card.classList.add('selected');
                card.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
            }}

            document.querySelectorAll('.room-polygon-g polygon').forEach(p => p.setAttribute('fill-opacity', '0.25'));
            const poly = document.querySelector('#poly_room_' + roomId + ' polygon');
            if (poly) {{
                poly.setAttribute('fill-opacity', '0.65');
            }}
        }}

        function switchInspectorTab(tabId, el) {{
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            el.classList.add('active');
            document.getElementById('roomsTab').style.display = tabId === 'roomsTab' ? 'block' : 'none';
            document.getElementById('jsonTab').style.display = tabId === 'jsonTab' ? 'block' : 'none';
        }}

        // Drag and drop handler
        function setupDragAndDrop() {{
            const box = document.getElementById('canvasBox');
            const overlay = document.getElementById('dropOverlay');

            box.addEventListener('dragover', (e) => {{
                e.preventDefault();
                overlay.classList.add('active');
            }});

            overlay.addEventListener('dragleave', () => {{
                overlay.classList.remove('active');
            }});

            overlay.addEventListener('drop', (e) => {{
                e.preventDefault();
                overlay.classList.remove('active');
                if (e.dataTransfer.files && e.dataTransfer.files[0]) {{
                    loadLocalFile(e.dataTransfer.files[0]);
                }}
            }});
        }}

        function handleFileSelect(e) {{
            if (e.target.files && e.target.files[0]) {{
                loadLocalFile(e.target.files[0]);
            }}
        }}

        function loadLocalFile(file) {{
            const reader = new FileReader();
            reader.onload = (evt) => {{
                try {{
                    const rawJson = JSON.parse(evt.target.result);
                    // Dynamically parse in browser JS
                    const parsed = parseClientFloorplan(rawJson, file.name);
                    renderFloorplan(parsed);
                }} catch (err) {{
                    alert('Error parsing JSON file: ' + err.message);
                }}
            }};
            reader.readAsText(file);
        }}

        // Client-side fallback parser for client-uploaded files
        function parseClientFloorplan(data, filename) {{
            let unit = (data.unit || '').toLowerCase();
            const layers = data.layers || {{}};
            const selId = data.selectedLayer || Object.keys(layers)[0] || 'layer-1';
            const layer = layers[selId] || {{}};
            const vertices = layer.vertices || {{}};
            const lines = layer.lines || {{}};
            const holes = layer.holes || {{}};
            const areas = layer.areas || {{}};

            // Auto-detect millimeters vs centimeters
            let isMm = unit === 'mm';
            if (!isMm && unit !== 'cm') {{
                for (let lid in lines) {{
                    let props = lines[lid].properties || {{}};
                    let thick = (props.thickness && props.thickness.length) || 0;
                    let height = (props.height && props.height.length) || 0;
                    if (thick > 50 || height > 1000) {{
                        isMm = true;
                        break;
                    }}
                }}
            }}

            const scaleCm = isMm ? 0.1 : 1.0;
            const scaleM = isMm ? 0.001 : 0.01;
            unit = isMm ? 'mm' : 'cm';

            let walls = [];
            for (let lid in lines) {{
                let l = lines[lid];
                let vids = l.vertices || [];
                if (vids.length === 2 && vertices[vids[0]] && vertices[vids[1]]) {{
                    let v0 = vertices[vids[0]];
                    let v1 = vertices[vids[1]];
                    let length_cm = Math.hypot(v1.x - v0.x, v1.y - v0.y) * scaleCm;
                    walls.push({{
                        id: lid,
                        x1: v0.x,
                        y1: v0.y,
                        x2: v1.x,
                        y2: v1.y,
                        length_m: +(length_cm / 100).toFixed(2)
                    }});
                }}
            }}

            let openings = [];
            let doorsCnt = 0;
            let windowsCnt = 0;
            for (let hid in holes) {{
                let h = holes[hid];
                let l = lines[h.line];
                if (!l) continue;
                let vids = l.vertices || [];
                if (vids.length === 2 && vertices[vids[0]] && vertices[vids[1]]) {{
                    let v0 = vertices[vids[0]];
                    let v1 = vertices[vids[1]];
                    let dx = v1.x - v0.x;
                    let dy = v1.y - v0.y;
                    let L = Math.hypot(dx, dy);
                    if (L > 0) {{
                        let off = h.offset || 0.5;
                        let offRatio = off > 1.0 ? (off / L) : off;
                        offRatio = Math.max(0.0, Math.min(1.0, offRatio));
                        let cx = v0.x + offRatio * dx;
                        let cy = v0.y + offRatio * dy;
                        let w = (h.properties && h.properties.width && h.properties.width.length) || 80;
                        let halfW = w / 2;
                        let ux = dx / L;
                        let uy = dy / L;
                        let nx = -uy;
                        let ny = ux;
                        let hname = h.name || '';
                        let isDoor = hname.toLowerCase().includes('door');
                        let isWin = hname.toLowerCase().includes('window');
                        let opType = isDoor ? 'door' : (isWin ? 'window' : 'opening');
                        if (opType === 'door') doorsCnt++;
                        if (opType === 'window') windowsCnt++;

                        openings.push({{
                            id: hid,
                            name: hname,
                            type: opType,
                            width: w,
                            cx: +cx.toFixed(1),
                            cy: +cy.toFixed(1),
                            p0: {{ x: +(cx - halfW * ux).toFixed(1), y: +(cy - halfW * uy).toFixed(1) }},
                            p1: {{ x: +(cx + halfW * ux).toFixed(1), y: +(cy + halfW * uy).toFixed(1) }},
                            leaf: {{ x: +(cx - halfW * ux + w * nx).toFixed(1), y: +(cy - halfW * uy + w * ny).toFixed(1) }}
                        }});
                    }}
                }}
            }}

            let rooms = [];
            let totalM2 = 0;
            for (let aid in areas) {{
                let a = areas[aid];
                let vids = a.vertices || [];
                let poly = [];
                let polyM = [];
                vids.forEach(vid => {{
                    if (vertices[vid]) {{
                        poly.push({{ id: vid, x: vertices[vid].x, y: vertices[vid].y }});
                        polyM.push([vertices[vid].x * scaleM, vertices[vid].y * scaleM]);
                    }}
                }});

                let areaM2 = 0;
                for (let i = 0; i < polyM.length; i++) {{
                    let j = (i + 1) % polyM.length;
                    areaM2 += polyM[i][0] * polyM[j][1];
                    areaM2 -= polyM[j][0] * polyM[i][1];
                }}
                areaM2 = Math.abs(areaM2) / 2;
                totalM2 += areaM2;

                let cx = poly.length ? poly.reduce((acc, p) => acc + p.x, 0) / poly.length : 0;
                let cy = poly.length ? poly.reduce((acc, p) => acc + p.y, 0) / poly.length : 0;

                rooms.push({{
                    id: aid,
                    name: a.name || `Area_${{aid}}`,
                    area_m2: +areaM2.toFixed(2),
                    area_sqft: +(areaM2 * 10.7639).toFixed(1),
                    vertices_count: vids.length,
                    polygon: poly,
                    center: {{ x: +cx.toFixed(1), y: +cy.toFixed(1) }}
                }});
            }}

            return {{
                unit: unit,
                direction: data.direction || 'N',
                direction_angle: data.directionangle || 0,
                total_area_m2: +totalM2.toFixed(2),
                total_area_sqft: +(totalM2 * 10.7639).toFixed(1),
                rooms_count: rooms.length,
                walls_count: walls.length,
                doors_count: doorsCnt,
                windows_count: windowsCnt,
                rooms: rooms,
                walls: walls,
                openings: openings,
                raw_json: data
            }};
        }}
    </script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(
        description="Standalone Smart Wizard JSON Floorplan Visualizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python visualize_smart_wizard.py -f 1bhk_south_square_853.2sqft.json
  python visualize_smart_wizard.py -b 1BHK/Comfort_Focused_Living/1bhk_south_rect_818.9sqft.json
  python visualize_smart_wizard.py --serve
  python visualize_smart_wizard.py -f 1bhk_south_square_853.2sqft.json -o preview.html --no-browser
        """
    )
    parser.add_argument("-f", "--file", type=str, help="Path to local Smart Wizard floorplan JSON file")
    parser.add_argument("-b", "--blob", type=str, help="Azure Blob template name (e.g. 1BHK/.../....json)")
    parser.add_argument("-o", "--output", type=str, help="Output HTML file path (default: preview_floorplan.html)")
    parser.add_argument("--serve", action="store_true", help="Launch a local HTTP server with drag-and-drop support")
    parser.add_argument("--port", type=int, default=8080, help="Port for local HTTP server (default: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open the browser")

    args = parser.parse_args()

    # Case 1: Azure Blob Template
    if args.blob:
        print(f"[*] Connecting to Azure Blob Storage to fetch: '{args.blob}'...")
        try:
            from azure_blob_service import get_blob_manager
            mgr = get_blob_manager()
            resolved = mgr.find_blob(args.blob)
            raw_data = mgr.get_floorplan_json(resolved)
            title = f"Smart Wizard: {os.path.basename(resolved)}"
            print(f"[+] Successfully downloaded '{resolved}' from Azure Blob Container.")
        except Exception as e:
            print(f"[!] Error fetching blob from Azure: {e}", file=sys.stderr)
            sys.exit(1)

    # Case 2: Local File
    elif args.file:
        if not os.path.exists(args.file):
            print(f"[!] Error: File '{args.file}' not found.", file=sys.stderr)
            sys.exit(1)
        print(f"[*] Loading local floorplan JSON: '{args.file}'...")
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            title = f"Smart Wizard: {os.path.basename(args.file)}"
        except Exception as e:
            print(f"[!] Error reading JSON file: {e}", file=sys.stderr)
            sys.exit(1)

    # Case 3: Default fallback to any local sample JSON if available, or empty visualizer
    else:
        sample_file = "1bhk_south_square_853.2sqft.json"
        if os.path.exists(sample_file):
            print(f"[*] No input specified. Using sample: '{sample_file}' (Drop any other file in browser)")
            with open(sample_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            title = f"Smart Wizard: {sample_file}"
        else:
            raw_data = {"unit": "cm", "direction": "N", "layers": {}}
            title = "Smart Wizard Floorplan Visualizer"

    # Parse and process
    parsed = parse_smart_wizard_json(raw_data)

    print("\n" + "=" * 65)
    print("             SMART WIZARD FLOORPLAN SUMMARY")
    print("=" * 65)
    print(f"  • Title / File       : {title}")
    print(f"  • Total Surface Area : {parsed['total_area_m2']} m² ({parsed['total_area_sqft']} sqft)")
    print(f"  • Orientation (North): {parsed['direction']} (Angle: {parsed['direction_angle']}°)")
    print(f"  • Total Rooms        : {parsed['rooms_count']}")
    print(f"  • Structural Walls   : {parsed['walls_count']}")
    print(f"  • Doors Count        : {parsed['doors_count']} 🚪")
    print(f"  • Windows Count      : {parsed['windows_count']} 🪟")
    print("-" * 65)
    print("  Rooms Breakdown:")
    for r in parsed["rooms"]:
        print(f"    - {r['name']:<22} : {r['area_m2']:>6.2f} m² ({r['area_sqft']:>6.1f} sqft)")
    print("=" * 65 + "\n")

    # Generate HTML
    html_content = generate_visualizer_html(parsed, title=title)
    out_file = args.output or "preview_floorplan.html"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"[+] Visualizer HTML generated at: {os.path.abspath(out_file)}")

    # Serve or open
    if args.serve:
        class CustomHandler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/" or self.path.startswith("/?"):
                    self.send_response(200)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_content.encode("utf-8"))
                else:
                    super().do_GET()

        port = args.port
        print(f"[*] Starting local visualizer server at: http://localhost:{port}")
        server = socketserver.TCPServer(("", port), CustomHandler)

        if not args.no_browser:
            threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{port}")).start()

        try:
            print("[*] Serving visualizer... Press Ctrl+C to stop.")
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[*] Server stopped.")
            server.server_close()
    else:
        if not args.no_browser:
            full_path = "file://" + os.path.abspath(out_file).replace("\\", "/")
            print(f"[*] Opening in browser: {full_path}")
            webbrowser.open(full_path)


if __name__ == "__main__":
    main()
