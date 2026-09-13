"""Run with `python -m unittest discover -s tools -p test_providers.py`."""

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from rq.job import JobStatus

from handler.providers.downloads import (
    filename_safe,
    http_download,
    publish_file,
    selected_torrent_file,
    torrent_download,
)
from handler.providers.jobs import status
from handler.providers.mega import decrypt_file, xor
from handler.providers.minerva import (
    build_index,
    get_entry,
    index_status,
    parse_torrent,
    search_index,
)
from handler.providers.sources import download_url, parse_edge, parse_vimm


def encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    return (
        b"d"
        + b"".join(encode(key) + encode(item) for key, item in sorted(value.items()))
        + b"e"
    )


class ProviderChecks(unittest.TestCase):
    def test_torrent_storage_error_fails_promptly_and_stops_transfer(self):
        requests = []

        def respond(request):
            requests.append(request.url.path)
            if request.url.path.endswith("auth/login"):
                return httpx.Response(200, text="Ok.")
            if request.url.path.endswith("torrents/info"):
                return httpx.Response(
                    200, json=[{"category": "romm-providers", "state": "error"}]
                )
            if request.url.path.endswith("torrents/files"):
                return httpx.Response(
                    200,
                    json=[{"name": "Game.gba", "size": 12, "index": 0, "progress": 0}],
                )
            return httpx.Response(200)

        client = httpx.Client(
            base_url="http://qbit.test/api/v2/", transport=httpx.MockTransport(respond)
        )
        with (
            patch("handler.providers.downloads.httpx.Client", return_value=client),
            patch(
                "handler.providers.downloads.PROVIDER_QBITTORRENT_URL",
                "http://qbit.test",
            ),
            patch("handler.providers.downloads.PROVIDER_DOWNLOAD_PATH", str(self.root)),
            patch(
                "handler.providers.downloads.time.sleep",
                side_effect=AssertionError("Unexpected wait"),
            ),
            self.assertRaisesRegex(ValueError, "permissions"),
        ):
            torrent_download(
                {"info_hash": "0" * 40, "file_path": "Game.gba", "size": 12},
                lambda **_: None,
            )
        self.assertEqual(requests.count("/api/v2/torrents/stop"), 2)

    def test_worker_failure_is_not_hidden_by_stale_progress(self):
        job = Mock()
        job.id = "interrupted"
        job.get_meta.return_value = {"kind": "download", "state": "started"}
        job.get_status.return_value = JobStatus.FAILED
        self.assertEqual(status(job).state, "failed")
        job.get_meta.return_value = {"kind": "download"}
        job.get_status.return_value = JobStatus.DEFERRED
        self.assertEqual(status(job).state, "deferred")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.info = {
            b"name": b"Nintendo - Game Boy Advance",
            b"piece length": 16384,
            b"pieces": bytes(20),
            b"files": [
                {b"length": 3, b"path": [b"../unsafe.gba"]},
                {b"length": 12, b"path": ["Pokémon - Emerald (USA).gba".encode()]},
                {b"length": 15, b"path": [b"Super Mario (Europe).gba"]},
            ],
        }
        self.torrent = encode({b"info": self.info})
        self.bundle = self.root / "bundle.zip"
        with zipfile.ZipFile(self.bundle, "w") as archive:
            archive.writestr(
                "Minerva_Myrient - Nintendo - Game Boy Advance.torrent", self.torrent
            )

    def test_exact_info_hash_and_bad_path_preserves_indices(self):
        identity, _, files = parse_torrent(self.torrent)
        self.assertEqual(
            identity, hashlib.sha1(encode(self.info), usedforsecurity=False).hexdigest()
        )
        self.assertEqual(files[0], ("", 3))
        self.assertEqual(files[1][0], "Pokémon - Emerald (USA).gba")
        for invalid in (b"", b"d4:infod", self.torrent + b"junk", b"d4:infoi4ee"):
            with self.assertRaises(ValueError):
                parse_torrent(invalid)

    def test_search_refresh_checkpoint_and_last_good_snapshot(self):
        root = self.root / "index"
        self.assertEqual(build_index(self.bundle, lambda **_: None, root), 2)
        result = search_index("pokemon emerald", "gba", "USA", 1, 20, root)
        self.assertEqual(result.total, 1)
        self.assertEqual(get_entry(result.items[0].id, root)["file_index"], 1)
        self.assertEqual(search_index('" OR *', "", "", 1, 20, root).total, 0)
        self.assertEqual(search_index("", "gba", "Europe", 1, 20, root).total, 1)

        def interrupt(**_):
            raise RuntimeError("interrupted")

        with self.assertRaises(RuntimeError):
            build_index(self.bundle, interrupt, root)
        self.assertTrue(index_status(root)["index_ready"])
        self.assertEqual(search_index("pokemon", "", "", 1, 20, root).total, 1)
        self.assertEqual(build_index(self.bundle, lambda **_: None, root), 2)

    def test_exact_torrent_file_does_not_trust_order(self):
        entry = {"file_path": "Game.gba", "size": 12, "file_index": 0}
        files = [
            {"index": 0, "name": "set/Other.gba", "size": 12},
            {"index": 5, "name": "set/Game.gba", "size": 12},
        ]
        self.assertEqual(selected_torrent_file(files, entry)["index"], 5)
        with self.assertRaises(ValueError):
            selected_torrent_file(files + [files[1]], entry)
        with self.assertRaises(ValueError):
            selected_torrent_file([{**files[1], "size": 13}], entry)

    def test_download_boundary(self):
        for url in (
            "http://edgeemu.net/game",
            "https://edgeemu.net.evil.test/game",
            "https://user@edgeemu.net/game",
            "https://127.0.0.1/game",
            "https://edgeemu.net:444/game",
        ):
            with self.assertRaises(ValueError):
                download_url(url)
        self.assertEqual(
            download_url("https://edgeemu.net/download/gba/Game.zip"),
            "https://edgeemu.net/download/gba/Game.zip",
        )
        for name in (
            "../game.gba",
            "..\\game.gba",
            "CON.zip",
            "x:stream",
            "x\n.gba",
            "NUL",
            "game. ",
        ):
            with self.assertRaises(ValueError):
                filename_safe(name)

    def test_publish_no_overwrite_and_cancel_cleanup(self):
        source = self.root / "source"
        source.write_bytes(b"preserve-me")
        destination = self.root / "game.gba"
        destination.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            publish_file(source, destination, lambda **_: None)
        self.assertEqual(destination.read_bytes(), b"original")
        destination.unlink()

        def interrupt(**_):
            raise RuntimeError("cancelled")

        with self.assertRaises(RuntimeError):
            publish_file(source, destination, interrupt)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob("*.provider-part")), [])
        publish_file(source, destination, lambda **_: None)
        self.assertEqual(destination.read_bytes(), b"preserve-me")

    def test_provider_html_contracts(self):
        edge = '<details><summary>Game (USA)</summary>system: <span>Nintendo Game Boy Advance</span><a href="/download/gba/Game%20(USA).zip">download</a></details>'
        result = parse_edge(edge)[0]
        self.assertEqual(result.platform, "gba")
        self.assertEqual(result.filename, "Game (USA).zip")
        self.assertEqual(result.options[0].method, "http")
        vimm = '<tr><td>Nintendo 64</td><td><a href="/vault/999999" style="display:none">9</a><a href= "/vault/1234">Example &amp; Game</a></td></tr>'
        self.assertEqual(parse_vimm(vimm)[0].name, "Example & Game")

    def test_http_resume_and_redirect_boundary(self):
        data = b"z" * (2 * 1024**2 + 3)
        target = self.root / "download"
        requests = []

        def respond(request):
            requests.append(request)
            offset = int(request.headers.get("range", "bytes=0-")[6:-1])
            headers = {
                "etag": '"stable"',
                "content-type": "application/octet-stream",
                "content-disposition": 'attachment; filename="Game.zip"',
            }
            if offset:
                headers["content-range"] = f"bytes {offset}-{len(data)-1}/{len(data)}"
            return httpx.Response(
                206 if offset else 200, headers=headers, content=data[offset:]
            )

        def interrupt(**_):
            raise RuntimeError("cancelled")

        with patch(
            "handler.providers.downloads.create_httpx_client",
            side_effect=lambda: httpx.Client(transport=httpx.MockTransport(respond)),
        ):
            with self.assertRaises(RuntimeError):
                http_download("https://edgeemu.net/game", target, interrupt)
            self.assertEqual(target.stat().st_size, 1024**2)
            self.assertEqual(
                http_download("https://edgeemu.net/game", target, lambda **_: None),
                "Game.zip",
            )
        self.assertEqual(requests[1].headers["if-range"], '"stable"')
        self.assertEqual(target.read_bytes(), data)

        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    302, headers={"location": "https://127.0.0.1/private"}
                )
            )
        )
        with (
            patch(
                "handler.providers.downloads.create_httpx_client", return_value=client
            ),
            self.assertRaises(ValueError),
        ):
            http_download(
                "https://edgeemu.net/redirect", self.root / "redirect", lambda **_: None
            )

    def test_mega_chunk_boundary_vectors_and_tampering(self):
        # Meta-MAC vectors generated independently with Node's crypto implementation.
        aes_key = bytes.fromhex("00112233445566778899aabbccddeeff")
        nonce = bytes.fromhex("1020304050607080")
        for length, expected_mac in (
            (4718592, "3fda167929217462"),
            (4718595, "cfe33b66094dc9da"),
        ):
            plain = (bytes(range(251)) * (length // 251 + 1))[:length]
            trailer = nonce + bytes.fromhex(expected_mac)
            node_key = xor(aes_key, trailer) + trailer
            cipher = Cipher(
                algorithms.AES(aes_key), modes.CTR(nonce + bytes(8))
            ).encryptor()
            encrypted = cipher.update(plain) + cipher.finalize()
            source, destination = self.root / "encrypted", self.root / "decrypted"
            source.write_bytes(encrypted)
            decrypt_file(source, destination, node_key, lambda **_: None)
            self.assertEqual(destination.read_bytes(), plain)
            source.write_bytes(encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
            with self.assertRaises(ValueError):
                decrypt_file(source, destination, node_key, lambda **_: None)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
