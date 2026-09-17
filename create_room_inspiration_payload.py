#!/usr/bin/env python3
"""
create_room_inspiration_payload.py

Step 1 – Generate Room JSON Pipeline

Flow:
style_id + room_type -> find matching room -> validate style & area range -> extract room data -> populate room_json -> return payload

Output Structure:
{
  "room_json": {
    "...": "complete matching room data"
  },
  "style_id": 214,
  "room_type": "Bedroom",
  "apply_door_model": true,
  "apply_window_model": true,
  "apply_floor": true,
  "apply_ceiling": true,
  "apply_wall": true
}
"""

import os
import json
import math
import sys
import argparse
import copy
from typing import Dict, Any, List, Tuple, Optional, Union
from datetime import datetime

# Mapping of common floorplan room names/aliases to style rule categories
ROOM_TYPE_ALIASES: Dict[str, str] = {
    'toilet': 'Bathroom',
    'restroom': 'Bathroom',
    'washroom': 'Bathroom',
    'powder room': 'Bathroom',
    'powder': 'Bathroom',
    'bath': 'Bathroom',
    'bathroom': 'Bathroom',
    'm.bath': 'Bathroom',
    'mbath': 'Bathroom',
    'bath-1': 'Bathroom',
    'bath-2': 'Bathroom',
    'bath-3': 'Bathroom',
    'bath-4': 'Bathroom',
    'bath 1': 'Bathroom',
    'bath 2': 'Bathroom',
    'bath 3': 'Bathroom',
    'bath 4': 'Bathroom',
    'shower': 'Bathroom',
    'custom shower': 'Bathroom',
    'closet': 'Bathroom',
    'm.closet': 'Bathroom',
    'w.i.c': 'Bathroom',
    'wic': 'Bathroom',
    'walk-in closet': 'Bathroom',
    'walk in closet': 'Bathroom',
    'wardrobe': 'Bathroom',
    'dress': 'Bathroom',
    'dressing': 'Bathroom',
    'dressing room': 'Bathroom',
    'living room': 'Living Room',
    'livingroom': 'Living Room',
    'living': 'Living Room',
    'hall': 'Living Room',
    'great room': 'Living Room',
    'greatroom': 'Living Room',
    'family room': 'Living Room',
    'family': 'Living Room',
    'drawing room': 'Living Room',
    'drawing': 'Living Room',
    'foyer': 'Living Room',
    'lounge': 'Living Room',
    'sitout': 'Living Room',
    'balcony': 'Living Room',
    'terrace': 'Living Room',
    'porch': 'Living Room',
    'rear porch': 'Living Room',
    'front porch': 'Living Room',
    'bedroom': 'Bedroom',
    'master bedroom': 'Bedroom',
    'master bed': 'Bedroom',
    'bed room': 'Bedroom',
    'bed': 'Bedroom',
    'br': 'Bedroom',
    'bedroom- 1': 'Bedroom',
    'bedroom- 2': 'Bedroom',
    'bedroom- 3': 'Bedroom',
    'bedroom- 4': 'Bedroom',
    'bedroom-1': 'Bedroom',
    'bedroom-2': 'Bedroom',
    'bedroom-3': 'Bedroom',
    'bedroom-4': 'Bedroom',
    'bedroom 1': 'Bedroom',
    'bedroom 2': 'Bedroom',
    'bedroom 3': 'Bedroom',
    'bedroom 4': 'Bedroom',
    'bed 1': 'Bedroom',
    'bed 2': 'Bedroom',
    'bed 3': 'Bedroom',
    'guest room': 'Bedroom',
    'kids bedroom': 'Bedroom',
    'children room': 'Bedroom',
    'kitchen': 'Kitchen',
    'modular kitchen': 'Kitchen',
    'pantry': 'Kitchen',
    'utility': 'Kitchen',
    'utility room': 'Kitchen',
    'mud room': 'Kitchen',
    'mudroom': 'Kitchen',
    'store': 'Kitchen',
    'storage': 'Kitchen',
    'dining': 'Dining Rooms',
    'dining room': 'Dining Rooms',
    'dining rooms': 'Dining Rooms',
    'office': 'Office Room',
    'office room': 'Office Room',
    'study': 'Office Room',
    'study room': 'Office Room',
    'work shop': 'Office Room',
    'workshop': 'Office Room',
    'laundry': 'Laundry Room',
    'laundry room': 'Laundry Room',
    'pooja': 'Laundry Room',
    'pooja room': 'Laundry Room'
}

def calculate_polygon_area(coords: List[Tuple[float, float]]) -> float:
    """
    Calculates the 2D polygon area using the Shoelace formula (Gauss's area formula).
    
    Area = 0.5 * |sum_{i=0}^{n-1} (x_i * y_{i+1} - x_{i+1} * y_i)|
    """
    n = len(coords)
    if n < 3:
        return 0.0
    
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += coords[i][0] * coords[j][1]
        area -= coords[j][0] * coords[i][1]
        
    return abs(area) / 2.0

