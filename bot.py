# bot.py
# Single-event bot for "Shadowfront" — global app commands, minimal buttons, manager/admin slash commands

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
INTENTS.members = True  # resolve mentions & build mention strings

bot = commands.Bot(command_prefix=None, intents=INTENTS, help_command=None)
tree = bot.tree

# Prefer a mounted volume on Railway if available; otherwise fall back
DEFAULT_DB = "guild_teams_new.db"
DB_PATH = os.getenv("DB_PATH")
if not DB_PATH:
    DB_PATH = "/data/shadowfront.db" if os.path.isdir("/data") else DEFAULT_DB
# Control global sync on startup (default true)
SYNC_ON_STARTUP = os.getenv("SYNC_ON_STARTUP", "true").lower() in ("1", "true", "yes")
# Always sync commands once per bot process on startup/redeploy.
# This avoids needing to manually run /sync after uploading a new version.
STARTUP_SYNC_DONE = False

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
        # Backfill new columns on older DBs (safe no-ops if they already exist)
        try:
            c.execute("ALTER TABLE events ADD COLUMN squads INTEGER NOT NULL DEFAULT 2")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE events ADD COLUMN remind_enabled INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE events ADD COLUMN remind_lead_minutes INTEGER NOT NULL DEFAULT 60")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE events ADD COLUMN team_a_last_remind_epoch INTEGER")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE events ADD COLUMN team_b_last_remind_epoch INTEGER")
        except Exception:
            pass

        # Manual/non-Discord roster entries store their plain-text name here.
        try:
            c.execute("ALTER TABLE rosters ADD COLUMN display_name TEXT")
        except Exception:
            pass

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
            auto_refresh_day TEXT DEFAULT 'SUN',
            auto_refresh_hour INTEGER DEFAULT 9,
            auto_refresh_tz TEXT DEFAULT 'Australia/Brisbane',
            auto_refresh_last_epoch INTEGER,
            squads INTEGER NOT NULL DEFAULT 2,
            remind_enabled INTEGER NOT NULL DEFAULT 1,
            remind_lead_minutes INTEGER NOT NULL DEFAULT 60,
            team_a_last_remind_epoch INTEGER,
            team_b_last_remind_epoch INTEGER,
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
            display_name TEXT,
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
        # New roster layout: one main squad per Squad 1/Squad 2 entry with 3 commander slots + 17 member slots.
        # Backups are separate and must be chosen explicitly.
        c.execute(
            """
            UPDATE events
            SET squad_a_size=20,
                squad_b_size=0,
                squad_a_commander_quota=3,
                squad_b_commander_quota=0,
                squads=1,
                team_a_label=CASE WHEN team_a_label IS NULL OR team_a_label LIKE '%Team 1%' THEN 'Shadowfront Squad 1' ELSE team_a_label END,
                team_b_label=CASE WHEN team_b_label IS NULL OR team_b_label LIKE '%Team 2%' THEN 'Shadowfront Squad 2' ELSE team_b_label END,
                auto_refresh_day=CASE WHEN auto_refresh_day IS NULL OR auto_refresh_day='MON' THEN 'SUN' ELSE auto_refresh_day END
            WHERE id=?
            """,
            (row["id"],)
        )
        c.execute("SELECT * FROM events WHERE id=?", (row["id"],))
        return c.fetchone()
    # Defaults: two squad entries; one main squad per Squad 1/Squad 2 entry (3 commanders + 17 members), backups 10
    c.execute(
        """
        INSERT INTO events(
            guild_id, name, team_size, backup_size, teams, status,
            created_by, display_channel_id, display_message_id,
            team_a_label, team_b_label,
            team_a_slot, team_b_slot,
            squad_a_size, squad_b_size, squad_a_commander_quota, squad_b_commander_quota,
            auto_refresh_enabled, auto_refresh_day, auto_refresh_hour, auto_refresh_tz,
            squads, remind_enabled, remind_lead_minutes
        )
        VALUES (?,?,?,?,?, 'open', ?, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, 1, 'SUN', 9, 'Australia/Brisbane', 1, 1, 60)
        """,
        (
            guild_id, FIXED_EVENT_NAME, 20, 10, 2,
            creator_id, "Shadowfront Squad 1", "Shadowfront Squad 2",
            20, 0, 3, 0
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

def manual_name_exists(conn, event_id: int, name: str) -> bool:
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM rosters WHERE event_id=? AND lower(display_name)=lower(?)",
        (event_id, name.strip())
    )
    return c.fetchone() is not None

def next_manual_user_id(conn, event_id: int) -> int:
    """Return a negative synthetic user_id for non-Discord roster entries."""
    c = conn.cursor()
    c.execute("SELECT MIN(user_id) FROM rosters WHERE event_id=? AND user_id < 0", (event_id,))
    current_min = c.fetchone()[0]
    return -1 if current_min is None else int(current_min) - 1

def roster_display_name(guild: discord.Guild, uid: int, display_name: Optional[str] = None) -> str:
    if display_name:
        return display_name
    member = guild.get_member(uid)
    if member:
        return member.display_name
    return f"User ID: {uid}"

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
    try:
        return int(ev["squads"]) if ev["squads"] is not None else 2
    except Exception:
        return 2

def button_dual_time_label(ev: sqlite3.Row, team: str) -> str:
    slot = ev["team_a_slot"] if team == "A" else ev["team_b_slot"]
    return f"(UTC {slot if slot else '----'})"

# ---------- Roster logic ----------
def team_label(ev: sqlite3.Row, team: str) -> str:
    return (ev["team_a_label"] or "Shadowfront Squad 1") if team == "A" else (ev["team_b_label"] or "Shadowfront Squad 2")

def add_participant(conn, ev: sqlite3.Row, user_id: int, team: str, squad: Optional[str] = None, force_backup: bool = False) -> Tuple[str, str]:
    """Add a participant to either main or backup.

    Important: this no longer auto-falls back from mains to backups. Players/managers must
    choose backup explicitly by pressing a backup button or using as_backup=True.
    """
    if ev["status"] != "open":
        return ("", "This event is currently locked.")

    existing = user_enrollment(conn, ev["id"], user_id)
    if existing:
        if existing["team"] == team:
            loc = f"{team_label(ev, team)} (backup)" if existing["slot_type"] == "backup" else f"{team_label(ev, team)} — Mains"
            return (existing["slot_type"], f"You are already on {loc}.")
        return ("", f"You are already registered on {team_label(ev, existing['team'])}. Leave first with /leave.")

    c = conn.cursor()
    backups = count_backups(conn, ev["id"], team)

    if force_backup:
        if backups >= int(ev["backup_size"] or 0):
            return ("", f"{team_label(ev, team)} backups are full.")
        c.execute(
            "INSERT INTO rosters(event_id,user_id,team,squad,slot_type,is_commander,joined_at) VALUES (?,?,?,?,?,?,?)",
            (ev["id"], user_id, team, None, "backup", 0, int(time.time()))
        )
        return ("backup", "joined")

    # Main signups only use Squad A. Capacity is 17 regular members because Squad A is 20 total
    # with 3 commander slots reserved.
    main_cap = non_commander_cap(ev, "SA")
    current_mains = count_mains(conn, ev["id"], team, "SA", non_commanders_only=True)
    if current_mains >= main_cap:
        return ("", f"{team_label(ev, team)} mains are full. Please choose the backup button if you want to be a backup.")

    c.execute(
        "INSERT INTO rosters(event_id,user_id,team,squad,slot_type,is_commander,joined_at) VALUES (?,?,?,?,?,?,?)",
        (ev["id"], user_id, team, "SA", "main", 0, int(time.time()))
    )
    return ("main", "joined")



def add_manual_participant(
    conn,
    ev: sqlite3.Row,
    name: str,
    team: str,
    force_backup: bool = False,
    as_commander: bool = False
) -> Tuple[str, str]:
    """Add a plain-text/manual roster entry for someone who is not in Discord."""
    clean_name = " ".join((name or "").strip().split())
    if not clean_name:
        return ("", "Please provide a name.")
    if len(clean_name) > 80:
        return ("", "Name is too long. Please use 80 characters or fewer.")
    if ev["status"] != "open":
        return ("", "This event is currently locked.")
    if manual_name_exists(conn, ev["id"], clean_name):
        return ("", f"**{clean_name}** is already on the roster.")

    c = conn.cursor()
    uid = next_manual_user_id(conn, ev["id"])

    if force_backup:
        backups = count_backups(conn, ev["id"], team)
        if backups >= int(ev["backup_size"] or 0):
            return ("", f"{team_label(ev, team)} backups are full.")
        c.execute(
            "INSERT INTO rosters(event_id,user_id,team,squad,slot_type,is_commander,joined_at,display_name) VALUES (?,?,?,?,?,?,?,?)",
            (ev["id"], uid, team, None, "backup", 0, int(time.time()), clean_name)
        )
        return ("backup", "joined")

    if as_commander:
        commanders = count_mains(conn, ev["id"], team, "SA", commanders_only=True)
        if commanders >= int(ev["squad_a_commander_quota"] or 0):
            return ("", f"{team_label(ev, team)} already has the maximum of {ev['squad_a_commander_quota']} commanders.")
        c.execute(
            "INSERT INTO rosters(event_id,user_id,team,squad,slot_type,is_commander,joined_at,display_name) VALUES (?,?,?,?,?,?,?,?)",
            (ev["id"], uid, team, "SA", "main", 1, int(time.time()), clean_name)
        )
        return ("commander", "joined")

    main_cap = non_commander_cap(ev, "SA")
    current_mains = count_mains(conn, ev["id"], team, "SA", non_commanders_only=True)
    if current_mains >= main_cap:
        return ("", f"{team_label(ev, team)} mains are full. Add them as a backup instead if needed.")

    c.execute(
        "INSERT INTO rosters(event_id,user_id,team,squad,slot_type,is_commander,joined_at,display_name) VALUES (?,?,?,?,?,?,?,?)",
        (ev["id"], uid, team, "SA", "main", 0, int(time.time()), clean_name)
    )
    return ("main", "joined")

def promote_one_non_commander(conn, ev: sqlite3.Row, team: str, squad: str) -> Optional[int]:
    # Automatic backup promotion has been intentionally disabled.
    return None

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

# ---------- Embed ----------
def roster_embed(ev: sqlite3.Row, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title=f"Event: {FIXED_EVENT_NAME}",
        color=discord.Color.blurple()
    )
    with db() as conn:
        for team in ["A", "B"][:ev["teams"]]:
            label = team_label(ev, team)
            embed.add_field(name=f"{label} — Time (UTC slot)", value=embed_time_for_team(ev, team), inline=False)
            commanders_sa, mains_sa, commanders_sb, mains_sb, backups = get_roster(conn, ev["id"], team)

            def mentions(uids: List[int]) -> str:
                # Display roster names as plain text instead of clickable Discord mentions.
                names = []
                for uid in uids:
                    c = conn.cursor()
                    c.execute("SELECT display_name FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], uid))
                    row = c.fetchone()
                    manual_name = row["display_name"] if row and "display_name" in row.keys() else None
                    names.append(roster_display_name(guild, uid, manual_name))
                return "\n".join(names) if names else "*None*"

            embed.add_field(
                name=f"{label} — Commanders ({len(commanders_sa)}/{ev['squad_a_commander_quota']})",
                value=mentions(commanders_sa), inline=True
            )
            embed.add_field(
                name=f"{label} — Mains ({len(mains_sa)}/{non_commander_cap(ev, 'SA')})",
                value=mentions(mains_sa), inline=True
            )
            embed.add_field(name="\u200b", value="\u200b", inline=False)
            if event_squads(ev) >= 2:
                embed.add_field(
                    name=f"{label} — Squad B — Commanders ({len(commanders_sb)}/{ev['squad_b_commander_quota']})",
                    value=mentions(commanders_sb), inline=True
                )
                embed.add_field(
                    name=f"{label} — Squad B — Mains ({len(mains_sb)}/{non_commander_cap(ev, 'SB')})",
                    value=mentions(mains_sb), inline=True
                )
                embed.add_field(name="\u200b", value="\u200b", inline=False)
            embed.add_field(
                name=f"{label} — Backups ({len(backups)}/{ev['backup_size']})",
                value=mentions(backups), inline=False
            )
            embed.add_field(name="\u200b", value="\u200b", inline=False)
    return embed

# ---------- Buttons (reduced UI) ----------
class RosterView(discord.ui.View):
    """Squad main buttons, squad backup buttons, and Leave."""
    def __init__(self, ev: sqlite3.Row):
        super().__init__(timeout=None)
        self.teams = int(ev["teams"] or 2)
        self.ev = ev

        self._add_button(f"Squad 1 Main {button_dual_time_label(ev, 'A')}", discord.ButtonStyle.primary, 0, lambda i: self._join_main(i, "A"))
        self._add_button(f"Squad 1 Backup {button_dual_time_label(ev, 'A')}", discord.ButtonStyle.secondary, 1, lambda i: self._join_backup(i, "A"))

        if self.teams >= 2:
            self._add_button(f"Squad 2 Main {button_dual_time_label(ev, 'B')}", discord.ButtonStyle.primary, 0, lambda i: self._join_main(i, "B"))
            self._add_button(f"Squad 2 Backup {button_dual_time_label(ev, 'B')}", discord.ButtonStyle.secondary, 1, lambda i: self._join_backup(i, "B"))

        self._add_button("Leave", discord.ButtonStyle.danger, 2, self._leave_common)

    def _add_button(self, label: str, style: discord.ButtonStyle, row: int, handler):
        b = discord.ui.Button(label=label, style=style, row=row)
        async def cb(i: discord.Interaction):
            await handler(i)
        b.callback = cb
        self.add_item(b)

    async def _join_main(self, interaction: discord.Interaction, team: str):
        with db() as conn:
            ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
            slot_type, note = add_participant(conn, ev, interaction.user.id, team, "SA", False)
            if not slot_type:
                await interaction.response.send_message(note, ephemeral=True)
                return
        await refresh_roster_message(interaction.guild)
        await interaction.response.send_message(
            f"Joined **{team_label(ev, team)} — Mains**.",
            ephemeral=True
        )

    async def _join_backup(self, interaction: discord.Interaction, team: str):
        with db() as conn:
            ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
            slot_type, note = add_participant(conn, ev, interaction.user.id, team, None, True)
            if not slot_type:
                await interaction.response.send_message(note, ephemeral=True)
                return
        await refresh_roster_message(interaction.guild)
        await interaction.response.send_message(
            f"Joined **{team_label(ev, team)} — Backup**.",
            ephemeral=True
        )

    async def _leave_common(self, interaction: discord.Interaction):
        with db() as conn:
            ev = get_fixed_event(conn, interaction.guild_id)
            if not ev:
                await interaction.response.send_message("Event not found.", ephemeral=True)
                return
            c = conn.cursor()
            c.execute("SELECT * FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], interaction.user.id))
            prior = c.fetchone()
            if not prior:
                await interaction.response.send_message("You are not registered for this event.", ephemeral=True)
                return
            c.execute("DELETE FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], interaction.user.id))
        await refresh_roster_message(interaction.guild)
        await interaction.response.send_message("You have left the event.", ephemeral=True)

# ---------- Live message helpers ----------
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
    embed = roster_embed(ev, guild)
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
    global STARTUP_SYNC_DONE
    init_db()
    # Ensure the single event exists for every guild the bot is in
    with db() as conn:
        for g in bot.guilds:
            ensure_fixed_event(conn, g.id, bot.user.id)

    try:
        # Log commands in memory
        names = [c.name for c in tree.get_commands()]
        print(f"Loaded {len(names)} commands: {names}")

        # --- GLOBAL SYNC ON STARTUP/REDEPLOY ---
        # Sync once per process startup. Do not clear commands here; tree.sync() publishes
        # the commands currently registered in this file.
        if not STARTUP_SYNC_DONE:
            synced = await tree.sync()
            STARTUP_SYNC_DONE = True
            print(f"[GLOBAL] Startup sync published {len(synced)} command(s) globally.")
        else:
            print("[GLOBAL] Startup sync already completed for this process; skipping duplicate on_ready sync.")

        print(f"Using DB at: {os.path.abspath(DB_PATH)}")
        print(f"Intents.Members enabled: {bot.intents.members}")

        # Attach/refresh views (live roster message)
        for g in bot.guilds:
            with db() as conn:
                ev = get_fixed_event(conn, g.id)
            if ev:
                await ensure_roster_message(ev, g)

        # Start weekly refresh loop
        if not weekly_refresh_task.is_running():
            weekly_refresh_task.start()
        # Start reminders loop
        if not reminders_task.is_running():
            reminders_task.start()

        print("Startup complete.")
    except Exception as e:
        print("Startup error:", e)

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ---------- Weekly auto-reset ----------
def map_weekday_name(dt: datetime) -> str:
    return ["MON","TUE","WED","THU","FRI","SAT","SUN"][dt.weekday()]

async def reset_roster_and_post_new_message(guild: discord.Guild, ev: sqlite3.Row) -> None:
    """Clear the roster, delete the old live roster message if possible, and post a fresh one.

    This keeps the configured display channel but makes the roster the newest message in that channel.
    """
    channel_id = ev["display_channel_id"]
    message_id = ev["display_message_id"]

    if channel_id and message_id:
        channel = guild.get_channel(channel_id)
        if channel:
            try:
                old_msg = await channel.fetch_message(message_id)
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

    with db() as conn:
        conn.execute("DELETE FROM rosters WHERE event_id=?", (ev["id"],))
        conn.execute(
            "UPDATE events SET status='open', display_message_id=NULL WHERE id=?",
            (ev["id"],)
        )
        fresh_ev = get_fixed_event(conn, guild.id)

    if fresh_ev:
        await ensure_roster_message(fresh_ev, guild)

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
            if map_weekday_name(now_local) != (ev["auto_refresh_day"] or "SUN").upper():
                continue
            if now_local.hour != int(ev["auto_refresh_hour"] or 9):
                continue
            start_of_hour = int(now_local.replace(minute=0, second=0, microsecond=0).timestamp())
            last = int(ev["auto_refresh_last_epoch"] or 0)
            if last >= start_of_hour:
                continue
            try:
                await reset_roster_and_post_new_message(g, ev)
                with db() as conn2:
                    conn2.execute("UPDATE events SET auto_refresh_last_epoch=? WHERE id=?", (start_of_hour, ev["id"]))
                print(f"Weekly roster reset completed in guild {g.id}.")
            except Exception as e:
                print(f"Weekly auto-reset failed in guild {g.id}: {e}")

# ---------- Reminders (every 5 minutes) ----------
@tasks.loop(minutes=5)
async def reminders_task():
    now = int(time.time())
    for g in bot.guilds:
        with db() as conn:
            ev = get_fixed_event(conn, g.id)
            if not ev or not ev["remind_enabled"]:
                continue
            ch_id = ev["display_channel_id"]
            if not ch_id:
                continue
            channel = g.get_channel(ch_id)
            if not channel:
                continue
            for team in ["A", "B"][: int(ev["teams"] or 2)]:
                lead = int(ev["remind_lead_minutes"] or 60)
                slot = ev["team_a_slot"] if team == "A" else ev["team_b_slot"]
                if not slot:
                    continue
                event_epoch = next_epoch_for_slot(slot)
                if not event_epoch:
                    continue
                rem_epoch = max(0, event_epoch - lead * 60)
                last_key = "team_a_last_remind_epoch" if team == "A" else "team_b_last_remind_epoch"
                last_sent = int(ev[last_key] or 0)
                if last_sent >= rem_epoch or now < rem_epoch:
                    continue
                commanders_sa, mains_sa, commanders_sb, mains_sb, backups = get_roster(conn, ev["id"], team)
                members = list(dict.fromkeys(commanders_sa + mains_sa + commanders_sb + mains_sb + backups))
                if not members:
                    continue
                label = team_label(ev, team)
                when = f"<t:{event_epoch}:F> (<t:{event_epoch}:R>)"
                mentions = " ".join((g.get_member(uid).mention if g.get_member(uid) else f"<@{uid}>") for uid in members if uid > 0)
                content = f"⏰ Reminder: **{label}** starts {when}." + (f"\n{mentions}" if mentions else "")
                try:
                    await channel.send(content)
                    conn.execute(f"UPDATE events SET {last_key}=? WHERE id=?", (rem_epoch, ev["id"]))
                    ev = get_fixed_event(conn, g.id)  # refresh row thereafter
                except discord.Forbidden:
                    pass

# ---------- Slash Commands ----------
class TeamChoice(app_commands.Transformer):
    async def transform(self, interaction: discord.Interaction, value: str) -> str:
        v = value.upper()
        if v not in ("A", "B"):
            raise app_commands.AppCommandError("Squad must be A or B.")
        return v

class SquadChoice(app_commands.Transformer):
    async def transform(self, interaction: discord.Interaction, value: str) -> str:
        v = value.upper()
        if v not in ("A", "B"):
            raise app_commands.AppCommandError("Squad must be A or B.")
        with db() as conn:
            ev = get_fixed_event(conn, interaction.guild_id)
        if v == "B" and (not ev or event_squads(ev) < 2):
            raise app_commands.AppCommandError("Only Squad A is configured for this event.")
        return "SA" if v == "A" else "SB"

# ---- Config/admin (no event name args) ----
@tree.command(description="Add a manager for Shadowfront (admin/manager only).")
async def addmanager(interaction: discord.Interaction, user: discord.Member):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)

        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message(
                "You must be an event manager or have Manage Server.",
                ephemeral=True
            )
            return

        conn.execute(
            "INSERT OR IGNORE INTO managers(event_id, user_id) VALUES (?, ?)",
            (ev["id"], user.id)
        )

    await interaction.response.send_message(
        f"{user.mention} is now a Shadowfront manager.",
        ephemeral=True
    )
@tree.command(description="Remove a manager from Shadowfront (admin/manager only).")
async def removemanager(interaction: discord.Interaction, user: discord.Member):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)

        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return

        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message(
                "You must be an event manager or have Manage Server.",
                ephemeral=True
            )
            return
            
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "You can't remove yourself as a manager.",
                ephemeral=True
            )
            return
        conn.execute(
            "DELETE FROM managers WHERE event_id=? AND user_id=?",
            (ev["id"], user.id)
        )

    await interaction.response.send_message(
        f"{user.mention} has been removed as a Shadowfront manager.",
        ephemeral=True
    )
@tree.command(description="Set the roster display channel (manager only).")
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        conn.execute("UPDATE events SET display_channel_id=?, display_message_id=NULL WHERE id=?", (channel.id, ev["id"]))
        ev = get_fixed_event(conn, interaction.guild_id)
    await ensure_roster_message(ev, interaction.guild)
    await interaction.response.send_message(f"Display channel set to {channel.mention}.", ephemeral=True)

@tree.command(description="Set the time slot for Squad 1 or Squad 2 (choose 09:00, 18:00, or 23:00 UTC).")
@app_commands.rename(team="squad")
@app_commands.describe(team="A or B (A = Squad 1, B = Squad 2)", slot="One of 09:00, 18:00, 23:00 UTC")
@app_commands.choices(slot=[
    app_commands.Choice(name="09:00 UTC", value="0900"),
    app_commands.Choice(name="18:00 UTC", value="1800"),
    app_commands.Choice(name="23:00 UTC", value="2300"),
])
async def setsquadtime(interaction: discord.Interaction, team: app_commands.Transform[str, TeamChoice], slot: str):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        if team == "A":
            conn.execute("UPDATE events SET team_a_slot=? WHERE id=?", (slot, ev["id"]))
        else:
            conn.execute("UPDATE events SET team_b_slot=? WHERE id=?", (slot, ev["id"]))
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(f"Set **{team_label(ev, team)}** time to **{slot} UTC**.", ephemeral=True)

@tree.command(description="Configure weekly auto-reset for the roster (manager only).")
async def setautorefresh(interaction: discord.Interaction, enable: bool = True, day: str = "SUN", hour: int = 9, tz: str = "Australia/Brisbane"):
    day = day.upper()
    if day not in {"MON","TUE","WED","THU","FRI","SAT","SUN"}:
        await interaction.response.send_message("Invalid day. Use MON..SUN.", ephemeral=True); return
    if hour < 0 or hour > 23:
        await interaction.response.send_message("Invalid hour. Use 0-23.", ephemeral=True); return
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        if ZoneInfo:
            try:
                _ = ZoneInfo(tz)
            except Exception:
                await interaction.response.send_message("Invalid timezone. Provide a valid IANA timezone.", ephemeral=True); return
        conn.execute(
            """
            UPDATE events SET auto_refresh_enabled=?, auto_refresh_day=?, auto_refresh_hour=?, auto_refresh_tz=? WHERE id=?
            """,
            (1 if enable else 0, day, hour, tz, ev["id"])
        )
    await interaction.response.send_message(f"Auto-reset {'enabled' if enable else 'disabled'}: {day} @ {hour:02d}:00 ({tz}).", ephemeral=True)

# ---- Manager actions (no UI buttons) ----
@tree.command(description="Lock Shadowfront to stop new signups (manager only).")
async def lock(interaction: discord.Interaction):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True); return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        conn.execute("UPDATE events SET status='locked' WHERE id=?", (ev["id"],))
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
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message("Event unlocked. Roster updated.", ephemeral=True)

