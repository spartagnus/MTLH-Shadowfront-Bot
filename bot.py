
# bot.py
# Discord Guild Teams Manager — Shadowfront Teams with Squads
# - Dedicated channel live roster message (single message, edited on changes)
# - Two teams with caps; defaults: Team mains split into Squad A (15) + Squad B (5)
# - Commanders reserved per squad: Squad A = 2, Squad B = 1 (total 3 per team)
# - Backups per team: 10 (single pool per team)
# - Per-team time display; editable only by a chosen role (or manager/admin)
# - Button UI: Join Squad A/B per team, Backup per team, Leave
# - Manager-only buttons: Lock, Unlock, Promote per team & squad, Reset
# - Weekly auto-refresh (default: Monday 09:00 Australia/Brisbane), configurable per event
# - Team labels default to "Shadowfront Team 1" and "Shadowfront Team 2"; configurable
# - SQLite persistence; Railway-friendly (DB_PATH env to use a volume)

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

# Use Railway volume if provided (recommended): set DB_PATH=/data/guild_teams.db
DB_PATH = os.getenv("DB_PATH", "guild_teams.db")

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
            team_size INTEGER NOT NULL DEFAULT 20,     -- total mains per team (A+B)
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
            team TEXT NOT NULL,            -- 'A' or 'B' (team code)
            squad TEXT,                    -- 'SA' or 'SB' for mains; NULL for backups
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
    """Ensure newer columns exist in older DBs and backfill sensible defaults."""
    c = conn.cursor()
    c.execute("PRAGMA table_info(events)")
    ecols = {row[1] for row in c.fetchall()}
    c.execute("PRAGMA table_info(rosters)")
    rcols = {row[1] for row in c.fetchall()}

    # Team labels, time editing, commander quota (team), auto-refresh
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
    # Squad sizes and commander quotas per squad
    if "squad_a_size" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN squad_a_size INTEGER DEFAULT 15")
    if "squad_b_size" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN squad_b_size INTEGER DEFAULT 5")
    if "squad_a_commander_quota" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN squad_a_commander_quota INTEGER DEFAULT 2")
    if "squad_b_commander_quota" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN squad_b_commander_quota INTEGER DEFAULT 1")
    # Auto-refresh config
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
    # Rosters: squad & is_commander (if not exists)
    if "squad" not in rcols:
        c.execute("ALTER TABLE rosters ADD COLUMN squad TEXT")
        # Backfill: assign existing mains to Squad A by default (safe baseline)
        c.execute("UPDATE rosters SET squad='SA' WHERE slot_type='main' AND squad IS NULL")
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

def count_mains(conn, event_id: int, team: str, squad: Optional[str] = None, commanders_only: bool = False, non_commanders_only: bool = False) -> int:
    c = conn.cursor()
    where = "slot_type='main' AND team=? AND event_id=?"
    params = [team, event_id]
    if squad:
        where += " AND squad=?"
        params.append(squad)
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
    # Totals per squad and team backups
    commanders_sa = count_mains(conn, ev["id"], team, "SA", commanders_only=True)
    mains_sa = count_mains(conn, ev["id"], team, "SA", non_commanders_only=True)
    commanders_sb = count_mains(conn, ev["id"], team, "SB", commanders_only=True)
    mains_sb = count_mains(conn, ev["id"], team, "SB", non_commanders_only=True)
    backups = count_backups(conn, ev["id"], team)
    return (commanders_sa, mains_sa, commanders_sb, mains_sb, backups)

def non_commander_cap(ev: sqlite3.Row, squad_code: str) -> int:
    """Maximum number of non-commander mains per squad."""
    if squad_code == "SA":
        return max(0, int(ev["squad_a_size"]) - int(ev["squad_a_commander_quota"]))
    else:
        return max(0, int(ev["squad_b_size"]) - int(ev["squad_b_commander_quota"]))

