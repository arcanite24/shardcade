import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main
from endpoints.assistant import ConfirmRequest, _tool, confirm
from endpoints.responses.providers import ProviderOption, ProviderResult
from fastapi import HTTPException


class AssistantActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_minerva_search_retries_without_release_qualifiers(self):
        searches = []

        def search(query, *_):
            searches.append(query)
            return SimpleNamespace(total=1 if query == "Mario Kart 7" else 0, items=[])

        async def threaded(fn, *args):
            return fn(*args)

        with (
            patch("endpoints.assistant.run_in_threadpool", threaded),
            patch("endpoints.assistant.minerva.search_index", search),
        ):
            result, _ = await _tool(
                "search_provider",
                {
                    "provider": "minerva",
                    "query": "Mario Kart 7 USA decrypted 3DS base game",
                    "platform": "3ds",
                    "region": "USA",
                },
            )
        self.assertEqual(result["total"], 1)
        self.assertEqual(searches[-1], "Mario Kart 7")

    async def test_batch_selects_decrypted_base_release(self):
        def release(collection, filename):
            return ProviderResult(
                id=f"minerva:{collection}:{filename}",
                provider="minerva",
                name="Mario Kart 7",
                platform="3ds",
                region="USA",
                collection=collection,
                filename=filename,
                size=100,
                source_url="https://example.com",
                options=[
                    ProviderOption(
                        label="Torrent",
                        method="torrent",
                        url="https://example.com/game.torrent",
                    )
                ],
            )

        encrypted = release("Nintendo 3DS (Encrypted)", "Mario Kart 7 (USA).zip")
        update = release("Nintendo 3DS (Decrypted)", "Mario Kart 7 (USA) (Update).zip")
        decrypted = release("Nintendo 3DS (Decrypted)", "Mario Kart 7 (USA).zip")

        async def threaded(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with (
            patch("endpoints.assistant.run_in_threadpool", threaded),
            patch(
                "endpoints.assistant.db_platform_handler.get_platform_by_fs_slug",
                return_value=SimpleNamespace(id=16, name="Nintendo 3DS"),
            ),
            patch(
                "endpoints.assistant.db_rom_handler.get_roms_scalar", return_value=[]
            ),
            patch(
                "endpoints.assistant.minerva.search_index",
                return_value=SimpleNamespace(items=[encrypted, update, decrypted]),
            ),
        ):
            read, action = await _tool(
                "propose_imports",
                {
                    "titles": ["Mario Kart 7"],
                    "platform": "3ds",
                    "region": "USA",
                    "decrypted": True,
                },
            )
        self.assertTrue(read["requires_confirmation"])
        self.assertEqual(action["items"][0]["result_id"], decrypted.id)

        class Cache:
            def getdel(self, _):
                return json.dumps({"user_id": 1, **action}).encode()

        async def downloaded(_request, body):
            self.assertEqual(body.result_id, decrypted.id)
            return SimpleNamespace(id="job-1")

        with (
            patch("endpoints.assistant._guard", return_value=1),
            patch("endpoints.assistant.redis_client", Cache()),
            patch("endpoints.providers.download", downloaded),
        ):
            confirmed = await confirm.__wrapped__(
                SimpleNamespace(), ConfirmRequest(action_id="a" * 32)
            )
        self.assertEqual(confirmed["job_ids"], ["job-1"])

    async def test_import_requires_confirmation_and_is_single_use(self):
        result = ProviderResult(
            id="minerva:test:1",
            provider="minerva",
            name="Mario Kart 7",
            platform="3ds",
            region="USA",
            collection="Nintendo 3DS (Decrypted)",
            filename="Mario Kart 7 (USA).zip",
            size=100,
            source_url="https://example.com",
            options=[
                ProviderOption(
                    label="Torrent",
                    method="torrent",
                    url="https://example.com/game.torrent",
                )
            ],
        )
        platform = SimpleNamespace(id=16, name="Nintendo 3DS")

        async def resolved(_):
            return result

        async def threaded(fn, *args):
            return fn(*args)

        with (
            patch("endpoints.assistant.resolve", resolved),
            patch("endpoints.assistant.run_in_threadpool", threaded),
            patch(
                "endpoints.assistant.db_platform_handler.get_platform_by_fs_slug",
                return_value=platform,
            ),
        ):
            read, action = await _tool(
                "propose_import",
                {"result_id": result.id, "platform": "3ds", "option": 0},
            )
        self.assertTrue(read["requires_confirmation"])
        self.assertEqual(action["result_id"], result.id)

        class Cache:
            value = json.dumps({"user_id": 1, **action}).encode()

            def getdel(self, _):
                value, self.value = self.value, None
                return value

        cache = Cache()

        async def downloaded(_request, body):
            self.assertEqual(body.platform_id, 16)
            return SimpleNamespace(id="job-1", name="Mario Kart 7")

        with (
            patch("endpoints.assistant._guard", return_value=1),
            patch("endpoints.assistant.redis_client", cache),
            patch("endpoints.providers.download", downloaded),
        ):
            confirmed = await confirm.__wrapped__(
                SimpleNamespace(), ConfirmRequest(action_id="a" * 32)
            )
            self.assertEqual(confirmed["job_id"], "job-1")
            with self.assertRaises(HTTPException) as second:
                await confirm.__wrapped__(
                    SimpleNamespace(), ConfirmRequest(action_id="a" * 32)
                )
            self.assertEqual(second.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
