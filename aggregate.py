#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订阅聚合器 v1.0
============================================================
多源抓取 → 解析 → 去重 → TCP 粗筛 → 输出高质量订阅

设计原则
  · 纯 Python 标准库，不需要 pip install 任何东西
  · 只做"聚合 + 粗筛"，精确测速交给客户端（v2rayN / FlClash 内置）
  · 输出两种格式：base64 txt（通用）+ Clash YAML（规则模式）

用法
  python aggregate.py              # 默认：TCP 粗筛，3 秒超时
  python aggregate.py --no-check   # 跳过测活，只做合并去重（最快）
  python aggregate.py --timeout 5  # 调整 TCP 超时
  python aggregate.py --limit 300  # 只保留前 N 个节点
"""

import sys
import os
import re
import json
import base64
import socket
import time
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
# 输出目录固定在本脚本同级，保证「本地跑」和「GitHub Actions 跑」路径一致
# 用英文名是为了让订阅 URL 保持干净，避免出现 %E8%AE%A2%E9%98%85 这种编码
OUT_DIR = BASE / "output"
SOURCES_FILE = BASE / "sources.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SCHEMES = ("vless", "vmess", "trojan", "ss", "hysteria2", "hy2", "tuic")
NODE_RE = re.compile(r"(?:vless|vmess|trojan|ss|hysteria2|hy2|tuic)://[^\s\"'<>]+")

# mihomo 支持的 SS 加密方式白名单（不在其中的直接丢弃，否则整个配置会校验失败）
SS_CIPHERS = {
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "rc4-md5", "chacha20-ietf", "xchacha20", "chacha20",
    "bf-cfb", "cast5-cfb", "des-cfb", "idea-cfb", "rc2-cfb",
    "seed-cfb", "salsa20",
    "camellia-128-cfb", "camellia-192-cfb", "camellia-256-cfb",
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305", "none",
}


# ---------------------------------------------------------------- 工具函数
def b64pad(s):
    """补齐 base64 的 = 填充"""
    s = s.strip().replace("-", "+").replace("_", "/")
    return s + "=" * (-len(s) % 4)


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- 抓取
def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_body(raw):
    """从响应体里抽出节点链接。先按明文找，找不到再尝试 base64 解码。"""
    text = raw.decode("utf-8", "ignore")
    found = NODE_RE.findall(text)
    if found:
        return found
    try:
        dec = base64.b64decode(b64pad(text)).decode("utf-8", "ignore")
        return NODE_RE.findall(dec)
    except Exception:
        return []


# ---------------------------------------------------------------- 解析端点
def vmess_json(link):
    """vmess://<base64 json> → dict"""
    body = link[8:].split("#")[0]
    return json.loads(base64.b64decode(b64pad(body)).decode("utf-8", "ignore"))


def endpoint(link):
    """从节点链接里取出 (host, port)，失败返回 None"""
    try:
        if link.startswith("vmess://"):
            j = vmess_json(link)
            return str(j["add"]), int(j["port"])
        body = link.split("://", 1)[1].split("#")[0]
        hostport = body.split("@")[-1].split("?")[0].split("/")[0]
        host, _, port = hostport.rpartition(":")
        if not host or not port.isdigit():
            return None
        return host.strip("[]"), int(port)
    except Exception:
        return None


def fingerprint(link):
    """去重指纹：协议 + 端点 + 凭据"""
    ep = endpoint(link)
    if not ep:
        return None
    body = link.split("://", 1)[1].split("#")[0]
    cred = body.split("@")[0] if "@" in body else ""
    if link.startswith("vmess://"):
        try:
            cred = vmess_json(link).get("id", "")
        except Exception:
            cred = ""
    return "%s|%s|%s|%s" % (link.split("://")[0], ep[0], ep[1], cred[:40])


def node_name(link):
    """提取备注名"""
    if "#" in link:
        try:
            return unquote(link.split("#", 1)[1]).strip()[:60]
        except Exception:
            pass
    if link.startswith("vmess://"):
        try:
            return str(vmess_json(link).get("ps", ""))[:60]
        except Exception:
            pass
    ep = endpoint(link)
    return ("%s:%s" % ep) if ep else "node"


def is_reality_ws(link):
    """判断是否为 REALITY over WebSocket。

    Xray 26.x 只支持 REALITY over RAW / XHTTP / gRPC，不支持 WS；
    混入一个这样的节点会让内核直接拒绝整份配置（v2rayN 症状：10808 端口起不来）。
    mihomo / sing-box 支持该组合，所以只在输出 Xray 兼容版时剔除。
    """
    low = link.lower()
    return "security=reality" in low and "type=ws" in low