def add_participant(conn, ev: sqlite3.Row, user_id: int, team: str, squad: Optional[str] = None, force_backup: bool = False) -> Tuple[str, str]:
    """
    Signup flow for normal users (non-commander).
    Returns (slot_type, note). slot_type in {'main','backup',''}; note may be message.
    """
    if ev["status"] != "open":
        return ("", "This event is currently locked. Ask a manager to /event unlock.")

    existing = user_enrollment(conn, ev["id"], user_id)
    if existing:
        if existing["team"] == team:
            # Show current assignment including squad if main
            if existing["slot_type"] == "main":
                current_label = f"{team_label(ev, team)} — {'Squad A' if existing['squad']=='SA' else 'Squad B'}"
            else:
                current_label = f"{team_label(ev, team)} (backup)"
            return (existing["slot_type"], f"You are already on {current_label}.")
        else:
            return ("", f"You are already registered on {team_label(ev, existing['team'])}. Leave first with /leave.")

    # Determine squad preference
    chosen_squad = squad if squad in ("SA", "SB") else "SA"  # default to Squad A if not specified

    commanders_sa, mains_sa, commanders_sb, mains_sb, backups = get_team_counts(conn, ev, team)
    # Compute capacity for chosen squad
    current_mains = mains_sa if chosen_squad == "SA" else mains_sb
    cap_mains = non_commander_cap(ev, chosen_squad)

    if not force_backup:
        if current_mains < cap_mains:
            slot_type = "main"
        else:
            slot_type = None
        if slot_type is None:
            # fallback to team backups if space
            if backups < ev["backup_size"]:
                slot_type = "backup"
            else:
                return ("", f"{team_label(ev, team)} is full (non-commander mains and backups).")
    else:
        if backups < ev["backup_size"]:
            slot_type = "backup"
        else:
            return ("", f"{team_label(ev, team)} backups are full.")

    c = conn.cursor()
    c.execute(
        "INSERT INTO rosters(event_id, user_id, team, squad, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?,?)",
        (ev["id"], user_id, team, chosen_squad if slot_type == "main" else None, slot_type, 0, int(time.time()))
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

def promote_one_non_commander(conn, ev: sqlite3.Row, team: str, squad: str) -> Optional[int]:
    """
    Promote earliest team backup to main if the target squad's non-commander capacity allows.
    """
    current_mains = count_mains(conn, ev["id"], team, squad, non_commanders_only=True)
    if current_mains >= non_commander_cap(ev, squad):
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
    c.execute("UPDATE rosters SET slot_type='main', is_commander=0, squad=? WHERE event_id=? AND user_id=?", (squad, ev["id"], uid))
    return uid

def get_roster(conn, event_id: int, team: str):
    c = conn.cursor()
    # Squad A
    c.execute("""
        SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND squad='SA' AND is_commander=1
        ORDER BY joined_at ASC
    """, (event_id, team))
    commanders_sa = [r[0] for r in c.fetchall()]
    c.execute("""
        SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND squad='SA' AND is_commander=0
        ORDER BY joined_at ASC
    """, (event_id, team))
    mains_sa = [r[0] for r in c.fetchall()]
    # Squad B
    c.execute("""
        SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND squad='SB' AND is_commander=1
        ORDER BY joined_at ASC
    """, (event_id, team))
    commanders_sb = [r[0] for r in c.fetchall()]
    c.execute("""
        SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='main' AND squad='SB' AND is_commander=0
        ORDER BY joined_at ASC
    """, (event_id, team))
    mains_sb = [r[0] for r in c.fetchall()]
    # Backups (team-wide)
    c.execute("""
        SELECT user_id FROM rosters WHERE event_id=? AND team=? AND slot_type='backup'
        ORDER BY joined_at ASC
    """, (event_id, team))
    backups = [r[0] for r in c.fetchall()]
    return commanders_sa, mains_sa, commanders_sb, mains_sb, backups

def has_time_edit_permission(ev: sqlite3.Row, member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    with db() as conn:
        if is_manager(conn, ev["id"], member.id):
            return True
    role_id = ev["authorized_role_id"]
    return bool(role_id and any(r.id == role_id for r in member.roles))

def team_label(ev: sqlite3.Row, team_code: str) -> str:
    """Return display label for team 'A' or 'B'."""
    if team_code == "A":
        return ev["team_a_label"] or "Shadowfront Team 1"
    else:
        return ev["team_b_label"] or "Shadowfront Team 2"

def roster_embed(ev: sqlite3.Row, guild: discord.Guild, title_suffix: str = "") -> discord.Embed:
    embed = discord.Embed(
        title=f"Event: {ev['name']} {title_suffix}".strip(),
        description=(
            f"Status: **{ev['status']}** | Teams: **{ev['teams']}** | "
            f"Mains per team: **{ev['team_size']}** (Squad A {ev['squad_a_size']} incl. {ev['squad_a_commander_quota']} cmdrs; "
            f"Squad B {ev['squad_b_size']} incl. {ev['squad_b_commander_quota']} cmdrs) | "
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

            commanders_sa, mains_sa, commanders_sb, mains_sb, backups = get_roster(conn, ev["id"], team)

            def name_list(uids: List[int]) -> str:
                names = [guild.get_member(uid).mention if guild.get_member(uid) else f"<@{uid}>" for uid in uids]
                return "\n".join(names) if names else "*None*"

            # Squad A
            embed.add_field(
                name=f"{label} — Squad A — Commanders ({len(commanders_sa)}/{ev['squad_a_commander_quota']})",
                value=name_list(commanders_sa),
                inline=True
            )
            embed.add_field(
                name=f"{label} — Squad A — Mains ({len(mains_sa)}/{non_commander_cap(ev, 'SA')})",
                value=name_list(mains_sa),
                inline=True
            )
            # spacer
            embed.add_field(name="\u200b", value="\u200b", inline=False)

            # Squad B
            embed.add_field(
                name=f"{label} — Squad B — Commanders ({len(commanders_sb)}/{ev['squad_b_commander_quota']})",
                value=name_list(commanders_sb),
                inline=True
            )
            embed.add_field(
                name=f"{label} — Squad B — Mains ({len(mains_sb)}/{non_commander_cap(ev, 'SB')})",
                value=name_list(mains_sb),
                inline=True
            )
            # Backups (team-wide)
            embed.add_field(
                name=f"{label} — Backups ({len(backups)}/{ev['backup_size']})",
                value=name_list(backups),
                inline=False
            )
            embed.add_field(name="\u200b", value="\u200b", inline=False)

    embed.set_footer(text="Buttons: Join Squad A/B, Backup, Leave • Manager: Lock/Unlock, Promote Squad A/B, Reset • Slash: /event setteamlabels, /event setteamtime, /event setcommander, /event unsetcommander, /event setautorefresh")
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
    def __init__(self, ev: sqlite3.Row):
        super().__init__(timeout=None)
        self.event_name = ev["name"]
        self.teams_count = int(ev["teams"] or 2)
        self.team1_label = team_label(ev, "A")
        self.team2_label = team_label(ev, "B")

        # Player buttons (row 0)
        self._add_button(f"Join {self.team1_label} — Squad A", discord.ButtonStyle.primary, 0, lambda i: self._join_common(i, "A", "SA", False))
        self._add_button(f"Join {self.team1_label} — Squad B", discord.ButtonStyle.primary, 0, lambda i: self._join_common(i, "A", "SB", False))
        if self.teams_count >= 2:
            self._add_button(f"Join {self.team2_label} — Squad A", discord.ButtonStyle.primary, 0, lambda i: self._join_common(i, "B", "SA", False))
            self._add_button(f"Join {self.team2_label} — Squad B", discord.ButtonStyle.primary, 0, lambda i: self._join_common(i, "B", "SB", False))
        self._add_button("Backup (Team 1)", discord.ButtonStyle.secondary, 0, lambda i: self._join_common(i, "A", None, True))
        if self.teams_count >= 2:
            self._add_button("Backup (Team 2)", discord.ButtonStyle.secondary, 0, lambda i: self._join_common(i, "B", None, True))
        self._add_button("Leave", discord.ButtonStyle.danger, 0, self._leave_common)

        # Manager buttons (row 1)
        self._add_button("Lock", discord.ButtonStyle.secondary, 1, self._mgr_lock)
        self._add_button("Unlock", discord.ButtonStyle.secondary, 1, self._mgr_unlock)
        self._add_button(f"Promote {self.team1_label} — Squad A", discord.ButtonStyle.success, 1, lambda i: self._mgr_promote(i, "A", "SA"))
        self._add_button(f"Promote {self.team1_label} — Squad B", discord.ButtonStyle.success, 1, lambda i: self._mgr_promote(i, "A", "SB"))
        if self.teams_count >= 2:
            self._add_button(f"Promote {self.team2_label} — Squad A", discord.ButtonStyle.success, 1, lambda i: self._mgr_promote(i, "B", "SA"))
            self._add_button(f"Promote {self.team2_label} — Squad B", discord.ButtonStyle.success, 1, lambda i: self._mgr_promote(i, "B", "SB"))
        self._add_button("Reset", discord.ButtonStyle.danger, 1, self._mgr_reset)

    def _add_button(self, label: str, style: discord.ButtonStyle, row: int, handler):
        btn = discord.ui.Button(label=label, style=style, row=row)
        async def _callback(interaction: discord.Interaction):
            await handler(interaction)
        btn.callback = _callback
        self.add_item(btn)

    # --------- PLAYER ACTIONS ---------
    async def _join_common(self, interaction: discord.Interaction, team: str, squad: Optional[str], force_backup: bool = False):
        with db() as conn:
            ev = get_event(conn, interaction.guild_id, self.event_name)
            if not ev:
                await interaction.response.send_message("Event not found.", ephemeral=True)
                return
            slot_type, note = add_participant(conn, ev, interaction.user.id, team, squad, force_backup)
            if not slot_type:
                await interaction.response.send_message(note, ephemeral=True)
                return
        await refresh_roster_message(interaction.guild, self.event_name)
        if slot_type == "backup":
            await interaction.response.send_message(f"Joined **{team_label(ev, team)}** as **backup**.", ephemeral=True)
        else:
            squad_name = "Squad A" if (squad or "SA") == "SA" else "Squad B"
            await interaction.response.send_message(f"Joined **{team_label(ev, team)} — {squad_name}** as **main**.", ephemeral=True)

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
            if prior["slot_type"] == "main" and prior["is_commander"] == 0 and prior["squad"] in ("SA","SB"):
                promoted_user_id = promote_one_non_commander(conn, ev, prior["team"], prior["squad"])
        await refresh_roster_message(interaction.guild, self.event_name)
        msg = "You have left the event."
        if promoted_user_id:
            member = interaction.guild.get_member(promoted_user_id)
            msg += f" Promoted {member.mention if member else f'<@{promoted_user_id}>'} to main on {team_label(ev, prior['team'])} — {'Squad A' if prior['squad']=='SA' else 'Squad B'}."
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

    async def _mgr_promote(self, interaction: discord.Interaction, team: str, squad: str):
        ev = await self._require_manager(interaction)
        if not ev: return
        uid = None
        with db() as conn:
            uid = promote_one_non_commander(conn, ev, team, squad)
        await refresh_roster_message(interaction.guild, self.event_name)
        if uid:
            m = interaction.guild.get_member(uid)
            await interaction.response.send_message(f"Promoted {m.mention if m else f'<@{uid}>'} to main (non-commander) on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"No backups to promote or squad mains are at capacity for {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}.", ephemeral=True)

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
    view = RosterView(ev)

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
        print("Command sync or view attach error:", e)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ---- Scheduled Weekly Auto-Refresh ----

def map_weekday_name(dt: datetime) -> str:
    """Return 'MON'..'SUN' for dt.weekday()."""
    return ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][dt.weekday()]

@tasks.loop(minutes=10)
async def weekly_refresh_task():
    """Every 10 minutes, check which events hit their scheduled refresh window and refresh them."""
    for g in bot.guilds:
        with db() as conn:
            events = list_events_for_guild(conn, g.id)
        for ev in events:
            if not ev["auto_refresh_enabled"]:
                continue

            tzname = ev["auto_refresh_tz"] or "Australia/Brisbane"
            try:
                tz = ZoneInfo(tzname) if ZoneInfo else timezone.utc
            except Exception:
                tz = ZoneInfo("Australia/Brisbane") if ZoneInfo else timezone.utc

            now_local = datetime.now(tz)
            target_day = (ev["auto_refresh_day"] or "MON").upper()
            target_hour = int(ev["auto_refresh_hour"] or 9)

            if map_weekday_name(now_local) != target_day:
                continue
            if now_local.hour != target_hour:
                continue

            # Prevent multiple refreshes in the same hour
            start_of_hour = int(now_local.replace(minute=0, second=0, microsecond=0).timestamp())
            last = int(ev["auto_refresh_last_epoch"] or 0)
            if last >= start_of_hour:
                continue

            try:
                await refresh_roster_message(g, ev["name"])
                with db() as conn:
                    c = conn.cursor()
                    c.execute("UPDATE events SET auto_refresh_last_epoch=? WHERE id=?", (start_of_hour, ev["id"]))
            except Exception as e:
                print(f"Auto-refresh failed for event '{ev['name']}' in guild {g.id}: {e}")

# ---- Slash Commands ----

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

@tree.command(description="Create a new event (defaults: 2 teams, Squad A=15 (2 cmdrs), Squad B=5 (1 cmdr), backups=10).")
@app_commands.describe(
    name="Event name (unique per server)",
    starts_at="Optional date/time text (e.g., Sat 7pm AEST)",
    squad_a_size="Size of Squad A (default 15)",
    squad_b_size="Size of Squad B (default 5)",
    squad_a_cmdrs="Commander quota in Squad A (default 2)",
    squad_b_cmdrs="Commander quota in Squad B (default 1)",
    backup_size="Backups per team (default 10)",
    teams="Number of teams (1 or 2)",
    channel="Channel to display the live roster (optional)"
)
async def event_create(
    interaction: discord.Interaction,
    name: str,
    starts_at: str = "",
    squad_a_size: int = 15,
    squad_b_size: int = 5,
    squad_a_cmdrs: int = 2,
    squad_b_cmdrs: int = 1,
    backup_size: int = 10,
    teams: int = 2,
    channel: Optional[discord.TextChannel] = None
):
    if teams < 1 or teams > 2:
        await interaction.response.send_message("For now, teams must be 1 or 2.", ephemeral=True)
        return
    if squad_a_size < 0 or squad_b_size < 0 or backup_size < 0:
        await interaction.response.send_message("Sizes cannot be negative.", ephemeral=True)
        return
    total_team_size = squad_a_size + squad_b_size
    with db() as conn:
        try:
            c = conn.cursor()
            c.execute("""
                INSERT INTO events(guild_id, name, starts_at, team_size, backup_size, teams, status, created_by, display_channel_id, display_message_id,
                    team_a_label, team_b_label, squad_a_size, squad_b_size, squad_a_commander_quota, squad_b_commander_quota, commander_quota)
                VALUES (?,?,?,?,?,?, 'open', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
            """, (
                interaction.guild_id, name, starts_at.strip(), total_team_size, backup_size, teams,
                interaction.user.id, channel.id if channel else None,
                "Shadowfront Team 1", "Shadowfront Team 2",
                squad_a_size, squad_b_size, squad_a_cmdrs, squad_b_cmdrs, squad_a_cmdrs + squad_b_cmdrs
            ))
            event_id = c.lastrowid
            c.execute("INSERT INTO managers(event_id, user_id) VALUES (?,?)", (event_id, interaction.user.id))
        except sqlite3.IntegrityError:
            await interaction.response.send_message("An event with that name already exists here.", ephemeral=True)
            return

    if channel:
        with db() as conn:
            ev = get_event(conn, interaction.guild_id, name)
        await ensure_roster_message(ev, interaction.guild)
        await interaction.response.send_message(
            f"Event **{name}** created. Live roster posted in {channel.mention}.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"Event **{name}** created. Use `/event setchannel` to choose a display channel.",
            ephemeral=True
        )

@tree.command(description="Change or set the event's display channel (manager only).")
@app_commands.describe(name="Event name", channel="Channel to show the live roster")
async def event_setchannel(interaction: discord.Interaction, name: str, channel: discord.TextChannel):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return
        c = conn.cursor()
        c.execute("UPDATE events SET display_channel_id=?, display_message_id=NULL WHERE id=?", (channel.id, ev["id"]))
        ev = get_event(conn, interaction.guild_id, name)
    await ensure_roster_message(ev, interaction.guild)
    await interaction.response.send_message(f"Display channel set to {channel.mention}. Live roster message created/updated.", ephemeral=True)

@tree.command(description="Lock an event to stop new signups.")
async def event_lock(interaction: discord.Interaction, name: str):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return
        c = conn.cursor()
        c.execute("UPDATE events SET status='locked' WHERE id=?", (ev["id"],))
    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message("Event locked. Roster updated.", ephemeral=True)

@tree.command(description="Unlock an event to allow signups again.")
async def event_unlock(interaction: discord.Interaction, name: str):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return
        c = conn.cursor()
        c.execute("UPDATE events SET status='open' WHERE id=?", (ev["id"],))
    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message("Event unlocked. Roster updated.", ephemeral=True)

@tree.command(description="Join a team & squad (auto main if squad has space, otherwise team backup).")
@app_commands.describe(name="Event name", team="A or B", squad="Squad A or B")
async def join(
    interaction: discord.Interaction,
    name: str,
    team: app_commands.Transform[str, TeamChoice],
    squad: app_commands.Transform[str, SquadChoice] = "SA"
):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        slot_type, note = add_participant(conn, ev, interaction.user.id, team, squad, False)
        if not slot_type:
            await interaction.response.send_message(note, ephemeral=True)
            return
    await refresh_roster_message(interaction.guild, name)
    if slot_type == "backup":
        await interaction.response.send_message(f"You joined **{team_label(ev, team)}** as **backup**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"You joined **{team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}** as **main**.", ephemeral=True)

@tree.command(description="Explicitly join the backup list for a team (no squad).")
@app_commands.describe(name="Event name", team="A or B")
async def backup(interaction: discord.Interaction, name: str, team: app_commands.Transform[str, TeamChoice]):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        slot_type, note = add_participant(conn, ev, interaction.user.id, team, None, True)
        if not slot_type:
            await interaction.response.send_message(note, ephemeral=True)
            return
    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message(f"You joined **{team_label(ev, team)}** as **backup**.", ephemeral=True)

@tree.command(description="Leave the event (removes you from main/backup).")
async def leave(interaction: discord.Interaction, name: str):
    promoted_user_id = None
    prior = None
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        prior = remove_participant(conn, ev["id"], interaction.user.id)
        if not prior:
            await interaction.response.send_message("You are not registered for this event.", ephemeral=True)
            return
        # Auto-promotion only for non-commander mains; into the same squad
        if prior["slot_type"] == "main" and prior["is_commander"] == 0 and prior["squad"] in ("SA","SB"):
            promoted_user_id = promote_one_non_commander(conn, ev, prior["team"], prior["squad"])
    await refresh_roster_message(interaction.guild, name)
    msg = "You have left the event."
    if promoted_user_id:
        member = interaction.guild.get_member(promoted_user_id)
        msg += f" Promoted {member.mention if member else f'<@{promoted_user_id}>'} to main on {team_label(ev, prior['team'])} — {'Squad A' if prior['squad']=='SA' else 'Squad B'}."
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(description="Promote the earliest team backup to a squad's main (manager only, non-commander).")
@app_commands.describe(name="Event name", team="A or B", squad="Squad A or B")
async def promote(
    interaction: discord.Interaction,
    name: str,
    team: app_commands.Transform[str, TeamChoice],
    squad: app_commands.Transform[str, SquadChoice]
):
    uid = None
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return
        uid = promote_one_non_commander(conn, ev, team, squad)
        if not uid:
            await interaction.response.send_message(
                f"No backups to promote or squad mains are at capacity for {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}.",
                ephemeral=True
            )
            return
    await refresh_roster_message(interaction.guild, name)
    member = interaction.guild.get_member(uid)
    await interaction.response.send_message(
        f"Promoted {member.mention if member else f'<@{uid}>'} to main (non-commander) on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}.",
        ephemeral=True
    )

@tree.command(description="Show the roster (ephemeral) and refresh the live message.")
async def roster(interaction: discord.Interaction, name: str):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        embed = roster_embed(ev, interaction.guild)
    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(description="Export roster as CSV (manager only).")
async def export(interaction: discord.Interaction, name: str):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return
        lines = ["team_label,team_code,squad,slot_type,is_commander,user_id,mention"]
        for code in ["A", "B"][:ev["teams"]]:
            label = team_label(ev, code)
            commanders_sa, mains_sa, commanders_sb, mains_sb, backups = get_roster(conn, ev["id"], code)
            for uid in commanders_sa:
                lines.append(f"{label},{code},SA,main,1,{uid},@{uid}")
            for uid in mains_sa:
                lines.append(f"{label},{code},SA,main,0,{uid},@{uid}")
            for uid in commanders_sb:
                lines.append(f"{label},{code},SB,main,1,{uid},@{uid}")
            for uid in mains_sb:
                lines.append(f"{label},{code},SB,main,0,{uid},@{uid}")
            for uid in backups:
                lines.append(f"{label},{code},,backup,0,{uid},@{uid}")
        data = "\n".join(lines).encode("utf-8")
        file = discord.File(fp=io.BytesIO(data), filename=f"{ev['name']}_roster.csv")
    await interaction.response.send_message(content="Export complete.", file=file, ephemeral=True)

# --- Reset event roster ---
def reset_event_roster(conn, event_id: int):
    c = conn.cursor()
    c.execute("DELETE FROM rosters WHERE event_id=?", (event_id,))
    c.execute("UPDATE events SET status='open' WHERE id=?", (event_id,))

@tree.command(description="Reset the event: clears all mains/backups and re-opens signups (manager only).")
@app_commands.describe(
    name="Event name to reset",
    clear_message="If true, delete the live roster message (bot will recreate next update)."
)
async def event_reset(interaction: discord.Interaction, name: str, clear_message: bool = False):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return

        reset_event_roster(conn, ev["id"])

        if clear_message and ev["display_channel_id"] and ev["display_message_id"]:
            channel = interaction.guild.get_channel(ev["display_channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(ev["display_message_id"])
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
            c = conn.cursor()
            c.execute("UPDATE events SET display_message_id=NULL WHERE id=?", (ev["id"],))

    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message(
        "Event reset: cleared all sign-ups and re-opened. Live roster message updated." + (" (Recreated.)" if clear_message else ""),
        ephemeral=True
    )

# --- Team time role gate + setters ---
@tree.command(description="Set which role can edit Team times (manager only).")
@app_commands.describe(name="Event name", role="Role allowed to edit team times")
async def event_settimerole(interaction: discord.Interaction, name: str, role: discord.Role):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return
        c = conn.cursor()
        c.execute("UPDATE events SET authorized_role_id=? WHERE id=?", (role.id, ev["id"]))
    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message(
        f"Updated: only members with {role.mention} (or managers/admins) can edit Team times.",
        ephemeral=True
    )

@tree.command(description="Set/clear the time for Team 1 or Team 2. Use as_unix to render Discord timestamps.")
@app_commands.describe(
    name="Event name",
    team="A or B (A = Team 1, B = Team 2)",
    when="Time text (e.g., 'Sat 7pm AEST') or epoch seconds (if as_unix=true)",
    as_unix="Store as UNIX epoch seconds to render as a Discord timestamp",
    clear="Clear the time for the team"
)
async def event_setteamtime(
    interaction: discord.Interaction,
    name: str,
    team: app_commands.Transform[str, TeamChoice],
    when: str = "",
    as_unix: bool = False,
    clear: bool = False
):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return

        if not has_time_edit_permission(ev, interaction.user):
            await interaction.response.send_message(
                "You don't have permission to edit team times for this event.",
                ephemeral=True
            )
            return

        c = conn.cursor()
        if clear:
            if team == "A":
                c.execute("UPDATE events SET team_a_time_text=NULL, team_a_time_unix=NULL WHERE id=?", (ev["id"],))
            else:
                c.execute("UPDATE events SET team_b_time_text=NULL, team_b_time_unix=NULL WHERE id=?", (ev["id"],))
            action_msg = f"Cleared time for {team_label(ev, team)}."
        else:
            if as_unix:
                try:
                    epoch = int(when.strip())
                    if team == "A":
                        c.execute("UPDATE events SET team_a_time_unix=?, team_a_time_text=NULL WHERE id=?", (epoch, ev["id"]))
                    else:
                        c.execute("UPDATE events SET team_b_time_unix=?, team_b_time_text=NULL WHERE id=?", (epoch, ev["id"]))
                    action_msg = f"Set time for {team_label(ev, team)} to `<t:{epoch}:F>`."
                except ValueError:
                    await interaction.response.send_message(
                        "Invalid epoch. Provide UNIX seconds (e.g., `1737270000`) or set `as_unix:false` to store text.",
                        ephemeral=True
                    )
                    return
            else:
                clean = when.strip()
                if not clean:
                    await interaction.response.send_message("Please provide a non-empty time string.", ephemeral=True)
                    return
                if team == "A":
                    c.execute("UPDATE events SET team_a_time_text=?, team_a_time_unix=NULL WHERE id=?", (clean, ev["id"]))
                else:
                    c.execute("UPDATE events SET team_b_time_text=?, team_b_time_unix=NULL WHERE id=?", (clean, ev["id"]))
                action_msg = f"Set time for {team_label(ev, team)} to: **{clean}**"

    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message(action_msg + " Live roster updated.", ephemeral=True)

# --- Commander management (manager/admin only, squad-aware) ---
@tree.command(description="Assign a commander to a team & squad (counts inside squad mains; respects per-squad quotas).")
@app_commands.describe(
    name="Event name",
    team="A or B (A = Team 1, B = Team 2)",
    squad="Squad A or B",
    user="Member to assign as commander"
)
async def event_setcommander(
    interaction: discord.Interaction,
    name: str,
    team: app_commands.Transform[str, TeamChoice],
    squad: app_commands.Transform[str, SquadChoice],
    user: discord.Member
):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return

        # Capacity checks per squad
        commanders_sa, mains_sa, commanders_sb, mains_sb, backups = get_team_counts(conn, ev, team)
        if squad == "SA":
            if commanders_sa >= int(ev["squad_a_commander_quota"] or 0):
                await interaction.response.send_message(f"{team_label(ev, team)} — Squad A already has the maximum of {ev['squad_a_commander_quota']} commanders.", ephemeral=True)
                return
            total_in_squad = commanders_sa + mains_sa
            if total_in_squad >= int(ev["squad_a_size"]):
                await interaction.response.send_message(f"{team_label(ev, team)} — Squad A is at full capacity ({ev['squad_a_size']}).", ephemeral=True)
                return
        else:
            if commanders_sb >= int(ev["squad_b_commander_quota"] or 0):
                await interaction.response.send_message(f"{team_label(ev, team)} — Squad B already has the maximum of {ev['squad_b_commander_quota']} commanders.", ephemeral=True)
                return
            total_in_squad = commanders_sb + mains_sb
            if total_in_squad >= int(ev["squad_b_size"]):
                await interaction.response.send_message(f"{team_label(ev, team)} — Squad B is at full capacity ({ev['squad_b_size']}).", ephemeral=True)
                return

        existing = user_enrollment(conn, ev["id"], user.id)
        c = conn.cursor()

        if existing:
            if existing["team"] != team:
                await interaction.response.send_message(f"{user.mention} is registered on {team_label(ev, existing['team'])}. Ask them to /leave first.", ephemeral=True)
                return
            if existing["slot_type"] == "backup":
                # Promote from backup to main commander in chosen squad
                c.execute("UPDATE rosters SET slot_type='main', squad=?, is_commander=1 WHERE event_id=? AND user_id=?", (squad, ev["id"], user.id))
                action = f"Promoted {user.mention} from backup to **commander** on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}."
            else:
                # Already main; set commander and squad (move squads if necessary)
                if existing["is_commander"] == 1 and existing["squad"] == squad:
                    await interaction.response.send_message(f"{user.mention} is already a commander on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}.", ephemeral=True)
                    return
                c.execute("UPDATE rosters SET is_commander=1, squad=? WHERE event_id=? AND user_id=?", (squad, ev["id"], user.id))
                action = f"Set {user.mention} as **commander** on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}."
        else:
            # Not enrolled: add directly as main commander in chosen squad
            c.execute(
                "INSERT INTO rosters(event_id, user_id, team, squad, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?,?)",
                (ev["id"], user.id, team, squad, "main", 1, int(time.time()))
            )
            action = f"Added {user.mention} as **commander** on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}."

    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message(action + " Live roster updated.", ephemeral=True)

@tree.command(description="Remove commander status from a user (squad-aware). Optionally demote to backup if needed.")
@app_commands.describe(
    name="Event name",
    team="A or B",
    user="Commander to unset",
    demote_if_needed="If non-commander mains would exceed squad cap, demote the user to backup automatically"
)
async def event_unsetcommander(
    interaction: discord.Interaction,
    name: str,
    team: app_commands.Transform[str, TeamChoice],
    user: discord.Member,
    demote_if_needed: bool = True
):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
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

        squad = existing["squad"] or "SA"
        current_non_cmd = count_mains(conn, ev["id"], team, squad, non_commanders_only=True)
        c = conn.cursor()

        # Removing commander converts them to normal main if squad capacity allows
        if current_non_cmd + 1 <= non_commander_cap(ev, squad):
            c.execute("UPDATE rosters SET is_commander=0 WHERE event_id=? AND user_id=?", (ev["id"], user.id))
            action = f"Unset commander: {user.mention} is now a normal **main** on {team_label(ev, team)} — {'Squad A' if squad=='SA' else 'Squad B'}."
        else:
            if demote_if_needed:
                backups = count_backups(conn, ev["id"], team)
                if backups < ev["backup_size"]:
                    c.execute("UPDATE rosters SET is_commander=0, squad=NULL, slot_type='backup' WHERE event_id=? AND user_id=?", (ev["id"], user.id))
                    action = f"Unset commander and **demoted to backup** (squad mains were full) for {user.mention} on {team_label(ev, team)}."
                else:
                    await interaction.response.send_message(
                        "Cannot unset: squad non-commander mains are full and backups are also full. Free a slot or disable demote_if_needed.",
                        ephemeral=True
                    )
                    return
            else:
                await interaction.response.send_message(
                    "Cannot unset: squad non-commander mains are full. Enable demote_if_needed or free a main slot.",
                    ephemeral=True
                )
                return

    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message(action + " Live roster updated.", ephemeral=True)

# --- Team label configuration (per event) ---
@tree.command(description="Set the display labels for Team 1 (A) and Team 2 (B).")
@app_commands.describe(
    name="Event name",
    team1_label="Label for Team A (e.g., 'Shadowfront Team 1')",
    team2_label="Label for Team B (e.g., 'Shadowfront Team 2')"
)
async def event_setteamlabels(
    interaction: discord.Interaction,
    name: str,
    team1_label: str,
    team2_label: str
):
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return
        c = conn.cursor()
        c.execute("UPDATE events SET team_a_label=?, team_b_label=? WHERE id=?", (team1_label.strip(), team2_label.strip(), ev["id"]))
    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message(
        f"Updated team labels:\n• Team A → **{team1_label}**\n• Team B → **{team2_label}**\nLive roster updated.",
        ephemeral=True
    )

# --- Auto-refresh configuration (per event) ---
@tree.command(description="Configure weekly auto-refresh for the roster (manager only).")
@app_commands.describe(
    name="Event name",
    enable="Enable or disable auto-refresh",
    day="Day code (MON, TUE, WED, THU, FRI, SAT, SUN)",
    hour="Hour in 24h format (0-23) local to the selected timezone",
    tz="IANA timezone (default 'Australia/Brisbane')"
)
async def event_setautorefresh(
    interaction: discord.Interaction,
    name: str,
    enable: bool = True,
    day: str = "MON",
    hour: int = 9,
    tz: str = "Australia/Brisbane"
):
    valid_days = {"MON","TUE","WED","THU","FRI","SAT","SUN"}
    day = day.upper()
    if day not in valid_days:
        await interaction.response.send_message("Invalid day. Use one of: MON,TUE,WED,THU,FRI,SAT,SUN.", ephemeral=True)
        return
    if hour < 0 or hour > 23:
        await interaction.response.send_message("Invalid hour. Use 0-23.", ephemeral=True)
        return

    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
        if not ev:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        if not user_is_event_manager_or_admin(ev, interaction.user):
            await interaction.response.send_message("You must be an event manager or have Manage Server.", ephemeral=True)
            return

        # Validate timezone if ZoneInfo is available
        if ZoneInfo:
            try:
                _ = ZoneInfo(tz)
            except Exception:
                await interaction.response.send_message("Invalid timezone. Provide a valid IANA timezone (e.g., 'Australia/Brisbane').", ephemeral=True)
                return
        c = conn.cursor()
        c.execute("""
            UPDATE events
            SET auto_refresh_enabled=?, auto_refresh_day=?, auto_refresh_hour=?, auto_refresh_tz=?
            WHERE id=?
        """, (1 if enable else 0, day, hour, tz, ev["id"]))
    await interaction.response.send_message(
        f"Auto-refresh {'enabled' if enable else 'disabled'} for **{name}**: {day} @ {hour:02d}:00 ({tz}).",
        ephemeral=True
    )

bot.run(TOKEN)
