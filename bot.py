
# bot.py
# Discord Guild Teams Manager — Shadowfront Teams with Squads + Fixed UTC time slots
# Buttons reduced to: Team 1, Team 2 (if applicable), Leave

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
    ZoneInfo = None  # Fallback; timestamps still render locally via Discord

# --- Config / Tokens ---
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Please set DISCORD_TOKEN environment variable.")

DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # optional: for instant guild-scoped /sync_here
dev_guild = None
if DEV_GUILD_ID:
    try:
        dev_guild = discord.Object(id=int(DEV_GUILD_ID))
    except ValueError:
        dev_guild = None

INTENTS = discord.Intents.default()
INTENTS.members = True  # mention resolution

# Slash-only bot (no prefix) to avoid needing Message Content intent
bot = commands.Bot(command_prefix=None, intents=INTENTS, help_command=None)
tree = bot.tree

# Use Railway Volume for persistence if available
DB_PATH = os.getenv("DB_PATH", "guild_teams.db")

# ---- Database helpers ----

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
            teams INTEGER NOT NULL DEFAULT 2,          -- 1 or 2
            status TEXT NOT NULL DEFAULT 'open',       -- open|locked|closed
            created_by INTEGER NOT NULL,
            display_channel_id INTEGER,
            display_message_id INTEGER,
            UNIQUE(guild_id, name)
        );
        """)
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS rosters(
            event_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            team TEXT NOT NULL,            -- 'A' or 'B'
            squad TEXT,                    -- 'SA' or 'SB' for mains; NULL for backups
            slot_type TEXT NOT NULL,       -- 'main' or 'backup'
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
        add_missing_columns(conn)

def add_missing_columns(conn: sqlite3.Connection):
    """Ensure newer columns exist in older DBs; backfill safe defaults."""
    c = conn.cursor()
    c.execute("PRAGMA table_info(events)")
    ecols = {row[1] for row in c.fetchall()}
    c.execute("PRAGMA table_info(rosters)")
    rcols = {row[1] for row in c.fetchall()}

    # Labels, role gate for time editing, auto-refresh, squads, commander quotas
    if "team_a_label" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN team_a_label TEXT")
    if "team_b_label" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN team_b_label TEXT")

    if "authorized_role_id" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN authorized_role_id INTEGER")

    # Fixed UTC slot selection per team ('0900'|'1800'|'2300')
    if "team_a_slot" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN team_a_slot TEXT")
    if "team_b_slot" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN team_b_slot TEXT")

    # Commander quotas (team total) + squad structure
    if "commander_quota" not in ecols:
        c.execute("ALTER TABLE events ADD COLUMN commander_quota INTEGER DEFAULT 3")
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

    # Rosters: ensure squad & is_commander
    if "squad" not in rcols:
        c.execute("ALTER TABLE rosters ADD COLUMN squad TEXT")
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

# ---- Fixed UTC Slot time utilities ----

FIXED_SLOTS = {"0900": (9, 0), "1800": (18, 0), "2300": (23, 0)}  # hours, minutes (UTC)

def next_epoch_for_slot(slot: Optional[str]) -> Optional[int]:
    """Return epoch seconds for the next occurrence of the given UTC slot (today or tomorrow)."""
    if not slot or slot not in FIXED_SLOTS:
        return None
    h, m = FIXED_SLOTS[slot]
    now_utc = datetime.now(timezone.utc)
    today_slot = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
    if today_slot > now_utc:
        target = today_slot
    else:
        target = today_slot + timedelta(days=1)
    return int(target.timestamp())

def team_slot(ev: sqlite3.Row, team_code: str) -> Optional[str]:
    return ev["team_a_slot"] if team_code == "A" else ev["team_b_slot"]

def event_tz(ev: sqlite3.Row):
    """Return ZoneInfo for the event's reference timezone (used only for button 'L' display)."""
    tzname = ev["auto_refresh_tz"] if "auto_refresh_tz" in ev.keys() and ev["auto_refresh_tz"] else "Australia/Brisbane"
    try:
        return ZoneInfo(tzname) if ZoneInfo else timezone.utc
    except Exception:
        return ZoneInfo("Australia/Brisbane") if ZoneInfo else timezone.utc

def local_hhmm_no_colon(ev: sqlite3.Row, slot: Optional[str]) -> str:
    """Compute next occurrence of the UTC slot, convert to event tz, format HHMM (no colon)."""
    epoch = next_epoch_for_slot(slot)
    if not epoch:
        return "----"
    tz = event_tz(ev)
    dt_local = datetime.fromtimestamp(epoch, tz=tz)
    return dt_local.strftime("%H%M")

