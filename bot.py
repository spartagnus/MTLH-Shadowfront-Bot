
# bot.py
# Discord Guild Teams Manager
# - Dedicated channel live roster message (single message, edited on changes)
# - Two teams with caps; defaults: 20 mains + 10 backups each
# - 3 reserved commander slots per team (inside the 20 mains), manager/admin only
# - Per-team time display; editable only by a chosen role (or manager/admin)
# - Button UI for roster actions: Join/Backup/Leave
# - Manager-only buttons: Lock, Unlock, Promote Team 1/2, Reset
# - Weekly auto-refresh (default: Monday 09:00 Australia/Brisbane), configurable per event
# - Team labels (defaults to "Shadowfront Team 1" and "Shadowfront Team 2") with /event setteamlabels
# - SQLite persistence

import os
import sqlite3
import time
import io
from contextlib import contextmanager
from typing import Optional, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None  # Fallback if unavailable; we'll default to UTC

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Please set DISCORD_TOKEN environment variable.")

INTENTS = discord.Intents.default()
INTENTS.members = True  # for mentions/resolution

bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

DB_PATH = "guild_teams.db"

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
            name TEXT NOT NULL,
            starts_at TEXT,
            team_size INTEGER NOT NULL DEFAULT 20,     -- total mains per team
            backup_size INTEGER NOT NULL DEFAULT 10,   -- backups per team
            teams INTEGER NOT NULL DEFAULT 2,          -- 1 or 2 supported in UI
            status TEXT NOT NULL DEFAULT 'open',       -- open|locked|closed
            created_by INTEGER NOT NULL,
            display_channel_id INTEGER,
            display_message_id INTEGER,
            UNIQUE(guild_id, name)
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS rosters(
            event_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            team TEXT NOT NULL,            -- 'A' or 'B'
            slot_type TEXT NOT NULL,       -- 'main' or 'backup'
            is_commander INTEGER NOT NULL DEFAULT 0,  -- 0/1
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
        add_missing_columns(conn)

def add_missing_columns(conn: sqlite3.Connection):
    """Ensure newer columns exist in older DBs."""
    c = conn.cursor()
    c.execute("PRAGMA table_info(events)")
    ecols = {row[1] for row in c.fetchall()}
    c.execute("PRAGMA table_info(rosters)")
    rcols = {row[1] for row in c.fetchall()}

    # events: labels, time editing, commander quota, auto-refresh
    if "team_a_label" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN team_a_label TEXT")
    if "team_b_label" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN team_b_label TEXT")
    if "authorized_role_id" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN authorized_role_id INTEGER")
    if "team_a_time_text" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN team_a_time_text TEXT")
    if "team_b_time_text" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN team_b_time_text TEXT")
    if "team_a_time_unix" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN team_a_time_unix INTEGER")
    if "team_b_time_unix" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN team_b_time_unix INTEGER")
    if "commander_quota" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN commander_quota INTEGER DEFAULT 3")
    if "auto_refresh_enabled" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN auto_refresh_enabled INTEGER DEFAULT 1")
    if "auto_refresh_day" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN auto_refresh_day TEXT DEFAULT 'MON'")
    if "auto_refresh_hour" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN auto_refresh_hour INTEGER DEFAULT 9")
    if "auto_refresh_tz" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN auto_refresh_tz TEXT DEFAULT 'Australia/Brisbane'")
    if "auto_refresh_last_epoch" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN auto_refresh_last_epoch INTEGER")

    # rosters: is_commander
    if "is_commander" not in rcols:
        c.execute("ALTER TABLE rosters ADD COLUMN is_commander INTEGER NOT NULL DEFAULT 0")

def is_manager(conn, event_id: int, user_id: int) -> bool:
    c = conn.cursor()
    c.execute("SELECT 1 FROM managers WHERE event_id=? AND user_id=?", (event_id, user_id))
    if c.fetchone():
        return True
    c.execute("SELECT 1 FROM events WHERE id=? AND created_by=?", (event_id, user_id))
    return c.fetchone() is not None

def get_event(conn, guild_id: int, name: str) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute("SELECT * FROM events WHERE guild_id=? AND name=?", (guild_id, name))
    return c.fetchone()

def list_events_for_guild(conn, guild_id: int) -> List[sqlite3.Row]:
    c = conn.cursor()
    c.execute("SELECT * FROM events WHERE guild_id=?", (guild_id,))
    return c.fetchall()

def user_enrollment(conn, event_id: int, user_id: int) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute("SELECT * FROM rosters WHERE event_id=? AND user_id=?", (event_id, user_id))
    return c.fetchone()

def count_mains(conn, event_id: int, team: str, commanders_only: bool = False, non_commanders_only: bool = False) -> int:
    c = conn.cursor()
    where = "slot_type='main' AND team=? AND event_id=?"
    if commanders_only:
        where += " AND is_commander=1"
    if non_commanders_only:
        where += " AND is_commander=0"
    c.execute(f"SELECT COUNT(*) FROM rosters WHERE {where}", (team, event_id))
    return c.fetchone()[0]

def count_backups(conn, event_id: int, team: str) -> int:
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM rosters WHERE slot_type='backup' AND team=? AND event_id=?", (team, event_id))
    return c.fetchone()[0]

def get_team_counts(conn, ev: sqlite3.Row, team: str):
    total_mains = count_mains(conn, ev["id"], team)
    commanders = count_mains(conn, ev["id"], team, commanders_only=True)
    non_commanders = count_mains(conn, ev["id"], team, non_commanders_only=True)
    backups = count_backups(conn, ev["id"], team)
    return total_mains, commanders, non_commanders, backups

def non_commander_cap(ev: sqlite3.Row) -> int:
    """Maximum number of non-commander mains per team (team_size - commander_quota)."""
    return max(0, int(ev["team_size"]) - int(ev["commander_quota"] or 0))

def add_participant(conn, ev: sqlite3.Row, user_id: int, team: str, force_backup: bool = False) -> Tuple[str, str]:
    """
    Signup flow for normal users (non-commander).
    Returns (slot_type, note). slot_type in {'main','backup',''}; note may be message.
    """
    if ev["status"] != "open":
        return ("", "This event is currently locked. Ask a manager to /event unlock.")

    existing = user_enrollment(conn, ev["id"], user_id)
    if existing:
        if existing["team"] == team:
            return (existing["slot_type"], f"You are already on {team_label(ev, team)} as {('commander ' if existing['is_commander'] else '')}{existing['slot_type']}.")
        else:
            return ("", f"You are already registered on {team_label(ev, existing['team'])}. Leave first with /leave.")

    total_mains, commanders, non_commanders, backups = get_team_counts(conn, ev, team)

    # Decide slot
    if not force_backup:
        # Respect reserved commander slots: non-commander mains limited to team_size - commander_quota
        if non_commanders < non_commander_cap(ev):
            slot_type = "main"
        else:
            slot_type = None

        if slot_type is None:
            # fallback to backup if space
            if backups < ev["backup_size"]:
                slot_type = "backup"
            else:
                return ("", f"{team_label(ev, team)} is full (mains and backups).")
    else:
        if backups < ev["backup_size"]:
            slot_type = "backup"
        else:
            return ("", f"{team_label(ev, team)} backups are full.")

    c = conn.cursor()
    c.execute(
        "INSERT INTO rosters(event_id, user_id, team, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?)",
        (ev["id"], user_id, team, slot_type, 0, int(time.time()))
    )
    return (slot_type, "joined")

def remove_participant(conn, event_id: int, user_id: int) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute("SELECT * FROM rosters WHERE event_id=? AND user_id=?", (event_id, user_id))
    row = c.fetchone()
    if not row:
        return None
    c.execute("DELETE FROM rosters WHERE event_id=? AND user_id=?", (event_id, user_id))
    return row

def promote_one_non_commander(conn, ev: sqlite3.Row, team: str) -> Optional[int]:
    """
    Promote earliest backup to main if non-commander capacity allows.
    Does NOT fill commander slots; only non-commander mains promoted.
    """
    _, _, non_commanders, _ = get_team_counts(conn, ev, team)
    if non_commanders >= non_commander_cap(ev):
        return None
    c = conn.cursor()
    c.execute("""
        SELECT user_id FROM rosters
        WHERE event_id=? AND team=? AND slot_type='backup'
        ORDER BY joined_at ASC LIMIT 1
    """, (ev["id"], team))
    row = c.fetchone()
    if not row:
        return None
    uid = row["user_id"]
    c.execute("UPDATE rosters SET slot_type='main', is_commander=0 WHERE event_id=? AND user_id=?", (ev["id"], uid))
    return uid

def get_roster(conn, event_id: int, team: str):
    c = conn.cursor()
    c.execute("""
        SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND is_commander=1
        ORDER BY joined_at ASC
    """, (event_id, team))
    commanders = [r[0] for r in c.fetchall()]
    c.execute("""
        SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND is_commander=0
        ORDER BY joined_at ASC
    """, (event_id, team))
    mains_non_cmd = [r[0] for r in c.fetchall()]
    c.execute("""
        SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='backup'
        ORDER BY joined_at ASC
    """, (event_id, team))
    backups = [r[0] for r in c.fetchall()]
    return commanders, mains_non_cmd, backups

def has_time_edit_permission(ev: sqlite3.Row, member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    with db() as conn:
        if is_manager(conn, ev["id"], member.id):
            return True
    role_id = ev["authorized_role_id"]
    return bool(role_id and any(r.id == role_id for r in member.roles))

def team_label(ev: sqlite3.Row, team_code: str) -> str:
    """Return display label for 'A' or 'B'."""
    if team_code == "A":
        return ev["team_a_label"] or "Shadowfront Team 1"
    else:
        return ev["team_b_label"] or "Shadowfront Team 2"

def roster_embed(ev: sqlite3.Row, guild: discord.Guild, title_suffix: str = "") -> discord.Embed:
    embed = discord.Embed(
        title=f"Event: {ev['name']} {title_suffix}".strip(),
        description=(
            f"Status: **{ev['status']}** | Teams: **{ev['teams']}** | "
            f"Mains per team: **{ev['team_size']}** (incl. {ev['commander_quota']} commanders) | "
            f"Backups per team: **{ev['backup_size']}**"
        ),
        color=discord.Color.blurple()
    )
    if ev['starts_at']:
        embed.add_field(name="Event Start", value=ev['starts_at'], inline=False)

    def format_team_time(team: str) -> str:
        if team == "A":
            unix = ev["team_a_time_unix"]
            text = ev["team_a_time_text"]
        else:
            unix = ev["team_b_time_unix"]
            text = ev["team_b_time_text"]
        if unix and isinstance(unix, int):
            return f"<t:{unix}:F> (<t:{unix}:R>)"
        elif text and len(str(text).strip()) > 0:
            return str(text).strip()
        else:
            return "_Not set_"

    with db() as conn:
        for team in ["A", "B"][:ev["teams"]]:
            label = team_label(ev, team)

            # Time
            embed.add_field(name=f"{label} — Time", value=format_team_time(team), inline=False)

            commanders, mains_non_cmd, backups = get_roster(conn, ev["id"], team)
            # Names
            def name_list(uids: List[int]) -> str:
                names = [guild.get_member(uid).mention if guild.get_member(uid) else f"<@{uid}>" for uid in uids]
                return "\n".join(names) if names else "*None*"

            # Commanders
            embed.add_field(
                name=f"{label} — Commanders ({len(commanders)}/{ev['commander_quota']})",
                value=name_list(commanders),
                inline=True
            )
            # Non-commander mains
            embed.add_field(
                name=f"{label} — Mains ({len(mains_non_cmd)}/{non_commander_cap(ev)})",
                value=name_list(mains_non_cmd),
                inline=True
            )
            # Backups
            embed.add_field(
                name=f"{label} — Backups ({len(backups)}/{ev['backup_size']})",
                value=name_list(backups),
                inline=False
            )
            embed.add_field(name="\u200b", value="\u200b", inline=False)

    embed.set_footer(text="Buttons: Join/Backup/Leave + Manager: Lock/Unlock, Promote Team 1/2, Reset • Slash: /event setteamlabels, /event setteamtime, /event setcommander, /event unsetcommander, /event setautorefresh")
    return embed

def user_is_event_manager_or_admin(ev: sqlite3.Row, member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    with db() as conn:
        return is_manager(conn, ev["id"], member.id)

# ---------- BUTTON VIEW ----------
class RosterView(discord.ui.View):
    """
    Buttons for roster actions. Attached to the live roster message.
    Recreated on every message update and on bot startup for existing events.
    """
    def __init__(self, event_name: str, label_team1: str, label_team2: str, teams_count: int):
        super().__init__(timeout=None)  # persistent until message replaced
        self.event_name = event_name
        self.label_team1 = label_team1
        self.label_team2 = label_team2
        self.teams_count = teams_count

        # Player buttons (row 0)
        self._add_button(f"Join {self.label_team1}", discord.ButtonStyle.primary, 0, lambda i: self._join_common(i, "A", False))
        if self.teams_count >= 2:
            self._add_button(f"Join {self.label_team2}", discord.ButtonStyle.primary, 0, lambda i: self._join_common(i, "B", False))
        self._add_button(f"Backup {self.label_team1}", discord.ButtonStyle.secondary, 0, lambda i: self._join_common(i, "A", True))
        if self.teams_count >= 2:
            self._add_button(f"Backup {self.label_team2}", discord.ButtonStyle.secondary, 0, lambda i: self._join_common(i, "B", True))
        self._add_button("Leave", discord.ButtonStyle.danger, 0, self._leave_common)

        # Manager buttons (row 1)
        self._add_button("Lock", discord.ButtonStyle.secondary, 1, self._mgr_lock)
        self._add_button("Unlock", discord.ButtonStyle.secondary, 1, self._mgr_unlock)
        self._add_button(f"Promote {self.label_team1}", discord.ButtonStyle.success, 1, lambda i: self._mgr_promote(i, "A"))
        if self.teams_count >= 2:
            self._add_button(f"Promote {self.label_team2}", discord.ButtonStyle.success, 1, lambda i: self._mgr_promote(i, "B"))
        self._add_button("Reset", discord.ButtonStyle.danger, 1, self._mgr_reset)

    def _add_button(self, label: str, style: discord.ButtonStyle, row: int, handler):
        btn = discord.ui.Button(label=label, style=style, row=row)
        async def _callback(interaction: discord.Interaction):
            await handler(interaction)
        btn.callback = _callback
        self.add_item(btn)

    # --------- PLAYER ACTIONS ---------
    async def _join_common(self, interaction: discord.Interaction, team: str, force_backup: bool = False):
        with db() as conn:
            ev = get_event(conn, interaction.guild_id, self.event_name)
            if not ev:
                await interaction.response.send_message("Event not found.", ephemeral=True)
                return
            if force_backup:
                slot_type, note = add_participant(conn, ev, interaction.user.id, team, force_backup=True)
            else:
                slot_type, note = add_participant(conn, ev, interaction.user.id, team)
            if not slot_type:
                await interaction.response.send_message(note, ephemeral=True)
                return
        await refresh_roster_message(interaction.guild, self.event_name)
        label = team_label(ev, team) if 'ev' in locals() and ev else ("Team 1" if team == "A" else "Team 2")
        if force_backup:
            await interaction.response.send_message(f"Joined **{label}** as **backup**.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Joined **{label}** as **{slot_type}**.", ephemeral=True)

    async def _leave_common(self, interaction: discord.Interaction):
        promoted_user_id = None
        prior = None
        with db() as conn:
            ev = get_event(conn, interaction.guild_id, self.event_name)
            if not ev:
                await interaction.response.send_message("Event not found.", ephemeral=True)
                return
            prior = remove_participant(conn, ev["id"], interaction.user.id)
            if not prior:
                await interaction.response.send_message("You are not registered for this event.", ephemeral=True)
                return
            if prior["slot_type"] == "main" and prior["is_commander"] == 0:
                promoted_user_id = promote_one_non_commander(conn, ev, prior["team"])
        await refresh_roster_message(interaction.guild, self.event_name)
        msg = "You have left the event."
        if promoted_user_id:
            member = interaction.guild.get_member(promoted_user_id)
            msg += f" Promoted {member.mention if member else f'<@{promoted_user_id}>'} to main on {team_label(ev, prior['team'])}."
        await interaction.response.send_message(msg, ephemeral=True)

    # --------- MANAGER-ONLY ACTIONS ---------
    async def _require_manager(self, interaction: discord.Interaction) -> Optional[sqlite3.Row]:
        with db() as conn:
            ev = get_event(conn, interaction.guild_id, self.event_name)
            if not ev:
                await interaction.response.send_message("Event not found.", ephemeral=True)
                return None
            if not user_is_event_manager_or_admin(ev, interaction.user):
                await interaction.response.send_message("Manager-only action. You must be an event manager or have Manage Server.", ephemeral=True)
                return None
            return ev

    async def _mgr_lock(self, interaction: discord.Interaction):
        ev = await self._require_manager(interaction)
        if not ev: return
        with db() as conn:
            c = conn.cursor()
            c.execute("UPDATE events SET status='locked' WHERE id=?", (ev["id"],))
        await refresh_roster_message(interaction.guild, self.event_name)
        await interaction.response.send_message("Event locked. Roster updated.", ephemeral=True)

    async def _mgr_unlock(self, interaction: discord.Interaction):
        ev = await self._require_manager(interaction)
        if not ev: return
        with db() as conn:
            c = conn.cursor()
            c.execute("UPDATE events SET status='open' WHERE id=?", (ev["id"],))
        await refresh_roster_message(interaction.guild, self.event_name)
        await interaction.response.send_message("Event unlocked. Roster updated.", ephemeral=True)

    async def _mgr_promote(self, interaction: discord.Interaction, team: str):
        ev = await self._require_manager(interaction)
        if not ev: return
        uid = None
        with db() as conn:
            uid = promote_one_non_commander(conn, ev, team)
        await refresh_roster_message(interaction.guild, self.event_name)
        if uid:
            m = interaction.guild.get_member(uid)
            await interaction.response.send_message(f"Promoted {m.mention if m else f'<@{uid}>'} to main (non-commander) on {team_label(ev, team)}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"No backups to promote or non-commander mains are at capacity for {team_label(ev, team)}.", ephemeral=True)

    async def _mgr_reset(self, interaction: discord.Interaction):
        ev = await self._require_manager(interaction)
        if not ev: return
        with db() as conn:
            reset_event_roster(conn, ev["id"])
        await refresh_roster_message(interaction.guild, self.event_name)
        await interaction.response.send_message("Event reset: cleared all sign-ups and re-opened. Live roster updated.", ephemeral=True)

# ---------- END BUTTON VIEW ----------

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
    view = RosterView(
        ev["name"],
        team_label(ev, "A"),
        team_label(ev, "B"),
        int(ev["teams"] or 2)
    )

    if msg is None:
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return None
        with db() as conn:
            c = conn.cursor()
            c.execute("UPDATE events SET display_message_id=? WHERE id=?", (msg.id, ev["id"]))
    else:
        try:
            await msg.edit(embed=embed, view=view)
        except discord.Forbidden:
            return None

    return msg

async def refresh_roster_message(guild: discord.Guild, name: str):
    with db() as conn:
        ev = get_event(conn, guild.id, name)
        if not ev:
            return
    await ensure_roster_message(ev, guild)

@bot.event
async def on_ready():
    init_db()
    # Reattach views to all live roster messages across all guilds (so buttons work after restart)
    try:
        for g in bot.guilds:
            with db() as conn:
                for ev in list_events_for_guild(conn, g.id):
                    try:
                        await ensure_roster_message(ev, g)
                    except Exception as e:
                        print(f"Failed to attach view for event '{ev['name']}' in guild {g.id}: {e}")
        # Start scheduled weekly refresh
        if not weekly_refresh_task.is_running():
            weekly_refresh_task.start()
        await tree.sync()
    except Exception as e:
