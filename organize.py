#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
4_临时 自动整理引擎
------------------------------------------------------------------
实时监控 → 读内容 → 改名 → 按 年/类型 归档 → 三层索引(台账/SQLite/目录)

设计要点(见 README.md):
  - 文件夹(年/类型)是笨存储;检索价值全在索引(标签 + 梗概)
  - 大脑 = llm_relay(claude-sonnet-4-6),只负责 主题/标签/梗概
  - 安全:dry-run 默认、台账可撤销、移动不覆盖、敏感件不读不外发、单飞锁
  - 日期:文件名 > 内容元数据 > min(创建,修改)+合理区间;创建日期永不单用

命令:
  python organize.py scan            # dry-run:出 _预览.md,不动文件
  python organize.py scan --apply    # 实跑:改名+归档+写索引(台账可 undo)
  python organize.py undo [--last N] # 撤销最近 N 条操作(默认全部本批)
  python organize.py search "关键词" [--客户 X] [--类型 Y]
  python organize.py folders         # 分析根目录文件夹,出裁决清单(不动)
  python organize.py reindex         # 从台账重建 SQLite + 目录.md
  python organize.py reconcile       # 索引↔磁盘对账:摘除悬空(台账留 tombstone)+ 孤儿出清单;不动任何文件
  python organize.py migrate         # 一次性回迁:已归档文档按体裁规则搬进 会议/口播/健康/财务/方案/知识(可 undo)
  python organize.py init            # 建 索引/ 系统目录 + 默认规则.json
