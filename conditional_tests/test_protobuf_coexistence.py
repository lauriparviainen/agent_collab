"""Conditional proof that xai-sdk and google-antigravity share one process.

Runs only where the real vendor SDKs are installed (the durable user venv;
CI installs no vendor SDKs). Verifies the protobuf-7 coexistence contract end
to end: the compat shim imports ``xai_sdk`` under whatever protobuf runtime is
installed, its selected gencode round-trips messages, and — when the runtime
is the 7.x line the durable install pins — ``google.antigravity`` imports in
the very same process. See ``agent_collab/backends/xai_sdk/compat.py``.
"""

from __future__ import annotations

import importlib.util
import inspect
import unittest


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


class ProtobufCoexistenceTest(unittest.TestCase):
    def setUp(self):
        if not _installed("google.protobuf"):
            self.skipTest("protobuf is not installed")
        if not _installed("xai_sdk"):
            self.skipTest("xai-sdk is not installed")

    def test_xai_sdk_imports_and_roundtrips_via_shim(self):
        import google.protobuf as protobuf

        version_before = protobuf.__version__

        from agent_collab.backends.xai_sdk.compat import import_xai_sdk

        xai_sdk = import_xai_sdk()

        self.assertEqual(protobuf.__version__, version_before)
        self.assertTrue(hasattr(xai_sdk, "AsyncClient"))

        from xai_sdk.proto import sample_pb2

        message_classes = [
            value
            for value in vars(sample_pb2).values()
            if inspect.isclass(value) and hasattr(value, "SerializeToString")
        ]
        self.assertTrue(message_classes, "no generated message classes found")
        message = message_classes[0]()
        parsed = message_classes[0]()
        parsed.ParseFromString(message.SerializeToString())

    def test_antigravity_imports_in_the_same_process(self):
        import google.protobuf as protobuf

        if int(protobuf.__version__.split(".")[0]) < 7:
            self.skipTest("protobuf runtime is older than the antigravity floor")
        if not _installed("google.antigravity"):
            self.skipTest("google-antigravity is not installed")

        from agent_collab.backends.xai_sdk.compat import import_xai_sdk

        import_xai_sdk()
        import google.antigravity

        self.assertTrue(hasattr(google.antigravity, "Agent"))


if __name__ == "__main__":
    unittest.main()
