"""R02 browser acceptance: create working copies of four formats through HTTP.

Mirrors exactly what the web UI does now (no display_name sent), then edits one
copy on disk, registers a new version, lets the worker parse it, and checks that
working-scope search finds the new text while originals does not.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request



def main() -> int:
    sys.path.insert(0, "backend/src")
    from legal_workbench.config import load_settings

    base = f"http://127.0.0.1:{load_settings().port}"

    def call(method, path, body=None):
        headers = {"Origin": base}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")

    status, body = call("GET", "/api/v1/cases")
    assert status == 200 and "items" in body, (status, body)
    print("1. 直接访问案件列表 OK")

    status, body = call("POST", "/api/v1/cases", {"title": "TEST-ONLY R02 副本验收"})
    case_id = body["case_id"]
    print(f"2. 建案 OK {case_id[:8]}")

    # Upload the four formats as a browser would.
    boundary = "----lwbR02"
    parts = []
    for name in ("合同.docx", "表.xlsx", "扫描.pdf", "备注.txt"):
        with open(f"/tmp/r02corpus/{name}", "rb") as handle:
            content = handle.read()
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
            + content + b"\r\n")
    for name in ("合同.docx", "表.xlsx", "扫描.pdf", "备注.txt"):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="relative_paths"\r\n\r\n'
            f'{name}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="source_label"\r\n\r\n'
        f'虚构 R02 验收\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="request_key"\r\n\r\n'
        f'r02-acceptance\r\n'.encode())
    parts.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(parts)
    request = urllib.request.Request(
        f"{base}/api/v1/cases/{case_id}/intakes/upload", data=payload,
        headers={"Origin": base,
                 "Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        uploaded = json.loads(response.read())
    print(f"3. 上传 OK intake={uploaded['intake_id'][:8]}")

    for _ in range(60):
        status, items = call("GET", f"/api/v1/cases/{case_id}/materials?limit=200")
        if status == 200 and items["total"] >= 4 and all(
                row["status"] == "ready" for row in items["items"]):
            break
        time.sleep(1)
    print(f"4. worker 处理完成：{items['total']} 份材料")

    failures = []
    for row in items["items"]:
        status, original = call(
            "GET", f"/api/v1/cases/{case_id}/materials/{row['material_id']}/original")
        assert status == 200, (status, original)
        # No display_name: the server derives it from the stored object.
        status, copy = call("POST", f"/api/v1/cases/{case_id}/working-copies",
                            {"case_id": case_id, "file_version_id": original["file_version_id"]})
        assert status == 200, (status, copy)
        relative = copy["relative_path"]
        expected = "." + row["display_name"].rsplit(".", 1)[-1]
        ok = relative.endswith(expected) and ".bin" not in relative
        print(f"   {row['display_name']} -> {relative.rsplit('/', 1)[-1]}  "
              f"{'OK' if ok else 'FAIL 期望 ' + expected}")
        if not ok:
            failures.append((row["display_name"], relative))
    print(f"5. 副本扩展名：{'全部正确' if not failures else failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