"""
import argparse
import datetime as dt
import hashlib
import html.parser
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from urllib.parse import urlparse

# ----------------------------------------------------------------------------
# 默认配置(规则.json 存在则覆盖)
# ----------------------------------------------------------------------------
DEFAULTS = {
    "general": {
        "root": "~/Inbox",
        "system_dir": "索引",
        "stable_seconds": 4,            # 文件 mtime 需早于现在这么多秒才算"下完"。必须 < watcher 去抖(6s),否则扫描永远早于稳定→跳过卡死
        "max_filename_len": 180,
        "max_path_len": 250,
    },
    "date": {"min_year": 2000},
    "types": {
        "文档": ["pdf","doc","docx","txt","md","rtf","odt","html","htm","csv","tsv","epub","mobi","xls","xlsx","ppt","pptx"],
        "图片": ["jpg","jpeg","png","gif","webp","bmp","tiff","tif","heic","heif","svg","psd","raw","dng","cr2","nef","arw"],
        "视频": ["mp4","mkv","mov","avi","wmv","flv","webm","m4v","mpg","mpeg","ts"],
        "音频": ["mp3","wav","flac","m4a","aac","ogg","wma","opus","aiff"],
        "压缩包": ["zip","rar","7z","tar","gz","bz2","xz","tgz"],
        "程序": ["exe","msi","msix","appx","xapk","apk","dmg","pkg","deb","rpm","iso","img","bin","appimage"],
        "代码": ["py","js","ts","jsx","tsx","ps1","sh","bat","cmd","json","yaml","yml","toml","ini","cfg","conf","sql","java","c","cpp","h","go","rs","rb","php","vue","css","scss"],
    },
    # 文档体裁顶级夹:文档(含敏感件)按命名里的 _类型_ 标签路由到与「文档」平级的顶级夹。
    # 子串匹配,命中第一个即停;都不中→留「文档」兜底。证件/凭据/纯敏感不在任何关键词里,自然落兜底(不进可见体裁夹)。
    "doc_genre_folders": [
        # match="name" 的规则匹配整个新文件名,"label"(默认)只匹配 类型 标签。
        # 口播/会议匹配整名:口播稿的"口播稿"在主题里、敏感转写的"逐字稿/转写"在文件名里,类型标签看不到。
        # 口播排在会议前:口播稿的类型标签也是"逐字稿",先用主题里的"口播稿"判进口播,否则被会议的"逐字稿"吞走。
        # 其余(健康/财务/方案/知识)只匹配类型标签,避免主题词误伤(如书名含"方法论"被吞进知识)。
        {"folder": "口播", "match": "name", "keywords": ["口播稿"]},
        {"folder": "会议", "match": "name", "keywords": ["会议纪要", "会议记录", "纪要", "逐字稿", "转写"]},
        {"folder": "健康", "keywords": ["健康报告", "健康", "体检", "基因", "病历", "诊断", "医疗", "医学", "化验"]},
        {"folder": "财务", "keywords": ["财务", "票据", "发票", "收据", "报价单", "报价", "账单", "对账", "完税", "报税", "预算"]},
        {"folder": "方案", "keywords": ["方案", "策划", "企划", "大纲", "计划书", "提案"]},
        {"folder": "知识", "keywords": ["技术指南", "指南", "教程", "方法论", "教学", "攻略", "手册", "白皮书"]},
    ],
    "no_read": {
        "extensions": ["exe","msi","msix","appx","xapk","apk","dmg","pkg","iso","img","bin","zip","rar","7z","tar","gz","tgz","mp4","mkv","mov","avi","wmv","webm","m4v","mpg","mp3","wav","flac","m4a","aac","psd"],
        "max_read_mb": 50,
    },
    "ignore": {
        "patterns": ["*.tmp","*.crdownload","*.part","*.partial","*.download","*.opdownload","~$*","*.~*",".*","desktop.ini","Thumbs.db","*.lnk"],
    },
    "sensitive": {
        "keywords": ["基因","dna","健康报告","健康管理报告","医疗","病历","诊断","流水","对账","报税","完税","franchise tax","receipt","invoice","发票","身份证","护照","签证","银行卡","社保","公积金","薪资","工资","backup_code","backup code","恢复码","密钥","密码","password","secret","token","api_key","apikey","凭据","私钥","助记词"],
        "extensions": ["pem","key","p12","pfx","kdbx"],
        # 内容级敏感词:抽完正文扫这些,命中≥content_min 个就判敏感(防文件名无关键词但内容是隐私的漏网)
        "content_keywords": ["账户","余额","交易","流水","对账","开户行","转账","扣款","银行卡","卡号","身份证号","开户","escrow","托管账户","收支明细","还款","本金","抵押贷款","按揭","对公账户","银行流水","基因","序列","变异","snp","病历","诊断","医保","处方","报税","完税","纳税","薪资","工资条","公积金","社保","backup_code","恢复码","私钥","助记词","seed phrase",
                             "check number","account number","routing number","wire transfer","iban","swift code","mortgage","overdraft","balance","transaction history","statement period","principal balance","bank statement"],
        "content_min": 2,
        # 敏感件怎么读(2026-08-14)。默认 none = 老行为:一个字节都不读、不外发。
        #   none  永不读,只按文件名归档 + 索引打码
        #   local 用**本机 Claude 订阅**读(claude -p):正文只到 Anthropic,不经任何中转、不用别人的 key
        #   brain 跟普通件一样走 brain.backend(⚠ 若 backend=llm_relay,正文会经过那台中转)
        # 🔴 "跑在自己机器上、用自己的订阅"这句话只有在 local 下才成立 —— 默认的 llm_relay
        #    是第三方中转,把敏感件交给它等于把闸门拆了还搬到了别人家。
        "read_with": "none",
        "local_model": "sonnet",        # local 模式下交给 claude -p 的模型
    },
    "naming": {"date_format": "%Y-%m-%d"},
    "brain": {
        "backend": "llm_relay",         # llm_relay=OpenAI 兼容 HTTP(推荐,任意官方/中转) | claude_cli=走本机 Claude CLI 订阅(慢~50s/文件)
        "model": "gpt-4o-mini",         # 任意 OpenAI 兼容多模态模型;要用 Claude/别家只需改 credentials 的 base_url + 这里的 model
        "credential_group": "llm_relay",
        # 🔴🔴 local_only(2026-08-14):正文一个字节都不出这台机器。打开后
        #   · 大脑必须在本机,否则启动即报错 —— 这条不能靠"记得配"。两种合法配置:
        #       ①backend=claude_cli(本机 Claude 订阅)
        #       ②backend=llm_relay + API_BASE_URL 指向本机(127.0.0.1/localhost/::1,
        #         即 llama.cpp / ollama / vLLM 这类本地 OpenAI 兼容端点;按生效凭据判,不认配置字样)
        #   · 音频/视频**一律不读**:那条路只有 Gemini(第三方),本机没有对应实现。
        #     少一个能力,好过嘴上说不出机、实际把录音发出去。
        "local_only": False,
        "bypass_proxy": True,
        "timeout": 180,
        "batch_size": 8,
        "max_text_chars": 6000,
        "max_concurrent": 3,            # claude -p 每个进程重,并发别太高;中转可调到 5+
        "gemini": {                     # 音频/视频走 Gemini(Google 国内必须代理)
            "model": "gemini-2.5-flash",
            "use_proxy": False,             # 若 Google 在你所在地区需代理,置 True 并填下面 proxy
            "proxy": "http://127.0.0.1:7890",
            "audio_segment_sec": 120,   # 音频>15MB 时截前 N 秒
            "video_clip_sec": 45,       # 视频截前 N 秒
            "video_scale": 360,         # 视频片段高度(360p,给 Gemini 够用、体积小)
        },
    },
    "vocab": {
        # 别名归一表:把同一实体的多种叫法归到一个标准名,防标签裂变。格式 "标准名": ["别名1","别名2"]。
        # 按你自己的客户/项目/人名填,例:"项目": {"Acme": ["acme","acme inc"]}
        "客户": {},
        "项目": {},
        "人名": {},
    },
    "folders": {"auto": False, "zip_software": True, "stable_seconds": 30, "user_content_max_ratio": 0.2},
}

# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def strip_jsonc(text):
    # 仅删除整行 // 注释(行首空白后是 //),不碰 https:// 这类行内值
    return re.sub(r"(?m)^\s*//.*$", "", text)


def is_loopback(base_url):
    """base_url 是否指向本机(127.0.0.1 / localhost / ::1)。

    local_only 靠它判断"正文到底会不会出机":跑本机 llama.cpp / ollama / vLLM 的 llm_relay
    同样一个字节都不外发,不该被 "必须 claude_cli" 那条规则一道挡在门外。"""
    try:
        host = (urlparse(base_url or "").hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return host in ("localhost", "::1") or host.startswith("127.")


def load_config(root_hint=None):
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    root = root_hint or cfg["general"]["root"]
    cfgpath = Path(root) / cfg["general"]["system_dir"] / "规则.json"
    if cfgpath.exists():
        try:
            user = json.loads(strip_jsonc(cfgpath.read_text(encoding="utf-8")))
            cfg = deep_merge(cfg, user)
        except Exception as e:
            log(f"⚠ 规则.json 解析失败,用默认配置: {e}")
    # 🔴 local_only 的承诺必须在这儿兑现,而不是靠配置的人记得同时把 backend 也改了。
    #    配错就当场停,不给"以为不出机、其实全发中转"这种状态存在的机会。
    #    两条合法路径:①本机 Claude 订阅(claude_cli) ②llm_relay 的 base_url 指向本机
    #    —— 后者是 llama.cpp / ollama 这类"模型在本机、仍走 OpenAI 协议"的用法(常见)。
    if cfg["brain"].get("local_only"):
        backend = cfg["brain"].get("backend")
        if backend != "claude_cli":
            # 按实际生效的凭据判断(环境变量优先,再看 settings.local.json),不只看配置里的字样
            _, base_url = read_credential(cfg["brain"].get("credential_group") or "llm_relay")
            if not is_loopback(base_url):
                raise SystemExit(
                    "规则.json 配置冲突: brain.local_only=true 要求大脑跑在本机,二选一 ——\n"
                    "  · brain.backend=\"claude_cli\"(本机 Claude 订阅)\n"
                    "  · brain.backend=\"llm_relay\" 且 API_BASE_URL 指向本机(127.0.0.1 / localhost / ::1)\n"
                    f"当前 backend=\"{backend}\",生效的 base_url=\"{base_url}\"。"
                    "正文不出机这件事不能只写一半。")
    return cfg


def _load_settings():
    """凭据来源(可选文件,缺了也不报错):环境变量 GUIWEI_SETTINGS 指定的路径 →
    脚本同目录 settings.local.json → ~/.claude/settings.local.json(Claude Code 用户)。"""
    cands = []
    envp = os.environ.get("GUIWEI_SETTINGS")
    if envp:
        cands.append(Path(envp))
    cands.append(Path(__file__).resolve().parent / "settings.local.json")
    cands.append(Path.home() / ".claude" / "settings.local.json")
    for p in cands:
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def read_credential(group):
    """文档/图片大脑(OpenAI 兼容)的 key + base_url。环境变量优先,再看 settings 文件的
    credentials.<group>,最后退到 OpenAI 官方端点。"""
    cr = _load_settings().get("credentials", {}).get(group, {})
    key = (os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
           or cr.get("API_KEY") or cr.get("key") or cr.get("api_key"))
    base = (os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
            or cr.get("API_BASE_URL") or cr.get("base_url") or "https://api.openai.com/v1")
    return key, base.rstrip("/")


def sha256_of(path, limit_mb=None):
    h = hashlib.sha256()
    cap = (limit_mb * 1024 * 1024) if limit_mb else None
    read = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            read += len(b)
            if cap and read >= cap:
                break
    return h.hexdigest()


def system_dir(cfg):
    return Path(cfg["general"]["root"]) / cfg["general"]["system_dir"]


# ----------------------------------------------------------------------------
# 忽略 / 稳定性 / 敏感
# ----------------------------------------------------------------------------
def fnmatch_any(name, patterns):
    import fnmatch
    low = name.lower()
    return any(fnmatch.fnmatch(low, p.lower()) for p in patterns)


def is_stable(path, stable_seconds):
    try:
        st1 = path.stat()
        if (time.time() - st1.st_mtime) < stable_seconds:
            return False
        # 二次采样:大小不再变
        time.sleep(0.4)
        st2 = path.stat()
        if st1.st_size != st2.st_size:
            return False
        # 独占打开检测(被占用则未就绪)
        with open(path, "rb"):
            pass
        return True
    except (PermissionError, OSError):
        return False


def is_sensitive(path, cfg):
    ext = path.suffix.lower().lstrip(".")
    if ext in cfg["sensitive"]["extensions"]:
        return True
    hay = str(path).lower()
    return any(kw.lower() in hay for kw in cfg["sensitive"]["keywords"])


def sensitive_label(name):
    """敏感文件不读内容,只按文件名给个粗类型,避免难看的"敏感文件"。"""
    n = name.lower()
    table = [(("发票", "receipt", "invoice"), "票据"),
             (("基因", "健康", "医疗", "病历", "诊断", "dna"), "健康报告"),
             (("流水", "对账", "报税", "完税", "franchise tax", "薪资", "工资", "社保", "公积金"), "财务"),
             (("身份证", "护照", "签证", "银行卡"), "证件"),
             (("backup_code", "backup code", "恢复码", "密钥", "密码", "password", "secret",
               "token", "凭据", "私钥", "助记词", "api_key", "apikey"), "凭据")]
    for kws, label in table:
        if any(k in n for k in kws):
            return label
    return "敏感"


def content_is_sensitive(text, cfg):
    """内容级敏感检测:抽完正文后扫,命中≥content_min 个敏感词即判敏感(防文件名无关键词的漏网)。"""
    if not text:
        return False
    low = text[:4000].lower()
    kws = cfg["sensitive"].get("content_keywords", [])
    hits = sum(1 for k in kws if k.lower() in low)
    return hits >= cfg["sensitive"].get("content_min", 2)


# ----------------------------------------------------------------------------
# 日期解析:文件名 > 内容元数据 > min(创建,修改) + 合理区间;创建日期永不单用
# ----------------------------------------------------------------------------
def _valid_date(y, m, d, min_year, max_year):
    try:
        if not (min_year <= y <= max_year):
            return None
        return dt.date(y, m, d)
    except ValueError:
        return None


def date_from_filename(name, min_year, max_year):
    # 20260604 / 2026-06-04 / 2026.06.04 / 2026_06_04 / 微信14位时间戳前8位
    for m in re.finditer(r"(20\d{2})[-._]?(0[1-9]|1[0-2])[-._]?(0[1-9]|[12]\d|3[01])", name):
        d = _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), min_year, max_year)
        if d:
            return d
    return None


def date_from_metadata(path, ext):
    try:
        if ext == "pdf":
            import fitz
            doc = fitz.open(path)
            raw = (doc.metadata or {}).get("creationDate", "") or (doc.metadata or {}).get("modDate", "")
            doc.close()
            m = re.search(r"D:(\d{4})(\d{2})(\d{2})", raw or "")
            if m:
                return _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), 2000, 2100)
        elif ext in ("jpg", "jpeg", "tiff", "tif", "heic", "heif", "png", "webp"):
            from PIL import Image
            img = Image.open(path)
            exif = getattr(img, "getexif", lambda: None)()
            if exif:
                for tag in (36867, 306):  # DateTimeOriginal, DateTime
                    v = exif.get(tag)
                    if v:
                        m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})", str(v))
                        if m:
                            return _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), 2000, 2100)
        elif ext in ("docx", "pptx", "xlsx"):
            with zipfile.ZipFile(path) as z:
                if "docProps/core.xml" in z.namelist():
                    xml = z.read("docProps/core.xml").decode("utf-8", "ignore")
                    m = re.search(r"<dcterms:created[^>]*>(\d{4})-(\d{2})-(\d{2})", xml)
                    if m:
                        return _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), 2000, 2100)
    except Exception:
        pass
    return None


def resolve_date(path, ext, cfg):
    min_year = cfg["date"]["min_year"]
    today = dt.date.today()
    max_year = today.year + 1
    d = date_from_filename(path.name, min_year, max_year)
    if d:
        return d, "filename"
    d = date_from_metadata(path, ext)
    if d:
        return d, "metadata"
    st = path.stat()
    created = getattr(st, "st_birthtime", st.st_ctime)
    cands = []
    for tsv in (created, st.st_mtime):
        try:
            dd = dt.date.fromtimestamp(tsv)
            if min_year <= dd.year <= max_year:
                cands.append(dd)
        except (OSError, ValueError, OverflowError):
            pass
    if cands:
        return min(cands), "fs_min"
    return today, "fallback"


# ----------------------------------------------------------------------------
# 正文抽取(stdlib + fitz/PIL/openpyxl,零新依赖)
# ----------------------------------------------------------------------------
class _HTMLText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            t = data.strip()
            if t:
                self.parts.append(t)


def _strip_xml(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def extract_text(path, ext, cfg):
    """返回正文片段(已截断);图片返回 None 表示走视觉;不可读返回 ''。"""
    limit = cfg["brain"]["max_text_chars"]
    img_exts = {"jpg","jpeg","png","gif","webp","bmp","tiff","tif"}
    if ext in img_exts:
        return None  # 视觉路径
    try:
        if ext in ("txt","md","csv","tsv","log","json","yaml","yml","toml","ini","cfg","conf","srt","vtt","py","js","ts","html","htm"):
            raw = path.read_text(encoding="utf-8", errors="ignore")
            if ext in ("html", "htm"):
                p = _HTMLText()
                p.feed(raw)
                raw = "  ".join(p.parts)
            return raw[:limit]
        if ext == "pdf":
            import fitz
            doc = fitz.open(path)
            txt = []
            for i, page in enumerate(doc):
                if i >= 8:
                    break
                txt.append(page.get_text())
                if sum(len(t) for t in txt) > limit:
                    break
            doc.close()
            return "".join(txt)[:limit]
        if ext == "docx":
            with zipfile.ZipFile(path) as z:
                return _strip_xml(z.read("word/document.xml").decode("utf-8", "ignore"))[:limit]
        if ext == "pptx":
            with zipfile.ZipFile(path) as z:
                slides = [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)]
                buf = []
                for n in sorted(slides)[:20]:
                    buf.append(_strip_xml(z.read(n).decode("utf-8", "ignore")))
                return "  ".join(buf)[:limit]
        if ext in ("xls", "xlsx"):
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            buf = []
            for ws in wb.worksheets[:2]:
                for r, row in enumerate(ws.iter_rows(values_only=True)):
                    if r >= 40:
                        break
                    buf.append(" ".join("" if c is None else str(c) for c in row))
                    if sum(len(x) for x in buf) > limit:
                        break
            wb.close()
            return "\n".join(buf)[:limit]
    except Exception as e:
        log(f"  · 抽取失败 {path.name}: {e}")
    return ""


# ----------------------------------------------------------------------------
# 大脑(llm_relay,OpenAI 兼容)
# ----------------------------------------------------------------------------
def _opener(bypass_proxy):
    if bypass_proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _chat(messages, cfg, max_tokens=1500, force_json=False):
    key, base = read_credential(cfg["brain"]["credential_group"])
    if not key:
        raise RuntimeError("未找到 llm_relay API_KEY")
    op = _opener(cfg["brain"]["bypass_proxy"])
    payload = {"model": cfg["brain"]["model"], "max_tokens": max_tokens, "messages": messages}
    if force_json:
        payload["response_format"] = {"type": "json_object"}
    last = None
    for attempt in range(3):
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(base + "/chat/completions", data=body,
                                         headers={"Authorization": "Bearer " + key,
                                                  "Content-Type": "application/json"})
            r = op.open(req, timeout=cfg["brain"]["timeout"])
            data = json.loads(r.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            msg = e.read()[:300]
            last = f"HTTP {e.code}: {msg!r}"
            if e.code == 400 and b"response_format" in msg and "response_format" in payload:
                payload.pop("response_format")  # 中转不支持就去掉重试
                continue
            if e.code < 500:
                break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"大脑调用失败: {last}")


def _escape_inner_quotes(text):
    """转义字符串值内未转义的英文双引号(LLM 常见畸形:summary 里塞了 "xxx" 没转义→JSON 提前断开)。
    启发式:串内引号若后面紧跟 , : } ](忽略空白)视为结构性闭合,否则视为内容引号转义之。
    中文内容用全角标点、JSON 结构用 ASCII,故内容引号后多是全角字符,判别可靠。"""
    out, in_str, i, n = [], False, 0, len(text)
    while i < n:
        c = text[i]
        if not in_str:
            out.append(c)
            if c == '"':
                in_str = True
        elif c == '\\' and i + 1 < n:
            out.append(c); out.append(text[i + 1]); i += 2; continue
        elif c == '"':
            j = i + 1
            while j < n and text[j] in ' \t\r\n':
                j += 1
            if j >= n or text[j] in ',:}]':
                out.append(c); in_str = False           # 结构性闭合
            else:
                out.append('\\"')                        # 内容引号,转义
        else:
            out.append(c)
        i += 1
    return ''.join(out)


def _parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    # 截取第一个 { 到最后一个 }
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        text = text[a:b + 1]
    nocomma = re.sub(r",(\s*[}\]])", r"\1", text)         # 去尾逗号
    for cand in (text, nocomma, _escape_inner_quotes(text), _escape_inner_quotes(nocomma)):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return json.loads(_escape_inner_quotes(nocomma))     # 抛最后一次的异常


def _claude_bin():
    if not hasattr(_claude_bin, "_p"):
        import shutil
        _claude_bin._p = shutil.which("claude") or "claude"
    return _claude_bin._p


def _claude_cli(system, user, cfg, image_path=None):
    """走 Claude 订阅(claude -p):--system-prompt 覆盖 + 排除动态段省上下文;prompt 走 stdin。"""
    import subprocess, tempfile
    model = cfg["brain"].get("model", "claude-sonnet-4-6")
    args = [_claude_bin(), "-p", "--model", model, "--system-prompt", system,
            "--exclude-dynamic-system-prompt-sections", "--output-format", "json", "--strict-mcp-config"]
    if image_path:
        args += ["--allowedTools", "Read", "--permission-mode", "bypassPermissions",
                 "--add-dir", str(Path(image_path).parent), "--max-turns", "4"]
    else:
        args += ["--max-turns", "1"]
    last = None
    for attempt in range(2):
        try:
            proc = subprocess.run(args, input=user, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=cfg["brain"]["timeout"], cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired:
            last = "超时"; continue
        if proc.returncode != 0:
            last = f"exit {proc.returncode}: {(proc.stderr or '')[:150]}"; time.sleep(1); continue
        try:
            env = json.loads(proc.stdout)
        except Exception:
            last = f"envelope非JSON: {(proc.stdout or '')[:120]}"; continue
        if env.get("is_error"):
            last = f"claude错误: {str(env.get('result', ''))[:150]}"; continue
        return env.get("result", "")
    raise RuntimeError(f"claude -p 失败: {last}")


def _brain_text(system, user, cfg):
    """文本大脑调用,按 backend 分发,返回模型输出文本。"""
    if cfg["brain"].get("backend") == "claude_cli":
        return _claude_cli(system, user, cfg)
    return _chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                 cfg, max_tokens=3000, force_json=True)


SYS_PROMPT = """你是文件归档助手。给你一批文件的正文片段,为每个文件产出:命名要素 + 标签 + 一句话梗概。
规则:
- semantic_type 语义类型:如 方案/报价单/合同/会议纪要/逐字稿/技术指南/论文/海报/截图/发票/报告/教程/简历 等,用最贴切的中文短词。
- subject 主体:该文件最能标识的实体,优先级 客户 > 项目 > 人名。没有就留空。
- topic 内容要点:6-16字中文,概括这份文件讲什么,不要重复 semantic_type。
- tags:{"客户":"","人名":"","项目":"","类型":""};类型同 semantic_type;能识别到客户/项目/人名就填,没有留空。
- summary 梗概:不超过120字中文,信息密度高,便于检索(写清是什么、关于谁/什么项目、关键内容)。
- 已知实体(尽量归一到这些标准名,不要造别名):{vocab}
只输出 JSON,格式 {"items":[{"i":0,"semantic_type":"","subject":"","topic":"","tags":{"客户":"","人名":"","项目":"","类型":""},"summary":""}]}"""


def brain_text_batch(items, cfg):
    """items: [{i, name, type_bucket, date, excerpt}] -> {i: result}"""
    vocab_known = {k: list(v.keys()) for k, v in cfg.get("vocab", {}).items()}
    sysp = SYS_PROMPT.replace("{vocab}", json.dumps(vocab_known, ensure_ascii=False))
    payload = [{"i": it["i"], "文件名": it["name"], "物理类型": it["type_bucket"],
                "日期": it["date"], "正文片段": (it["excerpt"] or "")[:cfg["brain"]["max_text_chars"]]}
               for it in items]
    user = "为这些文件产出 JSON:\n" + json.dumps(payload, ensure_ascii=False)
    last = None
    for attempt in range(2):
        u = user if attempt == 0 else user + "\n\n上次输出不是合法 JSON。务必只输出严格合法 JSON:字符串内引号转义,无尾逗号、无注释、无多余文字。"
        content = _brain_text(sysp, u, cfg)
        try:
            out = _parse_json(content)
            return {int(o["i"]): o for o in out.get("items", [])}
        except Exception as e:
            last = e
    raise RuntimeError(f"JSON解析失败: {last}")


def _sniff_image_mime(raw):
    """按文件魔数判断真实图片类型(扩展名可能谎报,如 webp 命名为 .jpg)。"""
    if raw[:8].startswith(b"\x89PNG"):
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def brain_image(path, name, date, cfg):
    vocab_known = {k: list(v.keys()) for k, v in cfg.get("vocab", {}).items()}
    sysp = SYS_PROMPT.replace("{vocab}", json.dumps(vocab_known, ensure_ascii=False))
    # claude_cli:让 claude 用 Read 工具看图(走订阅,无 base64/10MB 限制)
    if cfg["brain"].get("backend") == "claude_cli":
        prompt = (f"用 Read 工具读取这张图片,据其视觉内容输出 JSON。文件名={name} 日期={date}。\n"
                  f"图片绝对路径:{path}\n"
                  '只输出 JSON(放进 items 数组,i=0):{"items":[{"i":0,"semantic_type":"","subject":"","topic":"","tags":{"客户":"","人名":"","项目":"","类型":""},"summary":""}]}')
        for attempt in range(2):
            try:
                content = _claude_cli(sysp, prompt, cfg, image_path=str(path))
                out = _parse_json(content)
                items = out.get("items", [out]) if isinstance(out, dict) else out
                return items[0] if items else None
            except Exception:
                prompt += "\n上次不是合法 JSON,只输出严格合法 JSON。"
        return None
    # llm_relay:base64 直传视觉。统一过 PIL 转 JPEG(同时修正"webp 谎报 jpeg"与 base64>10MB 两类失败)
    import base64, io
    raw = path.read_bytes()
    mime = "image/jpeg"
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((1568, 1568))                     # 长边 >1568 才缩,否则不动
        q = 85
        for _ in range(5):                             # 循环压到 base64 稳在 10MB 下
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=q)
            raw = buf.getvalue()
            if len(raw) <= 6 * 1024 * 1024:
                break
            w, h = im.size
            im = im.resize((max(1, int(w * 0.8)), max(1, int(h * 0.8))))
            q = max(60, q - 5)
    except Exception:
        mime = _sniff_image_mime(raw) or "image/jpeg"  # PIL 不可用:按魔数给真实 mime(扩展名会谎报)
    if len(raw) > 9 * 1024 * 1024:
        return None
    b64 = base64.b64encode(raw).decode()
    user = [{"type": "text", "text": f"文件名={name} 日期={date}。看图后输出单个文件的 JSON(放进 items 数组,i=0)。"},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
    msgs = [{"role": "system", "content": sysp}, {"role": "user", "content": user}]
    for attempt in range(2):
        content = _chat(msgs, cfg, max_tokens=800, force_json=True)
        try:
            out = _parse_json(content)
            items = out.get("items", [out]) if isinstance(out, dict) else out
            return items[0] if items else None
        except Exception:
            msgs = msgs[:2] + [{"role": "assistant", "content": content[:400]},
                               {"role": "user", "content": "上次不是合法 JSON。只输出严格合法 JSON,字符串内引号用 \\\" 转义,无尾逗号。"}]
    return None


# ----------------------------------------------------------------------------
# 音频 / 视频:Gemini 2.5 flash(原生多模态,走代理)
# ----------------------------------------------------------------------------
def read_credential_raw(group, field):
    if field == "GEMINI_API_KEY" and os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    return _load_settings().get("credentials", {}).get(group, {}).get(field)


GEMINI_MEDIA_PROMPT = ('看/听这个媒体文件,只输出一个 JSON(不要 markdown 代码块、不要多余文字):\n'
                       '{"semantic_type":"语义类型(配音/科普/播客/录屏/演示/广告/vlog/教程/素材/会议录音 等)",'
                       '"subject":"最能标识的主体:客户>项目>人名,没有留空",'
                       '"topic":"6-16字内容要点",'
                       '"tags":{"客户":"","人名":"","项目":"","类型":"同 semantic_type"},'
                       '"summary":"≤120字中文梗概:是什么、关于谁/什么项目、关键内容"}\n'
                       '参考文件名={name} 日期={date}')


def _gemini(parts, cfg):
    gc = cfg["brain"].get("gemini", {})
    key = read_credential_raw("gemini", "GEMINI_API_KEY")
    if not key:
        raise RuntimeError("未找到 gemini.GEMINI_API_KEY")
    model = gc.get("model", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    px = gc.get("proxy", "http://127.0.0.1:7890")
    handler = urllib.request.ProxyHandler({"https": px, "http": px} if gc.get("use_proxy", True) else {})
    opener = urllib.request.build_opener(handler)
    body = json.dumps({"contents": [{"parts": parts}]}).encode("utf-8")
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            d = json.loads(opener.open(req, timeout=cfg["brain"]["timeout"]).read().decode("utf-8"))
            return d["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:150]}"
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Gemini 失败: {last}")


def _gemini_media(parts_media, name, date, cfg):
    prompt = GEMINI_MEDIA_PROMPT.replace("{name}", name).replace("{date}", date)
    for attempt in range(2):
        content = _gemini([{"text": prompt}] + parts_media, cfg)
        try:
            return _parse_json(content)
        except Exception:
            prompt += "\n务必只输出严格合法 JSON,无 markdown、无多余文字。"
    return None


def brain_audio(path, name, date, cfg):
    import base64, subprocess, shutil, tempfile
    gc = cfg["brain"].get("gemini", {})
    src, tmp = path, None
    if path.stat().st_size > 15 * 1024 * 1024:  # 大音频:截前段 + 转单声道低码率压体积
        ff = shutil.which("ffmpeg")
        if ff:
            tmp = Path(tempfile.gettempdir()) / f"_aud_{abs(hash(str(path))) % 10**8}.mp3"
            subprocess.run([ff, "-y", "-i", str(path), "-t", str(gc.get("audio_segment_sec", 120)),
                            "-ac", "1", "-ar", "16000", "-b:a", "48k", str(tmp)],
                           capture_output=True, timeout=120)
            if tmp.exists():
                src = tmp
    try:
        if src.stat().st_size > 18 * 1024 * 1024:
            return None
        ext = src.suffix.lower().lstrip(".")
        mime = {"mp3": "audio/mp3", "m4a": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav",
                "ogg": "audio/ogg", "flac": "audio/flac", "opus": "audio/opus"}.get(ext, "audio/mpeg")
        b64 = base64.b64encode(src.read_bytes()).decode()
        return _gemini_media([{"inline_data": {"mime_type": mime, "data": b64}}], name, date, cfg)
    finally:
        if tmp and tmp.exists():
            try: tmp.unlink()
            except Exception: pass


def brain_video(path, name, date, cfg):
    import base64, subprocess, shutil, tempfile
    gc = cfg["brain"].get("gemini", {})
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    clip = Path(tempfile.gettempdir()) / f"_vid_{abs(hash(str(path))) % 10**8}.mp4"
    try:
        subprocess.run([ff, "-y", "-i", str(path), "-t", str(gc.get("video_clip_sec", 45)),
                        "-vf", f"scale=-2:{gc.get('video_scale', 360)}", "-c:v", "libx264", "-crf", "30",
                        "-c:a", "aac", "-b:a", "64k", str(clip)], capture_output=True, timeout=180)
        if not clip.exists() or clip.stat().st_size > 18 * 1024 * 1024:
            return None
        b64 = base64.b64encode(clip.read_bytes()).decode()
        return _gemini_media([{"inline_data": {"mime_type": "video/mp4", "data": b64}}], name, date, cfg)
    finally:
        if clip.exists():
            try: clip.unlink()
            except Exception: pass


# ----------------------------------------------------------------------------
# 词表归一 + 命名 + 清洗
# ----------------------------------------------------------------------------
def normalize_tag(value, facet, cfg):
    if not value:
        return ""
    table = cfg.get("vocab", {}).get(facet, {})
    low = value.strip().lower()
    for canon, aliases in table.items():
        if low == canon.lower() or low in [a.lower() for a in aliases]:
            return canon
    return value.strip()


WIN_RESERVED = {"con","prn","aux","nul"} | {f"com{i}" for i in range(1,10)} | {f"lpt{i}" for i in range(1,10)}


def sanitize(s):
    s = re.sub(r'[\\/:*?"<>|]', " ", s or "")
    s = re.sub(r"[\x00-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s


def strip_dates(s):
    """剥离正文里的日期/时间戳,避免和文件名前缀日期重复(如 路演20260324 → 路演)。"""
    if not s:
        return s
    s = re.sub(r"\d{4}\s*[-./年]\s*\d{1,2}\s*[-./月]\s*\d{1,2}\s*日?", " ", s)  # 2025-11-23 / 2025.11.23 / 2025年11月23日
    s = re.sub(r"(?<!\d)\d{8,}(?!\d)", " ", s)                                    # 20260324 / 20260614163226 等 8+ 连续数字
    s = re.sub(r"\d{4}\s*[-./年]\s*\d{1,2}\s*月?", " ", s)                        # 残留的 2025-11 / 2025年11月
    s = re.sub(r"[ \-_.·]{2,}", " ", s)
    return s.strip(" -_.·")


def build_name(date_str, semantic_type, subject, topic, ext, cfg):
    parts = [date_str]
    for x in (semantic_type, subject, topic):
        x = sanitize(strip_dates(x))   # 先剥日期再清洗,避免主体/主题里的日期与前缀日期重复
        if x:
            parts.append(x)
    stem = "_".join(parts)
    if stem.split("_")[0].lower() in WIN_RESERVED:
        stem = "_" + stem
    maxlen = cfg["general"]["max_filename_len"] - len(ext) - 1
    if len(stem) > maxlen:
        stem = stem[:maxlen - 1] + "…"
    return f"{stem}.{ext}" if ext else stem


def unique_dest(folder, filename):
    dest = folder / filename
    if not dest.exists():
        return dest
    stem = dest.stem
    suf = dest.suffix
    for i in range(2, 1000):
        cand = folder / f"{stem} ({i}){suf}"
        if not cand.exists():
            return cand
    return folder / f"{stem} ({int(time.time())}){suf}"


def type_bucket(ext, cfg):
    for bucket, exts in cfg["types"].items():
        if ext in exts:
            return bucket
    return "其他"


def route_category(base_category, label, full_name, cfg):
    """文档路由到体裁顶级夹。每条规则按 match 决定匹配源:
    "label"(默认)只匹配 类型 标签——健康/财务/方案/知识用,避免主题词(如书名里的「方法论」)误伤;
    "name" 匹配整个新文件名——口播/会议用,因为口播稿的"口播稿"在主题里、敏感转写的"逐字稿/转写"在文件名里,标签看不到。
    非文档→原样;敏感件命中体裁照样进对应夹(索引仍打码),证件/凭据/纯敏感不命中→留「文档」。"""
    if base_category != "文档":
        return base_category                 # 媒体/程序/代码 按模态,不动
    label = label or ""
    name = full_name or ""
    for rule in cfg.get("doc_genre_folders", []):
        hay = name if rule.get("match") == "name" else label
        for kw in rule.get("keywords", []):
            if kw and kw in hay:
                return rule["folder"]
    return "文档"


def dest_folder_names(cfg):
    """所有归档目的地夹名(模态 + 体裁),analyze_folders 用来跳过、不当待裁决项。"""
    names = set(cfg.get("types", {}).keys()) | {"其他", "文档"}
    for rule in cfg.get("doc_genre_folders", []):
        if rule.get("folder"):
            names.add(rule["folder"])
    return names


# ----------------------------------------------------------------------------
# 台账(jsonl,权威源 + 撤销日志) / SQLite(检索) / 目录.md(人读)
# ----------------------------------------------------------------------------
def ledger_path(cfg):
    return system_dir(cfg) / "台账.jsonl"


def load_seen_hashes(cfg):
    # 只认"仍存活"的归档(末态非 gone);被 reconcile 摘除的 sha 不再算重复,同内容重新落盘会再整理
    return {r["sha256"]: r.get("new_path") for r in live_records(cfg) if r.get("sha256")}


def ledger_append(cfg, rec):
    p = ledger_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def live_records(cfg):
    """台账去重后仍"存活"的归档记录:按 sha256(退化到 new_path)取最后一条,末态为 gone(reconcile 摘除)的排除。
    move/folder_zip/rename → 存活;gone tombstone → 该 sha 作废。台账 append-only,这里只做投影,索引/目录/判重都以此为准。"""
    p = ledger_path(cfg)
    by_key = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("action") not in ("move", "rename", "folder_zip", "gone"):
                continue
            by_key[r.get("sha256") or r.get("new_path")] = r   # 后写胜:最后一条决定末态
    return [r for r in by_key.values() if r.get("action") != "gone"]


def db_path(cfg):
    return system_dir(cfg) / "索引.db"


def db_connect(cfg):
    con = sqlite3.connect(str(db_path(cfg)))
    # 普通表 + LIKE 子串检索 —— CJK 正确(FTS5 默认分词器把整串中文当一个 token,搜不动)
    con.execute("""CREATE TABLE IF NOT EXISTS idx (
        sha256 TEXT PRIMARY KEY, new_name TEXT, kehu TEXT, renming TEXT, xiangmu TEXT, leixing TEXT,
        summary TEXT, orig_name TEXT, orig_path TEXT, new_path TEXT, category TEXT, date TEXT,
        size INTEGER, ext TEXT, sensitive TEXT, ts TEXT)""")
    return con


def db_upsert(con, rec):
    t = rec.get("tags", {})
    con.execute("INSERT OR REPLACE INTO idx (sha256,new_name,kehu,renming,xiangmu,leixing,summary,orig_name,orig_path,new_path,category,date,size,ext,sensitive,ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["sha256"], rec["new_name"], t.get("客户",""), t.get("人名",""), t.get("项目",""), t.get("类型",""),
                 rec.get("summary",""), rec["orig_name"], rec["orig_path"], rec["new_path"],
                 rec["category"], rec["date"], rec.get("size",0), rec.get("ext",""),
                 "1" if rec.get("sensitive") else "0", rec["ts"]))
    con.commit()


def render_catalog(cfg):
    """从台账重建 目录.md(只含存活记录,reconcile 摘除的 gone 已排除)"""
    rows = live_records(cfg)
    rows.sort(key=lambda r: (r.get("category",""), r.get("new_name","")))
    out = ["# 目录索引 Catalog", "",
           f"_自动生成 · {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · 共 {len(rows)} 条 · 勿手改_", ""]
    cur = None
    for r in rows:
        cat = r.get("category", "?")
        if cat != cur:
            out += ["", f"## {cat}", "", "| 文件 | 类型 | 客户/项目/人名 | 日期 | 梗概 |", "|---|---|---|---|---|"]
            cur = cat
        t = r.get("tags", {})
        who = " / ".join(x for x in (t.get("客户"), t.get("项目"), t.get("人名")) if x) or "—"
        # 判据同命名那处:看这一件有没有真被读过(打码件的 summary 本身就是那句说明),
        # 不再一刀切按 sensitive 遮 —— 本机订阅读过的敏感件，梗概只存在这台机器上。
        summ = (r.get("summary", "") or "")[:60]
        name = (r.get("new_name","") or "").replace("|", "\\|")
        out.append(f"| {name} | {t.get('类型','—')} | {who} | {r.get('date','')} | {summ} |")
    (system_dir(cfg) / "目录.md").write_text("\n".join(out) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------
# 单飞锁
# ----------------------------------------------------------------------------
def acquire_lock(cfg):
    lk = system_dir(cfg) / ".lock"
    lk.parent.mkdir(parents=True, exist_ok=True)
    if lk.exists():
        try:
            old = json.loads(lk.read_text(encoding="utf-8"))
            if time.time() - old.get("ts", 0) < 600:
                return None  # 有人在跑
        except Exception:
            pass
    lk.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
    return lk


def release_lock(lk):
    try:
        if lk and lk.exists():
            lk.unlink()
    except Exception:
        pass


# ----------------------------------------------------------------------------
# scan / apply
# ----------------------------------------------------------------------------
def gather_files(cfg):
    root = Path(cfg["general"]["root"])
    sysname = cfg["general"]["system_dir"]
    files = []
    for entry in root.iterdir():
        if entry.is_dir():
            continue
        if entry.name == sysname:
            continue
        if fnmatch_any(entry.name, cfg["ignore"]["patterns"]):
            continue
        files.append(entry)
    return files


# ----------------------------------------------------------------------------
# 文件夹判断:整个夹是一个软件 → 压成 zip 改名归档(其余文件夹不动,进待裁决清单)
# ----------------------------------------------------------------------------
def _folder_files(d):
    return [f for f in d.rglob("*") if f.is_file()]


def is_software_folder(d, cfg):
    """整个文件夹就是一个软件:有可执行文件、不是代码项目、用户文档/媒体占比低。"""
    files = _folder_files(d)
    if len(files) < 3:
        return False
    proj = (".git", "package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml", "node_modules", "pom.xml", "tsconfig.json")
    if any((d / m).exists() for m in proj):
        return False                                     # 代码项目,绝不碰
    exts = [f.suffix.lower().lstrip(".") for f in files]
    if not any(e in ("exe", "msi", "dll", "sys") for e in exts):
        return False                                     # 没可执行文件,不是软件
    user_content = {"pdf","doc","docx","ppt","pptx","xls","xlsx","mp4","mov","avi","mkv","mp3","wav","flac","epub","psd"}
    uc_ratio = sum(1 for e in exts if e in user_content) / len(exts)
    return uc_ratio < cfg.get("folders", {}).get("user_content_max_ratio", 0.2)


def folder_is_stable(d, stable_seconds):
    """文件夹里最新文件的 mtime 早于现在 stable_seconds 秒,才算复制完成。"""
    files = _folder_files(d)
    if not files:
        return False
    return (time.time() - max(f.stat().st_mtime for f in files)) >= stable_seconds


def brain_software_name(folder_name, exes, n_files, cfg):
    """据文件夹名+主程序名让大脑起中文软件名(二进制不读内容,只据名判断)。"""
    sysp = "你是软件归档助手。据文件夹名和主程序名判断这是什么软件,只输出 JSON。"
    user = (f"软件文件夹名:{folder_name}\n主程序:{', '.join(exes) or '未知'}\n文件数:{n_files}\n"
            '只输出 JSON:{"name":"简洁名(原英文名+用途,如 CrystalDiskInfo磁盘健康检测工具)","summary":"50字内:什么软件、干什么用"}')
    try:
        out = _parse_json(_brain_text(sysp, user, cfg))
        return (out.get("name") or "").strip() or folder_name, (out.get("summary") or "")
    except Exception:
        return folder_name, ""


def process_folders(cfg, apply, quiet):
    """判断根目录下的文件夹:软件文件夹→压 zip 进 程序/(校验后删原夹);其余不动。"""
    import zipfile, shutil
    root = Path(cfg["general"]["root"])
    sysname = cfg["general"]["system_dir"]
    dests = dest_folder_names(cfg)
    fcfg = cfg.get("folders", {})
    if not fcfg.get("zip_software", True):
        return
    cands = [d for d in root.iterdir() if d.is_dir() and d.name != sysname and d.name not in dests]
    zipped = 0
    for d in cands:
        if not is_software_folder(d, cfg):
            continue                                     # 非软件夹:不动(留给 folders 命令出裁决清单)
        files = _folder_files(d)
        n = len(files)
        if not apply:
            log(f"  [dry] 软件文件夹 {d.name}（{n}文件）→ 压成 zip 进 程序/")
            continue
        if not folder_is_stable(d, fcfg.get("stable_seconds", 30)):
            if not quiet:
                log(f"  · 文件夹 {d.name} 还在写入,跳过(下轮再处理)")
            continue
        exes = sorted({f.name for f in files if f.suffix.lower() in (".exe", ".msi")})[:5]
        name, summary = brain_software_name(d.name, exes, n, cfg)
        date = dt.datetime.fromtimestamp(max(f.stat().st_mtime for f in files))
        zip_stem = build_name(date.strftime(cfg["naming"]["date_format"]), "软件", "", name, "", cfg)
        prog = root / "程序"
        prog.mkdir(parents=True, exist_ok=True)
        dest = unique_dest(prog, zip_stem + ".zip")
        tmp = d.parent / (d.name + ".ziptmp")
        try:
            with zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    z.write(str(f), str(f.relative_to(root)))    # arcname 含顶层夹名,解压可还原
            with zipfile.ZipFile(str(tmp)) as z:
                entries = [e for e in z.namelist() if not e.endswith("/")]
                broken = z.testzip()
            if broken is not None or len(entries) != n:
                log(f"  ⚠ {d.name} zip 校验失败(条目 {len(entries)}≠{n} 或损坏 {broken}),保留原夹不删")
                tmp.unlink()
                continue
            shutil.move(str(tmp), str(dest))
            shutil.rmtree(str(d))                          # 已校验完整,删原夹
            rec = {"orig_name": d.name + "/", "orig_path": str(d), "new_name": dest.name,
                   "new_path": str(dest), "category": "程序", "date": date.strftime(cfg["naming"]["date_format"]),
                   "date_source": "folder_mtime", "tags": {"客户": "", "人名": "", "项目": "", "类型": "软件"},
                   "summary": summary or f"软件文件夹打包,{n} 个文件", "sha256": sha256_of(dest),
                   "size": dest.stat().st_size, "ext": "zip", "sensitive": False,
                   "action": "folder_zip", "ts": dt.datetime.now().isoformat(timespec="seconds")}
            ledger_append(cfg, rec)
            con = db_connect(cfg); db_upsert(con, rec); con.close()
            zipped += 1
            log(f"  ✓ 软件夹 {d.name}（{n}文件）→ 程序/{dest.name}")
        except Exception as e:
            log(f"  ⚠ {d.name} 压缩失败: {e},保留原夹")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
    if zipped and not quiet:
        render_catalog(cfg)


def process(cfg, apply=False, quiet=False):
    lk = acquire_lock(cfg) if apply else True
    if lk is None:
        if not quiet:
            log("已有整理进程在运行(单飞锁),跳过。")
        return
    try:
        seen = load_seen_hashes(cfg)
        files = gather_files(cfg)
        if not files:
            if not quiet:
                log("根目录没有待处理的散文件。")
            process_folders(cfg, apply, quiet)   # 没散文件也要判断文件夹(软件→压zip)
            return
        log(f"发现 {len(files)} 个散文件,开始{'实跑' if apply else 'dry-run'}…")

        # 1) 采集 + 抽取
        records, text_items, image_items, audio_items, video_items, skipped = [], [], [], [], [], []
        text_local_items = []      # 敏感且 read_with=local:走本机 claude 订阅读,不经中转
        for idx, path in enumerate(files):
            if apply and not is_stable(path, cfg["general"]["stable_seconds"]):
                skipped.append((path.name, "未就绪/下载中"))
                continue
            ext = path.suffix.lower().lstrip(".")
            sz = path.stat().st_size
            sha = sha256_of(path)
            if sha in seen:
                skipped.append((path.name, f"重复(已存在)"))
                continue
            seen[sha] = None   # 批内回填:同批出现的同内容文件后续判重跳过,避免生成 (2) 冗余副本
            date, dsrc = resolve_date(path, ext, cfg)
            date_str = date.strftime(cfg["naming"]["date_format"])
            bucket = type_bucket(ext, cfg)
            sens = is_sensitive(path, cfg)
            rec = {"i": idx, "path": path, "orig_name": path.name, "ext": ext, "size": sz,
                   "sha256": sha, "date": date_str, "date_source": dsrc, "category": bucket,
                   "type_bucket": bucket, "sensitive": sens}
            records.append(rec)
            sens_read = cfg["sensitive"].get("read_with", "none")
            # 🔴 音视频只有 Gemini 一条路(第三方),所以敏感音视频无论 read_with 怎么配都不读 ——
            #    "本机订阅"在这两个模态上没有对应实现,不能拿它当借口把正文发出去。
            local_only = cfg["brain"].get("local_only", False)
            if local_only and bucket in ("音频", "视频"):
                rec["_route"] = "noread"             # 本机订阅没有音视频通道 ⇒ 不读,只按名归档
            elif sens and (sens_read == "none" or bucket in ("音频", "视频")):
                rec["_route"] = "noread"             # 敏感件不读
            elif bucket == "音频":
                rec["_route"] = "audio"; audio_items.append(rec)
            elif bucket == "视频":
                rec["_route"] = "video"; video_items.append(rec)
            elif ext in cfg["no_read"]["extensions"] or sz > cfg["no_read"]["max_read_mb"]*1024*1024:
                rec["_route"] = "noread"
            else:
                txt = extract_text(path, ext, cfg)
                if txt is None:
                    rec["_route"] = "image"
                    image_items.append(rec)
                elif content_is_sensitive(txt, cfg) and sens_read == "none":
                    rec["sensitive"] = True          # 内容级敏感 → 不发 LLM,按敏感打码
                    rec["_route"] = "noread"
                else:
                    if content_is_sensitive(txt, cfg):
                        rec["sensitive"] = True      # 仍标敏感:决定用哪个大脑读,不决定读不读
                    rec["_excerpt"] = txt
                    if rec["sensitive"] and sens_read == "local":
                        rec["_route"] = "text_local"
                        text_local_items.append(rec)
                    else:
                        rec["_route"] = "text"
                        text_items.append(rec)

        # 2) 大脑:文本批 + 各图片 并发跑(每个调用独立 → 总耗时≈最慢一个,不再串行排队)
        import concurrent.futures
        results = {}
        bs = cfg["brain"]["batch_size"]
        jobs = []
        for k in range(0, len(text_items), bs):
            chunk = text_items[k:k+bs]
            jobs.append(("text", [{"i": r["i"], "name": r["orig_name"], "type_bucket": r["type_bucket"],
                                   "date": r["date"], "excerpt": r["_excerpt"]} for r in chunk]))
        for r in image_items:
            jobs.append(("image", r))
        for r in audio_items:
            jobs.append(("audio", r))
        for r in video_items:
            jobs.append(("video", r))
        # 敏感批:同样按 batch_size 分批,但 cfg 换成本机 claude 订阅
        if text_local_items:
            local_cfg = json.loads(json.dumps(cfg))
            local_cfg["brain"]["backend"] = "claude_cli"
            local_cfg["brain"]["model"] = cfg["sensitive"].get("local_model", "sonnet")
            for k in range(0, len(text_local_items), bs):
                chunk = text_local_items[k:k + bs]
                jobs.append(("text_local", [{"i": r["i"], "name": r["orig_name"], "type_bucket": r["type_bucket"],
                                             "date": r["date"], "excerpt": r["_excerpt"]} for r in chunk]))

        def _run(job):
            kind, data = job
            if kind in ("image", "audio", "video"):
                fn = {"image": brain_image, "audio": brain_audio, "video": brain_video}[kind]
                try:
                    res = fn(data["path"], data["orig_name"], data["date"], cfg)
                    return {data["i"]: res} if res else {}
                except Exception as e:
                    log(f"  ⚠ {kind}失败 {data['orig_name'][:30]}: {e}")
                    return {}
            if kind == "text_local":
                try:
                    return brain_text_batch(data, local_cfg)   # 本机订阅,正文不出机
                except Exception as e:
                    log(f"  ⚠ 敏感批(本机订阅)失败,这些件按敏感打码归档: {e}")
                    return {}
            try:
                return brain_text_batch(data, cfg)  # 内部已带 force_json + 重试
            except Exception as e:
                log(f"  ⚠ 文本批失败,逐个重试: {e}")
                out = {}
                for one in data:  # 批失败 → 逐个调,单条极少出错,只有真失败才降级
                    try:
                        out.update(brain_text_batch([one], cfg))
                    except Exception as e2:
                        log(f"    · 逐个仍失败,降级文件名: {one['name'][:30]} ({e2})")
                return out

        if jobs:
            workers = max(1, min(cfg["brain"].get("max_concurrent", 3), len(jobs)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                for got in ex.map(_run, jobs):
                    results.update(got)
            log(f"  · 大脑并发完成 {len(results)}/{len(text_items)+len(text_local_items)+len(image_items)+len(audio_items)+len(video_items)} 条")

        # 3) 组装命名 + 落地/预览
        proposals = []
        root = Path(cfg["general"]["root"])
        con = db_connect(cfg) if apply else None
        deferred = 0
        for r in records:
            res = results.get(r["i"], {})
            # 🔴 本该读却没读到（中转 5xx / claude 不在 PATH / 网络抖）→ 这轮不动它，下轮再试。
            #    自动跑起来之后，"降级成 日期_文档_原名 照样归档" 会把一次暂时性故障
            #    变成写进台账的既成事实；晚十分钟比错一个名字便宜得多。
            #    ⚠ 只管本该读的路由；noread（敏感不读/二进制/超大件）本来就没有结果，照常落地。
            if not res and r.get("_route") in ("text", "text_local", "image", "audio", "video"):
                deferred += 1
                continue
            # 🔴 判据从"是不是敏感"改成"这一件到底有没有被读到" —— read_with=local 时敏感件
            #    是真读过的,还按打码命名等于白花那笔订阅额度、检索价值也拿不到。
            if r["sensitive"] and not res:
                stem = sanitize(Path(r["orig_name"]).stem)
                semantic = sensitive_label(r["orig_name"])
                tags = {"客户":"", "人名":"", "项目":"", "类型": semantic, "敏感": True}
                summary = "「敏感·健康/财务/凭据 不入明文索引,仅登记类型与位置」"
                new_name = build_name(r["date"], semantic, "", stem[:40], r["ext"], cfg)
            else:
                semantic = res.get("semantic_type", "") or r["type_bucket"]
                subject = normalize_tag(res.get("subject", ""), "项目", cfg) or res.get("subject", "")
                topic = res.get("topic", "") or sanitize(Path(r["orig_name"]).stem)
                raw_tags = res.get("tags", {}) or {}
                tags = {"客户": normalize_tag(raw_tags.get("客户",""), "客户", cfg),
                        "人名": normalize_tag(raw_tags.get("人名",""), "人名", cfg),
                        "项目": normalize_tag(raw_tags.get("项目",""), "项目", cfg),
                        "类型": semantic}
                subj = tags["客户"] or tags["项目"] or tags["人名"] or subject
                summary = res.get("summary", "") or ""
                if not summary:
                    summary = ("（%s，未读内容，按文件名归档）" % r["type_bucket"]) if r.get("_route") == "noread" else "（自动梗概失败，沿用原名要素）"
                new_name = build_name(r["date"], semantic, subj, topic, r["ext"], cfg)
            r["category"] = route_category(r["category"], tags.get("类型", ""), new_name, cfg)  # 文档→体裁顶级夹
            folder = root / r["category"]
            dest = unique_dest(folder, new_name)
            prop = {"orig_name": r["orig_name"], "orig_path": str(r["path"]),
                    "new_name": dest.name, "new_path": str(dest), "category": r["category"],
                    "date": r["date"], "date_source": r["date_source"], "tags": tags,
                    "summary": summary, "sha256": r["sha256"], "size": r["size"],
                    "ext": r["ext"], "sensitive": r["sensitive"]}
            proposals.append(prop)
            if apply:
                folder.mkdir(parents=True, exist_ok=True)
                os.replace(str(r["path"]), str(dest))  # 同盘移动,原子
                rec = dict(prop); rec["action"] = "move"; rec["ts"] = dt.datetime.now().isoformat(timespec="seconds")
                ledger_append(cfg, rec)
                db_upsert(con, rec)
                log(f"  ✓ {r['orig_name'][:34]} → {r['category']}/{dest.name[:40]}")
        if apply and con:
            con.close()
            render_catalog(cfg)

        # 4) 报告
        for nm, why in skipped:
            log(f"  · 跳过 {nm[:34]}: {why}")   # 让 watch.log 看得到跳过原因(未就绪→周期重扫会重试)
        write_report(cfg, proposals, skipped, apply)
        if deferred:
            log(f"  ⏸ {deferred} 件大脑没读到,这轮不动它们,下轮再试")
        log(f"完成。处理 {len(proposals)} 条,跳过 {len(skipped)} 条。"
            f"{'已写入台账,可 undo。' if apply else '这是 dry-run,未动文件,看 索引/_预览.md。'}")
        process_folders(cfg, apply, quiet)   # 散文件处理完,再判断文件夹(软件→压zip)
    finally:
        if apply:
            release_lock(lk)


def write_report(cfg, proposals, skipped, apply):
    title = "实跑结果" if apply else "DRY-RUN 预览(未动文件)"
    out = [f"# {title}", "", f"_{dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · {len(proposals)} 条_", ""]
    out += ["| 原名 | → 新位置/新名 | 类型 | 客户/项目/人名 | 日期(来源) | 梗概 |", "|---|---|---|---|---|---|"]
    for p in proposals:
        t = p["tags"]
        who = " / ".join(x for x in (t.get("客户"), t.get("项目"), t.get("人名")) if x) or "—"
        # 同 目录.md:读过就写真梗概(打码件的 summary 本身就是那句说明)
        summ = (p["summary"] or "")[:50]
        o = p["orig_name"].replace("|", "\\|")
        n = f"{p['category']}/{p['new_name']}".replace("|", "\\|")
        out.append(f"| {o} | {n} | {t.get('类型','—')} | {who} | {p['date']}({p['date_source']}) | {summ} |")
    if skipped:
        out += ["", "## 跳过", ""]
        for name, why in skipped:
            out.append(f"- {name} — {why}")
    (system_dir(cfg) / "_预览.md").write_text("\n".join(out) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------
# undo / reindex / search / folders / init
# ----------------------------------------------------------------------------
def undo(cfg, last):
    p = ledger_path(cfg)
    if not p.exists():
        log("无台账,无可撤销。")
        return
    lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    recs = [json.loads(l) for l in lines]
    moves = [i for i, r in enumerate(recs) if r.get("action") == "move"]
    target = moves if last is None else moves[-last:]
    undone = 0
    keep = list(lines)
    for i in reversed(target):
        r = recs[i]
        src = Path(r["new_path"]); dst = Path(r["orig_path"])
        try:
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(src), str(dst))
                undone += 1
                keep[i] = None
                log(f"  ↩ {src.name} → {dst.name}")
            else:
                log(f"  ⚠ 源已不在,跳过: {src.name}")
        except Exception as e:
            log(f"  ⚠ 撤销失败 {src.name}: {e}")
    # 重写台账(去掉已撤销行)+ 重建索引
    p.write_text("\n".join(l for l in keep if l) + ("\n" if any(keep) else ""), encoding="utf-8")
    rebuild_db(cfg)
    render_catalog(cfg)
    log(f"撤销 {undone} 条,已重建索引。")


def rebuild_db(cfg):
    dbp = db_path(cfg)
    if dbp.exists():
        dbp.unlink()
    con = db_connect(cfg)
    n = 0
    for r in live_records(cfg):        # 存活记录(gone tombstone 已摘除)
        db_upsert(con, r); n += 1
    con.close()
    return n


def search(cfg, query, filters):
    con = db_connect(cfg)
    where, params = [], []
    if query:
        where.append("(new_name LIKE ? OR summary LIKE ? OR kehu LIKE ? OR renming LIKE ? OR xiangmu LIKE ? OR leixing LIKE ?)")
        params += [f"%{query}%"] * 6
    for facet, col in (("客户","kehu"),("人名","renming"),("项目","xiangmu"),("类型","leixing")):
        if filters.get(facet):
            where.append(f"{col} LIKE ?"); params.append(f"%{filters[facet]}%")
    sql = "SELECT new_name, category, date, kehu, xiangmu, renming, leixing, new_path, summary FROM idx"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC LIMIT 50"
    rows = con.execute(sql, params).fetchall()
    con.close()
    if not rows:
        log("无结果。"); return
    for r in rows:
        who = " / ".join(x for x in (r[3], r[4], r[5]) if x) or "—"
        print(f"\n● {r[0]}\n  {r[1]} · {r[2]} · {r[6]} · {who}\n  {r[7]}\n  {(r[8] or '')[:90]}")


def analyze_folders(cfg):
    root = Path(cfg["general"]["root"])
    sysname = cfg["general"]["system_dir"]
    out = ["# 文件夹待裁决清单", "",
           f"_{dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · v1 只识别不动手,你定夺_", "",
           "| 文件夹 | 文件数 | 大小MB | 推断类型 | 建议 | 压缩? |", "|---|---|---|---|---|---|"]
    proj_markers = [".git", "package.json", "requirements.txt", "pom.xml", "Cargo.toml", "go.mod", "pyproject.toml", "node_modules"]
    media_ext = {"jpg","jpeg","png","gif","webp","mp4","mkv","mov","m4v","heic"}
    dests = dest_folder_names(cfg)            # 自己的归档目的地夹不算待裁决
    for d in sorted([x for x in root.iterdir() if x.is_dir() and x.name != sysname and x.name not in dests]):
        files = [f for f in d.rglob("*") if f.is_file()]
        n = len(files)
        size_mb = round(sum(f.stat().st_size for f in files) / 1024 / 1024, 1)
        names = {f.name.lower() for f in files} | {p.name.lower() for p in d.rglob("*")}
        exts = [f.suffix.lower().lstrip(".") for f in files]
        media_ratio = (sum(1 for e in exts if e in media_ext) / n) if n else 0
        has_proj = any(m in names for m in proj_markers) or any((d / m).exists() for m in proj_markers)
        has_app = any(e in ("exe","dll","sys","inf") for e in exts)
        nm = d.name.lower()
        if has_proj:
            kind, advice, zip_ = "代码项目", "建议升级到 3_项目(不压)", "✗"
        elif has_app:
            kind, advice, zip_ = "软件/驱动", "移到 年/程序 或保留", "✗"
        elif media_ratio > 0.6:
            kind, advice, zip_ = "媒体集", "移到 年/图片或视频", "✗已压缩"
        elif "backup" in nm or "config" in nm or "备份" in nm:
            kind, advice, zip_ = "配置/备份", "压成 zip 移到 年/压缩包", "✓"
        elif n > 0 and sum(1 for e in exts if e in ("pdf","doc","docx","txt","md","ppt","pptx")) / n > 0.5:
            kind, advice, zip_ = "文档集", "压成 zip 移到 年/压缩包", "✓"
        else:
            kind, advice, zip_ = "混杂", "人工裁决(不自动动)", "✗"
        out.append(f"| {d.name} | {n} | {size_mb} | {kind} | {advice} | {zip_} |")
    (system_dir(cfg) / "_文件夹待裁决.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    log(f"已写 {system_dir(cfg).name}/_文件夹待裁决.md")


def migrate(cfg):
    """一次性回迁:把已归档文档按 doc_genre_folders 规则搬到体裁顶级夹,同步台账/索引/目录.md。
    幂等(已在正确夹的不动)、可 undo(undo 仍把文件还原到最初落点)、不删除(同盘原子移动)。"""
    lk = acquire_lock(cfg)
    if lk is None:
        log("已有整理进程在运行(单飞锁),稍后再试。")
        return
    try:
        root = Path(cfg["general"]["root"])
        p = ledger_path(cfg)
        if not p.exists():
            log("无台账,无可回迁。")
            return
        lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        recs = [json.loads(l) for l in lines]
        doc_folders = {"文档"} | {ru.get("folder") for ru in cfg.get("doc_genre_folders", [])}
        moved = 0
        for i, r in enumerate(recs):
            if r.get("action") != "move":
                continue
            cur = r.get("category", "")
            base = "文档" if cur in doc_folders else cur   # 已在体裁夹的也重判,容许夹间迁移(如口播里的会议转写)
            target = route_category(base, (r.get("tags") or {}).get("类型", ""), r.get("new_name", ""), cfg)
            if target == cur:
                continue                       # 已在正确夹 / 长尾留文档 / 媒体不动
            src = Path(r["new_path"])
            if not src.exists():
                continue                       # 历史或已撤销记录,文件不在,跳过
            folder = root / target
            folder.mkdir(parents=True, exist_ok=True)
            dest = unique_dest(folder, src.name)
            os.replace(str(src), str(dest))    # 同盘原子移动,不删除、不覆盖(dest 唯一)
            r["category"] = target
            r["new_path"] = str(dest)
            r["new_name"] = dest.name
            moved += 1
            log(f"  ↪ {cur}/{src.name[:34]} → {target}/{dest.name[:34]}")
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
        rebuild_db(cfg)
        render_catalog(cfg)
        log(f"回迁完成:移动 {moved} 个文件,台账/索引/目录.md 已重建。")
    finally:
        release_lock(lk)


def reconcile(cfg, apply=True, quiet=False):
    """索引 ↔ 磁盘对账(不碰任何文件,只修索引):
      悬空 = 索引有、磁盘无 → 台账追加 gone tombstone,重建 SQLite/目录.md 摘除(append-only,不影响 undo)
      孤儿 = 归档夹里有、索引无 → 写 _对账清单.md 待用户裁决,绝不自动改名/移动"""
    lk = acquire_lock(cfg) if apply else True
    if lk is None:
        if not quiet:
            log("已有整理进程在运行(单飞锁),稍后再对账。")
        return
    try:
        root = Path(cfg["general"]["root"])
        live = live_records(cfg)
        # 1) 悬空判定:exists()==False ≠ "被删"。os.path.exists 吞掉一切 OSError(盘符掉线/权限/
        #    超时/云盘后端抖动)都返回 False。二次确认:父目录必须可列出、且确实不含该文件,才判真删;
        #    父目录读不到或文件其实在 → 视为瞬时不可达,本轮不动,避免误 tombstone 真实文件。
        confirmed_gone, transient = [], []
        for r in live:
            p = r.get("new_path")
            if not p:
                continue
            fp = Path(p)
            if fp.exists():
                continue
            try:
                listing = os.listdir(fp.parent) if fp.parent.is_dir() else None
            except OSError:
                listing = None
            if listing is None or fp.name in listing:
                transient.append(r)          # 父目录不可达 / exists 抖动 → 不 tombstone
            else:
                confirmed_gone.append(r)     # 目录可读且确无此文件 → 真删
        # 熔断:单轮"真删"占比过高,几乎必是存储整体故障而非用户逐个删 → 放弃本轮,一条都不摘
        danger_cap = max(20, int(len(live) * 0.5))
        tripped = len(confirmed_gone) > danger_cap
        dangling = [] if tripped else confirmed_gone
        if tripped:
            log(f"  ⚠ 悬空 {len(confirmed_gone)} 条 > 阈值 {danger_cap},疑似存储故障,本轮熔断不摘除")
        for r in dangling:
            log(f"  {'✓ 摘除悬空' if apply else '[dry] 悬空'} {r.get('category','?')}/{r.get('new_name','?')}")
            if apply:
                ledger_append(cfg, {
                    "action": "gone", "sha256": r.get("sha256"),
                    "orig_name": r.get("orig_name", ""), "new_name": r.get("new_name", ""),
                    "new_path": r.get("new_path", ""), "category": r.get("category", ""),
                    "reason": "missing_on_disk",
                    "ts": dt.datetime.now().isoformat(timespec="seconds")})
        if transient:
            log(f"  · {len(transient)} 条疑似瞬时不可达(父目录读不到),未处理,下轮复核")
        # 2) 孤儿判定:磁盘有、索引无。但"同内容多副本"(sha 已被别的记录索引)不算孤儿——
        #    否则清单会诱导用户把副本移回根目录,反被 sha 判重永久跳过(死循环)。
        indexed_paths = {str(Path(r["new_path"]).resolve()).lower() for r in live if r.get("new_path")}
        indexed_shas = {r.get("sha256") for r in live if r.get("sha256")}
        orphans, dup_copies = [], []
        for dname in dest_folder_names(cfg):
            d = root / dname
            if not d.exists():
                continue
            for f in d.rglob("*"):
                if not f.is_file() or fnmatch_any(f.name, cfg["ignore"]["patterns"]):
                    continue
                if str(f.resolve()).lower() in indexed_paths:
                    continue
                fsha = None
                try:
                    if f.stat().st_size <= 500 * 1024 * 1024:   # 超大文件不逐轮 hash,直接当孤儿
                        fsha = sha256_of(f)
                except OSError:
                    pass
                if fsha and fsha in indexed_shas:
                    dup_copies.append(f)     # 内容已被索引,只是多存一份副本,不是孤儿
                else:
                    orphans.append(f)
        write_reconcile_report(cfg, dangling, orphans, dup_copies, transient, tripped, apply)
        # 3) 重建派生索引(仅当真有悬空被摘除)
        if apply and dangling:
            rebuild_db(cfg)
            render_catalog(cfg)
        if not quiet or dangling or orphans or dup_copies:
            log(f"对账完成:悬空 {len(dangling)} {'已摘' if apply else '(dry)'},"
                f"孤儿 {len(orphans)},重复副本 {len(dup_copies)}(见 索引/_对账清单.md)")
    finally:
        if apply:
            release_lock(lk)


def write_reconcile_report(cfg, dangling, orphans, dup_copies=None, transient=None, tripped=False, applied=True):
    dup_copies = dup_copies or []
    transient = transient or []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    tag = "已摘除" if applied else "预览·未摘除"
    out = ["# 索引对账清单", "",
           f"_{now} · 悬空 {len(dangling)}({tag}) · 孤儿 {len(orphans)} · 重复副本 {len(dup_copies)} · 勿手改_", ""]
    if tripped:
        out += ["> ⚠ 本轮悬空数超阈值,疑似存储故障,已熔断——未摘除任何条目。"
                "确认盘符/云盘正常后手动跑 `reconcile`。", ""]
    out += ["## 悬空 — 索引有、磁盘已无(已从 SQLite/目录.md 摘除,台账留 tombstone)", ""]
    if dangling:
        out += ["| 位置 | 文件 |", "|---|---|"]
        for r in dangling:
            nm = (r.get("new_name", "") or "").replace("|", "\\|")
            out.append(f"| {r.get('category','?')} | {nm} |")
    else:
        out.append("_无_")
    out += ["", "## 孤儿 — 磁盘有、索引无(未自动改动,你来裁决)", ""]
    if orphans:
        out += ["| 位置 | 文件 | KB |", "|---|---|---|"]
        for f in orphans:
            nm = f.name.replace("|", "\\|")
            out.append(f"| {f.parent.name} | {nm} | {round(f.stat().st_size / 1024)} |")
        out += ["", "_要归位的:移回根目录 `4_临时\\` 让 watcher 重整;要保留的:忽略即可(下次对账仍会列出)。_"]
    else:
        out.append("_无_")
    out += ["", "## 重复副本 — 内容已被索引,磁盘多存一份(别移回根目录,会被判重跳过)", ""]
    if dup_copies:
        out += ["| 位置 | 文件 | KB |", "|---|---|---|"]
        for f in dup_copies:
            nm = f.name.replace("|", "\\|")
            out.append(f"| {f.parent.name} | {nm} | {round(f.stat().st_size / 1024)} |")
        out += ["", "_确认多余就直接删这份副本(内容在索引里另有其名);要另作它用就忽略。_"]
    else:
        out.append("_无_")
    if transient:
        out += ["", f"## 疑似瞬时不可达 — {len(transient)} 条(父目录读不到,未判悬空,下轮复核)", ""]
    (system_dir(cfg) / "_对账清单.md").write_text("\n".join(out) + "\n", encoding="utf-8")


def init(cfg):
    sd = system_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    cfgfile = sd / "规则.json"
    if not cfgfile.exists():
        cfgfile.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"已写默认 {cfgfile}")
    ledger_path(cfg).touch(exist_ok=True)
    db_connect(cfg).close()
    log(f"系统目录就绪: {sd}")


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="4_临时 自动整理引擎")
    ap.add_argument("cmd", choices=["scan", "apply", "undo", "reindex", "search", "folders", "init", "migrate", "reconcile"])
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")   # reconcile 用:只预览悬空/孤儿,不写 tombstone、不重建索引
    ap.add_argument("--quiet", action="store_true")   # 周期重扫用:无文件时不刷屏
    ap.add_argument("--last", type=int, default=None)
    ap.add_argument("--root", default=None)
    for facet in ("客户", "人名", "项目", "类型"):
        ap.add_argument(f"--{facet}", default=None)
    args = ap.parse_args()

    cfg = load_config(args.root)
    if args.root:
        cfg["general"]["root"] = args.root

    if args.cmd == "init":
        init(cfg)
    elif args.cmd == "scan":
        process(cfg, apply=args.apply, quiet=args.quiet)
    elif args.cmd == "apply":
        process(cfg, apply=True, quiet=args.quiet)
    elif args.cmd == "undo":
        undo(cfg, args.last)
    elif args.cmd == "reindex":
        n = rebuild_db(cfg); render_catalog(cfg); log(f"重建 {n} 条。")
    elif args.cmd == "reconcile":
        reconcile(cfg, apply=not args.dry_run, quiet=args.quiet)
    elif args.cmd == "search":
        search(cfg, args.query, {f: getattr(args, f) for f in ("客户", "人名", "项目", "类型")})
    elif args.cmd == "folders":
        analyze_folders(cfg)
    elif args.cmd == "migrate":
        migrate(cfg)


if __name__ == "__main__":
    main()