@tree.command(description="Reset Shadowfront: clears all mains/backups and posts a fresh roster message (manager only).")
async def reset(interaction: discord.Interaction, clear_message: bool = True):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return

    if clear_message:
        await reset_roster_and_post_new_message(interaction.guild, ev)
    else:
        with db() as conn:
            conn.execute("DELETE FROM rosters WHERE event_id=?", (ev["id"],))
            conn.execute("UPDATE events SET status='open' WHERE id=?", (ev["id"],))
        await refresh_roster_message(interaction.guild)

    await interaction.response.send_message("Event reset. Fresh roster message posted." if clear_message else "Event reset. Live roster updated.", ephemeral=True)


@tree.command(description="Assign a commander to a squad (manager only).")
@app_commands.rename(team="squad")
@app_commands.describe(team="A or B (A = Squad 1, B = Squad 2)")
async def setcommander(
    interaction: discord.Interaction,
    team: app_commands.Transform[str, TeamChoice],
    user: discord.Member
):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return

        commanders_sa, mains_sa, _, _, _ = get_team_counts(conn, ev, team)
        if commanders_sa >= int(ev["squad_a_commander_quota"] or 0):
            await interaction.response.send_message(
                f"{team_label(ev, team)} already has the maximum of {ev['squad_a_commander_quota']} commanders.",
                ephemeral=True
            )
            return

        existing = user_enrollment(conn, ev["id"], user.id)
        c = conn.cursor()
        if existing:
            if existing["team"] != team:
                await interaction.response.send_message(
                    f"{user.mention} is registered on {team_label(ev, existing['team'])}. Remove them first before assigning them to this squad.",
                    ephemeral=True
                )
                return
            if existing["slot_type"] == "main" and existing["is_commander"] == 1:
                await interaction.response.send_message(f"{user.mention} is already a commander on {team_label(ev, team)}.", ephemeral=True)
                return

            # If they were a main or backup, convert them to commander.
            c.execute(
                "UPDATE rosters SET slot_type='main', squad='SA', is_commander=1 WHERE event_id=? AND user_id=?",
                (ev["id"], user.id)
            )
            action = f"Set {user.mention} as **commander** on {team_label(ev, team)}."
        else:
            c.execute(
                "INSERT INTO rosters(event_id, user_id, team, squad, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?,?)",
                (ev["id"], user.id, team, "SA", "main", 1, int(time.time()))
            )
            action = f"Added {user.mention} as **commander** on {team_label(ev, team)}."

    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(action + " Live roster updated.", ephemeral=True)


@tree.command(description="Remove commander status (manager only).")
@app_commands.rename(team="squad")
@app_commands.describe(team="A or B (A = Squad 1, B = Squad 2)")
async def unsetcommander(
    interaction: discord.Interaction,
    team: app_commands.Transform[str, TeamChoice],
    user: discord.Member,
    demote_to_backup: bool = False
):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return

        existing = user_enrollment(conn, ev["id"], user.id)
        if not existing or existing["team"] != team or existing["is_commander"] != 1 or existing["slot_type"] != "main":
            await interaction.response.send_message(f"{user.mention} is not a main commander on {team_label(ev, team)}.", ephemeral=True)
            return

        c = conn.cursor()
        if demote_to_backup:
            backups = count_backups(conn, ev["id"], team)
            if backups >= int(ev["backup_size"] or 0):
                await interaction.response.send_message(f"{team_label(ev, team)} backups are full.", ephemeral=True)
                return
            c.execute(
                "UPDATE rosters SET is_commander=0, squad=NULL, slot_type='backup' WHERE event_id=? AND user_id=?",
                (ev["id"], user.id)
            )
            action = f"Unset commander and moved {user.mention} to **backup** on {team_label(ev, team)}."
        else:
            current_non_cmd = count_mains(conn, ev["id"], team, "SA", non_commanders_only=True)
            if current_non_cmd >= non_commander_cap(ev, "SA"):
                await interaction.response.send_message(
                    f"Cannot unset commander into mains because {team_label(ev, team)} mains are full. Use demote_to_backup=True.",
                    ephemeral=True
                )
                return
            c.execute(
                "UPDATE rosters SET is_commander=0, squad='SA', slot_type='main' WHERE event_id=? AND user_id=?",
                (ev["id"], user.id)
            )
            action = f"Unset commander: {user.mention} is now a normal **main** on {team_label(ev, team)}."

    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(action + " Live roster updated.", ephemeral=True)

