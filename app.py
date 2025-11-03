from __future__ import annotations

import os
import re
from datetime import datetime

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from task_tracker.database import Task, TaskRepository

BOT_MENTION_PATTERN = re.compile(r"<@([A-Z0-9]+)>")
CHECKBOX_ACTION_PATTERN = re.compile(r"task_checkboxes_(\d+)")


class SlackTaskTracker:
    def __init__(self) -> None:
        self.repo = TaskRepository(os.environ.get("TASK_DB_PATH", "tasks.db"))
        self.app = App(
            token=self._require_env("SLACK_BOT_TOKEN"),
            signing_secret=self._require_env("SLACK_SIGNING_SECRET"),
        )
        self.bot_user_id: str | None = None
        self.tasks_channel = self._require_env("TASKS_CHANNEL")
        self._register_handlers()

    def _require_env(self, key: str) -> str:
        value = os.environ.get(key)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {key}")
        return value

    def _register_handlers(self) -> None:
        @self.app.event("app_mention")
        def handle_app_mention(event, say, client, context):
            self.bot_user_id = context.get("bot_user_id")
            channel = event.get("channel")
            if channel is None:
                return

            text = event.get("text", "")
            author_id = event.get("user")
            developer_id, project_manager_id, description = self._parse_task_request(text)
            if developer_id is None:
                say(
                    text=(
                        "Please mention a developer when creating a task, "
                        "e.g. `@bot @developer @pm Implement feature`."
                    ),
                    channel=channel,
                )
                return
            if author_id is None:
                say(text="Unable to determine who created the task.", channel=channel)
                return

            task = self.repo.create_task(
                description,
                developer_id,
                project_manager_id,
                self.tasks_channel,
            )
            response = self._post_task_message(client, task)
            if response:
                self.repo.update_message_reference(task.id, response["channel"], response["ts"])
                say(text=f"Task #{task.id} created in <#{self.tasks_channel}>.", channel=channel)

        @self.app.command("/tasks")
        def handle_tasks_command(ack, respond, command):
            ack()
            text = (command.get("text") or "").strip()
            if text.lower().startswith("delete"):
                parts = text.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    respond("Usage: /tasks delete <task_id>")
                    return
                task_id = int(parts[1])
                try:
                    task = self.repo.get_task(task_id)
                except KeyError:
                    respond(f"Task #{task_id} not found.")
                    return

                user_id = command.get("user_id")
                if user_id not in {task.developer_id, task.project_manager_id}:
                    respond(
                        "Only the assigned developer or project manager can delete this task."
                    )
                    return

                try:
                    self.repo.delete_task(task_id)
                except KeyError:
                    respond(f"Task #{task_id} not found.")
                    return

                client = self.app.client
                self._delete_task_message(client, task)
                self._send_channel_notification(
                    client,
                    task.channel_id,
                    f"Task #{task_id} was deleted by <@{user_id}>.",
                )
                respond(f"Task #{task_id} deleted.")
                return
            if text.lower().startswith("show"):
                parts = text.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    respond("Usage: /tasks show <task_id>")
                    return
                task_id = int(parts[1])
                try:
                    task = self.repo.get_task(task_id)
                except KeyError:
                    respond(f"Task #{task_id} not found.")
                    return
                respond(self._format_task_details(task))
                return

            status = None
            if text.lower() in {"completed", "complete"}:
                status = "completed"
            elif text.lower() in {"pending", "open", "incomplete"}:
                status = "pending"

            tasks = self.repo.list_tasks(status=status)
            if not tasks:
                respond("No tasks found.")
                return

            blocks: list[dict] = []
            for index, task in enumerate(tasks):
                blocks.extend(self._task_summary_blocks(task))
                if index < len(tasks) - 1:
                    blocks.append({"type": "divider"})

            respond(blocks=blocks)

        @self.app.action(CHECKBOX_ACTION_PATTERN)
        def handle_checkbox_action(ack, body, client):
            ack()
            action = body["actions"][0]
            match = CHECKBOX_ACTION_PATTERN.match(action["action_id"])
            if not match:
                return
            task_id = int(match.group(1))
            task = self.repo.get_task(task_id)

            selected_values = {opt["value"] for opt in action.get("selected_options", [])}
            developer_checked = f"{task_id}|developer" in selected_values
            project_manager_checked = f"{task_id}|pm" in selected_values

            user_id = body["user"]["id"]
            previous_developer_checked = task.developer_checked
            previous_pm_checked = task.project_manager_checked

            if developer_checked != previous_developer_checked and user_id != task.developer_id:
                self._send_ephemeral(
                    client,
                    task.channel_id,
                    user_id,
                    "Only the assigned developer can toggle their checkbox.",
                )
                developer_checked = previous_developer_checked
            if project_manager_checked != previous_pm_checked and user_id != task.project_manager_id:
                self._send_ephemeral(
                    client,
                    task.channel_id,
                    user_id,
                    "Only the project manager can toggle their checkbox.",
                )
                project_manager_checked = previous_pm_checked

            updated_task = self.repo.update_checkmarks(task_id, developer_checked, project_manager_checked)
            self._update_task_message(client, updated_task)

            if developer_checked and not previous_developer_checked:
                self._send_channel_notification(
                    client,
                    updated_task.channel_id,
                    f"Developer <@{task.developer_id}> marked task #{task_id} complete.",
                )
            if project_manager_checked and not previous_pm_checked:
                self._send_channel_notification(
                    client,
                    updated_task.channel_id,
                    f"Project manager <@{task.project_manager_id}> approved task #{task_id}.",
                )
            if updated_task.completed_at and not task.completed_at:
                self._send_channel_notification(
                    client,
                    updated_task.channel_id,
                    f"Task #{task_id} is now fully completed.",
                )

    def _send_ephemeral(self, client: WebClient, channel: str, user: str, text: str) -> None:
        try:
            client.chat_postEphemeral(channel=channel, user=user, text=text)
        except SlackApiError:
            pass

    def _send_channel_notification(self, client: WebClient, channel: str, text: str) -> None:
        try:
            client.chat_postMessage(channel=channel, text=text)
        except SlackApiError:
            pass

    def _delete_task_message(self, client: WebClient, task: Task) -> None:
        if not task.message_ts:
            return
        try:
            client.chat_delete(channel=task.channel_id, ts=task.message_ts)
        except SlackApiError as exc:
            print(f"Failed to delete task message: {exc}")

    def _parse_task_request(
        self, text: str
    ) -> tuple[str | None, str | None, str]:
        mention_ids = BOT_MENTION_PATTERN.findall(text)
        description = BOT_MENTION_PATTERN.sub("", text).strip()

        assignee_mentions: list[str] = []
        for user_id in mention_ids:
            if user_id == self.bot_user_id:
                continue
            assignee_mentions.append(user_id)

        developer_id = assignee_mentions[0] if assignee_mentions else None
        if len(assignee_mentions) > 1:
            project_manager_id = assignee_mentions[1]
        else:
            project_manager_id = developer_id

        return developer_id, project_manager_id, description or "(no description provided)"

    def _task_summary_blocks(self, task: Task) -> list[dict]:
        status_label = ":white_check_mark: Completed" if task.completed_at else ":hourglass_flowing_sand: Pending"
        developer_status = (
            ":white_check_mark: Done" if task.developer_checked else ":hourglass_flowing_sand: Pending"
        )
        project_manager_status = (
            ":white_check_mark: Approved"
            if task.project_manager_checked
            else ":hourglass_flowing_sand: Awaiting review"
        )

        timeline_elements = [
            {"type": "mrkdwn", "text": f":calendar: Created {self._format_timestamp(task.created_at)}"}
        ]
        if task.completed_at:
            timeline_elements.append(
                {
                    "type": "mrkdwn",
                    "text": f":checkered_flag: Completed {self._format_timestamp(task.completed_at)}",
                }
            )

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Task #{task.id}", "emoji": True},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{status_label}\n*{task.description}*",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Developer*\n<@{task.developer_id}>"},
                    {"type": "mrkdwn", "text": f"*Project manager*\n<@{task.project_manager_id}>"},
                ],
            },
            {"type": "context", "elements": timeline_elements},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Developer status*\n{developer_status}"},
                    {"type": "mrkdwn", "text": f"*PM status*\n{project_manager_status}"},
                ],
            },
        ]

        return blocks

    def _format_task_details(self, task: Task) -> str:
        lines = [
            f"*Task #{task.id}*",
            f"Status: {'Completed' if task.completed_at else 'Pending'}",
            f"Developer: <@{task.developer_id}>",
            f"Project manager: <@{task.project_manager_id}>",
            f"Created: {self._format_timestamp(task.created_at)}",
        ]
        if task.completed_at:
            lines.append(f"Completed: {self._format_timestamp(task.completed_at)}")
        lines.append("")
        lines.append(task.description)
        return "\n".join(lines)

    def _format_timestamp(self, ts: str | None) -> str:
        if not ts:
            return "-"
        try:
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            return dt.strftime("%b %d, %Y %H:%M UTC")
        except ValueError:
            return ts

    def _build_task_blocks(self, task: Task) -> list[dict]:
        checkbox_options = [
            {
                "text": {"type": "mrkdwn", "text": f"Developer complete (<@{task.developer_id}>)"},
                "value": f"{task.id}|developer",
            },
            {
                "text": {
                    "type": "mrkdwn",
                    "text": f"Project manager approved (<@{task.project_manager_id}>)",
                },
                "value": f"{task.id}|pm",
            },
        ]

        checkboxes_element = {
            "type": "checkboxes",
            "action_id": f"task_checkboxes_{task.id}",
            "options": checkbox_options,
        }

        initial_options: list[dict] = []
        if task.developer_checked:
            initial_options.append(checkbox_options[0])
        if task.project_manager_checked:
            initial_options.append(checkbox_options[1])

        if initial_options:
            checkboxes_element["initial_options"] = initial_options

        summary_blocks = self._task_summary_blocks(task)
        extra_blocks = [
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": ":white_check_mark: Completion checklist"}],
            },
            {
                "type": "actions",
                "block_id": f"task-{task.id}-actions",
                "elements": [checkboxes_element],
            },
        ]

        return summary_blocks + extra_blocks

    def _post_task_message(self, client: WebClient, task: Task) -> dict | None:
        try:
            response = client.chat_postMessage(
                channel=self.tasks_channel,
                text=f"Task #{task.id}",
                blocks=self._build_task_blocks(task),
            )
            return {
                "channel": response.get("channel"),
                "ts": response.get("ts"),
            }
        except SlackApiError as exc:
            print(f"Failed to post task message: {exc}")
            return None

    def _update_task_message(self, client: WebClient, task: Task) -> None:
        if not task.message_ts:
            return
        try:
            client.chat_update(
                channel=task.channel_id,
                ts=task.message_ts,
                text=f"Task #{task.id}",
                blocks=self._build_task_blocks(task),
            )
        except SlackApiError as exc:
            print(f"Failed to update task message: {exc}")

    def start(self) -> None:
        handler = SocketModeHandler(self.app, self._require_env("SLACK_APP_TOKEN"))
        handler.start()


def main() -> None:
    SlackTaskTracker().start()


if __name__ == "__main__":
    main()

