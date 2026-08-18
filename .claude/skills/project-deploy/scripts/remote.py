"""在部署服务器上执行命令。

凭据只从环境变量读取，不带默认值：服务器地址和密码每次部署由用户当场提供，
写进文件就意味着它会进版本库、进日志、进别人的机器。

用法：

    $env:DEPLOY_HOST='...'; $env:DEPLOY_USER='...'; $env:DEPLOY_PASSWORD='...'
    python remote.py "systemctl status <服务名> --no-pager" "journalctl -u <服务名> -n 20"
"""

from __future__ import annotations

import os
import sys

import paramiko


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"缺少环境变量 {name}；本次部署的服务器信息需要由用户当场提供。")
    return value


def connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        _required("DEPLOY_HOST"),
        username=_required("DEPLOY_USER"),
        password=_required("DEPLOY_PASSWORD"),
        timeout=25,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def run(
    client: paramiko.SSHClient, command: str, *, quiet: bool = False, timeout: float = 900,
) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, 输出)。

    长耗时的操作（下载、编译）请在服务器上用 `nohup ... &` 后台跑再轮询，
    否则这里的超时会把它拦腰打断，留下半截文件。
    """

    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    body = (out + err).rstrip()
    if not quiet:
        print(f"$ {command}")
        if body:
            print(body)
        if code:
            print(f"[exit {code}]")
    return code, body


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        sys.exit("用法：remote.py \"命令1\" \"命令2\" ...")
    client = connect()
    try:
        for command in sys.argv[1:]:
            run(client, command)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
