#!/usr/bin/env python3
"""
test_step1.py - Automated tests for Step 1: Generate Room JSON
"""

import os
import json
import unittest
from create_room_inspiration_payload import (
    calculate_polygon_area,
    calculate_room_area_m2,
    identify_room_type,
    load_style_rules,
    find_matching_room,
    validate_style_id_for_room,
    generate_room_json,
    generate_all_room_payloads
)

FLOORPLAN_FILE = "1bhk_south_square_853.2sqft.json"
RULES_FILE = "response_1789025921629.json"

class TestStep1GenerateRoomJson(unittest.TestCase):

    def setUp(self):
        with open(FLOORPLAN_FILE, 'r', encoding='utf-8') as f:
            self.fp = json.load(f)
        self.styles = load_style_rules(RULES_FILE)

    def test_shoelace_formula_basic_geometry(self):
        # 10m x 10m square = 100 m²
        square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        self.assertAlmostEqual(calculate_polygon_area(square), 100.0)

        # Right triangle base 6, height 8 = 24
        triangle = [(0.0, 0.0), (6.0, 0.0), (0.0, 8.0)]
        self.assertAlmostEqual(calculate_polygon_area(triangle), 24.0)

        # Fewer than 3 points
        self.assertEqual(calculate_polygon_area([(0.0, 0.0), (1.0, 1.0)]), 0.0)

    def test_floorplan_room_areas(self):
        layer = self.fp['layers']['layer-1']
        vertices = layer['vertices']
        areas = layer['areas']

        # Bedroom: 22.69 m²
        bed_area = calculate_room_area_m2(vertices, areas['a-1770955324792-8uewx7i2']['vertices'])
        self.assertEqual(bed_area, 22.69)

        # Toilet: 11.34 m²
        toilet_area = calculate_room_area_m2(vertices, areas['a-1770955339845-sep4p0q8']['vertices'])
        self.assertEqual(toilet_area, 11.34)

        # Living Room: 24.95 m²
        living_area = calculate_room_area_m2(vertices, areas['a-1770955354118-ko2gu9tn']['vertices'])
        self.assertEqual(living_area, 24.95)

        # Kitchen: 20.28 m²
        kitchen_area = calculate_room_area_m2(vertices, areas['a-1770955365279-354luvc8']['vertices'])
        self.assertEqual(kitchen_area, 20.28)

    def test_find_matching_room(self):
        layers = self.fp['layers']
        selected_layer = 'layer-1'

        # Match Bedroom
        aid, area = find_matching_room(layers, selected_layer, "Bedroom")
        self.assertEqual(aid, "a-1770955324792-8uewx7i2")
        self.assertEqual(area['name'], "Bedroom")

        # Match Bathroom from Toilet
        aid, area = find_matching_room(layers, selected_layer, "Bathroom")
        self.assertEqual(aid, "a-1770955339845-sep4p0q8")

        # Non-existent room raises ValueError
        with self.assertRaises(ValueError):
            find_matching_room(layers, selected_layer, "Home Cinema")

    def test_validate_style_id_for_room(self):
        # 1. Valid match: public allowed user_style_id 237 is Contemporary Modern (8-100 m2) and area is 22.69 m2
        style, rule = validate_style_id_for_room(237, "Bedroom", 22.69, self.styles)
        self.assertEqual(style['user_style_id'], 237)
        self.assertEqual(style['is_public'], True)
        self.assertEqual(rule['room_type'], "Bedroom")

        # 2. Non-existent style ID
        with self.assertRaises(ValueError) as ctx:
            validate_style_id_for_room(999999, "Bedroom", 22.69, self.styles)
        self.assertIn("not found", str(ctx.exception))

        # 3. Style exists but wrong room type (user_style_id 237 is Bedroom, but checking Kitchen)
        with self.assertRaises(ValueError) as ctx:
            validate_style_id_for_room(237, "Kitchen", 20.28, self.styles)
        self.assertIn("does not support room type", str(ctx.exception))

        # 4. Style not in allowed list (e.g. style 308 is not in allowed list)
        with self.assertRaises(ValueError):
            validate_style_id_for_room(308, "Living Room", 24.95, self.styles)

    def test_generate_room_json_structure(self):
        payload = generate_room_json(
            floorplan_data=self.fp,
            style_id=237,
            room_type="Bedroom",
            rules_source=self.styles,
            apply_door_model=True,
            apply_window_model=False,
            apply_floor=True,
            apply_ceiling=True,
            apply_wall=False
        )

        # 1. Exact top-level keys check
        expected_keys = {
            "room_json",
            "style_id",
            "room_type",
            "apply_door_model",
            "apply_window_model",
            "apply_floor",
            "apply_ceiling",
            "apply_wall"
        }
        self.assertEqual(set(payload.keys()), expected_keys)

        # 2. Values preserved and style_id taken from user_style_id
        self.assertEqual(payload['style_id'], 237)
        self.assertEqual(payload['room_type'], "Bedroom")
        self.assertEqual(payload['apply_door_model'], True)
        self.assertEqual(payload['apply_window_model'], False)
        self.assertEqual(payload['apply_floor'], True)
        self.assertEqual(payload['apply_ceiling'], True)
        self.assertEqual(payload['apply_wall'], False)

        # 3. room_json contains complete matching room data
        room_data = payload['room_json']
        self.assertIsInstance(room_data, dict)
        self.assertIn('layers', room_data)
        layer = room_data['layers']['layer-1']
        self.assertEqual(list(layer['areas'].keys()), ["a-1770955324792-8uewx7i2"])
        self.assertEqual(len(layer['vertices']), 5)
        self.assertEqual(len(layer['lines']), 5)
        self.assertEqual(len(layer['holes']), 3)

        # 4. No nested room_json
        self.assertNotIn('room_json', room_data)

    def test_generate_all_room_payloads(self):
        import shutil
        test_dir = "test_room_payloads"
        try:
            results = generate_all_room_payloads(
                floorplan_data=self.fp,
                rules_source=self.styles,
                output_dir=test_dir
            )
            self.assertEqual(len(results), 4)
            files = os.listdir(test_dir)
            self.assertEqual(len(files), 4)
            for fname in ["bedroom.json", "toilet.json", "living_room.json", "kitchen.json"]:
                self.assertIn(fname, files)
                with open(os.path.join(test_dir, fname)) as f:
                    p = json.load(f)
                    self.assertIn("room_json", p)
                    self.assertIn("style_id", p)
                    self.assertIn("room_type", p)
                    self.assertNotIn("room_json", p["room_json"])
        finally:
            if os.path.exists(test_dir):
                shutil.rmtree(test_dir)

if __name__ == '__main__':
    unittest.main()
