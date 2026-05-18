"""LLM client that shells out to the local `claude` CLI in headless mode.

Local-only: spawns `claude -p` using the operator's Max plan via OAuth — no
ANTHROPIC_API_KEY needed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import preferences
from ._proc import child_env
from .wechat import Message

log = logging.getLogger(__name__)


_SUMMARY_PROMPT = """你是「ChatLens」的社群知识管家。
你会收到一段微信群当日的聊天记录。请输出两部分：

# 第一部分：每日精华 Markdown

直接以下面这一行 H1 开头，无前言、无代码块。

# {date} {group} · 每日精华
> 一句话总结今天讨论的主线。

## 今日话题
列出 2-6 个话题，每条一句话。**严格使用以下格式**，每个话题前必须有一个 `[T1]` `[T2]` …的标记；条目末尾用半角括号附上该话题的起始时间（取该话题最早一条消息的 `[HH:MM]`），话题跨度大时可写区间 `(HH:MM-HH:MM)`：
- [T1] 话题一标题（一句话）`(14:53)`
- [T2] 话题二标题（一句话）`(15:30-16:10)`

## 🧰 工具与资源
列出今天群里出现过的 URL / 文章 / 工具 / 文件 / 产品。**每条必须用 Markdown 链接语法可点击**；条目末尾用半角括号附上该资源在群里出现时的 `[HH:MM]`：
- [名称](URL) — 分享人 · 简短说明 `(14:53)`
没有的话写 「暂无」。

## 💡 主要观点
3-8 条观点，用 blockquote 引用，注明发言人；每条引用末尾用半角括号附上发言时间：
> 观点内容 — 发言人 `(14:53)`

# 第二部分：META JSON

紧接上面摘要的最后一行，输出严格用下面两行包裹的 **一个** JSON。整段必须可被 `json.loads`。

<<<META>>>
{{
  "topics": [
    {{
      "id": "T1",
      "title": "话题一标题",
      "lines": [3, 4, 5, 7]
    }},
    {{ "id": "T2", "title": "话题二标题", "lines": [12, 13, 14] }}
  ],
  "shares": [
    {{
      "title": "分享标题，应能独立成立",
      "speaker": "分享人在群里的昵称",
      "body_md": "完整的分享正文 Markdown。包含背景、核心要点、操作步骤/案例、可执行总结。如果分享中有图片，用 ![](filename.jpg) 嵌入文件名（必须与下面聊天记录中 [图片 xxx.jpg] 完全一致）。如果分享中包含 URL，用 [名称](URL) 嵌入。",
      "image_refs": ["filename1.jpg"],
      "lines": [20, 21, 22, 23]
    }}
  ]
}}
<<<END>>>

# 规则与判定

- `topics[].id` 必须与 H2「今日话题」列表里 `[T1]` 的标记一一对应。
- `topics[].lines` 必须是聊天记录里 `L0001`、`L0002`… 行号中的整数（去掉前缀 `L` 和前导零），覆盖该话题涉及到的**所有**消息，不要遗漏。
- **判定何为「分享」**：某人或多人围绕一个明确主题连续给出 3 条以上结构化、有信息量的发言（教程、工具讲解、深度观点、案例分析）；纯闲聊/抱怨/问答不算。如果今天没有，`shares` 数组保持为空 `[]`。
- 分享的 `body_md` 必须自成一篇，读者不回原聊天也能看懂。`lines` 列出分享所跨越的所有消息行号。
- 不要在两部分之外有任何额外输出（无前言、结语、解释、代码块）。

# 群名
{group}

# 日期
{date}

# 聊天记录（行格式：`Lxxxx [HH:MM] 发言人: 内容`，日期就是上方的 {date}；图片标注为 `[图片 xxx.jpg]`；语音/视频已简化）

