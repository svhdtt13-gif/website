#!/usr/bin/env python3
"""Slice 11 direct service tests for fresh-list and delete response validation."""
import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "webapp", "backend")
sys.path.insert(0, BACKEND)
from repositories.aitool import UpstreamError
from services import backup

PASS = FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("PASS: " + name)
    else:
        FAIL += 1
        print("FAIL: " + name + " " + detail)


class FakeRepository:
    def __init__(self):
        self.list_result = (b'{"backups":[{"name":"cycle_test.zip"}]}', 200, "application/json")
        self.delete_result = (b'{"status":"OK","deleted":"cycle_test.zip"}', 200, "application/json")
        self.delete_calls = []

    def get(self, path):
        return self.list_result

    def delete_backup(self, name):
        self.delete_calls.append(name)
        return self.delete_result


def expect_generic_502(fake, label):
    backup.ai_tool = fake
    try:
        backup.delete_cycle_backup("cycle_test.zip")
        check(label + " -> raises generic 502", False, "no exception")
    except UpstreamError as error:
        check(label + " -> raises generic 502", error.status == 502
              and error.body == b'{"error":"backup deletion unavailable"}')
    check(label + " -> zero DELETE", fake.delete_calls == [])


def main():
    original = backup.ai_tool
    try:
        fake = FakeRepository()
        backup.ai_tool = fake
        result = backup.delete_cycle_backup("cycle_test.zip")
        check("valid service delete -> canonical success", result == (b'{"status":"OK","deleted":"cycle_test.zip"}', 200, "application/json"))
        check("valid service delete -> exact name call", fake.delete_calls == ["cycle_test.zip"])

        for body, status, content_type, label in (
            (b'{"backups":', 200, "application/json", "malformed fresh list"),
            (b'[{"name":"cycle_test.zip"}]', 200, "application/json", "non-object fresh list"),
            (b'{"backups":[{"size":1}]}', 200, "application/json", "missing fresh-list name"),
            (b'{"backups":[]}', 200, "application/json", "unknown fresh name"),
        ):
            fake = FakeRepository()
            fake.list_result = (body, status, content_type)
            if label != "unknown fresh name":
                expect_generic_502(fake, label)
            else:
                backup.ai_tool = fake
                try:
                    backup.delete_cycle_backup("cycle_test.zip")
                    check("unknown fresh name -> raises 409", False, "no exception")
                except UpstreamError as error:
                    check("unknown fresh name -> raises 409", error.status == 409
                          and error.body == b'{"error":"backup target unavailable"}')
                check("unknown fresh name -> zero DELETE", fake.delete_calls == [])

        fake = FakeRepository()
        fake.delete_result = (b'{"status":"OK"', 200, "application/json")
        backup.ai_tool = fake
        try:
            backup.delete_cycle_backup("cycle_test.zip")
            check("malformed delete success -> raises generic 502", False, "no exception")
        except UpstreamError as error:
            check("malformed delete success -> raises generic 502", error.status == 502
                  and error.body == b'{"error":"backup deletion unavailable"}')
        check("malformed delete success -> one DELETE", fake.delete_calls == ["cycle_test.zip"])
        print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
        return 0 if FAIL == 0 else 1
    finally:
        backup.ai_tool = original


if __name__ == "__main__":
    sys.exit(main())