def slot_hhmm_no_colon(slot: Optional[str]) -> str:
    return slot if slot in FIXED_SLOTS else "----"

def button_dual_time_label(ev: sqlite3.Row, team_code: str) -> str:
    """
    Build label segment: (L 0900 UTC 2300)
    L = event tz (not per-viewer; Discord buttons cannot render per-user local)
    UTC = slot string
    """
    slot = team_slot(ev, team_code)
    l_val = local_hhmm_no_colon(ev, slot)
    u_val = slot_hhmm_no_colon(slot)
    return f"(L {l_val} UTC {u_val})"

def embed_time_for_team(ev: sqlite3.Row, team_code: str) -> str:
    """
    Show <t:epoch:F> (<t:epoch:R>) for next occurrence of the team's UTC slot.
    Each viewer sees local time automatically (Discord feature).
    """
    slot = team_slot(ev, team_code)
    epoch = next_epoch_for_slot(slot)
    if epoch:
        return f"<t:{epoch}:F> (<t:{epoch}:R>)"
    return "_Not set_"

# ---- Roster logic ----

def add_participant(conn, ev: sqlite3.Row, user_id: int, team: str, squad: Optional[str] = None, force_backup: bool = False) -> Tuple[str, str]:
    """
    Join flow (non-commander):
      - If squad provided ('SA'/'SB'): try that squad non-commander cap, else fallback to team backup.
      - If no squad and not force_backup: try Squad A → Squad B → backup.
      - If force_backup: only backup.
    """
    if ev["status"] != "open":
        return ("", "This event is currently locked. Ask a manager to /event_unlock.")  # lock/unlock command still exists

    existing = user_enrollment(conn, ev["id"], user_id)
    if existing:
        if existing["team"] == team:
            if existing["slot_type"] == "main":
                current_label = f"{team_label(ev, team)} — {'Squad A' if existing['squad']=='SA' else 'Squad B'}"
            else:
                current_label = f"{team_label(ev, team)} (backup)"
            return (existing["slot_type"], f"You are already on {current_label}.")
        else:
            return ("", f"You are already registered on {team_label(ev, existing['team'])}. Leave first with /leave.")

    _, mains_sa, _, mains_sb, backups = get_team_counts(conn, ev, team)

    def can_join_non_cmd(target_squad: str) -> bool:
        return count_mains(conn, ev["id"], team, target_squad, non_commanders_only=True) < non_commander_cap(ev, target_squad)

    c = conn.cursor()

    if force_backup:
        if backups < ev["backup_size"]:
            c.execute(
                "INSERT INTO rosters(event_id, user_id, team, squad, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?,?)",
                (ev["id"], user_id, team, None, "backup", 0, int(time.time()))
            )
            return ("backup", "joined")
        else:
            return ("", f"{team_label(ev, team)} backups are full.")

    if squad in ("SA", "SB"):
        if can_join_non_cmd(squad):
            c.execute(
                "INSERT INTO rosters(event_id, user_id, team, squad, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?,?)",
                (ev["id"], user_id, team, squad, "main", 0, int(time.time()))
            )
            return ("main", "joined")
        if backups < ev["backup_size"]:
            c.execute(
                "INSERT INTO rosters(event_id, user_id, team, squad, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?,?)",
                (ev["id"], user_id, team, None, "backup", 0, int(time.time()))
            )
            return ("backup", "joined")
        return ("", f"{team_label(ev, team)} is full (chosen squad mains and backups).")

    # No squad requested: try A → B → backup
    if can_join_non_cmd("SA"):
        c.execute(
            "INSERT INTO rosters(event_id, user_id, team, squad, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?,?)",
            (ev["id"], user_id, team, "SA", "main", 0, int(time.time()))
        )
        return ("main", "joined")

    if can_join_non_cmd("SB"):
        c.execute(
            "INSERT INTO rosters(event_id, user_id, team, squad, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?,?)",
            (ev["id"], user_id, team, "SB", "main", 0, int(time.time()))
        )
        return ("main", "joined")

    if backups < ev["backup_size"]:
        c.execute(
            "INSERT INTO rosters(event_id, user_id, team, squad, slot_type, is_commander, joined_at) VALUES (?,?,?,?,?,?,?)",
            (ev["id"], user_id, team, None, "backup", 0, int(time.time()))
        )
        return ("backup", "joined")

    return ("", f"{team_label(ev, team)} is full (mains and backups).")

