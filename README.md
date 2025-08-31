# sopel-dbcontrol

A Sopel IRC bot plugin that exposes full IRC chat functionality via a SQLite database interface. This allows building custom IRC client applications or integrations by interacting solely with the database—no direct IRC protocol handling required. The plugin logs chat events, user states, and enables sending messages/commands through database inserts.

Designed as a foundation for commercial or open-source IRC clients, it provides a decoupled architecture where your application can query and manipulate IRC data asynchronously.

## Purpose

The primary goal of `sopel-dbcontrol` is to serve as a backend for IRC-based chat applications. By using a shared SQLite database as the sole interface, it enables:
- Real-time monitoring of channels, users, and private chats.
- Logging of all chat activities (messages, joins, parts, modes, topics, etc.).
- Sending IRC commands, channel messages, or private messages via simple database inserts.
- Virtual management of "open" private chat sessions for displaying conversation histories.

This plugin runs on a Sopel IRC bot instance, handling all IRC connections and events in the background. Your custom application (e.g., a web/mobile/desktop client) can poll or react to database changes to build a full-featured IRC experience.

## Features

### Database Schema
The plugin uses a SQLite database (configurable path, default: `/home/ircbot/.sopel/chat.db`) with the following tables:

- **channel_users**: Stores current users in joined channels and their privileges/flags.
  - Fields:
    - `channel` (TEXT): Channel name (e.g., `#mychannel`).
    - `nick` (TEXT): User's nickname.
    - `flags` (TEXT): Space-separated IRC flags (e.g., `+o +v` for operator and voice).

- **messages**: Logs all chat events, messages, and system notifications.
  - Fields:
    - `id` (INTEGER PRIMARY KEY): Auto-incremented ID.
    - `timestamp` (TEXT): ISO-formatted timestamp.
    - `sender` (TEXT): Sender's nick or 'SYSTEM' for events.
    - `channel` (TEXT): Channel name or user nick (for private messages).
    - `content` (TEXT): Message content or event description (e.g., `* User joined #channel`).

- **active_pchats**: Tracks "open" private chat sessions.
  - Fields:
    - `id` (INTEGER PRIMARY KEY): Auto-incremented ID.
    - `botuser` (TEXT): Bot's nickname.
    - `user` (TEXT): User's nickname.
  - An entry is created when a private message is sent or received. Deleting an entry "closes" the chat virtually, allowing your app to manage UI windows. History remains in `messages` for reloading if reopened.

- **pending_messages**: Queue for outgoing commands/messages.
  - Fields:
    - `id` (INTEGER PRIMARY KEY): Auto-incremented ID.
    - `channel` (TEXT): Target channel (e.g., `#mychannel`) or bot's nick for private messages.
    - `message` (TEXT): Command or message (e.g., `/msg Targetuser Hello`).
    - `sent` (INTEGER): 0 (pending) or 1 (sent).
    - `timestamp` (REAL): Unix timestamp for cleanup.

The plugin automatically sets up these tables on load.

