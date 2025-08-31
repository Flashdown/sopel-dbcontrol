# sopel-dbcontrol v0.2 Copyright (C) 2025 Enrico Heine https://github.com/Flashdown/sopel-dbcontrol
# Licensed under the Eiffel Forum License 2.

import sopel.plugin
import sqlite3
import time
import re
from datetime import datetime
from collections import defaultdict, deque

DB_PATH = '~/.sopel/chat.db'  # Passe an, z.B. '/path/to/chat.db'
LOG_FILTER_REGEX = re.compile(r'([\x00-\x1F\x7F])') # Skippe Control-Chars
COMMAND_SANITIZE_REGEX = re.compile(r'[\x00-\x1F\x7F]')  # Entferne Control-Chars

# Rate Limiting Settings
RATE_SHORT_WINDOW = 10  # Sekunden
RATE_SHORT_LIMIT = 10  # Messages in short window
RATE_LONG_WINDOW = 60  # Sekunden
RATE_LONG_LIMIT = 50  # Messages in long window
BAN_DURATION = 180  # Sekunden (3 Minuten)

# Globale Tracker (stateful im Plugin)
user_message_times = defaultdict(lambda: deque())  # user: deque of timestamps
banned_users = {}  # user: expiry timestamp

# DB-Setup (einmalig)
def setup_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")  # Aktiviere WAL für Concurrency
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            sender TEXT NOT NULL,
            channel TEXT NOT NULL,
            content TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            message TEXT NOT NULL,
            sent INTEGER DEFAULT 0,
            timestamp REAL DEFAULT 0
        )
    ''')
    try:
        cursor.execute('ALTER TABLE pending_messages ADD COLUMN timestamp REAL DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS channel_users (
            channel TEXT NOT NULL,
            nick TEXT NOT NULL,
            flags TEXT DEFAULT '',
            PRIMARY KEY (channel, nick)
        )
    ''')
    try:
        cursor.execute('ALTER TABLE channel_users ADD COLUMN flags TEXT DEFAULT ""')
    except sqlite3.OperationalError:
        pass
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_pchats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            botuser TEXT NOT NULL,
            user TEXT NOT NULL,
            UNIQUE (botuser, user)
        )
    ''')
    conn.commit()
    conn.close()

setup_db()  # Rufe beim Modul-Laden

# Hilfsfunktion zum Loggen von Events
def log_event(channel, sender, content):
    timestamp = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO messages (timestamp, sender, channel, content)
        VALUES (?, ?, ?, ?)
    ''', (timestamp, sender, channel, content))
    conn.commit()
    conn.close()

# Hilfsfunktion zum Überprüfen und Hinzufügen aktiver privater Chats
def ensure_active_pchat(bot, user):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM active_pchats WHERE botuser = ? AND user = ?', (bot.nick, user))
    if not cursor.fetchone():
        cursor.execute('INSERT INTO active_pchats (botuser, user) VALUES (?, ?)', (bot.nick, user))
    conn.commit()
    conn.close()

# Logging: Hook PRIVMSG-Event (inkl. /me Actions)
@sopel.plugin.event('PRIVMSG')
@sopel.plugin.rule('.*')
def log_message(bot, trigger):
    channel = trigger.sender
    sender = trigger.nick
    content = trigger.group(0)
    # Rate Limiting Check
    now = time.time()
    if sender in banned_users:
        if now > banned_users[sender]:
            del banned_users[sender]
            user_message_times[sender].clear()
        else:
            return
    user_message_times[sender].append(now)
    while user_message_times[sender] and user_message_times[sender][0] < now - RATE_LONG_WINDOW:
        user_message_times[sender].popleft()
    short_count = sum(1 for ts in user_message_times[sender] if ts > now - RATE_SHORT_WINDOW)
    long_count = len(user_message_times[sender])
    if short_count > RATE_SHORT_LIMIT or long_count > RATE_LONG_LIMIT:
        banned_users[sender] = now + BAN_DURATION
        return
    if LOG_FILTER_REGEX.search(content):
        return
    if trigger.ctcp == 'ACTION':
        content = f"* {sender} {content}"
    # Bestimme den log_channel (für PMs: immer der User-Nick)
    log_channel = channel
    if not channel.startswith('#'):
        log_channel = sender
    log_event(log_channel, sender, content)
    if not channel.startswith('#'):
        ensure_active_pchat(bot, sender)