def remove_participant(conn, event_id: int, user_id: int) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute("SELECT * FROM rosters WHERE event_id=? AND user_id=?", (event_id, user_id))
    row = c.fetchone()
    if not row:
        return None
    c.execute("DELETE FROM rosters WHERE event_id=? AND user_id=?", (event_id, user_id))
    return row

def promote_one_non_commander(conn, ev: sqlite3.Row, team: str, squad: str) -> Optional[int]:
    # kept for completeness; manager buttons removed from UI but command /promote still exists if you want it
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
    return (ev["team_a_label"] or "Shadowfront Team 1") if team_code == "A" else (ev["team_b_label"] or "Shadowfront Team 2")

# ---- Embed ----

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

    with db() as conn:
        for team in ["A", "B"][:ev["teams"]]:
            label = team_label(ev, team)
            embed.add_field(name=f"{label} — Time (UTC slot)", value=embed_time_for_team(ev, team), inline=False)

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
            # Backups
            embed.add_field(
                name=f"{label} — Backups ({len(backups)}/{ev['backup_size']})",
                value=name_list(backups),
                inline=False
            )
            embed.add_field(name="\u200b", value="\u200b", inline=False)

    embed.set_footer(text="Buttons: Team 1/2, Leave • Slash: /event_setteamtime, /event_setteamlabels, /event_setchannel, /event_setautorefresh, /event_deleteall, /sync_here")
    return embed

def user_is_event_manager_or_admin(ev: sqlite3.Row, member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    with db() as conn:
        return is_manager(conn, ev["id"], member.id)

# ---- Buttons (reduced) ----

class RosterView(discord.ui.View):
    """
    Reduced UI: Team 1, Team 2 (if applicable), Leave
    """
    def __init__(self, ev: sqlite3.Row):
        super().__init__(timeout=None)
        self.event_name = ev["name"]
        self.teams_count = int(ev["teams"] or 2)

        # Row 0: Team 1 / Team 2 join
        self._add_button(f"Team 1 {button_dual_time_label(ev, 'A')}", discord.ButtonStyle.primary, 0, lambda i: self._join_auto(i, "A"))
        if self.teams_count >= 2:
            self._add_button(f"Team 2 {button_dual_time_label(ev, 'B')}", discord.ButtonStyle.primary, 0, lambda i: self._join_auto(i, "B"))

        # Row 1: Leave only
        self._add_button("Leave", discord.ButtonStyle.danger, 1, self._leave_common)

    def _add_button(self, label: str, style: discord.ButtonStyle, row: int, handler):
        btn = discord.ui.Button(label=label, style=style, row=row)
        async def _callback(interaction: discord.Interaction):
            await handler(interaction)
        btn.callback = _callback
        self.add_item(btn)

    # Player actions
    async def _join_auto(self, interaction: discord.Interaction, team: str):
        with db() as conn:
            ev = get_event(conn, interaction.guild_id, self.event_name)
            if not ev:
                await interaction.response.send_message("Event not found.", ephemeral=True)
                return
            slot_type, note = add_participant(conn, ev, interaction.user.id, team, squad=None, force_backup=False)
            if not slot_type:
                await interaction.response.send_message(note, ephemeral=True)
                return
        await refresh_roster_message(interaction.guild, self.event_name)
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
        prior = None
        with db() as conn:
            ev = get_event(conn, interaction.guild_id, self.event_name)
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
            if prior["slot_type"] == "main" and prior["is_commander"] == 0 and prior["squad"] in ("SA","SB"):
                promoted_user_id = promote_one_non_commander(conn, ev, prior["team"], prior["squad"])
        await refresh_roster_message(interaction.guild, self.event_name)
        msg = "You have left the event."
        if promoted_user_id:
            member = interaction.guild.get_member(promoted_user_id)
            msg += f" Promoted {member.mention if member else f'<@{promoted_user_id}>'} to main on {team_label(ev, prior['team'])} — {'Squad A' if prior['squad']=='SA' else 'Squad B'}."
        await interaction.response.send_message(msg, ephemeral=True)

# ---- Live roster message helpers ----

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

# ---- Startup ----

@bot.event
async def on_ready():
    init_db()
    try:
        # Show the commands we have in memory before syncing
        cmd_names = [cmd.name for cmd in tree.get_commands()]
        print(f"Loaded {len(cmd_names)} commands in memory: {cmd_names}")

        # Reattach views for all events in all guilds
        for g in bot.guilds:
            with db() as conn:
                for ev in list_events_for_guild(conn, g.id):
                    try:
                        await ensure_roster_message(ev, g)
                    except Exception as e:
                        print(f"Failed to attach view for event '{ev['name']}' in guild {g.id}: {e}")

        # Start weekly refresh loop
        if not weekly_refresh_task.is_running():
            weekly_refresh_task.start()

        # Prefer dev guild instant sync if DEV_GUILD_ID is set
        if dev_guild:
            try:
                synced = await tree.sync(guild=dev_guild)
                print(f"[DEV] Synced {len(synced)} commands to guild {DEV_GUILD_ID}")
            except Exception as e:
                print("[DEV] Per-guild sync error:", e)
        else:
            # Otherwise, sync to each joined guild
            for g in bot.guilds:
                try:
                    synced = await tree.sync(guild=g)
                    print(f"Synced {len(synced)} commands to guild {g.id}")
                except Exception as e:
                    print("Per-guild sync error:", e)

        print("Per-guild command sync complete.")
    except Exception as e:
        print("Startup error:", e)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ---- Scheduled weekly auto-refresh ----

def map_weekday_name(dt: datetime) -> str:
    return ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][dt.weekday()]

