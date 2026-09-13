"""Public MEGA folder selection and authenticated file decryption."""

import base64
import hmac
import json
import re
import secrets
import struct
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from handler.providers.downloads import filename_safe, http_download
from utils.context import create_httpx_client


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


def file_key(key: bytes) -> bytes:
    if len(key) != 32:
        raise ValueError("Invalid MEGA file key")
    return xor(key[:16], key[16:])


def api(command: dict, folder: str | None = None) -> dict:
    with create_httpx_client() as client:
        params = {"id": str(secrets.randbelow(2**32))}
        if folder:
            params["n"] = folder
        with client.stream(
            "POST",
            "https://g.api.mega.co.nz/cs",
            params=params,
            json=[command],
            timeout=60,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 32 * 1024**2:
                    raise ValueError("MEGA folder exceeds its listing limit")
    payload = json.loads(body)[0]
    if not isinstance(payload, dict):
        raise ValueError(
            "MEGA rejected this link or its transfer quota is exhausted"
        )  # noqa: TRY004
    return payload


def parse_link(url: str) -> tuple[str, str, bytes]:
    parsed = urlsplit(url)
    match = re.fullmatch(r"/(folder|file)/([\w-]{8})", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ("mega.nz", "mega.co.nz")
        or not match
    ):
        raise ValueError(
            "Use a public MEGA file or folder link with its decryption key"
        )
    key = decode(parsed.fragment.split("/", 1)[0])
    if len(key) != (16 if match[1] == "folder" else 32):
        raise ValueError("The MEGA link is missing its decryption key")
    return match[1], match[2], key


def attribute_name(attributes: str, key: bytes) -> str:
    raw = decode(attributes)
    cipher = Cipher(
        algorithms.AES(file_key(key) if len(key) == 32 else key), modes.CBC(bytes(16))
    ).decryptor()
    plain = (cipher.update(raw) + cipher.finalize()).rstrip(b"\0")
    if not plain.startswith(b"MEGA{"):
        raise ValueError("MEGA decryption key is incorrect")
    return str(json.loads(plain[4:])["n"])


def listing(url: str) -> list[dict]:
    kind, identity, key = parse_link(url)
    if kind == "file":
        info = api({"a": "g", "g": 1, "p": identity})
        return [
            {
                "id": identity,
                "name": attribute_name(info["at"], key),
                "size": int(info["s"]),
                "key": key,
                "folder": None,
            }
        ]
    nodes = api({"a": "f", "c": 1, "ca": 1, "r": 1}, identity).get("f", [])
    files = []
    for node in nodes:
        if node.get("t") != 0:
            continue
        for segment in node.get("k", "").split("/"):
            try:
                encrypted = decode(segment.split(":", 1)[-1])
                decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
                node_key = decryptor.update(encrypted) + decryptor.finalize()
                name = attribute_name(node["a"], node_key)
                files.append(
                    {
                        "id": node["h"],
                        "name": name,
                        "size": int(node["s"]),
                        "key": node_key,
                        "folder": identity,
                    }
                )
                break
            except ValueError, KeyError, UnicodeDecodeError:
                continue
    return sorted(files, key=lambda node: node["name"].casefold())


def decrypt_file(source: Path, destination: Path, key: bytes, report) -> None:
    aes_key = file_key(key)
    nonce = key[16:24]
    decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(nonce + bytes(8))).decryptor()
    condensed = bytes(16)
    block_cipher = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
    size = source.stat().st_size
    read = 0
    chunk_size = 128 * 1024
    with source.open("rb") as incoming, destination.open("wb") as output:
        while encrypted := incoming.read(chunk_size):
            plain = decryptor.update(encrypted)
            output.write(plain)
            mac = Cipher(algorithms.AES(aes_key), modes.CBC(nonce * 2)).encryptor()
            padded = plain + bytes(-len(plain) % 16)
            chunk_mac = (mac.update(padded) + mac.finalize())[-16:]
            condensed = block_cipher.update(xor(condensed, chunk_mac))
            read += len(encrypted)
            report(
                phase="decrypting",
                completed_bytes=read,
                total_bytes=size,
                progress=read / size,
            )
            chunk_size = min(chunk_size + 128 * 1024, 1024 * 1024)
    words = struct.unpack(">4I", condensed)
    actual = struct.pack(">2I", words[0] ^ words[1], words[2] ^ words[3])
    if not hmac.compare_digest(actual, key[24:32]):
        destination.unlink(missing_ok=True)
        raise ValueError(
            "MEGA integrity verification failed; the file was not imported"
        )


def download(
    url: str, node_id: str | None, destination: Path, report
) -> tuple[Path, str]:
    nodes = listing(url)
    if node_id is None and len(nodes) == 1:
        node_id = nodes[0]["id"]
    node = next((node for node in nodes if node["id"] == node_id), None)
    if not node:
        raise ValueError("Select a file from the MEGA folder")
    name = filename_safe(node["name"])
    info = api(
        {"a": "g", "g": 1, "n" if node["folder"] else "p": node["id"]}, node["folder"]
    )
    encrypted = destination.with_suffix(".encrypted")
    http_download(info["g"], encrypted, report, expected_size=node["size"])
    decrypt_file(encrypted, destination, node["key"], report)
    encrypted.unlink(missing_ok=True)
    return destination, name