# JOIN Event (für Users und Bot – log Topic wenn Bot joint)
@sopel.plugin.event('JOIN')
def log_join(bot, trigger):
    channel = trigger.sender
    sender = trigger.nick
    if not channel.startswith('#'):
        return
    content = f"* {sender} has joined {channel}"
    log_event(channel, 'SYSTEM', content)
    if sender == bot.nick:
        topic = bot.channels[channel].topic
        if topic:
            topic_content = f"* Topic for {channel} is: {topic}"
        else:
            topic_content = f"* No topic is set for {channel}"
        log_event(channel, 'SYSTEM', topic_content)

# PART Event
@sopel.plugin.event('PART')
def log_part(bot, trigger):
    channel = trigger.sender
    sender = trigger.nick
    if not channel.startswith('#'):
        return
    reason = trigger.args[0] if trigger.args else ""
    content = f"* {sender} has left {channel} ({reason})"
    log_event(channel, 'SYSTEM', content)

# QUIT Event
@sopel.plugin.event('QUIT')
def log_quit(bot, trigger):
    sender = trigger.nick
    reason = trigger.args[0] if trigger.args else ""
    for channel in list(bot.channels.keys()):
        if sender in bot.channels[channel].users:
            content = f"* {sender} has quit ({reason})"
            log_event(channel, 'SYSTEM', content)

# MODE Event – korrigiertes Parsing mit custom Logging
@sopel.plugin.event('MODE')
def log_mode(bot, trigger):
    channel = trigger.sender
    setter = trigger.nick
    if not channel.startswith('#'):
        return
    args = trigger.args
    if len(args) < 2:
        return
    modes = args[1]
    targets = args[2:]
    if not modes or modes[0] not in '+-':
        return  # Invalid modes; skip logging
    mode_str = ''
    sign = ''
    target_index = 0
    for char in modes:
        if char in '+-':
            sign = char
            continue
        # Determine if this mode requires a target (basic check; expand if needed for your server)
        requires_target = char in 'vohqabekIl'  # Common user/channel modes that take params
        if requires_target and target_index < len(targets):
            target = targets[target_index]
            target_index += 1
        else:
            target = channel
        custom = None
        if char == 'v' and target != channel:
            custom = f" {'gives' if sign == '+' else 'removes'} voice {'to' if sign == '+' else 'from'} {target}"
        elif char == 'o' and target != channel:
            custom = f" {'gives' if sign == '+' else 'removes'} channel operator status {'to' if sign == '+' else 'from'} {target}"
        elif char == 'b' and target != channel:
            custom = f" {'bans' if sign == '+' else 'unbans'} {target}"
        elif char == 'k' and target != channel:
            if sign == '+':
                custom = f" sets channel key to {target}"
            else:
                custom = f" removes channel key (using {target})"
        if custom:
            mode_str += f" {custom};"
        else:
            action = 'sets' if sign == '+' else 'removes'
            role = f'mode {char}'
            if target != channel:
                mode_str += f" {action} {role} {'to' if sign == '+' else 'from'} {target};"
            else:
                mode_str += f" {action} {role} on {channel};"
    if mode_str:
        content = f"* {setter}{mode_str.strip(';')}"
        log_event(channel, setter, content)  # Sender als setter, nicht 'SYSTEM'

# TOPIC Event (für Changes)
@sopel.plugin.event('TOPIC')
def log_topic(bot, trigger):
    channel = trigger.sender
    setter = trigger.nick
    if not channel.startswith('#'):
        return
    new_topic = trigger.args[1] if len(trigger.args) > 1 else ''
    content = f"* {setter} changed the topic to: {new_topic}"
    log_event(channel, 'SYSTEM', content)

# KICK Event
@sopel.plugin.event('KICK')
def log_kick(bot, trigger):
    channel = trigger.sender
    kicker = trigger.nick
    if not channel.startswith('#'):
        return
    if len(trigger.args) < 2:
        return
    kicked = trigger.args[1]
    reason = trigger.args[2] if len(trigger.args) > 2 else ''
    content = f"* {kicker} kicked {kicked} ({reason})"
    log_event(channel, 'SYSTEM', content)

# Hook for ERR_CHANOPRIVSNEEDED (482)
@sopel.plugin.event('482')
def handle_chanop_error(bot, trigger):
    channel = trigger.args[1]  # #daplacetobe
    if not channel.startswith('#'):
        return
    error_msg = trigger.args[2].lstrip(':')  # "You're not channel operator" (strip leading :)
    content = f"* Command attempt failed: {error_msg}"
    log_event(channel, bot.nick, content)  # Log the failure
    print(f"DEBUG: Received 482 error for channel {channel}: {error_msg}")
    # Optionally: Notify an admin or retry logic here

