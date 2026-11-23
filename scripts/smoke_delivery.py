"""T20 delivery smoke test against the running local service.

Exercises the real HTTP surface end to end: read the case list, create a case, upload two
files as a browser would, wait for the worker to parse them, then search and read
a source.  Uses only synthetic TEST-ONLY content.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid



def main() -> int:
    sys.path.insert(0, "backend/src")
    from legal_workbench.config import load_settings

    base = f"http://127.0.0.1:{load_settings().port}"

    def call(method: str, path: str, *, body=None, raw=None, content_type=None):
        headers = {"Origin": base}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw
            headers["Content-Type"] = content_type
        request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    status, page = call("GET", "/api/v1/cases")
    assert status == 200 and "items" in page, (status, page)
    print("1. 直接访问案件列表 OK")

    status, case = call("POST", "/api/v1/cases", body={"title": "TEST-ONLY 交付验证案件"})
    assert status == 200, (status, case)
    case_id = case["case_id"]
    print(f"2. 建案 OK {case_id}")

    # Multipart upload, exactly as the browser panel builds it.
    boundary = "----lwb" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in (("case_id", case_id), ("source_label", "交付验证"),
                        ("request_key", uuid.uuid4().hex)):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     f"{value}\r\n".encode())
    files = [("虚构交付合同.txt", "TEST-ONLY 交付验证合同\n\n合同金额 5000元，签订日期 2024年3月5日。\n"),
             ("虚构交付备注.txt", "TEST-ONLY 备注\n\n补充说明：合同编号 HT-999。\n")]
    for name, text in files:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"relative_paths\"\r\n\r\n"
                     f"{name}\r\n".encode())
    for name, text in files:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
                      f"filename=\"{name}\"\r\nContent-Type: text/plain\r\n\r\n").encode()
                     + text.encode() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    status, intake = call("POST", f"/api/v1/cases/{case_id}/intakes/upload",
                          raw=b"".join(parts),
                          content_type=f"multipart/form-data; boundary={boundary}")
    assert status == 200, (status, intake)
    print(f"3. 上传 OK intake={intake['intake_id']} job={intake['job_id']}")

    deadline = time.time() + 90
    ready = False
    while time.time() < deadline:
        status, items = call("GET", f"/api/v1/cases/{case_id}/intakes/{intake['intake_id']}/items")
        assert status == 200, (status, items)
        states = {item["relative_source"]: item["status"] for item in items["items"]}
        if all(state in ("ready", "failed", "skipped") for state in states.values()):
            ready = True
            break
        time.sleep(2)
    print(f"4. worker 处理完成={ready} 逐项状态={json.dumps(states, ensure_ascii=False)}")
    assert ready and all(state == "ready" for state in states.values()), states

    status, materials = call("GET", f"/api/v1/cases/{case_id}/materials")
    assert status == 200 and materials["total"] == 2, (status, materials)
    for material in materials["items"]:
        coverage = material["coverage"]
        print(f"   {material['display_name']} 覆盖={coverage}")

    status, hits = call("POST", f"/api/v1/cases/{case_id}/search",
                        body={"case_id": case_id, "query": "合同", "mode": "literal"})
    assert status == 200, (status, hits)
    print(f"5. 检索「合同」命中 {hits['total']}，覆盖={hits['coverage']}")
    assert hits["total"] >= 2, hits

    status, amount = call("POST", f"/api/v1/cases/{case_id}/search",
                          body={"case_id": case_id, "query": "5000", "mode": "amount",
                                "currency": "CNY"})
    assert status == 200 and amount["total"] == 1, (status, amount)
    print(f"6. 金额 5000 CNY 命中 {amount['total']}，值={amount['hits'][0]['amount']}")

    source_ref = hits["hits"][0]["source"]["source_ref"]
    status, source = call("GET", f"/api/v1/cases/{case_id}/sources/{source_ref}?context_chars=40")
    assert status == 200, (status, source)
    print(f"7. read_source OK 片段={source['text'][:40]!r}")

    status, saved = call("POST", f"/api/v1/cases/{case_id}/analyses",
                         body={"case_id": case_id, "title": "TEST-ONLY 交付分析",
                               "content": "结论：材料齐全。",
                               "statement_kind": "agent_analysis", "source_refs": [source_ref]})
    assert status == 200, (status, saved)
    print(f"8. 保存分析 OK 版本={saved['version_id']}")

    status, bad = call("GET", "/api/v1/cases/not-a-uuid/materials")
    assert status == 422, (status, bad)
    print(f"9. 非法 UUID → {status} {bad['code']}")

    print("\nT20 交付冒烟：全部通过")
    print(f"CASE_ID={case_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