# ---- Player actions

# ---- Player actions ----
@tree.command(description="Join Shadowfront as a main or backup.")
@app_commands.rename(team="squad")
@app_commands.describe(team="A or B (A = Squad 1, B = Squad 2)")
async def join(
    interaction: discord.Interaction,
    team: app_commands.Transform[str, TeamChoice],
    as_backup: bool = False
):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        slot_type, note = add_participant(conn, ev, interaction.user.id, team, None, as_backup)
    if not slot_type:
        await interaction.response.send_message(note, ephemeral=True)
        return
    await refresh_roster_message(interaction.guild)
    if slot_type == "backup":
        await interaction.response.send_message(f"You joined **{team_label(ev, team)} — Backup**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"You joined **{team_label(ev, team)} — Mains**.", ephemeral=True)


@tree.command(description="Leave Shadowfront (removes you from main/backup).")
async def leave(interaction: discord.Interaction):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        c = conn.cursor()
        c.execute("SELECT * FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], interaction.user.id))
        prior = c.fetchone()
        if not prior:
            await interaction.response.send_message("You are not registered for this event.", ephemeral=True)
            return
        c.execute("DELETE FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], interaction.user.id))
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message("You have left the event.", ephemeral=True)


@tree.command(description="Show Shadowfront roster (ephemeral) and refresh the live message.")
async def roster(interaction: discord.Interaction):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
    embed = roster_embed(ev, interaction.guild)
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---- Manager: add/remove member ----
@tree.command(description="(Manager) Add a member to Squad 1 or Squad 2 as main or backup.")
@app_commands.rename(team="squad")
@app_commands.describe(
    user="Member to add",
    team="A or B (A = Squad 1, B = Squad 2)",
    as_backup="If true, add the member to the backups list for that squad"
)
async def addmember(
    interaction: discord.Interaction,
    user: discord.Member,
    team: app_commands.Transform[str, TeamChoice],
    as_backup: bool = False
):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have **Manage Server**.", ephemeral=True)
            return

        existing = user_enrollment(conn, ev["id"], user.id)
        if existing:
            if existing["team"] == team:
                loc = f"{team_label(ev, team)} — Backup" if existing["slot_type"] == "backup" else f"{team_label(ev, team)} — Mains"
                await interaction.response.send_message(f"{user.mention} is already on **{loc}**.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"{user.mention} is already registered on **{team_label(ev, existing['team'])}**. Remove them before re-adding.",
                ephemeral=True
            )
            return

        slot_type, note = add_participant(conn, ev, user.id, team, None, force_backup=as_backup)
        if not slot_type:
            await interaction.response.send_message(note, ephemeral=True)
            return

    await refresh_roster_message(interaction.guild)
    if slot_type == "backup":
        await interaction.response.send_message(f"Added {user.mention} to **{team_label(ev, team)} — Backup**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"Added {user.mention} to **{team_label(ev, team)} — Mains**.", ephemeral=True)


@tree.command(description="(Manager) Add a non-Discord member by name.")
@app_commands.rename(team="squad")
@app_commands.describe(
    name="Plain text name to show on the roster",
    team="A or B (A = Squad 1, B = Squad 2)",
    as_backup="If true, add the name to backups instead of mains",
    as_commander="If true, add the name as a commander instead of a normal main"
)
async def addmanualmember(
    interaction: discord.Interaction,
    name: str,
    team: app_commands.Transform[str, TeamChoice],
    as_backup: bool = False,
    as_commander: bool = False
):
    if as_backup and as_commander:
        await interaction.response.send_message("Choose either backup or commander, not both.", ephemeral=True)
        return

    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have **Manage Server**.", ephemeral=True)
            return

        slot_type, note = add_manual_participant(conn, ev, name, team, force_backup=as_backup, as_commander=as_commander)
        if not slot_type:
            await interaction.response.send_message(note, ephemeral=True)
            return

    await refresh_roster_message(interaction.guild)
    clean_name = " ".join(name.strip().split())
    if slot_type == "backup":
        await interaction.response.send_message(f"Added **{clean_name}** to **{team_label(ev, team)} — Backup**.", ephemeral=True)
    elif slot_type == "commander":
        await interaction.response.send_message(f"Added **{clean_name}** as **commander** on **{team_label(ev, team)}**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"Added **{clean_name}** to **{team_label(ev, team)} — Mains**.", ephemeral=True)


@tree.command(description="(Manager) Remove a non-Discord member by name.")
@app_commands.describe(name="Plain text roster name to remove")
async def removemanualmember(interaction: discord.Interaction, name: str):
    clean_name = " ".join((name or "").strip().split())
    if not clean_name:
        await interaction.response.send_message("Please provide a name.", ephemeral=True)
        return

    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have **Manage Server**.", ephemeral=True)
            return

        c = conn.cursor()
        c.execute(
            "SELECT * FROM rosters WHERE event_id=? AND lower(display_name)=lower(?)",
            (ev["id"], clean_name)
        )
        existing = c.fetchone()
        if not existing:
            await interaction.response.send_message(f"No non-Discord roster entry found for **{clean_name}**.", ephemeral=True)
            return
        conn.execute("DELETE FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], existing["user_id"]))

    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(f"Removed **{existing['display_name']}** from the roster.", ephemeral=True)


@tree.command(description="(Manager) Remove a member from Shadowfront.")
@app_commands.describe(user="Member to remove")
async def removemember(interaction: discord.Interaction, user: discord.Member):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have **Manage Server**.", ephemeral=True)
            return
        existing = user_enrollment(conn, ev["id"], user.id)
        if not existing:
            await interaction.response.send_message(f"{user.mention} is not registered for **{team_label(ev, 'A')}** or **{team_label(ev, 'B')}**.", ephemeral=True)
            return
        conn.execute("DELETE FROM rosters WHERE event_id=? AND user_id=?", (ev["id"], user.id))

    await refresh_roster_message(interaction.guild)
    msg = f"Removed {user.mention} from **{team_label(ev, existing['team'])}**."
    await interaction.response.send_message(msg, ephemeral=True)

# ---- Admin

# ---- Admin ----
@tree.command(description="Purge this server's guild-scoped commands (admin only).")
async def purge_guild(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You must have Manage Server.", ephemeral=True); return
    try:
        tree.clear_commands(guild=interaction.guild)
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

@tree.command(description="Safely publish the current global commands (admin only).")
async def sync_full(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You must have Manage Server.", ephemeral=True); return
    try:
        # Important: do NOT call tree.clear_commands(guild=None) here.
        # In discord.py that clears this bot's in-memory global command tree,
        # so syncing afterwards publishes zero commands.
        synced = await tree.sync()
        await interaction.response.send_message(f"🌍 Global command sync complete: **{len(synced)}** command(s) published.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Global sync failed: `{e}`", ephemeral=True)

# ---- Utility ----
@tree.command(description="Set number of squads (1 or 2).")
async def setsquadcount(interaction: discord.Interaction, count: app_commands.Range[int, 1, 2]):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        current = int(ev["teams"] or 2)
        if count == current:
            await interaction.response.send_message(f"Squads already set to {count}.", ephemeral=True); return
        if count == 1:
            c = conn.cursor()
            total_b = c.execute("SELECT COUNT(*) FROM rosters WHERE event_id=? AND team='B'", (ev["id"],)).fetchone()[0]
            if total_b > 0:
                await interaction.response.send_message(f"Cannot set to 1 squad: Squad 2 currently has {total_b} member(s). Remove or move them first.", ephemeral=True); return
        conn.execute("UPDATE events SET teams=? WHERE id=?", (count, ev["id"]))
    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(f"Set number of squads to **{count}**.", ephemeral=True)

@tree.command(description="Configure main and backup limits (manager only).")
@app_commands.describe(
    main_members="Number of normal main members per squad, not counting commanders",
    commander_slots="Number of commander slots per squad",
    backup_size="Number of backup slots per squad"
)
async def setlimits(
    interaction: discord.Interaction,
    main_members: app_commands.Range[int,1,50] = 17,
    commander_slots: app_commands.Range[int,0,10] = 3,
    backup_size: app_commands.Range[int,0,50] = 10
):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return

        c = conn.cursor()
        for team_code in ['A','B'][:int(ev['teams'] or 2)]:
            current_cmd = c.execute(
                "SELECT COUNT(*) FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND squad='SA' AND is_commander=1",
                (ev["id"], team_code)
            ).fetchone()[0]
            current_main = c.execute(
                "SELECT COUNT(*) FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND squad='SA' AND is_commander=0",
                (ev["id"], team_code)
            ).fetchone()[0]
            current_backup = c.execute(
                "SELECT COUNT(*) FROM rosters WHERE event_id=? AND team=? AND slot_type='backup'",
                (ev["id"], team_code)
            ).fetchone()[0]
            if current_cmd > commander_slots:
                await interaction.response.send_message(f"Squad {1 if team_code == 'A' else 2} currently has {current_cmd} commanders, which exceeds the proposed limit.", ephemeral=True)
                return
            if current_main > main_members:
                await interaction.response.send_message(f"Squad {1 if team_code == 'A' else 2} currently has {current_main} main members, which exceeds the proposed limit.", ephemeral=True)
                return
            if current_backup > backup_size:
                await interaction.response.send_message(f"Squad {1 if team_code == 'A' else 2} currently has {current_backup} backups, which exceeds the proposed limit.", ephemeral=True)
                return

        conn.execute(
            "UPDATE events SET squads=1, squad_a_size=?, squad_b_size=0, squad_a_commander_quota=?, squad_b_commander_quota=0, backup_size=? WHERE id=?",
            (main_members + commander_slots, commander_slots, backup_size, ev["id"])
        )

    await refresh_roster_message(interaction.guild)
    await interaction.response.send_message(
        f"Limits updated: **{main_members} mains**, **{commander_slots} commanders**, **{backup_size} backups** per squad.",
        ephemeral=True
    )


@tree.command(description="Configure reminder pings (manager only).")
async def setreminder(interaction: discord.Interaction, enable: bool = True, lead_minutes: app_commands.Range[int,5,180] = 60):
    with db() as conn:
        ev = get_fixed_event(conn, interaction.guild_id) or ensure_fixed_event(conn, interaction.guild_id, interaction.user.id)
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True); return
        conn.execute("UPDATE events SET remind_enabled=?, remind_lead_minutes=? WHERE id=?", (1 if enable else 0, int(lead_minutes), ev["id"]))
    await interaction.response.send_message(f"Reminders {'enabled' if enable else 'disabled'}; lead time set to {lead_minutes} minutes.", ephemeral=True)

# ---- Help ----
@tree.command(description="List all available commands and what they do.")
async def help(interaction: discord.Interaction):
    embed = discord.Embed(title="Shadowfront – Help", color=discord.Color.blurple())
    embed.set_footer(text="Use / and start typing to discover commands")
    lines: List[str] = []
    for c in tree.get_commands():
        lines.append(f"`/{c.name}` – {c.description or '(no description)'}")
    # Chunk across multiple fields if needed
    chunk: List[str] = []
    cur = 0
    for ln in lines:
        if cur + len(ln) + 1 > 1024:
            embed.add_field(name="Commands", value="\n".join(chunk), inline=False)
            chunk, cur = [], 0
        chunk.append(ln); cur += len(ln) + 1
    if chunk:
        embed.add_field(name="Commands", value="\n".join(chunk), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --------------- Run ---------------
bot.run(TOKEN)