# Hilfsfunktion zum Sanitizen von Command-Teilen
def sanitize_input(text):
    text = COMMAND_SANITIZE_REGEX.sub('', text)  # Entferne Control-Chars
    return text[:200]  # Begrenze Länge (anpassen nach Bedarf)

# Sending-Queue: Interval-Check – mit Command-Handling
@sopel.plugin.interval(5)
def check_queue(bot):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, channel, message FROM pending_messages WHERE sent = 0')
    rows = cursor.fetchall()
    for row in rows:
        id_, channel, message = row
        content = None  # Initialize content to None; only set for non-MODE commands
        is_channel = channel.startswith('#')
        # Entfernt: Unnötiger ensure_active_pchat für non-channel (würde bot mit bot erzeugen)
        if message.lower().startswith('/mode '):
            if not is_channel:
                continue  # Skip channel-specific commands in PMs
            mode_cmd = message.split(None, 1)[1].strip() if len(message.split(None, 1)) > 1 else ''
            parts = mode_cmd.split()
            # Unterstütze beide Formen: mit oder ohne #channel am Anfang
            if parts and parts[0].startswith('#'):
                modes = sanitize_input(parts[1])
                targets = [sanitize_input(t) for t in parts[2:]]
            else:
                modes = sanitize_input(parts[0])
                targets = [sanitize_input(t) for t in parts[1:]]
            if not modes:
                continue  # Skip invalid
            # Sende den Befehl (immer mit dem bekannten channel)
            raw_command = ['MODE', channel, modes] + targets
            bot.write(raw_command)
            print(f"DEBUG: Sending raw command: {' '.join(raw_command)}")
            # Do not set content here; let events handle logging
        elif message.lower().startswith('/topic '):
            if not is_channel:
                continue  # Skip channel-specific commands in PMs
            topic_cmd = message.split(None, 1)[1].strip() if len(message.split(None, 1)) > 1 else ''
            parts = topic_cmd.split(None, 1)
            # Unterstütze beide Formen: mit oder ohne #channel am Anfang
            if parts and parts[0].startswith('#'):
                new_topic = sanitize_input(parts[1] if len(parts) > 1 else '')
            else:
                new_topic = sanitize_input(topic_cmd)
            # Sende den Befehl (immer mit dem bekannten channel)
            raw_command = ['TOPIC', channel] + ([':' + new_topic] if new_topic else [':'])
            bot.write(raw_command)
            print(f"DEBUG: Sending raw command: {' '.join(raw_command)}")
            # Do not set content here; let events handle logging
        elif message.lower().startswith('/kick '):
            if not is_channel:
                continue  # Skip channel-specific commands in PMs
            kick_cmd = message.split(None, 1)[1].strip() if len(message.split(None, 1)) > 1 else ''
            parts = kick_cmd.split(None, 2)
            if parts and parts[0].startswith('#'):
                kicked = sanitize_input(parts[1] if len(parts) > 1 else '')
                reason = sanitize_input(parts[2] if len(parts) > 2 else '')
            else:
                kicked = sanitize_input(parts[0] if parts else '')
                reason = sanitize_input(parts[1] if len(parts) > 1 else '')
            if not kicked:
                continue  # Skip invalid
            raw_command = ['KICK', channel, kicked] + ([':' + reason] if reason else [])
            bot.write(raw_command)
            print(f"DEBUG: Sending raw command: {' '.join(raw_command)}")
            # Do not set content here; let events handle logging
        elif message.lower().startswith('/ban '):
            if not is_channel:
                continue  # Skip channel-specific commands in PMs
            ban_cmd = message.split(None, 1)[1].strip() if len(message.split(None, 1)) > 1 else ''
            parts = ban_cmd.split(None, 1)
            if parts and parts[0].startswith('#'):
                target_str = parts[1] if len(parts) > 1 else ''
            else:
                target_str = ban_cmd
            target = sanitize_input(target_str.strip())
            if not target:
                continue  # Skip invalid
            if '!' not in target and '@' not in target:
                mask = f"{target}!*@*"
            else:
                mask = target
            raw_command = ['MODE', channel, '+b', mask]
            bot.write(raw_command)
            print(f"DEBUG: Sending raw command: {' '.join(raw_command)}")
            # Do not set content here; let events handle logging
        elif message.lower().startswith('/unban '):
            if not is_channel:
                continue  # Skip channel-specific commands in PMs
            unban_cmd = message.split(None, 1)[1].strip() if len(message.split(None, 1)) > 1 else ''
            parts = unban_cmd.split(None, 1)
            if parts and parts[0].startswith('#'):
                target_str = parts[1] if len(parts) > 1 else ''
            else:
                target_str = unban_cmd
            target = sanitize_input(target_str.strip())
            if not target:
                continue  # Skip invalid
            if '!' not in target and '@' not in target:
                mask = f"{target}!*@*"
            else:
                mask = target
            raw_command = ['MODE', channel, '-b', mask]
            bot.write(raw_command)
            print(f"DEBUG: Sending raw command: {' '.join(raw_command)}")
            # Do not set content here; let events handle logging
        elif message.lower().startswith('/password '):
            if not is_channel:
                continue  # Skip channel-specific commands in PMs
            password_cmd = message.split(None, 1)[1].strip() if len(message.split(None, 1)) > 1 else ''
            parts = password_cmd.split(None, 1)
            if parts and parts[0].startswith('#'):
                key = sanitize_input(parts[1] if len(parts) > 1 else '')
            else:
                key = sanitize_input(password_cmd)
            if not key:
                continue  # Skip invalid
            raw_command = ['MODE', channel, '+k', key]
            bot.write(raw_command)
            print(f"DEBUG: Sending raw command: {' '.join(raw_command)}")
            # Do not set content here; let events handle logging
        elif message.lower().startswith('/msg '):
            msg_cmd = message.split(None, 2)
            if len(msg_cmd) < 3:
                continue  # Skip invalid
            target_user = sanitize_input(msg_cmd[1].strip())
            msg_text = sanitize_input(' '.join(msg_cmd[2:]))
            if not target_user or not msg_text:
                continue  # Skip invalid
            bot.say(msg_text, target_user)  # Korrigiert: Verwende bot.say statt bot.msg
            content = msg_text
            log_channel = target_user  # Log under the target's nick as channel
            ensure_active_pchat(bot, target_user)
        elif message.startswith('/me '):
            action_text = sanitize_input(message.lstrip('/me ').strip())
            bot.action(action_text, channel)
            content = f"* {bot.nick} {action_text}"
        elif message.startswith('/nick '):
            new_nick = sanitize_input(message.lstrip('/nick ').strip())
            if not new_nick:
                continue  # Skip invalid
            bot.write(['NICK', new_nick])
            content = f"* Changed nick to {new_nick}"
        elif message.startswith('/join '):
            if not is_channel:
                continue  # /join is for channels
            join_channel = sanitize_input(message.lstrip('/join ').strip())
            if not join_channel.startswith('#'):
                continue  # Skip invalid
            bot.join(join_channel)
            content = f"* Joined {join_channel}"
        else:
            bot.say(message, channel)
            content = message
        if content is not None:
            # Logge in gleicher Connection
            timestamp = datetime.now().isoformat()
            sender = bot.nick
            log_ch = log_channel if 'log_channel' in locals() else channel
            cursor.execute('''
                INSERT INTO messages (timestamp, sender, channel, content)
                VALUES (?, ?, ?, ?)
            ''', (timestamp, sender, log_ch, content))
        cursor.execute('UPDATE pending_messages SET sent = 1 WHERE id = ?', (id_,))
    conn.commit()
    conn.close()

