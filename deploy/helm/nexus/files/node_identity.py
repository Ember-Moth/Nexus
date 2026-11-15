"""Init container of a relay pod: select this ordinal's database login.

Every relay pod mounts the optional credential Secrets of all node slots, but only into this
init container. It copies its own slot (StatefulSet ordinal N -> Secret "<prefix>-N") into a
memory volume that the relay container reads through NEXUS_RELAY_DATABASE_URL_FILE, so the
relay process never sees another node's password. It also writes the CA bundle the relay's
Redis client trusts (public roots plus the in-cluster Redis CA).
"""

import os
import pathlib
import sys
import time
from urllib.parse import quote

name = os.environ["POD_NAME"]
ordinal = int(name.rsplit("-", 1)[1])
slot = pathlib.Path(os.environ["NODE_SLOTS"]) / str(ordinal)
runtime = pathlib.Path(os.environ["NODE_RUNTIME"])
secret = f"{os.environ['NODE_SECRET_PREFIX']}-{ordinal}"

# The relay-node Job creates the Secret before the StatefulSet grows; the kubelet refreshes
# projected Secrets periodically, so a Secret created moments ago may still be on its way.
deadline = time.monotonic() + int(os.environ.get("NODE_WAIT_SECONDS", "300"))
while not all((slot / key).is_file() for key in ("username", "password", "node-id")):
    if time.monotonic() > deadline:
        print(
            f"relay node {ordinal}: Secret {secret} is missing. Scale relay.replicas with "
            "`helm upgrade` so the relay-node Job creates and registers it.",
            file=sys.stderr,
        )
        sys.exit(1)
    time.sleep(5)

username = (slot / "username").read_text().strip()
password = (slot / "password").read_text().strip()
node_id = (slot / "node-id").read_text().strip()
url = (
    f"postgres://{quote(username, safe='')}:{quote(password, safe='')}"
    f"@{os.environ['DATABASE_HOST']}:{os.environ['DATABASE_PORT']}/{os.environ['DATABASE_NAME']}"
    f"?{os.environ['DATABASE_QUERY']}"
)


def private(path: pathlib.Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)


private(runtime / "database-url", url)
bundle = pathlib.Path("/etc/ssl/certs/ca-certificates.crt").read_text()
for extra in filter(None, os.environ.get("EXTRA_CA_FILES", "").split(":")):
    bundle += "\n" + pathlib.Path(extra).read_text()
(runtime / "ca-bundle.crt").write_text(bundle)
print(f"relay node {ordinal}: database role {username}, node id {node_id}")
