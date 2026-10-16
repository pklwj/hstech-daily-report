#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日美债 + 恒生科技晨报
数据源：
  - 美联储 H.15 利率 RSS（10年期国债收益率）
  - 美联储新闻稿 / 讲话 RSS（政策动向）
  - 腾讯行情接口（恒生科技指数 HSTECH）
送达：
  - QQ 邮箱 SMTP 发信
环境变量（由 GitHub Secrets 注入）：
  MAIL_USER       发件邮箱，如 pklwj@qq.com
  MAIL_AUTH_CODE  QQ邮箱 SMTP 授权码（16位），非登录密码
  MAIL_TO         收件邮箱，默认同 MAIL_USER
"""

import os
import re
import sys
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from xml.etree import ElementTree as ET

CST = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_AUTH_CODE = os.environ.get("MAIL_AUTH_CODE", "")
MAIL_TO = os.environ.get("MAIL_TO", "") or MAIL_USER


def fetch(url, headers=None, timeout=25):
    """带 UA 的 GET，返回文本；失败返回 None。"""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return raw.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[warn] fetch failed {url}: {e}", file=sys.stderr)
        return None


# ---------- 1. 美债：美联储 H.15 ----------

def get_treasury():
    """从美国财政部官方 CSV 取最近两个交易日的国债收益率曲线。
    数据源：home.treasury.gov Daily Treasury Par Yield Curve Rates
    CSV 首行表头形如：Date,"1 Mo",...,"10 Yr",...
    返回 {date, rate10y, prev_date, prev_rate10y, delta_bp, curve:{...}}
    """
    year = datetime.now(CST).year
    urls = []
    for y in (year, year - 1):
        urls.append(
            "https://home.treasury.gov/resource-center/data-chart-center/"
            f"interest-rates/daily-treasury-rates.csv/{y}/all"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv"
        )

    txt = None
    for u in urls:
        txt = fetch(u)
        if txt and "10 Yr" in txt:
            break

    if not txt or "10 Yr" not in txt:
        return None

    rows = [r for r in txt.splitlines() if r.strip()]
    if len(rows) < 2:
        return None

    # 解析表头
    header = [c.strip().strip('"') for c in rows[0].split(",")]
    try:
        idx_date = 0
        idx_10y = header.index("10 Yr")
        idx_2y = header.index("2 Yr") if "2 Yr" in header else None
        idx_30y = header.index("30 Yr") if "30 Yr" in header else None
    except ValueError as e:
        print(f"[warn] treasury header parse: {e}", file=sys.stderr)
        return None

    def parse_row(r):
        c = [x.strip() for x in r.split(",")]
        if len(c) <= idx_10y:
            return None
        try:
            d = c[idx_date]
            v10 = float(c[idx_10y])
        except ValueError:
            return None
        return {
            "date": d,
            "rate10y": v10,
            "rate2y": float(c[idx_2y]) if idx_2y is not None and c[idx_2y] else None,
            "rate30y": float(c[idx_30y]) if idx_30y is not None and c[idx_30y] else None,
        }

    # CSV 通常按日期倒序（最新在前）
    parsed = [p for p in (parse_row(r) for r in rows[1:]) if p]
    if not parsed:
        return None

    cur = parsed[0]
    prev = parsed[1] if len(parsed) > 1 else None

    delta_bp = None
    if prev:
        delta_bp = round((cur["rate10y"] - prev["rate10y"]) * 100, 1)

    return {
        "date": cur["date"],
        "rate10y": cur["rate10y"],
        "rate2y": cur["rate2y"],
        "rate30y": cur["rate30y"],
        "prev_date": prev["date"] if prev else "",
        "prev_rate10y": prev["rate10y"] if prev else None,
        "delta_bp": delta_bp,
    }


# ---------- 2. 美联储动向 ----------

def get_fed_news(limit=3):
    """拉取美联储新闻稿 + 讲话，返回最近若干条标题。"""
    feeds = [
        ("新闻稿", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("讲话", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ]
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    for label, url in feeds:
        txt = fetch(url)
        if not txt:
            continue
        try:
            root = ET.fromstring(txt)
        except ET.ParseError:
            continue
        count = 0
        for it in root.iter():
            if it.tag.split("}")[-1] != "item":
                continue
            title = link = pub = ""
            for ch in it:
                ct = ch.tag.split("}")[-1]
                if ct == "title":
                    title = (ch.text or "").strip()
                elif ct == "link":
                    link = (ch.text or "").strip()
                elif ct == "pubDate":
                    pub = (ch.text or "").strip()
            if not title:
                continue
            out.append({"kind": label, "title": title[:100], "link": link, "pub": pub})
            count += 1
            if count >= limit:
                break
    return out[:6]


# ---------- 3. 恒生科技 ----------

def get_hstech():
    """腾讯行情接口取恒生科技指数最新点位与涨跌幅。"""
    txt = fetch("https://qt.gtimg.cn/q=hkHSTECH")
    if not txt:
        return None
    m = re.search(r'"(.*?)"', txt, re.S)
    if not m:
        return None
    parts = m.group(1).split("~")
    if len(parts) < 33:
        return None
    try:
        return {
            "name": "恒生科技指数",
            "price": float(parts[3]),
            "prev_close": float(parts[4]),
            "open": float(parts[5]),
            "change": float(parts[31]) if parts[31] else None,   # 涨跌额
            "pct": float(parts[32]) if parts[32] else None,       # 涨跌幅 %
            "high": float(parts[33]) if len(parts) > 33 and parts[33] else None,
            "low": float(parts[34]) if len(parts) > 34 and parts[34] else None,
            "time": parts[30] if len(parts) > 30 else "",
        }
    except (ValueError, IndexError) as e:
        print(f"[warn] hstech parse: {e}", file=sys.stderr)
        return None


# ---------- 4. 拼装晨报 ----------

# ---------- 4. 一句话解读 ----------

def interpret(treasury, hk):
    """基于利率变动方向与幅度，生成当日传导提示。
    逻辑：美债利率下行通常利好港股估值（分母端），反之为压制。
    """
    if not treasury or treasury.get("delta_bp") is None:
        return "美债数据未更新，暂不做解读。"

    d = treasury["delta_bp"]
    if abs(d) < 1:
        return "10年期美债利率基本持平，对港股估值的边际扰动很小。"

    if d < 0:
        if abs(d) >= 10:
            return (f"10年期美债利率大幅下行 {abs(d):.0f} 个基点，"
                    "对港股分母端构成明显利好，科技股估值压力缓解；"
                    "但需留意是否由避险情绪驱动——若是，则分子端可能反向承压。")
        return (f"10年期美债利率下行 {abs(d):.0f} 个基点，"
                "对恒生科技属温和利好，估值约束边际放松。")
    else:
        if d >= 10:
            return (f"10年期美债利率大幅上行 {d:.0f} 个基点，"
                    "对港股分母端构成明显压制，高估值科技股首当其冲。")
        return (f"10年期美债利率上行 {d:.0f} 个基点，"
                "对恒生科技形成小幅压制，关注是否延续。")


def build_report():
    now = datetime.now(CST)
    treasury = get_treasury()
    fed = get_fed_news()
    hk = get_hstech()

    lines = []
    lines.append(f"【美债 · 恒生科技晨报】{now.strftime('%Y-%m-%d %H:%M')} (北京时间)")
    lines.append("=" * 46)
    lines.append("")

    # 一、美债
    lines.append("一、美债")
    if treasury and treasury.get("rate10y") is not None:
        r10 = treasury["rate10y"]
        lines.append(f"  · 10年期国债收益率：{r10:.2f}%")
        if treasury.get("delta_bp") is not None:
            d = treasury["delta_bp"]
            arrow = "↓" if d <= 0 else "↑"
            word = "下行" if d < 0 else ("上行" if d > 0 else "持平")
            lines.append(
                f"    较前一交易日（{treasury['prev_date']} {treasury['prev_rate10y']:.2f}%）"
                f" {arrow} {word} {abs(d):.1f} 个基点"
            )
        extra = []
        if treasury.get("rate2y") is not None:
            extra.append(f"2年期 {treasury['rate2y']:.2f}%")
        if treasury.get("rate30y") is not None:
            extra.append(f"30年期 {treasury['rate30y']:.2f}%")
        if extra:
            lines.append("    其他期限：" + " / ".join(extra))
        if treasury.get("date"):
            lines.append(f"    数据日期：{treasury['date']}（美国财政部）")
    else:
        lines.append("  · 10年期收益率：暂未取到（源：美国财政部）")
    lines.append("")

    # 二、美联储动向
    lines.append("二、美联储动向（近 3 日）")
    if fed:
        for f in fed:
            lines.append(f"  · [{f['kind']}] {f['title']}")
    else:
        lines.append("  · 近 3 日无新公告/讲话")
    lines.append("")

    # 三、恒生科技
    lines.append("三、恒生科技指数")
    if hk:
        arrow = "▲" if (hk.get("pct") or 0) >= 0 else "▼"
        pct = hk.get("pct")
        lines.append(f"  · 最新点位：{hk['price']:.2f}")
        if pct is not None:
            lines.append(f"  · 涨跌幅：{arrow} {pct:+.2f}%")
        if hk.get("high") and hk.get("low"):
            lines.append(f"  · 日内区间：{hk['low']:.2f} ~ {hk['high']:.2f}")
        if hk.get("time"):
            lines.append(f"  · 行情时间：{hk['time']}")
    else:
        lines.append("  · 暂未取到行情")

    lines.append("")
    # 四、一句话解读
    lines.append("四、一句话解读")
    lines.append("  " + interpret(treasury, hk))
    lines.append("")

    lines.append("-" * 46)
    lines.append("数据来源：美国财政部 / 美联储官网 / 腾讯行情")
    lines.append("本邮件为自动生成的信息摘要，不构成投资建议。")

    return "\n".join(lines)


# ---------- 5. 发信 ----------

def send_mail(subject, body):
    if not MAIL_USER or not MAIL_AUTH_CODE:
        print("[error] 缺少 MAIL_USER 或 MAIL_AUTH_CODE", file=sys.stderr)
        return False
    to = MAIL_TO or MAIL_USER
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((str(Header("每日晨报", "utf-8")), MAIL_USER))
    msg["To"] = to
    msg["Subject"] = Header(subject, "utf-8")

    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
            s.login(MAIL_USER, MAIL_AUTH_CODE)
            s.sendmail(MAIL_USER, [to], msg.as_string())
        print(f"[ok] mail sent to {to}")
        return True
    except Exception as e:
        print(f"[error] send failed: {e}", file=sys.stderr)
        return False


def main():
    body = build_report()
    print("----- REPORT PREVIEW -----")
    print(body)
    print("--------------------------")
    now = datetime.now(CST)
    ok = send_mail(f"美债·恒科晨报 {now.strftime('%Y-%m-%d')}", body)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