# Cleanup pending_messages: Lösche sent=1 älter als 60s
@sopel.plugin.interval(60)
def cleanup_pending(bot):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    old = time.time() - 60
    cursor.execute('DELETE FROM pending_messages WHERE sent = 1 AND timestamp < ?', (old,))
    conn.commit()
    conn.close()

# User-List Update: Periodisch (alle 5 Sekunden) aktualisieren
@sopel.plugin.interval(5)
def update_user_list(bot):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for channel in bot.channels:
        if not channel.startswith('#'):
            continue
        users = list(bot.channels[channel].users.keys())
        cursor.execute('DELETE FROM channel_users WHERE channel = ?', (channel,))
        for nick in users:
            priv = bot.channels[channel].privileges.get(nick, 0)
            flags = []
            if priv & sopel.plugin.VOICE:
                flags.append('+v')
            if priv & sopel.plugin.HALFOP:
                flags.append('+h')
            if priv & sopel.plugin.OP:
                flags.append('+o')
            if priv & sopel.plugin.ADMIN:
                flags.append('+a')
            if priv & sopel.plugin.OWNER:
                flags.append('+q')
            flags_str = ' '.join(flags)
            cursor.execute('''
                INSERT OR IGNORE INTO channel_users (channel, nick, flags)
                VALUES (?, ?, ?)
            ''', (channel, nick, flags_str))
    conn.commit()
    conn.close()
