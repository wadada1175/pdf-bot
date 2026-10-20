"""研究室プリンター用 Discord Bot。

/print で PDF を受け取り、研究室 LAN 内からしか開けない確認ページで
「印刷する」を押したときだけ印刷する。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import os
import secrets
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv
from pypdf import PdfReader

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
DB_PATH = BASE_DIR / "prints.db"

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ALLOWED_ROLE_ID = int(os.getenv("ALLOWED_ROLE_ID") or 0)
PRINT_CHANNEL_ID = int(os.getenv("PRINT_CHANNEL_ID") or 0)

# `lpstat -p` で表示されるプリンター名（CUPS のキュー名）
PRINTER_NAME = os.environ["PRINTER_NAME"]

LAN_NETWORKS = [
    ipaddress.ip_network(c.strip(), strict=False)
    for c in os.environ["LAN_CIDRS"].split(",")
    if c.strip()
]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "20"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "50"))
MAX_COPIES = int(os.getenv("MAX_COPIES", "5"))
DAILY_PAGE_LIMIT = int(os.getenv("DAILY_PAGE_LIMIT", "100"))
MAX_PENDING_PER_USER = int(os.getenv("MAX_PENDING_PER_USER", "3"))
# Discord の interaction followup は 15 分で失効するので、それより短くする
TOKEN_TTL_SEC = min(int(os.getenv("TOKEN_TTL_MIN", "10")), 14) * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("print-bot")


@dataclass
class Job:
    token: str
    user_id: int
    user_name: str
    filename: str
    path: Path
    pages: int
    copies: int
    duplex: bool
    created_at: float
    interaction: discord.Interaction

    @property
    def total_pages(self) -> int:
        return self.pages * self.copies

    def expired(self) -> bool:
        return time.time() - self.created_at > TOKEN_TTL_SEC


pending_jobs: dict[str, Job] = {}


# ---------- 印刷ログと枚数上限 ----------

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS prints (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                filename TEXT NOT NULL,
                pages INTEGER NOT NULL,
                copies INTEGER NOT NULL,
                duplex INTEGER NOT NULL,
                client_ip TEXT NOT NULL,
                printed_at INTEGER NOT NULL
            )"""
        )


def pages_printed_today(user_id: int) -> int:
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT COALESCE(SUM(pages * copies), 0) FROM prints"
            " WHERE user_id = ? AND printed_at >= ?",
            (user_id, int(today_start.timestamp())),
        ).fetchone()
    return row[0]


def pages_pending(user_id: int) -> int:
    return sum(j.total_pages for j in pending_jobs.values() if j.user_id == user_id)


def record_print(job: Job, client_ip: str) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO prints (user_id, user_name, filename, pages, copies, duplex,"
            " client_ip, printed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job.user_id, job.user_name, job.filename, job.pages, job.copies,
             int(job.duplex), client_ip, int(time.time())),
        )


# ---------- 印刷 ----------

def send_to_printer(job: Job) -> None:
    result = subprocess.run(
        ["lp", "-d", PRINTER_NAME,
         "-n", str(job.copies),
         "-o", "sides=two-sided-long-edge" if job.duplex else "sides=one-sided",
         "-o", "media=A4",
         "-o", "fit-to-page",
         "-t", job.filename,
         str(job.path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"lp failed ({result.returncode}): {result.stderr.strip()}")
    log.info("lp: %s", result.stdout.strip())


def discard_job(job: Job) -> None:
    pending_jobs.pop(job.token, None)
    job.path.unlink(missing_ok=True)


def count_pdf_pages(data: bytes) -> int:
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("パスワード付きの PDF は印刷できません")
    return len(reader.pages)


# ---------- LAN 内だけで開ける確認ページ ----------

def is_lan_client(request: web.Request) -> bool:
    if request.remote is None:
        return False
    try:
        ip = ipaddress.ip_address(request.remote)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in LAN_NETWORKS)


def page(title: str, body: str, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        content_type="text/html",
        charset="utf-8",
        text=f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font-family:sans-serif;max-width:480px;margin:40px auto;padding:0 16px;line-height:1.6}}
