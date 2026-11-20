"""Serve a task-scoped VISTA directory to the native DSH plugin over stdio."""

from __future__ import annotations

import base64
import json
import sys
from hashlib import sha256

from vista_memory import VistaError, VistaStore
from vista_memory.history_catalog import HistoryCatalog
from vista_memory.history_store import TaskHistory


def _view(view):
    return {
        "text": view.text,
        "view_id": view.view_id,
        "version": view.version,
        "covered_short_ids": view.covered_short_ids,
        "visible_short_ids": view.visible_short_ids,
        "entries": view.entries,
        "token_count": view.token_count,
        "token_count_kind": view.token_count_kind,
    }


class Bridge:
    def __init__(self, config):
        self.store = VistaStore(config["vistaRoot"])
        self.config = config
        self.fixed = config.get("fixedView")
        self.digests = {}
        self.turn = None
        self.reader = None
        if self.fixed is None:
            self.history = TaskHistory(
                self.store, config["taskDir"], task_id=config["taskId"]
            )
            counter = None
            if config.get("tokenizerPath"):
                from tokenizers import Tokenizer

                tokenizer = Tokenizer.from_file(config["tokenizerPath"])

                def counter(text):
                    return len(tokenizer.encode(text, add_special_tokens=False).ids)

            self.catalog = HistoryCatalog(self.history, count_tokens=counter)

    def digest(self, record):
        if record not in self.digests:
            try:
                image = self.store.read_record(record, content="image").image
            except VistaError:
                return None
            if image is not None:
                self.digests[record] = sha256(image).hexdigest()
        return self.digests.get(record)

    def rows(self):
        records = (
            self.fixed["records"]
            if self.fixed is not None
            else [
                {
                    "short_id": item.short_id,
                    "record": item.record,
                    "receipt": item.receipt,
                    "label": item.label,
                }
                for item in self.history.list_records()
            ]
        )
        return [
            {**record, "sha256": self.digest(record["record"])} for record in records
        ]

    def run(self, action, args):
        if action == "rows":
            return self.rows()
        if self.fixed is not None:
            if action in {"view", "get_view"}:
                return self.fixed["view"]
            if action == "read":
                self.prepare_turn(args.pop("turn"), args.pop("limit"))
                short_id = args.pop("short_id")
                for row in self.fixed["records"]:
                    if row["short_id"] == short_id:
                        if self.reader == 0:
                            raise VistaError("本轮停止读图")
                        self.reader -= 1
                        result = self.store.read_record(row["record"], **args)
                        return self.materials(short_id, result)
                raise VistaError("请刷新目录核对编号")
            raise VistaError("固定目录仅支持查询和读图")
        if action == "view":
            return _view(self.catalog.short_view(**args))
        if action == "get_view":
            return _view(self.catalog.get_view(**args))
        if action == "current_version":
            return self.catalog.current_version()
        if action == "expand":
            page = self.catalog.expand(**args)
            return {
                "text": page.text,
                "short_ids": page.short_ids,
                "next_cursor": page.next_cursor,
            }
        if action == "read":
            self.prepare_turn(args.pop("turn"), args.pop("limit"))
            result = self.reader.read(**args)
            return self.materials(args["short_id"], result)
        if action == "save":
            item = self.history.save(
                png=base64.b64decode(args.pop("png"), validate=True), **args
            )
            return {"short_id": item.short_id, "receipt": item.receipt}
        if action in {"user_feedback", "observe"}:
            write = (
                self.catalog.append_user_feedback
                if action == "user_feedback"
                else self.catalog.append_agent_observation
            )
            result = write(**args)
            return result is not None
        raise ValueError(f"Unknown VISTA history action {action!r}")

    def prepare_turn(self, turn, limit):
        if turn == self.turn:
            return
        self.turn = turn
        self.reader = (
            limit
            if self.fixed is not None
            else self.catalog.new_turn(image_limit=limit)
        )

    @staticmethod
    def materials(short_id, result):
        return {
            "metadata": {"short_id": short_id, **result.metadata},
            "image": (
                None
                if result.image is None
                else base64.b64encode(result.image).decode("ascii")
            ),
            "code": result.code,
        }


def main():
    bridge = Bridge(json.loads(sys.stdin.readline()))
    for line in sys.stdin:
        request = json.loads(line)
        response = {"id": request["id"]}
        try:
            response["result"] = bridge.run(request["action"], request["args"])
        except Exception as error:
            response["error"] = {"type": type(error).__name__, "message": str(error)}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
