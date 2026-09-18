"""Small application fixture exercising transitive imports and image links."""

import unittest
from pathlib import Path

from linked_value import VALUE


class PreparedImageTest(unittest.TestCase):
    def test_dependency(self):
        self.assertEqual(VALUE, "prepared-image")
        with self.assertRaises(OSError):
            Path(__file__).write_text("untrusted modification")
        with self.assertRaises(OSError):
            Path(__file__).with_name("control-link").read_bytes()


if __name__ == "__main__":
    unittest.main()