button{{font-size:1.1em;padding:10px 24px;cursor:pointer}}</style></head>
<body><h1>{html.escape(title)}</h1>{body}</body></html>""",
    )


def lookup_job(request: web.Request) -> Job | web.Response:
    if not is_lan_client(request):
        log.warning("LAN 外からのアクセスを拒否: %s", request.remote)
        return page("@Sugawara-Lab に接続していません", "<p>@Sugawara-Lab に接続した状態で開いてください。</p>", 403)
    job = pending_jobs.get(request.match_info["token"])
    if job is None or job.expired():
        return page("リンクが無効です", "<p>期限切れか、すでに印刷済みです。もう一度 /print してください。</p>", 404)
    return job


async def confirm_get(request: web.Request) -> web.Response:
    job = lookup_job(request)
    if isinstance(job, web.Response):
        return job
    # GET では印刷しない（リンクのプレビュー取得などで誤って印刷されないように）
    return page("印刷の確認", f"""
<p><b>{html.escape(job.filename)}</b><br>
{job.pages} ページ × {job.copies} 部 / {"両面" if job.duplex else "片面"}<br>
依頼者: {html.escape(job.user_name)}</p>
<form method="post"><button type="submit">印刷する</button></form>""")


async def confirm_post(request: web.Request) -> web.Response:
    job = lookup_job(request)
    if isinstance(job, web.Response):
        return job
    # 先に取り出して二重送信で 2 回印刷されないようにする
    pending_jobs.pop(job.token, None)
    try:
        await asyncio.to_thread(send_to_printer, job)
    except Exception:
        log.exception("印刷に失敗: %s", job.filename)
        job.path.unlink(missing_ok=True)
        await notify(job, f"❌ `{job.filename}` の印刷に失敗しました。管理者に連絡してください。")
        return page("印刷に失敗しました", "<p>管理者に連絡してください。</p>", 500)

    record_print(job, request.remote or "")
    job.path.unlink(missing_ok=True)
    log.info("印刷: %s (%s) %s %dp x%d from %s", job.user_name, job.user_id,
             job.filename, job.pages, job.copies, request.remote)
    await notify(job, f"🖨️ `{job.filename}` をプリンターに送りました（{job.total_pages} ページ）。")
    return page("プリンターに送りました", "<p>この画面は閉じて大丈夫です。</p>")


async def notify(job: Job, message: str) -> None:
    try:
        await job.interaction.followup.send(message, ephemeral=True)
    except discord.HTTPException:
        log.warning("Discord への通知に失敗: %s", job.user_name)


# ---------- Discord ----------

class PrintBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.web_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

        app = web.Application(client_max_size=1024)
        app.router.add_get("/confirm/{token}", confirm_get)
        app.router.add_post("/confirm/{token}", confirm_post)
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        await web.TCPSite(self.web_runner, WEB_HOST, WEB_PORT).start()
        log.info("確認ページ: %s (LAN: %s)", PUBLIC_BASE_URL, ", ".join(map(str, LAN_NETWORKS)))

        self.loop.create_task(self.cleanup_expired())

    async def close(self) -> None:
        if self.web_runner:
            await self.web_runner.cleanup()
        await super().close()

    async def cleanup_expired(self) -> None:
        while not self.is_closed():
            for job in [j for j in pending_jobs.values() if j.expired()]:
                discard_job(job)
                log.info("期限切れで破棄: %s", job.filename)
            await asyncio.sleep(60)


client = PrintBot()


def has_permission(interaction: discord.Interaction) -> str | None:
    """印刷してよければ None、だめなら理由を返す。"""
    if interaction.guild_id != GUILD_ID or not isinstance(interaction.user, discord.Member):
        return "研究室のサーバー内で使ってください。"
    if PRINT_CHANNEL_ID and interaction.channel_id != PRINT_CHANNEL_ID:
        return f"<#{PRINT_CHANNEL_ID}> で使ってください。"
    if ALLOWED_ROLE_ID and not any(r.id == ALLOWED_ROLE_ID for r in interaction.user.roles):
        return "印刷できるロールを持っていません。"
    return None


@client.tree.command(name="print", description="PDF を研究室のプリンターで印刷します")
@app_commands.describe(file="印刷する PDF", copies="部数")
async def print_command(
    interaction: discord.Interaction,
    file: discord.Attachment,
    copies: app_commands.Range[int, 1, MAX_COPIES] = 1,
) -> None:
    if reason := has_permission(interaction):
        await interaction.response.send_message(reason, ephemeral=True)
        return
    if not file.filename.lower().endswith(".pdf"):
        await interaction.response.send_message("PDF ファイルを添付してください。", ephemeral=True)
        return
    if file.size > MAX_FILE_MB * 1024 * 1024:
        await interaction.response.send_message(f"{MAX_FILE_MB}MB までの PDF にしてください。", ephemeral=True)
        return
    user_pending = [j for j in pending_jobs.values() if j.user_id == interaction.user.id]
    if len(user_pending) >= MAX_PENDING_PER_USER:
        await interaction.response.send_message(
            "確認待ちの印刷が多すぎます。先に確認リンクを開くか、期限切れを待ってください。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    data = await file.read()
    if not data.startswith(b"%PDF"):
        await interaction.followup.send("PDF として読めないファイルです。", ephemeral=True)
        return
    try:
        pages = await asyncio.to_thread(count_pdf_pages, data)
    except Exception as e:
        msg = str(e) if isinstance(e, ValueError) else "PDF を読み込めませんでした。"
        await interaction.followup.send(msg, ephemeral=True)
        return
    if pages > MAX_PAGES:
        await interaction.followup.send(f"{MAX_PAGES} ページまでの PDF にしてください（{pages} ページ）。", ephemeral=True)
        return

    used = pages_printed_today(interaction.user.id) + pages_pending(interaction.user.id)
    if used + pages * copies > DAILY_PAGE_LIMIT:
        await interaction.followup.send(
            f"1 日の上限 {DAILY_PAGE_LIMIT} ページを超えます（今日の使用・予約: {used} ページ）。", ephemeral=True)
        return

    token = secrets.token_urlsafe(24)
    path = JOBS_DIR / f"{token}.pdf"
    path.write_bytes(data)
    pending_jobs[token] = Job(
        token=token,
        user_id=interaction.user.id,
        user_name=interaction.user.display_name,
        filename=file.filename,
        path=path,
        pages=pages,
        copies=copies,
        duplex=False,
        created_at=time.time(),
        interaction=interaction,
    )
    await interaction.followup.send(
        f"📄 `{file.filename}`（{pages} ページ × {copies} 部・片面）\n"
        f"@Sugawara-Lab に接続した状態で、{TOKEN_TTL_SEC // 60} 分以内に次のリンクを開いて「印刷する」を押してください。\n"
        f"{PUBLIC_BASE_URL}/confirm/{token}",
        ephemeral=True,
    )


@client.tree.command(name="quota", description="今日の残り印刷ページ数を確認します")
async def quota_command(interaction: discord.Interaction) -> None:
    used = pages_printed_today(interaction.user.id)
    await interaction.response.send_message(
        f"今日は {used} ページ印刷済みです（残り {max(DAILY_PAGE_LIMIT - used, 0)} ページ）。", ephemeral=True)


def main() -> None:
    JOBS_DIR.mkdir(exist_ok=True)
    # 前回の起動で残った PDF は確認待ちの情報が失われているので消す
    for leftover in JOBS_DIR.glob("*.pdf"):
        leftover.unlink()
    init_db()
    client.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