def count_nodes_in(path):
    """读取已有的 base64 订阅文件，返回其中节点数（失败返回 0）"""
    try:
        raw = base64.b64decode(b64pad(path.read_text(encoding="utf-8")))
        text = raw.decode("utf-8", "ignore")
        return sum(1 for line in text.splitlines() if "://" in line)
    except Exception:
        return 0


# ---------------------------------------------------------------- TCP 测活
def tcp_alive(host, port, timeout=3.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


# ---------------------------------------------------------------- Clash 输出
def _yaml_str(v):
    return json.dumps(str(v), ensure_ascii=False)


def to_clash(link, idx):
    """节点链接 → Clash proxy dict。不支持的协议返回 None。"""
    try:
        scheme = link.split("://", 1)[0]
        name = "N%03d-%s" % (idx, node_name(link))[:80]

        if scheme == "vmess":
            j = vmess_json(link)
            p = {
                "name": name, "type": "vmess", "server": str(j["add"]),
                "port": int(j["port"]), "uuid": str(j["id"]),
                "alterId": int(j.get("aid", 0) or 0), "cipher": "auto", "udp": True,
            }
            net = str(j.get("net", "tcp"))
            p["network"] = net
            if str(j.get("tls", "")) in ("tls", "reality"):
                p["tls"] = True
                if j.get("sni") or j.get("host"):
                    p["servername"] = str(j.get("sni") or j.get("host"))
            if net == "ws":
                p["ws-opts"] = {"path": str(j.get("path") or "/")}
                h = str(j.get("host") or "")
                if h:
                    p["ws-opts"]["headers"] = {"Host": h}
            return p

        # 其余协议统一走 URL 参数解析
        body = link.split("://", 1)[1].split("#")[0]
        main, _, query = body.partition("?")
        qs = {k: v[0] for k, v in parse_qs(query).items()}
        cred, _, hostport = main.rpartition("@")
        host, _, port = hostport.partition(":")

        if scheme == "vless":
            p = {
                "name": name, "type": "vless", "server": host,
                "port": int(port), "uuid": unquote(cred), "udp": True,
                "tls": qs.get("security", "none") in ("tls", "reality"),
            }
            if qs.get("flow"):
                p["flow"] = qs["flow"]
            sni = qs.get("sni") or qs.get("peer")
            if sni:
                p["servername"] = sni
            net = qs.get("type", "tcp")
            p["network"] = net
            if net == "ws":
                p["ws-opts"] = {"path": unquote(qs.get("path", "/"))}
                if qs.get("host"):
                    p["ws-opts"]["headers"] = {"Host": qs["host"]}
            elif net == "grpc":
                p["grpc-opts"] = {"grpc-service-name": qs.get("serviceName", "")}
            if qs.get("security") == "reality":
                ro = {}
                if qs.get("pbk"):
                    ro["public-key"] = qs["pbk"]
                if qs.get("sid"):
                    ro["short-id"] = qs["sid"]
                if ro:
                    p["reality-opts"] = ro
            if qs.get("fp"):
                p["client-fingerprint"] = qs["fp"]
            return p

        if scheme == "trojan":
            p = {
                "name": name, "type": "trojan", "server": host, "port": int(port),
                "password": unquote(cred), "udp": True, "tls": True,
                "skip-cert-verify": True,
            }
            if qs.get("sni"):
                p["sni"] = qs["sni"]
            net = qs.get("type", "tcp")
            p["network"] = net
            if net == "ws":
                p["ws-opts"] = {"path": unquote(qs.get("path", "/"))}
                if qs.get("host"):
                    p["ws-opts"]["headers"] = {"Host": qs["host"]}
            return p

        if scheme == "ss":
            raw = main
            if "@" not in raw:                      # 整体 base64 的老格式
                dec = base64.b64decode(b64pad(raw)).decode("utf-8", "ignore")
                cred, _, hostport = dec.rpartition("@")
                host, _, port = hostport.partition(":")
                method, _, pwd = cred.partition(":")
            else:
                dec = base64.b64decode(b64pad(cred)).decode("utf-8", "ignore")
                method, _, pwd = dec.partition(":")
            method = method.strip().lower()
            if method not in SS_CIPHERS:
                return None
            return {
                "name": name, "type": "ss", "server": host, "port": int(port),
                "cipher": method, "password": pwd, "udp": True,
            }

        if scheme in ("hysteria2", "hy2"):
            p = {
                "name": name, "type": "hysteria2", "server": host, "port": int(port),
                "password": unquote(cred), "skip-cert-verify": True,
            }
            if qs.get("sni"):
                p["sni"] = qs["sni"]
            if qs.get("obfs"):
                p["obfs"] = qs["obfs"]
                if qs.get("obfs-password"):
                    p["obfs-password"] = qs["obfs-password"]
            return p

        return None
    except Exception:
        return None


def yaml_block(d, indent=2):
    """极简 dict → YAML（只覆盖 Clash proxy 需要的类型）"""
    pad = " " * indent
    out = []
    for k, v in d.items():
        if isinstance(v, dict):
            out.append("%s%s:" % (pad, k))
            out.append(yaml_block(v, indent + 2))
        elif isinstance(v, bool):
            out.append("%s%s: %s" % (pad, k, "true" if v else "false"))
        elif isinstance(v, (int, float)):
            out.append("%s%s: %s" % (pad, k, v))
        else:
            out.append("%s%s: %s" % (pad, k, _yaml_str(v)))
    return "\n".join(out)


def build_clash_yaml(links):
    proxies, names = [], []
    for i, link in enumerate(links, 1):
        p = to_clash(link, i)
        if p:
            proxies.append(p)
            names.append(p["name"])
    if not proxies:
        return None

    head = [
        "# 由 aggregate.py 自动生成 · %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "mixed-port: 7890",
        "allow-lan: false",
        "mode: rule",
        "log-level: info",
        "ipv6: false",
        "proxies:",
    ]
    body = []
    for p in proxies:
        first = True
        for k, v in p.items():
            block = yaml_block({k: v}, 4)
            body.append(("  - " + block.lstrip()) if first else block)
            first = False

    groups = [
        "proxy-groups:",
        "  - name: 自动选择",
        "    type: url-test",
        "    url: https://www.gstatic.com/generate_204",
        "    interval: 300",
        "    tolerance: 50",
        "    proxies:",
    ]
    for n in names:
        groups.append("      - %s" % _yaml_str(n))
    groups += [
        "  - name: 节点选择",
        "    type: select",
        "    proxies:",
        '      - 自动选择',
    ]
    for n in names:
        groups.append("      - %s" % _yaml_str(n))

    tail = [
        "rules:",
        "  - GEOIP,CN,DIRECT",
        "  - MATCH,节点选择",
    ]
    return "\n".join(head + body + groups + tail) + "\n"


# ---------------------------------------------------------------- 主流程
def read_sources():
    if not SOURCES_FILE.exists():
        log("[!] 找不到 sources.txt：%s" % SOURCES_FILE)
        return []
    urls = []
    for line in SOURCES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.startswith("http"):
            urls.append(line)
    return urls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-check", action="store_true", help="跳过 TCP 测活")
    ap.add_argument("--timeout", type=float, default=3.0, help="TCP 超时秒数")
    ap.add_argument("--limit", type=int, default=800,
                    help="最终保留节点数上限（默认 800，防止客户端卡死；0=不限）")
    ap.add_argument("--workers", type=int, default=64, help="并发数")
    ap.add_argument("--pre-limit", type=int, default=3000,
                    help="测活前先按源优先级截断到 N 个（默认 3000，0=不限）")
    ap.add_argument("--min-ratio", type=float, default=0.3,
                    help="缩水守门阈值：本轮少于上轮×该比例则保留旧结果（默认 0.3）")
    ap.add_argument("--force", action="store_true",
                    help="强制覆盖输出，跳过缩水守门")
    args = ap.parse_args()

    t0 = time.time()
    urls = read_sources()
    if not urls:
        log("[!] 源列表为空，退出")
        return 1

    log("=" * 62)
    log("  订阅聚合器 · %d 个源" % len(urls))
    log("=" * 62)

    # ① 抓取（记录源序号，用于后续按源质量排序）
    all_nodes, seen, stat = [], set(), []
    with ThreadPoolExecutor(max_workers=min(16, len(urls))) as ex:
        futs = {ex.submit(fetch, u): (i, u) for i, u in enumerate(urls)}
        for fut in as_completed(futs):
            si, u = futs[fut]
            short = u.split("/")[2] + "/" + u.rstrip("/").split("/")[-1][:28]
            try:
                nodes = parse_body(fut.result())
                new = 0
                for n in nodes:
                    fp = fingerprint(n)
                    if fp and fp not in seen:
                        seen.add(fp)
                        all_nodes.append((si, n))
                        new += 1
                stat.append((short, len(nodes), new, "ok"))
                log("  [ok]   %-52s 解析 %4d  新增 %4d" % (short, len(nodes), new))
            except Exception as e:
                stat.append((short, 0, 0, "fail"))
                log("  [fail] %-52s %s" % (short, str(e)[:30]))

    # 按源在 sources.txt 里的顺序排序 —— 靠前的源质量更高，优先保留
    all_nodes.sort(key=lambda x: x[0])
    all_nodes = [n for _, n in all_nodes]

    log("-" * 62)
    log("  抓取完成：%d 个节点（去重后）· 耗时 %.1fs" % (len(all_nodes), time.time() - t0))
    if not all_nodes:
        log("[!] 没有抓到任何节点")
        return 1

    # ② TCP 粗筛（先按源优先级预截断，否则上万节点测活要跑十几分钟）
    if not args.no_check:
        pre = args.pre_limit
        if pre and len(all_nodes) > pre:
            log("-" * 62)
            log("  按源优先级预截断：%d → %d 个送测" % (len(all_nodes), pre))
            all_nodes = all_nodes[:pre]
        log("-" * 62)
        log("  TCP 连通性粗筛（超时 %.1fs，并发 %d）..." % (args.timeout, args.workers))
        alive = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {}
            for n in all_nodes:
                ep = endpoint(n)
                if ep:
                    futs[ex.submit(tcp_alive, ep[0], ep[1], args.timeout)] = n
            done = 0
            for fut in as_completed(futs):
                done += 1
                if fut.result():
                    alive.append(futs[fut])
                if done % 200 == 0:
                    log("    已测 %d / %d，存活 %d" % (done, len(futs), len(alive)))
        log("  粗筛结果：%d → %d 个存活（剔除 %d 个死节点）"
            % (len(all_nodes), len(alive), len(all_nodes) - len(alive)))
        # 恢复源优先级顺序（as_completed 是乱序）
        order = {n: i for i, n in enumerate(all_nodes)}
        alive.sort(key=lambda n: order.get(n, 999999))
        all_nodes = alive

    if args.limit and len(all_nodes) > args.limit:
        all_nodes = all_nodes[:args.limit]
        log("  按 --limit 截断到 %d 个" % args.limit)

    # 缩水守门：本轮结果远少于上一轮时，保留上一轮输出
    # 目的：某轮所有源同时挂掉时，不要把订阅覆盖成空的，否则客户端直接断网
    if not args.force:
        prev = count_nodes_in(OUT_DIR / "merged.txt")
        if prev and len(all_nodes) < prev * args.min_ratio:
            log("-" * 62)
            log("  [!] 缩水守门触发：本轮 %d 个，上一轮 %d 个，低于阈值 %.0f%%"
                % (len(all_nodes), prev, args.min_ratio * 100))
            log("      已保留上一轮输出，本次不覆盖。")
            log("      确认要强制覆盖请加 --force")
            log("=" * 62)
            return 2
        if prev:
            log("  缩水守门：本轮 %d 个 / 上一轮 %d 个，通过" % (len(all_nodes), prev))

    # ③ 输出
    OUT_DIR.mkdir(exist_ok=True)
    txt = "\n".join(all_nodes)
    (OUT_DIR / "merged.txt").write_text(
        base64.b64encode(txt.encode()).decode(), encoding="utf-8")

    # 额外生成 Xray 兼容版（始终生成，避免文件时有时无导致订阅 404）
    # Xray 26.x 不支持 REALITY over WebSocket（只支持 RAW / XHTTP / gRPC），
    # 而 v2rayN 默认用 Xray 内核 —— 混入一个这样的节点会让整份配置被拒绝、端口起不来。
    # mihomo / sing-box 则支持，所以 merged.txt 保留全量。
    xray_ok = [n for n in all_nodes if not is_reality_ws(n)]
    blocked = len(all_nodes) - len(xray_ok)
    (OUT_DIR / "merged-xray.txt").write_text(
        base64.b64encode("\n".join(xray_ok).encode()).decode(), encoding="utf-8")

    clash = build_clash_yaml(all_nodes)
    if clash:
        (OUT_DIR / "merged.yaml").write_text(clash, encoding="utf-8")

    (OUT_DIR / "merged.urls").write_text(txt, encoding="utf-8")

    log("-" * 62)
    log("  输出完成 · 总耗时 %.1fs" % (time.time() - t0))
    log("    %s  (%d 节点 · base64 通用格式)" % (OUT_DIR / "merged.txt", len(all_nodes)))
    if clash:
        log("    %s  (Clash 规则模式)" % (OUT_DIR / "merged.yaml"))
    log("    %s  (明文，可肉眼检查)" % (OUT_DIR / "merged.urls"))
    if blocked:
        log("    %s  (%d 节点 · v2rayN/Xray 专用，已剔除 %d 个 REALITY+WS)"
            % (OUT_DIR / "merged-xray.txt", len(xray_ok), blocked))
    else:
        log("    %s  (%d 节点 · v2rayN/Xray 专用，本轮无不兼容节点)"
            % (OUT_DIR / "merged-xray.txt", len(xray_ok)))
    log("    ↑ Xray 不支持 REALITY over WS，用 v2rayN 请选这一份")
    log("=" * 62)
    log("  下一步：把 merged.txt 或 merged.yaml 导入客户端，")
    log("         在客户端里点一次「测延迟/测速」，它会帮你排好序。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n已取消")
        sys.exit(130)
