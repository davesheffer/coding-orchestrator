import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def installer(name):
    spec = importlib.util.spec_from_file_location(f"{name}_installer", ROOT / name / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MarkerTests(unittest.TestCase):
    def test_rejects_missing_duplicate_and_reversed_markers(self):
        for name in ("claude", "codex"):
            module = installer(name)
            start, end = module.START, module.END
            source = start + b"\nnew\n" + end
            for data in (b"", start, end, end + start, start + start + end, start + end + end):
                with self.subTest(installer=name, data=data):
                    with self.assertRaises(ValueError):
                        module.managed_block(data, Path("test"))
                    if data:
                        with self.assertRaises(ValueError):
                            module.merge_instructions(data, source, Path("test"))

    def test_large_merge_preserves_surrounding_bytes_and_is_idempotent(self):
        for name in ("claude", "codex"):
            with self.subTest(installer=name):
                module = installer(name)
                start, end = module.START, module.END
                prefix = b"private\xff\r\n" + b"x" * (1 << 20)
                suffix = b"\r\nprivate suffix\x00"
                source = start + b"\nnew\n" + end
                original = prefix + start + b"old" + end + suffix
                merged = module.merge_instructions(original, source, Path("test"))
                self.assertEqual(merged, prefix + source + suffix)
                self.assertEqual(module.merge_instructions(merged, source, Path("test")), merged)


if __name__ == "__main__":
    unittest.main()
