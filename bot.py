# bot.py
# Single-event bot for "Shadowfront" — global app commands, minimal buttons, manager/admin slash commands
# This version improves mention reliability on mobile by:
#  - Enabling a larger member cache (MemberCacheFlags.all()).
#  - Warming the guild member cache (guild.chunk / query_members) before rendering the roster.

import os
import sqlite3
import time
import io
from contextlib import contextmanager
from typing import Optional, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

# ---------- Configuration ----------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Please set DISCORD_TOKEN environment variable.")

INTENTS = discord.Intents.default()
INTENTS.members = True  # required for member cache & resolution

# Prefer a mounted volume on Railway if available; otherwise fall back
DEFAULT_DB = "guild_teams_new.db"
DB_PATH = os.getenv("DB_PATH")
if not DB_PATH:
    DB_PATH = "/data/shadowfront.db" if os.path.isdir("/data") else DEFAULT_DB
# Control global sync on startup (default true)
SYNC_ON_STARTUP = os.getenv("SYNC_ON_STARTUP", "true").lower() in ("1", "true", "yes")

# Use a larger member cache so get_member is reliable
MEMBER_CACHE_FLAGS = discord.MemberCacheFlags.all()

bot = commands.Bot(
    command_prefix=None,
    intents=INTENTS,
    member_cache_flags=MEMBER_CACHE_FLAGS,
    help_command=None
)

tree = bot.tree

FIXED_EVENT_NAME = "Shadowfront"