{transcript}
"""


_META_RE = re.compile(r"<<<META>>>\s*(\{.*?\})\s*<<<END>>>", re.DOTALL)
_NOISY_VOICE = re.compile(r"\[语音\]\s*<msg>.*?</msg>", re.DOTALL)
_URL_RE = re.compile(r"https?://[^\s<>\"'】）)]+")
_XML_TITLE = re.compile(r"<title>([^<]+)</title>")
_XML_URL = re.compile(r"<url>([^<]+)</url>")
# After a bracketed type label (`[视频]` `[系统]` `[图片]` `[语音]` …), wechat-cli
# often dumps a multi-hundred-byte XML blob (videomsg / voicemsg / sysmsg /
# mmchatroomtopmsg) full of base64 keys and CDN URLs. The model gets nothing
# from it but pays full token cost — drop everything after the label.
_TRAILING_XML = re.compile(
    r"(?P<tag>\[[^\]]+\])\s*(?:<\?xml[^?]*\?>\s*)?<(?:msg|sysmsg)\b.*",
    re.DOTALL,
)
# Same idea but for lines that start straight with XML (no bracketed label).
_BARE_SYSMSG = re.compile(r"<sysmsg\b.*?</sysmsg>\s*", re.DOTALL)
_BARE_MSG = re.compile(r"<msg\b.*?</msg>\s*", re.DOTALL)
# wechat-cli history sometimes trails messages with " (local_id=NNNN)" — a
# debug-style row id that's no signal for the LLM. Strip it to save tokens.
_LOCAL_ID_SUFFIX = re.compile(r"\s*\(local_id=\d+\)\s*$")


@dataclass(slots=True)
class Topic:
    id: str
    title: str
    lines: list[int] = field(default_factory=list)


@dataclass(slots=True)
class Share:
    title: str
    speaker: str
    body_md: str
    image_refs: list[str]
    lines: list[int] = field(default_factory=list)


@dataclass(slots=True)
class SummaryResult:
    summary_md: str
    topics: list[Topic]
    shares: list[Share]
    image_paths_by_name: dict[str, str]
    # Numbered transcript lines, indexed by integer line number → resolved message text
    line_to_message: dict[int, dict[str, str]]


def _claude_path() -> str:
    p = os.environ.get("CLAUDE_PATH") or shutil.which("claude")
    if not p:
        raise RuntimeError("`claude` CLI not found on PATH — install Claude Code")
    return p


# Tracks the most recent `claude -p` failure so the UI can show a banner
# without having to do an expensive probe call on every page load.
_last_error: dict[str, Any] | None = None

_RATE_LIMIT_RE = re.compile(
    r"(?ix)"
    r"rate[\s\-]?limit|"
    r"usage[\s\-]?limit|"
    r"reached\s+your\s+.{0,40}limit|"
    r"claude\s+usage\s+limit|"
    r"too\s+many\s+requests|"
    r"\b429\b|"
    r"quota.{0,40}exceeded|"
    r"5-?hour\s+limit|"
    r"weekly\s+limit"
)
_AUTH_RE = re.compile(r"(?i)not\s+authenticated|invalid\s+api\s+key|please\s+run\s+/login|session\s+expired")


class ClaudeRateLimitError(RuntimeError):
    """Raised when `claude -p` exits with a rate-limit-shaped error. Typed
    separately from RuntimeError so the job runner can recognise it and back
    off + retry rather than treating it as a fatal per-group failure."""
    pass


def _record_error(msg: str) -> None:
    global _last_error
    _last_error = {
        "at": time.time(),
        "msg": msg[:400],
        "rate_limited": bool(_RATE_LIMIT_RE.search(msg)),
        "auth_error": bool(_AUTH_RE.search(msg)),
    }


def _clear_error() -> None:
    global _last_error
    _last_error = None


def _extract_title(payload: str) -> str:
    """Return a trimmed <title> from an XML payload, or '' if none. Capped at
    200 chars so a giant appmsg title can't single-handedly bloat the prompt."""
    m = _XML_TITLE.search(payload)
    if not m:
        return ""
    return m.group(1).strip()[:200]


