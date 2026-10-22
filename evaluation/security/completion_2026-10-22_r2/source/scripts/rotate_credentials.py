"""Validate or atomically rotate one tenant's encrypted credentials and MCP outputs."""

import argparse
import asyncio
import json
from uuid import UUID

from servicemind.persistence.database import close_database
from servicemind.security.rotation import rotate_tenant_credentials


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        count = await rotate_tenant_credentials(args.tenant_id, dry_run=not args.apply)
        print(
            json.dumps({"tenant_id": str(args.tenant_id), "applied": args.apply, "fields": count})
        )
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
