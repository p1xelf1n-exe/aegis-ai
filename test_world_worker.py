import json
import os
import unittest
from unittest import mock

from world_worker import (parse_plan, run_plan, apply_action, fresh_state,
                           clean_text, WORLD_PATH, SHAPES)


class WorldEngineTests(unittest.TestCase):
    def test_valid_plan_parses_and_applies(self):
        plan = {"thought": "first light",
                "actions": [
                    {"do": "spawn", "shape": "cube", "x": 1, "y": 0, "z": 2,
                     "color": "#ff00ff", "size": 1.5, "name": "pillar"},
                    {"do": "say", "text": "hello world"}]}
        thought, actions = parse_plan(json.dumps(plan))
        state = fresh_state()
        applied = run_plan(state, thought, actions)
        self.assertEqual(applied, 2)
        self.assertEqual(state['entities'][0]['name'], 'pillar')
        self.assertEqual(state['speech'][-1]['text'], 'hello world')
        self.assertEqual(state['thoughts'][-1]['text'], 'first light')

    def test_non_json_output_rejected(self):
        with self.assertRaises(RuntimeError):
            parse_plan('I will build a cube: [FINAL RESPONSE]')

    def test_actions_must_be_a_list(self):
        with self.assertRaises(RuntimeError):
            parse_plan('{"thought": "x", "actions": "none"}')

    def test_thought_required(self):
        with self.assertRaises(RuntimeError):
            parse_plan('{"actions": []}')

    def test_unknown_commands_are_ignored(self):
        thought, actions = parse_plan('{"thought": "hmm", "actions": [{"do": "rm -rf /"}]}')
        state = fresh_state()
        self.assertEqual(run_plan(state, thought, actions), 0)

    def test_duplicate_spawn_name_rejected(self):
        state = fresh_state()
        apply_action(state, {"do": "spawn", "shape": "cube", "x": 0, "y": 0, "z": 0, "name": "dupe"})
        self.assertFalse(apply_action(state, {"do": "spawn", "shape": "sphere", "x": 1, "y": 0, "z": 0, "name": "dupe"}))

    def test_bounds_and_values_clamped(self):
        state = fresh_state()
        self.assertTrue(apply_action(state, {"do": "body", "form": "star", "color": "zzz", "size": 99}))
        self.assertEqual(state['body']['size'], 4.0)
        self.assertEqual(state['body']['color'], '#9b6dff')  # invalid color replaced
        self.assertTrue(apply_action(state, {"do": "move", "x": 9999, "y": -50, "z": 0}))
        self.assertEqual(state['body']['x'], 40)
        self.assertEqual(state['body']['y'], 0)

    def test_spawn_requires_known_shape(self):
        state = fresh_state()
        self.assertFalse(apply_action(state, {"do": "spawn", "shape": "black hole", "x": 0, "y": 0, "z": 0}))
        for shape in SHAPES:
            self.assertTrue(apply_action(state, {"do": "spawn", "shape": shape, "x": 0, "y": 0, "z": 0, "name": shape}))

    def test_delete_recolor_and_move_object(self):
        state = fresh_state()
        apply_action(state, {"do": "spawn", "shape": "cube", "x": 0, "y": 0, "z": 0, "name": "a"})
        self.assertTrue(apply_action(state, {"do": "recolor", "name": "a", "color": "#123456"}))
        self.assertEqual(state['entities'][0]['color'], '#123456')
        self.assertTrue(apply_action(state, {"do": "move_object", "name": "a", "x": 5, "y": 1, "z": 5}))
        self.assertTrue(apply_action(state, {"do": "delete", "name": "a"}))
        self.assertEqual(state['entities'], [])

    def test_rotate_and_orbit_flags(self):
        state = fresh_state()
        apply_action(state, {"do": "spawn", "shape": "torus", "x": 0, "y": 0, "z": 0, "name": "ring1"})
        self.assertTrue(apply_action(state, {"do": "rotate", "name": "ring1", "speed": 5}))
        self.assertEqual(state['entities'][0]['spin'], 2.0)  # clamped
        self.assertTrue(apply_action(state, {"do": "orbit", "name": "ring1", "center": "0,2,0", "radius": 9, "speed": 1}))
        self.assertEqual(state['entities'][0]['orbit']['radius'], 9.0)

    def test_reset_clears_world(self):
        state = fresh_state()
        apply_action(state, {"do": "spawn", "shape": "cube", "x": 0, "y": 0, "z": 0, "name": "a"})
        self.assertTrue(apply_action(state, {"do": "reset"}))
        self.assertEqual(state['entities'], [])
        self.assertIsNone(state['body'])

    def test_object_cap_enforced(self):
        state = fresh_state()
        for i in range(90):
            apply_action(state, {"do": "spawn", "shape": "cube", "x": i, "y": 0, "z": 0, "name": "c%d" % i})
        self.assertLessEqual(len(state['entities']), 80)

    def test_text_sanitized(self):
        with mock.patch.dict(os.environ, {'GROQ_API_KEY': 'gsk-' + 'z' * 40}):
            text = clean_text('visit https://evil.example now gsk-' + 'z' * 40, 140)
            self.assertNotIn('http', text)
            self.assertNotIn('gsk', text)
            self.assertIn('████', text)

    def test_world_uses_own_document(self):
        self.assertEqual(WORLD_PATH, 'system/aegisWorld')


if __name__ == '__main__':
    unittest.main()
