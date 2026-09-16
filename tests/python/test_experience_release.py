import json
import unittest
from pathlib import Path
from lib.control import session_tui
from tests.python.test_control_session_tui_colors import snapshot


class ExperienceRelease(unittest.TestCase):
    def test_dashboard_keeps_capture_review_and_activity_independent(self):
        data = snapshot('finished')
        data['rows'][0].update(capture={'status': 'captured'}, experience_review='unsupported',
                               process_state='live')
        rendered = '\n'.join(session_tui.lines(data))
        self.assertIn('capture:captured', rendered)
        self.assertIn('review:unsupported', rendered)
        self.assertIn('Done: close', rendered)
        self.assertEqual(data['rows'][0]['activity'], 'finished')
        for width, height in ((1, 1), (20, 8), (40, 20)):
            self.assertLessEqual(len(session_tui.lines(data, width=width, height=height)), height)

    def test_every_harness_names_capture_guidance_and_unsupported_native_review(self):
        root = Path(__file__).resolve().parents[2]
        registry = json.loads((root / 'harnesses/capabilities.json').read_text())
        for name, harness in registry['harnesses'].items():
            with self.subTest(harness=name):
                self.assertIn('session-experience', harness['capabilities'])
                self.assertIn('session-guidance', harness['capabilities'])
                self.assertEqual(harness['capabilities']['experience-review']['support'], 'unsupported')