@tasks.loop(minutes=10)
async def weekly_refresh_task():
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

# ---- Slash Commands (kept) ----

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

@tree.command(description="Create a new event (Squad A=15 (2 cmdrs), Squad B=5 (1 cmdr), backups=10).")
@app_commands.describe(
    name="Event name (unique per server)",
    starts_at="Optional date/time text",
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
    await interaction.response.defer(ephemeral=True)

    if teams < 1 or teams > 2:
        await interaction.followup.send("For now, teams must be 1 or 2.", ephemeral=True)
        return
    if squad_a_size < 0 or squad_b_size < 0 or backup_size < 0:
        await interaction.followup.send("Sizes cannot be negative.", ephemeral=True)
        return
    total_team_size = squad_a_size + squad_b_size
    with db() as conn:
        try:
            c = conn.cursor()
            c.execute("""
                INSERT INTO events(guild_id, name, starts_at, team_size, backup_size, teams, status, created_by,
                                   display_channel_id, display_message_id,
                                   team_a_label, team_b_label,
                                   squad_a_size, squad_b_size, squad_a_commander_quota, squad_b_commander_quota, commander_quota,
                                   team_a_slot, team_b_slot)
                VALUES (?,?,?,?,?,?, 'open', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """, (
                interaction.guild_id, name, starts_at.strip(), total_team_size, backup_size, teams,
                interaction.user.id, channel.id if channel else None,
                "Shadowfront Team 1", "Shadowfront Team 2",
                squad_a_size, squad_b_size, squad_a_cmdrs, squad_b_cmdrs, squad_a_cmdrs + squad_b_cmdrs
            ))
            event_id = c.lastrowid
            c.execute("INSERT INTO managers(event_id, user_id) VALUES (?,?)", (event_id, interaction.user.id))
        except sqlite3.IntegrityError:
            await interaction.followup.send("An event with that name already exists here.", ephemeral=True)
            return

    if channel:
        with db() as conn:
            ev = get_event(conn, interaction.guild_id, name)
        try:
            await ensure_roster_message(ev, interaction.guild)
        except Exception as e:
            print("ensure_roster_message error:", e)
            await interaction.followup.send(
                "Event created, but failed to post the live roster message. "
                "Please check the bot’s permissions in the target channel (Send Messages, Embed Links).",
                ephemeral=True
            )
            return
        await interaction.followup.send(
            f"Event **{name}** created. Live roster posted in {channel.mention}.",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"Event **{name}** created. Use `/event_setchannel` to choose a display channel.",
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

# Team time setter (fixed UTC slots)
@tree.command(description="Set the time slot for Team 1 or Team 2 (choose 09:00, 18:00, or 23:00 UTC).")
@app_commands.describe(
    name="Event name",
    team="A or B (A = Team 1, B = Team 2)",
    slot="One of 09:00, 18:00, 23:00 UTC"
)
@app_commands.choices(slot=[
    app_commands.Choice(name="09:00 UTC", value="0900"),
    app_commands.Choice(name="18:00 UTC", value="1800"),
    app_commands.Choice(name="23:00 UTC", value="2300"),
])
async def event_setteamtime(
    interaction: discord.Interaction,
    name: str,
    team: app_commands.Transform[str, TeamChoice],
    slot: str
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
        if team == "A":
            c.execute("UPDATE events SET team_a_slot=? WHERE id=?", (slot, ev["id"]))
        else:
            c.execute("UPDATE events SET team_b_slot=? WHERE id=?", (slot, ev["id"]))

    label = {"0900": "09:00 UTC", "1800": "18:00 UTC", "2300": "23:00 UTC"}.get(slot, slot)
    await refresh_roster_message(interaction.guild, name)
    await interaction.response.send_message(
        f"Set **{team_label(ev, team)}** time to **{label}**. Live roster updated.",
        ephemeral=True
    )

# Roster/user actions (slash)
@tree.command(description="Join a team (auto: Squad A → Squad B → backup).")
@app_commands.describe(name="Event name", team="A or B", squad="(optional) Squad A or B")
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
        requested_squad = squad if squad in ("SA","SB") else None
        slot_type, note = add_participant(conn, ev, interaction.user.id, team, requested_squad, False)
        if not slot_type:
            await interaction.response.send_message(note, ephemeral=True)
            return
    await refresh_roster_message(interaction.guild, name)
    if slot_type == "backup":
        await interaction.response.send_message(f"You joined **{team_label(ev, team)}** as **backup**.", ephemeral=True)
    else:
        with db() as conn:
            rec = user_enrollment(conn, ev["id"], interaction.user.id)
        sq = rec["squad"] if rec else "SA"
        await interaction.response.send_message(f"You joined **{team_label(ev, team)} — {'Squad A' if sq=='SA' else 'Squad B'}** as **main**.", ephemeral=True)

@tree.command(description="Leave the event (removes you from main/backup).")
async def leave(interaction: discord.Interaction, name: str):
    promoted_user_id = None
    prior = None
    with db() as conn:
        ev = get_event(conn, interaction.guild_id, name)
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
        if prior["slot_type"] == "main" and prior["is_commander"] == 0 and prior["squad"] in ("SA","SB"):
            promoted_user_id = promote_one_non_commander(conn, ev, prior["team"], prior["squad"])
    await refresh_roster_message(interaction.guild, name)
    msg = "You have left the event."
    if promoted_user_id:
        member = interaction.guild.get_member(promoted_user_id)
        msg += f" Promoted {member.mention if member else f'<@{promoted_user_id}>'} to main on {team_label(ev, prior['team'])} — {'Squad A' if prior['squad']=='SA' else 'Squad B'}."
    await interaction.response.send_message(msg, ephemeral=True)

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

@tree.command(description="Delete ALL events for this server (admin only; cannot be undone).")
async def event_deleteall(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You must have **Manage Server** to delete all events.",
            ephemeral=True
        )
        return
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, display_channel_id, display_message_id, name FROM events WHERE guild_id=?", (interaction.guild_id,))
        rows = c.fetchall()
        if not rows:
            await interaction.response.send_message("There are no events to delete for this server.", ephemeral=True)
            return
        deleted_msgs = 0
        for row in rows:
            ch_id = row["display_channel_id"]
            msg_id = row["display_message_id"]
            if ch_id and msg_id:
                channel = interaction.guild.get_channel(ch_id)
                if channel:
                    try:
                        msg = await channel.fetch_message(msg_id)
                        await msg.delete()
                        deleted_msgs += 1
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
        c.execute("DELETE FROM events WHERE guild_id=?", (interaction.guild_id,))
    await interaction.response.send_message(
        f"✅ Deleted **{len(rows)}** event(s) for this server. Removed **{deleted_msgs}** roster message(s) where possible.",
        ephemeral=True
    )

# ---- Sync helpers ----

if dev_guild:
    @tree.command(name="sync_here", description="Guild-scoped sync to this server (admin only).", guild=dev_guild)
    async def sync_here(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You must have **Manage Server** to run /sync_here.",
                ephemeral=True
            )
            return
        try:
            synced = await tree.sync(guild=interaction.guild)
            await interaction.response.send_message(
                f"✅ Synced **{len(synced)}** command(s) to this server (guild-scoped).",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ sync_here failed: `{e}`",
                ephemeral=True
            )

@tree.command(description="Sync slash commands to this server (admin only).")
async def sync(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You must have **Manage Server** to run /sync.",
            ephemeral=True
        )
        return
    try:
        synced = await tree.sync(guild=interaction.guild)
        await interaction.response.send_message(
            f"✅ Synced **{len(synced)}** command(s) to this server.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Sync failed: `{e}`",
            ephemeral=True
        )

@tree.command(description="Full re-sync of commands to this server (admin only).")
async def sync_full(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You must have **Manage Server** to run /sync_full.",
            ephemeral=True
        )
        return
    try:
        await tree.clear_commands(guild=interaction.guild)
        await tree.copy_global_to(guild=interaction.guild)
        synced = await tree.sync(guild=interaction.guild)
        await interaction.response.send_message(
            f"✅ Full re-sync complete: **{len(synced)}** command(s) updated.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Sync failed: `{e}`",
            ephemeral=True
        )

bot.run(TOKEN)
``