def _condense(text: str) -> str:
    """Collapse the noisiest payloads so the LLM sees the gist, not the XML.

    XML in WeChat messages comes in two shapes: pure transport metadata
    (videomsg/voicemsg base64 keys, sysmsg op codes) and content cards that
    actually carry user-visible text in `<title>`/`<url>` (mini-program shares,
    public-account articles, ChatRoomNotice announcements). Strip the former
    aggressively; preserve a title from the latter so the model still sees what
    was shared, just without the kilobyte of attached gunk.
    """
    # voice XML → just the tag
    text = _NOISY_VOICE.sub("[语音]", text)
    # shared-article XML with both title+url → "[链接: title — url]"
    if "<title>" in text and "<url>" in text:
        t = _XML_TITLE.search(text)
        u = _XML_URL.search(text)
        if t and u:
            return f"[链接: {t.group(1).strip()} — {u.group(1).strip()}]"

    # Strip the noisy XML body but keep a title when present. Mirrors
    # wechat.py:_xml_to_tag, which does the same for reply-quote payloads.
    def _strip_trailing(m: re.Match) -> str:
        tag = m.group("tag")
        title = _extract_title(m.group(0))
        return f"{tag} {title}" if title else tag

    def _strip_bare_sysmsg(m: re.Match) -> str:
        title = _extract_title(m.group(0))
        return f"[系统: {title}]" if title else "[系统]"

    def _strip_bare_msg(m: re.Match) -> str:
        title = _extract_title(m.group(0))
        return f"[卡片: {title}]" if title else "[附件]"

    text = _TRAILING_XML.sub(_strip_trailing, text)
    text = _BARE_SYSMSG.sub(_strip_bare_sysmsg, text)
    text = _BARE_MSG.sub(_strip_bare_msg, text)
    # wechat-cli debug suffix — see _LOCAL_ID_SUFFIX.
    text = _LOCAL_ID_SUFFIX.sub("", text)
    return text


def _build_transcript(
    messages: list[Message],
    start_line: int = 1,
) -> tuple[str, dict[str, str], dict[int, dict[str, str]]]:
    """Number each line, collapse noise, return:
      (transcript_text, filename→path map, line_no → {ts, sender, text} map)

    `start_line` lets the incremental path continue numbering from where the
    previous transcript left off, so old `topics[].lines` references stay valid
    when the new meta is merged in.
    """
    lines: list[str] = []
    images: dict[str, str] = {}
    by_line: dict[int, dict[str, str]] = {}
    line_no = start_line - 1
    for m in messages:
        text = _condense(m.text)
        # image handling
        if m.image_paths:
            for p in m.image_paths:
                fname = Path(p).name
                images[fname] = p
                text = text.replace(p, "")
            text = re.sub(r"\(缩略图\)", "", text)
            tags = " ".join(f"[图片 {Path(p).name}]" for p in m.image_paths)
            text = re.sub(r"\[图片\](\s*)+", "", text).strip()
            text = (text + " " + tags).strip() if text else tags
        if not text.strip():
            continue
        line_no += 1
        if m.ts and m.sender:
            # Render only HH:MM in the prompt — the date is constant within
            # one summarize call and is already stated in the prompt header,
            # so 11 chars × N lines of repeated YYYY-MM-DD is pure noise. Keep
            # the full ts in by_line so the asset's drill-down view still has
            # absolute timestamps.
            short_ts = m.ts[11:] if len(m.ts) >= 11 and m.ts[10] == " " else m.ts
            lines.append(f"L{line_no:04d} [{short_ts}] {m.sender}: {text}")
            by_line[line_no] = {"ts": m.ts, "sender": m.sender, "text": text}
        else:
            lines.append(f"L{line_no:04d} {text}")
            by_line[line_no] = {"ts": "", "sender": "", "text": text}
    return "\n".join(lines), images, by_line


