import asyncio
import re

IHOR_HOST = "95.81.76.197"
IHOR_USER = "root"
IHOR_SSH_KEY = "/home/ubuntu/.ssh/ihor_tunnel"

COMBINED_CMD = "top -bn1 | grep 'Cpu(s)'; free -m; df -h /"


async def _run_local(cmd: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    return stdout.decode(errors="ignore")


async def _run_remote(cmd: str) -> str:
    ssh_cmd = (
        f"ssh -i {IHOR_SSH_KEY} -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "
        f"{IHOR_USER}@{IHOR_HOST} \"{cmd}\""
    )
    return await _run_local(ssh_cmd)


def _parse_stats(raw: str) -> dict:
    result = {"cpu": "н/д", "ram": "н/д", "swap": "н/д", "disk": "н/д"}

    cpu_match = re.search(r"Cpu\(s\):\s*([\d.]+)\s*us,\s*([\d.]+)\s*sy", raw)
    if cpu_match:
        used = float(cpu_match.group(1)) + float(cpu_match.group(2))
        result["cpu"] = f"{used:.1f}%"

    mem_match = re.search(r"Mem:\s+(\d+)\s+(\d+)", raw)
    if mem_match:
        total, used = int(mem_match.group(1)), int(mem_match.group(2))
        pct = (used / total * 100) if total else 0
        result["ram"] = f"{used} / {total} МБ ({pct:.0f}%)"

    swap_match = re.search(r"Swap:\s+(\d+)\s+(\d+)", raw)
    if swap_match:
        total, used = int(swap_match.group(1)), int(swap_match.group(2))
        if total:
            pct = used / total * 100
            result["swap"] = f"{used} / {total} МБ ({pct:.0f}%)"
        else:
            result["swap"] = "нет"

    disk_match = re.search(r"(\S+)\s+(\S+)\s+(\S+)\s+(\d+)%\s+/", raw)
    if disk_match:
        size, used, avail, pct = disk_match.groups()
        result["disk"] = f"{used} / {size} ({pct}%), свободно {avail}"

    return result


async def get_oracle_stats() -> dict:
    try:
        raw = await _run_local(COMBINED_CMD)
        return _parse_stats(raw)
    except Exception:
        return {"cpu": "ошибка", "ram": "ошибка", "swap": "ошибка", "disk": "ошибка"}


async def get_ihor_stats() -> dict:
    try:
        raw = await _run_remote(COMBINED_CMD)
        return _parse_stats(raw)
    except Exception:
        return {"cpu": "ошибка", "ram": "ошибка", "swap": "ошибка", "disk": "ошибка"}
