"""把当前工作树的代码推到部署服务器并重启服务。

只上传服务器真正要跑的东西：包源码、工作台前端、插件定义。虚拟环境、数据库、
上传文件和密钥都留在本地——服务器有它自己的环境和它自己的工作区，一次更新代码
不应该动线上的频道、消息和文件。

凭据每次由用户当场提供，只走环境变量：

    $env:DEPLOY_HOST='...'
    $env:DEPLOY_USER='...'
    $env:DEPLOY_PASSWORD='...'
    .\\.venv\\Scripts\\python.exe .claude\\skills\\project-deploy\\scripts\\deploy.py

可选：DEPLOY_REMOTE_DIR（默认 /opt/touyan）、DEPLOY_SERVICE（默认 touyan）、
DEPLOY_PORT（默认 8788）、DEPLOY_URL（公网地址，给完就顺便探活）。
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from remote import connect, run  # noqa: E402

# 仓库根目录：本文件在 <root>/.claude/skills/project-deploy/scripts/ 下
ROOT = Path(__file__).resolve().parents[4]

REMOTE_DIR = os.environ.get("DEPLOY_REMOTE_DIR", "/opt/touyan")
SERVICE = os.environ.get("DEPLOY_SERVICE", "touyan")
PORT = os.environ.get("DEPLOY_PORT", "8788")
PUBLIC_URL = os.environ.get("DEPLOY_URL", "")

INCLUDE = ["src", ".openharness/plugins", "pyproject.toml"]
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".git", ".venv"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def build_archive() -> bytes:
    buffer = io.BytesIO()
    count = 0
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for entry in INCLUDE:
            source = ROOT / entry
            if not source.exists():
                sys.exit(f"找不到 {source}；请在项目根目录运行，或检查 INCLUDE。")
            if source.is_file():
                archive.add(source, arcname=entry)
                count += 1
                continue
            for path in source.rglob("*"):
                if (
                    not path.is_file()
                    or any(part in SKIP_DIRS for part in path.parts)
                    or path.suffix in SKIP_SUFFIX
                ):
                    continue
                archive.add(path, arcname=str(path.relative_to(ROOT)).replace("\\", "/"))
                count += 1
    print(f"打包 {count} 个文件，{buffer.tell() / 1024:,.0f} KB")
    return buffer.getvalue()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    payload = build_archive()
    client = connect()
    try:
        started = time.time()
        sftp = client.open_sftp()
        with sftp.file("/tmp/deploy-payload.tar.gz", "wb") as handle:
            handle.set_pipelined(True)
            handle.write(payload)
        sftp.close()
        print(f"上传完成，用时 {time.time() - started:.1f}s")

        # 换掉代码，保留工作区（数据库和上传目录不在 INCLUDE 里，也不在这里删）
        run(client, f"rm -rf {REMOTE_DIR}/src {REMOTE_DIR}/.openharness/plugins")
        run(client, f"mkdir -p {REMOTE_DIR} && tar -xzf /tmp/deploy-payload.tar.gz -C {REMOTE_DIR}")
        run(client, f"systemctl restart {SERVICE}")
        # 等端口真的起来，而不是睡固定秒数：小机器上进程从 active 到开始监听可能要
        # 七八秒，睡四秒就会得到一个吓人的 502，而服务其实好好的。
        code, out = run(
            client,
            "for i in $(seq 1 30); do "
            f"s=$(curl -s -o /dev/null -w '%{{http_code}}' -m 5 http://127.0.0.1:{PORT}/); "
            "if [ \"$s\" = 200 ]; then echo \"local 200 (${i}s)\"; exit 0; fi; sleep 1; done; "
            "echo \"local 未就绪，最后状态 $s\"; exit 1",
        )
        run(client, f"systemctl is-active {SERVICE}")
        if PUBLIC_URL:
            run(client, f"curl -s -o /dev/null -w 'public %{{http_code}}\\n' -m 25 '{PUBLIC_URL}'")
        # 状态码只是起点：还要在浏览器里确认控制台无报错、并实发一条消息看 Agent 回复。
        print("\n下一步：打开站点，确认控制台 0 报错，并发一个话题看 Agent 是否真的回复。")
        return code
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