def calculate_room_area_m2(
    vertices_dict: Dict[str, Any],
    area_vertex_ids: List[str],
    scale_to_cm: float = 1.0
) -> float:
    """
    Calculates room area in m² directly from the room's vertex coordinates.
    Standard Smart Wizard coordinates are in centimeters (1 cm² = 0.0001 m²).
    """
    coords: List[Tuple[float, float]] = []
    for vid in area_vertex_ids:
        if vid in vertices_dict:
            v = vertices_dict[vid]
            coords.append((v['x'] * scale_to_cm, v['y'] * scale_to_cm))

    if len(coords) < 3:
        return 0.0

    area_cm2 = calculate_polygon_area(coords)
    area_m2 = area_cm2 / 10000.0  # 10,000 cm² = 1 m²
    return round(area_m2, 2)

def identify_room_type(area: Dict[str, Any], custom_room_type: Optional[str] = None) -> Tuple[str, str]:
    """
    Identifies the room type from area properties, name, or custom override.
    Returns:
        (normalized_room_type, raw_room_name)
    """
    if custom_room_type and custom_room_type.strip():
        raw = custom_room_type.strip()
    else:
        props = area.get('properties', {})
        raw = (props.get('label') or props.get('type') or area.get('name') or "Bedroom").strip()

    normalized = ROOM_TYPE_ALIASES.get(raw.lower(), raw)
    return normalized, raw

# User-specified allowed user_style_ids (styles with min 5 to max 100)
ALLOWED_USER_STYLE_IDS: List[int] = [
    237, 238, 239, 243, 249, 250, 251, 252, 253, 254, 255, 256, 257, 258,
    27, 28, 29, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 46, 47, 48
]

