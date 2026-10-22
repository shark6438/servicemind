import uvicorn

from service import service


async def no_recovery():
    return 0


service.recover_incomplete_runs = no_recovery
uvicorn.run(service.create_app(), host="127.0.0.1", port=18083)
