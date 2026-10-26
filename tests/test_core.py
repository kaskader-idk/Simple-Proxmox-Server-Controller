"""Run: python3 -m unittest discover -s tests -v (after pip install -r requirements.txt)."""
import base64
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile

from controller.guest import GUEST_SCRIPT, _decode
from controller.main import allowed_peer, origin_ok
from controller.proxmox import bytesize_from_config

def guest(op, *args):
    proc = subprocess.run([sys.executable, "-c", GUEST_SCRIPT, op, *map(str, args)],
                          capture_output=True, text=True, timeout=10, check=True)
    return _decode(proc.stdout)

class GuestOperations(unittest.TestCase):
    def test_list_read_write_and_move(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp)
            file = directory / "notes.txt"
            data = base64.b64encode(b"hello guest").decode()
            self.assertEqual(guest("write", file, "wb", data), {"ok": True})
            self.assertIn("notes.txt", [x["name"] for x in guest("list", directory)["items"]])
            result = guest("read", file, 0, 100)
            self.assertEqual(base64.b64decode(result["chunk"]), b"hello guest")
            self.assertTrue(result["eof"])
            guest("move", file, directory / "renamed.txt")
            self.assertFalse(file.exists())
            self.assertTrue((directory / "renamed.txt").exists())
            guest("delete", directory / "renamed.txt")
            self.assertFalse((directory / "renamed.txt").exists())

    def test_archiving(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "a.txt").write_text("contents")
            archive = root / "bundle.zip"
            guest("compress", root / "a.txt", archive, "zip")
            self.assertTrue(archive.exists())
            guest("extract", archive, root / "out")
            self.assertEqual((root / "out" / "a.txt").read_text(), "contents")

    def test_zip_slip_denied(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            with zipfile.ZipFile(root / "evil.zip", "w") as z:
                z.writestr("../escape.txt", "no")
            with self.assertRaisesRegex(Exception, "Unsafe"):
                guest("extract", root / "evil.zip", root / "dest")
            self.assertFalse((root / "escape.txt").exists())

    def test_delete_root_denied(self):
        with self.assertRaisesRegex(Exception, "Cannot delete"):
            guest("delete", "/")

class Security(unittest.TestCase):
    def test_private_network_only(self):
        for addr in ("127.0.0.1", "192.168.178.5", "10.1.3.2", "100.90.1.1", "::1"):
            self.assertTrue(allowed_peer(addr), addr)
        for addr in ("8.8.8.8", "1.2.3.4", "invalid"):
            self.assertFalse(allowed_peer(addr), addr)

    def test_origin(self):
        self.assertTrue(origin_ok("http://192.168.178.2:8787", "192.168.178.2:8787"))
        self.assertFalse(origin_ok("https://evil.example", "192.168.178.2:8787"))

    def test_config_disk_sizes(self):
        self.assertEqual(bytesize_from_config("local-lvm:vm-100-disk-0,size=20G"), 20 * 1024**3)
        self.assertEqual(bytesize_from_config("local:100/vm-100-disk,size=512M"), 512 * 1024**2)

if __name__ == "__main__":
    unittest.main()