def load_style_rules(
    rules_source: Union[str, List[Dict[str, Any]], Dict[str, Any]],
    only_public: bool = True,
    allowed_style_ids: Optional[List[int]] = None
) -> List[Dict[str, Any]]:
    """
    Reads style rules from a JSON file or in-memory data structure.
    By default filters to only styles where is_public == True and style is in allowed_style_ids.
    """
    if isinstance(rules_source, str):
        if not os.path.exists(rules_source):
            # Auto-fallback to any response_*.json in directory
            candidates = [f for f in os.listdir('.') if f.startswith('response_') and f.endswith('.json')]
            if candidates:
                rules_source = candidates[0]
            else:
                raise FileNotFoundError(f"Style rules file not found: {rules_source}")
        with open(rules_source, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = rules_source

    if isinstance(data, dict):
        if "styles" in data:
            data = data["styles"]
        elif "data" in data:
            data = data["data"]
        else:
            data = [data]

    if not isinstance(data, list):
        raise ValueError("Style rules format unrecognized: expected a list of styles.")

    if only_public:
        data = [s for s in data if s.get('is_public') is True]

    # Filter to only the allowed style IDs (defaulting to ALLOWED_USER_STYLE_IDS)
    target_ids = allowed_style_ids if allowed_style_ids is not None else ALLOWED_USER_STYLE_IDS
    if target_ids is not None:
        target_set = set(target_ids)
        data = [s for s in data if (s.get('user_style_id') in target_set or s.get('id') in target_set)]

    return data

def find_matching_room(
    layers: Dict[str, Any],
    selected_layer_id: str,
    target_room_type: str,
    area_id: Optional[str] = None
) -> Tuple[str, Dict[str, Any]]:
    """
    Finds the matching room in the Smart Wizard floorplan JSON using room_type.
    """
    layer = layers.get(selected_layer_id, {})
    areas = layer.get('areas', {})
    if not areas:
        raise ValueError(f"No areas found in layer '{selected_layer_id}'.")

    # If area_id is explicitly specified, verify and return it
    if area_id:
        if area_id not in areas:
            raise ValueError(f"Specified area_id '{area_id}' not found in layer.")
        return area_id, areas[area_id]

    target_norm = ROOM_TYPE_ALIASES.get(target_room_type.lower(), target_room_type.lower())

    # Search for matching room by type
    matched_areas: List[Tuple[str, Dict[str, Any]]] = []
    for aid, area in areas.items():
        norm_type, raw_name = identify_room_type(area)
        if (norm_type.lower() == target_norm or
            raw_name.lower() == target_room_type.lower() or
            norm_type.lower() == target_room_type.lower()):
            matched_areas.append((aid, area))

    if not matched_areas:
        available = [f"{aid} ({a.get('name') or a.get('properties', {}).get('label') or 'Unnamed'})" for aid, a in areas.items()]
        raise ValueError(
            f"No room matching room_type '{target_room_type}' found in floorplan.\nAvailable areas: {', '.join(available)}"
        )

    # Return the first matching room
    return matched_areas[0]

def validate_style_id_for_room(
    style_id: int,
    room_type: str,
    area_m2: float,
    styles: List[Dict[str, Any]]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Validates the style_id for the given room_type and its area range.
    Returns:
        (matched_style, matched_rule)
    Raises ValueError if invalid.
    """
    # 1. Find style by user_style_id (preferred) or internal id (fallback)
    matched_style = None
    for s in styles:
        if s.get('user_style_id') == style_id:
            matched_style = s
            break

    if not matched_style:
        for s in styles:
            s_id = s.get('id') if s.get('id') is not None else s.get('style_id')
            if s_id == style_id:
                matched_style = s
                break

    if not matched_style:
        raise ValueError(f"Style ID {style_id} not found in provided style rules.")

    if matched_style.get('is_public') is not True:
        raise ValueError(f"Style ID {style_id} ('{matched_style.get('name')}') is not public (is_public != True). Only public styles are allowed.")

    # 2. Check room_type match in style rules
    target_norm = ROOM_TYPE_ALIASES.get(room_type.lower(), room_type.lower())
    rules = matched_style.get('rules', [])
    matched_rule = None

    for rule in rules:
        rule_rt = (rule.get('room_type') or '').strip()
        rule_norm = ROOM_TYPE_ALIASES.get(rule_rt.lower(), rule_rt.lower())
        if rule_rt.lower() == target_norm or rule_norm == target_norm or rule_rt.lower() == room_type.lower():
            matched_rule = rule
            break

    if not matched_rule:
        supported = [r.get('room_type') for r in rules]
        raise ValueError(
            f"Style ID {style_id} ('{matched_style.get('name')}') does not support room type '{room_type}'. Supported room types: {supported}"
        )

    # 3. Check area range
    min_area = matched_rule.get('min_area_m2', 0)
    max_area = matched_rule.get('max_area_m2', float('inf'))

    if not (min_area <= area_m2 <= max_area):
        raise ValueError(
            f"Room area {area_m2} m2 is outside the valid range [{min_area} - {max_area}] m2 for Style ID {style_id} ('{matched_style.get('name')}') and room type '{room_type}'."
        )

    return matched_style, matched_rule

def is_point_in_polygon(point: Dict[str, float], polygon: List[Dict[str, float]]) -> bool:
    """
    Ray-casting algorithm to determine if a point (x, y) is inside a polygon.
    """
    x, y = point['x'], point['y']
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]['x'], polygon[i]['y']
        xj, yj = polygon[j]['x'], polygon[j]['y']
        
        intersect = ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) if (yj - yi) != 0 else 1e-9) + xi)
        if intersect:
            inside = not inside
        j = i
        
    return inside

def round_floats(obj: Any, decimals: int = 3) -> Any:
    """
    Recursively round float values in a dictionary/list structure to fixed decimals.
    """
    if isinstance(obj, float):
        return round(obj, decimals)
    elif isinstance(obj, dict):
        return {k: round_floats(v, decimals) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [round_floats(v, decimals) for v in obj]
    return obj

def scale_layer_data(layer: Dict[str, Any], scale: float = 0.1, convert_rotation: bool = True) -> Dict[str, Any]:
    """
    Scales physical dimensions and coordinates of a layer by scale factor (e.g., 0.1 for mm -> cm).
    """
    scaled_layer = copy.deepcopy(layer)
    
    # 1. Altitude
    if 'altitude' in scaled_layer:
        scaled_layer['altitude'] *= scale

    # 2. Vertices
    vertices = scaled_layer.get('vertices', {})
    for v in vertices.values():
        if 'x' in v: v['x'] *= scale
        if 'y' in v: v['y'] *= scale

    # 3. Lines / Walls
    lines = scaled_layer.get('lines', {})
    for wall in lines.values():
        props = wall.get('properties', {})
        if 'height' in props and 'length' in props['height']:
            props['height']['length'] *= scale
        if 'thickness' in props and 'length' in props['thickness']:
            props['thickness']['length'] *= scale

    # 4. Areas
    areas = scaled_layer.get('areas', {})
    for area in areas.values():
        props = area.get('properties', {})
        if 'height' in props and 'length' in props['height']:
            props['height']['length'] *= scale
        if 'floor_properties' in area:
            fp = area['floor_properties']
            if 'thickness' in fp and isinstance(fp['thickness'], (int, float)):
                fp['thickness'] *= scale
            if 'height' in fp and isinstance(fp['height'], (int, float)):
                fp['height'] *= scale
        if 'ceiling_properties' in area:
            cp = area['ceiling_properties']
            if 'thickness' in cp and isinstance(cp['thickness'], (int, float)):
                cp['thickness'] *= scale
            if 'height' in cp and isinstance(cp['height'], (int, float)):
                cp['height'] *= scale

    # 5. Items
    items = scaled_layer.get('items', {})
    for item in items.values():
        if 'x' in item: item['x'] *= scale
        if 'y' in item: item['y'] *= scale
        if convert_rotation and 'rotation' in item and isinstance(item['rotation'], (int, float)):
            if abs(item['rotation']) > 2 * math.pi:
                item['rotation'] = (item['rotation'] * math.pi) / 180.0
        iprops = item.get('properties', {})
        for dim in ['width', 'depth', 'height', 'altitude']:
            if dim in iprops and isinstance(iprops[dim], dict) and 'length' in iprops[dim]:
                iprops[dim]['length'] *= scale

    # 6. Holes (Doors / Windows)
    holes = scaled_layer.get('holes', {})
    for hole in holes.values():
        offset = hole.get('offset', 0)
        line_id = hole.get('line')
        if offset <= 1.0 and line_id and line_id in lines:
            wall = lines[line_id]
            v_ids = wall.get('vertices', [])
            if len(v_ids) == 2 and v_ids[0] in vertices and v_ids[1] in vertices:
                v0 = vertices[v_ids[0]]
                v1 = vertices[v_ids[1]]
                wall_len = math.hypot(v1['x'] - v0['x'], v1['y'] - v0['y'])
                hole['offset'] = offset * wall_len
        else:
            hole['offset'] = offset * scale

        hprops = hole.get('properties', {})
        for dim in ['width', 'height', 'altitude', 'thickness']:
            if dim in hprops and isinstance(hprops[dim], dict) and 'length' in hprops[dim]:
                hprops[dim]['length'] *= scale

    return scaled_layer

def extract_target_room_data(layer: Dict[str, Any], target_area_id: str) -> Dict[str, Any]:
    """
    Extracts enclosing vertices, lines, holes, and existing items associated with target_area_id.
    """
    areas = layer.get('areas', {})
    vertices_dict = layer.get('vertices', {})
    lines_dict = layer.get('lines', {})
    holes_dict = layer.get('holes', {})
    items_dict = layer.get('items', {})

    target_area = areas.get(target_area_id)
    if not target_area:
        raise ValueError(f"Area ID '{target_area_id}' not found in layer. Available areas: {list(areas.keys())}")

    area_vert_ids = target_area.get('vertices', [])
    filtered_vertices = {}
    filtered_lines = {}
    filtered_holes = {}
    filtered_items = {}

    min_x, max_x = float('inf'), float('-inf')
    min_y, max_y = float('inf'), float('-inf')

    for v_id in area_vert_ids:
        if v_id in vertices_dict:
            v = vertices_dict[v_id]
            filtered_vertices[v_id] = v
            min_x = min(min_x, v['x'])
            max_x = max(max_x, v['x'])
            min_y = min(min_y, v['y'])
            max_y = max(max_y, v['y'])

    tolerance = 5.0
    lines_to_include = set()

    # 1. Enclosing walls matching vertices
    for l_id, line in lines_dict.items():
        v_ids = line.get('vertices', [])
        if len(v_ids) == 2:
            has_v1 = v_ids[0] in area_vert_ids
            has_v2 = v_ids[1] in area_vert_ids
            if has_v1 and has_v2:
                lines_to_include.add(l_id)
            elif has_v1 or has_v2:
                v1 = vertices_dict.get(v_ids[0])
                v2 = vertices_dict.get(v_ids[1])
                if v1 and v2:
                    mid_x = (v1['x'] + v2['x']) / 2.0
                    mid_y = (v1['y'] + v2['y']) / 2.0
                    if (min_x - tolerance <= mid_x <= max_x + tolerance and
                        min_y - tolerance <= mid_y <= max_y + tolerance):
                        lines_to_include.add(l_id)

    # 2. Wall hosting area vertices
    for v_id in area_vert_ids:
        v = vertices_dict.get(v_id, {})
        hosted = v.get('hostedOn', {})
        if hosted and 'hostLineId' in hosted:
            lines_to_include.add(hosted['hostLineId'])

    # 3. Collect lines, vertices, and holes
    for l_id in lines_to_include:
        line = lines_dict.get(l_id)
        if line:
            filtered_lines[l_id] = line
            for v_id in line.get('vertices', []):
                if v_id in vertices_dict and v_id not in filtered_vertices:
                    filtered_vertices[v_id] = vertices_dict[v_id]
            
            for h_id, hole in holes_dict.items():
                if hole.get('line') == l_id:
                    filtered_holes[h_id] = hole

    # 4. Spatial containment check for items
    polygon_points = []
    for v_id in area_vert_ids:
        if v_id in vertices_dict:
            polygon_points.append({'x': vertices_dict[v_id]['x'], 'y': vertices_dict[v_id]['y']})

    for i_id, item in items_dict.items():
        item_pos = {'x': item.get('x', 0), 'y': item.get('y', 0)}
        if is_point_in_polygon(item_pos, polygon_points) or item.get('areaId') == target_area_id:
            filtered_items[i_id] = item

    return {
        'vertices': filtered_vertices,
        'lines': filtered_lines,
        'holes': filtered_holes,
        'items': filtered_items,
        'area': target_area
    }

def generate_room_json(
    floorplan_data: Dict[str, Any],
    style_id: int,
    room_type: str,
    rules_source: Union[str, List[Dict[str, Any]], Dict[str, Any]],
    area_id: Optional[str] = None,
    apply_door_model: bool = True,
    apply_window_model: bool = True,
    apply_floor: bool = True,
    apply_ceiling: bool = True,
    apply_wall: bool = True,
    force_scale: Optional[float] = None
) -> Dict[str, Any]:
    """
    Executes Step 1 – Generate Room JSON:
    1. Takes style_id and room_type.
    2. Finds the matching room in the floorplan using room_type.
    3. Validates the style_id for that room and its area range.
    4. Extracts the complete matching room data from the floorplan.
    5. Puts only that room data inside room_json.
    6. Keeps style_id, room_type, and all apply_* values unchanged.
    7. Does not create another nested room_json.
    """
    # 1. Resolve floorplan root data
    if "processed_house_json" in floorplan_data:
        floorplan_root = floorplan_data["processed_house_json"]
    elif "room_json" in floorplan_data:
        floorplan_root = floorplan_data["room_json"]
    elif "house_json" in floorplan_data:
        floorplan_root = floorplan_data["house_json"]
    else:
        floorplan_root = floorplan_data

    layers = floorplan_root.get('layers', {})
    if not layers:
        raise ValueError("No 'layers' key found in floorplan JSON.")

    selected_layer_id = floorplan_root.get('selectedLayer') or list(layers.keys())[0]
    layer = layers[selected_layer_id]

    # 2. Find matching room in floorplan using room_type
    matched_area_id, matched_area = find_matching_room(
        layers=layers,
        selected_layer_id=selected_layer_id,
        target_room_type=room_type,
        area_id=area_id
    )

    # Unit scaling detection (auto-detect mm if unit == 'mm' or wall thickness > 50 or height > 1000)
    current_unit = floorplan_root.get('unit', 'cm').lower()
    is_mm = current_unit == 'mm'
    if not is_mm and force_scale is None:
        lines_dict = layer.get('lines', {})
        for lid, linfo in lines_dict.items():
            props = linfo.get("properties", {})
            thick = props.get("thickness", {}).get("length", 0) if isinstance(props.get("thickness"), dict) else 0
            height = props.get("height", {}).get("length", 0) if isinstance(props.get("height"), dict) else 0
            if thick > 50 or height > 1000:
                is_mm = True
                break
    scale_factor = force_scale if force_scale is not None else (0.1 if is_mm else 1.0)

    # Calculate room area in m²
    area_vertex_ids = matched_area.get('vertices', [])
    area_m2 = calculate_room_area_m2(
        layer.get('vertices', {}),
        area_vertex_ids,
        scale_to_cm=scale_factor
    )

    # 3. Validate style_id for that room and its area range
    styles = load_style_rules(rules_source)
    matched_style, matched_rule = validate_style_id_for_room(
        style_id=style_id,
        room_type=room_type,
        area_m2=area_m2,
        styles=styles
    )

    # Style ID is taken from user_style_id from the style rules
    user_style_id = matched_style.get('user_style_id')
    effective_style_id = user_style_id if user_style_id is not None else matched_style.get('id', style_id)

    print(f"[+] [Validation Passed]")
    print(f"    - Matched Area ID : {matched_area_id}")
    print(f"    - Room Type       : {room_type}")
    print(f"    - Calculated Area : {area_m2} m2")
    print(f"    - User Style ID   : {effective_style_id} ('{matched_style.get('name')}')")
    print(f"    - Allowed Range   : [{matched_rule.get('min_area_m2')} - {matched_rule.get('max_area_m2')}] m2")

    # 4. Extract complete matching room data from the floorplan
    processed_layer = scale_layer_data(layer, scale=scale_factor) if scale_factor != 1.0 else copy.deepcopy(layer)
    extracted = extract_target_room_data(processed_layer, matched_area_id)

    # Identify Smart Wizard raw name
    _, raw_room_name = identify_room_type(matched_area)
    style_name = matched_style.get('name', f"Style {effective_style_id}")

    # 5. Build room_json containing ONLY that matching room's data
    room_json = {
        "unit": "cm",
        "name": floorplan_root.get('name', 'RoomExport'),
        "layers": {
            selected_layer_id: {
                **processed_layer,
                "vertices": round_floats(extracted['vertices']),
                "lines": extracted['lines'],
                "holes": extracted['holes'],
                "items": extracted['items'],
                "areas": {
                    matched_area_id: extracted['area']
                }
            }
        },
        "grids": floorplan_root.get('grids', {}),
        "selectedLayer": selected_layer_id,
        "groups": floorplan_root.get('groups', {}),
        "width": floorplan_root.get('width', 4000),
        "height": floorplan_root.get('height', 4000),
        "meta": {
            "export_timestamp": datetime.now().isoformat(),
            "format_version": "2.1",
            "pipeline_step": "Step 1 - Generate Room JSON",
            "area_m2": area_m2,
            "smart_wizard_room_name": raw_room_name,
            "smart_wizard_nominal_type": room_type,
            "smart_wizard_area_id": matched_area_id,
            "style_name": style_name
        },
        "guides": floorplan_root.get('guides', {"horizontal": {}, "vertical": {}, "circular": {}})
    }

    # 6 & 7. Final Output structure (no duplicate or nested room_json)
    payload = {
        "room_json": room_json,
        "style_id": effective_style_id,
        "room_type": room_type,
        "apply_door_model": apply_door_model,
        "apply_window_model": apply_window_model,
        "apply_floor": apply_floor,
        "apply_ceiling": apply_ceiling,
        "apply_wall": apply_wall
    }

    return payload

def find_matching_style_for_room(
    room_type: str,
    area_m2: float,
    styles: List[Dict[str, Any]],
    preferred_style_id: Optional[int] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Finds a valid matching style from style rules based on room area size (m2),
    ensuring maximum rooms can receive an inspiration even if the room name is
    non-standard (e.g. 'CLOSET', 'BEDROOM- 2', 'Room', 'M.BATH', 'GREAT ROOM').
    
    Returns (matched_style, matched_rule).
    """
    target_norm = ROOM_TYPE_ALIASES.get(room_type.lower(), room_type.lower())
    all_candidates: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    # 1. First attempt: match by normalized room type AND area size
    for s in styles:
        if s.get('is_public') is not True:
            continue
        rules = s.get('rules', [])
        for rule in rules:
            rule_rt = (rule.get('room_type') or '').strip()
            rule_norm = ROOM_TYPE_ALIASES.get(rule_rt.lower(), rule_rt.lower())
            if rule_rt.lower() == target_norm or rule_norm == target_norm or rule_rt.lower() == room_type.lower():
                min_a = rule.get('min_area_m2', 0)
                max_a = rule.get('max_area_m2', float('inf'))
                if min_a <= area_m2 <= max_a:
                    all_candidates.append((s, rule))

    # 2. Second attempt: Check partial keywords/hints in the room name
    if not all_candidates:
        nl = room_type.lower()
        hint_rt = None
        if any(k in nl for k in ['bed', 'master bed', 'br']):
            hint_rt = 'bedroom'
        elif any(k in nl for k in ['bath', 'toilet', 'wash', 'powder', 'closet', 'shower']):
            hint_rt = 'bathroom'
        elif any(k in nl for k in ['kitchen', 'pantry', 'cook']):
            hint_rt = 'kitchen'
        elif any(k in nl for k in ['living', 'great', 'hall', 'drawing', 'foyer', 'family', 'lounge', 'porch']):
            hint_rt = 'living room'

        if hint_rt:
            for s in styles:
                if s.get('is_public') is not True:
                    continue
                for rule in s.get('rules', []):
                    rule_rt = (rule.get('room_type') or '').strip().lower()
                    if rule_rt == hint_rt:
                        min_a = max(1.5, rule.get('min_area_m2', 0))
                        max_a = rule.get('max_area_m2', float('inf'))
                        if min_a <= area_m2 <= max_a:
                            all_candidates.append((s, rule))

    # 3. Third attempt: Pure area size matching (don't depend on room name, apply to maximum rooms)
    if not all_candidates:
        area_candidates = []
        for s in styles:
            if s.get('is_public') is not True:
                continue
            for rule in s.get('rules', []):
                min_a = max(1.5, rule.get('min_area_m2', 0))
                max_a = rule.get('max_area_m2', float('inf'))
                if min_a <= area_m2 <= max_a:
                    area_candidates.append((s, rule))

        if area_candidates:
            if area_m2 < 5.0:
                size_order = ['bathroom', 'kitchen', 'living room', 'bedroom']
            elif area_m2 < 10.0:
                size_order = ['bedroom', 'kitchen', 'bathroom', 'living room']
            elif area_m2 < 25.0:
                size_order = ['bedroom', 'living room', 'kitchen', 'bathroom']
            else:
                size_order = ['living room', 'bedroom', 'kitchen', 'bathroom']

            for target_bracket in size_order:
                bracket_matches = [
                    (s, r) for s, r in area_candidates
                    if (r.get('room_type') or '').strip().lower() == target_bracket
                ]
                if bracket_matches:
                    all_candidates = bracket_matches
                    break
            
            if not all_candidates:
                all_candidates = area_candidates

    if not all_candidates:
        raise ValueError(f"No style rule can fit room area {area_m2:.2f} m2 (room name '{room_type}').")

    if preferred_style_id is not None:
        for s, r in all_candidates:
            if s.get('user_style_id') == preferred_style_id or s.get('id') == preferred_style_id:
                return s, r

    # Prioritize based on the order of ALLOWED_USER_STYLE_IDS
    for allowed_id in ALLOWED_USER_STYLE_IDS:
        for s, r in all_candidates:
            if s.get('user_style_id') == allowed_id or s.get('id') == allowed_id:
                return s, r

    return all_candidates[0]

def generate_all_room_payloads(
    floorplan_data: Dict[str, Any],
    rules_source: Union[str, List[Dict[str, Any]], Dict[str, Any]],
    output_dir: Optional[str] = None,
    apply_door_model: bool = True,
    apply_window_model: bool = True,
    apply_floor: bool = True,
    apply_ceiling: bool = True,
    apply_wall: bool = True,
    force_scale: Optional[float] = None,
    style_overrides: Optional[Dict[str, int]] = None
) -> Dict[str, Dict[str, Any]]:
    """
    Generates separate room-level JSON payloads for each room in the floorplan.
    If output_dir is provided, saves each payload into its own JSON file.
    Otherwise, returns all room payloads in-memory.
    """
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    if "processed_house_json" in floorplan_data:
        floorplan_root = floorplan_data["processed_house_json"]
    elif "room_json" in floorplan_data:
        floorplan_root = floorplan_data["room_json"]
    elif "house_json" in floorplan_data:
        floorplan_root = floorplan_data["house_json"]
    else:
        floorplan_root = floorplan_data

    layers = floorplan_root.get('layers', {})
    selected_layer_id = floorplan_root.get('selectedLayer') or list(layers.keys())[0]
    layer = layers[selected_layer_id]
    areas = layer.get('areas', {})

    styles = load_style_rules(rules_source)
    all_results = {}
    used_filenames = set()

    if output_dir:
        print(f"[*] Generating separate room-level payloads for {len(areas)} rooms into: {output_dir}/")

    # Unit scaling detection (auto-detect mm if unit == 'mm' or wall thickness > 50 or height > 1000)
    current_unit = floorplan_root.get('unit', 'cm').lower()
    is_mm = current_unit == 'mm'
    if not is_mm and force_scale is None:
        lines_dict = layer.get('lines', {})
        for lid, linfo in lines_dict.items():
            props = linfo.get("properties", {})
            thick = props.get("thickness", {}).get("length", 0) if isinstance(props.get("thickness"), dict) else 0
            height = props.get("height", {}).get("length", 0) if isinstance(props.get("height"), dict) else 0
            if thick > 50 or height > 1000:
                is_mm = True
                break
    scale_factor = force_scale if force_scale is not None else (0.1 if is_mm else 1.0)

    for aid, area in areas.items():
        norm_type, raw_name = identify_room_type(area)
        area_m2 = calculate_room_area_m2(layer.get('vertices', {}), area.get('vertices', []), scale_to_cm=scale_factor)

        # Style selection: match by area size (with name hints if available)
        pref_id = (style_overrides or {}).get(aid) or (style_overrides or {}).get(norm_type)
        try:
            matched_style, matched_rule = find_matching_style_for_room(norm_type, area_m2, styles, preferred_style_id=pref_id)
        except ValueError as e:
            print(f"[-] Skipping area '{raw_name}' ({aid}): {e}")
            continue

        user_style_id = matched_style.get('user_style_id')
        effective_style_id = user_style_id if user_style_id is not None else matched_style.get('id')
        effective_room_type = matched_rule.get('room_type') or norm_type

        # Generate payload with matching room data
        payload = generate_room_json(
            floorplan_data=floorplan_data,
            style_id=effective_style_id,
            room_type=effective_room_type,
            rules_source=styles,
            area_id=aid,
            apply_door_model=apply_door_model,
            apply_window_model=apply_window_model,
            apply_floor=apply_floor,
            apply_ceiling=apply_ceiling,
            apply_wall=apply_wall,
            force_scale=force_scale
        )

        base_slug = "".join(c if (c.isalnum() or c in ('_', '-')) else '_' for c in raw_name.lower().strip()).strip('_')
        if not base_slug:
            base_slug = "room"
        if base_slug in used_filenames:
            filename = f"{base_slug}_{aid}.json"
        else:
            filename = f"{base_slug}.json"
        used_filenames.add(base_slug)

        if output_dir:
            filepath = os.path.join(output_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
            print(f"[+] Room '{raw_name}' ({effective_room_type}) -> style_id {effective_style_id} saved to: {filepath}")

        all_results[aid] = payload

    return all_results

def main():
    parser = argparse.ArgumentParser(
        description="Step 1: Generate Room JSON (style_id + room_type -> find room -> validate -> room_json)"
    )
    parser.add_argument("-i", "--input", default="1bhk_south_square_853.2sqft.json", help="Input floorplan JSON file path")
    parser.add_argument("-b", "--blob", help="Azure Blob template path, filename, search keyword, or index number (loads directly from Azure Blob Storage)")
    parser.add_argument("--list-blobs", action="store_true", help="List available floorplan templates in Azure Blob Storage")
    parser.add_argument("-r", "--rules", default="response_1789025921629.json", help="Input style rules JSON file path")
    parser.add_argument("-p", "--payload", help="Input request payload JSON file containing style_id, room_type, and apply_* flags")
    parser.add_argument("-s", "--style-id", type=int, default=237, help="User Style ID (default: 237)")
    parser.add_argument("-rt", "--room-type", default="Bedroom", help="Target Room Type (default: 'Bedroom')")
    parser.add_argument("-a", "--area-id", help="Target Area ID (optional, to disambiguate if multiple rooms of same type)")
    parser.add_argument("-o", "--output", default="room_level_payload.json", help="Output JSON file path")
    parser.add_argument("--output-dir", help="Directory to save separate room JSON files for all rooms")
    parser.add_argument("--all-rooms", action="store_true", help="Generate separate payloads for all rooms into output directory")
    parser.add_argument("--scale", type=float, help="Manual unit scale factor (e.g. 0.1 for mm->cm)")
    parser.add_argument("--apply-door-model", type=lambda x: (str(x).lower() == 'true'), default=True)
    parser.add_argument("--apply-window-model", type=lambda x: (str(x).lower() == 'true'), default=True)
    parser.add_argument("--apply-floor", type=lambda x: (str(x).lower() == 'true'), default=True)
    parser.add_argument("--apply-ceiling", type=lambda x: (str(x).lower() == 'true'), default=True)
    parser.add_argument("--apply-wall", type=lambda x: (str(x).lower() == 'true'), default=True)

    parser.add_argument("--send-to-api", action="store_true", help="Send generated payload(s) to inspiration endpoint after creation")

    args = parser.parse_args()

    if args.list_blobs:
        from azure_blob_service import get_blob_manager
        manager = get_blob_manager()
        templates = manager.list_floorplans()
        print(f"[*] Found {len(templates)} templates in Azure Blob container '{manager.container_name}':")
        for i, t in enumerate(templates, 1):
            print(f"  [{i:<3}] {t['category']:<8} {t['name']}")
        return

    if args.blob:
        from azure_blob_service import get_blob_manager
        manager = get_blob_manager()
        resolved_blob = manager.find_blob(args.blob)
        print(f"[*] [Step 1] Loading floorplan from Azure Blob: {resolved_blob}")
        fp_data = manager.get_floorplan_json(resolved_blob)
    else:
        print(f"[*] [Step 1] Loading floorplan: {args.input}")
        with open(args.input, 'r', encoding='utf-8') as f:
            fp_data = json.load(f)

    # If --all-rooms or --output-dir is specified, generate separate files for each room
    if args.all_rooms or args.output_dir:
        out_dir = args.output_dir or "room_level_payloads"
        all_res = generate_all_room_payloads(
            floorplan_data=fp_data,
            rules_source=args.rules,
            output_dir=out_dir,
            apply_door_model=args.apply_door_model,
            apply_window_model=args.apply_window_model,
            apply_floor=args.apply_floor,
            apply_ceiling=args.apply_ceiling,
            apply_wall=args.apply_wall,
            force_scale=args.scale
        )
        if args.send_to_api:
            from test_inspiration_api import run_automation
            run_automation(payloads_dir=out_dir)
        return all_res

    style_id = args.style_id
    room_type = args.room_type
    apply_door_model = args.apply_door_model
    apply_window_model = args.apply_window_model
    apply_floor = args.apply_floor
    apply_ceiling = args.apply_ceiling
    apply_wall = args.apply_wall

    # If an input payload JSON file is supplied, load parameters from it
    if args.payload:
        print(f"[*] Loading input parameters from payload: {args.payload}")
        with open(args.payload, 'r', encoding='utf-8') as pf:
            inp_data = json.load(pf)
        style_id = inp_data.get('style_id', style_id)
        room_type = inp_data.get('room_type', room_type)
        apply_door_model = inp_data.get('apply_door_model', apply_door_model)
        apply_window_model = inp_data.get('apply_window_model', apply_window_model)
        apply_floor = inp_data.get('apply_floor', apply_floor)
        apply_ceiling = inp_data.get('apply_ceiling', apply_ceiling)
        apply_wall = inp_data.get('apply_wall', apply_wall)

    result_payload = generate_room_json(
        floorplan_data=fp_data,
        style_id=style_id,
        room_type=room_type,
        rules_source=args.rules,
        area_id=args.area_id,
        apply_door_model=apply_door_model,
        apply_window_model=apply_window_model,
        apply_floor=apply_floor,
        apply_ceiling=apply_ceiling,
        apply_wall=apply_wall,
        force_scale=args.scale
    )

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as out_f:
            json.dump(result_payload, out_f, indent=2)
        print(f"[+] [Step 1] Successfully saved output to: {args.output}")

    if args.send_to_api:
        from test_inspiration_api import run_automation
        run_automation(target_file=args.output)

    return result_payload

if __name__ == "__main__":
    main()
