#!/usr/bin/env python3
"""validate_compose.py — структурная валидация deploy/compose.yml ботов botkit.

Ловит класс багов D2-fix (2026-09-25):/duplicate mapping keys, которых docker
compose не замечает (last-wins), healthcheck на 127.0.0.1:6380 вместо redis,
отсутствие host-gateway и т.п.

Usage:
    python3 validate_compose.py <path/to/compose.yml> [--bot=<name>]

Exit 0 = PASS, 1 = FAIL (с именем проблемы).
Совместим с PyYAML (yaml.safe_load) и Python 3.10+.
"""

import re
import sys

import yaml

EXPECTED_ENV = {
    "BIND_HOST", "METRICS_PORT", "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
    "WEBHOOK_SECRET_TOKEN", "WEBHOOK_URL", "WEBHOOK_CERT_PATH", "ADMIN_PASSWORD",
    "ADMIN_IDS", "REDIS_URL", "DATABASE_URL", "DB_PATH", "OTEL_EXPORTER_OTLP_ENDPOINT",
}
REQUIRED_SERVICE_KEYS = {
    "image", "container_name", "user", "cap_drop", "security_opt", "tmpfs",
    "networks", "extra_hosts", "environment", "ports", "volumes", "healthcheck",
    "restart", "mem_limit",
}


def fail(msg: str) -> str:
    return f"FAIL: {msg}"


def check_duplicates(root) -> list[str]:
    """Detect duplicate mapping keys across the whole document.

    docker compose (go-yaml) silently uses the last value; we want FAIL.
    """
    problems: list[str] = []

    def walk(node):
        if isinstance(node, yaml.MappingNode):
            seen = {}
            for key_node, _ in node.value:
                key = key_node.value if isinstance(key_node, yaml.ScalarNode) else repr(key_node)
                if key in seen:
                    problems.append(f"duplicate key '{key}' at line {key_node.start_mark.line + 1}")
                else:
                    seen[key] = True
            for _, val in node.value:
                walk(val)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item)

    walk(root)
    return problems


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if not args:
        print("Usage: validate_compose.py <compose.yml> [--bot=<name>]")
        return 2
    path = args[0]
    bot = None
    for a in args[1:]:
        if a.startswith("--bot="):
            bot = a[6:]
    if bot and bot.startswith("botkit-"):
        bot = bot[len("botkit-"):]
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(fail(f"cannot read {path}: {e}"))
        return 1

    # 1) duplicate keys on raw node tree (before safe_load loses them)
    try:
        node = yaml.compose(text)
    except yaml.YAMLError as e:
        print(fail(f"yaml parse error: {e}"))
        return 1
    dups = check_duplicates(node)
    if dups:
        for d in dups:
            print(fail(d))
        return 1

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        print(fail(f"yaml load error: {e}"))
        return 1
    if not isinstance(data, dict) or "services" not in data:
        print(fail("top-level 'services' missing"))
        return 1
    services = data["services"]
    if "bot" not in services:
        print(fail("service 'bot' missing"))
        return 1
    bot_svc = services["bot"]

    svc_missing = REQUIRED_SERVICE_KEYS - set(bot_svc)
    if svc_missing:
        print(fail(f"service.bot missing keys: {sorted(svc_missing)}"))
        return 1

    checks: list[tuple[str, bool]] = []
    checks.append(("user == 1001:1001", bot_svc.get("user") == "1001:1001"))
    checks.append(("cap_drop == [ALL]", bot_svc.get("cap_drop") == ["ALL"]))
    sec = bot_svc.get("security_opt") or []
    checks.append(
        ("security_opt contains no-new-privileges",
         any("no-new-privileges" in str(s) for s in sec))
    )
    tmpfs = bot_svc.get("tmpfs")
    checks.append(("tmpfs contains /tmp",
                   isinstance(tmpfs, list) and "/tmp" in tmpfs))

    # environment: list of KEY=... entries, each unique
    env = bot_svc.get("environment")
    if not isinstance(env, list):
        print(fail("service.bot.environment must be a list (KEY=value entries)"))
        return 1
    env_keys = []
    for e in env:
        m = re.match(r"^([A-Z0-9_]+)=", str(e))
        if not m:
            print(fail(f"environment entry not KEY=value: {e!r}"))
            return 1
        env_keys.append(m.group(1))
    dup_env = sorted({k for k in env_keys if env_keys.count(k) > 1})
    if dup_env:
        print(fail(f"duplicate environment keys: {dup_env}"))
        return 1
    missing_env = EXPECTED_ENV - set(env_keys)
    if missing_env:
        print(fail(f"environment missing keys: {sorted(missing_env)}"))
        return 1
    checks.append(("environment has 13 unique expected keys",
                   set(env_keys) == EXPECTED_ENV))

    # extra_hosts
    eh = bot_svc.get("extra_hosts")
    checks.append(
        ("extra_hosts contains host.docker.internal:host-gateway",
         isinstance(eh, list) and any("host.docker.internal:host-gateway" in str(x) for x in eh))
    )

    # ports: loopback publish
    ports = bot_svc.get("ports")
    p_ok = isinstance(ports, list) and all(
        str(p).startswith("127.0.0.1:") and "${METRICS_PORT}" in str(p)
        for p in ports
    )
    checks.append(("ports are 127.0.0.1:${METRICS_PORT}", p_ok))

    # healthcheck: must reach redis:6379 (NOT 127.0.0.1:6380), include redis host
    hc = bot_svc.get("healthcheck") or {}
    hc_test = " ".join(str(x) for x in (hc.get("test") or []))
    checks.append(
        ("healthcheck targets redis:6379 (and not 127.0.0.1:6380)",
         "redis" in hc_test and "6379" in hc_test and "127.0.0.1:6380" not in hc_test)
    )

    # service.bot.networks: must reference the default network (with alias)
    svc_nets = bot_svc.get("networks") or {}
    svc_net_cfg = svc_nets.get("default") if isinstance(svc_nets, dict) else None
    if not isinstance(svc_net_cfg, dict):
        print(fail("service.bot.networks.default missing"))
        return 1
    checks.append(("service network default has aliases",
                   svc_net_cfg.get("aliases") not in (None, [])))

    # top-level networks.default: name == botkit_<bot>, external
    top_nets = data.get("networks") or {}
    top_net = top_nets.get("default") if isinstance(top_nets, dict) else None
    if not isinstance(top_net, dict):
        print(fail("top-level networks.default missing"))
        return 1
    expect_net = f"botkit_{bot}" if bot else None
    net_name = top_net.get("name")
    if expect_net:
        checks.append((f"network name == {expect_net}", net_name == expect_net))
    checks.append(("network external == true", top_net.get("external") is True))

    # top-level compose 'name'
    checks.append(("top-level name present", bool(data.get("name"))))

    for label, ok in checks:
        if ok:
            print(f"  ok  {label}")
        else:
            print(fail(label))
    if all(ok for _, ok in checks) and dups == []:
        print("VALIDATE-COMPOSE: PASS")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
