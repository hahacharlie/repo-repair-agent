import io
from contextlib import redirect_stderr
import unittest

from cli import main, python_version_error


class CliTests(unittest.TestCase):
    def test_old_python_stops_before_cli_setup(self):
        message = python_version_error((3, 9, 18))
        self.assertIn("requires Python 3.10 or newer", message)
        self.assertIsNone(python_version_error((3, 10, 0)))

        output = io.StringIO()
        with redirect_stderr(output):
            result = main((3, 9, 18))
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), message)


if __name__ == "__main__":
    unittest.main()
