"""Unit tests for the xai-sdk protobuf-7 import shim.

The real ``xai-sdk`` and ``protobuf`` packages are never required: the tests
install fake ``google.protobuf`` modules into ``sys.modules`` and write a fake
gated ``xai_sdk`` package to a temporary directory that reproduces the 1.17.0
version-gate behavior (select gencode by protobuf major, reject >= 7).
"""

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

from agent_collab.backends.xai_sdk import compat as compat_mod
from agent_collab.backends.xai_sdk.compat import SPOOFED_VERSION, import_xai_sdk

GATED_PACKAGE = """
import google.protobuf

SEEN_VERSION = google.protobuf.__version__
_major = int(SEEN_VERSION.split(".")[0])
if _major not in (5, 6):
    raise ValueError(f"Unsupported protobuf version: {SEEN_VERSION}")


class AsyncClient:
    pass
"""

ALWAYS_GATED_PACKAGE = """
import google.protobuf

raise ValueError(f"Unsupported protobuf version: {google.protobuf.__version__}")
"""

UNRELATED_ERROR_PACKAGE = """
raise ValueError("unrelated import failure")
"""


class ImportXaiSdkTest(unittest.TestCase):
    def setUp(self):
        self._saved_modules = {
            name: sys.modules.get(name)
            for name in list(sys.modules)
            if name == "google"
            or name.startswith("google.")
            or name == "xai_sdk"
            or name.startswith("xai_sdk.")
        }
        for name in self._saved_modules:
            del sys.modules[name]
        self._saved_path = list(sys.path)

        google = ModuleType("google")
        protobuf = ModuleType("google.protobuf")
        protobuf.__version__ = "7.35.1"
        google.protobuf = protobuf
        sys.modules["google"] = google
        sys.modules["google.protobuf"] = protobuf
        self.protobuf = protobuf

    def tearDown(self):
        sys.path[:] = self._saved_path
        for name in [
            m
            for m in sys.modules
            if m == "google"
            or m.startswith("google.")
            or m == "xai_sdk"
            or m.startswith("xai_sdk.")
        ]:
            del sys.modules[name]
        for name, module in self._saved_modules.items():
            if module is not None:
                sys.modules[name] = module
        importlib.invalidate_caches()

    def _write_package(self, source: str) -> None:
        root = Path(tempfile.mkdtemp(prefix="fake-xai-sdk-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        package = root / "xai_sdk"
        package.mkdir()
        (package / "__init__.py").write_text(source, encoding="utf-8")
        sys.path.insert(0, str(root))
        importlib.invalidate_caches()

    def test_plain_import_wins_when_gate_accepts_runtime(self):
        self.protobuf.__version__ = "6.33.6"
        self._write_package(GATED_PACKAGE)

        module = import_xai_sdk()

        self.assertEqual(module.SEEN_VERSION, "6.33.6")
        self.assertEqual(self.protobuf.__version__, "6.33.6")

    def test_gate_is_defeated_under_protobuf_7(self):
        self._write_package(GATED_PACKAGE)
        with mock.patch.object(compat_mod, "_installed_xai_sdk_version", return_value="1.17.0"):
            module = import_xai_sdk()

        self.assertEqual(module.SEEN_VERSION, SPOOFED_VERSION)
        self.assertTrue(hasattr(module, "AsyncClient"))
        self.assertIs(sys.modules["xai_sdk"], module)
        self.assertEqual(self.protobuf.__version__, "7.35.1")

    def test_version_is_restored_when_the_retry_also_fails(self):
        self._write_package(ALWAYS_GATED_PACKAGE)

        with mock.patch.object(compat_mod, "_installed_xai_sdk_version", return_value="1.17.0"):
            with self.assertRaisesRegex(ValueError, "Unsupported protobuf version"):
                import_xai_sdk()
        self.assertEqual(self.protobuf.__version__, "7.35.1")
        self.assertNotIn("xai_sdk", sys.modules)

    def test_unverified_xai_sdk_series_is_not_spoofed(self):
        self._write_package(GATED_PACKAGE)
        with mock.patch.object(compat_mod, "_installed_xai_sdk_version", return_value="1.18.0"):
            with self.assertRaisesRegex(ValueError, "Unsupported protobuf version: 7.35.1"):
                import_xai_sdk()
        self.assertEqual(self.protobuf.__version__, "7.35.1")
        self.assertNotIn("xai_sdk", sys.modules)

    def test_unrelated_valueerror_propagates_without_spoofing(self):
        self._write_package(UNRELATED_ERROR_PACKAGE)

        with self.assertRaisesRegex(ValueError, "unrelated import failure"):
            import_xai_sdk()
        self.assertEqual(self.protobuf.__version__, "7.35.1")

    def test_protobuf_8_gate_is_not_spoofed(self):
        self.protobuf.__version__ = "8.0.0"
        self._write_package(GATED_PACKAGE)

        with self.assertRaisesRegex(ValueError, "Unsupported protobuf version: 8.0.0"):
            import_xai_sdk()
        self.assertEqual(self.protobuf.__version__, "8.0.0")
        self.assertNotIn("xai_sdk", sys.modules)

    def test_substring_gate_without_known_prefix_is_not_spoofed(self):
        self._write_package('raise ValueError("wrapper: Unsupported protobuf version: 7.35.1")\n')

        with self.assertRaisesRegex(ValueError, "wrapper:"):
            import_xai_sdk()
        self.assertEqual(self.protobuf.__version__, "7.35.1")

    def test_missing_package_raises_importerror(self):
        with self.assertRaises(ImportError):
            import_xai_sdk()


if __name__ == "__main__":
    unittest.main()