async def _run_claude(prompt: str, timeout_s: int | None = None) -> str:
    # Scale the timeout to the prompt size — sonnet on a 35 KB Chinese-heavy
    # transcript can legitimately run for >10 min (input streaming + output
    # generation + occasional Anthropic-side queueing when many parallel
    # `claude -p` calls fire from a batch). Floor at 600s for short prompts,
    # cap at 1800s so a truly stuck call still surfaces eventually.
    if timeout_s is None:
        timeout_s = min(1800, max(600, len(prompt) // 30))
    with tempfile.TemporaryDirectory(prefix="chatlens-claude-") as cwd:
        Path(cwd, ".gitignore").write_text("*\n")
        # Read the model on every call so a flip in the UI takes effect for
        # the very next summarize without restarting the server.
        model = preferences.get_model()
        cmd = [
            _claude_path(),
            "-p",
            "--model", model,
            "--output-format", "text",
            "--disable-slash-commands",
            "--exclude-dynamic-system-prompt-sections",
        ]
        log.info("calling claude -p (model=%s, prompt=%d chars, timeout=%ds)",
                 model, len(prompt), timeout_s)
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(
                f"claude -p timed out after {timeout_s}s (prompt {len(prompt)} chars, model={model})"
            )
        except asyncio.CancelledError:
            # Job was cancelled mid-call. Kill the subprocess so it stops
            # eating Claude tokens and bubble the cancellation up.
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            raise
        if proc.returncode != 0:
            # The CLI often writes diagnostics (rate limits, auth issues, oversized
            # prompts) to stdout rather than stderr, so include both.
            err = stderr.decode("utf-8", errors="replace").strip()
            out = stdout.decode("utf-8", errors="replace").strip()
            detail = err or out or "(no output)"
            _record_error(detail)
            msg = f"claude -p exited {proc.returncode} (prompt {len(prompt)} chars): {detail[:800]}"
            # Job runner relies on this typed exception to decide between
            # back-off-and-retry and "real failure, move on / bail".
            if _RATE_LIMIT_RE.search(detail):
                raise ClaudeRateLimitError(msg)
            raise RuntimeError(msg)
    _clear_error()
    return stdout.decode("utf-8", errors="replace").strip()


def _parse_meta(raw: str) -> tuple[str, list[Topic], list[Share]]:
    m = _META_RE.search(raw)
    if not m:
        return raw.strip(), [], []
    summary = raw[:m.start()].rstrip()
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        log.warning("could not parse META JSON: %s", e)
        return summary, [], []
    topics: list[Topic] = []
    for t in data.get("topics") or []:
        topics.append(Topic(
            id=str(t.get("id", "")).strip(),
            title=str(t.get("title", "")).strip(),
            lines=[int(x) for x in (t.get("lines") or []) if isinstance(x, (int, str)) and str(x).isdigit()],
        ))
    shares: list[Share] = []
    for s in data.get("shares") or []:
        shares.append(Share(
            title=str(s.get("title", "")).strip() or "未命名分享",
            speaker=str(s.get("speaker", "")).strip() or "未知",
            body_md=str(s.get("body_md", "")).strip(),
            image_refs=[str(x) for x in (s.get("image_refs") or [])],
            lines=[int(x) for x in (s.get("lines") or []) if isinstance(x, (int, str)) and str(x).isdigit()],
        ))
    return summary, topics, shares


async def summarize_chat(group_name: str, date: str, messages: list[Message]) -> SummaryResult:
    transcript, image_map, by_line = _build_transcript(messages)
    prompt = _SUMMARY_PROMPT.format(group=group_name, date=date, transcript=transcript)
    raw = await _run_claude(prompt)
    summary_md, topics, shares = _parse_meta(raw)
    return SummaryResult(
        summary_md=summary_md,
        topics=topics,
        shares=shares,
        image_paths_by_name=image_map,
        line_to_message=by_line,
    )


_INCREMENTAL_SUMMARY_PROMPT = """你是「ChatLens」的社群知识管家。你之前已经为这个微信群当日生成过一份「每日精华」。现在到达了一批新的聊天记录，请基于已有精华，吸纳新内容，输出更新后的完整版本。

# 已有的「每日精华」 (你早些时候输出的版本)

{old_summary_md}

# 已有话题列表 (保留这些 ID；如果新消息延续了某个旧话题，请在 META 里使用相同 ID)

{old_topics_legend}

# 新聊天记录 (行号从 L{next_line:04d} 开始，沿用旧的行号体系；时间格式 `[HH:MM]`，日期就是上方的 {date})

{transcript}

# 输出要求

## 第一部分：完整更新后的「每日精华」Markdown

直接以 H1 开头，无前言、无代码块。格式必须与旧版完全一致：

# {date} {group} · 每日精华
> 一句话总结今天讨论的主线 (如有新进展则更新).

## 今日话题
列出**所有**话题 (旧的 + 新增的)。旧话题保持 [T1] [T2] 等原 ID，按需更新一句话描述；新话题用 [T{next_topic_id}]、[T{next_topic_id_plus1}]... 递增 ID。**每条末尾用半角括号附上时间**（旧话题保留原有的时间，或在跨度变化时改成 `(HH:MM-HH:MM)`；新话题用其最早消息的 `[HH:MM]`）：
- [T1] 旧话题标题 (一句话, 如有新进展则更新) `(14:53)`
- [T{next_topic_id}] 新话题标题 (一句话) `(20:10)`

## 🧰 工具与资源
合并旧的 + 新增的资源。每条必须用 Markdown 链接语法可点击；条目末尾用半角括号附上该资源在群里出现时的 `[HH:MM]`：
- [名称](URL) — 分享人 · 简短说明 `(14:53)`

如果完全没有，写「暂无」。

## 💡 主要观点
合并旧的 + 新增的强观点，用 blockquote 引用，注明发言人；每条引用末尾用半角括号附上发言时间：
> 观点内容 — 发言人 `(14:53)`

## 第二部分：META JSON

紧接摘要最后一行，输出严格用下面两行包裹的 **一个** JSON。整段必须可被 `json.loads`：

<<<META>>>
{{
  "topics": [
    {{ "id": "T1", "title": "...", "lines": [<只列新增的 L 行号整数>] }},
    {{ "id": "T{next_topic_id}", "title": "...", "lines": [<新增的 L 行号整数>] }}
  ],
  "shares": [
    {{
      "title": "分享标题",
      "speaker": "分享人昵称",
      "body_md": "完整分享正文 Markdown (与旧版规则一致)",
      "image_refs": ["filename.jpg"],
      "lines": [<新 transcript 中的 L 行号整数>]
    }}
  ]
}}
<<<END>>>

# 规则与判定

- `topics[].lines` 只填**新 transcript 里**的行号 (整数, 去掉 `L` 前缀和前导零)。旧消息的行号不要重复列出——系统会自动把旧的 lines 和你输出的新 lines 拼接。
- 如果某个旧话题这次没有新消息延伸，仍要保留在 `topics` 列表里，但 `lines` 填空数组 `[]`。
- 新话题的 ID 从 `T{next_topic_id}` 开始递增，不要复用任何旧 ID。
- `shares` 只列**新消息**里检测到的全新嘉宾分享 (3 条以上结构化连续发言)。不要重复旧的分享——旧分享会保留原样。如果新消息里没有，`shares` 留空 `[]`。
- 不要在两部分之外有任何额外输出。

# 群名
{group}

# 日期
{date}
"""


def _format_topics_legend(topics: list[dict]) -> str:
    if not topics:
        return "(暂无旧话题)"
    lines: list[str] = []
    for t in topics:
        tid = str(t.get("id", "")).strip() or "T?"
        title = str(t.get("title", "")).strip() or "(无标题)"
        n = len(t.get("lines") or [])
        lines.append(f"- [{tid}] {title}  (已覆盖 {n} 条消息)")
    return "\n".join(lines)


def _next_topic_index(topics: list[dict]) -> int:
    """Return the next available T<n> index, given existing topics."""
    max_n = 0
    for t in topics:
        tid = str(t.get("id", "")).strip()
        if tid.startswith("T") and tid[1:].isdigit():
            max_n = max(max_n, int(tid[1:]))
    return max_n + 1


async def summarize_chat_incremental(
    group_name: str,
    date: str,
    new_messages: list[Message],
    old_summary_md: str,
    old_topics: list[dict],
    next_line: int,
) -> SummaryResult:
    """Incremental variant: re-summarize a group's day given the prior
    summary + only the new messages since it was generated.

    `next_line` is the L<n> number to assign to the first new message — must
    equal (max old line number) + 1 so the merged meta keeps line continuity.
    `old_topics` is a list of {id, title, lines} dicts (from the old meta_json);
    the LLM sees their IDs/titles and reuses them when extending threads.
    """
    transcript, image_map, by_line = _build_transcript(new_messages, start_line=next_line)
    next_topic = _next_topic_index(old_topics)
    prompt = _INCREMENTAL_SUMMARY_PROMPT.format(
        group=group_name,
        date=date,
        old_summary_md=old_summary_md.strip() or "(空)",
        old_topics_legend=_format_topics_legend(old_topics),
        transcript=transcript,
        next_line=next_line,
        next_topic_id=next_topic,
        next_topic_id_plus1=next_topic + 1,
    )
    raw = await _run_claude(prompt)
    summary_md, topics, shares = _parse_meta(raw)
    return SummaryResult(
        summary_md=summary_md,
        topics=topics,
        shares=shares,
        image_paths_by_name=image_map,
        line_to_message=by_line,
    )


_DAILY_DIGEST_PROMPT = """你是「ChatLens」的总编辑。下面给你今天若干微信群已经产出的「每日精华」摘要，按群分节。
请输出**一篇**跨群的「每日精华」Markdown 总览。

# 链接索引（在 Markdown 链接 `[文字](#xxx)` 里只能使用以下短 ID，不要写完整 UUID，也不要造新 ID）

{legend}

# 输出格式（务必严格遵守，直接以下面这一行 H1 开头，无前言、无代码块）

# {date} · 每日精华
> 一句话概括今天跨群讨论的最重要主线。

## 🌐 跨群主线
列 3-5 条今天在多个群里反复出现的话题、情绪或事件，每条一句话。
在每条末尾用括号附上对应的群话题链接（至少 1 个，能跨群最好），格式：
- 一句话主线描述（参见 [群名A·话题标题](#G1.T2)、[群名B·话题标题](#G3.T1)）

## 🔥 各群亮点
按群分小节，**小节标题必须用 `### [群名](#G<i>)` 形式**（链接到该群的当日精华），一两句话总结这群今天最值得一看的内容。
**每个群在本节最多出现一次**——不要把同一个群拆成两段，更不要把同一群名重复列出。如果一个群有多个看点，合并写进同一段。

## 📚 推荐深读
列出今天值得花时间细读的具体分享、文章、工具（仅当真实存在）。优先链接到嘉宾分享，如果是话题就链接到话题，再不行就链接到群精华本身：
- [嘉宾分享标题](#G1.S1) — 来自[《群名》](#G1)
- [话题描述](#G2.T3) — 来自[《群名》](#G2)
- 简短描述 — 来自[《群名》](#G3)

无则写「暂无」。

## ⚠️ 风险与异常
列出今天群里出现的值得警惕的信号或异常（仅当真实出现）。无则写「暂无」。

## 🏷 关键词
8-15 个跨群关键词，逗号分隔。

# 规则
- 客观、紧凑、不写空话；只摘真实出现过的内容。
- 链接的 URL 部分**必须**是 `#G<i>`、`#G<i>.T<n>`、`#G<i>.S<n>` 中的一种，且 ID 必须出现在上方「链接索引」里；不要写完整 UUID。
- 中文输出。
- 不要输出 Markdown 之外的任何文字（无前言、无解释、无代码块）。

# 日期
{date}

# 各群「每日精华」

{bodies}
"""


def _format_digest_legend(payloads: list[dict]) -> str:
    """Render the per-group ID legend the model uses to build Markdown links."""
    lines: list[str] = []
    for i, p in enumerate(payloads, start=1):
        tag = f"G{i}"
        lines.append(f"- **{tag}** = 《{p['name']}》（点 `#{tag}` 跳到该群当日精华）")
        for t in p.get("topics", []):
            tid = t.get("id") or ""
            title = (t.get("title") or "").strip() or "(无标题)"
            if tid:
                lines.append(f"  - `#{tag}.{tid}` = {title}")
        for j, s in enumerate(p.get("shares", []), start=1):
            title = (s.get("title") or "").strip() or "(无标题)"
            speaker = (s.get("speaker") or "").strip()
            tail = f"（主讲：{speaker}）" if speaker else ""
            lines.append(f"  - `#{tag}.S{j}` = 嘉宾分享：{title}{tail}")
    return "\n".join(lines) if lines else "（无可用群精华）"


async def daily_digest(date: str, group_payloads: list[dict]) -> str:
    """`group_payloads` is a list of dicts, one per group, with keys:
        name, body_md, topics: [{id, title}], shares: [{id, title, speaker}]
    The model emits short-token anchors (#G1, #G1.T2, #G1.S1) that the caller
    must resolve to real SPA hashes afterwards."""
    legend = _format_digest_legend(group_payloads)
    bodies = "\n\n".join(
        f"## ===== G{i} · 《{p['name']}》 =====\n\n{(p.get('body_md') or '').strip()}"
        for i, p in enumerate(group_payloads, start=1) if (p.get('body_md') or '').strip()
    )
    prompt = _DAILY_DIGEST_PROMPT.format(date=date, legend=legend, bodies=bodies)
    return await _run_claude(prompt)


def is_configured() -> bool:
    try:
        return bool(_claude_path())
    except RuntimeError:
        return False


def health() -> dict[str, Any]:
    try:
        path = _claude_path()
    except RuntimeError as e:
        return {"configured": False, "error": str(e), "last_error": None, "rate_limited": False, "auth_error": False}
    return {
        "configured": True,
        "claude_path": path,
        "last_error": _last_error,
        "rate_limited": bool(_last_error and _last_error.get("rate_limited")),
        "auth_error": bool(_last_error and _last_error.get("auth_error")),
    }