# ---------- Database ----------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,                  -- always 'Shadowfront'
            team_size INTEGER NOT NULL DEFAULT 20,
            backup_size INTEGER NOT NULL DEFAULT 10,
            teams INTEGER NOT NULL DEFAULT 2,    -- 1 or 2
            status TEXT NOT NULL DEFAULT 'open', -- open|locked|closed
            created_by INTEGER NOT NULL,
            display_channel_id INTEGER,
            display_message_id INTEGER,
            team_a_label TEXT,
            team_b_label TEXT,
            team_a_slot TEXT,                    -- '0900'|'1800'|'2300' or NULL
            team_b_slot TEXT,
            squad_a_size INTEGER DEFAULT 15,
            squad_b_size INTEGER DEFAULT 5,
            squad_a_commander_quota INTEGER DEFAULT 2,
            squad_b_commander_quota INTEGER DEFAULT 1,
            auto_refresh_enabled INTEGER DEFAULT 1,
            auto_refresh_day TEXT DEFAULT 'MON',
            auto_refresh_hour INTEGER DEFAULT 9,
            auto_refresh_tz TEXT DEFAULT 'Australia/Brisbane',
            auto_refresh_last_epoch INTEGER,
            UNIQUE(guild_id, name)
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS rosters(
            event_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            team TEXT NOT NULL,      -- 'A' or 'B'
            squad TEXT,              -- 'SA' or 'SB' for mains; NULL for backups
            slot_type TEXT NOT NULL, -- 'main' or 'backup'
            is_commander INTEGER NOT NULL DEFAULT 0,
            joined_at INTEGER NOT NULL,
            PRIMARY KEY(event_id, user_id),
            FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS managers(
            event_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY(event_id, user_id),
            FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
        );
        """)


def ensure_fixed_event(conn: sqlite3.Connection, guild_id: int, creator_id: int) -> sqlite3.Row:
    """Create or fetch the single 'Shadowfront' event for this guild."""
    c = conn.cursor()
    c.execute("SELECT * FROM events WHERE guild_id=? AND name=?", (guild_id, FIXED_EVENT_NAME))
    row = c.fetchone()
    if row:
        return row
    # Defaults: two teams; Squad A 15 (2 cmdrs), Squad B 5 (1 cmdr), backups 10
    c.execute(
        """
        INSERT INTO events(
            guild_id, name, team_size, backup_size, teams, status,
            created_by, display_channel_id, display_message_id,
            team_a_label, team_b_label,
            team_a_slot, team_b_slot,
            squad_a_size, squad_b_size, squad_a_commander_quota, squad_b_commander_quota,
            auto_refresh_enabled, auto_refresh_day, auto_refresh_hour, auto_refresh_tz
        )
        VALUES (?,?,?,?,?, 'open', ?, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, 1, 'MON', 9, 'Australia/Brisbane')
        """,
        (
            guild_id, FIXED_EVENT_NAME, 20, 10, 2,
            creator_id, "Shadowfront Team 1", "Shadowfront Team 2",
            15, 5, 2, 1
        )
    )
    event_id = c.lastrowid
    c.execute("INSERT INTO managers(event_id, user_id) VALUES (?,?)", (event_id, creator_id))
    c.execute("SELECT * FROM events WHERE id=?", (event_id,))
    return c.fetchone()


def get_fixed_event(conn: sqlite3.Connection, guild_id: int) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute("SELECT * FROM events WHERE guild_id=? AND name=?", (guild_id, FIXED_EVENT_NAME))
    return c.fetchone()


def is_manager(conn: sqlite3.Connection, ev_id: int, user_id: int) -> bool:
    c = conn.cursor()
    c.execute("SELECT 1 FROM managers WHERE event_id=? AND user_id=?", (ev_id, user_id))
    return c.fetchone() is not None


def user_enrollment(conn, event_id: int, user_id: int) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute("SELECT * FROM rosters WHERE event_id=? AND user_id=?", (event_id, user_id))
    return c.fetchone()


def count_mains(conn, event_id: int, team: str, squad: Optional[str] = None, *, commanders_only: bool = False, non_commanders_only: bool = False) -> int:
    c = conn.cursor()
    where = "slot_type='main' AND team=? AND event_id=?"
    params = [team, event_id]
    if squad:
        where += " AND squad=?"; params.append(squad)
    if commanders_only:
        where += " AND is_commander=1"
    if non_commanders_only:
        where += " AND is_commander=0"
    c.execute(f"SELECT COUNT(*) FROM rosters WHERE {where}", params)
    return c.fetchone()[0]


def count_backups(conn, event_id: int, team: str) -> int:
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM rosters WHERE slot_type='backup' AND team=? AND event_id=?", (team, event_id))
    return c.fetchone()[0]


def get_team_counts(conn, ev: sqlite3.Row, team: str):
    commanders_sa = count_mains(conn, ev["id"], team, "SA", commanders_only=True)
    mains_sa = count_mains(conn, ev["id"], team, "SA", non_commanders_only=True)
    commanders_sb = count_mains(conn, ev["id"], team, "SB", commanders_only=True)
    mains_sb = count_mains(conn, ev["id"], team, "SB", non_commanders_only=True)
    backups = count_backups(conn, ev["id"], team)
    return (commanders_sa, mains_sa, commanders_sb, mains_sb, backups)


def non_commander_cap(ev: sqlite3.Row, squad_code: str) -> int:
    if squad_code == "SA":
        return max(0, int(ev["squad_a_size"]) - int(ev["squad_a_commander_quota"]))
    else:
        return max(0, int(ev["squad_b_size"]) - int(ev["squad_b_commander_quota"]))

# ---------- Time utilities ----------
FIXED_SLOTS = {"0900": (9, 0), "1800": (18, 0), "2300": (23, 0)}


def next_epoch_for_slot(slot: Optional[str]) -> Optional[int]:
    if not slot or slot not in FIXED_SLOTS:
        return None
    h, m = FIXED_SLOTS[slot]
    now_utc = datetime.now(timezone.utc)
    # Next Friday (UTC)
    days_ahead = (4 - now_utc.weekday()) % 7
    target = now_utc.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_ahead)
    if target <= now_utc:
        target += timedelta(days=7)
    return int(target.timestamp())


def embed_time_for_team(ev: sqlite3.Row, team: str) -> str:
    slot = ev["team_a_slot"] if team == "A" else ev["team_b_slot"]
    epoch = next_epoch_for_slot(slot)
    return f"<t:{epoch}:F> (<t:{epoch}:R>)" if epoch else "_Not set_"


def event_tz(ev: sqlite3.Row):
    tzname = ev["auto_refresh_tz"] or "Australia/Brisbane"
    try:
        return ZoneInfo(tzname) if ZoneInfo else timezone.utc
    except Exception:
        return timezone.utc


def local_hhmm_no_colon(ev: sqlite3.Row, slot: Optional[str]) -> str:
    epoch = next_epoch_for_slot(slot)
    if not epoch:
        return "----"
    dt = datetime.fromtimestamp(epoch, tz=event_tz(ev))
    return dt.strftime("%H%M")

# Number of squads configured for this event (1 or 2; default 2)

def event_squads(ev: sqlite3.Row) -> int:
    return int(ev["teams"]) if (ev and ev.get("teams") is not None) else 2


def button_dual_time_label(ev: sqlite3.Row, team: str) -> str:
    slot = ev["team_a_slot"] if team == "A" else ev["team_b_slot"]
    return f"(UTC {slot if slot else '----'})"

# ---------- Warm member cache ----------
async def warm_cache_for_guild(guild: discord.Guild):
    """Attempt to ensure rostered members are present in cache so mentions render nicely on mobile."""
    if not guild:
        return
    # 1) Chunk guild to pull members via gateway (fills cache)
    try:
        await guild.chunk()
    except Exception:
        pass
    # 2) Query roster IDs for this guild
    with db() as conn:
        rows = conn.execute(
            "SELECT r.user_id FROM rosters r JOIN events e ON r.event_id=e.id WHERE e.guild_id=?",
            (guild.id,)
        ).fetchall()
        roster_ids = [int(r[0]) for r in rows]
    if not roster_ids:
        return
    # 3) Find IDs missing from cache and query them in bulk (gateway op)
    missing = [uid for uid in roster_ids if guild.get_member(uid) is None]
    if missing:
        try:
            await guild.query_members(user_ids=missing, presences=False, cache=True)
        except Exception:
            # Ignore; we'll still fall back to raw <@id> if needed
            pass

# ---------- Roster logic ----------

def team_label(ev: sqlite3.Row, team: str) -> str:
    return (ev["team_a_label"] or "Shadowfront Team 1") if team == "A" else (ev["team_b_label"] or "Shadowfront Team 2")


def add_participant(conn, ev: sqlite3.Row, user_id: int, team: str, squad: Optional[str] = None, force_backup: bool = False) -> Tuple[str, str]:
    if ev["status"] != "open":
        return ("", "This event is currently locked.")

    existing = user_enrollment(conn, ev["id"], user_id)
    if existing:
        if existing["team"] == team:
            if existing["slot_type"] == "main":
                loc = f"{team_label(ev, team)} — {'Squad A' if existing['squad']=='SA' else 'Squad B'}"
            else:
                loc = f"{team_label(ev, team)} (backup)"
            return (existing["slot_type"], f"You are already on {loc}.")
        else:
            return ("", f"You are already registered on {team_label(ev, existing['team'])}. Leave first with /leave.")

    _, mains_sa, _, mains_sb, backups = get_team_counts(conn, ev, team)

    def can_join_non_cmd(sq: str) -> bool:
        return count_mains(conn, ev["id"], team, sq, non_commanders_only=True) < non_commander_cap(ev, sq)

    c = conn.cursor()
    if force_backup:
        if backups < ev["backup_size"]:
            c.execute(
                "INSERT INTO rosters(event_id,user_id,team,squad,slot_type,is_commander,joined_at) VALUES (?,?,?,?,?,?,?)",
                (ev["id"], user_id, team, None, "backup", 0, int(time.time()))
            )
            return ("backup", "joined")
        return ("", f"{team_label(ev, team)} backups are full.")

    # If a specific squad was requested and space exists, use it
    if squad in ("SA", "SB"):
        if can_join_non_cmd(squad):
            c.execute(
                "INSERT INTO rosters(event_id,user_id,team,squad,slot_type,is_commander,joined_at) VALUES (?,?,?,?,?,?,?)",
                (ev["id"], user_id, team, squad, "main", 0, int(time.time()))
            )
            return ("main", "joined")
        # fall through to auto path if requested squad is full

    # Auto path: Squad A → (Squad B if enabled) → backup
    if can_join_non_cmd("SA"):
        c.execute(
            "INSERT INTO rosters(event_id,user_id,team,squad,slot_type,is_commander,joined_at) VALUES (?,?,?,?,?,?,?)",
            (ev["id"], user_id, team, "SA", "main", 0, int(time.time()))
        )
        return ("main", "joined")
    if (ev.get("teams") or 2) >= 2 and can_join_non_cmd("SB"):
        c.execute(
            "INSERT INTO rosters(event_id,user_id,team,squad,slot_type,is_commander,joined_at) VALUES (?,?,?,?,?,?,?)",
            (ev["id"], user_id, team, "SB", "main", 0, int(time.time()))
        )
        return ("main", "joined")
    if backups < ev["backup_size"]:
        c.execute(
            "INSERT INTO rosters(event_id,user_id,team,squad,slot_type,is_commander,joined_at) VALUES (?,?,?,?,?,?,?)",
            (ev["id"], user_id, team, None, "backup", 0, int(time.time()))
        )
        return ("backup", "joined")
    return ("", f"{team_label(ev, team)} is full (mains and backups).")


def promote_one_non_commander(conn, ev: sqlite3.Row, team: str, squad: str) -> Optional[int]:
    # Used when a main leaves to auto-promote from backups
    current_mains = count_mains(conn, ev["id"], team, squad, non_commanders_only=True)
    if current_mains >= non_commander_cap(ev, squad):
        return None
    c = conn.cursor()
    c.execute(
        """
        SELECT user_id FROM rosters
        WHERE event_id=? AND team=? AND slot_type='backup'
        ORDER BY joined_at ASC LIMIT 1
        """,
        (ev["id"], team)
    )
    row = c.fetchone()
    if not row:
        return None
    uid = row["user_id"]
    c.execute(
        "UPDATE rosters SET slot_type='main', is_commander=0, squad=? WHERE event_id=? AND user_id=?",
        (squad, ev["id"], uid)
    )
    return uid


def get_roster(conn, event_id: int, team: str):
    c = conn.cursor()
    # SA commanders + mains
    c.execute("SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND squad='SA' AND is_commander=1 ORDER BY joined_at ASC", (event_id, team))
    commanders_sa = [r[0] for r in c.fetchall()]
    c.execute("SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND squad='SA' AND is_commander=0 ORDER BY joined_at ASC", (event_id, team))
    mains_sa = [r[0] for r in c.fetchall()]
    # SB commanders + mains
    c.execute("SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND squad='SB' AND is_commander=1 ORDER BY joined_at ASC", (event_id, team))
    commanders_sb = [r[0] for r in c.fetchall()]
    c.execute("SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND squad='SB' AND is_commander=0 ORDER BY joined_at ASC", (event_id, team))
    mains_sb = [r[0] for r in c.fetchall()]
    # backups
    c.execute("SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='backup' ORDER BY joined_at ASC", (event_id, team))
    backups = [r[0] for r in c.fetchall()]
    return commanders_sa, mains_sa, commanders_sb, mains_sb, backups


def user_is_event_manager_or_admin(ev: sqlite3.Row, member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    with db() as conn:
        return is_manager(conn, ev["id"], member.id)


# ---------- Mentions helper ----------

def mentions_from_ids(guild: discord.Guild, uids: List[int]) -> str:
    """Return a newline-separated list of mentions where possible; raw <@id> as fallback."""
    if not uids:
        return "*None*"
    seen = set()
    mentions = []
    for uid in uids:
        if uid in seen:
            continue
        seen.add(uid)
        m = guild.get_member(uid)
        mentions.append(m.mention if m else f"<@{uid}>")
    return "\n".join(mentions) if mentions else "*None*"


# ---------- Embed ----------
async def roster_embed(ev: sqlite3.Row, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title=f"Event: {FIXED_EVENT_NAME}",
        color=discord.Color.blurple()
    )
    with db() as conn:
        for team in ["A", "B"][: ev["teams"]]:
            label = team_label(ev, team)
            embed.add_field(name=f"{label} — Time (UTC slot)", value=embed_time_for_team(ev, team), inline=False)
            commanders_sa, mains_sa, commanders_sb, mains_sb, backups = get_roster(conn, ev["id"], team)

            embed.add_field(
                name=f"{label} — Squad A — Commanders ({len(commanders_sa)}/{ev['squad_a_commander_quota']})",
                value=mentions_from_ids(guild, commanders_sa), inline=True
            )
            embed.add_field(
                name=f"{label} — Squad A — Mains ({len(mains_sa)}/{non_commander_cap(ev, 'SA')})",
                value=mentions_from_ids(guild, mains_sa), inline=True
            )
            embed.add_field(name="\u200b", value="\u200b", inline=False)

            # Only if 2 teams configured per squad
            if (ev.get("teams") or 2) >= 1:  # teams refers to Team 1/2; squads are fixed A/B in this build
                embed.add_field(
                    name=f"{label} — Squad B — Commanders ({len(commanders_sb)}/{ev['squad_b_commander_quota']})",
                    value=mentions_from_ids(guild, commanders_sb), inline=True
                )
                embed.add_field(
                    name=f"{label} — Squad B — Mains ({len(mains_sb)}/{non_commander_cap(ev, 'SB')})",
                    value=mentions_from_ids(guild, mains_sb), inline=True
                )
                embed.add_field(name="\u200b", value="\u200b", inline=False)

            embed.add_field(
                name=f"{label} — Backups ({len(backups)}/{ev['backup_size']})",
                value=mentions_from_ids(guild, backups), inline=False
            )
            embed.add_field(name="\u200b", value="\u200b", inline=False)
    return embed


# ---------- Buttons (reduced UI) ----------
class RosterView(discord.ui.View):
    """Only: Team 1 join, Team 2 join (if 2 teams), and Leave."""
    def __init__(self, ev: sqlite3.Row):
        super().__init__(timeout=None)
        self.teams = int(ev["teams"] or 2)
        self.ev = ev
        self._add_button(f"Team 1 {button_dual_time_label(ev, 'A')}", discord.ButtonStyle.primary, 0, lambda i: self._join_auto(i, "A"))
        if self.teams >= 2:
            self._add_button(f"Team 2 {button_dual_time_label(ev, 'B')}", discord.ButtonStyle.primary, 0, lambda i: self._join_auto(i, "B"))
        self._add_button("Leave", discord.ButtonStyle.danger, 1, self._leave_common)

    def _add_button(self, label: str, style: discord.ButtonStyle, row: int, handler):
        b = discord.ui.Button(label=label, style=style, row=row)
        async def cb(i: discord.Interaction):
            await handler(i)
        b.callback = cb
        self.add_item(b)

    async def _join_auto(self, interaction: discord.Interaction, team: str):
        with db() as conn:
            ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
            slot_type, note = add_participant(conn, ev, interaction.user.id, team, None, False)
            if not slot_type:
                await interaction.response.send_message(note, ephemeral=True); return
        await refresh_roster_message(interaction.guild)
        if slot_type == "backup":
            await interaction.response.send_message(f"Joined **{team_label(ev, team)}** as **backup**.", ephemeral=True)
        else:
            with db() as conn:
                rec = user_enrollment(conn, ev["id"], interaction.user.id)
                sq = rec["squad"] if rec else "SA"
            await interaction.response.send_message(
                f"Joined **{team_label(ev, team)} — {'Squad A' if sq=='SA' else 'Squad B'}** as **main**.",
                ephemeral=True
            )

    async def _leave_common(self, interaction: discord.Interaction):
        promoted_user_id = None
        with db() as conn:
            ev = get_fixed_event(conn, interaction.guild_id)
            if not ev:
                await interaction.response.send_message("Event not found.", ephemeral=True); return
            c = conn.cursor()
            c.execute("SELECT * FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], interaction.user.id))
            prior = c.fetchone()
            if not prior:
                await interaction.response.send_message("You are not registered for this event.", ephemeral=True)
                return
            c.execute("DELETE FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], interaction.user.id))
            if prior["slot_type"] == "main" and prior["is_commander"] == 0 and prior["squad"] in ("SA","SB"):
                promoted_user_id = promote_one_non_commander(conn, ev, prior["team"], prior["squad"])
        await refresh_roster_message(interaction.guild)
        msg = "You have left the event."
        if promoted_user_id:
            m = interaction.guild.get_member(promoted_user_id)
            msg += f" Promoted {m.mention if m else f'<@{promoted_user_id}>'} to main."
        await interaction.response.send_message(msg, ephemeral=True)


# ---------- Live message helpers ----------
async def ensure_roster_message(ev: sqlite3.Row, guild: discord.Guild) -> Optional[discord.Message]:
    channel_id = ev["display_channel_id"]
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if not channel:
        return None
    message_id = ev["display_message_id"]
    msg: Optional[discord.Message] = None
    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            msg = None

    # Warm cache before building the embed so mentions resolve better
    await warm_cache_for_guild(guild)

    embed = await roster_embed(ev, guild)
    view = RosterView(ev)
    if msg is None:
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return None
        with db() as conn:
            conn.execute("UPDATE events SET display_message_id=? WHERE id=?", (msg.id, ev["id"]))
    else:
        try:
            await msg.edit(embed=embed, view=view)
        except discord.Forbidden:
            return None
    return msg


async def refresh_roster_message(guild: discord.Guild):
    with db() as conn:
        ev = get_fixed_event(conn, guild.id)
        if not ev:
            return
    await ensure_roster_message(ev, guild)


# ---------- Startup ----------
@bot.event
async def on_ready():
    init_db()
    # Ensure the single event exists for every guild the bot is in
    with db() as conn:
        for g in bot.guilds:
            ensure_fixed_event(conn, g.id, bot.user.id)

    try:
        # Log commands in memory
        names = [c.name for c in tree.get_commands()]
        print(f"Loaded {len(names)} commands: {names}")

        # --- GLOBAL SYNC ---
        if SYNC_ON_STARTUP:
            synced = await tree.sync()  # publish globally
            print(f"[GLOBAL] Published {len(synced)} commands globally.")
        else:
            print("[GLOBAL] Skipping global sync on startup (SYNC_ON_STARTUP=false).")

        print(f"Using DB at: {os.path.abspath(DB_PATH)}")
        print(f"Intents.Members enabled: {bot.intents.members}")

        # Warm cache and attach/refresh views (live roster message)
        for g in bot.guilds:
            await warm_cache_for_guild(g)
            with db() as conn:
                ev = get_fixed_event(conn, g.id)
            if ev:
                await ensure_roster_message(ev, g)

        # Start weekly refresh loop
        if not weekly_refresh_task.is_running():
            weekly_refresh_task.start()
        print("Startup complete.")
    except Exception as e:
        print("Startup error:", e)

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


# ---------- Weekly auto-refresh ----------

def map_weekday_name(dt: datetime) -> str:
    return ["MON","TUE","WED","THU","FRI","SAT","SUN"][dt.weekday()]


@tasks.loop(minutes=10)
async def weekly_refresh_task():
    for g in bot.guilds:
        with db() as conn:
            ev = get_fixed_event(conn, g.id)
            if not ev or not ev["auto_refresh_enabled"]:
                continue
            tzname = ev["auto_refresh_tz"] or "Australia/Brisbane"
            try:
                tz = ZoneInfo(tzname) if ZoneInfo else timezone.utc
            except Exception:
                tz = timezone.utc
            now_local = datetime.now(tz)
            if map_weekday_name(now_local) != (ev["auto_refresh_day"] or "MON").upper():
                continue
            if now_local.hour != int(ev["auto_refresh_hour"] or 9):
                continue
            start_of_hour = int(now_local.replace(minute=0, second=0, microsecond=0).timestamp())
            last = int(ev["auto_refresh_last_epoch"] or 0)
            if last >= start_of_hour:
                continue
            try:
                # Warm cache just before refresh
                await warm_cache_for_guild(g)
                await refresh_roster_message(g)
                with db() as conn2:
                    conn2.execute("UPDATE events SET auto_refresh_last_epoch=? WHERE id=?", (start_of_hour, ev["id"]))
            except Exception as e:
                print(f"Auto-refresh failed in guild {g.id}: {e}")


# ---------- Slash Commands ----------
class TeamChoice(app_commands.Transformer):
    async def transform(self, interaction: discord.Interaction, value: str) -> str:
        v = value.upper()
        if v not in ("A", "B"):
            raise app_commands.AppCommandError("Team must be A or B.")
        return v


class SquadChoice(app_commands.Transformer):
    async def transform(self, interaction: discord.Interaction, value: str) -> str:
        v = value.upper()
        if v not in ("A", "B"):
            raise app_commands.AppCommandError("Squad must be A or B.")
        return "SA" if v == "A" else "SB"


@tree.command(description="Set the roster display channel (manager only).")
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        conn.execute("UPDATE events SET display_channel_id=?, display_message_id=NULL WHERE id=?", (channel.id, ev["id"]))
        ev = get_fixed_event(conn, interaction.guild_id)
    await warm_cache_for_guild(interaction.guild)
    await ensure_roster_message(ev, interaction.guild)
    await interaction.response.send_message(f"Display channel set to {channel.mention}.", ephemeral=True)


@tree.command(description="Set the time slot for Team 1 or Team 2 (choose 09:00, 18:00, or 23:00 UTC).")
@app_commands.describe(team="A or B (A = Team 1, B = Team 2)", slot="One of 09:00, 18:00, 23:00 UTC")
@app_commands.choices(slot=[
    app_commands.Choice(name="09:00 UTC", value="0900"),
    app_commands.Choice(name="18:00 UTC", value="1800"),
    app_commands.Choice(name="23:00 UTC", value="2300"),
])
async def setteamtime(interaction: discord.Interaction, team: app_commands.Transform[str, TeamChoice], slot: str):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        if team == "A":
            conn.execute("UPDATE events SET team_a_slot=? WHERE id=?", (slot, ev["id"]))
        else:
            conn.execute("UPDATE events SET team_b_slot=? WHERE id=?", (slot, ev["id"]))
    await warm_cache_for_guild(interaction.guild)
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(f"Set **{team_label(ev, team)}** time to **{slot} UTC**.", ephemeral=True)


@tree.command(description="Lock Shadowfront to stop new signups (manager only).")
async def lock(interaction: discord.Interaction):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True); return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        conn.execute("UPDATE events SET status='locked' WHERE id=?", (ev["id"],))
    await warm_cache_for_guild(interaction.guild)
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message("Event locked. Roster updated.", ephemeral=True)


@tree.command(description="Unlock Shadowfront to allow signups again (manager only).")
async def unlock(interaction: discord.Interaction):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True); return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        conn.execute("UPDATE events SET status='open' WHERE id=?", (ev["id"],))
    await warm_cache_for_guild(interaction.guild)
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message("Event unlocked. Roster updated.", ephemeral=True)


@tree.command(description="Reset Shadowfront: clears all mains/backups and re-opens signups (manager only).")
async def reset(interaction: discord.Interaction, clear_message: bool = False):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True); return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        conn.execute("DELETE FROM rosters WHERE event_id=?", (ev["id"],))
        conn.execute("UPDATE events SET status='open' WHERE id=?", (ev["id"],))
        if clear_message and ev["display_channel_id"] and ev["display_message_id"]:
            channel = interaction.guild.get_channel(ev["display_channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(ev["display_message_id"])
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
            conn.execute("UPDATE events SET display_message_id=NULL WHERE id=?", (ev["id"],))
    await warm_cache_for_guild(interaction.guild)
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message("Event reset. Live roster updated.", ephemeral=True)


@tree.command(description="Promote earliest team backup to a squad's main (manager only, non-commander).")
async def promote(interaction: discord.Interaction, team: app_commands.Transform[str, TeamChoice], squad: app_commands.Transform[str, SquadChoice]):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True); return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        uid = promote_one_non_commander(conn, ev, team, squad)
    if not uid:
        await interaction.response.send_message(
            f"No backups to promote or squad mains are at capacity for {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}.",
            ephemeral=True
        )
        return
    await warm_cache_for_guild(interaction.guild)
    await refresh_roster_message(interaction.guild)
    member = interaction.guild.get_member(uid)
    await interaction.response.send_message(
        f"Promoted {member.mention if member else f'<@{uid}>'} to main (non-commander) on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}.",
        ephemeral=True
    )


@tree.command(description="Assign a commander to a team & squad (manager only).")
async def setcommander(
    interaction: discord.Interaction,
    team: app_commands.Transform[str, TeamChoice],
    squad: app_commands.Transform[str, SquadChoice],
    user: discord.Member
):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True); return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        commanders_sa, mains_sa, commanders_sb, mains_sb, _ = get_team_counts(conn, ev, team)
        if squad == "SA":
            if commanders_sa >= int(ev["squad_a_commander_quota"] or 0):
                await interaction.response.send_message(f"{team_label(ev, team)} — Squad A already has the maximum of {ev['squad_a_commander_quota']} commanders.", ephemeral=True); return
            if (commanders_sa + mains_sa) >= int(ev["squad_a_size"]):
                await interaction.response.send_message(f"{team_label(ev, team)} — Squad A is at full capacity ({ev['squad_a_size']}).", ephemeral=True); return
        else:
            if commanders_sb >= int(ev["squad_b_commander_quota"] or 0):
                await interaction.response.send_message(f"{team_label(ev, team)} — Squad B already has the maximum of {ev['squad_b_commander_quota']} commanders.", ephemeral=True); return
            if (commanders_sb + mains_sb) >= int(ev["squad_b_size"]):
                await interaction.response.send_message(f"{team_label(ev, team)} — Squad B is at full capacity ({ev['squad_b_size']}).", ephemeral=True); return
        existing = user_enrollment(conn, ev["id"], user.id)
        c = conn.cursor()
        if existing:
            if existing["team"] != team:
                await interaction.response.send_message(f"{user.mention} is registered on {team_label(ev, existing['team'])}. Ask them to /leave first.", ephemeral=True); return
            if existing["slot_type"] == "backup":
                c.execute("UPDATE rosters SET slot_type='main', squad=?, is_commander=1 WHERE event_id=? AND user_id=?", (squad, ev["id"], user.id))
                action = f"Promoted {user.mention} from backup to **commander** on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}."
            else:
                if existing["is_commander"] == 1 and existing["squad"] == squad:
                    await interaction.response.send_message(f"{user.mention} is already a commander on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B' }.", ephemeral=True); return
                c.execute("UPDATE rosters SET is_commander=1, squad=? WHERE event_id=? AND user_id=?", (squad, ev["id"], user.id))
                action = f"Set {user.mention} as **commander** on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}."
        else:
            c.execute(
                "INSERT INTO rosters(event_id, user_id, team, squad, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?,?)",
                (ev["id"], user.id, team, squad, "main", 1, int(time.time()))
            )
            action = f"Added {user.mention} as **commander** on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}."
    await warm_cache_for_guild(interaction.guild)
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(action + " Live roster updated.", ephemeral=True)


@tree.command(description="Remove commander status (manager only). Optionally demote to backup.")
async def unsetcommander(
    interaction: discord.Interaction,
    team: app_commands.Transform[str, TeamChoice],
    user: discord.Member,
    demote_if_needed: bool = True
):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True); return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        existing = user_enrollment(conn, ev["id"], user.id)
        if not existing or existing["team"] != team or existing["is_commander"] != 1 or existing["slot_type"] != "main":
            await interaction.response.send_message(f"{user.mention} is not a main commander on {team_label(ev, team)}.", ephemeral=True); return
        squad = existing["squad"] or "SA"
        current_non_cmd = count_mains(conn, ev["id"], team, squad, non_commanders_only=True)
        c = conn.cursor()
        if current_non_cmd + 1 <= non_commander_cap(ev, squad):
            c.execute("UPDATE rosters SET is_commander=0 WHERE event_id=? AND user_id=?", (ev["id"], user.id))
            action = f"Unset commander: {user.mention} is now a normal **main** on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}."
        else:
            if demote_if_needed:
                backups = count_backups(conn, ev["id"], team)
                if backups < ev["backup_size"]:
                    c.execute("UPDATE rosters SET is_commander=0, squad=NULL, slot_type='backup' WHERE event_id=? AND user_id=?", (ev["id"], user.id))
                    action = f"Unset commander and **demoted to backup** (squad mains full) for {user.mention} on {team_label(ev, team)}."
                else:
                    await interaction.response.send_message(
                        "Cannot unset: squad non-commander mains are full and backups are also full. Free a slot or disable demote_if_needed.",
                        ephemeral=True
                    ); return
            else:
                await interaction.response.send_message(
                    "Cannot unset: squad non-commander mains are full. Enable demote_if_needed or free a main slot.",
                    ephemeral=True
                ); return
    await warm_cache_for_guild(interaction.guild)
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(action + " Live roster updated.", ephemeral=True)


# ---- Player actions ----
@tree.command(description="Join Shadowfront (auto: Squad A → Squad B → backup).")
async def join(
    interaction: discord.Interaction,
    team: app_commands.Transform[str, TeamChoice],
    squad: Optional[str] = None
):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        requested_squad = squad if squad in ("SA", "SB") else None
        slot_type, note = add_participant(conn, ev, interaction.user.id, team, requested_squad, False)
    if not slot_type:
        await interaction.response.send_message(note, ephemeral=True); return
    await warm_cache_for_guild(interaction.guild)
    await refresh_roster_message(interaction.guild)
    if slot_type == "backup":
        await interaction.response.send_message(f"You joined **{team_label(ev, team)}** as **backup**.", ephemeral=True)
    else:
        with db() as conn:
            rec = user_enrollment(conn, ev["id"], interaction.user.id)
            sq = rec["squad"] if rec else "SA"
        await interaction.response.send_message(f"You joined **{team_label(ev, team)} — {'Squad A' if sq=='SA' else 'Squad B'}** as **main**.", ephemeral=True)


@tree.command(description="Leave Shadowfront (removes you from main/backup).")
async def leave(interaction: discord.Interaction):
    promoted_user_id = None
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True); return
        c = conn.cursor()
        c.execute("SELECT * FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], interaction.user.id))
        prior = c.fetchone()
        if not prior:
            await interaction.response.send_message("You are not registered for this event.", ephemeral=True); return
        c.execute("DELETE FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], interaction.user.id))
        if prior["slot_type"] == "main" and prior["is_commander"] == 0 and prior["squad"] in ("SA","SB"):
            promoted_user_id = promote_one_non_commander(conn, ev, prior["team"], prior["squad"])
    await warm_cache_for_guild(interaction.guild)
    await refresh_roster_message(interaction.guild)
    msg = "You have left the event."
    if promoted_user_id:
        m = interaction.guild.get_member(promoted_user_id)
        msg += f" Promoted {m.mention if m else f'<@{promoted_user_id}>'} to main."
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(description="Show Shadowfront roster (ephemeral) and refresh the live message.")
async def roster(interaction: discord.Interaction):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
    await warm_cache_for_guild(interaction.guild)
    embed = await roster_embed(ev, interaction.guild)
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---- Admin ----
@tree.command(description="Purge this server's guild-scoped commands (admin only).")
async def purge_guild(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You must have Manage Server.", ephemeral=True); return
    try:
        await tree.clear_commands(guild=interaction.guild)
        await tree.sync(guild=interaction.guild)  # push empty set to guild scope
        await interaction.response.send_message(
            "🧹 Purged guild-scoped commands for this server. Global commands remain.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ Purge failed: `{e}`", ephemeral=True)


@tree.command(description="Sync (publish) the current command set globally (admin only).")
async def sync(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You must have Manage Server.", ephemeral=True); return
    try:
        synced = await tree.sync()
        await interaction.response.send_message(f"🌍 Published **{len(synced)}** command(s) globally.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Global sync failed: `{e}`", ephemeral=True)


@tree.command(description="Full re-sync globally: clear then republish (admin only).")
async def sync_full(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You must have Manage Server.", ephemeral=True); return
    try:
        await tree.clear_commands(guild=None)
        synced = await tree.sync()
        await interaction.response.send_message(f"🌍 Full global re-sync complete: **{len(synced)}** command(s).", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Full global re-sync failed: `{e}`", ephemeral=True)


# --------------- Run ---------------
bot.run(TOKEN)
