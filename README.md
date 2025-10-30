# Task Tracker Slack Bot

This project provides a Slack bot built with [Slack Bolt](https://slack.dev/bolt-python/) that allows teams to create and track development tasks directly from Slack conversations.

## Features

- Mention the bot together with a teammate (developer) **and** the project manager to create a task from any channel.
- Automatically posts task details to a dedicated task channel with interactive checkboxes.
- Separate completion checkboxes for the assigned developer and the project manager.
- Automatic notifications when either party marks their checkbox and when the task is fully completed.
- Slash command (`/tasks`) to list tasks, filter by status, or show a specific task by ID.
- SQLite storage to persist task data, including creation and completion timestamps.

## Requirements

- Python 3.11+
- A Slack app configured with the following scopes:
  - `app_mentions:read`
  - `chat:write`
  - `commands`
  - `im:write`
  - `reactions:write`
  - `users:read`
- A Slack bot token, signing secret, and Socket Mode app-level token.

## Installation

1. Clone the repository and install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Create a Slack app (if you do not already have one) and enable Socket Mode. Add the bot to the workspace and subscribe to the `app_mention` event. Configure the `/tasks` slash command to point to your Socket Mode app.

3. Export the required environment variables:

   ```bash
   export SLACK_BOT_TOKEN=xoxb-...
   export SLACK_SIGNING_SECRET=...
   export SLACK_APP_TOKEN=xapp-...
   export TASKS_CHANNEL=C1234567890  # Channel ID where tasks should be posted
   # Optional: override the default SQLite database path
   export TASK_DB_PATH=/path/to/tasks.db
   ```

4. Start the bot:

   ```bash
   python app.py
   ```

The bot listens for mentions and the `/tasks` slash command. When you tag the bot and include mentions for both the developer and the project manager in your message (e.g., `@taskbot @alex @casey Finish the API docs`), a new task is created and posted to the configured task channel.

## Usage

- **Create a task:** mention the bot, the developer, and the project manager in a message (e.g., `@taskbot @alex @casey Finish the API docs`). The project manager must be different from the task creator.
- **Mark developer completion:** the assigned developer clicks their checkbox in the task message. The bot announces their update in the task channel.
- **Mark project manager approval:** the project manager checks their box to approve the task. The bot announces their approval and, if both boxes are checked, marks the task as completed.
- **List tasks:** use `/tasks` to list all tasks, `/tasks completed` to show completed tasks, `/tasks pending` for outstanding tasks, or `/tasks show <id>` for detailed information about a specific task.

All task updates are persisted in the SQLite database, including timestamps for creation and completion.

## Development

The project is intentionally lightweight. Feel free to customize the database schema, message formatting, or Slack interactions to match your workflow.

