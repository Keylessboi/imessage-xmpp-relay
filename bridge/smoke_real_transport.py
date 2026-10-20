import asyncio, sys
sys.path.insert(0, "tests")
from mock_upstream import MockUpstream, serve
from relay.config import Config
from relay.main import run_service
from relay.logsetup import setup_logging

async def main():
    mock = MockUpstream()
    async with serve(mock) as url:
        cfg = Config(jid="bridge@127.0.0.1", password="hunter2", server="127.0.0.1", port=1,
                     targets=("me@example.org",), allowed=frozenset({"me@example.org"}),
                     api_url=url, backoff_initial=0.2, backoff_max=0.5, backoff_jitter=0.1)
        stop = asyncio.Event()
        task = asyncio.create_task(run_service(cfg, stop=stop))
        await asyncio.sleep(1.5)
        await mock.push({"type": "ready"})
        await mock.push({"type": "message", "guid": "smoke-1", "chat_guid": "c1",
                         "from": "+15551234567", "to": ["+15557654321"], "text": "smoke",
                         "timestamp": 1, "is_group": False, "sender_name": "Alice"})
        await asyncio.sleep(1.5)
        assert task and not task.done(), "service exited on its own"
        stop.set()
        await asyncio.wait_for(task, 10)
    print("SMOKE_OK")

setup_logging("INFO")
asyncio.run(main())