### Querying Data
Your application can query the database to fetch:
- **Current Channel Users and Flags**: `SELECT * FROM channel_users WHERE channel = '#mychannel';` (Updated every 5 seconds.)
- **Chat History**: `SELECT * FROM messages WHERE channel = '#mychannel' ORDER BY timestamp ASC;`
- **Active Private Chats**: `SELECT * FROM active_pchats WHERE botuser = 'YourBotNick';`
- **Private Chat History**: `SELECT * FROM messages WHERE channel = 'TargetUser' ORDER BY timestamp ASC;` (Private messages are logged under the user's nick as `channel`.)

No automatic cleanup in `messages`—your app controls retention via direct DB deletes.

### Sending Commands and Messages
Insert into `pending_messages` to queue actions. The plugin processes them every 5 seconds:
- Use `channel` as the target channel (for channel-specific commands) or your bot's nick (for private messages or global commands).
- Prefix with IRC-style commands like `/msg`, `/mode`, etc.

Examples (SQL inserts):
- Send an action in a channel: `INSERT INTO pending_messages (channel, message) VALUES ('#mychannel', '/me is very happy, because now I am feature complete.');`
- Send a private message: `INSERT INTO pending_messages (channel, message) VALUES ('YourBotNick', '/msg Targetuser this is a private message');`
- Set channel topic: `INSERT INTO pending_messages (channel, message) VALUES ('#mychannel', '/topic This shall be the channel topic');`
- Give operator status: `INSERT INTO pending_messages (channel, message) VALUES ('#mychannel', '/mode +o Targetuser');`
- Kick a user: `INSERT INTO pending_messages (channel, message) VALUES ('#mychannel', '/kick Targetuser ');`
- Ban a user: `INSERT INTO pending_messages (channel, message) VALUES ('#mychannel', '/ban Targetuser ');` (Uses `*!*@*` mask if no host provided.)
- Join a new channel: `INSERT INTO pending_messages (channel, message) VALUES ('#mychannel', '/join #newchannel');` (Can use any joined channel as base.)
- Set channel password: `INSERT INTO pending_messages (channel, message) VALUES ('#mychannel', '/password MyChannelPW');` (Translates to `/MODE #mychannel +k MyChannelPW`.)

Processed entries are marked `sent=1` and cleaned up after 60 seconds.

### Additional Behaviors
- **Rate Limiting**: Prevents spam (10 msgs/10s, 50 msgs/60s; 3-min ban on violation).
- **Filtering**: Skips logging URLs, commands starting with `!`, or control characters.
- **Private Chat Management**: Automatically creates `active_pchats` entries on PM send/receive. Your app can delete them to "close" chats.
- **Event Logging**: Captures joins, parts, quits, kicks, modes, topics, etc., as formatted strings in `messages`.

## Installation

1. Install Sopel: `pip install sopel`
2. Place `dbcontrol.py` in your Sopel's plugins directory (e.g., `~/.sopel/plugins/`).
3. Configure Sopel to load the plugin in your config file (e.g., `default.cfg`):
   ```
[core]
commands_on_connect = 
	set irc_hide_version 1
enable = dbcontrol
exclude = 
	xkcd
	wiktionary
	wikipedia
	version
	url
	uptime
	units
	unicode_info
	translate
	tld
	tell
	seen
	search
	safety
	reload
	rand
	pronouns
	ping
	lmgtfy
	isup
	invite
	help
	find_updates
	find
	emoticons
	dice
	currency
	countdown
	clock
	choose
	calc
	announce
	adminchannel
	admin
   ```
4. Set the DB path in the plugin code if needed: `DB_PATH = '/path/to/your/chat.db'`
5. Run Sopel: `sopel -c default.cfg`

Ensure your application has read/write access to the same SQLite DB.

## Usage in Your Application

- **Polling Loop**: Periodically query tables like `messages` for updates (use timestamps for deltas).
- **UI Integration**: Display active private chats from `active_pchats`, load histories from `messages`, and "close" by deleting entries.
- **Sending**: Insert into `pending_messages` and wait ~5s for processing.
- **Error Handling**: Log failed commands (e.g., missing op privileges) appear in `messages` as system events.

This setup allows building a complete IRC client without direct IRC socket management—ideal for web-based or multi-user apps.

## License

This plugin is licensed under the Eiffel Forum License v2.0 (EFL-2.0). See [LICENSE](LICENSE) for details.

THIS PACKAGE IS PROVIDED "AS IS" AND WITHOUT ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, WITHOUT LIMITATION, THE IMPLIED WARRANTIES OF MERCHANTIBILITY AND FITNESS FOR A PARTICULAR PURPOSE.

## Contributing

Contributions welcome! Fork the repo, make changes, and submit a pull request. Focus on maintaining the DB-centric interface.

## Contact

For issues or suggestions, open a GitHub issue.
