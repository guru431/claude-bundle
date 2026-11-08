"""Optional Matrix mirror with durable, destination-bound replay of failed parts."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
PART_CHARS = 30000
PART_BYTES = 58000
DEFAULT_RETRY_GAPS = "10,30"
RETENTION_SEC = 7 * 86400
PENDING = Path(__file__).resolve().parents[1] / "state/matrix-pending"


def _load_dotenv() -> None:
    # Importing just the splitter/constants must not read local configuration.
    from utils import _load_dotenv as load
    load()


def content_bytes(text: str) -> bytes:
    return json.dumps({"msgtype": "m.text", "body": text}, ensure_ascii=False).encode("utf-8")


def split_text(text: str) -> list[str]:
    """Use the mirror's character and escaped UTF-8 byte limits."""
    chunks, current, used = [], [], 0
    for char in text.strip():
        width = len(json.dumps(char, ensure_ascii=False)[1:-1].encode("utf-8"))
        if len(current) >= PART_CHARS or used + width > PART_BYTES:
            chunks.append("".join(current))
            current, used = [], 0
        current.append(char)
        used += width
    chunks.append("".join(current))
    return [f"({n}/{len(chunks)})\n{chunk}" if len(chunks) > 1 else chunk
            for n, chunk in enumerate(chunks, 1)]


def settings() -> tuple[str, str, str]:
    _load_dotenv()
    config = tuple(os.environ.get(name, "") for name in
                   ("MATRIX_HOMESERVER", "MATRIX_ROOM_ID", "MATRIX_ACCESS_TOKEN"))
    hs, room, token = config
    if not any(config):
        return config
    if not (re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::[0-9]+)?", hs)
            and re.fullmatch(r"![A-Za-z0-9._=+/-]+(?::[A-Za-z0-9.-]+(?::[0-9]+)?)?", room)
            and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token)):
        raise ValueError("incomplete or invalid configuration")
    return config


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def put(config: tuple[str, str, str], item: dict) -> bool | int:
    hs, room, token = config
    url = f'{hs}/_matrix/client/v3/rooms/{quote(room, safe="")}/send/m.room.message/{item["txn"]}'
    request = Request(url, data=content_bytes(item["body"]), method="PUT",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json; charset=utf-8"})
    try:
        with build_opener(_NoRedirect).open(request, timeout=30) as response:
            return response.status == 200
    except HTTPError as exc:
        code = exc.code
        exc.close()
        return code
    except (OSError, URLError):
        return False


def _atomic(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(item, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _pending_item(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > PART_BYTES * 3:
        raise ValueError("invalid pending file")
    item = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(item, dict)
            or not re.fullmatch(r"alert-[a-f0-9]{32}-[0-9]+", str(item.get("txn", "")))
            or path.stem != item["txn"] or type(item.get("created")) not in (float, int)
            or not math.isfinite(item["created"])
            or not isinstance(item.get("homeserver"), str) or not isinstance(item.get("room"), str)
            or not isinstance(item.get("body"), str)
            or len(content_bytes(item["body"])) > PART_BYTES + 256):
        raise ValueError("invalid pending part")
    return item


def retry_gaps() -> list[int]:
    raw = os.environ.get("MATRIX_RETRY_GAPS", DEFAULT_RETRY_GAPS)
    if not re.fullmatch(r"[0-9,\s]*", raw):
        raw = DEFAULT_RETRY_GAPS
    return [int(gap) for gap in re.split(r"[,\s]+", raw.strip()) if gap]


def _send(path: Path, item: dict, config: tuple[str, str, str], gaps: list[int]) -> bool:
    for gap in [*gaps, None]:
        result = put(config, item)
        if result is True:
            path.unlink(missing_ok=True)
            return True
        if result is not False and result != 429 and not 500 <= result <= 599:
            break
        if gap is None:
            break
        time.sleep(gap)
    print("telegram-send: matrix: part pending for replay", file=sys.stderr)
    return False


def deliver(text: str, config: tuple[str, str, str], pending: Path = PENDING,
            *, gaps: list[int] | None = None, now: float | None = None) -> bool:
    if not config[2]:
        return True
    from utils import masked
    now = time.time() if now is None else now
    gaps = retry_gaps() if gaps is None else gaps
    batch = uuid.uuid4().hex
    items = []
    # Persist the whole batch before the first request; a killed sender keeps its tail.
    for n, part in enumerate(split_text(masked(text.replace(config[2], "***TOKEN***"))), 1):
        item = {"txn": f"alert-{batch}-{n}", "body": part, "created": now,
                "homeserver": config[0], "room": config[1]}
        path = pending / (item["txn"] + ".json")
        _atomic(path, item)
        items.append((path, item))
    results = [_send(path, item, config, gaps) for path, item in items]
    return all(results)


def replay(config: tuple[str, str, str], pending: Path = PENDING, *, apply: bool = False,
           gaps: list[int] | None = None, now: float | None = None, txn: str | None = None) -> int:
    now = time.time() if now is None else now
    remaining = 0
    for path in sorted(pending.glob("*.json")):
        if txn is not None and path.stem != txn:
            continue
        try:
            item = _pending_item(path)
            expired = now - item["created"] >= RETENTION_SEC
            matches = (item["homeserver"], item["room"]) == config[:2]
            print(f'{item["txn"]}: {"expired" if expired else "pending"}, destination_match={matches}')
            if not apply:
                remaining += 1
            elif expired:
                path.unlink(missing_ok=True)
            elif not matches or not config[2] or not _send(
                    path, item, config, retry_gaps() if gaps is None else gaps):
                remaining += 1
        except (OSError, ValueError, TypeError, KeyError):
            print("telegram-send: matrix: unreadable pending record retained", file=sys.stderr)
            remaining += 1
    return remaining


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--send", type=Path, help="send a message file; used by the shared shell sender")
    mode.add_argument("--replay", action="store_true", help="inspect pending parts without sending or writing")
    parser.add_argument("--apply", action="store_true", help="send pending parts and expire records after 7 days")
    parser.add_argument("--txn", help="inspect/replay only this transaction")
    args = parser.parse_args(argv)
    try:
        config = settings()
        if not args.send and not args.apply:
            replay(config, PENDING, txn=args.txn)
            return 0
        if args.send:
            # New alerts have unique IDs: a busy replay must never discard one
            # before it is queued. Concurrent PUTs for one ID are idempotent.
            for path in PENDING.glob("*.json"):
                try:
                    if time.time() - _pending_item(path)["created"] >= RETENTION_SEC:
                        path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
            return 0 if deliver(args.send.read_text(encoding="utf-8", errors="replace"), config, PENDING) else 1
        from utils import _file_lock
        with _file_lock(PENDING.parent / ".matrix-delivery.lock", wait=0, stale=600,
                        fail_open=False, label="matrix delivery", mode="os") as held:
            if not held:
                return 1
            return 0 if replay(config, PENDING, apply=True, txn=args.txn) == 0 else 1
    except (OSError, ValueError, KeyError, TypeError):
        print("telegram-send: matrix: incomplete or invalid configuration or local delivery failure; queue retained",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
